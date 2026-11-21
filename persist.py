"""Renderの一時ディスク向け。店データ一式（SQLiteと利用額台帳）を専用Supabaseへ保存し、起動時に復元する。
環境変数が無いときは何もしない（既存テスト・ローカル）。接続情報の値はログに出さない。
"""
import gzip, hashlib, io, json, os, signal, sqlite3, sys, tarfile, tempfile, threading, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

def _db_path():
    return os.environ.get('SK_DB', os.path.join(HERE, 'data', 'stageB.db'))

def _spend_dir():
    return os.environ.get('KOMORE_SPEND_DIR') or os.path.join(HERE, 'data', 'komore_spend')

def _url():
    return (os.environ.get('KOMORE_SUPABASE_URL') or '').rstrip('/')

def _token():
    return os.environ.get('KOMORE_PERSIST_TOKEN') or ''

def _fn():
    return os.environ.get('KOMORE_PERSIST_FN') or 'komore-persist'
TIMEOUT = 60
DEBOUNCE_SEC = 2.0
PERIOD_SEC = 30.0

_dirty = False
_lock = threading.Lock()
_worker_started = False

def enabled():
    return bool(_url() and _token())

def _fn_url():
    return _url() + '/functions/v1/' + _fn()

def _log(msg):
    sys.stderr.write('persist: ' + msg + '\n')

def _headers():
    return {'X-Komore-Persist-Token': _token(), 'User-Agent': 'komore-persist/1'}

def pack_bytes():
    """SQLiteの一貫バックアップと台帳を tar.gz にする。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w:gz') as tar:
        fd, tmp = tempfile.mkstemp(suffix='.db', prefix='komore_bak_')
        os.close(fd)
        try:
            if os.path.exists(_db_path()):
                src = sqlite3.connect(_db_path(), timeout=30)
                try:
                    dst = sqlite3.connect(tmp)
                    try:
                        src.backup(dst)
                    finally:
                        dst.close()
                finally:
                    src.close()
                tar.add(tmp, arcname='stageB.db')
            spend = [('ledger.jsonl', os.path.join(_spend_dir(), 'ledger.jsonl')),
                     ('total.json', os.path.join(_spend_dir(), 'total.json'))]
            for name, path in spend:
                if os.path.exists(path):
                    tar.add(path, arcname='komore_spend/' + name)
        finally:
            try: os.remove(tmp)
            except OSError: pass
    return raw.getvalue()

def _integrity_ok(dbfile):
    c = sqlite3.connect(dbfile)
    try:
        r = c.execute('pragma integrity_check').fetchone()
        return r and r[0] == 'ok'
    finally:
        c.close()

def unpack_bytes(blob, dest_db, dest_spend):
    bio = io.BytesIO(blob)
    with tarfile.open(fileobj=bio, mode='r:gz') as tar:
        names = tar.getnames()
        if any(n.startswith('/') or '..' in n.split('/') for n in names):
            raise ValueError('bad archive paths')
        tmpd = tempfile.mkdtemp(prefix='komore_un_')
        try:
            tar.extractall(tmpd, numeric_owner=True)
            db_src = os.path.join(tmpd, 'stageB.db')
            if not os.path.exists(db_src):
                raise ValueError('snapshot has no database')
            if not _integrity_ok(db_src):
                raise ValueError('sqlite integrity_check failed')
            os.makedirs(os.path.dirname(dest_db), exist_ok=True)
            os.makedirs(dest_spend, exist_ok=True)
            fd, tmpdb = tempfile.mkstemp(dir=os.path.dirname(dest_db), prefix='stageB.')
            os.close(fd)
            with open(db_src, 'rb') as i, open(tmpdb, 'wb') as o:
                o.write(i.read())
            os.replace(tmpdb, dest_db)
            src_spend = os.path.join(tmpd, 'komore_spend')
            for name in ('ledger.jsonl', 'total.json'):
                sp = os.path.join(src_spend, name)
                if os.path.exists(sp):
                    with open(sp, 'rb') as i, open(os.path.join(dest_spend, name), 'wb') as o:
                        o.write(i.read())
        finally:
            # cleanup extracted files
            for root, dirs, files in os.walk(tmpd, topdown=False):
                for f in files:
                    try: os.remove(os.path.join(root, f))
                    except OSError: pass
                for d in dirs:
                    try: os.rmdir(os.path.join(root, d))
                    except OSError: pass
            try: os.rmdir(tmpd)
            except OSError: pass

def _request(method, data=None):
    req = urllib.request.Request(_fn_url(), data=data, method=method, headers=_headers())
    if data is not None:
        req.add_header('Content-Type', 'application/gzip')
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
            return r.status, body, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b'', dict(e.headers or {})

def restore():
    """起動時。戻り値: 'skipped' | 'empty' | 'restored'。失敗は例外（空データで上書きしない）。"""
    if not enabled():
        return 'skipped'
    status, body, _hdr = _request('GET')
    if status == 404:
        _log('no snapshot yet')
        return 'empty'
    if status != 200 or not body:
        raise RuntimeError('restore failed HTTP ' + str(status))
    unpack_bytes(body, _db_path(), _spend_dir())
    _log('restored bytes=' + str(len(body)))
    return 'restored'

def restore_or_exit():
    try:
        st = restore()
        _log('boot ' + st)
    except Exception as e:
        _log('BOOT FAIL ' + type(e).__name__)
        sys.exit(2)

def save():
    if not enabled():
        return {'skipped': True}
    blob = pack_bytes()
    if len(blob) < 20:
        return {'ok': False, 'error': 'empty pack'}
    status, body, _hdr = _request('PUT', blob)
    if status != 200:
        raise RuntimeError('save failed HTTP ' + str(status))
    info = {}
    try:
        info = json.loads(body.decode() or '{}')
    except Exception:
        info = {'ok': True}
    _log('saved bytes=' + str(len(blob)))
    return {'ok': True, 'bytes': len(blob), 'sha256': info.get('sha256')}

def mark_dirty():
    global _dirty
    if not enabled():
        return
    with _lock:
        _dirty = True

def flush():
    global _dirty
    if not enabled():
        return None
    with _lock:
        if not _dirty:
            return None
        _dirty = False
    try:
        return save()
    except Exception as e:
        with _lock:
            _dirty = True
        _log('save fail ' + type(e).__name__)
        return None

def _worker():
    last = 0.0
    while True:
        time.sleep(DEBOUNCE_SEC)
        now = time.time()
        with _lock:
            d = _dirty
        if d and now - last >= DEBOUNCE_SEC:
            flush()
            last = time.time()
        elif now - last >= PERIOD_SEC:
            flush()
            last = now

def _on_stop(*_a):
    flush()

def start_worker():
    global _worker_started
    if not enabled() or _worker_started:
        return
    _worker_started = True
    t = threading.Thread(target=_worker, name='komore-persist', daemon=True)
    t.start()
    try:
        signal.signal(signal.SIGTERM, _on_stop)
    except Exception:
        pass
    import atexit
    atexit.register(flush)
