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
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("[DB] Supabase conectado.")
    except Exception as e:
        print(f"[DB] Falha ao conectar Supabase: {e}")


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
        print(f'[DB] get_or_create_player error: {e}')
        return {'id': str(uuid.uuid4()), 'name': name, 'turma': turma, 'wins': 0}


def db_add_win(player_id):
    if not supabase or not player_id:
        return
    try:
        res = supabase.table('players').select('wins').eq('id', player_id).execute()
        if res.data:
            wins = res.data[0]['wins'] + 1
            supabase.table('players').update({'wins': wins}).eq('id', player_id).execute()
    except Exception as e:
        print(f'[DB] add_win error: {e}')


def db_get_leaderboard():
    if not supabase:
        return []
    try:
        res = (supabase.table('players')
               .select('name,turma,wins')
               .order('wins', desc=True)
               .limit(10)
               .execute())
        return res.data or []
    except Exception as e:
        print(f'[DB] leaderboard error: {e}')
        return []


# ── CONSTANTES DE FÍSICA ──────────────────────────────────────────────────────
BALL_SPEED_BASE  = 0.009
BALL_SPEED_MAX   = 0.018
GAME_DURATION    = 180
PADDLE_H         = 0.22
PADDLE_W         = 0.04
BALL_R           = 0.025
BALL_RESET_DELAY = 1.5

# ── FILA / SALAS ──────────────────────────────────────────────────────────────
rooms        = {}
waiting      = None
waiting_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
class GameRoom:

    QUESTION_TIME = {
        'soma': 5, 'sub': 5, 'mult': 6,
        'mmc': 8,  'mdc': 8, 'fracao': 9,
        'fracao_soma': 9, 'fracao_sub': 9,
        'potencia': 6, 'raiz': 6,
        'porcentagem': 8, 'regra3': 10,
        'equacao': 12, 'equacao2': 15,
        'pa': 14, 'area': 10,
        'log_basico': 10, 'log': 12,
        'trig': 10, 'det2x2': 12,
        'derivada': 13,
    }

    WINNER_EFFECTS = [
        {'id': 'slow_ball',  'label': '🐢 Bola lenta!',           'client_id': 'slow_ball'},
        {'id': 'fast_opp',   'label': '⚡ Inimigo acelerado!',     'client_id': 'fast_enemy'},
        {'id': 'big_paddle', 'label': '🏓 Raquete gigante!',       'client_id': 'big_paddle'},
        {'id': 'tiny_opp',   'label': '🔬 Raquete minúscula!',     'client_id': 'small_enemy'},
        {'id': 'reverse',    'label': '🔄 Controles invertidos!',  'client_id': 'reverse_enemy'},
        {'id': 'freeze',     'label': '🧊 Congelar bola!',         'client_id': 'freeze_ball'},
    ]

    def __init__(self, room_id, p1_sid, p1_info, p2_sid, p2_info, level=6):
        self.room_id  = room_id
        self.level    = max(6, min(12, level))
        self.players  = {
            p1_sid: {'idx': 0, 'info': p1_info, 'paddle_y': 0.0},
            p2_sid: {'idx': 1, 'info': p2_info, 'paddle_y': 0.0},
        }
        self.sids           = [p1_sid, p2_sid]
        self.scores         = [0, 0]
        self.running        = False
        self.math_active    = False
        self.math_answered  = [False, False]
        self.math_results   = [None, None]
        self.math_questions = [None, None]
        self._finalizing    = False
        self.speed_mult     = 1.0
        self.paddle_scales  = [1.0, 1.0]
        self.rally          = 0
        self.last_hit       = -1
        self.time_left      = float(GAME_DURATION)
        self._lock          = threading.Lock()
        self.ball           = {}
        self.reset_ball(random.choice([1, -1]))
        self.thread         = None

    # ── BOLA ─────────────────────────────────────────────────────────────────

    def reset_ball(self, direction=1):
        angle = (random.random() * 0.45 + 0.15) * random.choice([1, -1])
        spd   = BALL_SPEED_BASE * self.speed_mult
        self.ball = {
            'x': 0.0, 'y': 0.0,
            'vx': spd * direction,
            'vy': angle * spd,
        }

    def _clamp_ball_speed(self):
        b   = self.ball
        spd = math.sqrt(b['vx'] ** 2 + b['vy'] ** 2)
        cap = min(BALL_SPEED_MAX, BALL_SPEED_BASE * self.speed_mult * 1.8)
        if spd > cap:
            r = cap / spd
            b['vx'] *= r
            b['vy'] *= r

    def _relaunch(self, toward_loser_idx=None):
        direction = (1 if toward_loser_idx == 1 else -1) if toward_loser_idx is not None else random.choice([1, -1])
        self.reset_ball(direction)

    # ── LOOP ─────────────────────────────────────────────────────────────────

    def start(self):
        self.running = True
        self.thread  = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        TICK = 1 / 60
        _broadcast_acc = 0.0
        BROADCAST_INTERVAL = 1 / 30  # envia state ao cliente em 30Hz; física roda em 60Hz

        while self.running:
            t0 = time.perf_counter()

            if not self.math_active:
                self.time_left -= TICK
                if self.time_left <= 0:
                    self.time_left = 0
                    self._end_game_by_time()
                    return

            if not self.math_active:
                self._update()

            _broadcast_acc += TICK
            if _broadcast_acc >= BROADCAST_INTERVAL:
                _broadcast_acc -= BROADCAST_INTERVAL
                state = self._state()
                state['time_left'] = int(self.time_left)
                socketio.emit('game_state', state, room=self.room_id)

            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, TICK - elapsed))

    def _update(self):
        b = self.ball
        self._clamp_ball_speed()

        b['x'] += b['vx']
        b['y'] += b['vy']

        # Paredes
        if b['y'] < -1 + BALL_R:
            b['y']  = -1 + BALL_R
            b['vy'] = abs(b['vy'])
        elif b['y'] > 1 - BALL_R:
            b['y']  = 1 - BALL_R
            b['vy'] = -abs(b['vy'])

        p0y = self.players[self.sids[0]]['paddle_y']
        p1y = self.players[self.sids[1]]['paddle_y']

        # Raquete P0 (esquerda)
        s0 = self.paddle_scales[0]
        hx0 = -0.9 + PADDLE_W + BALL_R
        if b['x'] <= hx0 and b['vx'] < 0 and self.last_hit != 0:
            if abs(b['y'] - p0y) < PADDLE_H * s0 + BALL_R:
                b['x'] = hx0
                relY   = max(-1.0, min(1.0, (b['y'] - p0y) / (PADDLE_H * s0)))
                angle  = relY * math.pi * 0.38
                spd    = BALL_SPEED_BASE * self.speed_mult
                b['vx'] =  abs(math.cos(angle)) * spd
                b['vy'] =  math.sin(angle) * spd
                self.last_hit = 0
                self.rally   += 1
                if self.rally % 3 == 0 and not self.math_active:
                    self._trigger_math(0)

        # Raquete P1 (direita)
        s1 = self.paddle_scales[1]
        hx1 = 0.9 - PADDLE_W - BALL_R
        if b['x'] >= hx1 and b['vx'] > 0 and self.last_hit != 1:
            if abs(b['y'] - p1y) < PADDLE_H * s1 + BALL_R:
                b['x'] = hx1
                relY   = max(-1.0, min(1.0, (b['y'] - p1y) / (PADDLE_H * s1)))
                angle  = relY * math.pi * 0.38
                spd    = BALL_SPEED_BASE * self.speed_mult
                b['vx'] = -abs(math.cos(angle)) * spd
                b['vy'] =  math.sin(angle) * spd
                self.last_hit = 1
                self.rally   += 1
                if self.rally % 3 == 0 and not self.math_active:
                    self._trigger_math(1)

        # Gol
        if b['x'] < -1.08:
            self._point(1)
        elif b['x'] > 1.08:
            self._point(0)

    # ── PLACAR ───────────────────────────────────────────────────────────────

    def _point(self, scorer_idx):
        with self._lock:
            self.scores[scorer_idx] += 1
            self.rally     = 0
            self.last_hit  = -1
            self.speed_mult = 1.0
            self.paddle_scales = [1.0, 1.0]

        socketio.emit('score_update', {
            'scores':        self.scores[:],
            'scorer':        scorer_idx,
            'reset_effects': True,
        }, room=self.room_id)

        socketio.emit('effect_reverse', {'player_idx': -1}, room=self.room_id)
        self.ball = {'x': 0.0, 'y': 0.0, 'vx': 0.0, 'vy': 0.0}

        def delayed_reset():
            time.sleep(BALL_RESET_DELAY)
            if self.running:
                # Bola vai em direção a quem tomou o gol (cria pressão)
                direction = 1 if scorer_idx == 1 else -1
                self.reset_ball(direction)

        threading.Thread(target=delayed_reset, daemon=True).start()

    def _end_game_by_time(self):
        self.running = False
        if self.scores[0] > self.scores[1]:
            winner_idx = 0
        elif self.scores[1] > self.scores[0]:
            winner_idx = 1
        else:
            winner_idx = random.choice([0, 1])

        winner_sid  = self.sids[winner_idx]
        winner_info = self.players[winner_sid]['info']
        db_add_win(winner_info.get('id'))
        leaderboard = db_get_leaderboard()

        socketio.emit('game_over', {
            'winner_idx':   winner_idx,
            'winner_name':  winner_info['name'],
            'winner_turma': winner_info.get('turma', ''),
            'scores':       self.scores[:],
            'leaderboard':  leaderboard,
        }, room=self.room_id)

    # ── MATEMÁTICA ───────────────────────────────────────────────────────────

    def _trigger_math(self, player_idx):
        if self.math_active:
            return
        self.math_active   = True
        self.math_answered = [False, False]
        self.math_results  = [None, None]
        self._finalizing   = False

        q0 = self._gen_question()
        q1 = self._gen_question()
        self.math_questions = [q0, q1]

        for i, sid in enumerate(self.sids):
            q = self.math_questions[i]
            socketio.emit('math_question', {
                'player_idx': i,
                'for_me':     True,
                'question':   q['text'],
                'choices':    q['choices'],
                'time_limit': q['time_limit'],
            }, to=sid)

        max_time = max(q0['time_limit'], q1['time_limit']) + 1

        def timeout_check():
            time.sleep(max_time)
            for i in range(2):
                if not self.math_answered[i]:
                    self._resolve_player_math(i, -1)

        threading.Thread(target=timeout_check, daemon=True).start()

    def _resolve_player_math(self, player_idx, chosen_idx):
        with self._lock:
            if self.math_answered[player_idx]:
                return
            self.math_answered[player_idx] = True

        q        = self.math_questions[player_idx]
        correct  = (chosen_idx != -1) and (chosen_idx == q['correct_idx'])
        feedback = 'correct' if correct else ('timeout' if chosen_idx == -1 else 'wrong')
        self.math_results[player_idx] = correct

        with self._lock:
            if correct:
                self.scores[player_idx] += 1
            else:
                self.scores[player_idx] = max(0, self.scores[player_idx] - 1)

        sid = self.sids[player_idx]
        socketio.emit('math_result', {
            'feedback':    feedback,
            'correct_idx': q['correct_idx'],
        }, to=sid)

        socketio.emit('score_update', {
            'scores':            self.scores[:],
            'scorer':            player_idx if correct else -1,
            'math_penalty':      not correct,
            'penalty_player':    player_idx if not correct else -1,
            'buff_player':       player_idx if correct else -1,
            'math_correct_buff': correct,
        }, room=self.room_id)

        if all(self.math_answered) and not self._finalizing:
            threading.Thread(target=self._finalize_math, daemon=True).start()

    def _finalize_math(self):
        with self._lock:
            if self._finalizing:
                return
            self._finalizing = True

        time.sleep(1.2)

        r0, r1     = self.math_results
        winner_idx = None

        if r0 is True and r1 is not True:
            winner_idx = 0
        elif r1 is True and r0 is not True:
            winner_idx = 1

        if winner_idx is not None:
            effect = random.choice(self.WINNER_EFFECTS)
            socketio.emit('roulette_spin', {
                'winner_player_idx': winner_idx,
                'effect_id':         effect['client_id'],
                'all_effects':       [e['label'] for e in self.WINNER_EFFECTS],
            }, room=self.room_id)

            time.sleep(4.8)  # aguarda animação da roleta
            self.math_active = False
            self._apply_effect(effect['id'], winner_idx)
        else:
            if r0 and r1:
                self.speed_mult = max(0.6, self.speed_mult * 0.88)
            elif not r0 and not r1:
                self.speed_mult = min(1.6, self.speed_mult * 1.12)
            self._relaunch()
            self.math_active = False

        self._finalizing = False

    # ── EFEITOS ──────────────────────────────────────────────────────────────

    def _apply_effect(self, effect_id, winner_idx):
        loser_idx = 1 - winner_idx

        if effect_id == 'slow_ball':
            self.speed_mult = max(0.35, self.speed_mult * 0.55)
            self._relaunch()
            def restore():
                time.sleep(6)
                self.speed_mult = min(1.0, self.speed_mult / 0.55)
            threading.Thread(target=restore, daemon=True).start()

        elif effect_id == 'fast_opp':
            self.speed_mult = min(BALL_SPEED_MAX / BALL_SPEED_BASE, self.speed_mult * 1.5)
            self._relaunch(toward_loser_idx=loser_idx)

        elif effect_id == 'big_paddle':
            self.paddle_scales[winner_idx] = 1.75
            socketio.emit('effect_paddle', {'size': 1.75, 'player_idx': winner_idx}, room=self.room_id)
            self._relaunch()
            wi = winner_idx
            def reset_big():
                time.sleep(8)
                self.paddle_scales[wi] = 1.0
                socketio.emit('effect_paddle', {'size': 1.0, 'player_idx': wi}, room=self.room_id)
            threading.Thread(target=reset_big, daemon=True).start()

        elif effect_id == 'tiny_opp':
            self.paddle_scales[loser_idx] = 0.38
            socketio.emit('effect_paddle', {'size': 0.38, 'player_idx': loser_idx}, room=self.room_id)
            self._relaunch()
            li = loser_idx
            def reset_tiny():
                time.sleep(8)
                self.paddle_scales[li] = 1.0
                socketio.emit('effect_paddle', {'size': 1.0, 'player_idx': li}, room=self.room_id)
            threading.Thread(target=reset_tiny, daemon=True).start()

        elif effect_id == 'reverse':
            socketio.emit('effect_reverse', {'player_idx': loser_idx}, room=self.room_id)
            self._relaunch()
            def reset_rev():
                time.sleep(6)
                socketio.emit('effect_reverse', {'player_idx': -1}, room=self.room_id)
            threading.Thread(target=reset_rev, daemon=True).start()

        elif effect_id == 'freeze':
            socketio.emit('effect_freeze', {}, room=self.room_id)
            self.ball = {'x': self.ball['x'], 'y': self.ball['y'], 'vx': 0.0, 'vy': 0.0}
            def unfreeze():
                time.sleep(2.5)
                self._relaunch(toward_loser_idx=loser_idx)
            threading.Thread(target=unfreeze, daemon=True).start()

    # ── ESTADO ───────────────────────────────────────────────────────────────

    def _state(self):
        return {
            'ball':    dict(self.ball),
            'paddle0': self.players[self.sids[0]]['paddle_y'],
            'paddle1': self.players[self.sids[1]]['paddle_y'],
            'scores':  self.scores[:],
        }

    def move_paddle(self, sid, y):
        if sid not in self.players:
            return
        y = max(-1.0 + PADDLE_H, min(1.0 - PADDLE_H, float(y)))
        self.players[sid]['paddle_y'] = y

    def answer_math(self, sid, chosen_idx):
        if sid in self.players and self.math_active:
            player_idx = self.players[sid]['idx']
            threading.Thread(
                target=self._resolve_player_math,
                args=(player_idx, chosen_idx),
                daemon=True
            ).start()

    def remove_player(self, sid):
        self.running = False
        socketio.emit('opponent_left', {}, room=self.room_id)

    # ── GERADOR DE QUESTÕES ──────────────────────────────────────────────────

    def _gen_question(self):
        lv = self.level

        def q_soma():
            a, b = random.randint(1, 60), random.randint(1, 60)
            return f'{a} + {b} = ?', a + b

        def q_sub():
            a = random.randint(10, 80)
            b = random.randint(1, a)
            return f'{a} - {b} = ?', a - b

        def q_mult():
            a, b = random.randint(2, 12), random.randint(2, 12)
            return f'{a} × {b} = ?', a * b

        def q_mmc():
            pairs = [(4,6),(6,9),(4,10),(3,5),(6,8),(5,10),(4,12),(6,10),(8,12),(9,12)]
            a, b = random.choice(pairs)
            return f'MMC({a},{b}) = ?', (a * b) // math.gcd(a, b)

        def q_mdc():
            pairs = [(12,8),(15,10),(18,12),(20,16),(24,18),(30,20),(36,24),(40,30)]
            a, b = random.choice(pairs)
            return f'MDC({a},{b}) = ?', math.gcd(a, b)

        def q_fracao():
            den   = random.choice([2, 3, 4, 5, 6, 8, 10])
            num_a = random.randint(1, den * 2)
            num_b = random.randint(1, num_a)
            return f'{num_a}/{den} - {num_b}/{den} = ?', num_a - num_b

        def q_fracao_soma():
            den = random.choice([3, 4, 5, 6, 8, 10])
            na  = random.randint(1, den)
            nb  = random.randint(1, den)
            return f'{na}/{den} + {nb}/{den} = ?', na + nb

        def q_potencia():
            base = random.randint(2, 9)
            exp  = random.choice([2, 3])
            sym  = '²' if exp == 2 else '³'
            return f'{base}{sym} = ?', base ** exp

        def q_raiz():
            n = random.choice([4, 9, 16, 25, 36, 49, 64, 81, 100, 121, 144])
            return f'√{n} = ?', int(n ** 0.5)

        def q_porcentagem():
            perc = random.choice([10, 20, 25, 50])
            val  = random.choice([20, 40, 60, 80, 100, 120, 150, 200, 250, 300])
            return f'{perc}% de {val} = ?', (perc * val) // 100

        def q_regra_tres():
            a   = random.choice([2, 3, 4, 5, 6, 8, 10])
            mul = random.randint(3, 8)
            b   = a * mul
            c   = random.choice([2, 3, 4, 5, 6])
            return f'{a} → {b}\n{c} → ?', (b * c) // a

        def q_equacao_1grau():
            a = random.randint(2, 9)
            x = random.randint(1, 15)
            b = random.randint(0, 20)
            c = a * x + b
            return f'{a}x + {b} = {c}  →  x = ?', x

        def q_equacao_produto_nulo():
            r1  = random.randint(-6, 6)
            r2  = random.randint(-6, 6)
            ans = max(r1, r2)
            B   = -(r1 + r2)
            C   =  r1 * r2
            bs  = f'+ {B}' if B >= 0 else f'- {abs(B)}'
            cs  = f'+ {C}' if C >= 0 else f'- {abs(C)}'
            return f'x² {bs}x {cs} = 0\nMaior raiz = ?', ans

        def q_progressao_aritmetica():
            a1 = random.randint(1, 10)
            r  = random.randint(2, 8)
            n  = random.randint(4, 7)
            an = a1 + (n - 1) * r
            return f'PA: {a1}, {a1+r}, {a1+2*r}, ...\n{n}º termo = ?', an

        def q_log():
            base = random.choice([2, 3, 5, 10])
            exp  = random.randint(1, 4)
            return f'log_{base}({base**exp}) = ?', exp

        def q_log_basico():
            base = random.choice([2, 3, 10])
            exp  = random.randint(1, 3)
            return f'log_{base}({base**exp}) = ?', exp

        def q_trigonometria():
            opts = [
                ('2 · sen(30°) = ?', 1),
                ('2 · cos(60°) = ?', 1),
                ('2 · sen(90°) = ?', 2),
                ('2 · cos(0°) = ?',  2),
                ('4 · sen(30°) = ?', 2),
                ('4 · cos(60°) = ?', 2),
            ]
            return random.choice(opts)

        def q_derivada_simples():
            n    = random.randint(2, 6)
            a    = random.randint(1, 8)
            coef = a * n
            return f"f(x) = {a}x^{n}  →  f'(coef x^{n-1}) = ?", coef

        def q_geometria_area():
            b = random.randint(4, 20)
            h = random.randint(2, 16)
            return f'Triâng.: base={b}, h={h}\nÁrea = ?', (b * h) // 2

        def q_det2x2():
            a, b, c, d = [random.randint(-5, 8) for _ in range(4)]
            return f'det|{a} {b} / {c} {d}| = ?', a * d - b * c

        # Pool por série
        LEVELS = {
            6:  [('soma', q_soma), ('sub', q_sub), ('mult', q_mult),
                 ('mmc', q_mmc), ('mdc', q_mdc), ('fracao', q_fracao)],
            7:  [('fracao_soma', q_fracao_soma), ('fracao_sub', q_fracao),
                 ('equacao', q_equacao_1grau), ('porcentagem', q_porcentagem),
                 ('potencia', q_potencia)],
            8:  [('potencia', q_potencia), ('raiz', q_raiz),
                 ('regra3', q_regra_tres), ('equacao', q_equacao_1grau),
                 ('porcentagem', q_porcentagem)],
            9:  [('equacao2', q_equacao_produto_nulo), ('pa', q_progressao_aritmetica),
                 ('area', q_geometria_area), ('raiz', q_raiz), ('potencia', q_potencia)],
            10: [('equacao2', q_equacao_produto_nulo), ('pa', q_progressao_aritmetica),
                 ('log_basico', q_log_basico), ('trig', q_trigonometria),
                 ('area', q_geometria_area)],
            11: [('log', q_log), ('det2x2', q_det2x2), ('pa', q_progressao_aritmetica),
                 ('equacao2', q_equacao_produto_nulo), ('raiz', q_raiz)],
            12: [('derivada', q_derivada_simples), ('log', q_log),
                 ('det2x2', q_det2x2), ('pa', q_progressao_aritmetica),
                 ('trig', q_trigonometria)],
        }

        pool         = LEVELS.get(lv, LEVELS[6])
        qtype, gen_fn = random.choice(pool)

        try:
            text, answer = gen_fn()
            answer = int(answer)
        except Exception:
            a, b = random.randint(5, 40), random.randint(5, 40)
            text, answer, qtype = f'{a} + {b} = ?', a + b, 'soma'

        time_limit = self.QUESTION_TIME.get(qtype, 8)

        # Alternativas erradas plausíveis
        spread   = max(3, abs(answer) // 4 + 2)
        wrongs   = set()
        attempts = 0
        while len(wrongs) < 3 and attempts < 300:
            attempts += 1
            delta = random.randint(1, spread)
            w     = answer + random.choice([-1, 1]) * delta
            if w != answer:
                wrongs.add(w)
        fb = 1
        while len(wrongs) < 3:
            wrongs.add(answer + fb)
            fb += 1

        choices = list(wrongs) + [answer]
        random.shuffle(choices)

        return {
            'text':        text,
            'choices':     choices,
            'correct_idx': choices.index(answer),
            'time_limit':  time_limit,
            'qtype':       qtype,
        }


# ── HTTP ROUTES ───────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/menu')
def menu():
    return render_template('index.html')

@app.route('/leaderboard')
def leaderboard():
    return jsonify(db_get_leaderboard())

@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'rooms': len(rooms)})


# ── SOCKET EVENTS ─────────────────────────────────────────────────────────────

@socketio.on('connect')
def on_connect():
    print(f"[CONN] {request.sid}")


@socketio.on('join_queue')
def on_join_queue(data):
    global waiting
    sid   = request.sid
    name  = str(data.get('name', 'Anônimo')).strip()[:30] or 'Anônimo'
    turma = str(data.get('turma', '')).strip()[:20]
    try:
        level = max(6, min(12, int(data.get('level', 6))))
    except (ValueError, TypeError):
        level = 6

    player_info = db_get_or_create_player(name, turma)
    player_info['level'] = level

    with waiting_lock:
        if waiting and waiting['sid'] != sid:
            w       = waiting
            waiting = None
            room_id = str(uuid.uuid4())[:8]

            join_room(room_id, sid=w['sid'])
            join_room(room_id, sid=sid)

            avg_level = (w['info'].get('level', 6) + level) // 2
            room = GameRoom(room_id, w['sid'], w['info'], sid, player_info, level=avg_level)
            rooms[room_id] = room

            socketio.emit('match_found', {
                'room_id': room_id, 'player_idx': 0,
                'opponent': player_info['name'], 'turma': player_info.get('turma', ''),
            }, to=w['sid'])
            socketio.emit('match_found', {
                'room_id': room_id, 'player_idx': 1,
                'opponent': w['info']['name'], 'turma': w['info'].get('turma', ''),
            }, to=sid)

            room.start()
            print(f"[GAME] {room_id} — {w['info']['name']} vs {name} (nível {avg_level})")
        else:
            waiting = {'sid': sid, 'info': player_info}
            emit('waiting_for_opponent', {})
            print(f"[QUEUE] {name} aguardando...")


@socketio.on('move_paddle')
def on_move_paddle(data):
    sid     = request.sid
    room_id = data.get('room_id')
    if room_id and room_id in rooms:
        try:
            rooms[room_id].move_paddle(sid, float(data.get('y', 0)))
        except (ValueError, TypeError):
            pass


@socketio.on('answer_math')
def on_answer_math(data):
    sid     = request.sid
    room_id = data.get('room_id')
    if room_id and room_id in rooms:
        try:
            idx = int(data.get('idx', -1))
        except (ValueError, TypeError):
            idx = -1
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
            print(f"[GAME] {room_id} encerrada por desconexão de {sid}")
            break


# ── ENTRY POINT ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"[START] Math Pong 3D — porta {port}")
    socketio.run(app, host='0.0.0.0', port=port, debug=False)
