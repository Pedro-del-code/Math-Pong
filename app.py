import os
import uuid
import math
import random
import threading
import time
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from supabase import create_client, Client

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'mathpong-secret-2024')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ── SUPABASE ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', '')
supabase: Client = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

def db_get_or_create_player(name, turma):
    if not supabase:
        return {'id': str(uuid.uuid4()), 'name': name, 'turma': turma, 'wins': 0}
    try:
        res = supabase.table('players').select('*').eq('name', name).eq('turma', turma).execute()
        if res.data:
            return res.data[0]
        new = supabase.table('players').insert({'name': name, 'turma': turma, 'wins': 0}).execute()
        return new.data[0]
    except Exception as e:
        print(f'DB error: {e}')
        return {'id': str(uuid.uuid4()), 'name': name, 'turma': turma, 'wins': 0}

def db_add_win(player_id):
    if not supabase:
        return
    try:
        res = supabase.table('players').select('wins').eq('id', player_id).execute()
        if res.data:
            wins = res.data[0]['wins'] + 1
            supabase.table('players').update({'wins': wins}).eq('id', player_id).execute()
    except Exception as e:
        print(f'DB win error: {e}')

def db_get_leaderboard():
    if not supabase:
        return []
    try:
        res = supabase.table('players').select('name,turma,wins').order('wins', desc=True).limit(10).execute()
        return res.data or []
    except Exception as e:
        print(f'DB leaderboard error: {e}')
        return []

# ── GAME STATE ────────────────────────────────────────────────────────────────
BALL_SPEED   = 0.013
GAME_DURATION = 180  # 3 minutos em segundos
PADDLE_H     = 0.22
PADDLE_W     = 0.04
BALL_R       = 0.025

rooms   = {}          # room_id -> GameRoom
waiting = None        # socket_id esperando oponente
waiting_lock = threading.Lock()

class GameRoom:
    def __init__(self, room_id, p1_sid, p1_info, p2_sid, p2_info):
        self.room_id  = room_id
        self.players  = {
            p1_sid: {'idx': 0, 'info': p1_info, 'paddle_y': 0.0},
            p2_sid: {'idx': 1, 'info': p2_info, 'paddle_y': 0.0},
        }
        self.sids     = [p1_sid, p2_sid]
        self.scores   = [0, 0]
        self.running  = False
        self.math_active = False
        self.math_player = -1
        self.math_q   = None
        self.speed_mult = 1.0
        self.rally    = 0
        self.last_hit = -1
        self.time_left = GAME_DURATION
        self.reset_ball(random.choice([1, -1]))
        self.thread   = None

    def reset_ball(self, direction=1):
        angle = (random.random() * 0.5 + 0.2) * random.choice([1, -1])
        s = BALL_SPEED * self.speed_mult
        self.ball = {
            'x':  0.0, 'y': 0.0,
            'vx': s * direction,
            'vy': angle * s,
        }

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        TICK = 1 / 60
        while self.running:
            time.sleep(TICK)
            if not self.math_active:
                self.time_left -= TICK
                if self.time_left <= 0:
                    self.time_left = 0
                    self._end_game_by_time()
                    return
            self._update()
            state = self._state()
            state['time_left'] = int(self.time_left)
            socketio.emit('game_state', state, room=self.room_id)

    def _update(self):
        b  = self.ball
        steps = 4
        for _ in range(steps):
            b['x'] += b['vx']
            b['y'] += b['vy']

            # Top/bottom bounce
            if b['y'] < -1 + BALL_R:
                b['y']  =  -1 + BALL_R
                b['vy'] =  abs(b['vy'])
            if b['y'] >  1 - BALL_R:
                b['y']  =   1 - BALL_R
                b['vy'] = -abs(b['vy'])

            p0y = self.players[self.sids[0]]['paddle_y']
            p1y = self.players[self.sids[1]]['paddle_y']

            # Left paddle P1
            if b['x'] < -0.9 + PADDLE_W + BALL_R and b['vx'] < 0 and self.last_hit != 0:
                if abs(b['y'] - p0y) < PADDLE_H + BALL_R:
                    b['x'] = -0.9 + PADDLE_W + BALL_R
                    relY   = (b['y'] - p0y) / PADDLE_H
                    speed  = math.sqrt(b['vx']**2 + b['vy']**2) * 1.01
                    angle  = relY * math.pi * 0.38
                    b['vx'] =  abs(math.cos(angle)) * speed
                    b['vy'] =  math.sin(angle) * speed
                    self.last_hit = 0
                    self.rally   += 1
                    if self.rally % 3 == 0:
                        self._trigger_math(0)

            # Right paddle P2
            if b['x'] >  0.9 - PADDLE_W - BALL_R and b['vx'] > 0 and self.last_hit != 1:
                if abs(b['y'] - p1y) < PADDLE_H + BALL_R:
                    b['x'] =  0.9 - PADDLE_W - BALL_R
                    relY   = (b['y'] - p1y) / PADDLE_H
                    speed  = math.sqrt(b['vx']**2 + b['vy']**2) * 1.01
                    angle  = relY * math.pi * 0.38
                    b['vx'] = -abs(math.cos(angle)) * speed
                    b['vy'] =  math.sin(angle) * speed
                    self.last_hit = 1
                    self.rally   += 1
                    if self.rally % 3 == 0:
                        self._trigger_math(1)

            # Score
            if b['x'] < -1.05:
                self._point(1)
                return
            if b['x'] >  1.05:
                self._point(0)
                return

    def _point(self, player_idx):
        self.scores[player_idx] += 1
        self.rally    = 0
        self.last_hit = -1
        self.speed_mult = 1.0
        socketio.emit('score_update', {
            'scores': self.scores,
            'scorer': player_idx
        }, room=self.room_id)
        self.reset_ball(1 if player_idx == 1 else -1)

    def _end_game_by_time(self):
        self.running = False
        if self.scores[0] > self.scores[1]:
            winner_idx = 0
        elif self.scores[1] > self.scores[0]:
            winner_idx = 1
        else:
            winner_idx = random.choice([0, 1])  # empate: sorteio
        winner_sid  = self.sids[winner_idx]
        winner_info = self.players[winner_sid]['info']
        db_add_win(winner_info.get('id'))
        leaderboard = db_get_leaderboard()
        socketio.emit('game_over', {
            'winner_idx':   winner_idx,
            'winner_name':  winner_info['name'],
            'winner_turma': winner_info['turma'],
            'scores':       self.scores,
            'leaderboard':  leaderboard,
        }, room=self.room_id)

    def _trigger_math(self, player_idx):
        if self.math_active:
            return
        self.math_active = True
        self.math_player = player_idx
        q0 = self._gen_question()
        q1 = self._gen_question()
        self.math_questions = [q0, q1]
        self.math_answered  = [False, False]
        self.math_results   = [None, None]

        for i, sid in enumerate(self.sids):
            q = self.math_questions[i]
            socketio.emit('math_question', {
                'player_idx': player_idx,
                'for_me':     True,
                'question':   q['text'],
                'choices':    q['choices'],
            }, to=sid)

        def timeout():
            time.sleep(5)
            for i in range(2):
                if not self.math_answered[i]:
                    self._resolve_player_math(i, -1)
            self._finalize_math()
        threading.Thread(target=timeout, daemon=True).start()

    def _resolve_player_math(self, player_idx, chosen_idx):
        if self.math_answered[player_idx]:
            return
        self.math_answered[player_idx] = True
        q = self.math_questions[player_idx]
        correct = chosen_idx == q['correct_idx']
        feedback = 'correct' if correct else ('wrong' if chosen_idx != -1 else 'timeout')
        self.math_results[player_idx] = correct
        sid = self.sids[player_idx]
        socketio.emit('math_result', {
            'feedback':    feedback,
            'correct_idx': q['correct_idx'],
        }, to=sid)
        if all(self.math_answered):
            threading.Thread(target=self._finalize_math, daemon=True).start()

    def _finalize_math(self):
        if not self.math_active:
            return
        time.sleep(1.3)
        self.math_active = False
        r = self.math_results
        if r[0] is True and r[1] is True:
            self.speed_mult = max(0.7, self.speed_mult * 0.92)
        elif r[0] is False and r[1] is False:
            self.speed_mult = min(1.8, self.speed_mult * 1.15)
        elif r[0] is True and r[1] is not True:
            self.speed_mult = max(0.7, self.speed_mult * 0.85)
        elif r[1] is True and r[0] is not True:
            self.speed_mult = min(1.8, self.speed_mult * 1.2)
        b = self.ball
        spd = math.sqrt(b['vx']**2 + b['vy']**2)
        new_spd = BALL_SPEED * self.speed_mult
        if spd > 0:
            ratio = new_spd / spd
            b['vx'] *= ratio
            b['vy'] *= ratio

    def _gen_question(self):
        ops = ['+', '-', '×', '÷']
        diff = min(self.rally // 6, 3)
        if diff == 0:
            a  = random.randint(1, 10)
            b  = random.randint(1, 10)
            op = random.choice(['+', '-'])
        elif diff == 1:
            a  = random.randint(1, 20)
            b  = random.randint(1, 20)
            op = random.choice(['+', '-', '×'])
        else:
            a  = random.randint(5, 50)
            b  = random.randint(2, 15)
            op = random.choice(ops)

        if op == '+':   answer = a + b
        elif op == '-': answer = a - b
        elif op == '×': answer = a * b
        else:
            answer = a
            a = a * b
            op = '÷'

        wrongs = set()
        while len(wrongs) < 3:
            delta = random.randint(1, 8)
            w = answer + (delta if random.random() < 0.5 else -delta)
            if w != answer:
                wrongs.add(w)

        choices = list(wrongs) + [answer]
        random.shuffle(choices)
        return {
            'text':        f'{a} {op} {b} = ?',
            'choices':     choices,
            'correct_idx': choices.index(answer),
        }

    def _state(self):
        return {
            'ball':    self.ball,
            'paddle0': self.players[self.sids[0]]['paddle_y'],
            'paddle1': self.players[self.sids[1]]['paddle_y'],
            'scores':  self.scores,
        }

    def move_paddle(self, sid, y):
        if sid in self.players:
            y = max(-1 + PADDLE_H, min(1 - PADDLE_H, y))
            self.players[sid]['paddle_y'] = y

    def answer_math(self, sid, chosen_idx):
        if sid in self.players and self.math_active:
            player_idx = self.players[sid]['idx']
            threading.Thread(target=self._resolve_player_math, args=(player_idx, chosen_idx), daemon=True).start()

    def remove_player(self, sid):
        self.running = False
        socketio.emit('opponent_left', {}, room=self.room_id)

# ── HTTP ROUTES ───────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/leaderboard')
def leaderboard():
    data = db_get_leaderboard()
    return jsonify(data)

# ── SOCKET EVENTS ─────────────────────────────────────────────────────────────
@socketio.on('join_queue')
def on_join_queue(data):
    global waiting
    sid   = request.sid
    name  = data.get('name', 'Anônimo').strip()[:30]
    turma = data.get('turma', '').strip()[:20]
    player_info = db_get_or_create_player(name, turma)

    with waiting_lock:
        if waiting and waiting['sid'] != sid:
            w = waiting
            waiting = None
            room_id = str(uuid.uuid4())[:8]
            join_room(room_id, sid=w['sid'])
            join_room(room_id, sid=sid)

            room = GameRoom(
                room_id,
                w['sid'], w['info'],
                sid,      player_info,
            )
            rooms[room_id] = room

            socketio.emit('match_found', {
                'room_id':    room_id,
                'player_idx': 0,
                'opponent':   player_info['name'],
                'turma':      player_info['turma'],
            }, to=w['sid'])
            socketio.emit('match_found', {
                'room_id':    room_id,
                'player_idx': 1,
                'opponent':   w['info']['name'],
                'turma':      w['info']['turma'],
            }, to=sid)

            room.start()
        else:
            waiting = {'sid': sid, 'info': player_info}
            emit('waiting_for_opponent', {})

@socketio.on('move_paddle')
def on_move_paddle(data):
    sid     = request.sid
    room_id = data.get('room_id')
    y       = float(data.get('y', 0))
    if room_id in rooms:
        rooms[room_id].move_paddle(sid, y)

@socketio.on('answer_math')
def on_answer_math(data):
    sid     = request.sid
    room_id = data.get('room_id')
    idx     = int(data.get('idx', -1))
    if room_id in rooms:
        rooms[room_id].answer_math(sid, idx)

@socketio.on('disconnect')
def on_disconnect():
    global waiting
    sid = request.sid
    with waiting_lock:
        if waiting and waiting['sid'] == sid:
            waiting = None
    for room_id, room in list(rooms.items()):
        if sid in room.players:
            room.remove_player(sid)
            del rooms[room_id]
            break

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
