"""区切りE：写真からの取り込み（メニュー・料金表・営業時間の貼り紙など）。2026-11-21
流れ: 店長が写真を送る → 長辺1600pxのJPEGに縮小（EXIF・位置情報は落ちる）→ qwen3.7-flash（画像入力）が項目ごとに抜き出す
→ コードで検証（数字が根拠の文字に含まれるか・曜日を根拠の表記から読み直す・支払いは店内の支払いと明記された時だけ）
→ 全項目を確認カードに：営業時間・営業日・支払いは1件ずつ「要確認」、その他はまとめて1枚（項目ごとに選べる）。承認されるまで接客には使わない。
写真そのものは保存しない（ハッシュとファイル名だけカードに残す）。費用は spend.py の台帳（用途 photo_import_extract、Komore上限500円）と api_calls に記録。"""
import base64, hashlib, io, json, re
import core, web_import as W

MAX_BYTES = 6 * 1024 * 1024
PHOTO_SYS = W.EXTRACT_SYS.replace('あなたは店舗サイトから店舗情報を抜き出す担当です。与えられた資料（ページ本文または画像）に**書いてあることだけ**を抜き出します。',
    'あなたは店舗のメニュー表・料金表・貼り紙の写真から店舗情報を抜き出す担当です。写真に**写っている文字に書いてあることだけ**を抜き出します。読めない・かすれている文字は推測せず "conflict" に「読めない箇所あり」と書く。') \
    .replace('evidence（資料の原文をそのまま、40字以内。曜日の表記を必ず含める）', 'evidence（写真の文字をそのまま書き写す、40字以内。曜日・金額の表記を必ず含める）')

def to_jpeg_b64(raw, max_side=1600):
    try:
        from PIL import Image, ImageOps
    except ImportError:   # 画像ライブラリの無い環境（例：依存を入れていないデモサーバー）では使えない旨を返す
        raise ValueError('このサーバーでは写真取り込みを使えません（画像ライブラリ未導入）')
    if len(raw) > MAX_BYTES: raise ValueError('写真が大きすぎます（6MBまで）')
    try:
        im = Image.open(io.BytesIO(raw)); im = ImageOps.exif_transpose(im)
    except Exception:
        raise ValueError('画像として読めませんでした（JPEG・PNG・WebP）')
    return W._jpeg(im, max_side)

def propose_from_photo(store_id, raw, filename='photo.jpg', account_id=None, now=None):
    now = now or core.now_jst()
    b64 = to_jpeg_b64(raw)
    name = re.sub(r'[^\w.\-ぁ-んァ-ヶ一-龠]', '_', str(filename or 'photo.jpg'))[:60]
    sha = hashlib.sha256(raw).hexdigest()[:16]
    src = {'id': 'P1', 'url': f'写真:{name}', 'type': 'image', 'jpeg_b64': b64}
    got, usage, cost = W._vlm(src, store_id, now.date().isoformat(), system=PHOTO_SYS, purpose='photo_import_extract')
    items = []
    for it in got:
        it['_source_id'] = 'P1'; it['_source_url'] = src['url']; it['_flags'] = W.check_item(it, src)
        items.append(it)
    meta = {'source': 'photo_import', 'url': src['url'], 'fetched_at': core.iso(now), 'sha256_16': sha, 'sources': [{'id': 'P1', 'url': src['url'], 'type': 'image'}]}
    r = W.items_to_cards(store_id, items, meta, account_id, ('photo', 'photo_must'), now.date().isoformat(), replace_key=sha)
    return r | {'usage': usage, 'cost_usd': cost, 'n_items': len(items),
                'excluded': [{'kind': it.get('kind'), 'name': it.get('name') or it.get('text'), 'flags': it['_flags']} for it in items if any(f.startswith(W.EXCLUDE_FLAGS) for f in it['_flags'])]}
