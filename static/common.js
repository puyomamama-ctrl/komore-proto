// 録音（無音で自動停止）と 24kHz PCM ストリーム再生
const SK = {};
SK.ctx = null;
SK.ensureCtx = () => { if (!SK.ctx) SK.ctx = new (window.AudioContext || window.webkitAudioContext)(); if (SK.ctx.state === 'suspended') SK.ctx.resume(); return SK.ctx; };
SK.pickMime = () => ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/ogg'].find(m => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';
SK.record = async ({ maxMs = 15000, silenceMs = 1600, onLevel } = {}) => {
  const ctx = SK.ensureCtx();
  const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
  const mime = SK.pickMime();
  const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : {});
  const chunks = []; rec.ondataavailable = e => e.data.size && chunks.push(e.data);
  const src = ctx.createMediaStreamSource(stream); const an = ctx.createAnalyser(); an.fftSize = 1024; src.connect(an);
  const buf = new Float32Array(an.fftSize);
  let spoke = false, lastLoud = performance.now(), amb = 0, nAmb = 0; const t0 = performance.now();
  SK.stopRec = () => rec.state === 'recording' && rec.stop();
  return new Promise(res => {
    rec.onstop = async () => {
      stream.getTracks().forEach(t => t.stop()); src.disconnect();
      const blob = new Blob(chunks, { type: rec.mimeType || mime || 'audio/webm' });
      const b64 = await new Promise(r => { const fr = new FileReader(); fr.onload = () => r(fr.result.split(',')[1]); fr.readAsDataURL(blob); });
      res({ b64, mime: (blob.type || 'audio/webm').split(';')[0], ms: performance.now() - t0, endedAt: performance.now(), spoke });
    };
    rec.start(200);
    const tick = () => {
      if (rec.state !== 'recording') return;
      an.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v; const rms = Math.sqrt(s / buf.length);
      onLevel && onLevel(rms);
      const now = performance.now();
      if (now - t0 < 280) { amb += rms; nAmb += 1; requestAnimationFrame(tick); return; }  // 最初の一瞬は周囲音として見る
      const thresh = Math.max(0.03, (nAmb ? amb / nAmb : 0) * 2.5 + 0.012);
      if (rms > thresh) { spoke = true; lastLoud = now; }
      if ((spoke && now - lastLoud > silenceMs) || now - t0 > maxMs) return rec.stop();
      requestAnimationFrame(tick);
    };
    tick();
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
SK.hdr = () => ({ 'Content-Type': 'application/json', 'X-SK-Device': SK.deviceId() });
SK.post = async (url, body) => { const r = await fetch(url, { method: 'POST', headers: SK.hdr(), body: JSON.stringify(body) }); const j = await r.json(); if (!r.ok) { const e = new Error(j.error || r.status); e.code = j.code; e.status = r.status; throw e; } return j; };
SK.get = async url => { const r = await fetch(url, { headers: SK.hdr() }); const j = await r.json(); if (!r.ok) { const e = new Error(j.error || r.status); e.status = r.status; throw e; } return j; };
// ログイン／登録コード登録の共通フォーム（店舗ID＋パスワード。新端末は登録コード）
SK.authBox = (el, { slug = '', onDone }) => {
  el.innerHTML = `<div><b>ログイン</b>（店舗ID ＋ アカウントのパスワード）<br>
    店舗ID <input id="a_slug" value="${slug}" size="12"> パスワード <input id="a_pw" type="password" size="14"> <button id="a_login">ログイン</button></div>
    <details style="margin-top:8px"><summary>新しい端末・アカウントを登録（オーナーから受け取った登録コード）</summary>
    店舗ID <input id="r_slug" value="${slug}" size="12"> 登録コード <input id="r_code" size="12" placeholder="XXXXXXXXXX"><br>
    アカウント名 <input id="r_name" size="10" placeholder="例: 山田"> 新しいパスワード(8文字以上) <input id="r_pw" type="password" size="14"> <button id="r_go">登録</button></details>
    <div id="a_msg" style="color:#b91c1c;font-size:13px;margin-top:6px"></div>`;
  const g = id => el.querySelector('#' + id), msg = t => g('a_msg').textContent = t;
  g('a_login').onclick = async () => { try { onDone(await SK.post('/api/login', { slug: g('a_slug').value.trim(), password: g('a_pw').value, device_id: SK.deviceId() })); } catch (e) { msg(e.message); } };
  g('r_go').onclick = async () => { try { onDone(await SK.post('/api/register', { slug: g('r_slug').value.trim(), code: g('r_code').value, password: g('r_pw').value, name: g('r_name').value, device_id: SK.deviceId(), device_label: navigator.userAgent.slice(0, 40) })); } catch (e) { msg(e.message); } };
};
