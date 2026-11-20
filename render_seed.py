"""Render用：ダミー店舗を初期化する（seed_demo.py のロジックをそのまま使う）。
Renderのディスクは再起動で消えるので、起動のたびに実行する。
オーナーのパスワードは環境変数 DEMO_OWNER_PASSWORD から受け取る（コード・ログには出さない）。
"""
import os, sys
import seed_demo

pw = os.environ.get('DEMO_OWNER_PASSWORD')
if not pw:
    sys.exit('DEMO_OWNER_PASSWORD 未設定')
seed_demo.secrets.token_urlsafe = lambda n=None: pw   # 乱数生成の代わりに環境変数の値を使う
seed_demo.main()
try: os.remove(seed_demo.CRED)   # パスワードを書いたファイルはサーバーに残さない
except FileNotFoundError: pass
