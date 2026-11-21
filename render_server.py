"""Render用の起動ラッパー（server.py 本体は変えない）。
DEMO_NO_AUTH=1 のとき：ログインしていない来訪者を、ダミー店舗 demo-bar の店長として扱う
（URLを開くだけで接客ページも店長ページも使えるデモ用）。費用ガード（guard.py / limits.json）はそのまま有効。
データは架空のダミーのみで、再起動のたびに初期化される。
"""
import os, sys, time
import core, server

DEMO_SLUG = os.environ.get('DEMO_SLUG', 'demo-bar')

if os.environ.get('DEMO_NO_AUTH') == '1':
    _orig_acct = server.H._acct

    def _demo_acct(self):
        a = _orig_acct(self)
        if a: return a
        st = core.store_by_slug(DEMO_SLUG)
        if not st: return None
        with core.db() as c:
            r = c.execute("SELECT * FROM accounts WHERE store_id=? AND role='owner' AND status='active' ORDER BY id LIMIT 1",
                          (st['id'],)).fetchone()
        if not r: return None
        d = dict(r); d['session_id'] = 0
        d['reauth_until'] = time.time() + 3600   # 重要変更のパスワード再入力も不要
        return d

    server.H._acct = _demo_acct
    print('デモモード: ログイン不要（DEMO_NO_AUTH=1）', flush=True)


# ---- 運営用：店舗スナップショットの読み込み（実店舗データはリポジトリに置かず、運営の手元から送る）----
# 合言葉は運営の手元だけにあり、ここにはそのSHA-256だけを置く。ディスクは再起動で消えるので、消えたら運営が送り直す。
import hashlib, hmac, json, re
import demo_loader
BOOTSTRAP_SHA256 = 'f83ae44752dbe37238957f3a7ad87a5b4c4994d1492d890c58c9a1b78e67c57b'
with core.db() as _c:
    _c.execute('CREATE TABLE IF NOT EXISTS demo_public_kiosk(slug TEXT PRIMARY KEY)')

def _public_kiosk_slug(path):
    """お客さん画面のAPI（/api/k/<slug>/…）で、公開デモに指定された店舗なら slug を返す。店長APIには効かない。"""
    m = re.fullmatch(r'/api/k/([a-z0-9-]+)/(ask|tts|status|staff)', path.split('?')[0])
    if not m: return None
    with core.db() as c:
        return m.group(1) if c.execute('SELECT 1 FROM demo_public_kiosk WHERE slug=?', (m.group(1),)).fetchone() else None

_orig_acct2 = server.H._acct
def _kiosk_acct(self):
    slug = _public_kiosk_slug(self.path)
    if slug:   # 公開デモ店舗のお客さん画面：ログインなしでその店舗の接客端末として扱う（費用ガードはそのまま）
        st = core.store_by_slug(slug)
        with core.db() as c:
            r = c.execute("SELECT * FROM accounts WHERE store_id=? AND role='owner' AND status='active' ORDER BY id LIMIT 1", (st['id'],)).fetchone()
        if r:
            d = dict(r); d['session_id'] = 0; d['reauth_until'] = 0
            return d
    return _orig_acct2(self)
server.H._acct = _kiosk_acct

_orig_post = server.H.do_POST
def _do_post(self):
    if self.path.split('?')[0] != '/api/demo/load_store':
        return _orig_post(self)
    tok = self.headers.get('X-Komore-Bootstrap') or ''
    if not hmac.compare_digest(hashlib.sha256(tok.encode()).hexdigest(), BOOTSTRAP_SHA256):
        time.sleep(1); return self._send(403, {'error': 'forbidden'})
    try:
        body = self._json()
        slug = body['slug']
        if core.store_by_slug(slug): return self._send(409, {'error': 'already loaded'})
        sid, nq = demo_loader.load_snapshot(body['snapshot'], slug, body['owner_password'])
        if body.get('public_kiosk'):
            with core.db() as c: c.execute('INSERT OR IGNORE INTO demo_public_kiosk(slug) VALUES(?)', (slug,))
        return self._send(200, {'ok': True, 'store_id': sid, 'owner_questions': nq})
    except Exception as e:
        sys.stderr.write(f'ERROR load_store {type(e).__name__}\n')
        return self._send(400, {'error': type(e).__name__})
server.H.do_POST = _do_post

if __name__ == '__main__':
    if not os.environ.get('DASHSCOPE_API_KEY'): print('警告: DASHSCOPE_API_KEY 未設定', file=sys.stderr)
    srv = server.serve()
    print(f'http://{server.HOST}:{server.PORT}/ で起動', flush=True)
    srv.serve_forever()
