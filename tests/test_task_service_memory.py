"""Source-gated HTTP methods and truthful submission diagnostics."""
import hashlib,json,unittest
from copy import deepcopy
import test_task_pipeline as support
from agent import local_task_sandbox
from agent.team_agent import TeamAgent
from agent.task_journal import TaskJournal


class ServiceMemoryTests(unittest.TestCase):
    setUp=support.PipelineTests.setUp
    step=support.PipelineTests.step

    def prepare(self):
        self.fixture['files']['/tmp/unseen/work/API_DOCS.md']='GET http://127.0.0.1:8899/query with documented header and city parameter.'
        first=self.step()
        self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True))

    def response(self,kind,args):
        pending=self.state.team_agent.task.pending
        return json.dumps({'request_id':pending['token'],'plan':{
            'kind':kind,'tool_args':args,'evidence_ids':pending['evidence_ids']}})

    def http_receipt(self):
        return '[exitCode:0]\n'+json.dumps({'tool':'task-http/1','endpoint':'http://127.0.0.1:8899/query',
            'status':200,'data':{'total':2},'truncated':False,
            'profile':{'endpoint':'http://127.0.0.1:8899/query','auth':'bearer','aliases':{'city':'location'}}})

    def test_structured_tool_learns_only_correlated_document_bound_profile(self):
        self.prepare()
        command=self.step(llm=self.response('http',{'url':'http://127.0.0.1:8899/query',
            'headers':{'X-API-Key':'secret-fixture'},'params':{'city':'old-city'}}))
        self.assertTrue(command['executeCmd'].startswith('# task-http/1'))
        prompt=self.step(cmd=self.http_receipt())
        stored=self.state.team_agent.http_profiles
        self.assertEqual(len(stored),1)
        text=json.dumps(stored)
        self.assertNotIn('secret-fixture',text)
        self.assertNotIn('old-city',text)
        self.assertIn('city',text)
        self.assertIn('location',prompt['prompt'])
        owner=self.state.team_agent.owner
        saved=self.state.team_agent.dump()
        self.assertFalse(TeamAgent.load(json.loads(json.dumps(saved)),owner).degraded)
        self.assertTrue(TeamAgent.load(saved,'other-team').degraded)
        bad=deepcopy(saved);bad['httpProfiles'][0]['profile']['api_key']='secret'
        self.assertTrue(TeamAgent.load(bad,owner).degraded)
        old=deepcopy(saved);old['schema']='competition-team-agent/3';old.pop('httpProfiles')
        restored=TeamAgent.load(old,owner)
        self.assertFalse(restored.degraded)
        self.assertEqual(restored.http_profiles,[])

    def test_changed_document_does_not_revalidate_old_method(self):
        self.prepare()
        self.step(llm=self.response('http',{'url':'http://127.0.0.1:8899/query','params':{'city':'a'}}))
        self.step(cmd=self.http_receipt())
        # An explicit current read with different contents invalidates old hints.
        pending=self.state.team_agent.task.pending
        reply=json.dumps({'request_id':pending['token'],'plan':{'kind':'run',
            'command':'cat /tmp/unseen/work/API_DOCS.md','evidence_ids':pending['evidence_ids']}})
        self.step(llm=reply)
        prompt=self.step(cmd='[exitCode:0]\nGET http://127.0.0.1:8899/query now uses another API version.')
        self.assertNotIn('同文档重新核验的HTTP方法',prompt['prompt'])

    def test_unassociated_receipt_cannot_create_service_memory(self):
        self.prepare()
        # Current pending is prompt, so raw command result has no matching owner/operation.
        self.step(cmd=self.http_receipt())
        self.assertEqual(self.state.team_agent.http_profiles,[])

    def test_check_is_a_command_not_an_answer(self):
        self.prepare()
        reply=self.step(llm=self.response('check',{'path':'/tmp/work/check'}))
        self.assertTrue(reply['executeCmd'].startswith('# task-check/1'))
        self.assertFalse(any(c['action']=='submitAnswer' for c in reply['roleCommandMap'].values()))

    def test_new_episode_reuses_only_revalidated_service_method(self):
        self.prepare()
        issued = self.step(llm=self.response('http', {'url':'http://127.0.0.1:8899/query',
            'headers':{'X-API-Key':'fixture-key'},'params':{'city':'old-city'}}))
        fixture = {'http_services':{'http://127.0.0.1:8899/query':{
            'bearer_key':'fixture-key','parameter':'location','records':{'old-city':{'total':2}}}}}
        result = local_task_sandbox.execute(issued['executeCmd'], fixture, active=True)
        self.assertEqual([a['status'] for a in json.loads(result.split('\n',1)[1])['attempts']], [401,400,200])
        self.step(cmd=result)
        self.assertEqual(len(self.state.team_agent.http_profiles),1)
        self.step(description='')
        first=self.step()
        self.assertIn('executeCmd',first)
        prompt=self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True))
        self.assertIn('同文档重新核验的HTTP方法',prompt['prompt'])
        self.assertEqual(self.state.team_agent.task.inspections,0)
        self.assertEqual(self.state.team_agent.summary()['httpMethodCount'],1)

    def test_new_auth_failure_invalidates_old_hint(self):
        self.prepare()
        self.step(llm=self.response('http',{'url':'http://127.0.0.1:8899/query','params':{'city':'a'}}))
        self.step(cmd=self.http_receipt())
        self.step(llm=self.response('http',{'url':'http://127.0.0.1:8899/query','params':{'city':'b'}}))
        self.step(cmd='[exitCode:0]\n'+json.dumps({'tool':'task-http/1','status':401,'data':{},'profile':None,'truncated':False}))
        self.assertEqual(self.state.team_agent.http_profiles,[])


class SubmissionEvidenceTests(unittest.TestCase):
    def payload(self,n,task,gold,score,**extra):
        return {'roundNo':n,'phaseTask':task,
                'teamOur':{'type':'challenger','gold':gold,'totalScore':score,'roles':[]},**extra}

    def test_disappearance_after_submission_reports_delta_without_pass(self):
        journal=TaskJournal()
        answer='{"token":"fixture-token"}'
        journal.observe(self.payload(10,'task',0,10),
            {'roleCommandMap':{'42':{'action':'submitAnswer','taskAnswer':answer}}})
        rows=journal.observe(self.payload(11,'',80,95,lastRoundRoleActionResults={'42':True}),{})
        item=json.loads(next(r for r in rows if r['kind']=='task_text_ended')['content']['text'])
        self.assertFalse(item['official_success_confirmed'])
        self.assertEqual(item['outcome'],'unknown_without_judge_feedback')
        self.assertEqual(item['last_submission']['round'],10)
        self.assertEqual(item['last_submission']['answer_sha256'],hashlib.sha256(answer.encode()).hexdigest())
        self.assertEqual(item['change_since_previous_observation'],{'gold':80,'totalScore':85})
        self.assertTrue(item['action_receipt'])
        self.assertEqual(item['change_is_task_reward'],'unknown_other_actions_may_contribute')

    def test_gap_and_no_submission_never_imply_success(self):
        journal=TaskJournal()
        journal.observe(self.payload(1,'task',75,0),{})
        rows=journal.observe(self.payload(20,'',80,91,errors=[{'errorCode':2}]),{})
        item=json.loads(next(r for r in rows if r['kind']=='task_text_ended')['content']['text'])
        self.assertTrue(item['observation_gap'])
        self.assertIsNone(item['last_submission'])
        self.assertFalse(item['official_success_confirmed'])
