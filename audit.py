"""最小限の監査ログ（追記専用・ハッシュ連鎖）。
- 記録するのは「誰が・いつ・何をしたか」だけ。会話本文・音声・パスワード・登録コード本体は入れない。
- audit_log は DB トリガーで UPDATE/DELETE を拒否する。各行は直前行のハッシュを含む（verify() で改ざん検知）。
- 呼び出し側の接続 c の中で1文の INSERT…SELECT として書く（前行の取得と挿入が同一文＝並行しても連鎖が分岐しない）。
"""
import time
import core

SQL = """INSERT INTO audit_log(ts,store_id,account_id,event,detail,prev_hash,hash)
SELECT ?,?,?,?,?, p, sk_sha(p||'|'||?||'|'||IFNULL(?,'')||'|'||IFNULL(?,'')||'|'||?||'|'||?)
FROM (SELECT IFNULL((SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1),'') AS p)"""

def log(c, event, store_id=None, account_id=None, detail='', now=None):
    ts = core.iso(core.datetime.datetime.fromtimestamp(now, core.JST)) if now else core.iso()
    detail = (detail or '')[:200]
    c.execute(SQL, (ts, store_id, account_id, event, detail, ts, store_id, account_id, event, detail))

def log_standalone(event, store_id=None, account_id=None, detail='', now=None):
    with core.db() as c:
        log(c, event, store_id, account_id, detail, now)

def recent(store_id, limit=100):
    with core.db() as c:
        return [dict(r) for r in c.execute('SELECT id,ts,account_id,event,detail FROM audit_log WHERE store_id=? ORDER BY id DESC LIMIT ?', (store_id, limit))]

def verify():
    """連鎖を先頭から検証。(ok, 件数, 壊れた行id)"""
    import hashlib
    prev, n = '', 0
    with core.db() as c:
        for r in c.execute('SELECT * FROM audit_log ORDER BY id'):
            exp = hashlib.sha256(f"{prev}|{r['ts']}|{r['store_id'] if r['store_id'] is not None else ''}|{r['account_id'] if r['account_id'] is not None else ''}|{r['event']}|{r['detail']}".encode()).hexdigest()
            if r['prev_hash'] != prev or r['hash'] != exp:
                return False, n, r['id']
            prev = r['hash']; n += 1
    return True, n, None
