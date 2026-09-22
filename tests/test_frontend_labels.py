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
assert.ok(text.some(t=>t.s==='工1' && t.font.startsWith('bold 14px')));
assert.ok(text.some(t=>t.s==='工2' && t.font.startsWith('bold 14px')));
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

    def test_pioneer_stays_plain_text_during_tasks_and_hover(self):
        self.run_js("""
const text = [], painted = [];
const ctx = new Proxy({}, {get(target, prop){
  if(prop === 'measureText') return s => metrics(target.font)(s);
  if(prop === 'fillText') return s => text.push(s);
  if(['fillRect','strokeRect','fill','stroke'].includes(prop)) return () => painted.push(prop);
  return prop in target ? target[prop] : ()=>{};
}});
const renderer = Object.create(HW.Renderer.prototype);
renderer.viewport = {width:900,height:600};
renderer.camera = {x:902,y:704,scale:.5};
const pioneer = {id:10011,kind:'pioneer',owner:'own',
  pos:{x:20,y:16},rpos:{x:20,y:16},size:1,anim:[]};
const world = {state:{roundNo:15,phaseTask:'current task',teamOur:{type:'challenger'}},
  actors:[pioneer],zones:[]};
for(const ui of [{},{hoverActor:pioneer}]) {
  assert.deepEqual(renderer.drawCrewLabels(ctx,world,ui),[]);
}
renderer.selected = pioneer;
world.state.phaseTask = '';
renderer.drawCrewLabels(ctx,world,{});
assert.deepEqual(text,['拓','拓','拓']);
assert.deepEqual(painted,[], 'crew names have no background or border, including on hover');
""")
