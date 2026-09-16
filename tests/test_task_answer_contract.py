"""Source-derived structure and strict presentation wrappers, not grading oracles."""
import json
import unittest
from copy import deepcopy
import test_task_pipeline as support
from agent import task_answer_contract as contracts, local_task_sandbox
from agent.task_agent import TaskAgent
from agent.model_json import unwrap_json
from agent.llm_router import verify_json_token

TASK='通过submitAnswer提交答案，形式：\n```\n{"token":"xxx"}\n```'


class ContractTests(unittest.TestCase):
    def test_token_format_repair_requires_actual_successful_check(self):
        contract=contracts.derive(TASK,'task-doc')
        event={'command':'cd /tmp/work && ./check','verified':True,'exit_code':0,
               'truncated':False,'text':'[ OK ] 全部通过 (6/6)\nTOKEN: fresh-value-123\n'}
        ok,value,_=contracts.validate('fresh-value-123',contract,[event])
        self.assertTrue(ok);self.assertEqual(json.loads(value),{'token':'fresh-value-123'})
        for change in ({'exit_code':1},{'truncated':True},{'command':'cat /tmp/check'},
                       {'text':'[FAIL] failed\nTOKEN: fresh-value-123'},{'text':'TOKEN: example-value'}):
            self.assertFalse(contracts.validate('fresh-value-123',contract,[{**event,**change}])[0])
        self.assertFalse(contracts.validate('stale-value',contract,[event])[0])

    def test_api_contract_prevents_scalar_and_missing_fields_not_wrong_numbers(self):
        contract=contracts.derive('输出格式：{"city":"","total_count":0,"types":[]}', 'task')
        for answer in ('15','南京','{}','{"city":"a","total_count":true,"types":[]}'):
            self.assertFalse(contracts.validate(answer,contract,[])[0])
        exact=' {"city":"new","total_count":17,"types":[]}\n'
        self.assertEqual(contracts.validate(exact,contract,[])[:2],(True,exact))
        self.assertFalse(contracts.validate('{"city":"a","city":"b","total_count":2,"types":[]}',contract,[])[0])

    def test_only_named_task_file_not_api_response_examples_defines_contract(self):
        docs=[{'id':'api','path':'/tmp/API_DOCS.md','text':TASK,'verified':True,'exit_code':0,'truncated':False}]
        self.assertIsNone(contracts.for_task('请阅读task_A.md，获取任务信息',docs))
        docs.append({**docs[0],'id':'task','path':'/tmp/task_A.md'})
        self.assertEqual(contracts.for_task('请阅读task_A.md，获取任务信息',docs)['fields'],{'token':'str'})
        docs.append({**docs[-1],'truncated':True})
        self.assertIsNone(contracts.for_task('请阅读task_A.md，获取任务信息',docs))

    def test_unknown_numeric_and_conflicting_contracts_are_not_invented(self):
        self.assertIsNone(contracts.derive('只返回保留一位小数的数字字符串','task'))
        self.assertTrue(contracts.validate('0.3',None,[])[0])
        contract=contracts.derive('形式：{"a":1}\n形式：{"b":"x"}','task')
        self.assertEqual(contract['kind'],'ambiguous')

    def test_strict_single_fence_keeps_nonce_and_duplicate_key_checks(self):
        text='{"request_id":"abc123456789","plan":{"kind":"run","command":"ls"}}'
        wrapped='```json\n'+text+'\n```'
        self.assertEqual(unwrap_json(wrapped),text)
        self.assertTrue(verify_json_token(wrapped,'abc123456789',allow_fence=True))
        self.assertFalse(verify_json_token(wrapped,'wrong',allow_fence=True))
        for bad in ('note\n'+wrapped,wrapped+'\nextra',wrapped+'\n'+wrapped,
                    '```json\n'+text,wrapped.replace('"abc123456789"','"wrong","request_id":"abc123456789"')):
            self.assertFalse(verify_json_token(bad,'abc123456789',allow_fence=True))
        self.assertFalse(verify_json_token(wrapped,'abc123456789'))

    def test_observed_heredoc_family_and_valid_literal_content(self):
        self.assertTrue(contracts.bad_heredoc_chain("cat <<'EOF'\nhello\nEOF\n&& echo bad"))
        self.assertTrue(contracts.bad_heredoc_chain("cat <<-EOF\nhello\n\tEOF\n&& echo bad"))
        for valid in ("cat <<'EOF'\n&& literal\nEOF\necho ok",
                      'x="cat <<EOF\nEOF\n&& literal"',
                      'cat <<EOF\n  EOF\n&& literal\nEOF'):
            self.assertFalse(contracts.bad_heredoc_chain(valid))

    def test_exact_confirmed_syntax_failure_is_not_reissued(self):
        agent=TaskAgent();agent.begin('g','source')
        ask=agent.decide('ctx',['source'],active=True,round_no=1);agent.acknowledge(ask['token'],round_no=1)
        def reply(ask):return json.dumps({'request_id':ask['token'],'plan':{'kind':'run','command':'broken command','evidence_ids':['source']}})
        agent.receive(ask['token'],'prompt',reply(ask),round_no=2,verified=True)
        cmd=agent.proposal;agent.acknowledge(cmd['token'],round_no=2)
        agent.receive(cmd['token'],'cmd','[exitCode:2]\nsyntax error',round_no=3,verified=True)
        ask=agent.decide('ctx',['source'],active=True,round_no=3);agent.acknowledge(ask['token'],round_no=3)
        self.assertFalse(agent.receive(ask['token'],'prompt',reply(ask),round_no=4,verified=True))
        self.assertEqual(agent.commands,1)
        self.assertIsNone(agent.proposal)


class ContractPipelineTests(unittest.TestCase):
    setUp=support.PipelineTests.setUp
    step=support.PipelineTests.step
    answer=support.PipelineTests.answer

    def prepare(self):
        self.fixture['files']['/tmp/unseen/work/task_varied.md']=TASK
        first=self.step()
        return self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True))

    def test_fenced_command_and_bare_verified_token_submit_without_extra_round(self):
        for side in ('challenger','defender'):
            self.state=support.planner.PlannerState();self.round=1
            self.fixture['files']['/tmp/unseen/work/task_varied.md']=TASK
            first=self.step(side=side)
            prompt=self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True),side=side)
            self.assertIn('本题提交契约',prompt['prompt'])
            reply='```json\n'+self.answer('run','cd /tmp/unseen/work && ./check')+'\n```'
            command=self.step(llm=reply,side=side)
            self.assertIn('executeCmd',command)
            self.assertTrue(command['executeCmd'].startswith('# task-check/1\n'))
            self.step(cmd='[exitCode:0]\n'+json.dumps({'tool':'task-check/1','exit_code':0,'truncated':False,
                'stdout':'[ OK ] 全部通过 (6/6)\nTOKEN: fresh-token-123'}),side=side)
            submitted=self.step(llm=self.answer('answer','fresh-token-123'),side=side)
            answer=next(c['taskAnswer'] for c in submitted['roleCommandMap'].values() if c['action']=='submitAnswer')
            self.assertEqual(json.loads(answer),{'token':'fresh-token-123'})
            self.assertNotIn('prompt',submitted)
            self.assertEqual(self.state.team_agent.task.answers,1)

    def test_wrong_shape_replans_locally_without_emitting_invalid_submission(self):
        self.fixture['files']['/tmp/unseen/work/task_varied.md']='输出格式：{"city":"","total_count":0}'
        first=self.step();self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True))
        repair=self.step(llm=self.answer('answer','15'))
        self.assertIn('prompt',repair)
        self.assertIn('answer_contract_rejected',repair['prompt'])
        self.assertFalse(any(c['action']=='submitAnswer' for c in repair['roleCommandMap'].values()))
        corrected=self.step(llm=self.answer('answer','{"city":"new","total_count":2}'))
        self.assertTrue(any(c['action']=='submitAnswer' for c in corrected['roleCommandMap'].values()))

    def test_issue26_instruction_echo_is_rejected_and_corrected_on_both_sides(self):
        # Issue 26 quotes this bad answer from source 1831136d. The explicit
        # format below is an independent fixture, not an unavailable log replay.
        echo='requirement: 遵守题目原文指定的格式、字段与单位'
        for side in ('challenger','defender'):
            for invalid in (echo,json.dumps(echo,ensure_ascii=False)):
                with self.subTest(side=side,answer=invalid):
                    self.state=support.planner.PlannerState();self.round=1
                    self.fixture['files']['/tmp/unseen/work/task_varied.md']='输出格式：{"city":"","total_count":0}'
                    first=self.step(side=side)
                    self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True),side=side)
                    rejected=self.step(llm=self.answer('answer',invalid),side=side)
                    self.assertFalse(any(c['action']=='submitAnswer' for c in rejected['roleCommandMap'].values()))
                    self.assertIn('answer_contract_rejected',rejected['prompt'])
                    self.assertEqual(self.state.team_agent.task.answers,0)
                    exact=' {"city":"合成测试城市","total_count":17}\n'
                    repaired=self.step(llm=self.answer('answer',exact),side=side)
                    issued=[c for c in repaired['roleCommandMap'].values() if c['action']=='submitAnswer']
                    self.assertEqual(issued,[{'action':'submitAnswer','taskAnswer':exact}])
                    self.assertNotIn('prompt',repaired)
                    self.assertEqual(self.state.team_agent.task.answers,1)
                    self.assertLess(self.round,15)
