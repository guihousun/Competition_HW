"""Snapshot truth and zoom-independent crew callouts; no simulator oracle."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
BOOT = """
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
global.window = global;
for (const name of ['constants', 'viewmodel', 'sprites', 'renderer']) {
  vm.runInThisContext(fs.readFileSync(ROOT + '/web/js/' + name + '.js', 'utf8'));
}
const status = (state) => HW.viewModel.taskStatus({state});
// Realistic enough metrics for the 12-14px CJK UI font: a full-width glyph is
// about one em, digits and Latin are roughly half. The canvas font string is
// like `bold 14px ...`, so the size is the first number in it.
const metrics = (s) => {
  const size = parseFloat(/\\d+(?:\\.\\d+)?/.exec(s)[0]);
  return (text) => ({width: [...String(text)].reduce(
    (sum, ch) => sum + (ch.codePointAt(0) > 0x2e80 ? size : size * .55), 0)});
};
"""


@unittest.skipIf(NODE is None, 'node required')
class CrewDisplayTests(unittest.TestCase):
    def run_js(self, source):
        result = subprocess.run([NODE, '-e', 'const ROOT = ' + json.dumps(str(ROOT)) + ';' + BOOT + source],
                                capture_output=True, text=True, encoding='utf-8', timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_snapshot_countdown_is_remaining_time_and_rewinds(self):
        self.run_js("""
const active = {description:'current task', accepted:10, timeout:25, deadline:35};
Object.defineProperty(active, 'answer', {get(){throw Error('must not read reference answer');}});
const state = {roundNo:15, phaseTask:'current task', teamOur:{type:'challenger'},
  _demo:{task_world:{points:{challengerTaskPoint1:{active}}}}};
assert.equal(status(state).ratio, .8);
assert.equal(status(state).meter, '剩余 20 / 25 轮');
state.roundNo = 34; assert.equal(status(state).ratio, .04);
state.roundNo = 10; assert.equal(status(state).ratio, 1);
state.roundNo = 40; assert.equal(status(state).ratio, 0);
""")

    def test_unknown_fields_and_enemy_task_do_not_invent_progress(self):
        self.run_js("""
assert.equal(status({phaseTask:'task'}).ratio, null);
assert.equal(status({}).active, false);
const state = {roundNo:12, phaseTask:'task', teamOur:{type:'challenger'},
  _demo:{task_world:{points:{defenderTaskPoint1:{active:{description:'task',timeout:25,deadline:30}}}}}};
assert.equal(status(state).ratio, null);
state._demo.task_world.points.challengerTaskPoint1 = {active:{description:'old task',timeout:25,deadline:30}};
assert.equal(status(state).ratio, null);
""")

    def test_ended_is_not_automatically_success(self):
        self.run_js("""
assert.equal(status({_demo:{task_report:{ended:'timeout'}}}).ratio, null);
const partial = status({_demo:{task_report:{ended:'timeout',rewards:{rate:.4,score:20,gold:12}}}});
assert.equal(partial.ratio, .4);
assert.equal(partial.meter, '本地结算通过率 40%');
assert.equal(partial.active, false);
""")

    def test_callouts_are_compact_readable_and_follow_walk(self):
        self.run_js("""
const text = [], anchors = [];
const ctx = new Proxy({}, {get(target, prop){
  if(prop === 'measureText') return s => metrics(target.font)(s);
  if(prop === 'fillText') return (s,x,y) => text.push({s,x,y,font:target.font});
  if(prop === 'moveTo') return (x,y) => anchors.push({x,y});
  return prop in target ? target[prop] : ()=>{};
}});
const renderer = Object.create(HW.Renderer.prototype);
renderer.viewport = {width:900,height:600};
renderer.camera = {x:902,y:704,scale:.3};
const actor = (id,x) => ({id,kind:'worker',label:'工人',owner:'own',health:220,maxHealth:220,
  pos:{x,y:16},rpos:{x,y:16},size:1,backpack:[1,2],capacity:100,anim:[]});
const world = {state:{}, actors:[actor(10010,20),actor(10012,21)], zones:[]};
let widths;
for (const scale of [.3,1]) {
  renderer.camera.scale = scale;
  const boxes = renderer.drawCrewLabels(ctx,world,{});
  assert.equal(boxes.length,0);
  if(widths) assert.deepEqual(boxes.map(b=>b.w),widths);
  widths = boxes.map(b=>b.w);
}
// Worker labels are plain text and no longer reserve a plate or cover the map.
assert.ok(text.some(t=>t.s==='蓝一' && t.font.startsWith('bold 14px')));
assert.ok(text.some(t=>t.s==='蓝二' && t.font.startsWith('bold 14px')));
assert.ok(!text.some(t=>/HP|生命|背包/.test(t.s)));
renderer.selected=world.actors[0];
renderer.drawCrewLabels(ctx,world,{});
assert.ok(!text.some(t=>t.s==='工人 #10010'));
assert.ok(!text.some(t=>/HP 220\/220/.test(t.s) && /包 2\/100/.test(t.s)));
world.actors = [actor(10010,21)];
world.actors[0].anim = [{type:'walk',from:{x:20,y:16},to:{x:21,y:16}}];
anchors.length = 0;
renderer.drawCrewLabels(ctx,world,{walkProgress:0});
assert.equal(anchors.length,0);
""")

    def test_active_task_adds_a_short_countdown_and_thin_bar(self):
        self.run_js("""
const fills = [], text = [];
const ctx = new Proxy({}, {get(target, prop){
  if(prop === 'measureText') return s => metrics(target.font)(s);
  if(prop === 'fillText') return (s,x,y) => text.push({s,y,font:target.font,size:parseFloat(target.font)});
  if(prop === 'fillRect') return (x,y,w,h) => fills.push({x,y,w,h});
  return prop in target ? target[prop] : ()=>{};
}});
const renderer = Object.create(HW.Renderer.prototype);
renderer.viewport = {width:900,height:600};
renderer.camera = {x:902,y:704,scale:.5};
const active = {description:'current task', accepted:10, timeout:25, deadline:35};
const pioneer = {id:10011,kind:'pioneer',label:'开拓者',owner:'own',health:200,maxHealth:200,
  pos:{x:20,y:16},rpos:{x:20,y:16},size:1,backpack:[],capacity:40,anim:[]};
const world = {state:{roundNo:15,phaseTask:'current task',teamOur:{type:'challenger'},
  _demo:{task_world:{points:{challengerTaskPoint1:{active}}}}}, actors:[pioneer], zones:[]};
const [compact] = renderer.drawCrewLabels(ctx,world,{});
assert.equal(compact.w,84);assert.equal(compact.h,28);
assert.ok(!text.some(t=>t.s==='剩余 20 / 25 轮'));
fills.length=0;text.length=0;
const [box] = renderer.drawCrewLabels(ctx,world,{hoverActor:pioneer});
// Two short lines plus a countdown line and a thin bar, still far below a card.
assert.ok(box.h>=42 && box.h<=68, `task callout stays short (got ${box.h})`);
assert.ok(box.w>=140 && box.w<=155, `task callout stays narrow (got ${box.w})`);
const countdown = text.find(t=>t.s==='剩余 20 / 25 轮');
assert.ok(countdown, 'short remaining-round countdown');
const bar = fills.filter(f=>f.h===3);
assert.ok(bar.length===2, 'the thin bar has a track and a filled portion');
const track = bar[0];
assert.ok(track.w<=box.w-16, 'the bar stays inside the callout');
assert.ok(track.y>=box.y, 'the bar starts inside the callout');
assert.ok(track.y+track.h<=box.y+box.h, 'the bar ends inside the callout');
// No overlap: the bar starts below the countdown's text line.
assert.ok(track.y >= countdown.y, `bar (${track.y}) must not overlap the countdown baseline (${countdown.y})`);
assert.ok(track.y-(countdown.y) >= 4, 'safe gap between text and bar');
const value = bar[1];
assert.ok(Math.abs(value.w/track.w-.8)<1e-6, 'bar ratio is remaining / total');
// Without an active task the same callout drops back to the compact two lines.
world.state.phaseTask = '';
const [plain] = renderer.drawCrewLabels(ctx,world,{});
assert.equal(plain.h, 22);
assert.equal(plain.w, 84);
""")
