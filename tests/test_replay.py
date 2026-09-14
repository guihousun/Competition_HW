"""Archive round-trip and UI controller regressions, executed in Node."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent.debug import series_payload


@unittest.skipUnless(shutil.which('node'), 'Node required')
class ReplayTests(unittest.TestCase):
    def test_archive_and_playback_contracts(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'recording.json'
            data.write_text(json.dumps(series_payload(19, 'challenger', 1, 5)), encoding='utf-8')
            script = Path(tmp) / 'checks.cjs'
            script.write_text(r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const elements = new Map();
const node = () => ({value:'all', max:0, textContent:'', style:{}, hidden:false,
  classList:{add(){},remove(){},toggle(){}}, append(){}, replaceChildren(){},
  addEventListener(){}, setAttribute(){}});
const sandbox = {console, performance, TextEncoder, crypto:require('crypto').webcrypto,
  setTimeout, clearTimeout, addEventListener(){},
  document:{getElementById(id){if(!elements.has(id)) elements.set(id,node());return elements.get(id);},
    createElement:node, querySelectorAll:()=>[]}};
sandbox.window=sandbox; vm.createContext(sandbox);
for(const name of ['constants.js','viewmodel.js','replay.js','experience.js','app.js'])
  vm.runInContext(fs.readFileSync(process.argv[2]+'/'+name,'utf8'),sandbox);
const HW=sandbox.HW, original=JSON.parse(fs.readFileSync(process.argv[3],'utf8'));
(async()=>{
  const w=HW.World.fromRecording(original);
  assert.equal(w.index,0,'recording opens at initial state');
  assert.match(HW.experience.brief(w).title,/白天建设/);
  assert.match(HW.experience.brief(w).countdown,/70/);
  assert.match(HW.experience.brief({mode:'sample'}).title,/单轮/);
  const nightBrief=HW.experience.brief({mode:'live',result:()=>({done:false}),phase:HW.phaseInfo(71),counts:()=>({robots:4})});
  assert.match(nightBrief.text,/4/);assert.match(nightBrief.countdown,/60/);
  const archive=await HW.Replay.pack(w);
  const decoded=await HW.Replay.unpack(JSON.parse(JSON.stringify(archive)));
  assert.equal(JSON.stringify(decoded.states),JSON.stringify(original.states));
  assert.equal(JSON.stringify(decoded.frames),JSON.stringify(original.frames));
  archive.recording.states[1].teamOur.goldNum+=1;
  await assert.rejects(()=>HW.Replay.unpack(archive),/校验/);
  await assert.rejects(()=>HW.Replay.unpack({kind:HW.Replay.KIND,version:99}),/版本/);
  assert.throws(()=>HW.Replay.validate({...original, states:original.states.slice(1)}),/缺帧/);
  const bad=JSON.parse(JSON.stringify(original));bad.frames[0].round=99;
  assert.throws(()=>HW.Replay.validate(bad),/不匹配/);
  assert.throws(()=>HW.Replay.validate({...original,frames:1300}),/完整/);
  const live=HW.World.fromScenario({state:original.states[0]});
  const commands={'worker-x':{action:'collect'}}, preview={'pioneer-y':{action:'move'}};
  live.pushStep({state:original.states[1],frame:{round:1,actions:[]},executed:commands,roleCommandMap:preview,done:false});
  assert.equal(live.frame.executed,commands,'live submitted commands preserved');
  assert.equal(live.frame.commands,preview,'live next-round preview preserved');
  const rows=HW.Replay.markers({frames:[{round:70,skipped:['x']},{round:71,unitDeaths:[{id:1}]}],
    states:[{roundNo:70},{roundNo:71},{roundNo:72}]});
  assert(rows[0].types.includes('phase'));assert(rows[0].types.includes('skip'));
  assert.equal(HW.Replay.nextMarker(rows,0,1,'death'),2);
  assert.equal(HW.Replay.nextMarker(rows,2,-1,'skip'),1);
  assert.equal(HW.Replay.nextMarker(rows,2,1,'all'),null);
  w.done=true;
  assert.equal(w.result().done,false,'finished recording is not terminal at frame zero');
  w.applyFrame(w.maxIndex,{instant:true});assert.equal(w.result().done,true);
  w.applyFrame(0,{instant:true});
  const app=Object.create(HW.App.prototype);
  Object.assign(app,{world:w,playing:true,frameMs:550,follow:false,busy:false,
    panel:new Proxy({}, {get:()=>()=>{}}),
    effects:{clear(){},spawnForFrame(){},failed(){}},renderer:{},
    selected:null,markers:[],timings:{},effectsSpawnedFor:-1});
  app.enterFrame();assert.equal(app.playing,true,'early replay frame must not stop playback');
  app.bannerHtml='stale terminal';app.seek(1);
  assert.equal(app.bannerHtml,'','seeking clears terminal banner');
  assert.equal(app.previewCommands,w.frames[0].commands,'recorded preview used');
  app.busy=true;app.seek(4);assert.equal(w.index,1,'in-flight operation blocks seeks');
  app.busy=false;app.seek(NaN);assert.equal(w.index,1,'invalid seek cannot corrupt cursor');
  w.index=w.maxIndex;app.playing=true;app.enterFrame();assert.equal(app.playing,false);
  const retained=app.world;
  await app.importFile({target:{value:'bad.json',files:[{size:10,text:async()=>'{"kind":"competition-hw-replay","version":99}'}]}});
  assert.equal(app.world,retained,'failed import preserves current recording');
  assert.equal(app.busy,false,'failed import releases loading state');
  let starts=0;
  const starter=Object.create(HW.App.prototype);
  Object.assign(starter,{busy:false,newMatch:async()=>true,play:()=>{starts++;}});
  await starter.startNewMatch();assert.equal(starts,1,'one-click start begins playback after successful scenario creation');
  starter.newMatch=async()=>false;await starter.startNewMatch();assert.equal(starts,1,'failed creation never plays stale world');
  const rewinder=Object.create(HW.App.prototype);
  Object.assign(rewinder,{busy:false,world:live,stop(){},effects:{clear(){}},panel:app.panel,
    afterWorldChange(){},applyCurrentFrame(){}});
  await rewinder.reset();assert.equal(rewinder.world,live);assert.equal(live.index,0);
  assert.equal(live.frameCount,1,'back to beginning must preserve live history');
  console.log('archive round-trip, corruption, event navigation, live frames, replay playback: PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
''', encoding='utf-8')
            result = subprocess.run(['node', str(script), str(ROOT / 'web/js'), str(data)],
                                    capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
