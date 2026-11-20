"""ダミー店舗（実在しない架空の店）を作る。運営用スクリプト（localhost・DBに直接書く）。
- 店舗ID: demo-bar。オーナーのパスワードは乱数で生成し data/demo_credentials.txt（権限600）にだけ書く。画面・ログ・docsには出さない。
- すでにあれば何もしない（パスワードは上書きしない）。実在店舗・個人情報は入れない。
使い方: python3 seed_demo.py
"""
import os, secrets, stat
import core, accounts as A

SLUG = 'demo-bar'
CRED = os.path.join(core.HERE, 'data', 'demo_credentials.txt')

def main():
    core.init_db()
    if core.store_by_slug(SLUG):
        print(f'{SLUG} は作成済み（認証情報: {CRED}）'); return
    pw = secrets.token_urlsafe(12)
    sid = A.create_store(SLUG, 'ダミー酒場 ワニ亭（架空）', pw, 'bar')
    os.makedirs(os.path.dirname(CRED), exist_ok=True)
    fd = os.open(CRED, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(f'store_id={SLUG}\nowner_password={pw}\n')
    os.chmod(CRED, stat.S_IRUSR | stat.S_IWUSR)
    with core.db() as c:
        w = lambda t, v: core.write_row(c, sid, t, 'add', v, source='seed')
        for d in range(5): w('hours', {'weekday': d, 'open': '20:00', 'close': '05:00', 'closed': 0})
        w('hours', {'weekday': 5, 'open': '20:00', 'close': '06:00', 'closed': 0}); w('hours', {'weekday': 6, 'closed': 1})
        w('special_hours', {'date_from': '2026-12-31', 'date_to': '2026-12-31', 'closed': 1, 'note': '年末休業'})
        for n, a in (('60分', 3000), ('90分', 4500), ('120分', 6000)): w('prices', {'name': n, 'amount_yen': a})
        w('prices', {'name': 'ハッピーアワー', 'amount_yen': 2500, 'weekdays': '4'})
        for n, p, ct in (('ハイボール', 600, 'ドリンク'), ('生ビール', 700, 'ドリンク'), ('唐揚げ', 800, 'フード')): w('menu', {'name': n, 'price_yen': p, 'category': ct})
        for m in ('現金', 'クレジットカード', 'PayPay'): w('payments', {'method': m})
        w('faqs', {'topic': '駐車場', 'answer': 'お店の駐車場はありません。近くのコインパーキングをご利用ください。'})
        w('faqs', {'topic': '喫煙', 'answer': '店内は全席禁煙です。'})
    print(f'{SLUG} を作成しました。ID/パスワードの保存先: {CRED}（値は表示しません）')

if __name__ == '__main__':
    main()
