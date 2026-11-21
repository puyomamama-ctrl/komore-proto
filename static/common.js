// 録音（無音で自動停止）と 24kHz PCM ストリーム再生
const SK = {};
SK.ctx = null;
SK.ensureCtx = () => { if (!SK.ctx) SK.ctx = new (window.AudioContext || window.webkitAudioContext)(); if (SK.ctx.state === 'suspended') SK.ctx.resume(); return SK.ctx; };
SK.pickMime = () => ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
// マイクは一度開いたら使い回す（毎回開き直すと iPhone では話し始めが欠けやすい）
SK.mic = null;
SK.openMic = async () => {
  const ctx = SK.ensureCtx();
  const m = SK.mic;
  if (m && m.stream.getAudioTracks().some(t => t.readyState === 'live')) return m;
  if (m && m.opening) return m.opening;
  const opening = (async () => {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    const src = ctx.createMediaStreamSource(stream); const an = ctx.createAnalyser(); an.fftSize = 1024; src.connect(an);
    const mic = { stream, src, an, buf: new Float32Array(an.fftSize), ring: [], recording: false };
    // 録音していない間の音量を約2秒ぶん覚えておき、背景ノイズの基準にする
    mic.timer = setInterval(() => { if (mic.recording) return; mic.ring.push(SK.rmsOf(mic)); if (mic.ring.length > 40) mic.ring.shift(); }, 50);
    SK.mic = mic; return mic;
  })();
  SK.mic = { stream: new MediaStream(), opening };
  try { return await opening; } catch (e) { SK.mic = null; throw e; }
};
SK.closeMic = () => {
  const m = SK.mic; SK.mic = null;
  if (!m || m.opening) return;
  clearInterval(m.timer); try { m.src.disconnect(); } catch (e) {}
  m.stream.getTracks().forEach(t => t.stop());
};
SK.rmsOf = mic => { mic.an.getFloatTimeDomainData(mic.buf); let s = 0; for (const v of mic.buf) s += v * v; return Math.sqrt(s / mic.buf.length); };
SK.pct = (a, p) => { if (!a.length) return null; const b = [...a].sort((x, y) => x - y); return b[Math.min(b.length - 1, Math.floor(b.length * p))]; };
// 話し声のしきい値。背景ノイズは「静かな側（下位20%/10%）」から測るので、話し始めの声をノイズ扱いしない
SK.threshOf = floor => { const f = floor == null ? 0.008 : floor; return Math.min(Math.max(0.02, f * 2.5 + 0.012), Math.max(0.06, f * 1.6)); };
// 録音（無音で自動停止）。onStart は実際に録音が始まった瞬間に呼ばれる
SK.record = async ({ maxMs = 15000, silenceMs = 1600, onLevel, onStart } = {}) => {
  const mic = await SK.openMic();
  const mime = SK.pickMime();
  const rec = new MediaRecorder(mic.stream, mime ? { mimeType: mime } : {});
  const chunks = []; rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  const preFloor = mic.ring.length >= 5 ? SK.pct(mic.ring, 0.2) : null;
  const levels = [];
  let spoke = false, maxRms = 0, lastLoud = 0, t0 = 0;
  SK.stopRec = () => rec.state === 'recording' && rec.stop();
  return new Promise((res, rej) => {
    rec.onerror = e => { mic.recording = false; rej(e.error || new Error('録音エラー')); };
    rec.onstop = async () => {
      mic.recording = false;
      const blob = new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' });
      const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(',')[1]); fr.readAsDataURL(blob); });
      const ms = performance.now() - t0;
      const floor = preFloor ?? SK.pct(levels, 0.1);
      // 判定に落ちても、0.5秒以上あって背景より明らかに大きい音があれば認識に送る
      const voiced = spoke || (ms > 500 && maxRms > Math.max(0.012, (floor || 0) * 1.8));
      res({ b64, mime: (blob.type || 'audio/webm').split(';')[0], ms, endedAt: performance.now(), spoke, voiced });
    };
    let started = false;
    const begin = () => {
      if (started) return; started = true;
      mic.recording = true; t0 = performance.now(); lastLoud = t0;
      onStart && onStart();
      const tick = () => {
        if (rec.state !== 'recording') return;
        const rms = SK.rmsOf(mic); levels.push(rms); if (rms > maxRms) maxRms = rms;
        onLevel && onLevel(rms);
        const now = performance.now();
        let floor = preFloor;
        if (levels.length >= 15) { const p = SK.pct(levels, 0.1); floor = floor == null ? p : Math.min(floor, p); }
        if (rms > SK.threshOf(floor)) { spoke = true; lastLoud = now; }
        if ((spoke && now - lastLoud > silenceMs) || now - t0 > maxMs) return rec.stop();
        requestAnimationFrame(tick);
      };
      tick();
    };
    rec.onstart = begin;
    rec.start(200);
    setTimeout(() => { if (rec.state === 'recording') begin(); }, 600);  // onstart が来ないブラウザ向けの保険
  });
};
SK.sources = [];
SK.stopAudio = () => { SK.sources.forEach(s => { try { s.stop(); } catch (e) {} }); SK.sources = []; SK.abort && SK.abort.abort(); };
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
    const s = ctx.createBufferSource(); s.buffer = ab; s.connect(ctx.destination); t = Math.max(t, ctx.currentTime); s.start(t); t += ab.duration; SK.sources.push(s);
    if (first) { first = false; onFirst && onFirst(performance.now()); }
  }
  return new Promise(res => setTimeout(res, Math.max(0, (t - ctx.currentTime) * 1000)));
};
SK.speakLocal = (text, lang) => new Promise(res => { const u = new SpeechSynthesisUtterance(text); u.lang = lang === 'en' ? 'en-US' : 'ja-JP'; u.onend = res; u.onerror = res; speechSynthesis.cancel(); speechSynthesis.speak(u); });
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
