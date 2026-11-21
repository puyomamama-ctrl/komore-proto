"""コモレ分の DashScope 利用の別ログと累計上限（乃木決定 2026-11-21 17:08）。
- 鍵は LUV//U の Model Studio ワークスペースのもの（有料化済み）。コモレ分はここで別に記録し、累計 500円で自動停止する。
- 記録先: data/komore_spend/ledger.jsonl（1呼び出し1行：日時・用途・モデル・トークン等・USD・円）と total.json（累計）。
  鍵・本文・音声は記録しない。
- 上限チェックは「呼び出す前」。累計が上限以上なら SpendCapReached を出して呼ばない。
  1回の呼び出しで少し超える可能性があるため、残りが 1円 未満でも止める。
- 単価は pricing.json（定価）。無料枠で実際には請求されない分も、定価で数える（保守的）。
- 環境変数 KOMORE_SPEND_DIR で記録先を変えられる（テスト用）。
"""
import datetime, fcntl, json, os, threading
HERE = os.path.dirname(os.path.abspath(__file__))
DIR = os.environ.get('KOMORE_SPEND_DIR') or os.path.join(HERE, 'data', 'komore_spend')
CAP_JPY = float(os.environ.get('KOMORE_SPEND_CAP_JPY') or 500)
MARGIN_JPY = 1.0
PRICING = json.load(open(os.path.join(HERE, 'pricing.json')))
FX = PRICING['usd_jpy']
EXTRA_PRICE = {'qwen3.7-plus': {'in_per_m': 0.4, 'out_per_m': 1.6}}   # 採点用。定価 Singapore（2026-11-21 確認）
_lock = threading.Lock()

class SpendCapReached(RuntimeError): pass

def _paths():
    os.makedirs(DIR, exist_ok=True)
    return os.path.join(DIR, 'ledger.jsonl'), os.path.join(DIR, 'total.json')

def total():
    _, tp = _paths()
    try: return json.load(open(tp))
    except Exception: return {'jpy': 0.0, 'usd': 0.0, 'calls': 0}

def check(model=None):
    if model and model not in PRICING and model not in EXTRA_PRICE:
        raise SpendCapReached(f'単価未登録のモデルは呼ばない: {model}')
    t = total()
    if t['jpy'] >= CAP_JPY - MARGIN_JPY:
        raise SpendCapReached(f"コモレ分の累計 {t['jpy']:.1f}円 が上限 {CAP_JPY:.0f}円 に達したため停止")

def cost_usd(model, usage):
    p = PRICING.get(model) or EXTRA_PRICE.get(model) or {}
    return (usage.get('prompt_tokens', usage.get('in', 0)) / 1e6 * p.get('in_per_m', 0)
            + usage.get('completion_tokens', usage.get('out', 0)) / 1e6 * p.get('out_per_m', 0)
            + usage.get('seconds', 0) * p.get('per_sec', 0)
            + usage.get('characters', 0) / 10000 * p.get('per_10k_chars', 0))

def record(model, usage, purpose='', note=''):
    usd = cost_usd(model, usage or {})
    lp, tp = _paths()
    row = {'ts': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec='seconds'),
           'purpose': purpose or '', 'model': model, 'usage': {k: v for k, v in (usage or {}).items() if isinstance(v, (int, float))},
           'usd': round(usd, 7), 'jpy': round(usd * FX, 4)}
    if note: row['note'] = note
    with _lock, open(tp + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(lp, 'a') as f: f.write(json.dumps(row, ensure_ascii=False) + '\n')
        t = total(); t['usd'] = round(t['usd'] + usd, 7); t['jpy'] = round(t['usd'] * FX, 4); t['calls'] += 1
        t['cap_jpy'] = CAP_JPY; t['updated'] = row['ts']
        json.dump(t, open(tp, 'w'))
    return usd

def record_prior(jpy, note):
    """台帳を作る前の利用の見積もり（定価換算）を、上限の計算に含めるために1行だけ入れる。"""
    return _prior(jpy, note)

def _prior(jpy, note):
    lp, tp = _paths()
    row = {'ts': datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat(timespec='seconds'),
           'purpose': 'prior_estimate', 'model': '-', 'usage': {}, 'usd': round(jpy / FX, 7), 'jpy': jpy, 'note': note}
    with _lock, open(tp + '.lock', 'w') as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        with open(lp, 'a') as f: f.write(json.dumps(row, ensure_ascii=False) + '\n')
        t = total(); t['usd'] = round(t['usd'] + jpy / FX, 7); t['jpy'] = round(t['usd'] * FX, 4); t['calls'] += 0
        t['cap_jpy'] = CAP_JPY; t['updated'] = row['ts']; json.dump(t, open(tp, 'w'))

def summary():
    lp, _ = _paths(); by = {}
    if os.path.exists(lp):
        for l in open(lp):
            r = json.loads(l); k = r['purpose']; b = by.setdefault(k, {'calls': 0, 'jpy': 0.0, 'in': 0, 'out': 0})
            b['calls'] += 1; b['jpy'] = round(b['jpy'] + r['jpy'], 4)
            b['in'] += r['usage'].get('prompt_tokens', 0); b['out'] += r['usage'].get('completion_tokens', 0)
    return {'total': total(), 'by_purpose': by}

if __name__ == '__main__':
    print(json.dumps(summary(), ensure_ascii=False, indent=1))
