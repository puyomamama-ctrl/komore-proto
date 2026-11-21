"""Render用：店舗データのスナップショット（JSON）を店舗として読み込む汎用ローダー。AI・外部APIは使わない。
実店舗のデータはこのリポジトリに置かない。スナップショットは運営の手元から /api/demo/load_store に送る（render_server.py）。
形式：{name, category, tables:{表名:[行]}, price_tiers, owner_fixes, tier_prices, tier_faqs, owner_checks}
"""
import json
import core, accounts as A

def _src(c, sid, table, rid, s):
    if not s: return
    c.execute('INSERT INTO row_sources(store_id,table_name,row_id,kind,url,fetched_at,approved_at) VALUES(?,?,?,?,?,?,?)',
              (sid, table, rid, s.get('kind'), s.get('url'), s.get('fetched_at'), s.get('approved_at') or s.get('fetched_at')))

def _clean(v): return {k: x for k, x in v.items() if not k.startswith('_')}

def load_snapshot(ws, slug, owner_password):
    sid = A.create_store(slug, ws['name'], owner_password, ws.get('category') or '')
    with core.db() as c:
        for v in ws.get('price_tiers', []): core.write_row(c, sid, 'price_tiers', 'add', _clean(v), source='web_import_approved')
        for t, rows in ws.get('tables', {}).items():
            for v in rows: core.write_row(c, sid, t, 'add', _clean(v), source='web_import_approved')
        for f in ws.get('owner_fixes', []):   # 店長の回答で直した項目（出典を行ごとに残す）
            act = f.get('action', 'upsert')
            if act == 'set_tier_untiered':
                for r in c.execute('SELECT id FROM prices WHERE store_id=? AND tier IS NULL', (sid,)).fetchall():
                    core.write_row(c, sid, 'prices', 'update', {'tier': f['tier']}, r['id'], source='owner_answer'); _src(c, sid, 'prices', r['id'], f.get('_src'))
                continue
            if f['table'] not in core.TABLES: raise ValueError('bad table')
            col, val = next(iter(f['key'].items()))
            if col not in core.TABLES[f['table']]: raise ValueError('bad key')
            ex = c.execute(f"SELECT id FROM {f['table']} WHERE store_id=? AND {col}=?", (sid, val)).fetchone()
            if act == 'delete':
                if ex: core.write_row(c, sid, f['table'], 'delete', rid=ex['id'], source='owner_answer')
                continue
            rid = core.write_row(c, sid, f['table'], 'update' if ex else 'add', f['values'], ex['id'] if ex else None, source='owner_answer')
            _src(c, sid, f['table'], rid, f.get('_src'))
        for v in ws.get('tier_prices', []):
            rid = core.write_row(c, sid, 'prices', 'add', _clean(v), source='manual_approved'); _src(c, sid, 'prices', rid, v.get('_src'))
        for v in ws.get('tier_faqs', []):
            rid = core.write_row(c, sid, 'faqs', 'add', _clean(v), source='manual_approved'); _src(c, sid, 'faqs', rid, v.get('_src'))
        for q in ws.get('owner_checks', []):
            ts = core.iso(); draft = {'known': False, 'reply': '［　］', 'topic': q['topic'], 'choices': q.get('choices', []), 'ts': ts, 'source': q.get('_src')}
            c.execute('INSERT INTO qgroups(store_id,category,norm,sample,first_ts,last_ts,n,draft_json,kind) VALUES(?,?,?,?,?,?,0,?,?)',
                      (sid, '時間', core.norm_question(q['sample']), q['sample'], ts, ts, json.dumps(draft, ensure_ascii=False), 'owner_q'))
    return sid, core.ensure_tier_questions(sid)
