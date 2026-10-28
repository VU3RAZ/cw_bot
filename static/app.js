'use strict';
const $ = (id) => document.getElementById(id);
const state = {};
let dec = {};
let ws;

// ------------------------------------------------------------ websocket
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onopen = () => {
    $('conn').classList.add('ok');
    if (player.ctx) send({ cmd: 'listen', on: true });
    // the server forgets the station after a restart (grid = distances); never send dry_run from here
    const c = robotCfg();
    if (c.mycall) send({ cmd: 'robot_config', cfg: { mycall: c.mycall, name: c.name, qth: c.qth, grid: c.grid } });
    requestSpots();
  };
  ws.onclose = () => { $('conn').classList.remove('ok'); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return onBinary(ev.data);
    const m = JSON.parse(ev.data);
    if (m.type === 'radios') renderRadios(m);
    else if (m.type === 'state') { Object.assign(state, m.state); renderState(m.state); if (m.state.freq) spotsBandCheck(); }
    else if (m.type === 'decoder') { dec = m.status; renderDecoder(); }
    else if (m.type === 'cw') { appendText('cwOut', m.text); $('cwInfo').textContent = `${dec.cw_freq.toFixed(0)} Hz · ${m.wpm} WPM`; }
    else if (m.type === 'cw_over') renderOver(m.over);
    else if (m.type === 'cw2') { appendText('cw2Out', m.text); $('cw2Info').textContent = `${m.wpm} WPM`; }
    else if (m.type === 'rtty') appendText('rttyOut', m.text);
    else if (m.type === 'ft8') onFt8(m);
    else if (m.type === 'robot') renderRobot(m.status);
    else if (m.type === 'robot_event') robotEvent(m);
    else if (m.type === 'spots') renderSpots(m);
    else if (m.type === 'pathcheck') renderPathCheck(m.result);
    else if (m.type === 'beacons') renderBeacons(m.data);
    else if (m.type === 'error' || m.type === 'notice') toast(m.msg);
    else if (m.type === 'clock') renderClock(m);
    else if (m.type === 'kiwi_list') renderKiwiList(m);
    else if (m.type === 'skimmer') renderSkimmer(m);
  };
}
function send(obj) {
  if (BEACON_LOCKED.has(obj.cmd) && radioLocked()) return;
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
}
// While the NCDXF beacon monitor runs it owns the radio: no tuning, mode changes or transmit from here.
const BEACON_LOCKED = new Set(['freq', 'mode', 'ptt', 'tune', 'send_cw']);
function radioLocked() {
  if (!(bn && bn.active)) return false;
  toast('NCDXF beacon monitor is running - stop it to tune, change mode or transmit');
  return true;
}
function toast(msg) { const t = $('toast'); t.textContent = msg; t.style.display = 'block'; setTimeout(() => t.style.display = 'none', 3000); }

function onBinary(buf) {
  const u8 = new Uint8Array(buf);
  if (u8[0] === 1) {
    const dv = new DataView(buf);
    rf.lo = dv.getInt32(1, true); rf.hi = dv.getInt32(5, true);
    rf.push(u8.subarray(9));
  } else if (u8[0] === 2) {
    af.push(u8.subarray(1));
  } else if (u8[0] === 3) {
    player.play(buf);
  }
}

// ------------------------------------------------------------ colour map
const PALETTE = (() => {
  const stops = [[0, 0, 0, 8], [0.18, 10, 20, 90], [0.38, 20, 110, 200], [0.55, 40, 200, 200],
                 [0.7, 230, 220, 40], [0.85, 240, 100, 20], [1, 255, 255, 255]];
  const p = new Uint8Array(256 * 3);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0; while (t > stops[k + 1][0]) k++;
    const [t0, ...a] = stops[k], [t1, ...b] = stops[k + 1];
    const f = (t - t0) / (t1 - t0);
    for (let c = 0; c < 3; c++) p[i * 3 + c] = a[c] + (b[c] - a[c]) * f;
  }
  return p;
})();

// Scrolling waterfall drawn into a canvas whose width = number of bins.
class Waterfall {
  constructor(canvas, rows) { this.c = canvas; this.ctx = canvas.getContext('2d'); this.rows = rows; this.w = 0; }
  push(vals, map) {
    const n = vals.length;
    if (n !== this.w) { this.w = n; this.c.width = n; this.c.height = this.rows; this.row = this.ctx.createImageData(n, 1); }
    this.ctx.drawImage(this.c, 0, 0, n, this.rows - 1, 0, 1, n, this.rows - 1);
    const d = this.row.data;
    for (let i = 0; i < n; i++) {
      const v = Math.max(0, Math.min(255, map(vals[i]) | 0)) * 3;
      d[i * 4] = PALETTE[v]; d[i * 4 + 1] = PALETTE[v + 1]; d[i * 4 + 2] = PALETTE[v + 2]; d[i * 4 + 3] = 255;
    }
    this.ctx.putImageData(this.row, 0, 0);
  }
}

// ------------------------------------------------------------ RF panadapter (radio scope)
const rf = {
  lo: 0, hi: 0, avg: null, wf: new Waterfall($('rfWf'), 140),
  push(px) {
    const n = px.length;
    if (!this.avg || this.avg.length !== n) this.avg = Float32Array.from(px);
    for (let i = 0; i < n; i++) this.avg[i] = this.avg[i] * 0.5 + px[i] * 0.5;
    const k = +$('rfContrast').value, fl = +$('rfFloor').value;
    this.wf.push(px, (v) => (v - fl) * k * 1.6);
    this.drawSpec();
    drawAxis($('rfAxis'), this.lo, this.hi, (f) => (f / 1e6).toFixed(3), 6);
  },
  drawSpec() {
    const c = $('rfSpec'), dpr = devicePixelRatio || 1;
    const W = c.clientWidth * dpr, H = c.clientHeight * dpr;
    if (c.width !== W) { c.width = W; c.height = H; }
    const g = c.getContext('2d');
    g.clearRect(0, 0, W, H);
    // grid
    g.strokeStyle = '#1b222b'; g.lineWidth = 1;
    for (let i = 1; i < 8; i++) { const y = H * i / 8; g.beginPath(); g.moveTo(0, y); g.lineTo(W, y); g.stroke(); }
    // passband / tuned freq
    if (state.freq && this.hi > this.lo) {
      const x = (state.freq - this.lo) / (this.hi - this.lo) * W;
      const bw = { CW: 500, 'CW-R': 500, RTTY: 500, 'RTTY-R': 500, AM: 6000, FM: 12000 }[state.mode] || 2700;
      let x0, x1;
      if (state.mode === 'LSB') { x0 = x - bw / (this.hi - this.lo) * W; x1 = x; }
      else if (state.mode === 'USB') { x0 = x + 200 / (this.hi - this.lo) * W; x1 = x + 3000 / (this.hi - this.lo) * W; }
      else { x0 = x - bw / 2 / (this.hi - this.lo) * W; x1 = x + bw / 2 / (this.hi - this.lo) * W; }
      g.fillStyle = 'rgba(245,166,35,.12)'; g.fillRect(x0, 0, x1 - x0, H);
      g.strokeStyle = '#f5a623'; g.beginPath(); g.moveTo(x, 0); g.lineTo(x, H); g.stroke();
    }
    const a = this.avg, n = a.length;
    g.beginPath(); g.moveTo(0, H);
    const k = +$('rfContrast').value, fl = +$('rfFloor').value;
    for (let i = 0; i < n; i++) g.lineTo(i / (n - 1) * W, H - Math.max(0, Math.min(1, (a[i] - fl) * k / 100)) * H);
    g.lineTo(W, H); g.closePath();
    const grad = g.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0, 'rgba(80,200,255,.45)'); grad.addColorStop(1, 'rgba(80,200,255,.02)');
    g.fillStyle = grad; g.fill();
    g.strokeStyle = '#7fd8ff'; g.lineWidth = dpr; g.stroke();
  },
};

function drawAxis(el, lo, hi, fmt, nTicks) {
  const key = `${lo}:${hi}:${el.clientWidth}`;
  if (el._key === key) return; el._key = key;
  el.innerHTML = '';
  if (!(hi > lo)) return;
  const span = hi - lo, raw = span / nTicks, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  for (let f = Math.ceil(lo / step) * step; f <= hi; f += step) {
    const s = document.createElement('span');
    s.style.left = ((f - lo) / span * 100) + '%'; s.textContent = fmt(f); el.appendChild(s);
  }
}

function pxToFreq(wrap, ev, lo, hi) {
  const r = wrap.getBoundingClientRect();
  return lo + (ev.clientX - r.left) / r.width * (hi - lo);
}
const step = () => +$('step').value;
const roundTo = (f, s) => Math.round(f / s) * s;

$('rfWrap').addEventListener('click', (ev) => {
  if (!(rf.hi > rf.lo)) return;
  const s = Math.min(step(), 100);
  setFreq(roundTo(pxToFreq($('rfWrap'), ev, rf.lo, rf.hi), s));
});
$('rfWrap').addEventListener('wheel', (ev) => {
  ev.preventDefault();
  if (!state.freq) return;
  setFreq(roundTo(state.freq, step()) + (ev.deltaY < 0 ? 1 : -1) * step());
}, { passive: false });
hoverCursor($('rfWrap'), $('rfCursor'), () => [rf.lo, rf.hi], (f) => (f / 1e3).toFixed(2) + ' kHz');

function hoverCursor(wrap, cur, range, fmt) {
  wrap.addEventListener('mousemove', (ev) => {
    const [lo, hi] = range(); if (!(hi > lo)) return;
    const r = wrap.getBoundingClientRect();
    cur.style.display = 'block'; cur.style.left = (ev.clientX - r.left) + 'px';
    cur.dataset.f = fmt(pxToFreq(wrap, ev, lo, hi));
  });
  wrap.addEventListener('mouseleave', () => cur.style.display = 'none');
}

// ------------------------------------------------------------ audio waterfall
const AF_MAX = 4000;
const af = {
  wf: new Waterfall($('afWf'), 150), floor: null,
  push(bins) {
    // auto floor: running 30th percentile of the 100-3000 Hz part of the row, leaving out digital
    // silence (a KiwiSDR's audio outside a narrow filter), which would put the floor at zero
    const hzPerBin = AF_MAX / bins.length;
    const all = Array.from(bins.subarray(100 / hzPerBin | 0, 3000 / hzPerBin | 0));
    const live = all.filter((v) => v > 5);
    const sorted = (live.length >= all.length / 10 ? live : all).sort((a, b) => a - b);
    const p = sorted[Math.floor(sorted.length * 0.3)];
    this.floor = this.floor == null ? p : this.floor * 0.9 + p * 0.1;
    const k = +$('afContrast').value, fl = this.floor;
    this.wf.push(bins, (v) => (v - fl) * k * 2.2 + 25);
  },
};
drawAxis($('afAxis'), 0, AF_MAX, (f) => f, 8);
window.addEventListener('resize', () => { $('afAxis')._key = null; drawAxis($('afAxis'), 0, AF_MAX, (f) => f, 8); });
hoverCursor($('afWrap'), $('afCursor'), () => [0, AF_MAX], (f) => f.toFixed(0) + ' Hz');
$('afWrap').addEventListener('click', (ev) => {
  const f = pxToFreq($('afWrap'), ev, 0, AF_MAX);
  if ($('clickTarget').value === 'cw') send({ cmd: 'decoder', cw_freq: f, snap: $('snap').checked, enable: { cw: true } });
  else send({ cmd: 'decoder', rtty_freq: Math.round(f), enable: { rtty: true } });
});

function placeMarker(el, hz) {
  el.style.display = hz > 0 && hz < AF_MAX ? 'block' : 'none';
  el.style.left = (hz / AF_MAX * 100) + '%';
}

// ------------------------------------------------------------ frequency display
function renderFreq() {
  const f = state.freq || 0;
  const s = String(f).padStart(9, ' ');
  // digits: 10M 1M . 100k 10k 1k . 100 10 1
  let html = '';
  for (let i = 0; i < 9; i++) {
    const mult = 10 ** (8 - i);
    const ch = s[i] === ' ' ? '&nbsp;' : s[i];
    html += `<span class="d" data-m="${mult}">${ch}</span>`;
    if (i === 2 || i === 5) html += '<span class="sep">.</span>';
  }
  $('freq').innerHTML = html;
}
$('freq').addEventListener('wheel', (ev) => {
  ev.preventDefault();
  const m = +(ev.target.dataset.m || 0); if (!m || !state.freq) return;
  setFreq(state.freq + (ev.deltaY < 0 ? m : -m));
}, { passive: false });
$('freq').addEventListener('click', () => {
  const inp = $('freqInput');
  inp.value = ((state.freq || 0) / 1e3).toFixed(3); inp.hidden = false; $('freq').hidden = true; inp.focus(); inp.select();
});
$('freqInput').addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter') {
    const v = parseFloat(ev.target.value);
    // accept kHz (14074) or MHz (14.074)
    if (v > 0) setFreq(Math.round(v < 100 ? v * 1e6 : v * 1e3));
  }
  if (ev.key === 'Enter' || ev.key === 'Escape') { ev.target.hidden = true; $('freq').hidden = false; }
});
$('freqInput').addEventListener('blur', (ev) => { ev.target.hidden = true; $('freq').hidden = false; });

function setFreq(hz) {
  if (radioLocked()) return;
  hz = Math.round(hz);
  state.freq = hz; renderFreq(); rf.drawSpec();
  send({ cmd: 'freq', hz });
  saveBandFreq(hz);
}

document.addEventListener('keydown', (ev) => {
  if (ev.target.tagName === 'INPUT' || ev.target.tagName === 'SELECT' || !state.freq) return;
  if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') { setFreq(roundTo(state.freq, step()) + step()); ev.preventDefault(); }
  if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') { setFreq(roundTo(state.freq, step()) - step()); ev.preventDefault(); }
});

// ------------------------------------------------------------ bands / modes
const BANDS = [
  // name, lo, hi, cw, ssb, ft8, ft4
  ['160', 1.8e6, 2.0e6, 1.830e6, 1.850e6, 1.840e6, null],
  ['80', 3.5e6, 4.0e6, 3.520e6, 3.700e6, 3.573e6, 3.575e6],
  ['60', 5.3e6, 5.41e6, 5.354e6, 5.360e6, 5.357e6, null],
  ['40', 7.0e6, 7.3e6, 7.020e6, 7.150e6, 7.074e6, 7.0475e6],
  ['30', 10.1e6, 10.15e6, 10.115e6, 10.130e6, 10.136e6, 10.140e6],
  ['20', 14.0e6, 14.35e6, 14.025e6, 14.200e6, 14.074e6, 14.080e6],
  ['17', 18.068e6, 18.168e6, 18.080e6, 18.130e6, 18.100e6, 18.104e6],
  ['15', 21.0e6, 21.45e6, 21.025e6, 21.250e6, 21.074e6, 21.140e6],
  ['12', 24.89e6, 24.99e6, 24.900e6, 24.950e6, 24.915e6, 24.919e6],
  ['10', 28.0e6, 29.7e6, 28.025e6, 28.400e6, 28.074e6, 28.180e6],
  ['6', 50.0e6, 54.0e6, 50.090e6, 50.150e6, 50.313e6, 50.318e6],
];
const bandOf = (f) => BANDS.find((b) => f >= b[1] && f <= b[2]);
function lsGet(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } }
function lsSet(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* ignore */ } }
function saveBandFreq(hz) { const b = bandOf(hz); if (b) { const m = lsGet('bandFreq', {}); m[b[0]] = hz; lsSet('bandFreq', m); } }

for (const b of BANDS) {
  const btn = document.createElement('button');
  btn.textContent = b[0] + 'm'; btn.dataset.band = b[0];
  btn.onclick = () => {
    const mem = lsGet('bandFreq', {});
    const cwish = /CW|RTTY/.test(state.mode || '');
    setFreq(mem[b[0]] || (cwish ? b[3] : b[4]));
    if (!mem[b[0]] && !cwish) send({ cmd: 'mode', mode: b[4] < 10e6 ? 'LSB' : 'USB', data: false });
  };
  $('bands').appendChild(btn);
}
const MODES = ['LSB', 'USB', 'CW', 'CW-R', 'RTTY', 'RTTY-R', 'AM', 'FM'];
for (const m of MODES) {
  const btn = document.createElement('button');
  btn.textContent = m; btn.dataset.mode = m;
  btn.onclick = () => send({ cmd: 'mode', mode: m, data: false });
  $('modes').appendChild(btn);
}
{
  const btn = document.createElement('button');
  btn.textContent = 'DATA'; btn.id = 'dataBtn';
  btn.onclick = () => send({ cmd: 'mode', mode: state.mode, data: !state.data });
  $('modes').appendChild(btn);
}
for (const f of [1, 2, 3]) {
  const btn = document.createElement('button');
  btn.textContent = 'FIL' + f; btn.dataset.fil = f;      // relabelled per radio in renderRadios
  btn.onclick = () => send({ cmd: 'mode', mode: state.mode, filter: f, data: state.data });
  $('filters').appendChild(btn);
}

// ------------------------------------------------------------ receive / transmit controls
$('preamp').onclick = () => send({ cmd: 'preamp', value: ((state.preamp || 0) + 1) % 3 });
$('att').onclick = () => send({ cmd: 'att', value: !state.att });
$('nb').onclick = () => send({ cmd: 'nb', value: !state.nb });
$('nr').onclick = () => send({ cmd: 'nr', value: !state.nr });
$('agc').onclick = () => send({ cmd: 'agc', value: { FAST: 'MID', MID: 'SLOW', SLOW: 'FAST' }[state.agc] || 'MID' });

const LEVELS = {
  af: (v) => Math.round(v / 2.55) + '%', rf: (v) => Math.round(v / 2.55) + '%',
  power: (v) => Math.round(v / 2.55) + '%', keyspeed: (v) => Math.round(6 + v * 42 / 255) + 'wpm',
};
for (const [name, fmt] of Object.entries(LEVELS)) {
  const el = $(name);
  let t = 0;
  el.addEventListener('input', () => {
    $(name + 'V').textContent = fmt(+el.value);
    el._busy = Date.now();
    clearTimeout(t); t = setTimeout(() => send({ cmd: 'level', name, value: +el.value }), 60);
  });
}

$('ptt').onclick = () => send({ cmd: 'ptt', value: !state.tx });
$('tune').onclick = () => send({ cmd: 'tune' });
$('cwSend').onclick = () => { const t = $('cwText').value.trim(); if (t) { send({ cmd: 'send_cw', text: t }); $('cwText').value = ''; } };
$('cwText').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('cwSend').click(); });
$('cwStop').onclick = () => send({ cmd: 'stop_cw' });
{
  const call = () => ($('mycall').value || 'MYCALL').toUpperCase();
  const macros = [['CQ', () => `CQ CQ CQ DE ${call()} ${call()} K`], ['599', () => 'TU 5NN 5NN BK'],
                  ['QRZ?', () => 'QRZ?'], ['73', () => `73 TU DE ${call()} SK`], ['AGN', () => 'AGN?']];
  for (const [label, fn] of macros) {
    const b = document.createElement('button'); b.textContent = label;
    b.onclick = () => { $('cwText').value = fn(); $('cwText').focus(); };
    $('macros').appendChild(b);
  }
}

$('span').onchange = (ev) => send({ cmd: 'scope_span', value: +ev.target.value });
$('scopeMode').onchange = (ev) => send({ cmd: 'scope_mode', value: ev.target.value });
$('scopeSpeed').onchange = (ev) => send({ cmd: 'scope_speed', value: ev.target.value });

// ------------------------------------------------------------ render radio state
function sMeterText(v) {
  if (v <= 120) return 'S' + Math.round(v / 120 * 9);
  return 'S9+' + Math.round((v - 120) / 121 * 60);
}
// ------------------------------------------------------------ PC clock (NTP)
function renderClock(m) {
  const c = m.clock, el = $('clockBadge');
  el.textContent = c.ok ? `NTP ✓${c.offset_ms != null ? ' ' + Math.abs(c.offset_ms).toFixed(1) + ' ms' : ''}` : 'NTP ✗';
  el.className = 'clock-badge ' + (c.ok ? 'ok' : 'bad');
  el.title = m.text + `\n(checked ${c.checked_utc}; beacon monitor and FT8 need an NTP-synced clock)`;
}

// ------------------------------------------------------------ radio selection
let caps = { name: 'IC-7300', scope: true, filters: { 1: 'FIL1', 2: 'FIL2', 3: 'FIL3' }, preamp: ['P.AMP OFF', 'P.AMP 1', 'P.AMP 2'],
             att: 'ATT', notes: [] };
let radiosInfo = null;
function renderRadios(m) {
  radiosInfo = m; caps = m.caps;
  $('radioSel').innerHTML = m.radios.map((r) => `<option value="${r.name}">${r.name}`
    + `${r.network || r.connected ? '' : ' (not detected)'}</option>`).join('');
  $('radioSel').value = m.current;
  document.title = `${caps.name} Web`;
  document.querySelectorAll('#filters button').forEach((b) => { b.textContent = caps.filters[b.dataset.fil]; b.title = caps.filter_help || ''; });
  $('att').title = caps.att;
  $('rfNote').hidden = caps.scope;
  $('rfNote').textContent = caps.notes.join(' ');
  $('rfWrap').classList.toggle('no-scope', !caps.scope);
  document.body.classList.toggle('rx-only', !!caps.rx_only);
  document.body.classList.toggle('skim-ok', !!caps.skimmer);
  renderState({ mode: 1, preamp: 1 });
  if (m.kiwi && !$('kiwiRegion').options.length) {
    $('kiwiRegion').innerHTML = m.kiwi.regions.map((r) => `<option value="${r}">${r}</option>`).join('');
    $('kiwiRegion').value = m.kiwi.region;
  }
  renderKiwiBox();
  if (m.current === 'KiwiSDR' && !(m.kiwi || {}).receiver) openKiwiPanel();
}

// ------------------------------------------------------------ KiwiSDR receivers
let kiwiList = null;      // last kiwi_list reply
function renderKiwiBox() {
  const on = !!radiosInfo && radiosInfo.current === 'KiwiSDR';
  $('kiwiBox').hidden = !on;
  if (!on) { $('kiwiPanel').hidden = true; return; }
  const rx = state.receiver;
  $('kiwiBtn').textContent = (rx ? (rx.name || rx.url).split('|')[0].trim().slice(0, 40) : 'Choose receiver') + ' ▾';
  $('kiwiBtn').title = rx ? `${rx.name}\n${rx.url}\n${rx.antenna || ''} - click to choose another` : 'Public KiwiSDR receivers: click to choose';
  $('kiwiStatus').textContent = state.kiwi_status || '';
}
function openKiwiPanel(refresh) {
  $('kiwiPanel').hidden = false;
  if (refresh || !kiwiList || kiwiList.region !== $('kiwiRegion').value || (!kiwiList.receivers && !kiwiList.loading))
    send({ cmd: 'kiwi_list', region: $('kiwiRegion').value, refresh: !!refresh });
  else renderKiwiList(kiwiList);
}
function renderKiwiList(m) {
  if (m.region !== $('kiwiRegion').value) return;
  kiwiList = m;
  if (m.loading) { $('kiwiInfo').textContent = `asking every ${m.region} receiver for its status (up to a minute)…`; return; }
  $('kiwiInfo').textContent = `${m.receivers.length} usable · checked ${fmtAge(m.age)} ago`;
  const body = $('kiwiBody'); body.textContent = '';
  if (!m.receivers.length) {
    const td = body.insertRow().insertCell();
    td.colSpan = 7; td.className = 'hint'; td.textContent = 'No usable receiver here right now (80-20 m, online, antenna connected). Try another region.';
  }
  const cur = (state.receiver || {}).url;
  for (const r of m.receivers) {
    const tr = document.createElement('tr');
    if (r.url === m.recommended) tr.className = 'rec';
    if (r.url === cur) tr.classList.add('cur');
    if (r.refused) tr.classList.add('refused');
    tr.title = `${r.loc}\n${r.url}\n` + (r.refused ? `turned us away lately: ${r.refused}` : 'click to listen');
    const mark = (r.url === cur ? '● ' : '') + (r.url === m.recommended ? '★' : '');
    for (const v of [mark, r.name || r.loc, r.antenna, `${r.snr} dB`, r.free, r.time_limited ? 'yes' : 'none', r.km ?? '']) {
      const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
    }
    tr.onclick = () => { send({ cmd: 'kiwi_select', url: r.url }); $('kiwiPanel').hidden = true; toast(`connecting to ${r.name || r.url}`); };
    body.appendChild(tr);
  }
}
$('kiwiBtn').onclick = () => ($('kiwiPanel').hidden ? openKiwiPanel() : ($('kiwiPanel').hidden = true));
$('kiwiClose').onclick = () => { $('kiwiPanel').hidden = true; };
$('kiwiRefresh').onclick = () => openKiwiPanel(true);
$('kiwiRegion').onchange = () => openKiwiPanel();
$('radioSel').onchange = () => {
  const name = $('radioSel').value;
  if (radiosInfo) $('radioSel').value = radiosInfo.current;   // shown again once the server has switched
  send({ cmd: 'select_radio', name });
};

function renderState(ch) {
  if ('receiver' in ch || 'kiwi_status' in ch) renderKiwiBox();
  if ('freq' in ch) {
    renderFreq();
    const b = bandOf(state.freq);
    document.querySelectorAll('#bands button').forEach((x) => x.classList.toggle('on', !!b && x.dataset.band === b[0]));
  }
  if ('connected' in ch) $('conn').title = state.connected ? 'Radio connected' : 'Radio not connected';
  if ('mode' in ch || 'data' in ch || 'filter' in ch) {
    $('modeBadge').textContent = (state.mode || '—') + (state.data ? '-D' : '') + '  '
      + (caps.filters[state.filter] || (state.if_width ? '' : 'FIL?')) + (state.if_width ? ` ${state.if_width} Hz` : '');
    document.querySelectorAll('#modes button[data-mode]').forEach((x) => x.classList.toggle('on', x.dataset.mode === state.mode));
    $('dataBtn').classList.toggle('on', !!state.data);
    document.querySelectorAll('#filters button').forEach((x) => x.classList.toggle('on', +x.dataset.fil === state.filter));
    renderDecoder();
    applyModeFamily();
  }
  for (const name of Object.keys(LEVELS)) {
    if (name in ch) {
      const el = $(name);
      if (!el._busy || Date.now() - el._busy > 1500) el.value = state[name];
      $(name + 'V').textContent = LEVELS[name](state[name]);
    }
  }
  if ('preamp' in ch) { $('preamp').textContent = caps.preamp[state.preamp] || 'P.AMP'; $('preamp').classList.toggle('on', state.preamp > 0); }
  if ('att' in ch) $('att').classList.toggle('on', state.att);
  if ('nb' in ch) $('nb').classList.toggle('on', state.nb);
  if ('nr' in ch) $('nr').classList.toggle('on', state.nr);
  if ('agc' in ch) $('agc').textContent = 'AGC ' + (state.agc || '');
  if ('tx' in ch) {
    $('txBadge').textContent = state.tx ? 'TX' : 'RX';
    $('txBadge').classList.toggle('tx', state.tx); $('ptt').classList.toggle('on', state.tx);
  }
  if ('smeter' in ch || 'po' in ch || 'tx' in ch || 'swr' in ch) {
    if (state.tx) {
      const swr = state.swr <= 48 ? 1 + state.swr / 96 : state.swr <= 80 ? 1.5 + (state.swr - 48) / 64 : 2 + (state.swr - 80) / 40;
      $('meterLabel').textContent = `Po ${Math.round(state.po / 2.55)}%  SWR ${swr.toFixed(1)}`;
      $('meterFill').style.width = (state.po / 255 * 100) + '%';
    } else {
      $('meterLabel').textContent = sMeterText(state.smeter);
      $('meterFill').style.width = (Math.min(241, state.smeter) / 241 * 100) + '%';
    }
  }
  if ('scope_span' in ch && state.scope_span) $('span').value = String(state.scope_span);
  if ('scope_mode' in ch) $('scopeMode').value = state.scope_mode;
  if ('scope_speed' in ch && state.scope_speed) $('scopeSpeed').value = state.scope_speed;
}

// ------------------------------------------------------------ decoders
function renderDecoder() {
  if (!dec.enabled) return;
  $('enCw').checked = dec.enabled.cw; $('enRtty').checked = dec.enabled.rtty; $('enFt8').checked = dec.enabled.ft8;
  $('ft4').checked = dec.ft4; $('ft8Name').textContent = dec.ft4 ? 'FT4' : 'FT8';
  $('cwSql').value = dec.cw_squelch;
  $('cwAfc').checked = dec.cw_afc;
  $('cwEngine').value = dec.cw_engine;
  $('cw2Box').hidden = dec.cw_engine !== 'both';
  if (state.mode && dec.rtty_mark_high !== (defaultMarkHigh() !== $('rttyRev').checked))
    send({ cmd: 'decoder', rtty_mark_high: defaultMarkHigh() !== $('rttyRev').checked });
  $('cwInfo').textContent = `${dec.cw_freq.toFixed(0)} Hz · ${dec.cw_wpm} WPM` + (dec.cw_warning ? ` · ⚠ ${dec.cw_warning}` : '');
  $('rttyInfo').textContent = `${dec.rtty_freq.toFixed(0)} Hz · ${dec.rtty_shift}/${dec.rtty_baud} · mark ${dec.rtty_mark_high ? 'high' : 'low'}`;
  $('audioStatus').textContent = dec.audio ? '' : '⚠ no audio from radio';
  placeMarker($('mkCw'), dec.enabled.cw ? dec.cw_freq : -1);
  const sgn = dec.rtty_mark_high ? 1 : -1;
  placeMarker($('mkMark'), dec.enabled.rtty ? dec.rtty_freq + sgn * dec.rtty_shift / 2 : -1);
  placeMarker($('mkSpace'), dec.enabled.rtty ? dec.rtty_freq - sgn * dec.rtty_shift / 2 : -1);
}
// In LSB-based modes (LSB, RTTY) mark is the lower audio tone; in USB/RTTY-R it is higher.
const defaultMarkHigh = () => !['LSB', 'RTTY'].includes(state.mode);

$('enCw').onchange = (ev) => send({ cmd: 'decoder', enable: { cw: ev.target.checked } });
$('cwSql').onchange = (ev) => send({ cmd: 'decoder', cw_squelch: +ev.target.value });
$('cwAfc').onchange = (ev) => send({ cmd: 'decoder', cw_afc: ev.target.checked });
$('cwEngine').onchange = (ev) => send({ cmd: 'decoder', cw_engine: ev.target.value });
$('enRtty').onchange = (ev) => send({ cmd: 'decoder', enable: { rtty: ev.target.checked }, rtty_mark_high: defaultMarkHigh() !== $('rttyRev').checked });
$('rttyRev').checked = lsGet('rttyRev', false);
$('rttyRev').onchange = () => { lsSet('rttyRev', $('rttyRev').checked); send({ cmd: 'decoder', rtty_mark_high: defaultMarkHigh() !== $('rttyRev').checked }); };
$('enFt8').onchange = (ev) => send({ cmd: 'decoder', enable: { ft8: ev.target.checked } });
$('ft4').onchange = (ev) => send({ cmd: 'decoder', ft4: ev.target.checked });
$('ft8Go').onclick = () => {
  const b = bandOf(state.freq) || BANDS[5];
  const f = $('ft4').checked ? (b[6] || b[5]) : b[5];
  send({ cmd: 'mode', mode: 'USB', data: true });
  setFreq(f);
  send({ cmd: 'decoder', enable: { ft8: true } });
};
document.querySelectorAll('[data-clear]').forEach((b) => b.onclick = () => { $(b.dataset.clear).textContent = ''; });
$('mycall').value = lsGet('mycall', '');
$('mycall').addEventListener('change', (ev) => lsSet('mycall', ev.target.value.toUpperCase()));

function appendText(id, text) {
  const el = $(id);
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  el.textContent += text;
  if (el.textContent.length > 20000) el.textContent = el.textContent.slice(-15000);
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ---- CW tile: Raw (every character) / Parsed (over by over, read by cwparse)
function setCwView(v) {
  lsSet('cwView', v);
  $('cwOut').hidden = v !== 'raw'; $('cwParsed').hidden = v === 'raw';
  $('cwRaw').classList.toggle('on', v === 'raw'); $('cwParsedBtn').classList.toggle('on', v !== 'raw');
}
$('cwRaw').onclick = () => setCwView('raw');
$('cwParsedBtn').onclick = () => setCwView('parsed');
$('cwClear').onclick = () => { $('cwOut').textContent = ''; $('cwParsed').textContent = ''; };
setCwView(lsGet('cwView', 'raw'));

// ---- KiwiSDR skimmer: every CW signal in the I/Q passband, one row each
$('skOn').onchange = (ev) => send({ cmd: 'skimmer', on: ev.target.checked });
$('skCalls').checked = lsGet('skCalls', false);
$('skCalls').onchange = (ev) => { lsSet('skCalls', ev.target.checked); if (renderSkimmer.last) renderSkimmer(renderSkimmer.last); };
function renderSkimmer(m) {
  renderSkimmer.last = m;
  $('skOn').checked = m.on;
  const rows = $('skCalls').checked ? m.rows.filter((r) => r.call) : m.rows;
  $('skInfo').textContent = !m.on ? 'off' : !m.active ? 'on, waiting for CW / CW-R mode'
    : `${rows.length} signals copied · ${m.channels} tracked · ${((m.fc - m.span) / 1e3).toFixed(1)}–${((m.fc + m.span) / 1e3).toFixed(1)} kHz`
      + (m.wide ? ' · wideband receiver' : '');
  const body = $('skBody'); body.textContent = '';
  if (m.on && m.active && !rows.length) {
    const td = body.insertRow().insertCell(); td.colSpan = 6; td.className = 'hint';
    td.textContent = 'Listening… a signal shows once it has been copied.';
  }
  for (const r of rows) {
    const tr = body.insertRow();
    if (Math.abs(r.hz - state.freq) < 40) tr.className = 'cur';
    tr.title = `${(r.hz / 1e3).toFixed(2)} kHz (${r.offset >= 0 ? '+' : ''}${r.offset} Hz from the dial)`
      + (r.spotted ? ' · also spotted on RBN / the cluster' : '') + ' · click to tune there' + (r.call ? ' and set as robot target' : '');
    for (const v of [(r.hz / 1e3).toFixed(2), r.call + (r.spotted ? ' ✓' : ''), r.wpm || '', r.snr, fmtAge(r.age), r.text]) {
      const td = tr.insertCell(); td.textContent = v;
    }
    tr.cells[1].className = 'call';
    tr.onclick = () => {
      setFreq(r.hz);
      if (r.call && !robotInQso) { clearOther(false); $('rbTarget').value = r.call; setSrc('call', 'you'); }
    };
  }
}

function renderOver(o) {
  const box = $('cwParsed');
  let row = box.querySelector(`[data-over="${o.id}"]`);
  if (!o.text) { if (row) row.remove(); return; }
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
  if (!row) {
    row = document.createElement('div'); row.className = 'over'; row.dataset.over = o.id;
    box.appendChild(row);
    while (box.children.length > 200) box.firstChild.remove();
  }
  row.classList.toggle('live', !o.done);
  row.textContent = '';
  const sum = document.createElement('div'); sum.className = 'sum';
  const chip = (txt, cls) => { const s = document.createElement('span'); s.textContent = txt; if (cls) s.className = cls; sum.appendChild(s); };
  chip(new Date().toISOString().slice(11, 16), 'time');
  if (o.cq) chip('CQ', 'tag');
  if (o.call || o.to) chip(o.call && o.to ? `${o.call} → ${o.to}` : o.call || `→ ${o.to}`, 'who');
  if (o.rst) chip(`RST ${o.rst}`);
  if (o.name) chip(`name ${o.name}`);
  if (o.qth) chip(`QTH ${o.qth}`);
  if (o.asks_repeat) chip('asks repeat', 'tag');
  if (o.sent_73) chip('73', 'tag');
  if (!o.done) chip('…', 'time');
  row.appendChild(sum);
  const txt = document.createElement('div'); txt.className = 'txt';
  const calls = new Set(o.calls);
  const from = {};                   // corrected call -> the copies it was repaired from
  for (const [a, b] of Object.entries(o.fixed || {})) (from[b] = from[b] || []).push(a);
  o.text.split(' ').forEach((w, i) => {
    if (i) txt.appendChild(document.createTextNode(' '));
    if (calls.has(w) || from[w]) {
      const b = document.createElement('b'); b.textContent = w;
      if (from[w]) { b.className = 'fixed'; b.title = `corrected from ${from[w].join(', ')} (voted across its copies)`; }
      txt.appendChild(b);
    } else txt.appendChild(document.createTextNode(w));
  });
  row.appendChild(txt);
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function onFt8(m) {
  $('ft8Info').textContent = `slot ${m.slot} · ${m.decodes.length} decodes`;
  const body = $('ft8Body');
  const me = ($('mycall').value || '').toUpperCase().trim();
  const frag = document.createDocumentFragment();
  m.decodes.sort((a, b) => a.freq - b.freq).forEach((d, i) => {
    const tr = document.createElement('tr');
    if (/^CQ\s/.test(d.msg)) tr.className = 'cq';
    if (me && d.msg.split(/\s+/).includes(me)) tr.className = 'me';
    if (i === 0) tr.classList.add('slot');
    for (const v of [d.utc, d.snr.toFixed(0), d.dt.toFixed(1), d.freq.toFixed(0), d.msg]) {
      const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
    }
    frag.appendChild(tr);
  });
  body.insertBefore(frag, body.firstChild);
  while (body.rows.length > 600) body.deleteRow(-1);
  // label decodes on the audio waterfall
  const box = $('ft8marks'); box.innerHTML = '';
  m.decodes.forEach((d, i) => {
    if (d.freq >= AF_MAX) return;
    const el = document.createElement('div');
    el.style.left = (d.freq / AF_MAX * 100) + '%'; el.style.top = (2 + (i % 8) * 14) + 'px';
    el.textContent = d.msg; box.appendChild(el);
  });
}

// ------------------------------------------------------------ audio streaming
// 12 kHz s16 mono chunks are scheduled back to back with a small jitter buffer.
const player = {
  ctx: null, gain: null, next: 0,
  start() {
    this.ctx = new AudioContext({ sampleRate: 12000 });
    this.gain = this.ctx.createGain(); this.gain.gain.value = +$('vol').value;
    this.gain.connect(this.ctx.destination);
    this.next = 0;
    send({ cmd: 'listen', on: true });
  },
  stop() { send({ cmd: 'listen', on: false }); if (this.ctx) this.ctx.close(); this.ctx = null; },
  play(buf) {
    if (!this.ctx) return;
    const pcm = new Int16Array(buf.slice(1));
    const ab = this.ctx.createBuffer(1, pcm.length, 12000), ch = ab.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
    const src = this.ctx.createBufferSource(); src.buffer = ab; src.connect(this.gain);
    const now = this.ctx.currentTime;
    if (this.next < now + 0.05 || this.next > now + 0.8) this.next = now + 0.2;  // (re)buffer
    src.start(this.next); this.next += ab.duration;
  },
};
$('listen').onclick = () => {
  if (player.ctx) player.stop(); else player.start();
  $('listen').classList.toggle('on', !!player.ctx);
  $('listen').textContent = player.ctx ? '🔊 Listening' : '🔈 Listen';
};
$('vol').oninput = () => { if (player.gain) player.gain.gain.value = +$('vol').value; };

// ------------------------------------------------------------ CW robot
const RB_FIELDS = { rbCall: 'mycall', rbName: 'name', rbQth: 'qth', rbGrid: 'grid', rbWpm: 'wpm', rbMaxCq: 'max_cq', rbHuntWho: 'hunt' };
function robotCfg() {
  const cfg = { dry_run: $('rbDry').checked, afc: $('rbAfc').checked };
  for (const [id, k] of Object.entries(RB_FIELDS)) cfg[k] = id === 'rbWpm' || id === 'rbMaxCq' ? +$(id).value : $(id).value.toUpperCase().trim();
  return cfg;
}
for (const id of Object.keys(RB_FIELDS)) {
  $(id).value = lsGet('rb_' + id, $(id).value);
  $(id).addEventListener('change', () => { lsSet('rb_' + id, $(id).value); send({ cmd: 'robot_config', cfg: robotCfg() }); });
}
if (!$('rbCall').value && $('mycall').value) $('rbCall').value = $('mycall').value;
for (const id of ['rbDry', 'rbAfc']) $(id).onchange = () => send({ cmd: 'robot_config', cfg: robotCfg() });

function robotStart(mode) {
  const cfg = robotCfg();
  if (!cfg.mycall) return toast('Enter your callsign first');
  const what = mode === 'pathcheck' ? 'send QRL? and then 3 CQs'
    : mode === 'hunt' ? `listen for up to 30 min and TRANSMIT CW to answer the first CQ it hears from ${cfg.hunt === 'DX' ? 'a DX station (other country)' : 'any station'}` : 'TRANSMIT CW';
  if (!cfg.dry_run && !confirm(`The robot will ${what} as ${cfg.mycall} on ${(state.freq / 1e3).toFixed(1)} kHz.\n\n`
      + 'Stay at the radio as control operator and make sure the frequency is clear. Continue?')) return;
  send({ cmd: 'robot_start', mode, target: $('rbTarget').value.toUpperCase().trim(), cfg, other: otherFields() });
}
$('rbCq').onclick = () => robotStart('cq');
$('rbPath').onclick = () => robotStart('pathcheck');
$('rbAnswer').onclick = () => robotStart('answer');
$('rbHunt').onclick = () => robotStart('hunt');
$('rbStop').onclick = () => send({ cmd: 'robot_stop' });
$('rbInjectBtn').onclick = () => { const t = $('rbInject').value.trim(); if (t) send({ cmd: 'robot_inject', text: t }); $('rbInject').value = ''; };
$('rbInject').addEventListener('keydown', (ev) => { if (ev.key === 'Enter') $('rbInjectBtn').click(); });
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') send({ cmd: 'robot_stop' }); });

// Other station card: decoded values arrive in robot status; operator edits are sent
// as qso_edit during a contact, or used as the starting values when pressing Answer.
const OB = { rbTarget: 'call', obRstSent: 'rst_sent', obRstRcvd: 'rst_rcvd', obName: 'name', obQth: 'qth' };
let robotInQso = false;
function otherFields() {
  const o = {};
  for (const [id, k] of Object.entries(OB)) o[k] = $(id).value.toUpperCase().trim();
  return o;
}
function setSrc(k, text) { const el = $('src_' + k); el.textContent = text; el.className = 'src ' + text; }
function clearOther(keepCall) {
  for (const [id, k] of Object.entries(OB)) {
    if (keepCall && k === 'call') continue;
    $(id).value = ''; setSrc(k, '');
  }
}
for (const [id, k] of Object.entries(OB)) {
  $(id).addEventListener('change', () => {
    const v = $(id).value.toUpperCase().trim();
    $(id).value = v;
    setSrc(k, v ? 'you' : '');
    if (robotInQso) send({ cmd: 'qso_edit', field: k, value: v });
  });
}

// Tiles follow the mode: CW shows the CW decoder and the robot; data (RTTY, or the data
// flag: USB-D, LSB-D...) shows RTTY beside FT8/FT4 and the robot (CW transmit only so
// far); voice (SSB, AM, FM) shows neither, only the waterfalls. A running robot stays visible.
const modeFamily = () => /^CW/.test(state.mode || '') ? 'cw'
  : (/^RTTY/.test(state.mode || '') || state.data) ? 'data' : state.mode ? 'voice' : 'cw';
function applyModeFamily() {
  const fam = modeFamily();
  document.body.dataset.fam = fam;
  $('rbMode').textContent = fam === 'data' ? 'Data' : 'CW';
  const idle = !document.body.classList.contains('robot-active');
  document.querySelectorAll('.robot .tx-btns button:not(#rbStop)').forEach((b) => { b.disabled = fam === 'data' && idle; });
}

function renderRobot(st) {
  document.body.classList.toggle('robot-active', !!st.active);
  applyModeFamily();
  const el = $('robotState');
  el.textContent = st.active ? st.state : 'idle';
  el.className = 'robot-state' + (st.state === 'transmitting' ? ' tx' : st.active ? ' active' : '');
  $('rbDry').checked = st.cfg.dry_run;
  if (st.in_qso) {
    for (const [id, k] of Object.entries(OB)) {
      if (document.activeElement === $(id)) continue;     // don't fight the operator's typing
      $(id).value = st.qso[k] || '';
      setSrc(k, st.locked.includes(k) ? 'you' : st.qso[k] ? 'decoded' : '');
    }
    $('obState').textContent = st.active ? 'in contact' : 'contact';
  } else {
    if (robotInQso) clearOther(false);                   // contact ended (it is in the log)
    $('obState').textContent = 'no contact';
  }
  robotInQso = st.in_qso;
  $('rbCq').disabled = $('rbAnswer').disabled = $('rbPath').disabled = $('rbHunt').disabled = st.active;
  renderHunt(st);
}
function renderHunt(st) {
  const c = st.cands || [];
  $('rbHuntList').classList.toggle('on', c.length > 0);
  const body = $('rbHuntBody'); body.textContent = '';
  for (const r of c) {
    const tr = body.insertRow();
    tr.className = r.call === st.pick ? 'pick' : r.why ? 'skip' : '';
    tr.title = `${r.text}${r.directed ? ` · CQ ${r.directed}` : ''} · ${r.why || (r.call === st.pick ? 'being answered' : 'answerable')}`;
    for (const v of [(r.hz / 1e3).toFixed(2), r.call, r.country, r.snr, r.wpm || '',
      r.call === st.pick ? 'answering' : r.why || '']) tr.insertCell().textContent = v;
    tr.cells[1].className = 'call';
  }
}
function robotEvent(m) {
  const el = $('rbLog');
  const div = document.createElement('div');
  div.className = m.kind;
  const label = { tx: 'TX', rx: 'RX', llm: 'LLM', parse: 'READ', note: '··', error: 'ERR', log: 'LOG' }[m.kind] || m.kind;
  div.textContent = `${m.t} ${label}  ${m.text}`;
  el.appendChild(div);
  while (el.childNodes.length > 500) el.removeChild(el.firstChild);
  el.scrollTop = el.scrollHeight;
}

// ------------------------------------------------------------ spots (DX Summit + HamQTH + RBN), all modes
$('spBand').innerHTML = '<option value="">Current band</option><option value="all">All bands</option>'
  + BANDS.map((b) => `<option value="${b[0]}">${b[0]} m</option>`).join('');
$('spBand').value = lsGet('spBand', '');
$('spAge').value = lsGet('spAge', '900');
$('spMode').value = lsGet('spMode', '');
function spotsBand() {
  const v = $('spBand').value;
  if (v === 'all') return null;
  return v || (bandOf(state.freq) || [null])[0];
}
let spotsLastBand;   // band of the last request, to re-query when the radio changes band
function spotsBandCheck() { if (spotsBand() !== spotsLastBand) requestSpots(); }
function requestSpots() {
  spotsLastBand = spotsBand();
  const call = ($('rbCall').value || $('mycall').value || '').toUpperCase().trim();
  send({ cmd: 'spots', band: spotsBand(), max_age: +$('spAge').value, call: call || undefined });
}
for (const id of ['spBand', 'spAge']) $(id).onchange = () => { lsSet(id, $(id).value); requestSpots(); };
$('spMode').onchange = () => { lsSet('spMode', $('spMode').value); if (renderSpots.last) renderSpots(renderSpots.last); };
$('spRefresh').onclick = requestSpots;
setInterval(() => { if (ws && ws.readyState === 1) requestSpots(); }, 60000);

function fmtAge(s) { return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : `${Math.floor(s / 3600)}h`; }
// clicking a spot tunes the radio to it in its mode; FT8/FT4 snap to the dial frequency
const FT_DIALS = { FT8: BANDS.map((b) => b[5]), FT4: BANDS.map((b) => b[6]) };
function qsySpot(st) {
  const hz = Math.round(st.khz * 1000);
  const sideband = hz < 10e6 && !(hz >= 5.25e6 && hz <= 5.45e6) ? 'LSB' : 'USB';
  if (st.mode === 'CW') {
    send({ cmd: 'mode', mode: 'CW', data: false });
    setFreq(hz);
    if (state.cw_pitch) send({ cmd: 'decoder', cw_freq: state.cw_pitch });
    if (!robotInQso) { clearOther(false); $('rbTarget').value = st.call; setSrc('call', 'you'); }
  } else if (st.mode === 'SSB') {
    send({ cmd: 'mode', mode: sideband, data: false }); setFreq(hz);
  } else if (st.mode === 'RTTY') {
    send({ cmd: 'mode', mode: 'RTTY', data: false }); setFreq(hz);     // spot = mark, the RTTY dial
  } else if (st.mode === 'AM' || st.mode === 'FM') {
    send({ cmd: 'mode', mode: st.mode, data: false }); setFreq(hz);
  } else {                                                             // FT8 / FT4 / other data: USB-D
    const dial = (FT_DIALS[st.mode] || []).find((d) => d && hz - d >= 0 && hz - d <= 3000);
    send({ cmd: 'mode', mode: 'USB', data: true }); setFreq(dial || hz - 1500);
  }
}
function renderSpots(m) {
  if ((m.band || null) !== spotsBand()) return;   // reply to another client's / an older request
  renderSpots.last = m;
  const mode = $('spMode').value;
  const shown = mode ? m.stations.filter((st) => st.mode === mode) : m.stations;
  const rbn = m.status.rbn;
  $('spInfo').textContent = `${shown.length}${mode ? ' ' + mode + ' of ' + m.stations.length : ''} stations · RBN ${rbn.connected ? 'connected as ' + rbn.call
    : rbn.call ? 'connecting…' + (rbn.error ? ' (' + rbn.error + ')' : '') : 'off (enter your callsign)'}`
    + Object.entries(m.status.errors).map(([k, v]) => ` · ${k} error: ${v}`).join('');
  const body = $('spBody'); body.textContent = '';
  if (!shown.length) {
    const td = body.insertRow().insertCell();
    td.colSpan = 11; td.className = 'hint';
    td.textContent = `No ${mode || ''} spots ${m.band ? 'on ' + m.band + ' m ' : ''}in the last ${$('spAge').selectedOptions[0].text}.`
      + (rbn.connected ? '' : ' Enter your callsign in the robot panel to add live Reverse Beacon Network spots,')
      + ' or try All bands / All modes / a longer window.';
  }
  // stations on a path our last path check proved open come first
  const stations = [...shown].sort((a, b) => !!b.path - !!a.path);
  for (const st of stations) {
    const tr = document.createElement('tr');
    if (/\bCQ\b/.test(st.info)) tr.className = 'cq';
    if (st.path) tr.classList.add('path');
    tr.title = `${st.sources.join(' + ')}`
      + (st.path ? ` · spotted ${st.path[0]} km from ${st.path[1]}, who heard your path check` : '')
      + ` · ${st.country || '?'}` + (st.km != null ? ` · ${st.km} km` : '') + ` · ${st.spotters} spotter${st.spotters === 1 ? '' : 's'}`
      + (st.wpm ? ` · ${st.wpm} WPM` : '') + (st.info ? ` · "${st.info}"` : '')
      + ` · click to tune in ${st.mode}` + (st.mode === 'CW' ? ' and set as robot target' : '');
    for (const v of [fmtAge(st.age), st.khz.toFixed(1), st.mode, st.call, st.wpm ?? '', st.snr ?? '', st.spotters,
                     st.km ?? '', st.path ? '✓' : '', st.country, st.info]) {
      const td = document.createElement('td'); td.textContent = v; tr.appendChild(td);
    }
    tr.onclick = () => qsySpot(st);
    body.appendChild(tr);
  }
}

function renderPathCheck(r) {
  if (!r) return;
  const n = r.reports.length;
  const conts = [...new Set(r.reports.map((x) => x.cont).filter(Boolean))].join(' ');
  $('pcInfo').textContent = `${r.call} on ${r.band} m (${r.khz.toFixed(1)} kHz) at ${new Date(r.start * 1000).toISOString().slice(11, 16)} UTC`
    + (r.dry_run ? ' · DRY RUN, nothing was sent' : '')
    + ` · ${r.state === 'collecting' ? `collecting, ${r.remaining}s left` : r.state}`
    + ` · heard by ${n}${conts ? ' (' + conts + ')' : ''}`
    + ` · PSKReporter: ${r.psk === 'pending' ? `queried at the end (in ${r.remaining}s)` : r.psk}`;
  $('pcWrap').hidden = !n;
  const body = $('pcBody'); body.textContent = '';
  for (const x of r.reports) {
    const tr = body.insertRow();
    const via = x.sources.map((s) => ({ rbn: 'RBN', pskreporter: 'PSKReporter' })[s] || s).join(' + ');
    for (const v of [x.receiver, via, x.country, x.grid, x.km ?? '', x.bearing != null ? x.bearing + '°' : '',
                     x.snr ?? '', x.khz.toFixed(1), new Date(x.ts * 1000).toISOString().slice(11, 16)])
      tr.insertCell().textContent = v;
  }
  if (r.state === 'done' && renderPathCheck.lastStart !== r.start) { renderPathCheck.lastStart = r.start; requestSpots(); }
}

// ------------------------------------------------------------ NCDXF beacon monitor
let bn = null, bnSkew = 0;   // last data; client clock - server clock
function bnCfg() { return { mode: $('bnMode').value, band: +$('bnBand').value, beacon: +$('bnBeacon').value }; }
function bnShowPickers() {
  $('bnBand').hidden = $('bnMode').value !== 'band';
  $('bnBeacon').hidden = $('bnMode').value !== 'follow';
}
for (const id of ['bnMode', 'bnBand', 'bnBeacon']) $(id).addEventListener('change', () => { lsSet(id, $(id).value); bnShowPickers(); });
$('bnStart').onclick = () => send({ cmd: 'beacon_start', cfg: bnCfg() });
$('bnStop').onclick = () => send({ cmd: 'beacon_stop' });
const fmtDb = (v) => v == null ? '–' : (v > 0 ? '+' : '') + v.toFixed(0);

function renderBeacons(d) {
  if (d) { bnSkew = Date.now() / 1000 - d.server_time; }
  if (!bn && d) {             // first data: fill pickers and table header
    $('bnBand').innerHTML = d.bands.map((b, i) => `<option value="${i}">${b} m</option>`).join('');
    $('bnBeacon').innerHTML = d.beacons.map((b, i) => `<option value="${i}">${b.call} ${b.loc}</option>`).join('');
    for (const id of ['bnMode', 'bnBand', 'bnBeacon']) $(id).value = lsGet(id, $(id).value);
    bnShowPickers();
    $('bnHead').innerHTML = '<th>Beacon</th><th>Location</th><th>km</th><th>Brg</th>'
      + d.bands.map((b) => `<th>${b} m</th>`).join('');
  }
  if (d) bn = d;
  if (!bn) return;
  const now = Date.now() / 1000 - bnSkew;
  const cur = bn.now;
  $('bnStart').disabled = bn.active; $('bnStop').disabled = !bn.active;
  document.body.classList.toggle('radio-locked', bn.active);
  const heard = Object.values(bn.results).filter((r) => r.dashes > 0 && now - r.ts < 900).length;
  $('bnInfo').textContent = (bn.active ? (cur && cur.beacon != null
      ? `now ${bn.bands[cur.band]} m: ${bn.beacons[cur.beacon].call} (${bn.beacons[cur.beacon].loc})`
      : 'waiting for the beacon to reach our bands') : 'stopped')
    + ` · ${heard} heard in the last 15 min`;
  const body = $('bnBody'); body.textContent = '';
  bn.beacons.forEach((b, i) => {
    const tr = body.insertRow();
    for (const v of [b.call, b.loc, b.km ?? '', b.brg != null ? b.brg + '°' : '']) tr.insertCell().textContent = v;
    tr.cells[0].title = `${b.call} · ${b.loc}` + (b.km != null ? ` · ${b.km} km` : '') + (b.brg != null ? ` · ${b.brg}°` : '');
    bn.bands.forEach((band, j) => {
      const td = tr.insertCell(), r = bn.results[`${i}:${j}`];
      td.className = 'bn-cell';
      if (cur && cur.band === j && cur.beacon === i) td.classList.add('now');
      if (!r) { td.title = 'not measured yet'; return; }
      const age = now - r.ts;
      if (age > 900) td.classList.add('old');
      const bars = [0, 1, 2, 3].map((k) => `<i class="${k < r.dashes ? 'on' : ''}"></i>`).join('');
      td.innerHTML = `<span class="bars">${bars}</span> <span class="${r.dashes ? '' : 'dim'}">${r.dashes ? fmtDb(r.snr[0]) : '–'}</span>`;
      td.title = `${b.call} on ${band} m, ${new Date(r.ts * 1000).toISOString().slice(11, 19)} UTC (${fmtAge(Math.round(age))} ago)\n`
        + bn.powers.map((p, k) => `${p}: ${fmtDb(r.snr[k])} dB`).join(' · ')
        + `\ncallsign keying ${r.id_db} dB${r.dashes ? '' : ' (needs 6 dB: not identified)'} · tone ${r.hz} Hz · timing ${r.offset} s`;
    });
  });
}
setInterval(() => renderBeacons(null), 30000);

// ------------------------------------------------------------ collapsible panels
// Every <section data-title> gets a fold button; control groups fold by their title.
function setFolded(el, key, folded) {
  el.classList.toggle('collapsed', folded);
  lsSet('fold:' + key, folded);
  if (!folded) window.dispatchEvent(new Event('resize'));   // canvases re-measure when shown again
}
document.querySelectorAll('section[data-title]').forEach((sec) => {
  const key = sec.dataset.title;
  const btn = document.createElement('button');
  btn.className = 'small fold'; btn.title = 'Collapse / expand';
  btn.onclick = () => setFolded(sec, key, !sec.classList.contains('collapsed'));
  sec.prepend(btn);
  if (lsGet('fold:' + key, false)) sec.classList.add('collapsed');
});
document.querySelectorAll('.group > .gtitle').forEach((t) => {
  const g = t.parentElement, key = 'group ' + t.textContent;
  t.onclick = () => setFolded(g, key, !g.classList.contains('collapsed'));
  if (lsGet('fold:' + key, false)) g.classList.add('collapsed');
});

renderFreq();
connect();
