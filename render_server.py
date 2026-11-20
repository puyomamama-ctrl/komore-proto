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

if __name__ == '__main__':
    if not os.environ.get('DASHSCOPE_API_KEY'): print('警告: DASHSCOPE_API_KEY 未設定', file=sys.stderr)
    srv = server.serve()
    print(f'http://{server.HOST}:{server.PORT}/ で起動', flush=True)
    srv.serve_forever()
