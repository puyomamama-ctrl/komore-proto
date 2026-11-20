"""スマホAI接客 試作品 HTTPサーバー（標準ライブラリのみ・localhost専用）
段階B：費用ガード（guard.py）・監査ログ（audit.py）・重要変更の画面承認／再認証・矛盾検出・保存方針の変更を組み込み。
権限：接客エンドポイント（/api/k/…）は店舗データを変更する経路を持たない。店舗データの変更は /api/admin/（オーナーのセッション）のみ。
店舗IDはセッションから決める。URL・ボディのslugは『どの店舗の接客画面か』の指定にすぎず、セッションの店舗と一致しなければ拒否する。
"""
import base64, io, json, os, re, sqlite3, sys, threading, time, urllib.parse, wave
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import core, audit, guard
import accounts as A

HOST = os.environ.get('SK_HOST', '127.0.0.1')
PORT = int(os.environ.get('SK_PORT', '8787'))
STATIC = os.path.join(core.HERE, 'static')
MAX_JSON = 3 * 1024 * 1024
MAX_AUDIO_BYTES = {'kiosk': 400 * 1024, 'owner': 1024 * 1024}   # wav以外（webm/mp4）の大きさ上限。wavは秒数で検査
ALLOW_STORE_CREATE = os.environ.get('SK_ALLOW_STORE_CREATE') == '1'   # 既定は無効（店舗は運営が seed_demo.py 等で作る）
EVAL = os.environ.get('SK_EVAL') == '1'
SEC_HEADERS = {'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
               'Content-Security-Policy': "default-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; media-src 'self' blob:; frame-ancestors 'none'",
               'Permissions-Policy': 'microphone=(self)'}
IMPORTANT_TABLES = {'prices', 'hours', 'special_hours'}

class H(BaseHTTPRequestHandler):
    server_version = 'sekkyaku-proto'
    protocol_version = 'HTTP/1.1'
    def log_message(self, fmt, *a):  # 音声・本文・クエリはログに出さない
        sys.stderr.write('%s %s\n' % (self.command, self.path.split('?')[0]))

    def _ip(self): return self.client_address[0]

    def _send(self, code, obj=None, ctype='application/json', body=None, headers=None):
        b = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in {**SEC_HEADERS, **(headers or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _json(self):
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_JSON:
            raise ValueError('too large')
        return json.loads(self.rfile.read(n) or b'{}')

    def _acct(self):
        """Cookie(sk_session)＋ヘッダ X-SK-Device を検証して現在のアカウントを返す。無効なら None。
        状態を変えるPOSTは独自ヘッダが必須（他サイトのフォームからは付けられない）＋SameSite=Strict＝CSRF対策。"""
        m = re.search(r'sk_session=([\w-]+)', self.headers.get('Cookie') or '')
        return A.auth_session(m.group(1) if m else None, self.headers.get('X-SK-Device'))

    def _owner(self):
        a = self._acct()
        return a if a and a['role'] == 'owner' else None

    def _cookie(self, tok):
        sec = '; Secure' if self.headers.get('X-Forwarded-Proto') == 'https' else ''
        return {'Set-Cookie': f'sk_session={tok}; HttpOnly; SameSite=Strict; Path=/{sec}'}

    def _denied_admin(self, d):
        return self._send(d.http if d.http != 200 else 429, {'error': d.message, 'code': d.reason, 'level': d.level, 'until': d.until})

    # ------------------------------------------------------------------ GET
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ('/', '/index.html'):
            return self._file('index.html')
        if p == '/admin':
            return self._file('admin.html')
        if re.fullmatch(r'/k/[a-z0-9-]+', p):
            return self._file('kiosk.html')
        if p.startswith('/static/'):
            return self._file(p[len('/static/'):])
        if p == '/api/me':
            a = self._acct()
            if not a: return self._send(401, {'error': 'login required'})
            st = core.db().execute('SELECT slug,name FROM stores WHERE id=?', (a['store_id'],)).fetchone()
            return self._send(200, {'account': A.public_account(a), 'store': dict(st), 'reauthed': A.is_reauthed(a)})
        if p == '/api/admin/accounts':
            o = self._owner()
            if not o: return self._send(401, {'error': 'owner login required'})
            return self._send(200, A.list_accounts(o))
        if p == '/api/admin/usage':
            o = self._owner()
            if not o: return self._send(401, {'error': 'owner login required'})
            return self._send(200, guard.usage_report(o['store_id']))
        if p == '/api/admin/audit':
            o = self._owner()
            if not o: return self._send(401, {'error': 'owner login required'})
            return self._send(200, {'events': audit.recent(o['store_id'], 100), 'chain_ok': audit.verify()[0]})
        if p == '/api/staff/unanswered':   # 店員用：未回答の質問（読み取りのみ。個人情報マスク済み・30日で消去）
            a = self._acct()
            if not a: return self._send(401, {'error': 'login required'})
            with core.db() as c:
                rows = [dict(r) for r in c.execute("SELECT ts,question,status FROM conversations WHERE store_id=? AND mode='kiosk' AND status NOT LIKE 'answered%' ORDER BY id DESC LIMIT 50", (a['store_id'],))]
                calls = [dict(r) for r in c.execute('SELECT id,ts,handled FROM staff_calls WHERE store_id=? ORDER BY id DESC LIMIT 20', (a['store_id'],))]
            return self._send(200, {'unanswered': rows, 'staff_calls': calls})
        if p == '/api/admin/store':
            o = self._owner()
            if not o: return self._send(401, {'error': 'login required'})
            sid = o['store_id']
            d = core.store_data(sid)
            d['facts'] = core.facts_for(sid)
            d['conflicts'] = sorted(set(core.find_conflicts(d)[0].values()))
            return self._send(200, d)
        if p == '/api/admin/logs':
            o = self._owner()
            if not o: return self._send(401, {'error': 'login required'})
            sid = o['store_id']
            with core.db() as c:
                hist = [dict(r) for r in c.execute('SELECT id,ts,source,action,table_name,row_id,before_json,after_json FROM history WHERE store_id=? ORDER BY id DESC LIMIT 100', (sid,))]
                conv = [dict(r) for r in c.execute('SELECT id,ts,mode,lang,question,status,fact_ids,timings_json,cost_usd FROM conversations WHERE store_id=? ORDER BY id DESC LIMIT 200', (sid,))]
                stats = dict(c.execute("""SELECT COUNT(*) n, SUM(status NOT LIKE 'answered%') unanswered, SUM(cost_usd) cost
                                          FROM conversations WHERE store_id=? AND mode='kiosk'""", (sid,)).fetchone())
                staff = [dict(r) for r in c.execute('SELECT * FROM staff_calls WHERE store_id=? ORDER BY id DESC LIMIT 50', (sid,))]
                stats['staff_calls'] = len(staff)
            return self._send(200, {'history': hist, 'conversations': conv, 'stats': stats, 'staff_calls': staff})
        m = re.fullmatch(r'/api/k/([a-z0-9-]+)/info', p)
        if m:
            s = core.store_by_slug(m.group(1))
            if not s: return self._send(404, {'error': 'no store'})
            return self._send(200, {'name': s['name'], 'slug': s['slug']})
        m = re.fullmatch(r'/api/k/([a-z0-9-]+)/status', p)   # 接客端末：縮退状態（セッション必須）
        if m:
            s = core.store_by_slug(m.group(1)); a = self._acct()
            if not s or not a or a['store_id'] != s['id']: return self._send(401, {'error': 'login required'})
            lv = guard.level(s['id'])
            out = {'level': lv['level'], 'reason': lv['reason'], 'until': lv['until']}
            if lv['level'] in ('L2', 'L3'):
                out.update(guard.degraded_payload(s['id'], guard.Denied(lv['level'], lv['reason'], guard.MSG[lv['level']], until=lv['until'])))
            return self._send(200, out)
        self._send(404, {'error': 'not found'})

    def _file(self, name):
        fp = os.path.normpath(os.path.join(STATIC, name))
        if not fp.startswith(STATIC + os.sep) or not os.path.isfile(fp):
            return self._send(404, {'error': 'not found'})
        ct = {'html': 'text/html; charset=utf-8', 'js': 'text/javascript; charset=utf-8', 'css': 'text/css; charset=utf-8'}.get(fp.rsplit('.', 1)[-1], 'application/octet-stream')
        self._send(200, body=open(fp, 'rb').read(), ctype=ct)

    # ------------------------------------------------------------------ POST
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        try:
            body = self._json()
            if p == '/api/stores':   # localhost試作のみ・既定は無効
                if not ALLOW_STORE_CREATE: return self._send(403, {'error': '店舗の作成は運営のみです（このサーバーでは無効）'})
                sid = A.create_store(body.get('slug'), body.get('name') or '', body.get('owner_password'), body.get('category') or '')
                return self._send(200, {'id': sid})
            if p == '/api/login':
                tok, acct, st = A.login(body.get('slug') or '', body.get('password') or '', body.get('device_id'), ip=self._ip())
                return self._send(200, {'ok': True, 'account': A.public_account(acct), 'store': {'slug': st['slug'], 'name': st['name']}}, headers=self._cookie(tok))
            if p == '/api/register':   # ワンタイム登録コードで新端末/新アカウント登録
                tok, acct, st = A.redeem_code(body.get('slug') or '', body.get('code') or '', body.get('password') or '', body.get('device_id'),
                                              str(body.get('name') or ''), str(body.get('device_label') or ''), ip=self._ip())
                return self._send(200, {'ok': True, 'account': A.public_account(acct), 'store': {'slug': st['slug'], 'name': st['name']}}, headers=self._cookie(tok))
            if p == '/api/logout':
                m = re.search(r'sk_session=([\w-]+)', self.headers.get('Cookie') or '')
                if m: A.logout(m.group(1))
                return self._send(200, {'ok': True}, headers={'Set-Cookie': 'sk_session=; Max-Age=0; Path=/'})
            if p.startswith('/api/admin/'):
                return self._admin_post(p, body)
            m = re.fullmatch(r'/api/k/([a-z0-9-]+)/staff', p)
            if m:
                st = core.store_by_slug(m.group(1))
                if not st: return self._send(404, {'error': 'no store'})
                a = self._acct()
                if not a or a['store_id'] != st['id']: return self._send(401, {'error': 'login required'})
                if not guard._rate_check('staffcall', (st['id'], self._ip()), 10, time.time()): return self._send(429, {'error': guard.MSG['rate']})
                with core.db() as c:
                    c.execute('INSERT INTO staff_calls(store_id,ts,session) VALUES(?,?,?)', (st['id'], core.iso(), str(body.get('session') or '')[:40]))
                return self._send(200, {'ok': True})
            m = re.fullmatch(r'/api/k/([a-z0-9-]+)/(ask|tts)', p)
            if m:
                s = core.store_by_slug(m.group(1))
                if not s: return self._send(404, {'error': 'no store'})
                a = self._acct()   # 接客端末は有効な店舗アカウントのセッションが必須（停止・削除・失効で即停止）
                if not a or a['store_id'] != s['id']: return self._send(401, {'error': 'login required'})
                return self._kiosk_ask(s['id'], body, a, 'kiosk') if m.group(2) == 'ask' else self._tts(s['id'], body)
            self._send(404, {'error': 'not found'})
        except A.AuthError as e:
            self._send(e.http, {'error': e.msg, 'code': e.code})
        except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as e:
            self._send(400, {'error': str(e)[:200]})
        except Exception as e:
            # 内部エラーの詳細（鍵・パス等が混ざり得る）は応答に出さない
            sys.stderr.write(f'ERROR {self.path.split("?")[0]} {type(e).__name__}\n')
            self._send(500, {'error': 'internal error'})

    def _audio(self, body, kind):
        b = base64.b64decode(body['audio_b64'])
        cap = guard.LIM['conversation']['turn_audio_max_sec' if kind == 'kiosk' else 'owner_audio_max_sec']
        if b[:4] == b'RIFF':
            with wave.open(io.BytesIO(b)) as w:
                if w.getnframes() / float(w.getframerate()) > cap: raise ValueError(f'音声が長すぎます（最大{cap}秒）')
        elif len(b) > MAX_AUDIO_BYTES[kind]:
            raise ValueError(f'音声が長すぎます（最大{cap}秒）')
        return b, body.get('mime') or 'audio/webm'

    # ---- お客さん接客（読み取り専用：店舗データを変更するコード経路を持たない） ----
    def _kiosk_ask(self, sid, body, acct, kind):
        audio = self._audio(body, 'kiosk') if body.get('audio_b64') and kind == 'kiosk' else None   # 長すぎる音声は予約前に拒否
        session = str(body.get('session') or '')[:40]
        try:
            tk = guard.begin_turn(sid, kind, session, self._ip())
        except guard.Denied as d:
            if d.level == 'rate': return self._send(429, {'error': d.message, 'code': d.reason})
            return self._send(200, guard.degraded_payload(sid, d))
        t0 = time.time(); timings = {}; usage = {}; cost = 0.0; err = False; q = ''; res = None
        try:
            if audio:
                q, ai = core.asr(audio[0], audio[1], None, sid, 'kiosk_asr')
                timings['asr_ms'] = ai['ms']; usage['asr_sec'] = ai['usage']['seconds']; cost += ai['cost_usd']
            else:
                q = str(body.get('text') or '')
                timings['asr_ms'] = 0; usage['asr_sec'] = 0
                if body.get('client_stt'): usage['client_stt'] = True
            q = q.strip()[:core.MAX_QUESTION_CHARS]
            now = None
            if EVAL and body.get('now'):  # 実測用の時刻固定（本番では受け付けない）
                now = core.datetime.datetime.fromisoformat(body['now']).replace(tzinfo=core.JST)
            if len(q) < 1:   # 無音・認識なしは LLM に送らない（原価ゼロ）
                res = {'answer': core.FALLBACK['ja'], 'status': 'fallback', 'reason': 'empty', 'lang': 'ja', 'fact_ids': [],
                       'llm': {'model': '-', 'ms': 0, 'usage': {'in': 0, 'out': 0}, 'cost_usd': 0.0}}
            else:
                res = core.answer_question(sid, q, now=now, model=(body.get('model') if EVAL else None), purpose=body.get('purpose') or 'kiosk_ask')
            timings['llm_ms'] = res['llm']['ms']; usage['llm_in'] = res['llm']['usage']['in']; usage['llm_out'] = res['llm']['usage']['out']
            usage['llm_model'] = res['llm']['model']; cost += res['llm']['cost_usd']
        except Exception as e:   # 事業者API障害など：誤回答せず定型文＋スタッフ案内。エラーとして計上
            err = True; sys.stderr.write(f'provider error {type(e).__name__}\n')
            res = {'answer': core.FALLBACK['ja'], 'status': 'fallback', 'reason': 'provider_error', 'lang': 'ja', 'fact_ids': [],
                   'llm': {'model': '-', 'ms': 0, 'usage': {'in': 0, 'out': 0}, 'cost_usd': 0.0}}
        timings['server_ask_ms'] = round((time.time() - t0) * 1000)
        mode = body.get('mode') if body.get('mode') in ('kiosk', 'test', 'eval') else 'kiosk'
        if kind == 'test': mode = 'test'
        cid, tok = core.record_conversation(sid, mode, session, q, res, timings, usage, cost, (acct or {}).get('id'))
        yen = cost * guard.fx()
        guard.end_turn(tk, actual_yen=yen + guard.tts_est_yen(len(res['answer'])), actual_sec=usage.get('asr_sec', 0) + len(res['answer']) / 5,
                       error=err, question=q, status=res['status'] + ':' + res['reason'])
        return self._send(200, {'conv_id': cid, 'tts_token': tok, 'question': q, 'answer': res['answer'], 'status': res['status'],
                                'reason': res['reason'], 'lang': res['lang'], 'fact_ids': res['fact_ids'], 'timings': timings, 'cost_usd': cost,
                                'staff_call': res['status'] != 'answered'})

    def _tts(self, store_id, body):
        """サーバーが作った文だけ読み上げる（tts_token必須・1回限り・30秒で失効）。24kHz s16le をチャンク転送。"""
        r = core.tts_take(store_id, body.get('tts_token'))
        if not r:
            return self._send(403, {'error': 'bad token'})
        self._stream_tts(r['answer'], r['lang'], conv_id=r['conv_id'], store_id=store_id, est_chars=len(r['answer']))

    def _stream_tts(self, text, lang, conv_id=None, store_id=None, est_chars=0):
        self.send_response(200)
        self.send_header('Content-Type', 'audio/L16; rate=24000; channels=1')
        self.send_header('Transfer-Encoding', 'chunked')
        self.send_header('Cache-Control', 'no-store')
        for k, v in SEC_HEADERS.items(): self.send_header(k, v)
        self.end_headers()
        info = None
        for ch in core.tts_stream(text, lang):
            if isinstance(ch, tuple):
                info = ch[1]; break
            self.wfile.write(b'%x\r\n%s\r\n' % (len(ch), ch)); self.wfile.flush()
        self.wfile.write(b'0\r\n\r\n')
        if info:
            if conv_id: yen = core.add_tts_to_conv(conv_id, info, store_id) * guard.fx()
            else: yen = core.log_call('tts', 'qwen3-tts-flash', info['total_ms'], info['usage'], store_id, None, 'owner_tts') * guard.fx()
            if est_chars or not conv_id:   # 予約時の見積（文字数×2カウント）との差を確定
                guard.add_cost(store_id, yen - (guard.tts_est_yen(est_chars) if conv_id else 0))

    # ---- 店長（パスワード認証必須） ----
    def _admin_post(self, p, body):
        owner = self._owner()
        if not owner:
            return self._send(401, {'error': 'login required'})
        sid = owner['store_id']
        if p == '/api/admin/reauth':   # 重要変更の前のパスワード再入力
            until = A.reauth(owner, body.get('password'), ip=self._ip())
            return self._send(200, {'ok': True, 'reauth_seconds': A.REAUTH_TTL})
        if p == '/api/admin/codes':   # ワンタイム登録コード発行（平文はこの応答でのみ返る）
            return self._send(200, A.issue_code(owner, 'new_account', str(body.get('label') or ''), body.get('ttl_min', A.DEFAULT_TTL_MIN)))
        m = re.fullmatch(r'/api/admin/codes/(\d+)/revoke', p)
        if m:
            return self._send(200, {'revoked': A.revoke_code(owner, int(m.group(1)))})
        m = re.fullmatch(r'/api/admin/accounts/(\d+)/(suspend|resume|delete|kick)', p)
        if m:
            if m.group(2) == 'delete' and not A.is_reauthed(owner):   # アカウント削除（失効）は再認証必須
                return self._send(401, {'error': 'パスワードの再入力が必要です', 'code': 'reauth_required'})
            A.control(owner, int(m.group(1)), m.group(2)); return self._send(200, {'ok': True})
        m = re.fullmatch(r'/api/admin/accounts/(\d+)/reissue', p)
        if m:
            if not A.is_reauthed(owner): return self._send(401, {'error': 'パスワードの再入力が必要です', 'code': 'reauth_required'})
            r = A.reissue(owner, int(m.group(1)), body.get('ttl_min', A.DEFAULT_TTL_MIN)); return self._send(200, r)
        if p == '/api/admin/crud':   # 手入力フォーム（明示的な画面操作）。重要な表・削除は再認証必須
            table, action = body.get('table'), body.get('action')
            vals = core.clean_values(table, body.get('values')) if action != 'delete' else {}
            important = table in IMPORTANT_TABLES or action == 'delete' or (table == 'menu' and vals.get('price_yen') is not None)
            if important and not A.is_reauthed(owner):
                return self._send(401, {'error': 'パスワードの再入力が必要です', 'code': 'reauth_required'})
            with core.db() as c:
                rid = core.write_row(c, sid, table, action, vals, body.get('id'), 'form')
                audit.log(c, 'change_saved', sid, owner['id'], f'via=form {table} {action} id={rid}')
            conflicts = sorted(set(core.find_conflicts(core.store_data(sid))[0].values()))
            return self._send(200, {'id': rid, 'conflicts': conflicts})
        if p == '/api/admin/voice':
            audio = self._audio(body, 'owner') if body.get('audio_b64') else None
            try: tk = guard.begin_turn(sid, 'owner', '', self._ip())
            except guard.Denied as d: return self._denied_admin(d)
            t0 = time.time(); tr = None; err = False; cost = 0.0
            try:
                if audio:
                    text, ai = core.asr(audio[0], audio[1], 'ja', sid, 'owner_asr'); tr = ai; cost += ai['cost_usd']
                else:
                    text = str(body.get('text') or '')[:1000]
                r = core.owner_propose(sid, text, account_id=owner['id'])
                cost += r['llm']['cost_usd']
            except Exception:
                err = True; raise
            finally:
                guard.end_turn(tk, actual_yen=cost * guard.fx(), error=err)
            with core.db() as c: audit.log(c, 'change_proposed', sid, owner['id'], f"ops={r['ops_hash']} important={int(r['important'])} n={len(r['ops'])}")
            r['transcript'] = text; r['asr'] = tr; r['total_ms'] = round((time.time() - t0) * 1000)
            return self._send(200, r)
        if p == '/api/admin/confirm':   # 声または「はい／いいえ」ボタン。重要変更は声では保存されない
            if body.get('audio_b64'):
                audio = self._audio(body, 'owner')
                try: tk = guard.begin_turn(sid, 'test', '', self._ip())
                except guard.Denied as d: return self._denied_admin(d)
                err = False; yen = 0.0
                try:
                    text, ai = core.asr(audio[0], audio[1], 'ja', sid, 'owner_confirm_asr'); yen = ai['cost_usd'] * guard.fx()
                except Exception:
                    err = True; raise
                finally:
                    guard.end_turn(tk, actual_yen=yen, error=err)
            else:
                text = str(body.get('text') or '')
            r = core.owner_confirm(sid, body.get('pending_id'), text, owner['id'])
            r['transcript'] = text
            return self._send(200, r)
        if p == '/api/admin/approve':   # 画面の承認ボタン。表示したops_hashと一致した場合のみ。重要変更はパスワード再入力が必須
            if body.get('password'):
                A.reauth(owner, body['password'], ip=self._ip()); owner = self._owner()
            r = core.owner_approve(sid, body.get('pending_id'), body.get('ops_hash'), owner['id'], A.is_reauthed(owner))
            return self._send(401 if r['result'] == 'reauth_required' else 200, r | ({'code': 'reauth_required'} if r['result'] == 'reauth_required' else {}))
        if p == '/api/admin/say':  # 確認文の読み上げ（pendingの確認文 or 定型メッセージのみ）
            text = None
            with core.db() as c:
                if body.get('pending_id'):
                    r = c.execute('SELECT confirm_text FROM pending WHERE id=? AND store_id=?', (body['pending_id'], sid)).fetchone()
                    text = r['confirm_text'] if r else None
            if body.get('fixed') in ('保存しました。', '保存を取り消しました。', '「はい」か「いいえ」でお答えください。保存してよろしいですか？'):
                text = body['fixed']
            if not text: return self._send(400, {'error': 'nothing to say'})
            try: tk = guard.begin_turn(sid, 'owner_say', '', self._ip())
            except guard.Denied as d: return self._denied_admin(d)
            try:
                return self._stream_tts(text, 'ja', store_id=sid)
            finally:
                guard.end_turn(tk, actual_yen=0.0)
        if p == '/api/admin/test_ask':
            body = dict(body); body.pop('audio_b64', None)
            return self._kiosk_ask(sid, body, owner, 'test')
        return self._send(404, {'error': 'not found'})

def serve(host=HOST, port=PORT):
    core.init_db()
    core.purge()
    def loop():
        while True:
            time.sleep(3600)
            try: core.purge()
            except Exception as e: sys.stderr.write(f'purge error {type(e).__name__}\n')
    threading.Thread(target=loop, daemon=True).start()
    return ThreadingHTTPServer((host, port), H)

if __name__ == '__main__':
    for k in ('DASHSCOPE_API_KEY',):
        if not os.environ.get(k): print(f'警告: {k} 未設定', file=sys.stderr)
    srv = serve()
    print(f'http://{HOST}:{PORT}/ で起動', flush=True)
    srv.serve_forever()
