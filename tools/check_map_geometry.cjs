/* Independent hand-calculated geometry/source/request checks; no game or paid API. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value: '', textContent: '', className: '', style: {},
    classList: {remove() {}, add() {}}, addEventListener() {}});
  return elements.get(id);
}
const box = {window: {addEventListener() {}}, performance: {now: () => 1000},
  document: {getElementById: element}, sessionStorage: {setItem() {}}, URLSearchParams};
vm.createContext(box);
for (const name of ['map-source', 'constants', 'sprites', 'viewmodel', 'effects', 'renderer', 'app']) {
  vm.runInContext(fs.readFileSync(path.join(root, 'web/js', `${name}.js`), 'utf8'), box);
}
const HW = box.window.HW;
const plain = value => JSON.parse(JSON.stringify(value));
function context() {
  return new Proxy({calls: []}, {get(obj, key) {
    if (key in obj) return obj[key];
    if (key === 'measureText') return text => ({width: text.length * 5});
    if (key === 'createRadialGradient' || key === 'createLinearGradient') return () => ({addColorStop() {}});
    return (...args) => obj.calls.push([key, ...args]);
  }});
}
const ctx = context();
const renderer = new HW.Renderer({getContext: () => ctx});
renderer.viewport = {width: 0, height: 0}; // Identity projection, making expected pixels explicit.
const unit = (id, kind, x, y) => ({id, roleType: kind, pos: {x, y}, health: 1500, level: 1});
const zone = (kind, x, y) => ({neutralType: kind, pos: {x, y}});
const state = {roundNo: 1, mapInfo: {width: 41, height: 32, zones: [
  zone('challengerTaskPoint2', 17, 17), zone('challengerTaskPoint2', 16, 17),
  zone('vendor', 20, 16), zone('defenderTaskPoint2', 26, 17)]},
  teamOur: {type: 'challenger', roles: [unit(1, 'station', 9, 22), unit(2, 'worker', 8, 22)]},
  teamEnemy: {roles: [unit(3, 'station', 30, 10)]}, robot: {roles: []}};
const rawBefore = JSON.stringify(state);
const world = HW.World.fromScenario({state});
const base = world.actors.find(actor => actor.id === 1);
const other = world.actors.find(actor => actor.id === 3);
// Hand calculation from replay rows 9/10 and columns 9/10 with a 44px cell.
assert.deepEqual(plain(HW.mapRect(base.pos, base.footprint, 32, 44)),
  {x: 396, y: 396, width: 88, height: 88, cx: 440, cy: 440});
assert.deepEqual(plain(renderer.cellToScreen(base.pos, world, base.footprint)), {x: 440, y: 440});
assert.deepEqual(plain(renderer.cellToScreen(other.pos, world, other.footprint)), {x: 1364, y: 968});
assert.deepEqual(plain(renderer.cellToScreen({x: 0, y: 0}, world)), {x: 22, y: 1386});
assert.deepEqual(plain(renderer.cellToScreen({x: 40, y: 31}, world)), {x: 1782, y: 22});
for (const [x, y] of [[418, 418], [462, 418], [418, 462], [462, 462]]) {
  assert.equal(renderer.pick(world, x, y).id, 1, 'every occupied base cell selects the base');
}
assert.equal(renderer.pick(world, 418, 374), null, 'row above upper-left anchor is not base');
assert.equal(renderer.pick(world, 418, 506), null, 'row below two-cell footprint is not base');
assert.equal(renderer.pick(world, 374, 418).kind, 'worker', 'neighbor worker is not swallowed by base');
const task = world.zones.find(item => item.kind === 'challengerTaskPoint2');
assert.equal(world.zones.filter(item => item.kind === 'challengerTaskPoint2').length, 1);
assert.deepEqual(plain(task.pos), {x: 16, y: 17}, 'explicit pair uses left anchor even in reverse input order');
assert.deepEqual(plain(task.footprint), {width: 2, height: 1});
assert.deepEqual(plain(renderer.cellToScreen(task.pos, world, task.footprint)), {x: 748, y: 638});
for (const x of [16, 17]) assert.equal(renderer.zoneAt(world, {x, y: 17}), task);
for (const pos of [{x: 18, y: 17}, {x: 16, y: 16}, {x: 16, y: 18}]) {
  assert.equal(renderer.zoneAt(world, pos), null, 'no third column or second row of task point');
}
renderer.focusCell(task.pos, world, task.footprint);
assert.deepEqual(plain(renderer.camera), {x: 748, y: 638, scale: 1});
renderer.centerOnActor(world, base);
assert.deepEqual(plain(renderer.camera), {x: 440, y: 440, scale: 1});
renderer.camera = {x: 0, y: 0, scale: 1};
const originalPaint = HW.sprites.paint;
const paints = [];
HW.sprites.paint = (_ctx, actor, x, y) => paints.push({id: actor.id, x, y});
renderer.options = {health: false, labels: false};
renderer.drawActor(ctx, world, base, 44, {height: 32}, {});
assert.deepEqual(paints, [{id: 1, x: 440, y: 440}], 'sprite center follows protocol footprint');
HW.sprites.paint = originalPaint;
const selection = context();
renderer.drawSelection(selection, base, 440, 440, 44, 2);
assert.ok(selection.calls.some(row => JSON.stringify(row) === JSON.stringify(['strokeRect', 396, 396, 88, 88])));
const taskSelection = context();
renderer.drawSelection(taskSelection, task, 748, 638, 44, 2);
assert.ok(taskSelection.calls.some(row => JSON.stringify(row) === JSON.stringify(['strokeRect', 704, 616, 88, 44])));
const taskPaint = context();
HW.sprites.drawZone(taskPaint, 748, 638, 44, task);
const frame = taskPaint.calls.find(row => row[0] === 'strokeRect');
assert.deepEqual(frame, ['strokeRect', 708.4, 620.4, 79.2, 35.2], 'task beacon boundary is two by one');
assert.equal(JSON.stringify(state), rawBefore, 'normalization must not change snapshot cells');

const upgraded = JSON.parse(JSON.stringify(state));
upgraded.roundNo = 2; upgraded.teamOur.roles[0].level = 2;
world.pushStep({state: upgraded, frame: {actions: []}});
const effects = new HW.Effects();
effects.spawnForFrame(world, {actions: []}, 550, {height: 32, tile: 44});
assert.deepEqual(plain(effects.items.find(item => item.text && item.text.includes('1 → 2')).at), {x: 440, y: 440});

assert.match(HW.mapSource.describe(world), /未记录可识别.*不迁移/);
assert.doesNotMatch(HW.mapSource.describe(world), /五场/);
const observed = {id: 'attack-map-observed-v1'};
assert.match(HW.mapSource.describe({mode: 'sample', state: {_demo: {map_layout: observed}}}), /单帧.*保留原坐标.*未完成全图对齐/);
assert.match(HW.mapSource.describe({mode: 'record', metadata: {mapLayout: observed}}), /本地录像回看.*不等于官方回放/);
assert.match(HW.mapSource.describe({mode: 'live', state: {_demo: {map_layout: {id: 'seeded-local-v1'}}}}), /随机布局.*压力测试/);

async function requests() {
  element('seed').value = '7'; element('side').value = 'defender';
  element('pressure').value = '1'; element('profile').value = 'observed-seven-days';
  element('agent-demo-backend').value = 'scripted';
  const app = Object.create(HW.App.prototype);
  const sent = [];
  app.panel = new Proxy({}, {get: () => () => {}});
  app.renderer = {fit() {}}; app.timings = {};
  app.stop = () => {}; app.play = () => {}; app.afterWorldChange = () => {};
  app.refreshPreview = async () => {}; app.watchRecording = async () => {};
  app.getJson = async url => {sent.push({url}); return {state};};
  app.postJson = async (url, payload) => {sent.push({url, payload}); return {state, id: 'job-test'};};
  for (const layout of ['attack-map-observed-v1', 'seeded-local-v1']) {
    element('map-layout').value = layout;
    app.busy = false;
    assert.equal(await app.newMatch(), true);
    assert.equal(new URL(sent.at(-1).url, 'http://localhost').searchParams.get('map_layout'), layout);
    app.busy = false; await app.startLLMDemo();
    assert.equal(sent.at(-1).payload.map_layout, layout);
    app.busy = false; await app.loadSeries(10);
    assert.equal(sent.at(-1).url, '/debug/recording/start');
    assert.equal(sent.at(-1).payload.map_layout, layout);
  }
  element('map-layout').value = '';
  assert.equal(app.selectedMapLayout(), 'attack-map-observed-v1');
}
requests().then(() => console.log('PASS: map rectangles, pixel centers, all base cells, task pairs, focus, selection, upgrade effects, unchanged snapshots, source labels and map-layout requests.'))
  .catch(error => {console.error(error); process.exitCode = 1;});
