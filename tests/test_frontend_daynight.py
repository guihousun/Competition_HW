"""Day/night palette contract for the map surface and the letterbox around it.

The viewer runs without a browser in CI, so the two things a screenshot would
otherwise be needed for are checked as data:

  * the palette is derived from the current round's phase (official 70/60 split,
    read from the world/state, never from the wall clock or a future frame),
  * daylight is visibly brighter than night on both the outside-map background
    and the map terrain, dusk sits between them and reads warm,
  * a replay seek restores the palette of the frame seeked to, and the terrain
    bitmap is cached per phase instead of being rebuilt every frame.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')

# A DOM stub good enough for renderer.draw(): the terrain builder and the sky
# both go through createElement('canvas'), so the fills a screenshot would show
# can be compared as data. `C.record` turns the instrumentation on (the renderer
# accepts a record filter, which keeps its own code free of test hooks).
BOOT = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const C = {record: false, fills: []};
const noop = () => {};
const makeContext = () => {
  const state = {fillStyle: '#000000'};
  return new Proxy(state, {
    get(target, prop){
      if (prop === 'canvas') return {width: 1804, height: 1408};
      if (prop === 'createLinearGradient' || prop === 'createRadialGradient') {
        // Keep the stops so a gradient fill can be compared, not just a flat one.
        return () => ({stops: [], addColorStop(at, color){ this.stops.push(color); }});
      }
      if (prop === 'measureText') return (s) => ({width: String(s).length * 7});
      if (prop === 'fillRect') return (x, y, w, h) => {
        const fill = target.fillStyle;
        const color = typeof fill === 'string' ? fill : (fill && fill.stops || []).join('>');
        if (C.record && w >= 100 && h >= 40) C.fills.push({fill: color, w, h});
      };
      if (prop === 'save' || prop === 'restore' || prop === 'beginPath') return noop;
      return prop in target ? target[prop] : noop;
    },
    set(target, prop, value){ target[prop] = value; return true; },
  });
};
const sandbox = {
  console, performance: {now: () => 1000}, requestAnimationFrame: noop, devicePixelRatio: 1,
  setTimeout, clearTimeout, addEventListener: noop,
  document: {
    getElementById: () => null,
    createElement: () => ({width: 0, height: 0, style: {}, getContext: () => makeContext()}),
    querySelectorAll: () => [],
    addEventListener: noop,
  },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
for (const name of ['constants', 'viewmodel', 'sprites', 'effects', 'renderer']) {
  vm.runInContext(fs.readFileSync(ROOT + '/web/js/' + name + '.js', 'utf8'), sandbox, {filename: name});
}
const HW = sandbox.HW;
const context = makeContext();
const renderer = new HW.Renderer({width: 0, height: 0, style: {}, getContext: () => context});
renderer.viewport = {width: 900, height: 620, dpr: 1};
renderer.camera = {x: 902, y: 704, scale: .5};
const world = {state: {mapInfo: {width: 41, height: 32}, roundNo: 1}, actors: [], zones: [], deaths: []};
// The terrain bitmap is drawn on its own canvas, so the record filter only
// suppresses it here (returning true would also repaint it).
const paint = (roundNo) => {
  world.state.roundNo = roundNo;
  world.phase = HW.phaseInfo(roundNo);
  C.fills.length = 0;
  C.record = true;
  renderer.buildTerrain(world, world.phase, () => false);
  renderer.draw(world, null, {}, 1000);
  C.record = false;
  return C.fills.slice();
};
const rgb = (value) => {
  const m = String(value).match(/-?\d+(?:\.\d+)?/g);
  assert.ok(m && m.length >= 3, 'expected a colour, got ' + value);
  return m.slice(0, 3).map(Number);
};
const luma = (value) => { const [r, g, b] = rgb(value); return .2126 * r + .7152 * g + .0722 * b; };
// A frame's fills can be classified by shape: the sky is the first gradient over
// the whole viewport, the playfield plate is filled in map space (larger than the
// viewport), and the night veil is drawn over the sky afterwards.
const SCREEN = {w: 900, h: 620};
const screenFills = (fills) => fills.filter(f => f.w === SCREEN.w && f.h === SCREEN.h);
const sky = (fills) => {
  const hit = screenFills(fills);
  assert.ok(hit.length >= 1, 'the outside-map background must be painted');
  return hit[0].fill;
};
const skyStops = (fills) => sky(fills).split('>').map(rgb);
// One representative colour for brightness comparisons: the top of the gradient.
const skyColor = (fills) => skyStops(fills)[0];
const veil = (fills) => screenFills(fills).slice(1);
const ground = (fills) => {
  const hit = fills.filter(f => f.w > 1200 && f.h > 800);
  assert.ok(hit.length >= 1, 'the map terrain plate must be painted');
  return hit[0].fill;
};
"""


@unittest.skipIf(NODE is None, 'node required')
class DayNightPaletteTests(unittest.TestCase):
    def run_js(self, source):
        result = subprocess.run([NODE, '-e', 'const ROOT = ' + json.dumps(str(ROOT)) + ';' + BOOT + source],
                                capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_daylight_outside_background_is_brighter_than_night(self):
        self.run_js("""
const day = paint(20);          // mid-day round of the official 70/60 split
const dusk = paint(70);         // last day round: warm dusk shoulder
const night = paint(100);       // deep night
assert.ok(HW.phaseInfo(20).isDay && !HW.phaseInfo(100).isDay, 'rounds chosen from the official split');
const skyNight = rgb(skyColor(night)), skyDay = rgb(skyColor(day)), skyDusk = rgb(skyColor(dusk));
const groundNight = ground(night), groundDay = ground(day), groundDusk = ground(dusk);
assert.ok(luma(skyDay) - luma(skyNight) >= 100,
  `daylight background must read clearly brighter (${skyDay} vs ${skyNight})`);
assert.ok(luma(groundDay) - luma(groundNight) >= 70,
  `daylight terrain must read clearly brighter (${groundDay} vs ${groundNight})`);
assert.ok(luma(skyDay) > luma(skyNight) * 2 && luma(groundDay) > luma(groundNight) * 2,
  'both surfaces more than double in brightness');
const duskGround = rgb(groundDusk);
assert.ok(luma(skyDusk) > luma(skyNight), 'dusk is brighter than deep night');
assert.ok(luma(skyDusk) < luma(skyDay), 'dusk is dimmer than daylight');
// The warm dusk shoulder is the horizon stop of the gradient, not its top.
const horizon = skyStops(dusk).pop();
assert.ok(horizon[0] > horizon[2] + 20, `dusk horizon stays warm (${horizon.join(',')})`);
const dayHorizon = skyStops(day).pop();
// Warmth is red-above-blue, not the raw red level: the daylight stop is a pale
// sky blue with a high red channel but no warmth at all.
assert.ok(horizon[0] - horizon[2] > dayHorizon[0] - dayHorizon[2] + 20,
  `dusk is warmer than the flat daylight sky (${horizon.join(',')} vs ${dayHorizon.join(',')})`);
const nightSky = skyNight, nightGround = rgb(groundNight);
// Night reads as dark blue and much darker; the daylight sky is bright.
assert.ok(nightSky[2] > nightSky[0] && nightSky[2] > nightSky[1], 'night sky is blue-led');
assert.ok(luma(nightSky) < 40, `night sky stays dark (got ${luma(nightSky)})`);
assert.ok(luma(skyDay) > 120, `daylight sky stays bright (got ${luma(skyDay)})`);
assert.ok(nightGround[2] > nightGround[0], 'night terrain is blue-shifted');
assert.notEqual(groundDusk, groundNight, 'dusk ground is a distinct palette from night');
""")

    def test_palette_comes_from_the_frame_round_not_the_clock(self):
        self.run_js("""
// Same phase, different wall clock: the picture must not move.
const first = paint(90);
const before = renderer.skyKey;
renderer._time = 999999;
const second = paint(90);
assert.equal(before, renderer.skyKey, 'the palette must not depend on elapsed time');
assert.deepEqual(first.map(f => f.fill), second.map(f => f.fill), 'identical frames paint identical colours');
// The phase itself is the official round split, not a frame counter.
assert.ok(!HW.phaseInfo(90).isDay && HW.phaseInfo(71).inPhase === 1, 'round 90 is night round 20');
""")

    def test_terrain_is_cached_per_phase_not_rebuilt_every_frame(self):
        self.run_js("""
renderer.terrainCache.clear();
const built = () => renderer.terrainCache.size;
paint(40); paint(41); paint(40);
assert.equal(built(), 1, `the day phase is built once (got ${built()})`);
const dayKey = renderer.terrainKey;
paint(100); paint(101); paint(100);
assert.equal(built(), 2, `day and night are separate bitmaps (got ${built()})`);
assert.notEqual(renderer.terrainKey, dayKey, 'night uses its own bitmap');
// A plain redraw of a cached phase must not swap the bitmap back and forth.
const nightKey = renderer.terrainKey;
for (const round of [1, 40, 70, 100, 130, 40, 100]) paint(round);
assert.ok(renderer.terrainCache.size <= 3, `day / dusk / night only (got ${renderer.terrainCache.size})`);
assert.ok(renderer.terrainKey === dayKey || renderer.terrainKey === nightKey, 'cached bitmap reused');
paint(1);
assert.equal(renderer.terrainKey, dayKey, 'seeking back reuses the daylight bitmap');
""")

    def test_replay_seek_restores_the_frame_palette(self):
        self.run_js("""
// A replay seek re-applies the frame's state, so the palette follows it back.
const rounds = [1, 70, 71, 130, 131];
const states = rounds.map(roundNo => ({mapInfo: {width: 41, height: 32}, roundNo,
  teamOur: {type: 'challenger', roles: []}, teamEnemy: {type: 'defender', roles: []}}));
// A recording holds one more state than frames: index 0 is the initial layout.
const recording = {seed: 1, side: 'challenger', pressure: 1, done: false,
  initial: states[0], states,
  frames: rounds.slice(1).map((roundNo) => ({round: roundNo}))};
const replay = HW.World.fromRecording(recording);
assert.equal(replay.states.length, replay.frames.length + 1, 'states = frames + 1');
assert.deepEqual(replay.states.map(s => s.roundNo), states.map(s => s.roundNo),
  'the recording spans day, dusk and night');
const seen = {};
for (const index of [0, 1, 2, 3, 4, 1, 0, 4]) {
  replay.applyFrame(index, {instant: true});
  C.fills.length = 0;
  C.record = true;
  // Force the terrain bitmap to be rebuilt for this frame: a cached bitmap would
  // silently drop the ground colour from the record.
  renderer.terrainCache.clear();
  renderer.skyKey = '';
  renderer.buildTerrain(replay, replay.phase, () => false);
  renderer.draw(replay, null, {}, 1000);
  C.record = false;
  const fills = C.fills.slice();
  const key = replay.roundNo + '|' + sky(fills) + '|' + ground(fills);
  if (seen[index]) assert.equal(seen[index], key, `seek back to frame ${index} must restore its palette`);
  seen[index] = key;
}
assert.notEqual(seen[0].split('|')[1], seen[2].split('|')[1], 'day and night frames differ');
assert.notEqual(seen[2].split('|')[1], seen[4].split('|')[1], 'the next dawn is not the same as the night before');
assert.equal(seen[0].split('|')[0], '1');
assert.equal(seen[1].split('|')[0], '70', 'frame state supplies the round');
assert.equal(seen[2].split('|')[0], '71', 'the first night round comes from the official split');
assert.ok(HW.phaseInfo(70).isDay && !HW.phaseInfo(71).isDay, 'official 70/60 boundary');
""")

    def test_lighting_toggle_keeps_its_meaning(self):
        self.run_js("""
const nightOn = paint(100);
renderer.options.lighting = false;
const nightOff = paint(100);
const dayOff = paint(20);
renderer.options.lighting = true;
const dayOn = paint(20);
assert.deepEqual(sky(dayOff), sky(dayOn), 'the veil is a no-op in daylight');
assert.deepEqual(ground(nightOff), ground(nightOn), 'the terrain palette is not the lighting layer');
// Turning the layer off removes the veil: same terrain, different overlay.
assert.notEqual(JSON.stringify(veil(nightOff)), JSON.stringify(veil(nightOn)),
  'at night the lighting layer must still change the picture');
assert.equal(veil(dayOn).length, 0, 'no veil in daylight');
""")
