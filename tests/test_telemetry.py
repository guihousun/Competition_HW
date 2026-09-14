"""Independent trace expectations: HTTP isolation, data fidelity and visible loss."""
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'))
sys.path.insert(0,str(ROOT/'tools'))
from agent import telemetry as t, server
import trace_tool


def observation(round_no=1, health=220, robots=True):
    return {'roundNo':round_no,'mapInfo':{'width':41,'height':32,'zones':[]},
            'teamOur':{'type':'challenger','goldNum':75,'roles':[
                {'id':1,'roleType':'station','health':1500,'level':1,'pos':{'x':9,'y':22}},
                {'id':2,'roleType':'worker','health':health,'level':1,'pos':{'x':8,'y':21},'backpack':['stone']}]},
            'teamEnemy':{'roles':[]},'robot':{'roles':[{'id':10,'roleType':'smallRobot','health':40,'pos':{'x':20,'y':21}}] if robots else []}}


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def recorder(self, **kwargs):
        rec=t.Recorder(self.root/'run',{'code_commit':'a'*40},**kwargs)
        self.addCleanup(rec.close)
        return rec

    def add(self,rec,request,response=None):
        response={'roleCommandMap':{}} if response is None else response
        ticket=rec.begin()
        rec.submit(ticket,json.dumps(request).encode(),json.dumps(response).encode(),plan_ms=1.5)
        return ticket

    def turns(self,rec):
        self.assertTrue(rec.close())
        return [r for r in trace_tool.records(rec.directory) if r.get('event')=='turn']

    def test_full_request_response_and_consecutive_feedback_survive_readback(self):
        rec=self.recorder()
        first=observation()
        response={'roleCommandMap':{'2':{'action':'collect','targetPos':[{'x':7,'y':20}]}},'prompt':'task text'}
        self.add(rec,first,response)
        second=observation(2,health=200,robots=False)
        second['lastRoundRoleActionResults']={'2':False}
        second['errors']=[{'errorCode':7}]
        self.add(rec,second)
        rows=self.turns(rec)
        self.assertEqual(rows[0]['request'],first)
        self.assertEqual(rows[0]['response'],response)
        self.assertTrue(rows[0]['semantic_replay_exact'])
        self.assertEqual(rows[1]['previous_event_id'],rows[0]['event_id'])
        self.assertEqual(rows[1]['observed_delta']['teamOur']['changed']['2']['health'],{'before':220,'after':200})
        self.assertEqual(rows[1]['observed_delta']['robot']['no_longer_observed'],['10'])
        self.assertNotIn('dead',json.dumps(rows[1]['observed_delta']))
        self.assertEqual(rows[1]['previous_action_feedback']['lastRoundRoleActionResults'],{'2':False})
        self.assertTrue(trace_tool.inspect(rec.directory)['quality']['capture_complete'])

    def test_nested_secrets_are_redacted_and_exactness_is_not_claimed(self):
        rec=self.recorder();req=observation()
        secret='sk-unit-test-secret-01234567890123456789'
        req['credentials']={'api_key':secret,'authorization':'Bearer '+('x'*30)}
        req['lastCmdResult']='prefix '+secret
        self.add(rec,req)
        row=self.turns(rec)[0]
        self.assertNotIn(secret,json.dumps(row))
        self.assertFalse(row['semantic_replay_exact'])
        self.assertTrue(row['transformed_request_paths'])
        self.assertEqual(row['request']['credentials']['api_key'],'[REDACTED]')

    def test_gap_reset_and_out_of_order_do_not_create_false_feedback_links(self):
        rec=self.recorder()
        for round_no in (1,3,1):self.add(rec,observation(round_no))
        late=rec.begin();early=rec.begin()
        rec.submit(early,json.dumps(observation(4)).encode(),b'{"roleCommandMap":{}}')
        rec.submit(late,json.dumps(observation(2)).encode(),b'{"roleCommandMap":{}}')
        rows=self.turns(rec)
        self.assertEqual([r['continuity'] for r in rows],['first_observation','round_gap','round_reset_or_duplicate','round_gap','out_of_order_completion'])
        self.assertTrue(all('previous_action_feedback' not in r for r in rows))

    def test_rotation_retains_all_parts_and_existing_run_is_not_overwritten(self):
        rec=self.recorder(segment_bytes=2000)
        for n in range(1,5):self.add(rec,observation(n))
        self.assertEqual(len(self.turns(rec)),4)
        self.assertGreater(len(list(rec.directory.glob('events-*.jsonl'))),1)
        with self.assertRaises(FileExistsError):t.Recorder(rec.directory,{})

    def test_oversize_and_queue_budget_loss_are_visible(self):
        rec=self.recorder(max_pending_bytes=100,max_event_bytes=1000)
        self.add(rec,observation())
        self.assertEqual(self.turns(rec),[])
        status=json.loads((rec.directory/'status.json').read_text())
        self.assertEqual(status['dropped'],1)
        self.assertEqual(status['drop_reasons'],{'queue_bytes':1})
        self.assertFalse(trace_tool.inspect(rec.directory)['quality']['capture_complete'])

    def test_disk_limit_does_not_silently_delete_old_data(self):
        rec=self.recorder(max_total_bytes=6000,segment_bytes=2000)
        for n in range(1,6):self.add(rec,observation(n))
        rows=self.turns(rec)
        self.assertLess(len(rows),5)
        status=json.loads((rec.directory/'status.json').read_text())
        self.assertTrue(status['disk_limit_reached'])
        self.assertGreater(status['dropped'],0)
        self.assertTrue((rec.directory/'events-00001.jsonl').exists())

    def test_invalid_json_is_recorded_without_fabricating_an_observation(self):
        rec=self.recorder();ticket=rec.begin()
        rec.submit(ticket,b'not json',b'{"roleCommandMap":{}}',invalid_input=True)
        row=self.turns(rec)[0]
        self.assertIsNone(row['request'])
        self.assertTrue(row['invalid_input'])
        self.assertEqual(row['request_decode_error'],'JSONDecodeError')
        self.assertFalse(row['semantic_replay_exact'])

    def test_torn_tail_is_visible(self):
        part=self.root/'partial.jsonl'
        part.write_bytes((json.dumps({'schema':t.SCHEMA,'event':'start','run_id':'r'})+'\n').encode()+b'{"truncated":')
        report=trace_tool.inspect(part)
        self.assertEqual(report['quality']['incomplete_lines'],1)
        self.assertFalse(report['quality']['capture_complete'])

    def test_torn_tail_and_missing_index_prevent_exact_prefix_claim(self):
        rec=self.recorder()
        self.add(rec,observation());self.turns(rec)
        part=next(rec.directory.glob('events-*.jsonl'))
        with part.open('ab') as stream:stream.write(b'{"torn":')
        with patch('agent.brain.respond',return_value={'roleCommandMap':{}}):
            result=trace_tool.replay(rec.directory,self.root/'partial-replay.jsonl')
        self.assertFalse(result['complete_prefix_from_round_1'])
        self.assertEqual(result['source_quality']['incomplete_lines'],1)

    def test_disk_write_failure_disables_capture_and_keeps_submit_nonblocking(self):
        with patch.object(t.Recorder,'_write',side_effect=OSError('disk failure')):
            rec=self.recorder()
            rec.thread.join(timeout=3)
        ticket=rec.begin()
        self.assertFalse(rec.submit(ticket,b'{}',b'{"roleCommandMap":{}}'))
        self.assertEqual(rec.status()['disabled_reason'],'OSError')
        self.assertEqual(rec.status()['dropped'],1)

    def test_packaging_includes_tools_but_excludes_local_traces(self):
        from build_submission import select_files
        accepted,_=select_files(['tools/trace_tool.py','docs/TRACE_LOGGING.md',
                                'Demo/CoreGeek/src/agent/telemetry.py','logs/run/events-00001.jsonl'])
        self.assertEqual(len(accepted),3)
        self.assertNotIn('logs/run/events-00001.jsonl',accepted)

    def test_real_planner_sample_replays_in_fresh_process(self):
        # Independent source is the unchanged official sample, not a simulator-produced expected answer.
        code='''
import copy,json,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'Demo/CoreGeek/src'))
from agent import brain,telemetry
from agent.scenarios import observation
raw=json.loads(Path('docs/request.txt').read_text(encoding='utf-8'))
rec=telemetry.Recorder(Path(sys.argv[1]),{'test':'official-sample-policy-replay'})
for n in (1,2,3):
    request=copy.deepcopy(raw);request['roundNo']=n
    response=brain.respond(observation(request))
    rec.submit(rec.begin(),json.dumps(request).encode(),json.dumps(response).encode())
assert rec.close()
'''
        directory=self.root/'real-planner'
        result=subprocess.run([sys.executable,'-c',code,str(directory)],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        result=subprocess.run([sys.executable,str(ROOT/'tools/trace_tool.py'),'replay',str(directory),
                               '--output',str(self.root/'real-replay.jsonl')],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['matched'],3)
        self.assertEqual(report['mismatched'],0)

    def test_export_and_observed_transitions_keep_exact_round_pairing(self):
        rec=self.recorder()
        for n in (1,2,4):self.add(rec,observation(n))
        self.turns(rec)
        archive=self.root/'window.zip'
        result=trace_tool.export(rec.directory,archive,2,4)
        self.assertEqual(result['turns'],3)  # Includes round 1 to pair feedback for 2.
        self.assertFalse(trace_tool.inspect(archive)['quality']['has_end'])
        output=self.root/'transitions.jsonl'
        result=trace_tool.transitions(archive,output)
        self.assertEqual(result['transitions'],1)
        row=json.loads(output.read_text())
        self.assertEqual(row['round'],1)
        self.assertEqual(row['next_observation']['roundNo'],2)
        with self.assertRaises(trace_tool.TraceError):trace_tool.export(rec.directory,archive)

    def test_policy_replay_does_not_execute_response_channels(self):
        rec=self.recorder();expected={'roleCommandMap':{},'executeCmd':'do-not-execute-this'}
        self.add(rec,observation(),expected);self.turns(rec)
        with patch('agent.brain.respond',return_value=expected) as responder:
            result=trace_tool.replay(rec.directory,self.root/'replay.jsonl')
        self.assertEqual(result['matched'],1)
        responder.assert_called_once()
        self.assertTrue(result['complete_prefix_from_round_1'])

    def test_compare_distinguishes_round_alignment_from_state_differences(self):
        a,b=self.root/'actual.json',self.root/'predicted.json'
        a.write_text(json.dumps(observation(1,health=200)))
        b.write_text(json.dumps(observation(2,health=220)))
        self.assertFalse(trace_tool.compare(a,b)['aligned'])
        b.write_text(json.dumps(observation(1,health=220)))
        report=trace_tool.compare(a,b)
        self.assertEqual(report['difference']['teamOur']['changed']['2']['health'],{'before':200,'after':220})

    def test_trace_failure_cannot_change_http_response(self):
        expected={'roleCommandMap':{'2':{'action':'move','targetPos':[{'x':8,'y':22}]}}}
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            for function in ('begin','submit'):
                with patch.object(server,'respond',return_value=expected), patch.object(t,function,side_effect=RuntimeError('trace failure')):
                    req=Request(f'http://127.0.0.1:{http.server_port}/',data=json.dumps(observation()).encode(),headers={'Content-Type':'application/json'})
                    with urlopen(req,timeout=5) as response:
                        self.assertEqual(response.status,200)
                        self.assertEqual(json.load(response),expected)
        finally:
            http.shutdown();http.server_close();thread.join(timeout=5)

    def test_live_http_trace_has_wire_data_and_no_authorization_header(self):
        rec=self.recorder()
        expected={'roleCommandMap':{}}
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        try:
            with patch.object(t,'_recorder',rec), patch.object(server,'respond',return_value=expected):
                req=Request(f'http://127.0.0.1:{http.server_port}/',data=json.dumps(observation()).encode(),
                            headers={'Content-Type':'application/json','Authorization':'Bearer PRIVATE_HEADER_ONLY'})
                with urlopen(req,timeout=5) as response:self.assertEqual(json.load(response),expected)
                http.shutdown();http.server_close();thread.join(timeout=5)
        finally:
            if thread.is_alive():http.shutdown();http.server_close();thread.join(timeout=5)
        rows=self.turns(rec)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['request'],observation())
        self.assertEqual(rows[0]['response'],expected)
        self.assertTrue(rows[0]['response_sent'])
        self.assertNotIn('PRIVATE_HEADER_ONLY',json.dumps(rows))


class DecisionTraceTests(unittest.TestCase):
    setUp=TraceTests.setUp
    recorder=TraceTests.recorder
    turns=TraceTests.turns
    add=TraceTests.add
    def test_explanation_is_frozen_per_request_and_absent_from_wire(self):
        rec=self.recorder()
        decision={'supervisor':{'mode':'defend','reserve_pioneer':True,'reason':'base_damaged'},
                  'task':{'phase':'paused_for_defence'}}
        ticket=rec.begin()
        rec.submit(ticket,json.dumps(observation()).encode(),b'{"roleCommandMap":{}}',decision=decision)
        decision['supervisor']['mode']='mutated-after-submit'
        rows=self.turns(rec)
        self.assertEqual(rows[0]['decision']['supervisor']['mode'],'defend')
        self.assertEqual(rows[0]['summary']['decision']['supervisor']['mode'],'defend')
        self.assertEqual(rows[0]['response'],{'roleCommandMap':{}})

    def test_console_off_still_records_every_turn(self):
        rec=self.recorder(queue_size=64)
        with patch.dict('os.environ',{'COMPETITION_HW_CONSOLE':'off'}):
            for n in range(1,26):self.add(rec,observation(n))
        self.assertEqual([r['round'] for r in self.turns(rec)],list(range(1,26)))

    def test_large_explanation_is_bounded_without_losing_wire_data(self):
        rec=self.recorder()
        rec.submit(rec.begin(),json.dumps(observation()).encode(),b'{"roleCommandMap":{}}',decision={'x':'a'*10000})
        row=self.turns(rec)[0]
        self.assertEqual(row['decision'],{'unavailable':'decision_size_limit'})
        self.assertEqual(row['response'],{'roleCommandMap':{}})
