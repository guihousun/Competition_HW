"""Hand-authored Issue45 transition probes, not company-request replay.

Drive the actual brain, planner cache and router; round-trip all public requests,
responses and PlannerState through JSON. Model texts/tool results are controlled
fixtures, not real LLM accuracy or official judging evidence.
The narrow guard can identify only retained, previously quarantined old outputs;
unseen late outputs or evicted evidence remain ambiguous without command nonces.
Identical ambiguous bytes may also be a genuine new result: wait/expire rather
than infer an answer, and never globally deduplicate legitimate successful reads.
"""
import json
import os
from pathlib import Path
import sys
import unittest
from copy import deepcopy
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Demo/CoreGeek/src'))
from agent import brain, planner
from test_tasks import observation

BETA = 'Complete the beta workspace exercise using current documentation and service. Return a JSON object.'
NANJING = 'Query nanjing data using current documentation and service. Return a JSON object.'
OLD_RESULT = '[exitCode:0]\nBETA_OLD_RESULT_SENTINEL: {"beta_only":"old-value"}'


def wire(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


class Issue45TaskTransitionsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on',
            brain.ROUTER_ENV: 'on', brain.WORLD_AGENT_ENV: 'off'})
        self.env.start(); self.addCleanup(self.env.stop)
        planner.reset(); self.addCleanup(planner.reset)
        self.rows = []

    def step(self, side, round_no, text, *, llm='', tool=''):
        payload = observation(round_no=round_no, team=side, role_pos=(6,5),
            phase_task=text, llm_resp=llm, cmd_result=tool, timeout_rounds=30)
        payload['teamOur']['teamId'] = 'issue45-' + side
        # A running point cannot be reaccepted even when a stale result arrives.
        payload['teamOur']['playerTasks'][0]['isValid'] = False
        response = wire(brain.respond(wire(payload)))
        state = planner.state_for(payload)
        restored = planner.PlannerState.load(wire(state.dump()))
        self.assertIsNone(restored.degraded)
        planner._STATES[planner._match_key(payload)] = restored
        self.rows.append({'side':side,'round':round_no,'question':text,
            'response':response,'memory':wire(restored.dump())})
        if text:
            self.assertFalse(any(c.get('action')=='acceptTask'
                for c in response['roleCommandMap'].values()), 'never reaccept a published active task')
        return response, restored

    def model(self, state, kind, value):
        pending = state.team_agent.task.pending
        self.assertIsNotNone(pending)
        self.assertEqual(pending['kind'],'prompt')
        field = 'command' if kind=='run' else 'answer'
        return json.dumps({'request_id':pending['token'],'plan':{
            'kind':kind,field:value,'reason':'hand-authored current-task evidence',
            'evidence_ids':pending['evidence_ids']}})

    def new_task_after_empty(self, side):
        first, state = self.step(side,1,BETA)
        self.assertIn('prompt',first)
        old_answer = self.model(state,'answer','{"beta_only":"old-answer"}')
        _,state=self.step(side,2,BETA,llm=self.model(state,'run','cat /tmp/beta/README.md'))
        self.assertEqual(state.team_agent.task.pending['kind'],'cmd')
        old_generation=state.team_agent.task.generation
        response,state=self.step(side,3,'',tool=OLD_RESULT)
        self.assertIsNone(state.tasks.get('cycle'))
        self.assertIsNone(state.llm_router.pending['cmd'])
        self.assertFalse(any(r.status=='received' and r.text==OLD_RESULT
            for r in state.llm_router.receipts.values()))
        response,state=self.step(side,4,NANJING,tool=OLD_RESULT)
        self.assertIn('prompt',response)
        self.assertEqual(state.tasks['cycle'].description,NANJING)
        self.assertNotEqual(state.team_agent.task.generation,old_generation)
        self.assertEqual(state.team_agent.task.prompts,1)
        self.assertEqual(state.team_agent.task.commands,0)
        self.assertFalse(state.team_agent.evidence)
        return response,state,old_answer

    def test_published_new_task_survives_old_model_and_tool_repeats(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,old_answer=self.new_task_after_empty(side)
                generation=state.team_agent.task.generation
                response,state=self.step(side,5,NANJING,llm=old_answer,tool=OLD_RESULT)
                self.assertEqual(state.tasks['cycle'].description,NANJING)
                self.assertEqual(state.team_agent.task.generation,generation)
                self.assertEqual(state.team_agent.task.prompts,1)
                self.assertIsNotNone(state.team_agent.task.pending)
                self.assertEqual(state.team_agent.task.answers,0)
                self.assertFalse(state.team_agent.evidence)
                self.assertFalse(any(c.get('action')=='submitAnswer' for c in response['roleCommandMap'].values()))

    def test_old_round_observation_does_not_reset_new_task(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                before=wire(state.dump())
                response,state=self.step(side,2,BETA,tool=OLD_RESULT)
                self.assertEqual(response,{'roleCommandMap':{}})
                # Delayed packets still cannot mutate task/channel history.
                # The new clearance permission is deliberately revoked on a
                # backwards observation; no old packet grants night work.
                before.pop('clearedNightState',None)
                self.assertEqual(state.cleared_night_state,{})
                self.assertEqual(state.dump(),before)

    def test_old_tool_repeat_cannot_become_new_task_evidence(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                generation=state.team_agent.task.generation
                response,state=self.step(side,5,NANJING,llm=self.model(state,'run','cat /tmp/nanjing/README.md'))
                self.assertEqual(response.get('executeCmd'),'cat /tmp/nanjing/README.md')
                self.assertEqual(state.team_agent.task.pending['kind'],'cmd')
                response,state=self.step(side,6,NANJING,tool=OLD_RESULT)
                self.assertEqual(state.tasks['cycle'].description,NANJING)
                self.assertEqual(state.team_agent.task.generation,generation)
                self.assertFalse(any(OLD_RESULT.split('\n',1)[1] in e.get('text','')
                    for e in state.team_agent.evidence), 'old beta tool receipt must not become nanjing evidence')
                self.assertNotIn('BETA_OLD_RESULT_SENTINEL', response.get('prompt',''))
                self.assertEqual(state.team_agent.task.answers,0)

    def test_new_task_also_ends_while_old_tool_reply_repeats(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                response,state=self.step(side,5,NANJING,llm=self.model(state,'run','cat /tmp/nanjing/README.md'))
                self.assertIn('executeCmd',response)
                response,state=self.step(side,6,'',tool=OLD_RESULT)
                self.assertIsNone(state.tasks.get('cycle'))
                self.assertEqual(state.team_agent.task.stage,'ended')
                self.assertIsNone(state.llm_router.pending['cmd'])
                self.assertFalse(state.team_agent.evidence)
                response,state=self.step(side,7,'',tool=OLD_RESULT)
                self.assertIsNone(state.tasks.get('cycle'))
                self.assertFalse(state.team_agent.evidence)
                self.assertFalse(any(c.get('action') in ('acceptTask','submitAnswer')
                    for c in response['roleCommandMap'].values()))


    def test_quarantined_old_output_does_not_consume_real_new_result(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                _,state=self.step(side,5,NANJING,llm=self.model(state,'run','cat /tmp/nanjing/README.md'))
                generation=state.team_agent.task.generation
                command_id=state.llm_router.pending['cmd'].request_id
                _,state=self.step(side,6,NANJING,tool=OLD_RESULT)
                self.assertIsNotNone(state.llm_router.pending['cmd'])
                self.assertEqual(state.llm_router.pending['cmd'].request_id,command_id)
                self.assertTrue(any(r.reason=='ambiguous_prior_task_output'
                    for r in state.llm_router.receipts.values()))
                fresh='[exitCode:0]\nNANJING_CURRENT_DOCUMENT_SENTINEL'
                response,state=self.step(side,7,NANJING,tool=fresh)
                self.assertEqual(state.team_agent.task.generation,generation)
                self.assertEqual(state.tasks['cycle'].description,NANJING)
                self.assertIsNone(state.llm_router.pending['cmd'])
                self.assertEqual(state.team_agent.evidence[-1]['text'],fresh.split('\n',1)[1])
                self.assertIn('NANJING_CURRENT_DOCUMENT_SENTINEL',response.get('prompt',''))
                self.assertNotIn('BETA_OLD_RESULT_SENTINEL',response.get('prompt',''))
                self.assertEqual(state.team_agent.task.answers,0)

    def test_ambiguous_repeated_output_respects_original_wait_deadline(self):
        from agent.llm_router import WAIT_ROUNDS
        self.assertEqual(WAIT_ROUNDS,3,'do not shorten the official-channel wait to fix contamination')
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                _,state=self.step(side,5,NANJING,llm=self.model(state,'run','cat /tmp/nanjing/README.md'))
                generation=state.team_agent.task.generation
                command_id=state.llm_router.pending['cmd'].request_id
                # Preserve the existing NONEMPTY receipt window through N+3.
                # Empty receipts already expire at >=N+3; this test does not
                # globally change that pre-existing distinction.
                for round_no in (6,7,8):
                    response,state=self.step(side,round_no,NANJING,tool=OLD_RESULT)
                    self.assertEqual(state.llm_router.pending['cmd'].request_id,command_id)
                    self.assertEqual(state.llm_router.pending['cmd'].sent_round,5)
                    self.assertFalse(state.team_agent.evidence)
                response,state=self.step(side,5+WAIT_ROUNDS+1,NANJING,tool=OLD_RESULT)
                self.assertEqual(state.team_agent.task.generation,generation)
                self.assertEqual(state.tasks['cycle'].description,NANJING)
                self.assertIsNone(state.llm_router.pending['cmd'])
                self.assertTrue(any(r.request_id==command_id and r.status=='expired'
                    for r in state.llm_router.receipts.values()))
                self.assertFalse(state.team_agent.evidence)
                self.assertEqual(state.team_agent.task.answers,0)


    def test_true_new_result_can_arrive_on_last_nonempty_receipt_round(self):
        from agent.llm_router import WAIT_ROUNDS
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                _,state,_=self.new_task_after_empty(side)
                _,state=self.step(side,5,NANJING,llm=self.model(state,'run','cat /tmp/nanjing/README.md'))
                generation=state.team_agent.task.generation
                command_id=state.llm_router.pending['cmd'].request_id
                for round_no in (6,7):
                    _,state=self.step(side,round_no,NANJING,tool=OLD_RESULT)
                    self.assertEqual(state.llm_router.pending['cmd'].request_id,command_id)
                    self.assertEqual(state.llm_router.pending['cmd'].sent_round,5)
                fresh='[exitCode:0]\nNANJING_LAST_VALID_ROUND_SENTINEL'
                response,state=self.step(side,5+WAIT_ROUNDS,NANJING,tool=fresh)
                self.assertEqual(state.team_agent.task.generation,generation)
                self.assertIsNone(state.llm_router.pending['cmd'])
                received=[r for r in state.llm_router.receipts.values()
                    if r.request_id==command_id and r.status=='received']
                self.assertEqual(len(received),1)
                self.assertEqual(received[0].round_no,8)
                self.assertEqual(state.team_agent.evidence[-1]['text'],fresh.split('\n',1)[1])
                self.assertIn('NANJING_LAST_VALID_ROUND_SENTINEL',response.get('prompt',''))
                self.assertNotIn('BETA_OLD_RESULT_SENTINEL',response.get('prompt',''))
                self.assertEqual(state.team_agent.task.answers,0)

    def test_successfully_received_identical_output_is_not_globally_deduplicated(self):
        from agent.llm_router import LLMRouter
        from agent.sandbox import JudgeState
        from agent.task_context import TaskConfirmation
        router=LLMRouter(JudgeState())
        result='[exitCode:0]\nidentical legitimate README contents'
        def confirmation(generation):
            return TaskConfirmation(True,generation,'source-'+generation,'confirmed',{})
        for generation,round_no in (('beta',1),('nanjing',3)):
            router.note_round(round_no)
            request=router.offer('task',generation,'source-'+generation,kind='cmd',
                payload='cat /tmp/'+generation+'/README.md',nonce='',expected_result_shape='command_output')
            chosen=router.select({'roundNo':round_no},confirmed_task=confirmation(generation))
            self.assertIs(chosen,request)
            self.assertTrue(router.mark_emitted(chosen,round_no=round_no))
            router.ingest({'roundNo':round_no+1,'lastCmdResult':result},confirmed_task=confirmation(generation))
            receipts=[r for r in router.receipts.values() if r.request_id==request.request_id]
            self.assertEqual(receipts[-1].status,'received')
            self.assertIsNone(router.pending['cmd'])
        self.assertEqual(len(router.received_results('cmd')),2)


if __name__=='__main__':
    unittest.main()
