"""Viewer logic tests: coordinate math, animation derivation and rule tables.

These run the browser modules in a bare JS context (no DOM) so the parts that
must stay faithful to the simulator — the y-flip, the frame->animation mapping,
day/night phase, health/range tables and effect lifetimes — are covered by
``python -m unittest`` alongside the Python tests.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "js"
NODE = shutil.which("node")

# Ordered exactly like index.html: modules only depend on earlier ones.
MODULES = ["constants.js", "viewmodel.js", "sprites.js", "effects.js", "renderer.js"]

HARNESS = r"""
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const dir = %(dir)s;
const noop = () => {};
const ctx2d = new Proxy({}, {
  get: (target, prop) => {
    if (prop === 'canvas') return { width: 800, height: 600 };
    if (prop === 'getImageData') return () => ({ data: [0, 0, 0, 255] });
    if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
      return () => ({ addColorStop: noop });
    }
    if (prop === 'measureText') return () => ({ width: 10 });
    return noop;
  },
  set: () => true,
});
const sandbox = {
  console,
  performance: { now: () => 1000 },
  requestAnimationFrame: noop,
  devicePixelRatio: 1,
  setTimeout, clearTimeout,
  document: {
    getElementById: () => null,
    createElement: () => ({ width: 0, height: 0, getContext: () => ctx2d, style: {} }),
    querySelectorAll: () => [],
    addEventListener: noop,
  },
  addEventListener: noop,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const name of %(modules)s) {
  const code = fs.readFileSync(path.join(dir, name), 'utf8');
  vm.runInContext(code, sandbox, { filename: name });
}
const requests = JSON.parse(fs.readFileSync(%(requests)s, 'utf8'));
const checks = [];
const check = (name, fn) => {
  try { fn(); checks.push({ name, ok: true }); }
  catch (error) { checks.push({ name, ok: false, error: String(error && error.message || error) }); }
};
const assert = (cond, message) => { if (!cond) throw new Error(message || 'assertion failed'); };
const close = (a, b, tol) => Math.abs(a - b) <= (tol == null ? 1e-6 : tol);

const HW = sandbox.HW;
const U = HW.util;

/* ---- official split: 70 day rounds then 60 night rounds per 130 ------- */
check('phase boundaries follow the official 70/60 split', () => {
  assert(HW.phaseInfo(1).isDay, 'round 1 must be day');
  assert(HW.phaseInfo(70).isDay, 'round 70 must still be day');
  assert(!HW.phaseInfo(71).isDay, 'round 71 must be night');
  assert(!HW.phaseInfo(130).isDay, 'round 130 must be night');
  assert(HW.phaseInfo(131).isDay && HW.phaseInfo(131).day === 2, 'round 131 is dawn of day 2');
  const last = HW.phaseInfo(1300);
  assert(last.day === 10 && !last.isDay, 'round 1300 is the last night');
  assert(HW.phaseInfo(1300).inPhase === 60, 'round 1300 is night round 60');
});

check('health and range tables are used by level', () => {
  assert(HW.healthMax('station', 1) === 1500 && HW.healthMax('station', 3) === 4500, 'station hp by level');
  assert(HW.healthMax('wall', 2) === 1500, 'wall hp by level');
  assert(HW.healthMax('worker') === 220 && HW.healthMax('pioneer') === 200, 'crew hp');
  assert(HW.healthMax('bossRobot') === 800, 'boss hp');
  assert(HW.rangeOf('gatling', 1) === 3 && HW.rangeOf('gatling', 3) === 7, 'gatling range');
  assert(HW.rangeOf('railgun', 2) === 8, 'railgun range');
  assert(HW.rangeOf('rocket', 1) === 10 && HW.rangeOf('rocket', 2) === 15, 'rocket range');
  assert(HW.rangeOf('worker', 1) === 0, 'crew have no range');
});

check('preview and executed action vocabularies stay distinct', () => {
  assert(HW.IMPLEMENTED_ACTIONS.every((a) => HW.OFFICIAL_ACTIONS.includes(a)),
    'implemented actions must be a subset of the official action set');
  assert(HW.OFFICIAL_ACTIONS.includes('acceptTask') && HW.OFFICIAL_ACTIONS.includes('submitAnswer'),
    'the official action set is not narrowed');
  // What the local engine executes today (economy landed; tasks have not).
  for (const done of ['move', 'attack', 'build', 'collect', 'sell', 'buy', 'use', 'remove']) {
    assert(HW.IMPLEMENTED_ACTIONS.includes(done), `${done} is executed locally`);
  }
  assert(!HW.IMPLEMENTED_ACTIONS.includes('acceptTask'), 'task hand-in is not claimed as implemented');
  assert(!HW.IMPLEMENTED_ACTIONS.includes('summonTreasure'), 'treasure summoning is not claimed');
  assert(HW.UNCOVERED.some((line) => line.includes('任务')), 'uncovered list must mention tasks');
});

/* ---- renderer geometry ---------------------------------------------- */
const renderer = new HW.Renderer({ width: 0, height: 0, getContext: () => ctx2d, style: {} });
const view = {
  state: { mapInfo: { width: 41, height: 32 } }, actors: [], zones: [],
};
renderer.viewport = { width: 800, height: 600, dpr: 1 };
renderer.camera = { x: (41 * HW.BASE_TILE) / 2, y: (32 * HW.BASE_TILE) / 2, scale: 0.5 };

check('y flip matches the official bottom-left origin', () => {
  const bottomLeft = renderer.cellCenterScreen({ x: 0, y: 0 }, view, 1);
  const topLeft = renderer.cellCenterScreen({ x: 0, y: 31 }, view, 1);
  const sameRow = renderer.cellCenterScreen({ x: 40, y: 0 }, view, 1);
  // Official origin is bottom-left: growing y must move up the screen.
  assert(close(topLeft.y, bottomLeft.y - 31 * HW.BASE_TILE * renderer.camera.scale, 0.01),
    `one official y unit is one tile upwards (got ${topLeft.y} vs ${bottomLeft.y})`);
  assert(close(sameRow.y, bottomLeft.y, 0.01), 'the same y renders on the same row');
  assert(close(sameRow.x - bottomLeft.x, 40 * HW.BASE_TILE * renderer.camera.scale, 0.01),
    'growing x moves right by one tile each');
  assert(close(bottomLeft.x,
    renderer.worldToScreen(HW.BASE_TILE / 2, 0).x, 0.01),
  'the cell centre agrees with the raw world projection');
});

check('screenToCell inverts cellCenterScreen', () => {
  for (const cell of [{ x: 0, y: 0 }, { x: 7, y: 26 }, { x: 40, y: 31 }, { x: 20, y: 15 }]) {
    const screen = renderer.cellCenterScreen(cell, view, 1);
    const back = renderer.screenToCell(screen.x, screen.y, view);
    assert(back.x === cell.x && back.y === cell.y, `round trip failed for ${JSON.stringify(cell)}`);
  }
});

check('a 2x2 footprint centres between its four cells', () => {
  const one = renderer.cellCenterScreen({ x: 7, y: 26 }, view, 1);
  const two = renderer.cellCenterScreen({ x: 7, y: 26 }, view, 2);
  assert(close(two.x - one.x, HW.BASE_TILE * renderer.camera.scale / 2, 0.01),
    'footprint centre shifts half a tile right');
  assert(close(two.y - one.y, -HW.BASE_TILE * renderer.camera.scale / 2, 0.01),
    'footprint centre shifts half a tile up (official +y is up)');
});

check('zoom keeps the point under the cursor fixed', () => {
  const before = renderer.screenToWorld(300, 200);
  renderer.zoomAt(300, 200, 1.7);
  const after = renderer.screenToWorld(300, 200);
  assert(close(before.x, after.x, 0.001) && close(before.y, after.y, 0.001), 'zoom anchor drifted');
  renderer.zoomAt(300, 200, 1 / 1.7);
});

/* ---- view model ------------------------------------------------------ */
const realStates = JSON.parse(fs.readFileSync(%(cases)s, 'utf8'));

check('recorded frames produce actors with committed positions', () => {
  const world = HW.World.fromRecording(realStates.recording);
  assert(world.states.length === world.frames.length + 1, 'states must be frames + 1');
  const actor = world.actors.find((a) => a.kind === 'station');
  assert(actor, 'the own station must be present');
  assert(actor.key.startsWith('unit:own:'), 'identity comes from owner + official id');
  assert(actor.maxHealth === 1500, 'level 1 station shows 1500 max hp');
  assert(actor.rpos.x === actor.pos.x && actor.rpos.y === actor.pos.y, 'instant frames snap to real coords');
});

check('walk animation never starts ahead of the committed position', () => {
  const world = HW.World.fromRecording(realStates.recording);
  let checked = 0;
  for (let i = 1; i <= world.frames.length; i += 1) {
    const frame = world.frames[i - 1];
    world.index = i;
    world.applyFrame(i, { instant: false });
    for (const actor of world.actors) {
      const walk = actor.anim.find((a) => a.type === 'walk');
      if (!walk) continue;
      checked += 1;
      assert(walk.from.x === actor.rpos.x && walk.from.y === actor.rpos.y,
        'a tween must start at the previously committed coordinate');
      assert(walk.to.x === actor.pos.x && walk.to.y === actor.pos.y,
        'a tween must end at the newly committed coordinate');
      const step = Math.max(Math.abs(walk.to.x - walk.from.x), Math.abs(walk.to.y - walk.from.y));
      assert(step === 1, 'a single round moves exactly one cell (Chebyshev)');
    }
    void frame;
  }
  assert(checked > 0, 'the recording must contain at least one move to check');
});

check('frame events are only reported when they really happened', () => {
  const world = HW.World.fromRecording(realStates.recording);
  const attacked = world.frames.find((f) => (f.actions || []).some((a) => a.a === 'attack'));
  assert(attacked, 'recording must contain a shot');
  const action = attacked.actions.find((a) => a.a === 'attack');
  assert(action.salvos.length >= 1, 'a shot lists its salvos');
  assert(action.salvos.every((s) => Array.isArray(s.hits)), 'each salvo lists its hits');
  const damage = action.salvos.flatMap((s) => s.hits).reduce((sum, h) => sum + h.damage, 0);
  assert(damage > 0, 'a recorded shot must carry real damage');
  const killed = world.frames.find((f) => (f.robotDeaths || []).length);
  assert(killed && killed.robotDeaths[0].score >= 1, 'a kill records its score');
});

check('skipped commands are surfaced, not hidden', () => {
  // A frame that recorded a rejected command must describe it, so the viewer never
  // hides a failed instruction behind silence.
  const world = HW.World.fromRecording(realStates.recording);
  let checked = 0;
  for (let i = 1; i <= world.frames.length; i += 1) {
    const frame = world.frames[i - 1];
    if (!(frame.skipped || []).length) continue;
    const lines = world.describe(i);
    assert(lines.some((line) => line.type === 'skip'),
      `第 ${i} 帧有未执行指令，事件流必须出现"未执行"记录`);
    checked += 1;
  }
  if (!checked) {
    // The recording is allowed to have none; verify the rule synthetically then.
    const synthetic = { ...realStates.recording, frames: [{ actions: [], skipped: ['10011'],
                                   executed: { 10011: { action: 'move' } } }] };
    const stub = HW.World.fromRecording(synthetic);
    const lines = stub.describe(1);
    assert(lines.some((line) => line.type === 'skip'), '未执行指令应当出现在事件流里');
  }
});

check('describe() reports moves with both endpoints', () => {
  const world = HW.World.fromRecording(realStates.recording);
  for (let i = 1; i <= world.frames.length; i += 1) {
    const lines = world.describe(i);
    for (const line of lines) {
      if (line.type === 'move') assert(line.text.includes('移动') && line.text.includes('('), 'move line has cells');
    }
  }
});

check('a malformed recording degrades instead of crashing', () => {
  const world = HW.World.fromRecording({
    seed: 1, side: 'challenger', pressure: 1, done: false,
    initial: realStates.recording.initial,
    frames: [{ round: 1 }],
  });
  assert(world.states.length === 2, 'missing state is backfilled');
  assert(world.states[1] && world.states[1].teamOur, 'fallback state is usable');
  assert(world.diagnostics.notes.length >= 1, 'the fallback is reported as a diagnostic');
});

check('unknown unit types are flagged, known ones are not', () => {
  assert(!HW.viewModel.isUnmodelled('worker'), 'worker is modelled');
  assert(!HW.viewModel.isUnmodelled('bossRobot'), 'boss is modelled');
  assert(HW.viewModel.isUnmodelled('hoverTank'), 'an unmodelled type is flagged');
  const state = JSON.parse(JSON.stringify(realStates.recording.initial));
  state.teamOur.roles.push({ id: 99999, roleType: 'hoverTank', pos: { x: 1, y: 1 }, health: 10, level: 1 });
  const actors = HW.viewModel.actorsFromState(state);
  const unknown = actors.find((a) => a.kind === 'hoverTank');
  assert(unknown && unknown.unmodelled === true, 'degraded actor is marked');
  assert(HW.sprites.PAINTERS.hoverTank === undefined, 'no dedicated painter exists yet');
});

check('neutral points keep their official footprint', () => {
  const zones = HW.viewModel.buildZones(realStates.recording.initial);
  assert(zones.length > 0, 'zones exist');
  assert(HW.viewModel.footprintOf('challengerTaskPoint2') === 2, 'task point 2 spans two cells');
  assert(HW.viewModel.footprintOf('stone') === 1, 'a mine is one cell');
  assert(zones.every((z) => z.label && z.glyph), 'every zone has a label');
});

check('station health totals come from the state, not from art', () => {
  const world = HW.World.fromRecording(realStates.recording);
  const health = world.stationHealth();
  assert(health.total === 1500 && health.max === 1500 && health.alive === 1, 'full base reported');
});

/* ---- effects --------------------------------------------------------- */
check('effects expire and stay under the cap', () => {
  const effects = new HW.Effects();
  const world = HW.World.fromRecording(realStates.recording);
  let spawned = 0;
  for (let i = 1; i <= Math.min(30, world.frames.length); i += 1) {
    const frame = world.frames[i - 1];
    effects.spawnForFrame(world, frame, 550, { tile: HW.BASE_TILE, height: 32, spawnHits: true });
    spawned += 1;
    assert(effects.count <= HW.MAX_EFFECTS, 'effect queue must stay bounded');
  }
  assert(spawned > 0, 'frames were processed');
  effects.update(1000 + 60000);
  assert(effects.count === 0, 'all effects expire once their time passes');
});

check('effects reuse recorded damage values', () => {
  const effects = new HW.Effects();
  const world = HW.World.fromRecording(realStates.recording);
  const index = world.frames.findIndex((f) => (f.actions || []).some((a) => a.a === 'attack'));
  assert(index >= 0, 'recording has a shot');
  const frame = world.frames[index];
  effects.spawnForFrame(world, frame, 550, { tile: HW.BASE_TILE, height: 32, spawnHits: true });
  const text = effects.items.filter((e) => e.type === 'damage').map((e) => e.value);
  // Every damage number on screen must come from the frame: weapon hits first
  // (shots resolve before movement), then the battle items that hit this round,
  // then the robots' own attacks.
  const expected = frame.actions.filter((a) => a.a === 'attack')
    .flatMap((a) => a.salvos.flatMap((s) => s.hits.map((h) => h.damage)))
    .concat(frame.actions.filter((a) => a.a === 'bomb').map((a) => a.damage))
    .concat((frame.robotAttacks || []).map((a) => a.damage));
  assert(JSON.stringify(text) === JSON.stringify(expected),
    `damage numbers must match the frame (got ${JSON.stringify(text)} vs ${JSON.stringify(expected)})`);
});

check('recorded shots carry their real path and hits', () => {
  const world = HW.World.fromRecording(realStates.recording);
  let checked = 0;
  for (let i = 1; i <= world.frames.length; i += 1) {
    for (const action of world.frames[i - 1].actions || []) {
      if (action.a !== 'attack') continue;
      for (const salvo of action.salvos || []) {
        if (action.source !== 'ballistics' && action.source !== 'missile') continue;
        assert(Array.isArray(salvo.path), 'a ballistic shot must state its path');
        assert(salvo.path.length === 2, 'the path is tower centre -> aim cell');
        const hit = (salvo.hits || [])[0];
        if (salvo.blocked) {
          assert(hit, 'a blocked bullet must name the robot that consumed it');
        } else if (hit) {
          assert(hit.robot, 'a hit must name its robot');
        }
        if (action.kind === 'railgun') {
          assert(typeof salvo.energy === 'number', 'a railgun salvo states its energy');
          const total = (salvo.hits || []).reduce((sum, h) => sum + h.damage, 0);
          assert(total <= salvo.energy, 'penetration damage can never exceed the energy');
        }
        checked += 1;
      }
    }
  }
  assert(checked > 0, 'the recording must contain at least one ballistic shot');
});

check('damage numbers match every recorded cause', () => {
  const world = HW.World.fromRecording(realStates.recording);
  const effects = new HW.Effects();
  let compared = 0;
  for (let i = 1; i <= Math.min(world.frames.length, 240); i += 1) {
    const frame = world.frames[i - 1];
    const causes = (frame.actions || []).some((a) => a.a === 'attack' || a.a === 'bomb')
      || (frame.robotAttacks || []).length;
    if (!causes) continue;
    effects.clear();
    effects.spawnForFrame(world, frame, 550, { tile: HW.BASE_TILE, height: 32, spawnHits: true });
    const shown = effects.items.filter((e) => e.type === 'damage').map((e) => e.value);
    const expected = (frame.actions || []).filter((a) => a.a === 'attack')
      .flatMap((a) => (a.salvos || []).flatMap((s) => (s.hits || []).map((h) => h.damage)))
      .concat((frame.actions || []).filter((a) => a.a === 'bomb').map((a) => a.damage))
      .concat((frame.robotAttacks || []).map((a) => a.damage));
    assert(JSON.stringify(shown) === JSON.stringify(expected),
      `damage numbers must equal the recorded causes (${shown} vs ${expected})`);
    compared += 1;
  }
  assert(compared > 0, 'no shot frame to compare');
});

check('treasure and task events are described, and the attack text is not stale', () => {
  const describeFrame = (frame) => HW.viewModel.describe
    .call({ frames: [frame] }, 1).map((line) => line.text).join(' | ');
  // Task and treasure actions must produce a readable line, not be dropped.
  assert(/领取任务/.test(describeFrame(
    { actions: [{ a: 'acceptTask', id: 1, point: { x: 3, y: 4 } }] })),
  '领取任务应当出现在事件流里');
  assert(/开启宝藏/.test(describeFrame(
    { actions: [{ a: 'summonTreasure', id: 1, to: { x: 5, y: 6 }, items: ['StarSand'],
                  score: 300, gold: 150 }] })),
  '开启宝藏应当出现在事件流里');
  assert(/提交任务答案/.test(describeFrame(
    { actions: [{ a: 'submitAnswer', id: 1, answer: '服务=D9' }] })),
  '提交答案应当出现在事件流里');
  // The old text claimed ballistic blocking was not modelled; it is now.
  const text = describeFrame({ actions: [{ a: 'attack', id: 1, kind: 'gatling', level: 2,
                                           controller: 1, source: 'ballistics',
                                           salvos: [{ cell: { x: 4, y: 4 },
                                                      hits: [{ robot: 2, damage: 10 }] }] }] });
  assert(!/未计算弹道遮挡/.test(text), '弹道已实现，事件文案不应再说未计算遮挡');
  assert(/直线弹道/.test(text), '应当说明这是官方直线弹道');
  // Rockets state their own rule rather than the ballistic one.
  const rocket = describeFrame({ actions: [{ a: 'attack', id: 1, kind: 'rocket', level: 1,
                                             controller: 1, source: 'missile',
                                             salvos: [{ cell: { x: 4, y: 4 }, hits: [] }] }] });
  assert(/不被阻挡/.test(rocket), '火箭应当说明导弹不被阻挡');
});

check('round-scoped signals never reach the browser state', () => {
  // `_treasureRound` says "the treasure itinerary owns the pioneer this round". The
  // browser posts the returned state straight back, so a stale value suppresses the
  // task walk for the rest of the match — the treasure opened in-process but never
  // in the viewer. Nothing round-scoped may survive into the response.
  const trimmed = HW.viewModel.roundScopedKeys;
  assert(trimmed.includes('_treasureRound'),
    '应当在 roundScopedKeys 中登记 _treasureRound');
});

/* ---- sprite registry ------------------------------------------------- */
check('every modelled kind has a painter and a name', () => {
  const kinds = ['station', 'worker', 'pioneer', 'gatling', 'railgun', 'rocket', 'wall',
    'smallRobot', 'middleRobot', 'largeRobot', 'bossRobot'];
  for (const kind of kinds) {
    assert(typeof HW.sprites.PAINTERS[kind] === 'function', `${kind} needs a painter`);
    assert(HW.KIND_NAMES[kind], `${kind} needs a display name`);
  }
  assert(typeof HW.sprites.painterUnknown === 'function', 'unknown kinds need a fallback painter');
});

check('shop item names keep their official spelling', () => {
  // 任务书 §4.6.3 publishes these names; the viewer must not "fix" them.
  const required = ['WeaponUpgradeVoucher1', 'WeaponUpgradeVoucher2', 'WallUpgradeVoucher1',
    'WallUpgradeVoucher2', 'StationUpgradeVoucher1', 'StationUpgradeVoucher2', 'WallFixer',
    'Medicine', 'DizzyWeapon', 'Bomb', 'SmallRobotSummonOrder', 'MiddleRobotSummonOrder',
    'LargeRobotSummonOrder', 'BossRobotSummonOrder', 'AcientTablet'];
  for (const name of required) {
    assert(HW.ITEM_NAMES[name], `${name} needs a display label`);
    assert(HW.util.itemName(name) !== name, `${name} must be translated for the panel`);
  }
  assert(HW.util.itemName('UnknownThing') === 'UnknownThing', 'unknown items fall back to raw name');
});

check('economy event lines come from the recorded frame', () => {
  const world = HW.World.fromRecording(realStates.recording);
  const withAction = world.frames.findIndex((f) => (f.actions || []).some((a) => a.a === 'sell'));
  if (withAction < 0) return; // the short recording may contain no sale
  const lines = world.describe(withAction + 1);
  assert(lines.some((line) => line.text.includes('卖出')), 'a sell action is described');
});

check('command arrows resolve IDs to committed positions', () => {
  const renderer = new HW.Renderer({getContext: () => ctx2d});
  const origins = [];
  const context = new Proxy({}, {get: (_target, key) => key === 'moveTo'
    ? (x, y) => origins.push([x, y]) : noop, set: () => true});
  const world = {actors:[{id:7,pos:{x:3,y:4}}],frame:{actions:[{id:7,from:{x:2,y:4}}]}};
  const commands = {'7':{action:'move',targetPos:[{x:4,y:4}]}};
  renderer.drawCommands(context, world, commands, 44, {height:32}, '#fff', true);
  assert(origins[0][0] === 154 && origins[0][1] === 1210, 'preview uses current position, not string executor ID');
  origins.length = 0;
  renderer.drawCommands(context, world, commands, 44, {height:32}, '#fff', false);
  assert(origins[0][0] === 110 && origins[0][1] === 1210, 'executed movement uses recorded origin');
});

process.stdout.write(JSON.stringify(checks));
"""


@unittest.skipIf(NODE is None, "node is required for the viewer logic tests")
class ViewerLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = ROOT / "tests" / "_viewer_tmp"
        cls.work.mkdir(exist_ok=True)
        cls.cases = cls.work / "cases.json"
        cls.harness = cls.work / "harness.js"
        cls.cases.write_text(json.dumps(cls.build_cases(), ensure_ascii=False), encoding="utf-8")
        cls.harness.write_text(HARNESS % {
            "dir": json.dumps(str(WEB)),
            "modules": json.dumps(MODULES),
            "requests": json.dumps(str(cls.cases)),
            "cases": json.dumps(str(cls.cases)),
        }, encoding="utf-8")
        result = subprocess.run([NODE, str(cls.harness)], capture_output=True, text=True,
                                encoding="utf-8", timeout=180)
        if result.returncode != 0:
            raise AssertionError(f"node harness failed:\n{result.stderr}")
        cls.checks = json.loads(result.stdout)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    @classmethod
    def build_cases(cls):
        """Record a short real match so the viewer is checked against real frames."""
        import sys
        sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
        from agent.debug import series_payload
        # Long enough to include the first night (round 71) and its combat, which
        # several checks rely on as the source of real shot/death records.
        return {"recording": series_payload(3, "challenger", 2, 140)}

    def test_all_viewer_checks_pass(self):
        failures = [c for c in self.checks if not c["ok"]]
        self.assertTrue(self.checks, "the harness produced no checks")
        self.assertEqual([], [f"{c['name']}: {c['error']}" for c in failures],
                         f"{len(failures)} of {len(self.checks)} viewer checks failed")


if __name__ == "__main__":
    unittest.main()
