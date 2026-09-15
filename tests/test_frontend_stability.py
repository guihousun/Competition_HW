"""Frontend interaction stability: flicker, input preservation and async steps.

The browser modules run in a bare JS context with a small DOM stub, so the
regressions that only show up while the match is playing are covered by
``python -m unittest`` alongside the Python tests:

  * repeated identical canvas sizes must not clear the bitmap,
  * crew buttons keep their node identity (focus survives a frame) and their
    handlers resolve the actor that is current *now*,
  * one routine round is settled at a time, and pausing during a pending settle
    neither resumes playback nor leaves the transport locked,
  * a response that belongs to a replaced scene is dropped, including its
    ``finally`` handler,
  * typed seek input survives blur/change and only Jump/Enter/Escape or a new
    scene end the draft,
  * pending-LLM and error paths keep reporting and stay usable.

The DOM stub is deliberately minimal: it models node identity, parent/child
links, classList, textContent, value, disabled and event dispatch, which is what
these behaviours are defined in terms of. No browser or network is used.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "js"
NODE = shutil.which("node")

# Ordered exactly like index.html.
MODULES = ["constants.js", "viewmodel.js", "replay.js", "sprites.js", "effects.js",
           "renderer.js", "panel.js", "experience.js", "guide.js", "app.js"]

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const dir = %(dir)s;
const noop = () => {};

/* ---- minimal 2D context ------------------------------------------------ */
const ctx2d = new Proxy({}, {
  get: (target, prop) => {
    if (prop === 'canvas') return { width: 900, height: 600 };
    if (prop === 'getImageData') return () => ({ data: [0, 0, 0, 255] });
    if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
      return () => ({ addColorStop: noop });
    }
    if (prop === 'measureText') return () => ({ width: 10 });
    return noop;
  },
  set: () => true,
});

/* ---- minimal DOM ------------------------------------------------------- */
const TAG_BY_ID = {
  map: 'canvas', 'input-json': 'textarea', 'output-json': 'pre',
  'seek-frame': 'input', scrub: 'input', round: 'input', seed: 'input',
  'twomatch-seed': 'input', 'twomatch-rounds': 'input', 'replay-file': 'input', file: 'input',
  'recording-meter': 'progress', dev: 'section', 'map-guide': 'dialog',
};
const IDS = new Map();

function detach(node) {
  if (!node || !node.parentNode) return;
  const siblings = node.parentNode.children;
  const at = siblings.indexOf(node);
  if (at >= 0) siblings.splice(at, 1);
  node.parentNode = null;
}

function makeElement(tag, id) {
  const node = {
    tagName: String(tag || 'div').toUpperCase(),
    id: id || '',
    children: [],
    parentNode: null,
    attrs: {},
    listeners: {},
    dataset: {},
    style: {},
    value: '',
    disabled: false,
    hidden: false,
    checked: false,
    open: false,
    files: [],
    title: '',
    width: 300,
    height: 150,
    scrollTop: 0,
    scrollHeight: 0,
    type: '',
  };
  let classes = new Set();
  let className = '';
  let text = '';
  const syncClass = () => { className = Array.from(classes).join(' '); };
  Object.defineProperty(node, 'className', {
    get: () => className,
    set: (value) => { className = String(value || ''); classes = new Set(className.split(/\s+/).filter(Boolean)); },
    configurable: true,
  });
  Object.defineProperty(node, 'textContent', {
    get: () => (node.children.length ? node.children.map((child) => child.textContent).join('') : text),
    set: (value) => { node.children.slice().forEach(detach); node.children.length = 0; text = String(value); },
    configurable: true,
  });
  node.classList = {
    add: (...names) => { names.forEach((name) => classes.add(name)); syncClass(); },
    remove: (...names) => { names.forEach((name) => classes.delete(name)); syncClass(); },
    toggle: (name, force) => {
      const on = force === undefined ? !classes.has(name) : Boolean(force);
      if (on) classes.add(name); else classes.delete(name);
      syncClass();
      return on;
    },
    contains: (name) => classes.has(name),
  };
  node.setAttribute = (name, value) => { node.attrs[name] = String(value); };
  node.getAttribute = (name) => (Object.prototype.hasOwnProperty.call(node.attrs, name) ? node.attrs[name] : null);
  node.removeAttribute = (name) => { delete node.attrs[name]; };
  node.appendChild = (child) => { detach(child); child.parentNode = node; node.children.push(child); return child; };
  node.append = (...items) => { items.forEach((item) => node.appendChild(item)); };
  node.insertBefore = (child, ref) => {
    detach(child);
    const at = ref ? node.children.indexOf(ref) : -1;
    if (at < 0) node.children.push(child); else node.children.splice(at, 0, child);
    child.parentNode = node;
    return child;
  };
  node.remove = () => detach(node);
  node.replaceChildren = (...items) => {
    node.children.slice().forEach(detach);
    node.children.length = 0;
    text = '';
    items.forEach((item) => node.appendChild(item));
  };
  node.addEventListener = (type, handler) => { (node.listeners[type] = node.listeners[type] || []).push(handler); };
  node.removeEventListener = (type, handler) => {
    node.listeners[type] = (node.listeners[type] || []).filter((item) => item !== handler);
  };
  node.dispatchEvent = (event) => {
    const payload = Object.assign({ type: '', target: node, preventDefault: noop, stopPropagation: noop }, event);
    (node.listeners[payload.type] || []).slice().forEach((handler) => handler(payload));
    return true;
  };
  node.matches = (selector) => selector.split(',').some((part) => {
    const name = part.trim().toLowerCase();
    if (!name) return false;
    if (name.startsWith('.')) return classes.has(name.slice(1));
    return name === node.tagName.toLowerCase();
  });
  node.closest = (selector) => {
    let cursor = node;
    while (cursor) { if (cursor.matches && cursor.matches(selector)) return cursor; cursor = cursor.parentNode; }
    return null;
  };
  node.contains = (other) => {
    let cursor = other;
    while (cursor) { if (cursor === node) return true; cursor = cursor.parentNode; }
    return false;
  };
  node.querySelector = () => null;
  node.querySelectorAll = () => [];
  node.getBoundingClientRect = () => ({ width: 900, height: 600, left: 0, top: 0, right: 900, bottom: 600 });
  node.getContext = () => ctx2d;
  node.setPointerCapture = noop;
  node.releasePointerCapture = noop;
  node.focus = () => { document.activeElement = node; };
  node.blur = () => { if (document.activeElement === node) document.activeElement = null; };
  node.click = () => node.dispatchEvent({ type: 'click' });
  node.scrollIntoView = noop;
  node.showModal = () => { node.open = true; };
  node.close = () => { node.open = false; };
  return node;
}

const docListeners = {};
const document = {
  activeElement: null,
  body: makeElement('body'),
  documentElement: makeElement('html'),
  getElementById: (id) => {
    if (!IDS.has(id)) IDS.set(id, makeElement(TAG_BY_ID[id] || 'div', id));
    return IDS.get(id);
  },
  createElement: (tag) => makeElement(tag),
  querySelector: () => makeElement('div'),
  querySelectorAll: () => [],
  addEventListener: (type, handler) => { (docListeners[type] = docListeners[type] || []).push(handler); },
  dispatchEvent: (event) => { (docListeners[event.type] || []).slice().forEach((handler) => handler(event)); },
};

const winListeners = {};
let clock = 1000;
const timers = new Map();
let timerSeq = 0;

const sandbox = {
  console,
  performance: { now: () => (clock += 0.5) },
  requestAnimationFrame: noop,
  cancelAnimationFrame: noop,
  devicePixelRatio: 1,
  innerHeight: 900,
  innerWidth: 1440,
  setTimeout: (fn, ms) => { timerSeq += 1; timers.set(timerSeq, { fn, ms }); return timerSeq; },
  clearTimeout: (id) => { timers.delete(id); },
  setInterval: () => 0,
  clearInterval: noop,
  document,
  addEventListener: (type, handler) => { (winListeners[type] = winListeners[type] || []).push(handler); },
  dispatchEvent: (event) => { (winListeners[event.type] || []).slice().forEach((handler) => handler(event)); },
  ResizeObserver: class { constructor(cb) { this.cb = cb; } observe() {} unobserve() {} disconnect() {} },
  AbortSignal: { timeout: () => ({ aborted: false }) },
  sessionStorage: { getItem: () => null, setItem: noop, removeItem: noop },
  fetch: () => Promise.reject(new Error('fetch is not stubbed in this harness')),
  URL: { createObjectURL: () => 'blob:local', revokeObjectURL: noop },
  Blob: class { constructor(parts) { this.parts = parts; } },
  TextEncoder: class { encode() { return new Uint8Array(0); } },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
for (const name of %(modules)s) {
  vm.runInContext(fs.readFileSync(path.join(dir, name), 'utf8'), sandbox, { filename: name });
}

const HW = sandbox.HW;
const checks = [];
const check = async (name, fn) => {
  try { await fn(); checks.push({ name, ok: true }); }
  catch (error) { checks.push({ name, ok: false, error: String((error && error.message) || error) }); }
};
const assert = (cond, message) => { if (!cond) throw new Error(message || 'assertion failed'); };

/* ---- fixtures ---------------------------------------------------------- */
function rolesAt(positions) {
  const spot = (id, fallback) => (positions && positions[id]) || fallback;
  return [
    { id: 1, roleType: 'station', pos: { x: 3, y: 3 }, health: 1500, level: 1 },
    { id: 101, roleType: 'worker', pos: spot(101, { x: 5, y: 5 }), health: 220, level: 1 },
    { id: 102, roleType: 'pioneer', pos: spot(102, { x: 6, y: 5 }), health: 200, level: 1 },
  ];
}
function stateAt(roundNo, positions) {
  return {
    roundNo,
    mapInfo: { width: 41, height: 32, zones: [] },
    teamOur: { type: 'challenger', goldNum: 75, totalScore: 10, roles: rolesAt(positions) },
    teamEnemy: { type: 'defender', roles: [] },
    robot: { roles: [] },
    _demo: { seed: 1, pressure: 1, kills: 0 },
  };
}
function liveWorld(roundNo, positions) {
  return HW.World.fromScenario({ state: stateAt(roundNo || 1, positions), metadata: {} });
}
function stepResponse(roundNo, options) {
  const opts = options || {};
  return {
    state: stateAt(roundNo, opts.positions),
    frame: Object.assign({ round: roundNo, actions: [], executed: {}, skipped: [] }, opts.frame || {}),
    roleCommandMap: opts.commands || {},
    executed: opts.executed || {},
    done: Boolean(opts.done),
    pending: Boolean(opts.pending),
  };
}
function worldWithFrames(count) {
  const world = liveWorld(1);
  for (let i = 1; i <= count; i += 1) {
    world.pushStep(stepResponse(i + 1, i === 1
      ? { frame: { skipped: ['101'], spawned: [{ kind: 'smallRobot', robot: 900, pos: { x: 1, y: 1 } }] } }
      : {}));
  }
  world.applyFrame(0, { instant: true });
  return world;
}

/* ---- one app for the whole harness ------------------------------------- */
const app = new HW.App();
app._toasts = [];
app.panel.toast = (message, kind) => { app._toasts.push({ message: String(message), kind: kind || null }); };
HW.app = app;
app.bind();

function resetApp() {
  app.world = null;
  app.playing = false;
  app.busy = false;
  app.stepping = false;
  app.stepToken = null;
  app.generation = 0;
  app.seekDraft = false;
  app.scrubActive = false;
  app.waitingLLM = false;
  app.previewCommands = {};
  app.markers = [];
  app.markerWorld = undefined;
  app.panel.busy = false;
  app.panel.stepPending = false;
  app.panel.requestDirty = false;
  app.panel._twoMatchRendered = undefined;
  app._toasts.length = 0;
  app.effects.clear();
  document.activeElement = null;
  document.getElementById('input-json').value = '';
  document.getElementById('seek-frame').value = '';
  document.getElementById('scrub').value = '';
  // Select defaults as they appear in index.html: the DOM stub starts empty.
  document.getElementById('event-kind').value = 'all';
  document.getElementById('speed').value = '550';
  document.getElementById('side').value = 'challenger';
  document.getElementById('pressure').value = '1';
  document.getElementById('seed').value = '1';
  document.getElementById('crew-list').replaceChildren();
}
function stubPost() {
  const calls = [];
  const waiting = [];
  app.postJson = (url) => {
    calls.push(url);
    return new Promise((resolve, reject) => waiting.push({ resolve, reject }));
  };
  return {
    calls,
    get count() { return calls.length; },
    resolve: (index, value) => waiting[index].resolve(value),
    reject: (index, error) => waiting[index].reject(error),
  };
}

(async () => {
  /* ---- canvas sizing --------------------------------------------------- */
  await check('identical canvas sizes are a no-op', () => {
    resetApp();
    const canvas = document.getElementById('map');
    let writes = 0;
    let backing = { width: canvas.width, height: canvas.height };
    Object.defineProperty(canvas, 'width', {
      get: () => backing.width, set: (value) => { writes += 1; backing.width = value; }, configurable: true,
    });
    Object.defineProperty(canvas, 'height', {
      get: () => backing.height, set: (value) => { writes += 1; backing.height = value; }, configurable: true,
    });
    assert(app.renderer.setSize(800, 600) === true, 'the first size must be applied');
    const after = writes;
    assert(app.renderer.setSize(800, 600) === false, 'an identical size must report no change');
    assert(writes === after, 'an identical size must not write canvas.width/height');
    assert(app.renderer.setSize(801, 600) === true, 'a real change must resize');
    assert(writes > after, 'a real change must write canvas.width/height');
  });

  await check('resize ticks with the same box never clear the canvas', () => {
    resetApp();
    app.world = liveWorld(1);
    app.fitMode = true;
    document.getElementById('stage-canvas').getBoundingClientRect =
      () => ({ width: 1000, height: 620, left: 0, top: 0, right: 1000, bottom: 620 });
    app.resize();
    const canvas = document.getElementById('map');
    let writes = 0;
    let width = canvas.width;
    Object.defineProperty(canvas, 'width', {
      get: () => width, set: (value) => { writes += 1; width = value; }, configurable: true,
    });
    app.resize();
    app.resize();
    assert(writes === 0, 'the ResizeObserver may fire freely; the canvas must stay untouched');
  });

  await check('console scrolling and debug resizing leave the canvas alone', () => {
    resetApp();
    app.world = liveWorld(1);
    document.getElementById('stage-canvas').getBoundingClientRect =
      () => ({ width: 1000, height: 620, left: 0, top: 0, right: 1000, bottom: 620 });
    app.resize();
    const canvas = document.getElementById('map');
    let writes = 0;
    let width = canvas.width;
    Object.defineProperty(canvas, 'width', {
      get: () => width, set: (value) => { writes += 1; width = value; }, configurable: true,
    });
    const consoleNode = document.getElementById('console');
    consoleNode.scrollTop = 420;
    consoleNode.scrollHeight = 2400;
    app.resize();
    document.getElementById('dev').style.height = '520px';
    app.resize();
    assert(writes === 0, 'console-only layout changes must not resize the map');
  });

  /* ---- crew list ------------------------------------------------------- */
  await check('crew buttons keep node identity and resolve the current actor', () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.panel.updateHud(world, app.statusInfo());
    const crew = document.getElementById('crew-list');
    assert(crew.children.length === 2, 'the worker and the pioneer are listed');
    const button = crew.children[0];
    const label = button.getAttribute('aria-label');
    assert(label && label.indexOf('101') >= 0, 'the first button locates the worker');
    button.focus();

    world.pushStep(stepResponse(2, { positions: { 101: { x: 6, y: 5 } } }));
    app.panel.updateHud(world, app.statusInfo());
    assert(crew.children[0] === button, 'the same node must still be in the list');
    assert(crew.contains(document.activeElement), 'focus must survive a frame update');
    const current = world.actors.find((actor) => actor.key === 'unit:own:101');
    assert(current && current.pos.x === 6, 'the match really moved the worker');
    button.dispatchEvent({ type: 'click' });
    assert(app.selected === current, 'the handler must resolve the actor that is current now');
    assert(app.selected.pos.x === 6, 'a captured old actor object would still be at x=5');
  });

  await check('crew buttons disappear when their actor leaves the match', () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.panel.updateHud(world, app.statusInfo());
    const crew = document.getElementById('crew-list');
    const pioneer = crew.children[1];
    const next = stepResponse(2, {});
    next.state.teamOur.roles = next.state.teamOur.roles.filter((role) => role.id !== 102);
    world.pushStep(next);
    app.panel.updateHud(world, app.statusInfo());
    assert(crew.children.length === 1, 'the dead pioneer is removed from the list');
    assert(pioneer.parentNode === null, 'the removed button is detached, not duplicated');
  });

  await check('repeated identical HUD updates do not rebuild crew nodes', () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.panel.updateHud(world, app.statusInfo());
    const crew = document.getElementById('crew-list');
    const before = crew.children.slice();
    for (let i = 0; i < 5; i += 1) app.panel.updateHud(world, app.statusInfo());
    assert(crew.children.length === before.length, 'the list length is stable');
    assert(crew.children.every((node, index) => node === before[index]), 'no node was replaced');
  });

  /* ---- async rounds ---------------------------------------------------- */
  await check('duplicate rounds are excluded and the transport stays usable', async () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    const net = stubPost();
    const first = app.liveStep();
    const second = app.liveStep();
    const third = app.advance();
    assert(net.count === 1, 'only one settle may be in flight');
    assert(app.stepping === true && app.busy === false, 'a routine step is not blocking busy');
    assert(app.panel.busy === false, 'routine steps must not lock the transport');
    assert(document.getElementById('step').disabled === false, 'the step button stays usable');
    assert(document.getElementById('scrub').disabled === false, 'an in-progress seek stays usable');
    net.resolve(0, stepResponse(2));
    await first; await second; await third;
    assert(app.stepping === false, 'stepping clears when the round settles');
    assert(app.world.index === 1, 'the settled frame is applied');
    assert(app.panel.stepPending === false, 'the pending hint clears');
  });

  await check('pause during a pending round survives the response', async () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.playing = true;
    const net = stubPost();
    const pending = app.liveStep();
    assert(app.stepping === true, 'the round is in flight');
    assert(app.statusInfo().label === '播放中', 'the status label does not flicker per round');
    app.stop();
    assert(app.playing === false, 'pause takes effect immediately');
    net.resolve(0, stepResponse(2));
    await pending;
    assert(app.playing === false, 'the in-flight round must not resume playback');
    assert(app.world.index === 1, 'the real frame still lands while paused');
    assert(app.stepping === false && app.panel.busy === false, 'nothing stays locked');
    assert(document.getElementById('play').disabled === false, 'play is usable again');
    assert(app.statusInfo().label === '已暂停', 'the status reflects the pause');
  });

  await check('a failed round reports the error and pauses without locking up', async () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.playing = true;
    const net = stubPost();
    const pending = app.liveStep();
    net.reject(0, new Error('boom'));
    await pending;
    assert(app._toasts.some((toast) => toast.kind === 'error' && toast.message.indexOf('本地结算失败') >= 0),
      'the failure is reported as an error toast');
    assert(app.playing === false, 'a failed settle pauses the match');
    assert(app.world.index === 0, 'no frame is invented for a failed settle');
    assert(app.stepping === false && app.busy === false && app.panel.busy === false, 'nothing stays locked');
    assert(document.getElementById('step').disabled === false, 'a retry stays possible');
    const label = app.statusInfo().label;
    assert(label !== '正在结算' && label !== '请求中', `the HUD must not stay on a transient label (got ${label})`);
    assert(['已就绪', '已暂停', '已结束'].indexOf(label) >= 0, 'the HUD reports a settled state');
  });

  await check('a pending LLM answer pauses advancing but keeps polling', async () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.playing = true;
    const net = stubPost();
    const pending = app.liveStep();
    net.resolve(0, { pending: true, state: world.state, done: false, roleCommandMap: {} });
    await pending;
    assert(app.waitingLLM === true, 'the LLM wait is recorded');
    assert(app.world.index === 0, 'a pending answer never advances the frame');
    assert(app.statusInfo().label === '等待模型', 'the wait is visible in the status');
    assert(app.statusInfo().detail === '模型返回前不推进游戏回合', 'the wait explains why the frame is held');
    assert(app.playing === true, 'playback is not silently stopped by a pending answer');
    assert(app.stepping === false, 'the pending answer releases the settle slot');
    const again = app.liveStep();
    assert(net.count === 2, 'a pending answer must not block the next poll');
    net.resolve(1, stepResponse(2));
    await again;
    assert(app.world.index === 1, 'the poll can still settle a round');
  });

  /* ---- stale responses -------------------------------------------------- */
  await check('a response for a replaced scene is dropped', async () => {
    resetApp();
    const worldA = liveWorld(1);
    app.world = worldA;
    app.afterWorldChange();
    const net = stubPost();
    const pending = app.liveStep();
    const worldB = liveWorld(1);
    app.world = worldB;
    app.afterWorldChange();
    assert(app.stepping === false, 'the new scene is free to settle immediately');
    net.resolve(0, stepResponse(2));
    await pending;
    assert(app.world === worldB, 'the scene was not swapped back');
    assert(worldB.index === 0 && worldB.frames.length === 0, 'the stale frame never touches the new scene');
  });

  await check('a stale finally handler cannot clear a newer in-flight round', async () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    app.afterWorldChange();
    const net = stubPost();
    const oldRound = app.liveStep();
    const oldToken = app.stepToken;
    app.invalidateStep();
    const newRound = app.liveStep();
    assert(app.stepToken !== oldToken, 'the new round belongs to a new generation');
    assert(app.stepping === true && app.panel.stepPending === true, 'the new round owns the state');
    net.resolve(0, stepResponse(2));
    await oldRound;
    assert(app.stepping === true, 'the stale finally handler must not clear the newer round');
    assert(app.panel.stepPending === true, 'the newer pending hint stays on');
    assert(app.stepToken !== null, 'the newer round still owns the token');
    net.resolve(1, stepResponse(2));
    await newRound;
    assert(app.stepping === false && app.panel.stepPending === false, 'the newer round clears normally');
    assert(app.world.index === 1, 'the newer response is still applied');
  });

  await check('a stale preview response does not overwrite the current scene', async () => {
    resetApp();
    const worldA = liveWorld(1);
    app.world = worldA;
    app.afterWorldChange();
    const net = stubPost();
    const pending = app.refreshPreview();
    app.world = liveWorld(1);
    app.afterWorldChange();
    net.resolve(0, { roleCommandMap: { 101: { action: 'move', targetPos: [{ x: 9, y: 9 }] } } });
    await pending;
    assert(Object.keys(app.previewCommands).length === 0, 'the stale preview must not be shown');
    assert(app._toasts.every((toast) => toast.kind !== 'error'), 'a stale preview failure is not reported');
  });

  /* ---- input preservation ---------------------------------------------- */
  await check('typed seek input survives blur/change and Jump uses it', () => {
    resetApp();
    const world = worldWithFrames(5);
    app.world = world;
    app.refreshReplayTools();
    const seek = document.getElementById('seek-frame');
    seek.value = '3';
    seek.dispatchEvent({ type: 'input' });
    assert(app.seekDraft === true, 'typing opens a draft');
    seek.dispatchEvent({ type: 'change' });
    seek.dispatchEvent({ type: 'blur' });
    app.refreshReplayTools();
    assert(seek.value === '3', 'a frame update between typing and Jump must keep the typed number');
    document.getElementById('seek-go').dispatchEvent({ type: 'click' });
    assert(app.world.index === 3, 'Jump seeks to the typed frame, not to the displayed one');
  });

  await check('Enter commits a typed frame and Escape discards it', () => {
    resetApp();
    const world = worldWithFrames(5);
    app.world = world;
    app.refreshReplayTools();
    const seek = document.getElementById('seek-frame');
    seek.value = '4';
    seek.dispatchEvent({ type: 'input' });
    seek.dispatchEvent({ type: 'keydown', key: 'Enter' });
    assert(app.world.index === 4, 'Enter commits the typed frame');
    assert(app.seekDraft === false, 'committing ends the draft');
    seek.value = '2';
    seek.dispatchEvent({ type: 'input' });
    seek.dispatchEvent({ type: 'keydown', key: 'Escape' });
    assert(app.world.index === 4, 'Escape does not move the view');
    assert(seek.value === '4', 'Escape restores the displayed frame');
    assert(app.seekDraft === false, 'Escape ends the draft');
  });

  await check('a new scene replaces the typed seek draft', () => {
    resetApp();
    app.world = worldWithFrames(5);
    app.refreshReplayTools();
    const seek = document.getElementById('seek-frame');
    seek.value = '4';
    seek.dispatchEvent({ type: 'input' });
    app.world = liveWorld(1);
    app.afterWorldChange();
    assert(app.seekDraft === false, 'a new scene ends the draft');
    assert(seek.value === '0', 'the field shows the new recording');
  });

  await check('a dragged timeline thumb is not overwritten by frames', () => {
    resetApp();
    const world = worldWithFrames(5);
    app.world = world;
    app.syncTimeline(false);
    const scrub = document.getElementById('scrub');
    scrub.value = '2';
    scrub.dispatchEvent({ type: 'pointerdown', pointerId: 1 });
    world.index = 4;
    app.syncTimeline(false);
    assert(scrub.value === '2', 'a frame landing mid-drag must not move the thumb');
    scrub.dispatchEvent({ type: 'pointerup' });
    assert(scrub.value === '4', 'releasing the drag resyncs to the real frame');
    assert(app.scrubActive === false, 'the drag lock is released');
  });

  await check('unapplied request JSON edits are not overwritten by frames', () => {
    resetApp();
    const world = liveWorld(1);
    app.world = world;
    const box = document.getElementById('input-json');
    box.value = '{"mine":1}';
    box.dispatchEvent({ type: 'input' });
    assert(app.panel.requestDirty === true, 'editing marks the request as dirty');
    app.panel.setRequest(JSON.stringify(world.state, null, 2));
    assert(box.value === '{"mine":1}', 'a frame update must not discard the edit');
    app.panel.setRequest('{"forced":true}', true);
    assert(box.value === '{"forced":true}', 'an explicit replacement (new scene, import) wins');
    app.world = liveWorld(1);
    app.afterWorldChange();
    assert(app.panel.requestDirty === false, 'a new scene clears the dirty flag');
    assert(box.value.indexOf('"mapInfo"') >= 0, 'the new scene publishes its own request');
  });

  await check('loading the official sample replaces a dirty request box', async () => {
    resetApp();
    const box = document.getElementById('input-json');
    box.value = '{"mine":1}';
    box.dispatchEvent({ type: 'input' });
    const original = sandbox.fetch;
    sandbox.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      text: () => Promise.resolve(JSON.stringify(stateAt(85, {}))),
    });
    try {
      await app.loadSample();
    } finally {
      sandbox.fetch = original;
    }
    assert(box.value.indexOf('"mapInfo"') >= 0, 'an explicit load wins over the pending edit');
    assert(app.panel.requestDirty === false, 'applying the sample clears the dirty flag');
    assert(app.world && app.world.mode === 'sample', 'the sample scene is displayed');
  });

  /* ---- markers and debug layout ---------------------------------------- */
  await check('event markers keep labels, clicks and stable nodes', () => {
    resetApp();
    const world = worldWithFrames(2);
    app.world = world;
    app.refreshReplayTools();
    const box = document.getElementById('event-markers');
    assert(box.children.length === 1, 'the recorded failure produces one marker');
    const marker = box.children[0];
    assert(marker.getAttribute('aria-label'), 'every marker carries an accessible label');
    assert(marker.getAttribute('aria-label') === marker.title, 'the label matches the tooltip');
    marker.dispatchEvent({ type: 'click' });
    assert(app.world.index === 1, 'clicking a marker seeks to its frame');
    app.refreshReplayTools();
    assert(box.children[0] === marker, 'unchanged markers are not rebuilt per frame');
  });

  await check('Agent original-text inspector preserves source selection and scroll', async () => {
    resetApp();
    const world = liveWorld(8);
    world.state._demo = {planner: {judge: {llmUsedToday: 2}, teamAgent: {
      task: {stage: 'waiting_model', prompts: 3, commands: 2, inspections: 1},
      world: {status: {news: 'interpreted', treasure: 'waiting_model'}},
      memory: {records: [
        {id: 'question', label: '当前任务原文', received_chars: 8, text: 'question'},
        {id: 'document', label: 'cat /manual', received_chars: 24, text: '<script>literal</script>'}
      ]}}}};
    HW.experience.updateAgent(world);
    const select = document.getElementById('agent-source-select');
    const first = select.children[0];
    select.value = 'document';
    HW.experience.updateAgent(world);
    const area = document.getElementById('agent-source-text');
    area.scrollTop = 77;
    HW.experience.updateAgent(world);
    assert(select.value === 'document' && select.children[0] === first, 'source DOM and selection stay stable');
    assert(area.value === '<script>literal</script>' && area.scrollTop === 77, 'raw text is literal and scroll is preserved');
    assert(document.getElementById('agent-stage').textContent === '等待模型', 'stage is readable');
    assert(document.getElementById('agent-budget').textContent.includes('2/3'), 'shared quota is visible');
    world.state.phaseTask = '';
    world.state._demo.task_report = {ended: 'completed', rewards: {rate: 1}};
    HW.experience.updateAgent(world);
    assert(document.getElementById('agent-stage').textContent.includes('任务已结束'), 'settlement supersedes stale planner stage');
    world.state.phaseTask = 'A new task';
    HW.experience.updateAgent(world);
    assert(document.getElementById('agent-stage').textContent === '等待模型', 'old settlement does not override an active task');
  });

  await check('World evidence separates public sources, inference and missing conditions', async () => {
    resetApp();
    const world = liveWorld(12);
    world.state.roundNo = 132;
    world.state.vendorShopList = [{name:'iron',price:6}];
    world.state._demo = {planner:{teamAgent:{world:{
      sources:{news:[{id:'abcdef123456',firstRound:1,text:'<script>明日停矿</script>',truncated:true}],treasure:[]},
      status:{news:'invalid_reply'},
      news_events:[{resource:'iron',availability:'unavailable',startDay:2,endDay:3,priceDirection:'up'}],
      hypothesis:{site:{x:3,y:4},items:['StarSand','StarSand'],opensAt:null,closesAt:null,uncertain:true}
    }}}};
    HW.experience.updateAgent(world);
    const source = document.getElementById('agent-news-sources');
    assert(source.textContent.includes('<script>明日停矿</script>') && source.textContent.includes('仅保留片段'), 'source is literal with truncation stated');
    assert(document.getElementById('agent-news-inferences').textContent.includes('第 2—3 天'), 'dated prediction is separate');
    assert(document.getElementById('agent-news-prices').textContent.includes('6 金币'), 'actual observation price is shown');
    assert(document.getElementById('agent-news-unknown').textContent.includes('此前资料'), 'stale inference is not advertised as a fresh confirmed result');
    assert(document.getElementById('agent-treasure-inferences').textContent.includes('StarSand、StarSand'), 'duplicate requirements remain visible');
    assert(document.getElementById('agent-treasure-unknown').textContent.includes('开启与关闭回合'), 'unknown window is explicit');
    source.scrollTop = 83;
    HW.experience.updateAgent(world);
    assert(source.scrollTop === 83, 'same source does not reset scroll');

    const correctedId = 'nfedcba9876543210';
    const correctingId = 'n0123456789abcdef';
    const conflictingId = 'n1111111111111111';
    const priceA = 'n2222222222222222';
    const priceB = 'n3333333333333333';
    world.state._demo.planner.teamAgent.world.status = {news:'interpreted', treasure:'waiting_model'};
    world.state._demo.planner.teamAgent.world.news_gap_overflow = false;
    world.state._demo.planner.teamAgent.world.news_view = {
      schema:'competition-news-view/1',
      facts:[
        {id:correctedId, resource:'iron', availability:'unavailable', status:'corrected',
         effectiveDays:[2], possibleDays:[2,3], partial:false, startDay:2, endDay:3, resumeDay:null,
         priceDirection:'up', priceAmount:null, priceBasis:'unknown', sources:['abcdef123456'], resolution:null},
        {id:correctingId, resource:'iron', availability:'available', status:'active',
         effectiveDays:[3], possibleDays:[3,4,5,6,7,8,9,10], partial:false, startDay:3, endDay:10,
         resumeDay:null, priceDirection:'up', priceAmount:20, priceBasis:'percent', sources:['abcdef123456'],
         resolution:{kind:'corrected', targetId:correctedId}},
        {id:conflictingId, resource:'iron', availability:'unavailable', status:'active',
         effectiveDays:null, possibleDays:[2,3,4], partial:true, startDay:2, endDay:null, resumeDay:5,
         priceDirection:'unknown', priceAmount:null, priceBasis:'unknown', sources:['abcdef123456'], resolution:null},
        {id:priceA, resource:'copper', availability:'available', status:'active',
         effectiveDays:[2], possibleDays:[2], partial:false, startDay:2, endDay:2, resumeDay:null,
         priceDirection:'up', priceAmount:6, priceBasis:'absolute', sources:['abcdef123456'], resolution:null},
        {id:priceB, resource:'copper', availability:'available', status:'active',
         effectiveDays:[2], possibleDays:[2], partial:false, startDay:2, endDay:2, resumeDay:null,
         priceDirection:'up', priceAmount:7, priceBasis:'absolute', sources:['abcdef123456'], resolution:null}
      ],
      conflicts:[
        {resource:'copper', ids:[priceA, priceB], dimensions:['price'], days:[2], possibleDays:[2],
         definite:true, availability:['available'], directions:['up'], prices:['absolute:6', 'absolute:7'],
         sources:[['abcdef123456'], ['abcdef123456']]},
        {resource:'iron', ids:[correctedId, conflictingId], dimensions:['availability'], days:[],
         possibleDays:[2], definite:false, availability:['unavailable'], directions:['unknown'], prices:[],
         sources:[['abcdef123456'], ['abcdef123456']]}
      ],
      gaps:[{resource:'iron', startDay:1, endDay:10, count:2, lostIds:['n1111111111111111', 'n2222222222222222'],
             overflow:false}],
      gapOverflow:false,
      unverifiable:0
    };
    HW.experience.updateAgent(world);
    const ledger = document.getElementById('agent-news-inferences').textContent;
    assert(ledger.includes('模型推断'), 'every ledger line is labelled a model inference');
    assert(ledger.includes('已被更正') && ledger.includes('替代'), 'a correction points at the replaced event id');
    assert(ledger.includes('20%%'), 'a percent amount keeps its own unit');
    assert(ledger.includes('第 5 天恢复'), 'an explicit resume day is shown');
    assert(ledger.includes('有效日 2'), 'backend effective days are shown, not recomputed');
    assert(ledger.includes('与其他消息冲突（价格）'), 'a backend price conflict keeps its dimension');
    assert(ledger.includes('可能冲突（状态）'), 'a possible conflict is not presented as settled');
    assert(document.getElementById('agent-news-unknown').textContent.includes('已观测事实'), 'observed prices stay separate from inference');
    assert(document.getElementById('agent-news-unknown').textContent.includes('待逐字恢复'), 'bounded lost ids are shown');
    assert(document.getElementById('agent-news-unknown').textContent.includes('未解决冲突 2 项'), 'the backend conflict count is shown');
    world.state._demo.planner.teamAgent.world.news_view.gapOverflow = true;
    HW.experience.updateAgent(world);
    assert(document.getElementById('agent-news-unknown').textContent.includes('溢出'), 'an opaque overflow is stated, not hidden');
    world.state._demo.planner.tasks = {supervisor: {news_economy: {copper: {due_day: 2}}}};
    HW.experience.updateAgent(world);
    const saleAdvice = document.getElementById('agent-news-inferences').textContent;
    assert(saleAdvice.includes('第 2 天降价前优先出售铜') && saleAdvice.includes('回防安排'), 'economic advice explains the action and its constraints');
    delete world.state._demo.planner.tasks.supervisor.news_economy;
    HW.experience.updateAgent(world);
    assert(!document.getElementById('agent-news-inferences').textContent.includes('交易建议'), 'expired advice disappears');
  });

  process.stdout.write(JSON.stringify(checks));
})().catch((error) => {
  process.stdout.write(JSON.stringify([{ name: 'harness', ok: false, error: String((error && error.stack) || error) }]));
});
"""


@unittest.skipIf(NODE is None, "node is required for the frontend interaction tests")
class FrontendStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = ROOT / "tests" / "_frontend_tmp"
        cls.work.mkdir(exist_ok=True)
        cls.harness = cls.work / "stability_harness.js"
        cls.harness.write_text(HARNESS % {
            "dir": json.dumps(str(WEB)),
            "modules": json.dumps(MODULES),
        }, encoding="utf-8")
        result = subprocess.run([NODE, str(cls.harness)], capture_output=True, text=True,
                                encoding="utf-8", timeout=180)
        if result.returncode != 0:
            raise AssertionError(f"node harness failed:\n{result.stderr}")
        cls.checks = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_all_interaction_checks_pass(self):
        failures = [c for c in self.checks if not c["ok"]]
        self.assertTrue(self.checks, "the harness produced no checks")
        self.assertGreaterEqual(len(self.checks), 15, "the harness lost checks")
        self.assertEqual([], [f"{c['name']}: {c['error']}" for c in failures],
                         f"{len(failures)} of {len(self.checks)} interaction checks failed")


if __name__ == "__main__":
    unittest.main()
