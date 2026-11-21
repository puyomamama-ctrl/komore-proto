"""API費用ガード（docs/API_COST_GUARDRAILS.md の実装）。
目的：月額2,980円のプランでも赤字が無制限に拡大しないこと。上限到達時は接客を真っ白にせず安全に縮退する。
  層1 会話単位（900秒・20ターン）／層2 店舗の日・月（回数・分数・原価円）／層3 サービス全体の月予算／層4 レート・同時接続・異常検知
  ＋ 外部事業者コンソール側の月額上限（乃木／運営の作業。ここでは設定しない）
- 判定は『事前予約方式』：ターン開始時に想定最大原価を日・月・全体カウンターへ仮計上し、収まらなければ開始しない。
  カウンター更新は BEGIN IMMEDIATE の1トランザクション（並行リクエストでも上限を超えない）。終了時に実績で確定。
- フェイルクローズ：カウンターを読めない／例外のときは縮退（L2）として扱う。
- 縮退（L2/L3）では AI を使わず、DB から作ったカード表示（営業時間・料金・支払い方法・案内）とスタッフ呼び出しを出す。原価ゼロ。
時刻は epoch 秒（JST日付で日・月を区切る）。テストのため now を差し込める。
"""
import collections, datetime, json, os, threading, time
import core, audit

LIM = json.load(open(os.environ.get('SK_LIMITS', os.path.join(core.HERE, 'limits.json'))))
_L = threading.Lock()

class Denied(Exception):
    """level: L2（店舗）/ L3（全体）/ rate / conv_end / busy / blocked、http 応答用。"""
    def __init__(self, level, reason, message, http=200, until=None):
        super().__init__(reason); self.level, self.reason, self.message, self.http, self.until = level, reason, message, http, until

MSG = {
    'L2': '本日のAI音声案内は終了しました。スタッフにお声がけください。',
    'L3': '現在AI音声が利用できません。スタッフにお声がけください。',
    'busy': 'ただいま混み合っています。少し待ってからもう一度お話しください。',
    'conv_end': 'お時間になりました。続きはスタッフにお声がけください。',
    'rate': '操作が早すぎます。少し待ってからお試しください。',
    'blocked': '一時的にAI音声案内を停止しています。スタッフにお声がけください。',
    'owner_voice': '本日の音声登録の回数上限に達しました。画面から手入力で登録できます。',
}

def _now(now): return time.time() if now is None else now
def _jst(now): return datetime.datetime.fromtimestamp(now, core.JST)
def keys(now):
    d = _jst(now); return f"d:{d:%Y-%m-%d}", f"m:{d:%Y-%m}"
def next_reset(now, period):
    d = _jst(now)
    if period.startswith('d'): n = (d + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else: n = (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return core.iso(n)

def fx(): return core.PRICING['usd_jpy']

def reserve_yen(kind):
    """1ターンの想定最大原価（円）。pricing.json の単価×上限値から計算（上限値を変えれば自動で追従）。"""
    P, cv = core.PRICING, LIM['conversation']
    asr_sec = cv['owner_audio_max_sec'] if kind != 'kiosk' else cv['turn_audio_max_sec']
    asr = asr_sec * P['qwen3-asr-flash']['per_sec']
    m = P.get(core.LLM_MODEL, {}); tin, tout = (8000, 700) if kind != 'kiosk' else (6000, cv['llm_max_out_tokens'])
    llm = tin / 1e6 * m.get('in_per_m', 0) + tout / 1e6 * m.get('out_per_m', 0)
    tts = cv['tts_max_chars'] * 2 / 10000 * P['qwen3-tts-flash']['per_10k_chars']
    return (asr + llm + tts) * fx()

def tts_est_yen(chars): return chars * 2 / 10000 * core.PRICING['qwen3-tts-flash']['per_10k_chars'] * fx()

def plan_limits(c, store_id):
    r = c.execute('SELECT plan FROM stores WHERE id=?', (store_id,)).fetchone()
    if not r: raise Denied('L2', 'no_store', MSG['L2'])
    lim = dict(LIM['plans'][r['plan'] or 'light'])
    o = c.execute('SELECT json FROM store_limits WHERE store_id=?', (store_id,)).fetchone()
    if o: lim.update(json.loads(o['json']))
    lim['day_yen'] = lim.get('day_yen', lim['month_yen'] * LIM['day_yen_ratio'])
    return lim

def set_store_limits(store_id, **kw):
    """運営専用（APIなし）。オーナー画面・店舗の操作では上限を引き上げられない。"""
    with core.db() as c:
        o = c.execute('SELECT json FROM store_limits WHERE store_id=?', (store_id,)).fetchone()
        cur = json.loads(o['json']) if o else {}; cur.update(kw)
        c.execute('INSERT OR REPLACE INTO store_limits(store_id,json) VALUES(?,?)', (store_id, json.dumps(cur)))
        audit.log(c, 'limits_changed', store_id, None, ','.join(kw))

# ---------------- メモリ内の状態（再起動で消える。試作の制約として docs に明記） ----------------
class State:
    def __init__(self): self.reset()
    def reset(self):
        self.rate = collections.defaultdict(collections.deque)   # key -> timestamps
        self.denials = collections.defaultdict(int)
        self.blocked = {}                                        # (scope,key) -> (until, reason)
        self.inflight = collections.Counter()                    # store_id / 0
        self.convs = {}                                          # (store_id, session) -> dict(start, turns, closed)
        self.events = collections.defaultdict(lambda: collections.deque(maxlen=60))   # store_id -> (ts, normq, session, empty)
        self.outcomes = collections.deque(maxlen=40)             # 事業者呼び出しの成否（全体）
        self.provider_degraded_until = 0
        self.warned = set()
ST = State()

def reset_state(): ST.reset()

def _rate_check(scope, key, limit, now):
    dq = ST.rate[(scope, key)]
    while dq and dq[0] < now - 60: dq.popleft()
    if len(dq) >= limit:
        ST.denials[(scope, key)] += 1
        if ST.denials[(scope, key)] >= LIM['rate']['denials_to_block']:
            ST.blocked[(scope, key)] = (now + LIM['rate']['block_minutes'] * 60, 'rate')
            ST.denials[(scope, key)] = 0
        return False
    dq.append(now); return True

def block(scope, key, reason, now, store_id=None, minutes=None):
    until = now + (minutes or LIM['rate']['block_minutes']) * 60
    ST.blocked[(scope, key)] = (until, reason)
    audit.log_standalone('guard_block', store_id, None, f'{scope}:{reason}', now)

def _is_blocked(scope, key, now):
    b = ST.blocked.get((scope, key))
    if b and b[0] > now: return b
    if b: del ST.blocked[(scope, key)]
    return None

# ---------------- カウンター ----------------
def _get(c, store_id, period):
    c.execute('INSERT OR IGNORE INTO usage_counters(store_id,period) VALUES(?,?)', (store_id, period))
    return c.execute('SELECT * FROM usage_counters WHERE store_id=? AND period=?', (store_id, period)).fetchone()

def _txn():
    c = core.db(); c.isolation_level = None; c.execute('BEGIN IMMEDIATE'); return c

class Ticket:
    def __init__(self, **kw): self.__dict__.update(kw)

def begin_turn(store_id, kind='kiosk', session='', ip='', now=None, new_conv_ok=True):
    """kind: kiosk（客の質問）/ owner（店長の音声登録）/ test（店長の回答テスト）/ owner_say（確認文の読み上げ）。
    通れば Ticket（終了時に end_turn へ渡す）。上限に当たれば Denied。内部エラーは縮退（フェイルクローズ）。"""
    now = _now(now)
    try:
        return _begin(store_id, kind, session, ip, now)
    except Denied:
        raise
    except Exception as e:   # 例外＝上限を判定できない → 縮退
        try: audit.log_standalone('guard_error', store_id, None, type(e).__name__, now)
        except Exception: pass
        raise Denied('L2', 'guard_error', MSG['L2'])

def _begin(store_id, kind, session, ip, now):
    R, G, cv = LIM['rate'], LIM['global'], LIM['conversation']
    if ST.provider_degraded_until > now:
        raise Denied('L3', 'provider_errors', MSG['L3'], until=core.iso(_jst(ST.provider_degraded_until)))
    with _L:
        if kind == 'kiosk':
            for scope, key in (('ip', ip), ('store', store_id), ('session', (store_id, session))):
                b = _is_blocked(scope, key, now)
                if b: raise Denied('blocked', 'blocked:' + b[1], MSG['blocked'], until=core.iso(_jst(b[0])))
            for scope, key, lim in (('ip', ip, R['ip_per_min']), ('store', store_id, R['store_per_min']), ('session', (store_id, session), R['session_per_min'])):
                if not _rate_check(scope, key, lim, now):
                    raise Denied('rate', 'rate_' + scope, MSG['rate'], http=429)
        else:
            if not _rate_check('admin', store_id, R['admin_per_min'], now):
                raise Denied('rate', 'rate_admin', MSG['rate'], http=429)
        if ST.inflight[0] >= G['concurrent']:
            raise Denied('busy', 'global_concurrent', MSG['busy'])
    c = _txn()
    try:
        lim = plan_limits(c, store_id)
        if kind == 'kiosk' and ST.inflight[store_id] >= lim['concurrent']:
            c.execute('ROLLBACK'); raise Denied('busy', 'store_concurrent', MSG['busy'])
        dk, mk = keys(now)
        d, m, g = _get(c, store_id, dk), _get(c, store_id, mk), _get(c, 0, mk)
        res, rs = reserve_yen(kind), cv['turn_reserve_sec'] if kind == 'kiosk' else 0
        if g['yen'] + res > G['month_yen']:
            c.execute('ROLLBACK'); audit.log_standalone('guard_deny', store_id, None, 'global_month_yen', now)
            raise Denied('L3', 'global_month_yen', MSG['L3'], until=next_reset(now, 'm'))
        newconv, conv = False, None
        if kind == 'kiosk':
            ck = (store_id, session)
            conv = ST.convs.get(ck)
            if conv and conv['closed']:
                c.execute('ROLLBACK'); raise Denied('conv_end', 'conv_closed', MSG['conv_end'])
            if conv and (now - conv['start'] > cv['max_seconds'] or conv['turns'] >= cv['max_turns']):
                conv['closed'] = True; c.execute('ROLLBACK'); raise Denied('conv_end', 'conv_limit', MSG['conv_end'])
            newconv = conv is None
        checks = [
            ('day_yen', d['yen'] + res > lim['day_yen'], dk), ('month_yen', m['yen'] + res > lim['month_yen'], mk),
        ]
        if kind == 'kiosk':
            checks += [('day_minutes', d['seconds'] + rs > lim['day_minutes'] * 60, dk), ('month_minutes', m['seconds'] + rs > lim['month_minutes'] * 60, mk),
                       ('day_convs', newconv and d['convs'] + 1 > lim['day_convs'], dk)]
        if kind == 'owner':
            checks.append(('owner_voice_day', d['owner_voice'] + 1 > LIM['owner_voice_per_day'], dk))
        for name, hit, per in checks:
            if hit:
                c.execute('ROLLBACK')
                audit.log_standalone('guard_deny', store_id, None, f'{kind}:{name}', now)
                if name == 'owner_voice_day': raise Denied('L2', name, MSG['owner_voice'], http=429, until=next_reset(now, per))
                raise Denied('L2', name, MSG['L2'], until=next_reset(now, per))
        for per in (dk, mk):
            c.execute('UPDATE usage_counters SET yen=yen+?, seconds=seconds+?, convs=convs+?, owner_voice=owner_voice+?, turns=turns+1 WHERE store_id=? AND period=?',
                      (res, rs, 1 if newconv else 0, 1 if kind == 'owner' else 0, store_id, per))
        c.execute('UPDATE usage_counters SET yen=yen+? WHERE store_id=0 AND period=?', (res, mk))
        c.execute('COMMIT')
    except Denied:
        raise
    except BaseException:
        try: c.execute('ROLLBACK')
        except Exception: pass
        raise
    finally:
        c.close()
    with _L:
        ST.inflight[store_id] += 1; ST.inflight[0] += 1
        if kind == 'kiosk':
            conv = ST.convs.setdefault((store_id, session), {'start': now, 'turns': 0, 'closed': False})
            conv['turns'] += 1
            if len(ST.convs) > 5000:
                for k in [k for k, v in ST.convs.items() if now - v['start'] > 600]: del ST.convs[k]
    return Ticket(store_id=store_id, kind=kind, session=session, reserved_yen=res, reserved_sec=rs, now=now, t0=time.time(), done=False)

def end_turn(t, actual_yen=0.0, actual_sec=0.0, error=False, question=None, status=None, now=None):
    """実績で確定（予約との差分を戻す）。同時接続を解放。事業者呼び出しの成否・異常検知を記録。"""
    if t.done: return
    t.done = True; now = _now(now) if now is not None else time.time()
    with _L:
        ST.inflight[t.store_id] = max(0, ST.inflight[t.store_id] - 1); ST.inflight[0] = max(0, ST.inflight[0] - 1)
    try:
        dk, mk = keys(t.now)
        c = _txn()
        try:
            dy, ds = actual_yen - t.reserved_yen, (actual_sec - t.reserved_sec) if t.kind == 'kiosk' else 0
            for per in (dk, mk):
                _get(c, t.store_id, per)
                c.execute('UPDATE usage_counters SET yen=MAX(0,yen+?), seconds=MAX(0,seconds+?), errors=errors+? WHERE store_id=? AND period=?', (dy, ds, 1 if error else 0, t.store_id, per))
            _get(c, 0, mk); c.execute('UPDATE usage_counters SET yen=MAX(0,yen+?) WHERE store_id=0 AND period=?', (dy, mk))
            c.execute('COMMIT')
        except BaseException:
            c.execute('ROLLBACK'); raise
        finally:
            c.close()
        _after(t, error, question, status, now)
    except Exception as e:
        audit.log_standalone('guard_error', t.store_id, None, 'end_turn:' + type(e).__name__)

def add_cost(store_id, yen, seconds=0.0, now=None):
    """予約外に後から確定した費用（TTSの実績との差分など）をカウンターへ加える。マイナスも可。"""
    now = _now(now); dk, mk = keys(now)
    c = _txn()
    try:
        for sid, per in ((store_id, dk), (store_id, mk), (0, mk)):
            _get(c, sid, per); c.execute('UPDATE usage_counters SET yen=MAX(0,yen+?), seconds=MAX(0,seconds+?) WHERE store_id=? AND period=?', (yen, seconds if sid else 0, sid, per))
        c.execute('COMMIT')
    except BaseException:
        c.execute('ROLLBACK'); raise
    finally:
        c.close()

def _after(t, error, question, status, now):
    E, A = LIM['errors'], LIM['anomaly']
    with _L:
        ST.outcomes.append(0 if error else 1)
        o = list(ST.outcomes)[-E['window']:]
        if len(o) >= E['min_samples'] and o.count(0) / len(o) >= E['ratio'] and ST.provider_degraded_until <= now:
            ST.provider_degraded_until = now + E['degrade_seconds']
            audit.log_standalone('guard_provider_errors', None, None, f'{o.count(0)}/{len(o)} failed -> L3 {E["degrade_seconds"]}s', now)
        if t.kind != 'kiosk' or error: return
        ev = ST.events[t.store_id]
        nq = ''.join((question or '').split()).lower()
        ev.append((now, nq, t.session, not nq or (status or '').endswith('empty')))
        same = [e for e in ev if e[1] and e[1] == nq and e[0] > now - 60]
        if nq and len(same) >= A['same_question_per_min']:
            block('store', t.store_id, 'same_question', now, t.store_id); ev.clear(); return
        mine = [e[0] for e in ev if e[2] == t.session]
        if len(mine) >= A['rapid_count'] and all(b - a < A['rapid_gap_sec'] for a, b in zip(mine[-A['rapid_count']:], mine[-A['rapid_count'] + 1:])):
            block('session', (t.store_id, t.session), 'rapid_fire', now, t.store_id); return
        last = list(ev)[-A['empty_window']:]
        if len(last) >= A['empty_window'] and sum(1 for e in last if e[3]) / len(last) >= A['empty_ratio']:
            block('store', t.store_id, 'mostly_empty', now, t.store_id); ev.clear()

# ---------------- 状態・表示 ----------------
def level(store_id, now=None):
    """L0〜L3 と理由・再開時刻。読めなければ L2（フェイルクローズ）。"""
    now = _now(now)
    try:
        if ST.provider_degraded_until > now:
            return {'level': 'L3', 'reason': 'provider_errors', 'until': core.iso(_jst(ST.provider_degraded_until))}
        for scope, key in (('store', store_id),):
            b = _is_blocked(scope, key, now)
            if b: return {'level': 'L2', 'reason': 'blocked:' + b[1], 'until': core.iso(_jst(b[0]))}
        dk, mk = keys(now)
        with core.db() as c:
            lim = plan_limits(c, store_id); d, m, g = _get(c, store_id, dk), _get(c, store_id, mk), _get(c, 0, mk)
        res = reserve_yen('kiosk')
        if g['yen'] + res > LIM['global']['month_yen']: return {'level': 'L3', 'reason': 'global_month_yen', 'until': next_reset(now, 'm')}
        for name, used, cap, per in (('day_yen', d['yen'] + res, lim['day_yen'], dk), ('month_yen', m['yen'] + res, lim['month_yen'], mk),
                                     ('day_minutes', d['seconds'] + LIM['conversation']['turn_reserve_sec'], lim['day_minutes'] * 60, dk),
                                     ('month_minutes', m['seconds'] + LIM['conversation']['turn_reserve_sec'], lim['month_minutes'] * 60, mk),
                                     ('day_convs', d['convs'] + 1, lim['day_convs'], dk)):
            if used > cap: return {'level': 'L2', 'reason': name, 'until': next_reset(now, per)}
        ratio = max(d['yen'] / lim['day_yen'], m['yen'] / lim['month_yen'], d['seconds'] / (lim['day_minutes'] * 60), m['seconds'] / (lim['month_minutes'] * 60), d['convs'] / lim['day_convs'])
        if ratio >= LIM['warn_ratio']:
            wk = (store_id, dk, 'L1')
            if wk not in ST.warned: ST.warned.add(wk); audit.log_standalone('guard_warn', store_id, None, f'usage {ratio:.0%} of cap', now)
            return {'level': 'L1', 'reason': 'warn_80', 'until': None}
        return {'level': 'L0', 'reason': '', 'until': None}
    except Exception:
        return {'level': 'L2', 'reason': 'guard_error', 'until': None}

def cards(store_id):
    """縮退時の文字カード（AI・外部API不使用）。矛盾している項目は載せない。"""
    d = core.store_data(store_id); bad, _ = core.find_conflicts(d); out = []
    hrs = {r['weekday']: r for r in d['hours'] if f"H{r['id']}" not in bad}
    if hrs:
        out.append({'title': '営業時間', 'lines': [f"{core.WD[w]}曜日: {core.hours_text(r)}" for w, r in sorted(hrs.items())]})
    sp = [f"{r['date_from']}: {core.hours_text(r)}" for r in d['special_hours'] if f"S{r['id']}" not in bad]
    if sp: out.append({'title': '特別営業日・臨時休業', 'lines': sp})
    pr = [f"{r['name']} {r['amount_yen']:,}円" + (f"（{r['valid_from'] or ''}〜{r['valid_to'] or ''}）" if r['valid_from'] or r['valid_to'] else '') for r in d['prices'] if f"P{r['id']}" not in bad]
    if pr: out.append({'title': '料金', 'lines': pr})
    mn = [f"{r['name']}" + (f" {r['price_yen']:,}円" if r['price_yen'] is not None else '') for r in d['menu'] if f"M{r['id']}" not in bad]
    if mn: out.append({'title': 'メニュー', 'lines': mn})
    if d['payments']: out.append({'title': '支払い方法', 'lines': [r['method'] for r in d['payments']]})
    fq = [f"{r['topic']}: {r['answer']}" for r in d['faqs'] if f"F{r['id']}" not in bad]
    if fq: out.append({'title': 'ご案内', 'lines': fq})
    return out

def degraded_payload(store_id, den):
    return {'degraded': True, 'level': den.level, 'reason': den.reason, 'message': den.message, 'until': den.until,
            'cards': cards(store_id) if den.level in ('L2', 'L3', 'blocked') else [], 'staff_call': True}

def usage_report(store_id, now=None):
    """オーナー画面の「利用状況」。会話本文は保存していないため表示しない。"""
    now = _now(now); dk, mk = keys(now)
    with core.db() as c:
        lim = plan_limits(c, store_id); d, m = _get(c, store_id, dk), _get(c, store_id, mk)
        lat = [json.loads(r['timings_json']).get('server_ask_ms') for r in c.execute("SELECT timings_json FROM conversations WHERE store_id=? AND mode='kiosk' ORDER BY id DESC LIMIT 50", (store_id,))]
        lat = [x for x in lat if x]
        unans = c.execute("SELECT COUNT(*) FROM conversations WHERE store_id=? AND mode='kiosk' AND status NOT LIKE 'answered%' AND ts >= ?", (store_id, f"{_jst(now):%Y-%m}-01")).fetchone()[0]
        warn = [dict(r) for r in c.execute("SELECT ts,event,detail FROM audit_log WHERE store_id=? AND event LIKE 'guard_%' ORDER BY id DESC LIMIT 10", (store_id,))]
    def row(used, cap): return {'used': round(used, 3), 'cap': cap, 'pct': round(100 * used / cap, 1) if cap else None}
    lv = level(store_id, now)
    o = list(ST.outcomes)[-LIM['errors']['window']:]
    return {'level': lv, 'plan_limits': lim,
            'today': {'conversations': row(d['convs'], lim['day_convs']), 'minutes': row(d['seconds'] / 60, lim['day_minutes']), 'cost_yen': row(d['yen'], lim['day_yen']),
                      'owner_voice': row(d['owner_voice'], LIM['owner_voice_per_day']), 'resets_at': next_reset(now, 'd')},
            'month': {'conversations': {'used': m['convs']}, 'minutes': row(m['seconds'] / 60, lim['month_minutes']), 'cost_yen': row(m['yen'], lim['month_yen']), 'resets_at': next_reset(now, 'm')},
            'avg_latency_ms': round(sum(lat) / len(lat)) if lat else None, 'error_rate_recent': round(o.count(0) / len(o), 2) if o else None,
            'unanswered_this_month': unans, 'warnings': warn,
            'note': '原価は予約＋実績の合算（円、為替 %s）。会話本文・音声は保存していないため表示しません。' % fx()}
