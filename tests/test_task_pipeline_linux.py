"""Linux-only real shell + localhost service fixtures; no official v7 isolation claim.

Models are scripted. We run only our own test-authored commands against a
temporary directory and an explicitly started loopback fixture server.
"""
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import brain, planner
from test_tasks import observation


@unittest.skipUnless(os.name == 'posix', 'real shell fixtures require Linux')
class LinuxPipelineTests(unittest.TestCase):
    def exercise(self, mode, index, side):
        with tempfile.TemporaryDirectory(prefix='codehw-pipeline-') as folder:
            work=Path(folder)/'nested'/('workspace_'+str(index))
            work.mkdir(parents=True)
            expected_port=8200+index
            token='verified-fixture-'+str(index)  # fixture oracle, never in task text/docs
            class Handler(BaseHTTPRequestHandler):
                def log_message(self,*args): pass
                def do_GET(self):
                    if self.path == '/check':
                        passed=((work/'config.txt').read_text() == 'port '+str(expected_port)
                                and stat.S_IMODE((work/'start.sh').stat().st_mode) == 0o755)
                        answer={'token':token} if passed else {'error':'configuration or executable mode incorrect'}
                    else:
                        answer={'region':'region-'+str(index),'count':11+index}
                    raw=json.dumps(answer).encode()
                    self.send_response(200); self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                base='http://127.0.0.1:'+str(server.server_port)
                task=work/'task_variant.md'
                if mode=='api':
                    task.write_text('Read API_DOCS.md. Query current records. Return exactly the JSON object region/count.')
                    (work/'API_DOCS.md').write_text('GET '+base+'/records?area='+str(index)+'. Response is JSON with region/count.')
                    command="python3 -c "+shlex.quote("import urllib.request;print(urllib.request.urlopen("+repr(base+'/records?area='+str(index))+",timeout=2).read().decode())")
                    expected={'region':'region-'+str(index),'count':11+index}
                else:
                    task.write_text('Read spec.md. Repair configuration and executable mode, run ./check. Submit JSON containing token from successful check.')
                    (work/'spec.md').write_text('config.txt must equal port '+str(expected_port)+'. start.sh executable mode must be 755.')
                    (work/'config.txt').write_text('port 9999')
                    (work/'start.sh').write_text('#!/bin/sh\nexit 0\n')
                    (work/'start.sh').chmod(0o644)
                    check="import urllib.request;print(urllib.request.urlopen("+repr(base+'/check')+",timeout=2).read().decode())"
                    (work/'check').write_text('#!/bin/sh\npython3 -c '+shlex.quote(check)+'\n')
                    (work/'check').chmod(0o755)
                    repair="from pathlib import Path;Path('config.txt').write_text("+repr('port '+str(expected_port))+");Path('start.sh').chmod(0o755)"
                    command='cd '+shlex.quote(str(work))+' && python3 -c '+shlex.quote(repair)+' && ./check'
                    expected={'token':token}
                memory=planner.PlannerState()
                description='Read '+str(task)+'.'
                def step(n,llm='',cmd=''):
                    nonlocal memory
                    payload=observation(round_no=n,team=side,role_pos=(6,5),phase_task=description,
                        llm_resp=llm,cmd_result=cmd,timeout_rounds=15)
                    with patch.object(planner,'state_for',return_value=memory):
                        response=brain.respond(payload)
                    memory=planner.PlannerState.load(json.loads(json.dumps(memory.dump())))
                    self.assertFalse(memory.team_agent.degraded)
                    return response
                def shell(cmd):
                    process=subprocess.run(['sh','-c',cmd],cwd='/',capture_output=True,
                        text=True,encoding='utf-8',timeout=5)
                    self.assertEqual(process.returncode,0,process.stderr)
                    return '[exitCode:0]\n'+process.stdout
                def plan(kind,value):
                    pending=memory.team_agent.task.pending
                    return json.dumps({'request_id':pending['token'],'plan':{
                        'kind':kind,'command' if kind=='run' else 'answer':value,
                        'evidence_ids':pending['evidence_ids']}})
                first=step(1)
                self.assertIn('executeCmd',first)
                second=step(2,cmd=shell(first['executeCmd']))
                self.assertIn('prompt',second)
                third=step(3,llm=plan('run',command))
                result=shell(third['executeCmd'])
                observed=json.loads(result.split('\n',1)[1])
                self.assertEqual(observed,expected)
                step(4,cmd=result)
                last=step(5,llm=plan('answer',json.dumps(observed)))
                answers=[c['taskAnswer'] for c in last['roleCommandMap'].values() if c['action']=='submitAnswer']
                self.assertEqual(len(answers),1)
                self.assertEqual(json.loads(answers[0]),expected)
                self.assertEqual(memory.team_agent.task.commands,2)
                self.assertEqual(memory.team_agent.task.prompts,2)
            finally:
                server.shutdown();server.server_close();thread.join(timeout=3)

    def test_api_and_engineering_variants_both_teams(self):
        with patch.dict(os.environ,{brain.TASK_AGENT_ENV:'on',brain.WORLD_AGENT_ENV:'off'}):
            for mode in ('api','engineering'):
                for index in (1,7,19):
                    for side in ('challenger','defender'):
                        with self.subTest(mode=mode,index=index,side=side):
                            self.exercise(mode,index,side)


if __name__=='__main__':
    unittest.main()
