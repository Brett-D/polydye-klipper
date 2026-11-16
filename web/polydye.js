// PolyDye page: talks to Moonraker's websocket JSON-RPC API, like Mainsail.
// This file is part of polydye-klipper (GPLv3).
'use strict';

const $ = (id) => document.getElementById(id);
const state = { klippy: 'disconnected', message: '', board: false, printer: {} };
let ws = null;
let nextId = 1;
const pending = new Map();

function log(line) {
  const el = $('log');
  const t = new Date().toLocaleTimeString();
  el.textContent = `${t}  ${line}\n` + el.textContent.slice(0, 20000);
}

function rpc(method, params = {}) {
  return new Promise((resolve, reject) => {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      reject(new Error('not connected to Moonraker'));
      return;
    }
    const id = nextId++;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ jsonrpc: '2.0', method, params, id }));
  });
}

async function gcode(script) {
  log(`> ${script}`);
  try {
    await rpc('printer.gcode.script', { script });
  } catch (e) {
    log(`! ${e.message}`);
  }
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/websocket`);
  ws.onopen = async () => {
    setConn('connected', 'ok');
    try {
      await rpc('server.connection.identify', {
        client_name: 'PolyDye', version: '0.1', type: 'web', url: location.href,
      });
    } catch (e) { /* older Moonraker: identification is optional */ }
    initKlippy();
    loadSettings();
  };
  ws.onclose = () => {
    setConn('disconnected', 'bad');
    for (const p of pending.values()) p.reject(new Error('connection closed'));
    pending.clear();
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id !== undefined && pending.has(msg.id)) {
      const p = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) p.reject(new Error(msg.error.message || 'error'));
      else p.resolve(msg.result);
      return;
    }
    switch (msg.method) {
      case 'notify_status_update':
        merge(msg.params[0]);
        render();
        break;
      case 'notify_gcode_response': {
        const line = String(msg.params[0]);
        if (/polydye|^!!/i.test(line)) log(line);
        break;
      }
      case 'notify_klippy_ready':
      case 'notify_klippy_shutdown':
      case 'notify_klippy_disconnected':
        setTimeout(initKlippy, 500);
        break;
      default:
        break;
    }
  };
}

function setConn(text, cls) {
  const el = $('conn');
  el.textContent = text;
  el.className = `pill ${cls}`;
}

function merge(status) {
  for (const [obj, fields] of Object.entries(status || {})) {
    state.printer[obj] = Object.assign(state.printer[obj] || {}, fields);
  }
}

async function initKlippy() {
  try {
    const info = await rpc('printer.info');
    state.klippy = info.state;
    state.message = info.state_message || '';
    if (info.state !== 'ready') {
      state.board = false;
      render();
      if (info.state === 'startup') setTimeout(initKlippy, 1000);
      return;
    }
    const list = await rpc('printer.objects.list');
    state.board = list.objects.includes('polydye');
    const objects = {
      webhooks: ['state', 'state_message'],
      print_stats: ['state'],
      toolhead: ['homed_axes'],
    };
    if (state.board) {
      objects.polydye = null;
      objects['mcu polydye'] = ['mcu_version'];
    }
    state.printer = {};
    const sub = await rpc('printer.objects.subscribe', { objects });
    merge(sub.status);
  } catch (e) {
    state.klippy = 'disconnected';
    state.message = e.message;
  }
  render();
}

function text(id, value, cls) {
  const el = $(id);
  el.textContent = value;
  el.className = `v ${cls || ''}`;
}

function render() {
  const banner = $('banner');
  const p = state.printer;
  const printing = ['printing', 'paused'].includes((p.print_stats || {}).state);
  if (state.klippy !== 'ready') {
    banner.className = 'banner bad';
    banner.textContent = `Klipper is ${state.klippy}. ${state.message}`;
  } else if (!state.board) {
    banner.className = 'banner';
    banner.textContent = 'The PolyDye board is not active. Plug its USB (J7) into the Pi, then restart the Klipper '
      + 'service (Mainsail power menu, Services, klipper). Without the board the printer works as normal.';
  } else {
    banner.className = 'banner hidden';
  }
  const pd = p.polydye || {};
  const known = state.board && pd.passes !== undefined;
  text('s-fw', ((p['mcu polydye'] || {}).mcu_version) || '–');
  text('s-boost', known ? (pd.boost ? 'on' : 'off') : '–', pd.boost ? 'on' : '');
  text('s-live', known ? (pd.live ? 'LIVE ink' : 'dry run') : '–', pd.live ? 'live' : '');
  text('s-running', known ? (pd.running ? 'yes' : 'no') : '–');
  text('s-queued', known ? pd.queued : '–');
  text('s-passes', known ? pd.passes : '–');
  text('s-faults', known ? pd.faults : '–', known && pd.faults ? 'live' : '');
  text('s-late', known ? `${pd.late_max_us.toFixed(1)} µs` : '–');
  text('s-store', known ? pd.store_count : '–');
  text('s-free', known ? `${Math.round(pd.store_free / 1024)} kB` : '–');
  document.querySelectorAll('.needs-board').forEach((b) => {
    b.disabled = !state.board || (b.classList.contains('needs-idle') && printing);
  });
}

// ---- settings: edit the [polydye] section of polydye.cfg ------------------------------------------------------

const FIELDS = ['pass_axis', 'offset_x', 'offset_y', 'dpi', 'pass_speed'];
let cfgText = '';

function sectionRange(lines) {
  const start = lines.findIndex((l) => /^\s*\[polydye\]\s*$/.test(l));
  if (start < 0) return null;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (/^\s*\[/.test(lines[i])) { end = i; break; }
  }
  return [start, end];
}

function keyRegex(key) {
  return new RegExp(`^(\\s*)(#\\s*)?${key}\\s*[:=]\\s*([^#]*?)\\s*(#.*)?$`);
}

async function loadSettings() {
  try {
    const r = await fetch('/server/files/config/polydye.cfg', { cache: 'no-store' });
    if (!r.ok) throw new Error(`polydye.cfg: HTTP ${r.status}`);
    cfgText = await r.text();
  } catch (e) {
    log(`! could not read polydye.cfg: ${e.message}`);
    return;
  }
  const lines = cfgText.split('\n');
  const range = sectionRange(lines);
  if (!range) return;
  const form = $('settings');
  for (const key of FIELDS) {
    for (let i = range[0] + 1; i < range[1]; i++) {
      const m = lines[i].match(keyRegex(key));
      if (m && !m[2]) {
        form.elements[key].value = m[3];
        break;
      }
    }
  }
}

function applySettings(textIn, values) {
  const lines = textIn.split('\n');
  const range = sectionRange(lines);
  if (!range) throw new Error('polydye.cfg has no [polydye] section');
  for (const [key, value] of Object.entries(values)) {
    if (value === '') continue;
    let done = false;
    for (let i = range[0] + 1; i < range[1] && !done; i++) {
      const m = lines[i].match(keyRegex(key));
      if (m) {
        lines[i] = `${m[1]}${key}: ${value}${m[4] ? `  ${m[4]}` : ''}`;
        done = true;
      }
    }
    if (!done) {
      lines.splice(range[0] + 1, 0, `${key}: ${value}`);
      range[1] += 1;
    }
  }
  return lines.join('\n');
}

$('settings').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  const values = {};
  for (const key of FIELDS) values[key] = form.elements[key].value.trim();
  try {
    const updated = applySettings(cfgText, values);
    const body = new FormData();
    body.append('root', 'config');
    body.append('file', new Blob([updated], { type: 'text/plain' }), 'polydye.cfg');
    const r = await fetch('/server/files/upload', { method: 'POST', body });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    cfgText = updated;
    log('saved polydye.cfg');
    $('restart-btn').classList.remove('hidden');
  } catch (e) {
    log(`! save failed: ${e.message}`);
  }
});

$('restart-btn').addEventListener('click', async () => {
  $('restart-btn').classList.add('hidden');
  log('> RESTART');
  try {
    await rpc('printer.restart');
  } catch (e) {
    log(`! ${e.message}`);
  }
});

// ---- controls -------------------------------------------------------------------------------------------------

document.querySelectorAll('button[data-gcode]').forEach((b) => {
  b.addEventListener('click', () => gcode(b.dataset.gcode));
});

$('live-btn').addEventListener('click', () => {
  const ok = window.confirm('Enable LIVE ink?\n\nOnly with a cartridge fitted in a calibrated mount. '
    + 'Passes will fire real ink until you press Off or Klipper stops.');
  if (ok) gcode('POLYDYE_ENABLE LIVE=1');
});

connect();
render();
