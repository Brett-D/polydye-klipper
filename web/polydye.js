// PolyDye page: talks to Moonraker's websocket JSON-RPC API, like Mainsail.
// This file is part of polydye-klipper (GPLv3).
'use strict';

const $ = (id) => document.getElementById(id);
const state = {
  online: false, klippy: 'disconnected', message: '', board: false, printer: {},
  fw: { bundled: null, updating: false, power: null, boardOnUsb: false, files: {} },
};
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

// Runs a script, logging it and any error; returns whether it went through.
async function gcode(script) {
  log(`> ${script}`);
  try {
    await rpc('printer.gcode.script', { script });
    return true;
  } catch (e) {
    log(`! ${e.message}`);
    return false;
  }
}

function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/websocket`);
  ws.onopen = async () => {
    state.online = true;
    render();
    try {
      await rpc('server.connection.identify', {
        client_name: 'PolyDye', version: '0.1', type: 'web', url: location.href,
      });
    } catch (e) { /* older Moonraker: identification is optional */ }
    initKlippy();
    loadSettings();
  };
  ws.onclose = () => {
    state.online = false;
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

// The header pill sums up the state that matters, most important first.
function statusPill() {
  const pd = state.printer.polydye || {};
  if (!state.online) return ['Moonraker offline', 'bad'];
  if (state.fw.updating) return ['updating board firmware', 'warn'];
  if (state.klippy === 'startup') return ['Klipper starting', 'warn'];
  if (state.klippy === 'shutdown') return ['Klipper shut down', 'bad'];
  if (state.klippy !== 'ready') return [`Klipper ${state.klippy}`, 'bad'];
  if (!state.board) return [state.fw.boardOnUsb ? 'PolyDye not enabled' : 'PolyDye board not plugged in', 'warn'];
  if (pd.passes === undefined) return ['PolyDye connecting', 'warn'];
  if (pd.live) return ['LIVE ink', 'bad'];
  if (pd.running) return ['printing a pass (dry run)', 'ok'];
  if (pd.boost) return ['PolyDye on, dry run', 'ok'];
  return ['PolyDye ready', 'ok'];
}

function setConn() {
  const [label, cls] = statusPill();
  const el = $('conn');
  el.textContent = label;
  el.className = `pill ${cls}`;
  $('estop').disabled = !state.online;
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
      state.printer = {};  // stale while Klipper isn't ready (e.g. "printing" from before a shutdown)
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
  } finally {
    // The firmware API works without Klipper, so this also runs when Klipper isn't ready.
    render();
    loadFirmwareInfo();
  }
}

// USB presence (for the pill and Enable PolyDye) changes without any Klipper event, so poll it while the board
// isn't in use.
setInterval(() => {
  if (state.online && !state.board && !state.fw.updating) loadFirmwareInfo();
}, 5000);

function text(id, value, cls) {
  const el = $(id);
  el.textContent = value;
  el.className = `v ${cls || ''}`;
}

function render() {
  const corrupt = state.fw.files.corrupt || [];
  $('files-banner').classList.toggle('hidden', !corrupt.length);
  $('files-banner').textContent = corrupt.length ? `This add-on's files are damaged: ${corrupt.join(', ')}, `
    + 'probably by an unsafe shutdown (the Pi froze or lost power) right after an update. Recover them in Mainsail: '
    + 'Machine, Update Manager, polydye, Recover, Hard Recovery. See the README, "Raspberry Pi 3: freezes and '
    + 'damaged files".' : '';
  const banner = $('banner');
  const bannerText = $('banner-text');
  const p = state.printer;
  const printing = state.klippy === 'ready' && ['printing', 'paused'].includes((p.print_stats || {}).state);
  $('fw-restart-btn').classList.toggle('hidden', !['shutdown', 'error'].includes(state.klippy));
  if (state.klippy !== 'ready') {
    banner.className = 'banner bad';
    bannerText.textContent = `Klipper is ${state.klippy}. ${state.message}`;
  } else if (!state.board) {
    banner.className = 'banner';
    bannerText.textContent = state.fw.boardOnUsb
      ? 'The PolyDye board is plugged in, but Klipper started without it. Enable PolyDye restarts the Klipper '
        + 'service so it picks the board up (the printer must be idle).'
      : 'The PolyDye board is not active. Plug its USB (J7) into the Pi, then restart the Klipper '
        + 'service (Mainsail power menu, Services, klipper). Without the board the printer works as normal.';
  } else {
    banner.className = 'banner hidden';
  }
  $('enable-btn').classList.toggle('hidden', !(state.klippy === 'ready' && !state.board && state.fw.boardOnUsb));
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
  text('s-tpl', known ? (pd.cartridge_template || 'unknown: update the board firmware') : '–');
  text('s-pulse', known && pd.pulse_words ? `${pd.pulse_words} words, ${pd.pulse_us} µs` : '–');
  let clock = '–';
  if (known && pd.bus_clock) {
    clock = `${pd.bus_clock}, ${(pd.bus_hz / 1e6).toFixed(3)} MHz`;
    if (pd.bus_hz_measured) clock += ` (measured ${(pd.bus_hz_measured / 1e6).toFixed(3)})`;
  }
  text('s-clock', clock);
  text('s-head', known ? headText(pd) : '–');
  renderNudges(pd);
  // Idle-only controls also wait for a firmware update: the dry run uses the same data port. Off stays available.
  document.querySelectorAll('.needs-board').forEach((b) => {
    b.disabled = !state.board || (b.classList.contains('needs-idle') && (printing || state.fw.updating));
  });
  renderFirmware(printing);
  renderDiagnostics(pd, printing);
  setConn();
}

// The head's warmth as the automatic warm-up sees it: when it last fired, and whether the next live pass warms it.
function headText(pd) {
  const warm = pd.auto_warm;
  const idle = pd.head_idle_s;
  const fired = idle === null || idle === undefined ? 'not fired since Klipper started'
    : `last fired ${idle < 120 ? `${Math.round(idle)} s` : `${(idle / 60).toFixed(0)} min`} ago`;
  if (!warm || !warm.seconds) return `${fired} (automatic warm-up off)`;
  if (warm.available === false) return `${fired} (automatic warm-up needs newer board firmware: Update to bundled firmware)`;
  const cold = idle === null || idle === undefined || idle > warm.idle_time;
  return cold ? `${fired}: a live pass warms it first (${warm.seconds} s)` : `${fired}: warm`;
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
  // false only once Klipper has connected to board firmware that restarts the chip on Klipper's reset.
  $('fw-hard-reset').classList.toggle('hidden', (state.printer.polydye || {}).board_soft_reset !== false);
}

async function loadFirmwareInfo() {
  try {
    const info = await rpc('server.polydye.firmware');
    state.fw.bundled = info.bundled;
    state.fw.updating = info.updating;
    state.fw.power = info.power_problem;
    state.fw.boardOnUsb = info.board_on_usb;
    state.fw.klipperService = info.klipper_service || 'klipper';
    state.fw.files = info.files || {};
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
  const ok = window.confirm(`${label}?\n\nThe printer must be idle. The ${state.fw.klipperService} service is `
    + 'stopped for about half a minute while the board restarts into the new firmware, then started again.');
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

const FIELDS = ['pass_axis', 'offset_x', 'offset_y', 'direction_trim', 'cartridge_template', 'pulse_words', 'dpi',
  'pass_speed'];
const NUDGED = ['offset_x', 'offset_y', 'direction_trim'];
// The colour registration (POLYDYE_ALIGN), whole columns and positions; POLYDYE_SAVE stores it with the offsets.
const REG_KEYS = ['cyan_columns', 'cyan_positions', 'yellow_columns', 'yellow_positions'];
let cfgSaves;  // printer.polydye.cfg_saves last seen: Klipper wrote polydye.cfg when it changes
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

// The value of an active (uncommented) setting in the [polydye] section, or null.
function cfgValue(textIn, key) {
  const lines = textIn.split('\n');
  const range = sectionRange(lines);
  if (!range) return null;
  for (let i = range[0] + 1; i < range[1]; i++) {
    const m = lines[i].match(keyRegex(key));
    if (m && !m[2]) return m[3];
  }
  return null;
}

async function readCfg() {
  const r = await fetch('/server/files/config/polydye.cfg', { cache: 'no-store' });
  if (!r.ok) throw new Error(`polydye.cfg: HTTP ${r.status}`);
  return r.text();
}

async function fetchCfg() {
  cfgText = await readCfg();
  return cfgText;
}

async function loadSettings() {
  try {
    await fetchCfg();
  } catch (e) {
    log(`! could not read polydye.cfg: ${e.message}`);
    return;
  }
  const form = $('settings');
  for (const key of FIELDS) {
    let value = cfgValue(cfgText, key);
    // Klipper lowercases the template; the dropdown's values are lowercase, so match them or it shows nothing and
    // the next save would comment the setting out (as it does a template that was removed).
    if (value !== null && key === 'cartridge_template') value = value.toLowerCase();
    form.elements[key].value = value === null ? '' : value;
  }
  render();
}

// Templates the board offers (the add-on's templates): board firmware without the stock clock has no color_b_late.
const templateOk = (name) => ((state.printer.polydye || {}).templates || []).includes(name);
const templateOldText = (name) => `The ${name} template needs newer board firmware: Update to bundled firmware under `
  + 'Board firmware.';

// values: key -> string to set ('' leaves the line alone) or null to comment the setting out.
function applySettings(textIn, values) {
  const lines = textIn.split('\n');
  const range = sectionRange(lines);
  if (!range) throw new Error('polydye.cfg has no [polydye] section');
  const setLine = (h, v, commented) => {
    lines[h.i] = `${h.m[1]}${commented ? '#' : ''}${h.key}: ${v}${h.m[4] ? `  ${h.m[4]}` : ''}`;
  };
  for (const [key, value] of Object.entries(values)) {
    if (value === '') continue;
    const hits = [];
    for (let i = range[0] + 1; i < range[1]; i++) {
      const m = lines[i].match(keyRegex(key));
      if (m) hits.push({ i, m, key, active: !m[2] });
    }
    const active = hits.filter((h) => h.active);
    if (value === null) {
      active.forEach((h) => setLine(h, h.m[3], true));
      continue;
    }
    // Replace the active setting; reuse a commented-out example only when there is none. Klipper rejects an option
    // set twice, so any further active copies are commented out.
    const target = active[0] || hits[0];
    if (target) {
      setLine(target, value, false);
      active.slice(1).forEach((h) => setLine(h, h.m[3], true));
    } else {
      lines.splice(range[0] + 1, 0, `${key}: ${value}`);
      range[1] += 1;
    }
  }
  return lines.join('\n');
}

async function uploadCfg(updated) {
  const body = new FormData();
  body.append('root', 'config');
  body.append('file', new Blob([updated], { type: 'text/plain' }), 'polydye.cfg');
  const r = await fetch('/server/files/upload', { method: 'POST', body });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  cfgText = updated;
}

// Save and Store each read, merge and upload the whole file; run them one at a time so neither overwrites the other.
let cfgChain = Promise.resolve();
function queueCfg(op) {
  const run = cfgChain.then(op);
  cfgChain = run.catch(() => {});
  return run;
}

$('settings').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const form = ev.target;
  try {
    await queueCfg(async () => {
      // Read the form only now: a Store queued before this Save may just have refreshed the offset inputs.
      const values = {};
      for (const key of FIELDS) values[key] = form.elements[key].value.trim();
      values.cartridge_template = values.cartridge_template || null;
      values.pulse_words = values.pulse_words || null;
      // The bus runs only at the stock clock now, and Klipper refuses any other bus_clock: comment it out.
      values.bus_clock = null;
      // Klipper refuses to start with a pulse outside 6-8 words, so don't save one.
      const words = Number(values.pulse_words);
      if (values.pulse_words && !pulseOk(words)) throw new Error(pulseRangeText(words));
      // ... or with a template the board doesn't have.
      const tpl = values.cartridge_template;
      if (tpl && state.board && !templateOk(tpl)) throw new Error(templateOldText(tpl));
      // Re-read first: the file may have been edited elsewhere since the page loaded.
      await uploadCfg(applySettings(await fetchCfg(), values));
    });
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

// ---- ink position nudges: POLYDYE_NUDGE changes Klipper's live values; Store writes them to polydye.cfg -------

let nudgeStep = 0.1;
const fmtMm = (v) => String(Number(v.toFixed(4)));
// Nudges and Back to stored values run one after another in click order. Store waits for the nudges clicked before
// it, and Back for the Save/Store clicked before it. Each waits only on earlier clicks, so the two queues can never
// wait on each other in a circle.
let nudgeChain = Promise.resolve();
function queueNudge(op) {
  nudgeChain = nudgeChain.then(op).catch((e) => log(`! ${e.message}`));
  return nudgeChain;
}

function storedNudged(key, textIn = cfgText) {
  const raw = cfgValue(textIn, key);
  const v = parseFloat(raw === null && key === 'direction_trim' ? '0' : raw);
  return Number.isFinite(v) ? v : null;
}

function renderNudges(pd) {
  const axis = (pd.pass_axis || 'y').toUpperCase();
  $('trim-minus').textContent = `−${axis}`;
  $('trim-plus').textContent = `+${axis}`;
  $('trim-minus').title = `Move the ink of reverse passes towards −${axis}`;
  $('trim-plus').title = `Move the ink of reverse passes towards +${axis}`;
  for (const [key, id] of [['offset_x', 'n-x'], ['offset_y', 'n-y'], ['direction_trim', 'n-trim']]) {
    const live = state.board ? pd[key] : undefined;
    if (typeof live !== 'number') {
      text(id, '–');
      continue;
    }
    const stored = storedNudged(key);
    const unsaved = cfgText !== '' && (stored === null || Math.abs(live - stored) > 1e-6);
    text(id, `${live.toFixed(3)} mm${unsaved ? ' (not stored)' : ''}`, unsaved ? 'on' : '');
  }
  for (const [id, keys] of [['n-cyan', REG_KEYS.slice(0, 2)], ['n-yellow', REG_KEYS.slice(2)]]) {
    const live = keys.map((k) => (state.board ? pd[k] : undefined));
    if (live.some((v) => typeof v !== 'number')) {
      text(id, '–');
      continue;
    }
    const unsaved = cfgText !== '' && keys.some((k, i) => storedReg(k) !== live[i]);
    const signed = (v) => (v > 0 ? `+${v}` : `${v}`);
    text(id, `${signed(live[0])} columns, ${signed(live[1])} positions${unsaved ? ' (not stored)' : ''}`,
      unsaved ? 'on' : '');
  }
  // POLYDYE_SAVE, POLYDYE_ALIGN or POLYDYE_OFFSET X_TICK= wrote polydye.cfg: show what it holds now.
  if (pd.cfg_saves !== undefined && pd.cfg_saves !== cfgSaves) {
    const first = cfgSaves === undefined;
    cfgSaves = pd.cfg_saves;
    if (!first) queueCfg(refreshStored).catch((e) => log(`! could not read polydye.cfg: ${e.message}`));
  }
}

// A colour registration value as polydye.cfg has it (unset: 0), or null if it isn't a whole number.
function storedReg(key, textIn = cfgText) {
  const raw = cfgValue(textIn, key);
  if (raw === null) return 0;
  return /^-?\d+$/.test(raw.trim()) ? parseInt(raw, 10) : null;
}

// Re-reads polydye.cfg after Klipper stored values in it, and shows them in the Settings fields they belong to (only
// those: other fields may hold edits not saved yet).
async function refreshStored() {
  await fetchCfg();
  const form = $('settings');
  for (const key of NUDGED) {
    const v = cfgValue(cfgText, key);
    if (v !== null) form.elements[key].value = v;
  }
  render();
}

document.querySelectorAll('#nudge-steps button').forEach((b) => {
  b.addEventListener('click', () => {
    nudgeStep = parseFloat(b.dataset.step);
    document.querySelectorAll('#nudge-steps button').forEach((o) => o.classList.toggle('selected', o === b));
  });
});

document.querySelectorAll('button[data-nudge]').forEach((b) => {
  b.addEventListener('click', () => {
    const script = `POLYDYE_NUDGE ${b.dataset.nudge}=${fmtMm(parseFloat(b.dataset.sign) * nudgeStep)}`;
    queueNudge(() => gcode(script));
  });
});

$('nudge-store').addEventListener('click', async () => {
  const nudgesBefore = nudgeChain;
  try {
    await queueCfg(async () => {
      // After those nudges have run; Klipper writes what it uses (offsets, trim, colour registration).
      await nudgesBefore;
      log('> POLYDYE_SAVE');
      await rpc('printer.gcode.script', { script: 'POLYDYE_SAVE' });
      await refreshStored();
    });
  } catch (e) {
    log(`! store failed: ${e.message}`);
  }
});

$('nudge-undo').addEventListener('click', () => {
  // Take a place in the queue now, so nudges clicked while polydye.cfg loads run after the restore. Read without
  // touching the page's copy of the file, which Save and Store manage.
  const cfgBefore = cfgChain;
  queueNudge(async () => {
    await cfgBefore;    // a Store still uploading would otherwise be read as the old values
    let textIn;
    try {
      textIn = await readCfg();
    } catch (e) {
      log(`! could not read polydye.cfg: ${e.message}`);
      return;
    }
    const x = storedNudged('offset_x', textIn);
    const y = storedNudged('offset_y', textIn);
    const trim = storedNudged('direction_trim', textIn);
    const reg = REG_KEYS.map((k) => storedReg(k, textIn));
    if (x === null || y === null || trim === null || reg.includes(null)) {
      log('! polydye.cfg has no valid offset_x, offset_y or direction_trim, or an invalid colour registration');
      return;
    }
    await gcode(`POLYDYE_OFFSET X=${fmtMm(x)} Y=${fmtMm(y)} TRIM=${fmtMm(trim)} `
      + `${REG_KEYS.map((k, i) => `${k.toUpperCase()}=${reg[i]}`).join(' ')}`);
  });
});

// ---- diagnostics: fire test (PD_FIRE_TEST), spit test (POLYDYE_SPIT), signal check (POLYDYE_LEVEL) ---------------

// Cartridge contact pads (hardware/README.md section 4, from Sprite_tm's photo, looking at the contact side).
const PAD_SIDES = [
  ['7-pad side', ['S3', 'DCLK', 'D1', 'TS', 'D2', 'GND', 'PWRA']],
  ['9-pad side', ['PWRB', 'S5', 'D3', 'CSYNC', 'S1', 'S4', 'S2', 'GND', 'ID']],
];
const SIG_LINES = ['D1', 'D2', 'D3', 'CSYNC', 'S1', 'S2', 'S3', 'S4', 'S5', 'DCLK'];
const LOW_RAIL = ['D1', 'D2', 'D3', 'CSYNC'];
const SIG_POWER = [
  ['PWRA', 'PWRA', 'AO3401 drain, F3 pulse limiter'],
  ['PWRB', 'PWRB', 'AO3401 drain, F5 pulse limiter'],
  ['GND', 'GND', 'CD4504 pin 8'],
  ['GND (2nd)', 'GND', 'CD4504 pin 8'],
];
const SIG_KEY = 'polydye-signal-check';
const SIG_LAYOUT_KEY = 'polydye-signal-layout';
// Where each line should come out of the level shifters, from the C1/C2 schematic draft (hardware/README.md
// section 5): U2 is the CD4504 on the high rail, U1 the one on the low rail. A hypothesis until measured.
const CHIP_LEGS = {
  S5: 'U2 2', S1: 'U2 4', S4: 'U2 6', S2: 'U2 10', S3: 'U2 12', DCLK: 'U2 15',
  D1: 'U1 4', D2: 'U1 6', CSYNC: 'U1 10', D3: 'U1 12',
};
const SPIT_HZ = 1000;
// The bus runs only at the stock firmware's clock, 2.857 MHz (one word of fire pulse = 1 / BUS_HZ).
const BUS_HZ = 2857142;
const PULSE_MAX_US = 2.9;  // live-ink limit (polydye.py PULSE_MAX_US)
// Fire pulses to ask for (polydye.py PULSE_MIN, PULSE_MAX): 6 words is Sprite_tm's 1.8 µs rounded up, 8 the longest
// within the limit.
const PULSE_MIN_WORDS = 6;
const PULSE_MAX_WORDS = 8;
const pulseUs = (words) => (words * 1e6) / BUS_HZ;
const pulseOk = (words) => words >= PULSE_MIN_WORDS && words <= PULSE_MAX_WORDS;
const pulseRangeText = (words) => `fire pulse ${words} words is ${pulseUs(words).toFixed(2)} µs; use `
  + `${PULSE_MIN_WORDS}-${PULSE_MAX_WORDS} words (${pulseUs(PULSE_MIN_WORDS).toFixed(2)}-`
  + `${pulseUs(PULSE_MAX_WORDS).toFixed(2)} µs, at most ${PULSE_MAX_US} µs)`;
const TEMPLATES = ['color_b_late', 'black_a', 'black_b'];
// The random pattern's knobs (PATTERN=stock): form field, G-code parameter, range, whole numbers only.
const KNOBS = [
  ['density', 'DENSITY', 0, 100, false], ['repeat', 'REPEAT', 1, 16, true], ['first', 'FIRST', 0, 111, true],
  ['last', 'LAST', 0, 111, true], ['per_pulse', 'PER_PULSE', 0, 12, true],
];
const WARM_OLD_US = 0.75;  // warm-up pulse limit of board firmware without the warm-up duty limit (polydye.py)
const WARM_OLD_S = 10;     // and its longest warm-up

// A number field's value: { empty } when blank, { bad } when it isn't a number in min..max (whole if asked).
function numField(el, min, max, whole) {
  const raw = el.value.trim();
  const badInput = !!(el.validity && el.validity.badInput);
  if (!raw && !badInput) return { empty: true };
  const v = Number(raw);
  const bad = badInput || !Number.isFinite(v) || v < min || v > max || (whole && !Number.isInteger(v));
  return { v, bad, raw };
}

// The knobs a form sets, as G-code parameters (empty fields keep the stock pattern's values), or an error.
function knobParams(f) {
  let text = '';
  for (const [name, param, min, max, whole] of KNOBS) {
    const n = numField(f[name], min, max, whole);
    if (n.empty) continue;
    if (n.bad) {
      return { error: `${param} must be ${whole ? 'a whole number ' : ''}from ${min} to ${max}`
        + `${n.raw ? `, not "${n.raw}"` : ''}` };
    }
    text += ` ${param}=${n.v}`;
  }
  const colors = f.colors.value.trim().toLowerCase();
  if (colors) {
    if (!/^[cmy]{1,3}$/.test(colors) || new Set(colors).size !== colors.length) {
      return { error: `COLORS must be any of c, m and y, e.g. cmy or m, not "${f.colors.value.trim()}"` };
    }
    text += ` COLORS=${colors}`;
  }
  const first = f.first.value.trim() ? Number(f.first.value) : 14;
  const last = f.last.value.trim() ? Number(f.last.value) : 85;
  if (first > last) return { error: `the first position (${first}) is after the last (${last})` };
  return { text, error: '' };
}

const knobsGiven = (f) => KNOBS.some(([name]) => f[name].value.trim() !== '') || f.colors.value.trim() !== '';

// A form's warm-up fields as G-code parameters (WARM, WARM_PULSE, WARM_PERIOD_US; nothing without a warm-up), or an
// error.
function warmParams(f) {
  const warm = numField(f.warm, 0, 30, false);
  const pulse = numField(f.warm_pulse, 1, PULSE_MAX_WORDS, true);
  const period = numField(f.warm_period, 1, 65535, true);
  if (warm.bad || pulse.bad || period.bad) {
    return { error: `the warm-up is 0-30 s, its pulse 1-${PULSE_MAX_WORDS} words and its period 1-65535 µs (or leave `
      + 'them empty)' };
  }
  const seconds = warm.empty ? 0 : warm.v;
  if (!seconds) return { text: '', seconds: 0, error: '' };
  return {
    text: ` WARM=${seconds}${pulse.empty ? '' : ` WARM_PULSE=${pulse.v}`}`
      + `${period.empty ? '' : ` WARM_PERIOD_US=${period.v}`}`,
    seconds,
    pulse: pulse.empty ? 0 : pulse.v,
    error: '',
  };
}

// The knob fields only go with the stock pattern.
function showKnobs() {
  ['fire-form', 'spit-form'].forEach((id) => {
    const stock = $(id).elements.pattern.value === 'stock';
    $(id).querySelectorAll('.knob').forEach((el) => el.classList.toggle('hidden', !stock));
  });
}

function expectText(line) {
  return LOW_RAIL.includes(line) ? 'about 7.6 V (low rail, the reference design\'s 9 V)'
    : 'about 12.6 V (high rail, the reference design\'s 16 V)';
}

// "7-pad side: S3 · DCLK · D1 · …" with the signal in bold; GND matches both GND pads.
function padCell(name) {
  const td = document.createElement('td');
  td.className = 'pads';
  PAD_SIDES.forEach(([side, pads]) => {
    if (!pads.includes(name)) return;
    const div = document.createElement('div');
    div.append(`${side}: `);
    pads.forEach((pad, i) => {
      if (i) div.append(' · ');
      if (pad === name) {
        const b = document.createElement('b');
        b.textContent = pad;
        div.append(b);
      } else {
        div.append(pad);
      }
    });
    td.append(div);
  });
  return td;
}

let sigResults = {};
try {
  sigResults = JSON.parse(localStorage.getItem(SIG_KEY) || '{}') || {};
} catch (e) { /* unreadable: start empty */ }

function sigInput(key, field, type) {
  const el = document.createElement(type === 'beep' ? 'select' : 'input');
  if (type === 'beep') {
    ['', 'yes', 'no'].forEach((v) => {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = v || '–';
      el.append(o);
    });
  } else {
    el.type = type;
    if (type === 'number') el.step = 'any';
  }
  el.value = (sigResults[key] || {})[field] || '';
  el.addEventListener('input', () => {
    sigResults[key] = Object.assign(sigResults[key] || {}, { [field]: el.value.trim() });
    try {
      localStorage.setItem(SIG_KEY, JSON.stringify(sigResults));
    } catch (e) { /* storage full or blocked: keep it in memory */ }
  });
  const td = document.createElement('td');
  td.append(el);
  return td;
}

function cell(textValue) {
  const td = document.createElement('td');
  td.textContent = textValue;
  return td;
}

function signalLayout() {
  return $('sig-layout').value === 'pogo' ? 'pogo' : 'legs';
}

function headRow(names) {
  const tr = document.createElement('tr');
  names.forEach((name) => {
    const th = document.createElement('th');
    th.textContent = name;
    tr.append(th);
  });
  return tr;
}

function buildSignalTables() {
  const legs = signalLayout() === 'legs';
  document.querySelectorAll('.sig-legs').forEach((el) => el.classList.toggle('hidden', !legs));
  document.querySelectorAll('.sig-pogo').forEach((el) => el.classList.toggle('hidden', legs));
  const head = $('sig-head');
  head.textContent = '';
  head.append(headRow(legs
    ? ['Signal', '', 'Expect', 'Expected leg (unverified)', 'Leg that reads it', 'Reading (V)']
    : ['Signal', '', 'Expect at its joint', 'Cartridge pad (contact side, Sprite_tm\'s photo)', 'Joint / pogo found',
      'Reading (V)']));
  const rows = $('sig-rows');
  rows.textContent = '';
  for (const line of SIG_LINES) {
    const tr = document.createElement('tr');
    tr.dataset.line = line;
    const hold = document.createElement('button');
    hold.type = 'button';
    hold.className = 'sig-high';
    hold.textContent = 'High';
    hold.addEventListener('click', () => gcode(`POLYDYE_LEVEL LINE=${line} STATE=high`));
    const holdTd = document.createElement('td');
    holdTd.append(hold);
    if (legs) {
      tr.append(cell(line), holdTd, cell(expectText(line)), cell(CHIP_LEGS[line]), sigInput(line, 'leg', 'text'),
        sigInput(line, 'leg_volts', 'number'));
    } else {
      tr.append(cell(line), holdTd, cell(expectText(line)), padCell(line), sigInput(line, 'joint', 'text'),
        sigInput(line, 'volts', 'number'));
    }
    rows.append(tr);
  }
  const power = $('sig-power-rows');
  power.textContent = '';
  for (const [key, pad, from] of SIG_POWER) {
    const tr = document.createElement('tr');
    tr.append(cell(key), cell(from), padCell(pad), sigInput(key, 'joint', 'text'), sigInput(key, 'beep', 'beep'));
    power.append(tr);
  }
}

function signalReport() {
  const pad = (s, n) => String(s).padEnd(n);
  const lines = [
    `PolyDye signal check, ${new Date().toISOString()}, board firmware ${boardVersion() || 'unknown'}`,
    'Cartridge out; black lead on CD4504 pin 8. One line held HIGH at a time.',
    '',
    'At the CD4504 output legs (expected legs from the C1/C2 schematic draft, unverified):',
    `${pad('Signal', 10)}${pad('Expect', 12)}${pad('Expected', 12)}${pad('Found', 14)}Reading (V)`,
  ];
  for (const line of SIG_LINES) {
    const r = sigResults[line] || {};
    lines.push(`${pad(line, 10)}${pad(LOW_RAIL.includes(line) ? '~7.6 V' : '~12.6 V', 12)}${pad(CHIP_LEGS[line], 12)}`
      + `${pad(r.leg || '-', 14)}${r.leg_volts || '-'}`);
  }
  lines.push('', 'At the pogo pins\' solder joints (interface board out of the holder, face down, FFC connected):',
    `${pad('Signal', 10)}${pad('Expect', 12)}${pad('Joint / pogo', 22)}Reading (V)`);
  for (const line of SIG_LINES) {
    const r = sigResults[line] || {};
    lines.push(`${pad(line, 10)}${pad(LOW_RAIL.includes(line) ? '~7.6 V' : '~12.6 V', 12)}${pad(r.joint || '-', 22)}`
      + `${r.volts || '-'}`);
  }
  lines.push('', `${pad('Signal', 10)}${pad('Beep from', 34)}${pad('Joint / pogo', 22)}Beeps`);
  for (const [key, , from] of SIG_POWER) {
    const r = sigResults[key] || {};
    lines.push(`${pad(key, 10)}${pad(from, 34)}${pad(r.joint || '-', 22)}${r.beep || '-'}`);
  }
  lines.push('', 'Cartridge pads (contact side, Sprite_tm\'s photo):');
  PAD_SIDES.forEach(([side, pads]) => lines.push(`  ${side}: ${pads.join(' ')}`));
  return `${lines.join('\n')}\n`;
}

$('sig-save').addEventListener('click', async () => {
  try {
    const body = new FormData();
    body.append('root', 'config');
    body.append('file', new Blob([signalReport()], { type: 'text/plain' }), 'polydye_signal_check.txt');
    const r = await fetch('/server/files/upload', { method: 'POST', body });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    log('saved polydye_signal_check.txt in the config folder');
  } catch (e) {
    log(`! save failed: ${e.message}`);
  }
});

$('sig-clear').addEventListener('click', () => {
  if (!window.confirm('Clear the signal check results in this browser?')) return;
  sigResults = {};
  try {
    localStorage.removeItem(SIG_KEY);
  } catch (e) { /* nothing stored */ }
  buildSignalTables();
  render();
});

$('sig-cart-out').addEventListener('change', () => render());
try {
  $('sig-layout').value = localStorage.getItem(SIG_LAYOUT_KEY) === 'pogo' ? 'pogo' : 'legs';
} catch (e) { /* storage blocked: the default */ }
$('sig-layout').addEventListener('change', () => {
  try {
    localStorage.setItem(SIG_LAYOUT_KEY, signalLayout());
  } catch (e) { /* storage blocked: only for this visit */ }
  buildSignalTables();
  render();
});
$('sig-start').addEventListener('click', () => gcode('POLYDYE_ENABLE LIVE=0'));
$('sig-low').addEventListener('click', () => gcode('POLYDYE_LEVEL STATE=off'));
$('sig-finish').addEventListener('click', () => gcode('POLYDYE_LEVEL STATE=off\nPOLYDYE_DISABLE'));

function fireValues() {
  const f = $('fire-form').elements;
  const num = (name, fallback) => {
    const v = parseFloat(f[name].value);
    return Number.isFinite(v) ? v : fallback;
  };
  const stripes = f.stripes.value.replace(/\s+/g, '').split(',').filter((s) => s);
  // TEMPLATE:PULSE; a :stock clock (from before the stock-only cleanup) is still taken.
  const parsed = stripes.map((s) => s.match(/^([a-z_]+):(\d+)(:stock)?$/i));
  const oldTemplates = [...new Set(parsed.map((m) => m && m[1].toLowerCase())
    .filter((t) => TEMPLATES.includes(t) && state.board && !templateOk(t)))];
  return {
    x: num('x', 70), y: num('y', 60), z: num('z', 0.5), spacing: num('spacing', 18), stripes,
    pattern: f.pattern.value,
    valid: stripes.length > 0 && !oldTemplates.length
      && parsed.every((m) => m && TEMPLATES.includes(m[1].toLowerCase()) && pulseOk(Number(m[2]))),
    oldTemplates,
  };
}

function renderFireLegend(axis) {
  const v = fireValues();
  const table = $('fire-legend');
  table.textContent = '';
  if (!v.valid) {
    const tr = document.createElement('tr');
    tr.append(cell(`Each stripe is template:pulse, e.g. color_b_late:8 or black_a:6, with a template of `
      + `${TEMPLATES.join(', ')} and a pulse of ${PULSE_MIN_WORDS}-${PULSE_MAX_WORDS} words `
      + `(${pulseUs(PULSE_MIN_WORDS).toFixed(2)}-${pulseUs(PULSE_MAX_WORDS).toFixed(2)} µs).`
      + `${v.oldTemplates.map((t) => ` ${templateOldText(t)}`).join('')}`));
    table.append(tr);
    return;
  }
  v.stripes.forEach((s, i) => {
    const [tpl, words] = s.toLowerCase().split(':');
    const x = axis === 'x' ? v.x : v.x + i * v.spacing;
    const y = axis === 'x' ? v.y + i * v.spacing : v.y;
    const tr = document.createElement('tr');
    tr.append(cell(`${i + 1}`), cell(`X ${fmtMm(x)}  Y ${fmtMm(y)}`), cell(tpl),
      cell(`pulse ${words} words (${pulseUs(Number(words)).toFixed(2)} µs)`));
    table.append(tr);
  });
}

$('fire-form').addEventListener('input', () => renderFireLegend((state.printer.polydye || {}).pass_axis));
$('fire-form').addEventListener('change', () => render());
$('fire-form').addEventListener('submit', (ev) => ev.preventDefault());
$('spit-form').addEventListener('submit', (ev) => ev.preventDefault());
$('spit-form').addEventListener('change', () => render());

$('fire-btn').addEventListener('click', () => {
  const v = fireValues();
  if (!v.valid) return;
  const knobs = v.pattern === 'stock' ? knobParams($('fire-form').elements) : { text: '' };
  const warm = warmParams($('fire-form').elements);
  if (knobs.error || warm.error) {
    log(`! fire test: ${knobs.error || warm.error}`);
    return;
  }
  const ok = window.confirm(`Print the fire test with LIVE INK?\n\nPaper taped on the bed, cartridge fitted. It homes `
    + `if needed, lowers the nozzle to Z ${v.z} mm and prints ${v.stripes.length} stripes, the first at X ${v.x}, `
    + `Y ${v.y}${warm.seconds ? `, each after a ${warm.seconds} s head warm-up` : ''}. The template and pulse go back `
    + 'to what they were afterwards.');
  if (!ok) return;
  gcode(`PD_FIRE_TEST X=${v.x} Y=${v.y} Z=${v.z} SPACING=${v.spacing} PATTERN=${v.pattern}${knobs.text}${warm.text} `
    + `STRIPES=${v.stripes.join(',')}`);
});

$('spit-btn').addEventListener('click', () => {
  const f = $('spit-form').elements;
  const seconds = Math.min(10, Math.max(0.5, parseFloat(f.seconds.value) || 2));
  const pulse = parseInt(f.pulse.value, 10);
  const pd = state.printer.polydye || {};
  const stock = f.pattern.value === 'stock';
  // PATTERN=stock fires 6 words (2.10 µs, stock's) unless another pulse is picked.
  const words = pulse >= 1 ? pulse : (stock ? PULSE_MIN_WORDS : pd.pulse_words);
  const periodText = f.period.value.trim();
  const period = periodText ? Number(periodText) : 0;
  // A number field reports text it can't parse as an empty value; badInput tells that apart from an empty field.
  const badPeriod = !!(f.period.validity && f.period.validity.badInput);
  // Only whole µs in the board's range; an empty field is the default (1000 µs).
  if (badPeriod || (periodText && !(/^\d+$/.test(periodText) && period >= 1 && period <= 65535))) {
    log('! spit test: the column period must be a whole number of µs from 1 to 65535 (or empty for 1000)'
      + `${periodText ? `, not "${periodText}"` : ''}`);
    return;
  }
  if (words && !pulseOk(words)) {
    log(`! spit test: ${pulseRangeText(words)}${pulse >= 1 ? '' : ': enter a fire pulse'}`);
    return;
  }
  const knobs = stock ? knobParams(f) : { text: '' };
  const warm = warmParams(f);
  if (knobs.error || warm.error) {
    log(`! spit test: ${knobs.error || warm.error}`);
    return;
  }
  const warmS = warm.seconds;
  const warmParamText = warm.text;
  // The stock pattern's default period is the stock firmware's, 508 µs; the others' 1000 µs.
  const every = period > 0 ? period : (stock ? 508 : 0);
  const each = every > 0 ? `a column every ${every} µs` : `${SPIT_HZ} columns a second`;
  const rate = every > 0 ? `every ${every} µs for ${seconds} s` : `${SPIT_HZ} times a second for ${seconds} s`;
  let which = `Every nozzle fires ${rate}.`;
  if (stock) {
    which = knobs.text ? `The stock firmware's random pattern with${knobs.text} fires ${each} for ${seconds} s.`
      : `A random quarter of positions 14-85 fires in each column, each column twice, like the stock firmware, `
      + `${each} for ${seconds} s.`;
  }
  const first = warmS > 0 ? `First a ${warmS} s warm-up fires every nozzle with `
    + `${warm.pulse ? `${warm.pulse}-word pulses` : 'short pulses'}, then, in the same run: ` : '';
  const ok = window.confirm(`Spit test: LIVE INK, no motion.\n\nHold a folded paper towel under the cartridge, `
    + `against the nozzles. ${first}${which}`);
  if (!ok) return;
  gcode(`POLYDYE_SPIT SECONDS=${seconds}${f.template.value ? ` TEMPLATE=${f.template.value}` : ''}`
    + `${pulse >= 1 ? ` PULSE=${pulse}` : ''} PATTERN=${f.pattern.value}${knobs.text}`
    + `${period > 0 ? ` PERIOD_US=${period}` : ''}${warmParamText} CONFIRM=1`);
});

// ---- calibration prints: the alignment page and the offset print (README, "Calibration prints") ------------------

// ` NAME=value` for each filled-in number field of `form` ([field, NAME] pairs), or an error.
function numParams(form, fields) {
  const out = [];
  for (const [name, param] of fields) {
    const el = form.elements[name];
    const raw = el.value.trim();
    if ((el.validity && el.validity.badInput) || (raw && !Number.isFinite(Number(raw)))) {
      return { error: `${el.parentElement.firstChild.textContent.trim()} must be a number` };
    }
    if (raw) out.push(`${param}=${Number(raw)}`);
  }
  return { text: out.map((p) => ` ${p}`).join(''), count: out.length };
}

// A command that changes the live values by readings and writes polydye.cfg itself (POLYDYE_ALIGN, POLYDYE_OFFSET
// X_TICK=): after the nudges and the Save/Store clicked before it (the readings correct what was printed), and before
// nudges clicked after it; it waits only on earlier clicks. The readings add to the values, so they must go through
// once: the button `key` takes no more clicks until it's done. Returns true when Klipper applied them (clear the
// readings), false when it refused them, and also true when the connection dropped while it ran: they may have been
// applied, and entering them again could apply them twice.
const storing = new Set();
function queueStoring(key, script) {
  if (storing.has(key)) return Promise.resolve(false);
  storing.add(key);
  const nudgesBefore = nudgeChain;
  const run = queueCfg(async () => {
    await nudgesBefore;
    log(`> ${script}`);
    try {
      await rpc('printer.gcode.script', { script });
    } catch (e) {
      log(`! ${e.message}`);
      if (e.message !== 'connection closed') return false;
      log('! the connection dropped while it ran, so it may have been applied: check the values under Ink position '
        + 'before entering these readings again');
      return true;
    }
    // Klipper has applied and stored it: a failed re-read mustn't look like a failed command.
    await refreshStored().catch((e) => log(`! could not read polydye.cfg: ${e.message}`));
    return true;
  });
  nudgeChain = nudgeChain.then(() => run).catch(() => {});  // logged below
  return run.catch((e) => {
    log(`! ${e.message}`);
    return false;
  }).finally(() => storing.delete(key));
}

['align-form', 'align-read', 'offset-form', 'offset-read'].forEach((id) => {
  $(id).addEventListener('submit', (ev) => ev.preventDefault());
});

$('align-btn').addEventListener('click', () => {
  const p = numParams($('align-form'), [['x', 'X'], ['y', 'Y'], ['z', 'Z'], ['spacing', 'SPACING']]);
  if (p.error) {
    log(`! alignment page: ${p.error}`);
    return;
  }
  const ok = window.confirm('Print the alignment page with LIVE INK, on paper only?\n\nPaper taped over the glass '
    + 'where the page goes, at least 70 x 140 mm, so no ink lands on the glass (the console says exactly where); '
    + 'cartridge fitted. It homes if needed, warms the head over the wipe sponge and prints 8 strips along the pass '
    + 'axis.');
  if (ok) gcode(`PD_ALIGN_PRINT${p.text}`);
});

$('align-apply').addEventListener('click', async () => {
  const p = numParams($('align-read'), [['cyan', 'CYAN'], ['yellow', 'YELLOW'], ['direction', 'DIRECTION'],
    ['cyan_x', 'CYAN_X'], ['yellow_x', 'YELLOW_X']]);
  if (p.error || !p.count) {
    log(`! alignment: ${p.error || 'enter the numbers read off the page'}`);
    return;
  }
  if (await queueStoring('align', `POLYDYE_ALIGN${p.text}`)) $('align-read').reset();
});

$('offset-btn').addEventListener('click', () => {
  const p = numParams($('offset-form'), [['x', 'X'], ['y', 'Y'], ['layers', 'LAYERS'], ['gap', 'GAP'],
    ['extruder_temp', 'EXTRUDER_TEMP'], ['bed_temp', 'BED_TEMP']]);
  if (p.error) {
    log(`! offset print: ${p.error}`);
    return;
  }
  const ok = window.confirm('Print the offset check: a PLA plaque, then LIVE INK on it?\n\nCartridge fitted, PLA '
    + 'loaded, bed clean. It homes if needed, heats, prints a 26 x 38 mm plaque, turns the hotend off, warms the head '
    + 'over the wipe sponge and inks two scales across the groove. All the ink lands on the plaque.');
  if (ok) gcode(`PD_OFFSET_PRINT${p.text}`);
});

$('offset-apply').addEventListener('click', async () => {
  const p = numParams($('offset-read'), [['x_tick', 'X_TICK'], ['y_tick', 'Y_TICK']]);
  if (p.error || !p.count) {
    log(`! offset: ${p.error || 'enter the ticks read off the scales'}`);
    return;
  }
  if (await queueStoring('offset', `POLYDYE_OFFSET${p.text}`)) $('offset-read').reset();
});

function renderDiagnostics(pd, printing) {
  const known = state.board && pd.passes !== undefined;
  const diag = known && 'level' in pd;  // board firmware with the signal check and the spit test
  const busy = printing || state.fw.updating || !!pd.running || !!pd.queued;
  $('diag-old').classList.toggle('hidden', !known || diag);
  $('fire-btn').disabled = !known || !pd.cartridge_template || busy || !fireValues().valid
    // A warm-up per stripe came with the spit test's stock pattern and column period.
    || (parseFloat($('fire-form').elements.warm.value) > 0 && !(pd.spit_patterns || []).includes('stock'));
  // Board firmware without the stock clock has no color_b_late (the stock pattern's default template).
  const spit = $('spit-form').elements;
  const spitTemplate = spit.template.value || (spit.pattern.value === 'stock' ? 'color_b_late' : '');
  const templateOld = diag && !!spitTemplate && !templateOk(spitTemplate);
  // The stock pattern and the column period came together.
  const stockOld = diag && (spit.pattern.value === 'stock' || spit.period.value.trim() !== '')
    && !(pd.spit_patterns || []).includes('stock');
  // Then the warm-up's longer pulses and the stock pattern's knobs (the fire test takes the knobs on any firmware).
  const randomOld = diag && !stockOld && spit.pattern.value === 'stock' && knobsGiven(spit) && !pd.spit_random;
  const warmWords = parseInt(spit.warm_pulse.value, 10);
  const warmOld = diag && !stockOld && parseFloat(spit.warm.value) > 0 && !pd.warm_duty_ppm
    && ((warmWords > 0 && pulseUs(warmWords) > WARM_OLD_US + 1e-9)
      || parseFloat(spit.warm.value) > (pd.warm_max_s || WARM_OLD_S) + 1e-9);
  showKnobs();
  const oldText = [];
  if (templateOld) oldText.push(templateOldText(spitTemplate));
  if (stockOld) {
    oldText.push('The stock-emulation spit test and the column period need newer board firmware: Update to bundled '
      + 'firmware under Board firmware.');
  }
  if (randomOld) {
    oldText.push("The stock pattern's knobs need newer board firmware for the spit test: Update to bundled firmware "
      + 'under Board firmware. The fire test takes them with this one.');
  }
  if (warmOld) {
    oldText.push(`Warm-up pulses longer than ${WARM_OLD_US} µs and warm-ups longer than ${WARM_OLD_S} s need newer `
      + 'board firmware: Update to bundled firmware under Board firmware.');
  }
  $('spit-fw-old').textContent = oldText.join(' ');
  $('spit-fw-old').classList.toggle('hidden', !oldText.length);
  $('spit-btn').disabled = !diag || busy || templateOld || stockOld || randomOld || warmOld;
  const cartOut = $('sig-cart-out').checked;
  const level = pd.level || null;
  $('sig-start').disabled = !diag || busy || !cartOut || !!pd.live;
  $('sig-low').disabled = !diag || !level;
  $('sig-finish').disabled = !diag;
  const canHold = diag && cartOut && pd.boost && !pd.live && !busy;
  document.querySelectorAll('#sig-rows tr').forEach((tr) => {
    tr.classList.toggle('held', !!level && level.line === tr.dataset.line);
    tr.querySelector('.sig-high').disabled = !canHold;
  });
  let status = '';
  if (diag) {
    if (level) status = `${level.line} held ${level.state.toUpperCase()}, ${level.remaining} s left`;
    else if (pd.live) status = 'live ink is on: turn it off first';
    else if (pd.boost) status = 'boost on, every line LOW';
    else status = cartOut ? 'press Start' : 'take the cartridge out first';
  }
  text('sig-state', status, level ? 'on' : '');
  renderFireLegend(pd.pass_axis);
}

buildSignalTables();

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

// The plug-in toggle (scripts/polydye-toggle.sh) only runs when the Klipper service starts.
$('enable-btn').addEventListener('click', async () => {
  if (['printing', 'paused'].includes((state.printer.print_stats || {}).state)) return;
  $('enable-btn').classList.add('hidden');
  log('> restart the Klipper service to enable PolyDye');
  try {
    await rpc('machine.services.restart', { service: state.fw.klipperService || 'klipper' });
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
