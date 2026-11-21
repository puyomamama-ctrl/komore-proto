// 録音（無音で自動停止）と 24kHz PCM ストリーム再生
const SK = {};
SK.ctx = null;
SK.ensureCtx = () => { if (!SK.ctx) SK.ctx = new (window.AudioContext || window.webkitAudioContext)(); if (SK.ctx.state === 'suspended') SK.ctx.resume(); return SK.ctx; };
SK.pickMime = () => ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
// マイクは一度開いたら使い回す（毎回開き直すと iPhone では話し始めが欠けやすい）
// 音はPCMで受け取り、待ち受け中の周りの音（テレビ・隣の会話）を覚えておく。録音では「周りの音より十分大きい区間」
// ＝近くで話している声だけを残し、それ以外は切り捨てて 16kHz WAV で送る。
SK.VAD = { K: 2.0, ringSec: 5, silenceMs: 1200, preMs: 300, postMs: 600, gapMs: 1500, noSpeechMs: 6000, preRollMs: 500 };
SK.mic = null;
SK.isPlaying = () => (SK.ctx && SK.playEnd && SK.ctx.currentTime < SK.playEnd + 0.3) || (window.speechSynthesis && speechSynthesis.speaking);
SK.openMic = async () => {
  const ctx = SK.ensureCtx();
  const m = SK.mic;
  if (m && m.stream.getAudioTracks().some(t => t.readyState === 'live')) return m;
  if (m && m.opening) return m.opening;
  const opening = (async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    const src = ctx.createMediaStreamSource(stream);
    const mic = { stream, src, ring: [], pre: [], recording: false, sr: ctx.sampleRate, lastPcm: 0, onChunk: null };
    if (ctx.createScriptProcessor) {
      const sp = ctx.createScriptProcessor(2048, 1, 1); mic.sp = sp;
      const mute = ctx.createGain(); mute.gain.value = 0; mic.mute = mute;
      const ringMax = Math.round(SK.VAD.ringSec * ctx.sampleRate / 2048), preMax = Math.ceil(SK.VAD.preRollMs / 1000 * ctx.sampleRate / 2048);
      sp.onaudioprocess = e => {
        const x = new Float32Array(e.inputBuffer.getChannelData(0)); let s = 0; for (const v of x) s += v * v;
        const r = Math.sqrt(s / x.length); mic.lastPcm = performance.now();
        if (mic.onChunk) { mic.onChunk(x, r); return; }
        mic.pre.push([x, r]); if (mic.pre.length > preMax) mic.pre.shift();
        if (!SK.isPlaying()) { mic.ring.push(r); if (mic.ring.length > ringMax) mic.ring.shift(); }   // 自分の読み上げ中は周りの音に数えない
      };
      src.connect(sp); sp.connect(mute); mute.connect(ctx.destination);
    }
    SK.mic = mic; return mic;
  })();
  SK.mic = { stream: new MediaStream(), opening };
  try { return await opening; } catch (e) { SK.mic = null; throw e; }
};
SK.closeMic = () => {
  const m = SK.mic; SK.mic = null;
  if (!m || m.opening) return;
  try { m.src.disconnect(); m.sp && m.sp.disconnect(); m.mute && m.mute.disconnect(); } catch (e) {}
  m.stream.getTracks().forEach(t => t.stop());
};
SK.pct = (a, p) => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(b.length * p))]; };
// 近くの声のしきい値：静かな側（下位20%）の2.5倍＋α、かつ周りの音の大きい側（上位10%）の K 倍より大きいこと
SK.nearThresh = ring => {
  const ok = ring && ring.length >= 5, floor = ok ? SK.pct(ring, 0.2) : 0.008, bg = ok ? SK.pct(ring, 0.9) : 0;
  return Math.max(0.02, floor * 2.5 + 0.012, bg * SK.VAD.K);
};
SK.wav16k = (parts, sr) => {   // Float32 の配列 → 16kHz 16bit モノラル WAV
  let n = 0; for (const p of parts) n += p.length;
  const x = new Float32Array(n); let o = 0; for (const p of parts) { x.set(p, o); o += p.length; }
  const ratio = sr / 16000, m = Math.floor(x.length / ratio), out = new Int16Array(m);
  for (let i = 0; i < m; i++) { const a = Math.floor(i * ratio), b = Math.min(x.length, Math.floor((i + 1) * ratio)); let s = 0; for (let j = a; j < b; j++) s += x[j]; const v = Math.max(-1, Math.min(1, s / Math.max(1, b - a))); out[i] = v < 0 ? v * 32768 : v * 32767; }
  const buf = new ArrayBuffer(44 + out.length * 2), dv = new DataView(buf); const w = (p, t) => { for (let i = 0; i < t.length; i++) dv.setUint8(p + i, t.charCodeAt(i)); };
  w(0, 'RIFF'); dv.setUint32(4, 36 + out.length * 2, true); w(8, 'WAVE'); w(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true); w(36, 'data'); dv.setUint32(40, out.length * 2, true);
  new Int16Array(buf, 44).set(out);
  let bin = ''; const u8 = new Uint8Array(buf); for (let i = 0; i < u8.length; i += 0x8000) bin += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
  return btoa(bin);
};
// 録音（近くの声だけ・無音で自動停止）。onStart は実際に録音が始まった瞬間に呼ばれる。
// 返り値 voiced=false のときは近くで話した声が無かった（周りの音だけ）ので、認識に送らない。
SK.record = async ({ maxMs = 15000, silenceMs = SK.VAD.silenceMs, onLevel, onStart } = {}) => {
  const mic = await SK.openMic();
  const alive = mic.sp && performance.now() - mic.lastPcm < 600;
  if (!alive && mic.sp) await new Promise(r => setTimeout(r, 300));
  if (!mic.sp || performance.now() - mic.lastPcm > 600) return SK.recordMR(mic, { maxMs, silenceMs, onLevel, onStart });
  const V = SK.VAD, cs = 2048 / mic.sr * 1000;
  const thr = SK.nearThresh(mic.ring);
  const items = mic.pre.slice(); const npre = items.length; mic.pre = [];
  const loud = items.map(([, r]) => r > thr);
  let spoke = false, lastLoud = 0, k = 0; const t0 = performance.now();
  return new Promise(res => {
    const finish = () => {
      mic.onChunk = null; mic.recording = false;
      const a = Math.round(V.preMs / cs), b = Math.round(V.postMs / cs), g = Math.round(V.gapMs / cs);
      const keep = new Array(items.length).fill(false);
      loud.forEach((L, i) => { if (L) for (let j = Math.max(0, i - a); j <= Math.min(items.length - 1, i + b); j++) keep[j] = true; });
      const idx = []; keep.forEach((x, i) => x && idx.push(i));
      for (let i = 0; i + 1 < idx.length; i++) if (idx[i + 1] - idx[i] <= g) for (let j = idx[i]; j < idx[i + 1]; j++) keep[j] = true;
      const parts = items.filter((_, i) => keep[i]).map(([x]) => x);
      const voiced = spoke && parts.length > 0;
      res({ b64: voiced ? SK.wav16k(parts, mic.sr) : '', mime: 'audio/wav', ms: performance.now() - t0, endedAt: performance.now(), spoke, voiced,
            kept_ms: Math.round(parts.length * cs), thresh: thr });
    };
    SK.stopRec = () => { if (mic.onChunk) finish(); };
    mic.recording = true;
    mic.onChunk = (x, r) => {
      items.push([x, r]); const L = r > thr; loud.push(L); k++;
      onLevel && onLevel(r);
      const t = k * cs, recent = loud.slice(-4).filter(Boolean).length;
      if (L && (spoke || recent >= 2)) spoke = true;
      if (L) lastLoud = t;
      if ((spoke && t - lastLoud > silenceMs) || t > maxMs || (!spoke && t > V.noSpeechMs)) finish();
    };
    onStart && onStart();
  });
};
// ScriptProcessor が使えないブラウザ向け（従来の MediaRecorder 録音）
SK.recordMR = (mic, { maxMs, silenceMs, onLevel, onStart }) => {
  const ctx = SK.ensureCtx(); const an = ctx.createAnalyser(); an.fftSize = 1024; mic.src.connect(an); const buf = new Float32Array(1024);
  const lv = () => { an.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v; return Math.sqrt(s / buf.length); };
  const mime = SK.pickMime(); const rec = new MediaRecorder(mic.stream, mime ? { mimeType: mime } : {});
  const chunks = []; rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  const thr = SK.nearThresh(mic.ring);
  let spoke = false, lastLoud = 0, t0 = 0, started = false, recent = [];
  SK.stopRec = () => rec.state === 'recording' && rec.stop();
  return new Promise((res, rej) => {
    rec.onerror = e => { mic.recording = false; rej(e.error || new Error('録音エラー')); };
    rec.onstop = async () => {
      mic.recording = false; try { mic.src.disconnect(an); } catch (e) {}
      const blob = new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' });
      const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(',')[1]); fr.readAsDataURL(blob); });
      res({ b64, mime: (blob.type || 'audio/webm').split(';')[0], ms: performance.now() - t0, endedAt: performance.now(), spoke, voiced: spoke, thresh: thr });
    };
    const begin = () => {
      if (started) return; started = true; mic.recording = true; t0 = performance.now(); lastLoud = t0; onStart && onStart();
      const tick = () => {
        if (rec.state !== 'recording') return;
        const r = lv(); onLevel && onLevel(r); const now = performance.now(); const L = r > thr; recent.push(L); if (recent.length > 4) recent.shift();
        if (L && (spoke || recent.filter(Boolean).length >= 2)) spoke = true;
        if (L) lastLoud = now;
        if ((spoke && now - lastLoud > silenceMs) || now - t0 > maxMs || (!spoke && now - t0 > SK.VAD.noSpeechMs)) return rec.stop();
        requestAnimationFrame(tick);
      };
      tick();
    };
    rec.onstart = begin; rec.start(200); setTimeout(() => { if (rec.state === 'recording') begin(); }, 600);
  });
};
SK.sources = [];
SK.stopAudio = () => { SK.sources.forEach(s => { try { s.stop(); } catch (e) {} }); SK.sources = []; SK.playEnd = 0; SK.abort && SK.abort.abort(); };
// サーバーの /tts から PCM(s16le, 24kHz) を読みながら順に再生。最初の音が鳴った時刻を返す
SK.playPcmStream = async (url, body, onFirst) => {
  const ctx = SK.ensureCtx(); SK.stopAudio(); SK.abort = new AbortController();
  const r = await fetch(url, { method: 'POST', headers: SK.hdr(), body: JSON.stringify(body), signal: SK.abort.signal });
  if (!r.ok) throw new Error('tts ' + r.status);
  const reader = r.body.getReader(); let carry = new Uint8Array(0); let t = ctx.currentTime + 0.05; let first = true;
  while (true) {
    const { done, value } = await reader.read(); if (done) break;
    const all = new Uint8Array(carry.length + value.length); all.set(carry); all.set(value, carry.length);
    const n = all.length - (all.length % 2); carry = all.slice(n);
    if (n < 2) continue;
    const i16 = new Int16Array(all.buffer.slice(0, n)); const ab = ctx.createBuffer(1, i16.length, 24000); const ch = ab.getChannelData(0);
    for (let i = 0; i < i16.length; i++) ch[i] = i16[i] / 32768;
    const s = ctx.createBufferSource(); s.buffer = ab; s.connect(ctx.destination); t = Math.max(t, ctx.currentTime); s.start(t); t += ab.duration; SK.playEnd = t; SK.sources.push(s);
    if (first) { first = false; onFirst && onFirst(performance.now()); }
  }
  return new Promise(res => setTimeout(res, Math.max(0, (t - ctx.currentTime) * 1000)));
};
SK.speakLocal = (text, lang) => new Promise(res => { const u = new SpeechSynthesisUtterance(text); u.lang = ({ en: 'en-US', zh: 'zh-CN', ko: 'ko-KR', es: 'es-ES', fr: 'fr-FR', de: 'de-DE', it: 'it-IT', pt: 'pt-BR', ru: 'ru-RU', th: 'th-TH', vi: 'vi-VN', id: 'id-ID' })[lang] || 'ja-JP'; u.onend = res; u.onerror = res; speechSynthesis.cancel(); speechSynthesis.speak(u); });
// 端末ID：この端末のブラウザに保存するランダム値（登録コードで店員アカウントに紐付く。安全管理用で料金とは無関係）
SK.deviceId = () => { let d = localStorage.getItem('sk_device'); if (!d) { d = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2) + Date.now()); localStorage.setItem('sk_device', d); } return d; };
SK.adminSlug = () => { const m = (location.pathname || '').match(/^\/admin\/([a-z0-9-]+)$/); return m ? m[1] : ''; };
SK.hdr = () => {
  const h = { 'Content-Type': 'application/json', 'X-SK-Device': SK.deviceId() };
  const s = SK.adminSlug();
  if (s) h['X-Komore-Store'] = s;
  return h;
};
SK.post = async (url, body) => { const r = await fetch(url, { method: 'POST', headers: SK.hdr(), body: JSON.stringify(body) }); const j = await r.json(); if (!r.ok) { const e = new Error(j.error || r.status); e.code = j.code; e.status = r.status; throw e; } return j; };
SK.get = async url => { const r = await fetch(url, { headers: SK.hdr() }); const j = await r.json(); if (!r.ok) { const e = new Error(j.error || r.status); e.status = r.status; throw e; } return j; };
// ログイン／登録コード登録の共通フォーム（店舗ID＋パスワード。新端末は登録コード）
SK.authBox = (el, { slug = '', onDone, lockSlug = false }) => {
  const sl = slug || '';
  const ro = lockSlug && sl ? ' readonly' : '';
  el.innerHTML = `<div><b>ログイン</b>（店舗ID ＋ アカウントのパスワード）<br>
    店舗ID <input id="a_slug" value="${sl}" size="12"${ro}> パスワード <input id="a_pw" type="password" size="14"> <button id="a_login">ログイン</button></div>
    <details style="margin-top:8px"><summary>新しい端末・アカウントを登録（オーナーから受け取った登録コード）</summary>
    店舗ID <input id="r_slug" value="${sl}" size="12"${ro}> 登録コード <input id="r_code" size="12" placeholder="XXXXXXXXXX"><br>
    アカウント名 <input id="r_name" size="10" placeholder="例: 山田"> 新しいパスワード(8文字以上) <input id="r_pw" type="password" size="14"> <button id="r_go">登録</button></details>
    <div id="a_msg" style="color:#b91c1c;font-size:13px;margin-top:6px"></div>`;
  const g = id => el.querySelector('#' + id), msg = t => g('a_msg').textContent = t;
  g('a_login').onclick = async () => { try { onDone(await SK.post('/api/login', { slug: (lockSlug && sl) || g('a_slug').value.trim(), password: g('a_pw').value, device_id: SK.deviceId() })); } catch (e) { msg(e.message); } };
  g('r_go').onclick = async () => { try { onDone(await SK.post('/api/register', { slug: (lockSlug && sl) || g('r_slug').value.trim(), code: g('r_code').value, password: g('r_pw').value, name: g('r_name').value, device_id: SK.deviceId(), device_label: navigator.userAgent.slice(0, 40) })); } catch (e) { msg(e.message); } };
};
