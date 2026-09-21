"""UI reports recorded clearance state without inventing a new decision."""
import shutil
import subprocess
import unittest
from pathlib import Path


class ClearedNightFrontendTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_recorded_clearance_labels_and_stale_state(self):
        code = r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const box={window:{HW:{}},document:{getElementById:()=>null}};vm.createContext(box);
vm.runInContext(fs.readFileSync('web/js/experience.js','utf8'),box);
const brief=box.window.HW.experience.brief;
function world(report,round=120,isDay=false){return {
 mode:'live',state:{roundNo:round,_demo:{planner:{tasks:{supervisor:{cleared_night:report}}}}},
 phase:{isDay,day:1,inPhase:50,total:60},result:()=>({done:false}),counts:()=>({robots:0})};}
const r={phase:'productive',observed_round:119,safe_rounds:3,required_rounds:3,reason:'clearance_confirmed'};
assert.match(brief(world(r)).title,/清场后工作/);assert.match(brief(world(r)).text,/119 回合决策/);
assert.match(brief(world({...r,phase:'defend',safe_rounds:2,required_rounds:5,reason:'observed_quiet_interval'})).text,/2\/5 轮/);
assert.match(brief(world({...r,phase:'defend',safe_rounds:0,reason:'visible_threat'})).text,/发现威胁，回防/);
assert.match(brief(world({...r,phase:'defend',safe_rounds:0,reason:'incomplete_public_observation'})).text,/信息不足/);
assert.doesNotMatch(brief(world(r,250)).title,/清场/);
assert.doesNotMatch(brief(world({...r,observed_round:121})).title,/清场/);
assert.match(brief(world(r,120,true)).title,/白天建设/);
assert.match(brief(world(undefined)).title,/夜间防守/);
console.log('PASS clearance labels, custom threshold, threat, missing/stale/future reports, day phase');
'''
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run(['node','-e',code],cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=20)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)


if __name__=='__main__':unittest.main()
