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
BALL_SPEED   = 0.009
GAME_DURATION = 180  # 3 minutos em segundos
PADDLE_H     = 0.22
PADDLE_W     = 0.04
BALL_R       = 0.025

rooms   = {}          # room_id -> GameRoom
waiting = None        # socket_id esperando oponente
waiting_lock = threading.Lock()

class GameRoom:
    def __init__(self, room_id, p1_sid, p1_info, p2_sid, p2_info, level=6):
        self.level = level  # 6=6ºEF, 7=7ºEF, ... 12=3ºEM
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
        self.paddle_scales = [1.0, 1.0]  # visual + hitbox scales
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

        # Clamp speed so it never exceeds MAX_SPEED
        MAX_SPEED = BALL_SPEED * 1.8
        spd = math.sqrt(b['vx']**2 + b['vy']**2)
        if spd > MAX_SPEED:
            ratio = MAX_SPEED / spd
            b['vx'] *= ratio
            b['vy'] *= ratio

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

        # Left paddle P0 — use paddle scale for hit area
        scale0 = self.paddle_scales[0]
        if b['x'] < -0.9 + PADDLE_W + BALL_R and b['vx'] < 0 and self.last_hit != 0:
            if abs(b['y'] - p0y) < (PADDLE_H * scale0) + BALL_R:
                b['x'] = -0.9 + PADDLE_W + BALL_R
                relY   = (b['y'] - p0y) / (PADDLE_H * scale0)
                # NO speed increase on hit — keep constant speed
                speed  = BALL_SPEED * self.speed_mult
                angle  = relY * math.pi * 0.38
                b['vx'] =  abs(math.cos(angle)) * speed
                b['vy'] =  math.sin(angle) * speed
                self.last_hit = 0
                self.rally   += 1
                if self.rally % 3 == 0:
                    self._trigger_math(0)

        # Right paddle P1
        scale1 = self.paddle_scales[1]
        if b['x'] >  0.9 - PADDLE_W - BALL_R and b['vx'] > 0 and self.last_hit != 1:
            if abs(b['y'] - p1y) < (PADDLE_H * scale1) + BALL_R:
                b['x'] =  0.9 - PADDLE_W - BALL_R
                relY   = (b['y'] - p1y) / (PADDLE_H * scale1)
                speed  = BALL_SPEED * self.speed_mult
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
        self.paddle_scales = [1.0, 1.0]  # reset any paddle effects on point
        socketio.emit('score_update', {
            'scores': self.scores,
            'scorer': player_idx,
            'reset_effects': True,
        }, room=self.room_id)
        # Also reset reverse controls on the clients
        socketio.emit('effect_reverse', {'player_idx': -1}, room=self.room_id)
        # Brief pause before ball relaunch so players can reposition
        self.ball = {'x': 0.0, 'y': 0.0, 'vx': 0.0, 'vy': 0.0}
        def delayed_reset():
            time.sleep(1.5)
            self.reset_ball(1 if player_idx == 1 else -1)
        threading.Thread(target=delayed_reset, daemon=True).start()

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

    # Tempo (segundos) por tipo de questão
    QUESTION_TIME = {
        'soma':         5,
        'sub':          5,
        'mult':         6,
        'mmc':          8,
        'mdc':          8,
        'fracao':       9,
        'fracao_soma':  9,
        'fracao_sub':   9,
        'potencia':     6,
        'raiz':         6,
        'porcentagem':  8,
        'regra3':       10,
        'equacao':      12,
        'equacao2':     15,
        'pa':           14,
        'area':         10,
        'log_basico':   10,
        'log':          12,
        'trig':         10,
        'det2x2':       12,
        'derivada':     13,
    }

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
                'time_limit': q['time_limit'],
            }, to=sid)

        self._finalizing = False
        # Usa o maior tempo entre os dois jogadores como timeout do servidor
        max_time = max(q0['time_limit'], q1['time_limit'])
        def timeout():
            time.sleep(max_time)
            for i in range(2):
                if not self.math_answered[i]:
                    self._resolve_player_math(i, -1)
            if not self._finalizing:
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

        # Acerto: +1 ponto; Erro/Timeout: -1 ponto (minimo 0)
        if correct:
            self.scores[player_idx] += 1
        else:
            self.scores[player_idx] = max(0, self.scores[player_idx] - 1)

        socketio.emit('math_result', {
            'feedback':    feedback,
            'correct_idx': q['correct_idx'],
        }, to=sid)
        # Atualiza placar para ambos os jogadores
        socketio.emit('score_update', {
            'scores':         self.scores,
            'scorer':         player_idx if correct else -1,
            'math_penalty':   not correct,
            'penalty_player': player_idx if not correct else -1,
        }, room=self.room_id)
        if all(self.math_answered) and not self._finalizing:
            threading.Thread(target=self._finalize_math, daemon=True).start()

    # Efeitos disponíveis na roleta para quem acertar
    WINNER_EFFECTS = [
        {'id': 'slow',      'label': '🐢 Bola lenta!',        'desc': 'Velocidade reduzida'},
        {'id': 'fast_opp',  'label': '⚡ Inimigo acelerado!',  'desc': 'Oponente fica mais rápido'},
        {'id': 'big_paddle','label': '🏓 Raquete gigante!',    'desc': 'Sua raquete cresce'},
        {'id': 'tiny_opp',  'label': '🔬 Raquete minúscula!',  'desc': 'Raquete do inimigo encolhe'},
        {'id': 'reverse',   'label': '🔄 Controles invertidos!','desc': 'Inimigo com controles trocados'},
        {'id': 'freeze',    'label': '🧊 Congelar bola!',      'desc': 'Bola para por 2 segundos'},
    ]

    def _roll_effect(self):
        return random.choice(self.WINNER_EFFECTS)

    def _relaunch_ball_with_speed(self):
        """Relança a bola com speed_mult atual em direção aleatória."""
        direction = random.choice([1, -1])
        angle = (random.random() * 0.5 + 0.2) * random.choice([1, -1])
        s = BALL_SPEED * self.speed_mult
        self.ball = {
            'x': 0.0, 'y': 0.0,
            'vx': s * direction,
            'vy': angle * s,
        }

    def _apply_effect(self, effect_id, winner_idx):
        loser_idx = 1 - winner_idx

        if effect_id == 'slow':
            self.speed_mult = max(0.4, self.speed_mult * 0.65)
            # Bola estava parada durante math — relança com nova velocidade
            self._relaunch_ball_with_speed()

        elif effect_id == 'fast_opp':
            self.speed_mult = min(2.2, self.speed_mult * 1.4)
            # Relança bola indo em direção ao perdedor (pressão)
            direction = 1 if loser_idx == 1 else -1
            angle = (random.random() * 0.5 + 0.2) * random.choice([1, -1])
            s = BALL_SPEED * self.speed_mult
            self.ball = {'x': 0.0, 'y': 0.0, 'vx': s * direction, 'vy': angle * s}

        elif effect_id == 'big_paddle':
            self.paddle_scales[winner_idx] = 1.7
            socketio.emit('effect_paddle', {'size': 1.7, 'player_idx': winner_idx}, room=self.room_id)
            wi = winner_idx  # captura por valor para o closure
            def reset_paddle():
                time.sleep(8)
                self.paddle_scales[wi] = 1.0
                socketio.emit('effect_paddle', {'size': 1.0, 'player_idx': wi}, room=self.room_id)
            threading.Thread(target=reset_paddle, daemon=True).start()
            self._relaunch_ball_with_speed()

        elif effect_id == 'tiny_opp':
            self.paddle_scales[loser_idx] = 0.4
            socketio.emit('effect_paddle', {'size': 0.4, 'player_idx': loser_idx}, room=self.room_id)
            li = loser_idx
            def reset_opp():
                time.sleep(8)
                self.paddle_scales[li] = 1.0
                socketio.emit('effect_paddle', {'size': 1.0, 'player_idx': li}, room=self.room_id)
            threading.Thread(target=reset_opp, daemon=True).start()
            self._relaunch_ball_with_speed()

        elif effect_id == 'reverse':
            socketio.emit('effect_reverse', {'player_idx': loser_idx}, room=self.room_id)
            def reset_reverse():
                time.sleep(6)
                socketio.emit('effect_reverse', {'player_idx': -1}, room=self.room_id)
            threading.Thread(target=reset_reverse, daemon=True).start()
            self._relaunch_ball_with_speed()

        elif effect_id == 'freeze':
            socketio.emit('effect_freeze', {}, room=self.room_id)
            self.math_active = True  # pausa a bola por 2s
            def unfreeze():
                time.sleep(2)
                self._relaunch_ball_with_speed()
                self.math_active = False
            threading.Thread(target=unfreeze, daemon=True).start()

    def _finalize_math(self):
        if not self.math_active:
            return
        # Prevent double execution (timeout + answer both calling finalize)
        if hasattr(self, '_finalizing') and self._finalizing:
            return
        self._finalizing = True

        time.sleep(1.2)
        r = self.math_results

        winner_idx = None
        if r[0] is True and r[1] is not True:
            winner_idx = 0
        elif r[1] is True and r[0] is not True:
            winner_idx = 1

        if winner_idx is not None:
            effect = self._roll_effect()
            socketio.emit('roulette_spin', {
                'winner_idx': winner_idx,
                'effect': effect,
                'all_effects': [e['label'] for e in self.WINNER_EFFECTS],
            }, room=self.room_id)
            time.sleep(4.5)  # wait for full roulette animation + buffer
            # Libera math_active ANTES de aplicar efeito (freeze vai re-setar sozinho)
            self.math_active = False
            self._apply_effect(effect['id'], winner_idx)
        else:
            if r[0] is True and r[1] is True:
                self.speed_mult = max(0.6, self.speed_mult * 0.9)
            elif r[0] is False and r[1] is False:
                self.speed_mult = min(1.5, self.speed_mult * 1.1)
            self._relaunch_ball_with_speed()
            self.math_active = False

        self._finalizing = False

    def _gen_question(self):
        """Gera questões alinhadas ao currículo de cada série."""
        lv = self.level  # 6-12

        # Cada gerador retorna (text, answer)
        def q_fracao_simples():
            b = random.choice([2, 3, 4, 5, 6, 8, 10])
            a = random.randint(1, b * 2)
            c = random.randint(1, b * 2)
            if random.random() < 0.5:
                num = a + c
                return f'{a}/{b} + {c}/{b} = ?', num, b
            else:
                num = a - c
                return f'{a}/{b} - {c}/{b} = ?', num, b

        def q_fracao_resultado(num, den):
            import math as _math
            g = _math.gcd(abs(num), den) if den != 0 else 1
            n2, d2 = num // g, den // g
            if d2 == 1:
                return n2
            return None

        def q_potencia():
            base = random.randint(2, 9)
            exp  = random.choice([2, 3])
            return f'{base}² = ?' if exp == 2 else f'{base}³ = ?', base ** exp

        def q_raiz():
            n = random.choice([4, 9, 16, 25, 36, 49, 64, 81, 100, 121, 144])
            return f'√{n} = ?', int(n ** 0.5)

        def q_regra_tres():
            a = random.choice([2, 3, 4, 5, 6, 8, 10])
            b = a * random.randint(3, 8)
            c = random.choice([2, 3, 4, 5, 6])
            ans = (b * c) // a
            return f'{a} → {b}\n{c} → ?', ans

        def q_porcentagem():
            perc = random.choice([10, 20, 25, 50])
            val  = random.choice([20, 40, 60, 80, 100, 120, 150, 200])
            ans  = (perc * val) // 100
            return f'{perc}% de {val} = ?', ans

        def q_equacao_1grau():
            a = random.randint(2, 9)
            x = random.randint(1, 15)
            b = random.randint(0, 20)
            c = a * x + b
            return f'{a}x + {b} = {c}  →  x = ?', x

        def q_equacao_produto_nulo():
            r1 = random.randint(-6, 6)
            r2 = random.randint(-6, 6)
            ans = max(r1, r2)
            b   = -(r1 + r2)
            c   =  r1 * r2
            bs  = f'+ {b}' if b >= 0 else f'- {abs(b)}'
            cs  = f'+ {c}' if c >= 0 else f'- {abs(c)}'
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
            val  = base ** exp
            return f'log_{base}({val}) = ?', exp

        def q_trigonometria():
            int_pairs = [(30,'sen',1),(60,'cos',1)]
            ang, fn, ans = random.choice(int_pairs)
            return f'2 · {fn}({ang}°) = ?', ans

        def q_derivada_simples():
            n   = random.randint(2, 6)
            a   = random.randint(1, 8)
            coef = a * n
            return f"f(x) = {a}x^{n}  →  f'(coef. de x^{n-1}) = ?", coef

        def q_geometria_area():
            b = random.randint(4, 20)
            h = random.randint(2, 16)
            area = (b * h) // 2
            return f'Triâng.: base={b}, h={h}\nÁrea = ?', area

        def q_mmc():
            pairs = [(4,6),(6,9),(4,10),(3,5),(6,8),(5,10),(4,12),(6,10)]
            a, b = random.choice(pairs)
            import math as _math
            return f'MMC({a},{b}) = ?', (a*b)//_math.gcd(a,b)

        def q_mdc():
            pairs = [(12,8),(15,10),(18,12),(20,16),(24,18),(30,20)]
            a, b = random.choice(pairs)
            import math as _math
            return f'MDC({a},{b}) = ?', _math.gcd(a,b)

        # ── Seleciona questão por série, rastreando qtype ────────────────────
        qtype = None
        text = answer = None

        if lv <= 6:
            qtype = random.choice(['soma','sub','mult','mmc','mdc','fracao'])
            if qtype == 'soma':
                a, b = random.randint(1,50), random.randint(1,50)
                text, answer = f'{a} + {b} = ?', a + b
            elif qtype == 'sub':
                a = random.randint(10, 60); b = random.randint(1, a)
                text, answer = f'{a} - {b} = ?', a - b
            elif qtype == 'mult':
                a, b = random.randint(2,12), random.randint(2,12)
                text, answer = f'{a} × {b} = ?', a * b
            elif qtype == 'mmc':
                text, answer = q_mmc()
            elif qtype == 'mdc':
                text, answer = q_mdc()
            else:
                qtype = 'fracao'
                den = random.choice([2,3,4,5])
                num_a = random.randint(1, den*2)
                num_b = random.randint(1, num_a)
                num_r = num_a - num_b
                text  = f'{num_a}/{den} - {num_b}/{den} = ?'
                answer = num_r

        elif lv == 7:
            qtype = random.choice(['fracao_soma','fracao_sub','equacao','porcentagem','potencia'])
            if qtype in ('fracao_soma','fracao_sub'):
                den = random.choice([3,4,5,6,8,10])
                na  = random.randint(1, den)
                nb  = random.randint(1, den)
                if qtype == 'fracao_soma':
                    text   = f'{na}/{den} + {nb}/{den} = ?'
                    answer = na + nb
                else:
                    na, nb = max(na,nb), min(na,nb)
                    text   = f'{na}/{den} - {nb}/{den} = ?'
                    answer = na - nb
                import math as _math
                g   = _math.gcd(abs(answer), den)
                n2, d2 = answer // g, den // g
                answer = n2 if d2 == 1 else n2
            elif qtype == 'equacao':
                text, answer = q_equacao_1grau()
            elif qtype == 'porcentagem':
                text, answer = q_porcentagem()
            else:
                text, answer = q_potencia()

        elif lv == 8:
            qtype = random.choice(['potencia','raiz','regra3','equacao','porcentagem'])
            if qtype == 'potencia':    text, answer = q_potencia()
            elif qtype == 'raiz':      text, answer = q_raiz()
            elif qtype == 'regra3':    text, answer = q_regra_tres()
            elif qtype == 'equacao':   text, answer = q_equacao_1grau()
            else:                      text, answer = q_porcentagem()

        elif lv == 9:
            qtype = random.choice(['equacao2','pa','area','raiz','potencia'])
            if qtype == 'equacao2':  text, answer = q_equacao_produto_nulo()
            elif qtype == 'pa':      text, answer = q_progressao_aritmetica()
            elif qtype == 'area':    text, answer = q_geometria_area()
            elif qtype == 'raiz':    text, answer = q_raiz()
            else:                    text, answer = q_potencia()

        elif lv == 10:
            qtype = random.choice(['equacao2','pa','log_basico','trig','area'])
            if qtype == 'equacao2':    text, answer = q_equacao_produto_nulo()
            elif qtype == 'pa':        text, answer = q_progressao_aritmetica()
            elif qtype == 'log_basico':
                base = random.choice([2,3,10])
                exp  = random.randint(1,3)
                val  = base**exp
                text, answer = f'log_{base}({val}) = ?', exp
            elif qtype == 'trig':      text, answer = q_trigonometria()
            else:                      text, answer = q_geometria_area()

        elif lv == 11:
            qtype = random.choice(['log','det2x2','pa','equacao2','raiz'])
            if qtype == 'log':
                text, answer = q_log()
            elif qtype == 'det2x2':
                a,b,c,d = [random.randint(-5,8) for _ in range(4)]
                det = a*d - b*c
                text   = f'det|{a} {b} / {c} {d}| = ?'
                answer = det
            elif qtype == 'pa':        text, answer = q_progressao_aritmetica()
            elif qtype == 'equacao2':  text, answer = q_equacao_produto_nulo()
            else:                      text, answer = q_raiz()

        else:  # lv == 12
            qtype = random.choice(['derivada','log','det2x2','pa','trig'])
            if qtype == 'derivada':    text, answer = q_derivada_simples()
            elif qtype == 'log':       text, answer = q_log()
            elif qtype == 'det2x2':
                a,b,c,d = [random.randint(-6,9) for _ in range(4)]
                det = a*d - b*c
                text   = f'det|{a} {b} / {c} {d}| = ?'
                answer = det
            elif qtype == 'pa':        text, answer = q_progressao_aritmetica()
            else:                      text, answer = q_trigonometria()

        # tempo baseado no tipo real sorteado
        time_limit = self.QUESTION_TIME.get(qtype, 8)

        # ── Gera alternativas erradas ────────────────────────────────────────
        answer = int(answer)
        wrongs = set()
        spread = max(3, abs(answer) // 4 + 2)
        attempts = 0
        while len(wrongs) < 3 and attempts < 200:
            attempts += 1
            delta = random.randint(1, spread)
            w = answer + (delta if random.random() < 0.5 else -delta)
            if w != answer:
                wrongs.add(w)
        while len(wrongs) < 3:
            wrongs.add(answer + len(wrongs) + 1)

        choices = list(wrongs) + [answer]
        random.shuffle(choices)
        return {
            'text':        text,
            'choices':     choices,
            'correct_idx': choices.index(answer),
            'time_limit':  time_limit,
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
def splash():
    return render_template('splash.html')

@app.route('/menu')
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
    level = int(data.get('level', 6))  # 6–12 = 6ºEF ao 3ºEM
    player_info = db_get_or_create_player(name, turma)
    player_info['level'] = level

    with waiting_lock:
        if waiting and waiting['sid'] != sid:
            w = waiting
            waiting = None
            room_id = str(uuid.uuid4())[:8]
            join_room(room_id, sid=w['sid'])
            join_room(room_id, sid=sid)

            # Average level between the two players
            avg_level = (w['info'].get('level', 6) + player_info.get('level', 6)) // 2
            room = GameRoom(
                room_id,
                w['sid'], w['info'],
                sid,      player_info,
                level=avg_level,
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
