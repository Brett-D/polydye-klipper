// PolyDye page: talks to Moonraker's websocket JSON-RPC API, like Mainsail.
// This file is part of polydye-klipper (GPLv3).
'use strict';

const $ = (id) => document.getElementById(id);
const state = { klippy: 'disconnected', message: '', board: false, printer: {}, fw: { bundled: null, updating: false } };
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
    loadFirmwareInfo();
  };
  ws.onclose = () => {
    setConn('disconnected', 'bad');
    for (const p of pending.values()) p.reject(new Error('connection closed'));
    pending.clear();
    state.klippy = 'unreachable';
    state.message = 'Lost the connection to Moonraker; retrying.';
    render();
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
      case 'notify_polydye_firmware':
        firmwareProgress(msg.params[0]);
        break;
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
  $('estop').disabled = cls !== 'ok';
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
  const bannerText = $('banner-text');
  const p = state.printer;
  const printing = ['printing', 'paused'].includes((p.print_stats || {}).state);
  $('fw-restart-btn').classList.toggle('hidden', !['shutdown', 'error'].includes(state.klippy));
  if (state.klippy !== 'ready') {
    banner.className = 'banner bad';
    bannerText.textContent = `Klipper is ${state.klippy}. ${state.message}`;
  } else if (!state.board) {
    banner.className = 'banner';
    bannerText.textContent = 'The PolyDye board is not active. Plug its USB (J7) into the Pi, then restart the Klipper '
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
  renderFirmware(printing);
}

// ---- board firmware updates (moonraker/polydye.py runs them) --------------------------------------------------

function boardVersion() {
  return (state.printer['mcu polydye'] || {}).mcu_version || '';
}

function renderFirmware(printing) {
  const bundled = state.fw.bundled;
  const board = boardVersion();
  text('fw-board', board || (state.fw.updating ? 'updating…' : '–'));
  text('fw-bundled', bundled ? bundled.version : 'none');
  const same = bundled && board && bundled.version === board;
  $('fw-update-btn').textContent = same ? 'Reinstall bundled firmware' : 'Update to bundled firmware';
  const offline = !ws || ws.readyState !== WebSocket.OPEN;
  document.querySelectorAll('.fw-btn').forEach((b) => { b.disabled = offline || printing || state.fw.updating; });
  if (!bundled) $('fw-update-btn').disabled = true;
  const power = state.fw.power;
  $('fw-power').classList.toggle('hidden', !power);
  $('fw-power').textContent = power ? `Updates are off: ${power}. A Raspberry Pi 3 short of power loses USB data `
    + 'and can lock up during an update. Use a 5.1 V, 2.5 A supply (the official one is best), then restart the Pi.' : '';
  if (power) document.querySelectorAll('.fw-btn').forEach((b) => { b.disabled = true; });
}

async function loadFirmwareInfo() {
  try {
    const info = await rpc('server.polydye.firmware');
    state.fw.bundled = info.bundled;
    state.fw.updating = info.updating;
    state.fw.power = info.power_problem;
  } catch (e) {
    log(`! firmware info: ${e.message}`);
  }
  render();
}

function firmwareProgress(p) {
  $('fw-progress').classList.remove('hidden');
  if (p.progress !== null && p.progress !== undefined) $('fw-bar').value = p.progress;
  const msg = $('fw-msg');
  msg.textContent = p.message;
  msg.className = p.state === 'error' ? 'bad' : (p.state === 'done' ? 'good' : '');
  state.fw.updating = p.state === 'running';
  if (p.state !== 'running' || p.progress === null || p.progress === undefined) log(`firmware: ${p.message}`);
  render();
}

async function startFirmwareUpdate(params, label) {
  const ok = window.confirm(`${label}?\n\nThe printer must be idle. Klipper loses the PolyDye board for about half `
    + 'a minute while it restarts, then reconnects.');
  if (!ok) return;
  try {
    const r = await rpc('server.polydye.firmware.update', params);
    state.fw.updating = true;
    log(`> firmware update to ${r.version}`);
    firmwareProgress({ state: 'running', message: `updating to ${r.version}`, progress: 0 });
  } catch (e) {
    firmwareProgress({ state: 'error', message: e.message, progress: null });
  }
}

$('fw-update-btn').addEventListener('click', () => {
  const bundled = state.fw.bundled;
  const same = bundled && bundled.version === boardVersion();
  startFirmwareUpdate(same ? { force: true } : {}, `${same ? 'Reinstall' : 'Install'} firmware ${bundled.version}`);
});

$('fw-file-btn').addEventListener('click', () => $('fw-file').click());

$('fw-file').addEventListener('change', async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = '';
  if (!file) return;
  try {
    const body = new FormData();
    body.append('root', 'config');
    body.append('path', 'polydye-fw');
    body.append('file', file, file.name);
    const r = await fetch('/server/files/upload', { method: 'POST', body });
    if (!r.ok) throw new Error(`upload: HTTP ${r.status}`);
  } catch (e) {
    firmwareProgress({ state: 'error', message: e.message, progress: null });
    return;
  }
  startFirmwareUpdate({ file: `polydye-fw/${file.name}`, force: true }, `Install ${file.name}`);
});

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

// Moonraker's emergency stop goes straight to Klipper, not through the G-code queue, so it also stops a running
// macro. No confirmation, as in Mainsail.
$('estop').addEventListener('click', async () => {
  log('> EMERGENCY STOP');
  try {
    await rpc('printer.emergency_stop');
  } catch (e) {
    log(`! ${e.message}`);
  }
});

$('fw-restart-btn').addEventListener('click', async () => {
  $('fw-restart-btn').classList.add('hidden');
  log('> FIRMWARE_RESTART');
  try {
    await rpc('printer.firmware_restart');
  } catch (e) {
    log(`! ${e.message}`);
  }
});

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
