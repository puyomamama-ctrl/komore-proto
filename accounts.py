"""アカウント・ワンタイム登録コード・端末/セッション制御（試作品）
設計は docs/SYSTEM_ARCHITECTURE.md §端末制御。鍵やパスワード平文は保存しない（ハッシュのみ）。
アカウント数・端末数・同時利用台数は課金と無関係（端末制御は不正利用防止＝安全管理のみ。段階B2で課金連動を撤去）。
時刻は epoch 秒。テストのため now を引数で差し込める。
"""
import calendar, datetime, hashlib, hmac, json, re, secrets, time
import core, audit
from core import db, iso, JST

CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # 紛らわしい文字(0/O/1/I)を除く32文字
CODE_LEN = 10                                        # 32^10 ≒ 2^50
DEFAULT_TTL_MIN, MAX_TTL_MIN = 60 * 24, 60 * 24 * 7
SESSION_TTL = {'owner': 30 * 60, 'staff': 12 * 3600}  # 無操作での失効(秒)。操作のたびに延長
MIN_PW = 8

class AuthError(Exception):
    def __init__(self, code, msg, http=403):
        super().__init__(msg); self.code, self.msg, self.http = code, msg, http

def _now(now): return time.time() if now is None else now
def _h(s): return hashlib.sha256(s.encode()).hexdigest()
def dev_hash(device_id):
    if not device_id or len(device_id) < 8 or len(device_id) > 128:
        raise AuthError('bad_device', '端末IDが不正です', 400)
    return _h('dev:' + device_id)
def code_hash(code): return _h('code:' + re.sub(r'[\s-]', '', code or '').upper())
def _iso_ts(ts): return iso(datetime.datetime.fromtimestamp(ts, JST))

def event(c, store_id, account_id, kind, actor=None, detail='', now=None):
    ts = _now(now)
    c.execute('INSERT INTO account_events(store_id,account_id,ts,ts_iso,event,actor_account_id,detail) VALUES(?,?,?,?,?,?,?)',
              (store_id, account_id, ts, _iso_ts(ts), kind, actor, detail))
    audit.log(c, 'acct_' + kind, store_id, actor if actor is not None else account_id, f'target={account_id} {detail}', now)

# ---------------- アカウント ----------------
def _check_pw(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PW or len(pw) > 128:
        raise AuthError('weak_password', f'パスワードは{MIN_PW}文字以上にしてください', 400)

def _pw_taken(c, store_id, pw, except_id=None):
    """ログインが「店舗ID＋パスワード」だけなので、同じ店舗内でパスワードは重複不可。"""
    for r in c.execute("SELECT id,pw_salt,pw_hash FROM accounts WHERE store_id=? AND status!='deleted'", (store_id,)):
        if r['id'] != except_id and hmac.compare_digest(core._pw_hash(pw, r['pw_salt']), r['pw_hash']):
            return True
    return False

def _add_account(c, store_id, name, role, pw, device_hash=None, device_label=None, actor=None, now=None):
    if _pw_taken(c, store_id, pw):
        raise AuthError('password_in_use', 'このパスワードは使えません（別のものにしてください）', 400)
    salt = secrets.token_hex(16); t = iso(datetime.datetime.fromtimestamp(_now(now), JST))
    cur = c.execute('INSERT INTO accounts(store_id,name,role,pw_salt,pw_hash,device_hash,device_label,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (store_id, name, role, salt, core._pw_hash(pw, salt), device_hash, device_label, t, t))
    event(c, store_id, cur.lastrowid, 'created', actor, role, now)
    return cur.lastrowid

def create_store(slug, name, owner_password, category='', now=None):
    """店舗＋オーナーアカウントを作る（localhost試作のみ。本番は契約後に運営が作成）。"""
    if not re.fullmatch(r'[a-z0-9-]{3,32}', slug or ''):
        raise AuthError('bad_slug', '店舗IDは英小文字・数字・-で3〜32文字', 400)
    _check_pw(owner_password)
    with core._lock, db() as c:
        if c.execute('SELECT 1 FROM stores WHERE slug=?', (slug,)).fetchone():
            raise AuthError('slug_in_use', 'その店舗IDは使われています', 400)
        cur = c.execute('INSERT INTO stores(slug,name,category,created_at,updated_at) VALUES(?,?,?,?,?)', (slug, name or slug, category, iso(), iso()))
        sid = cur.lastrowid
        _add_account(c, sid, 'オーナー', 'owner', owner_password, now=now)
    return sid

def get_account(account_id):
    with db() as c:
        r = c.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        return dict(r) if r else None

def public_account(a):
    return {k: a[k] for k in ('id', 'name', 'role', 'status', 'needs_rebind', 'device_label', 'created_at', 'updated_at', 'deleted_at')}

# ---------------- 認証・セッション ----------------
FAILS = {}   # 'slug' / 'ip:..' / 'dev:..' -> [count, locked_until]   （試作：メモリ内。本番はDB/Redis）
def _keys(slug, ip, dh=None):
    return [slug or ''] + ([f'ip:{ip}'] if ip else []) + ([f'dev:{dh}'] if dh else [])
def _throttle(slug, now, ip=None, dh=None):
    """店舗ID・IP・端末IDの各単位で5回失敗→5分ロック。"""
    for k in _keys(slug, ip, dh):
        f = FAILS.get(k, [0, 0])
        if f[1] > now:
            raise AuthError('locked', '失敗が続いたため5分間ロックしています', 429)
    return None
def _fail(slug, f, now, ip=None, dh=None):
    for k in _keys(slug, ip, dh):
        f = FAILS.get(k, [0, 0]); f[0] += 1
        if f[0] >= (30 if k.startswith('ip:') else 5): f = [0, now + 300]
        FAILS[k] = f
def _ok(slug, ip=None, dh=None):
    FAILS.pop(slug, None)

def new_session(c, acct, dh, now):
    """1アカウントにつき有効セッションは1つ。新しいログインは古いものを失効させる（同時ログイン制限）。"""
    c.execute("UPDATE sessions SET revoked_at=?, revoked_reason='superseded' WHERE account_id=? AND revoked_at IS NULL", (now, acct['id']))
    tok = secrets.token_urlsafe(32)
    c.execute('INSERT INTO sessions(token_hash,account_id,store_id,device_hash,created_at,last_seen,expires_at) VALUES(?,?,?,?,?,?,?)',
              (_h(tok), acct['id'], acct['store_id'], dh, now, now, now + SESSION_TTL[acct['role']]))
    return tok

def login(slug, password, device_id, now=None, ip=None):
    now = _now(now); dh = dev_hash(device_id); f = _throttle(slug, now, ip, dh)
    st = core.store_by_slug(slug or '')
    found = None
    if st:
        with db() as c:
            for r in c.execute("SELECT * FROM accounts WHERE store_id=? AND status!='deleted'", (st['id'],)):
                if hmac.compare_digest(core._pw_hash(password or '', r['pw_salt']), r['pw_hash']):
                    found = dict(r); break
    if not found:
        _fail(slug or '', f, now, ip, dh)
        audit.log_standalone('login_fail', st['id'] if st else None, None, 'bad_credentials', now)
        raise AuthError('bad_credentials', '店舗IDまたはパスワードが違います')
    if found['status'] == 'suspended':
        raise AuthError('suspended', 'このアカウントは停止中です。オーナーに連絡してください')
    if found['needs_rebind']:
        raise AuthError('rebind_required', '再発行された登録コードで再登録してください')
    if found['role'] == 'staff' and found['device_hash'] != dh:
        raise AuthError('device_not_registered', 'この端末は未登録です。オーナーから登録コードを受け取ってください')
    _ok(slug)
    with core._lock, db() as c:
        tok = new_session(c, found, dh, now)
        event(c, st['id'], found['id'], 'login', found['id'], '', now)
    return tok, found, st

def auth_session(token, device_id, now=None):
    """Cookieのセッショントークン＋端末IDヘッダを検証。無効なら None。"""
    if not token: return None
    now = _now(now)
    with db() as c:
        s = c.execute('SELECT * FROM sessions WHERE token_hash=?', (_h(token),)).fetchone()
        if not s or s['revoked_at'] or s['expires_at'] < now: return None
        try:
            if not hmac.compare_digest(s['device_hash'], dev_hash(device_id)): return None
        except AuthError:
            return None
        a = c.execute('SELECT * FROM accounts WHERE id=?', (s['account_id'],)).fetchone()
        if not a or a['status'] != 'active' or a['needs_rebind']: return None
        c.execute('UPDATE sessions SET last_seen=?, expires_at=? WHERE id=?', (now, now + SESSION_TTL[a['role']], s['id']))
        d = dict(a); d['session_id'] = s['id']; d['reauth_until'] = s['reauth_until'] or 0
        return d

def logout(token, now=None):
    with db() as c:
        c.execute("UPDATE sessions SET revoked_at=?, revoked_reason='logout' WHERE token_hash=? AND revoked_at IS NULL", (_now(now), _h(token or '')))

# ---------------- ワンタイム登録コード ----------------
def issue_code(owner, kind='new_account', label='', ttl_min=DEFAULT_TTL_MIN, target_account_id=None, now=None):
    """オーナーのみ。平文コードはこの戻り値でだけ返す（DBにはハッシュのみ）。"""
    if owner['role'] != 'owner': raise AuthError('forbidden', 'オーナーのみ実行できます', 403)
    ttl_min = int(ttl_min)
    if not 1 <= ttl_min <= MAX_TTL_MIN: raise AuthError('bad_ttl', f'有効期限は1〜{MAX_TTL_MIN}分', 400)
    now = _now(now); code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    with core._lock, db() as c:
        if kind == 'rebind':
            t = c.execute("SELECT * FROM accounts WHERE id=? AND store_id=? AND status!='deleted' AND role='staff'", (target_account_id, owner['store_id'])).fetchone()
            if not t: raise AuthError('not_found', '対象の店員アカウントがありません', 404)
        else:
            target_account_id = None
        c.execute('UPDATE reg_codes SET revoked_at=? WHERE store_id=? AND target_account_id IS ? AND used_at IS NULL AND revoked_at IS NULL AND kind=?',
                  (now, owner['store_id'], target_account_id, kind)) if kind == 'rebind' else None
        cur = c.execute('INSERT INTO reg_codes(store_id,code_hash,kind,target_account_id,label,created_by,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)',
                        (owner['store_id'], code_hash(code), kind, target_account_id, label[:40], owner['id'], now, now + ttl_min * 60))
        event(c, owner['store_id'], target_account_id, 'code_issued', owner['id'], f'{kind}:{label[:40]}', now)
    return {'id': cur.lastrowid, 'code': code, 'expires_at': _iso_ts(now + ttl_min * 60), 'ttl_min': ttl_min}

def revoke_code(owner, code_id, now=None):
    now = _now(now)
    with db() as c:
        n = c.execute('UPDATE reg_codes SET revoked_at=? WHERE id=? AND store_id=? AND used_at IS NULL AND revoked_at IS NULL', (now, code_id, owner['store_id'])).rowcount
        if n: event(c, owner['store_id'], None, 'code_revoked', owner['id'], str(code_id), now)
    return bool(n)

def redeem_code(slug, code, password, device_id, name='', device_label='', now=None, ip=None):
    """登録コードを1回だけ使って、新しい店員アカウント作成（または再発行アカウントの再登録）＋端末紐付け。"""
    now = _now(now); dh = dev_hash(device_id); f = _throttle(slug, now, ip, dh)
    st = core.store_by_slug(slug or '')
    ch = code_hash(code)
    with core._lock, db() as c:
        r = c.execute('SELECT * FROM reg_codes WHERE code_hash=? AND store_id=?', (ch, st['id'] if st else -1)).fetchone() if st else None
        if not r:
            _fail(slug or '', f, now, ip, dh); audit.log_standalone('code_fail', st['id'] if st else None, None, 'invalid_code', now)
            raise AuthError('invalid_code', '登録コードが正しくありません', 400)
        if r['used_at']: raise AuthError('code_used', 'この登録コードは使用済みです（再利用できません）', 400)
        if r['revoked_at']: raise AuthError('code_revoked', 'この登録コードは無効化されています', 400)
        if r['expires_at'] < now: raise AuthError('code_expired', 'この登録コードは期限切れです', 400)
        _check_pw(password)
        # 先にコードを消費（原子的：used_at IS NULL の行だけ更新できる＝二重使用不可）
        if not c.execute('UPDATE reg_codes SET used_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL', (now, r['id'])).rowcount:
            raise AuthError('code_used', 'この登録コードは使用済みです（再利用できません）', 400)
        try:
            if r['kind'] == 'rebind':
                a = c.execute('SELECT * FROM accounts WHERE id=?', (r['target_account_id'],)).fetchone()
                if not a or a['status'] == 'deleted': raise AuthError('not_found', '対象アカウントは削除されています', 400)
                if _pw_taken(c, st['id'], password, a['id']): raise AuthError('password_in_use', 'このパスワードは使えません（別のものにしてください）', 400)
                salt = secrets.token_hex(16)
                c.execute('UPDATE accounts SET pw_salt=?,pw_hash=?,device_hash=?,device_label=?,needs_rebind=0,updated_at=? WHERE id=?',
                          (salt, core._pw_hash(password, salt), dh, device_label[:40] or a['device_label'], iso(), a['id']))
                aid = a['id']; event(c, st['id'], aid, 'rebound', aid, '', now)
            else:
                aid = _add_account(c, st['id'], (name or r['label'] or '店員')[:40], 'staff', password, dh, device_label[:40], None, now)
        except AuthError:
            c.rollback() if hasattr(c, 'rollback') else None
            raise
        c.execute('UPDATE reg_codes SET used_by_account=? WHERE id=?', (aid, r['id']))
        acct = dict(c.execute('SELECT * FROM accounts WHERE id=?', (aid,)).fetchone())
        tok = new_session(c, acct, dh, now)
    _ok(slug)
    return tok, acct, st

# ---------------- オーナーによる制御 ----------------
def _own(c, owner, account_id):
    a = c.execute('SELECT * FROM accounts WHERE id=? AND store_id=?', (account_id, owner['store_id'])).fetchone()
    if not a: raise AuthError('not_found', '対象アカウントがありません', 404)
    return a

def _kill_sessions(c, account_id, reason, now):
    c.execute('UPDATE sessions SET revoked_at=?, revoked_reason=? WHERE account_id=? AND revoked_at IS NULL', (now, reason, account_id))

def control(owner, account_id, action, now=None):
    """action: suspend / resume / delete / reissue（再発行は別途 issue_code(kind='rebind')）/ kick（セッションだけ失効）。"""
    if owner['role'] != 'owner': raise AuthError('forbidden', 'オーナーのみ実行できます', 403)
    now = _now(now)
    with core._lock, db() as c:
        a = _own(c, owner, account_id)
        if a['role'] == 'owner': raise AuthError('forbidden', 'オーナーアカウント自身は操作できません', 400)
        if a['status'] == 'deleted': raise AuthError('deleted', '削除済みです', 400)
        t = iso(datetime.datetime.fromtimestamp(now, JST))
        if action == 'suspend':
            c.execute("UPDATE accounts SET status='suspended',updated_at=? WHERE id=?", (t, account_id)); _kill_sessions(c, account_id, 'suspended', now)
        elif action == 'resume':
            if a['status'] != 'suspended': raise AuthError('bad_state', '停止中ではありません', 400)
            c.execute("UPDATE accounts SET status='active',updated_at=? WHERE id=?", (t, account_id))
        elif action == 'delete':
            # 削除＝失効。未使用の登録コードも無効化。ログイン不可・パスワード再利用可・課金は削除日の翌日から対象外
            c.execute("UPDATE accounts SET status='deleted',deleted_at=?,updated_at=? WHERE id=?", (t, t, account_id)); _kill_sessions(c, account_id, 'deleted', now)
            c.execute('UPDATE reg_codes SET revoked_at=? WHERE target_account_id=? AND used_at IS NULL AND revoked_at IS NULL', (now, account_id))
        elif action == 'kick':
            _kill_sessions(c, account_id, 'kicked', now)
        elif action == 'reissue_prepare':   # 再発行の前段：旧パスワード・旧端末・旧セッションを即時無効化
            if a['status'] == 'suspended': raise AuthError('bad_state', '停止中です。先に再開してください', 400)
            c.execute('UPDATE accounts SET needs_rebind=1,updated_at=? WHERE id=?', (t, account_id)); _kill_sessions(c, account_id, 'reissue', now)
        else:
            raise AuthError('bad_action', '不明な操作', 400)
        event(c, owner['store_id'], account_id, action, owner['id'], '', now)

def reissue(owner, account_id, ttl_min=DEFAULT_TTL_MIN, now=None):
    """再発行：旧資格を即時失効し、新しいワンタイム登録コードを返す。"""
    control(owner, account_id, 'reissue_prepare', now)
    return issue_code(owner, 'rebind', '', ttl_min, account_id, now)

def list_accounts(owner, now=None):
    now = _now(now)
    with db() as c:
        out = []
        for a in c.execute("SELECT * FROM accounts WHERE store_id=? ORDER BY id", (owner['store_id'],)):
            s = c.execute('SELECT last_seen,created_at FROM sessions WHERE account_id=? AND revoked_at IS NULL AND expires_at>? ORDER BY id DESC LIMIT 1', (a['id'], now)).fetchone()
            d = public_account(dict(a)); d['online'] = bool(s); d['last_seen'] = _iso_ts(s['last_seen']) if s else None
            d['device_bound'] = bool(a['device_hash'])
            d['conversations'] = c.execute('SELECT COUNT(*) FROM conversations WHERE account_id=?', (a['id'],)).fetchone()[0]
            out.append(d)
        codes = []
        for r in c.execute('SELECT * FROM reg_codes WHERE store_id=? ORDER BY id DESC LIMIT 50', (owner['store_id'],)):
            state = 'used' if r['used_at'] else 'revoked' if r['revoked_at'] else 'expired' if r['expires_at'] < now else 'unused'
            codes.append({'id': r['id'], 'kind': r['kind'], 'label': r['label'], 'target_account_id': r['target_account_id'], 'state': state,
                          'created_at': _iso_ts(r['created_at']), 'expires_at': _iso_ts(r['expires_at']), 'used_by_account': r['used_by_account']})
        ev = [dict(r) for r in c.execute('SELECT ts_iso,account_id,event,actor_account_id,detail FROM account_events WHERE store_id=? ORDER BY id DESC LIMIT 50', (owner['store_id'],))]
    return {'accounts': out, 'codes': codes, 'events': ev, 'plan': plan_info(owner['store_id'])}

# ---------------- 店舗プラン表示（アカウント数・端末数と無関係。実決済なし） ----------------
def plan_info(store_id):
    """店舗プランの固定情報（【仮説】の月額と会話分数の店舗共通枠）。アカウント数・端末数で変わらない。"""
    plans = core.PRICING['billing']['plans']
    with db() as c:
        plan = c.execute('SELECT plan FROM stores WHERE id=?', (store_id,)).fetchone()['plan'] or 'light'
    return {'plan': plan, 'base_yen': plans[plan]['base_yen'], 'included_minutes': plans[plan]['included_minutes'],
            'note': '【仮説】店舗プランの月額（実決済なし）。アカウント数・端末数・同時利用台数では変わりません'}


# ---------------- 再認証（重要変更の前にパスワードを再入力させる） ----------------
REAUTH_TTL = 300
def reauth(acct, password, now=None, ip=None):
    """ログイン中のアカウント自身のパスワードを再確認し、このセッションを5分間『再認証済み』にする。
    重要変更（料金・営業時間・削除の保存、アカウント削除など）はこれが必須。失敗は5回で5分ロック。"""
    now = _now(now); key = f"reauth:{acct['id']}"
    f = FAILS.get(key, [0, 0])
    if f[1] > now: raise AuthError('locked', '失敗が続いたため5分間ロックしています', 429)
    if not isinstance(password, str) or not hmac.compare_digest(core._pw_hash(password, acct['pw_salt']), acct['pw_hash']):
        f[0] += 1
        if f[0] >= 5: f = [0, now + 300]
        FAILS[key] = f
        audit.log_standalone('reauth_fail', acct['store_id'], acct['id'], '', now)
        raise AuthError('bad_credentials', 'パスワードが違います', 403)
    FAILS.pop(key, None)
    with db() as c:
        c.execute('UPDATE sessions SET reauth_until=? WHERE id=?', (now + REAUTH_TTL, acct['session_id']))
        audit.log(c, 'reauth_ok', acct['store_id'], acct['id'], '', now)
    return now + REAUTH_TTL

def is_reauthed(acct, now=None):
    return (acct.get('reauth_until') or 0) > _now(now)
