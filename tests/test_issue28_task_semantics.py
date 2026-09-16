"""Current-document semantics, varied synthetic data, no historical answer lookup."""
import json
import sys
from pathlib import Path
import unittest
from copy import deepcopy

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'Demo/CoreGeek/src'))
from agent import task_answer_contract as contract,task_tools,local_task_sandbox
import test_task_pipeline as support

TEMPLATE='''提交以下JSON格式的统计信息：
```json
{
  "city": "示例城市",
  "total_count": <总记录条数>,
  "types": [<所有不重复的类型数组>],
  "oldest_era": <年代最早的遗产名称>
}
```
'''


def data_doc(total=2):
    command=task_tools.command('http',{'url':'http://localhost:8899/query','params':{'location':'雾城'}})
    records=[{'name':'遗址甲','era':'旧石器时代','type':'遗址'},
             {'name':'园区乙','era':'晋','type':'园林'}]
    result={'tool':'task-http/1','status':200,'truncated':False,'data':{'code':200,'data':{
        'records':records,'pagination':{'total_count':total,'offset':0}}}}
    return {'id':'current-query','verified':True,'exit_code':0,'truncated':False,
            'command':command,'text':json.dumps(result,ensure_ascii=False)}


def answer(value):
    return json.dumps({'city':'雾城','total_count':2,'types':['遗址','园林'],'oldest_era':value},ensure_ascii=False)


class TaskSemanticsTests(unittest.TestCase):
    def test_placeholder_template_retains_shape_and_meaning_not_sample_answer(self):
        c=contract.derive(TEMPLATE,'task-doc')
        self.assertEqual(c['kind'],'object')
        self.assertEqual(c['fields'],{'city':'str','total_count':'NoneType','types':'list','oldest_era':'NoneType'})
        self.assertEqual(c['semantics']['oldest_era'],'年代最早的遗产名称')
        self.assertNotIn('示例城市',json.dumps(c,ensure_ascii=False))

    def test_literal_comments_and_quoted_url_are_not_confused(self):
        text='输出格式：\n{\n "url":"http://example.invalid/a",\n "earliest":"", // 年代最早的项目名称\n "count":0\n}'
        c=contract.derive(text,'t')
        self.assertEqual(c['fields']['url'],'str')
        self.assertEqual(c['semantics'],{'earliest':'年代最早的项目名称'})

    def test_conflicting_descriptions_are_not_silently_resolved(self):
        text=TEMPLATE+'\noldest_era: 年代最早的记录的年代\n'
        c=contract.derive(text,'t')
        self.assertEqual(c['kind'],'ambiguous')
        self.assertEqual(c['semantic_conflicts'],['oldest_era'])

    def test_name_requirement_rejects_observed_period_but_never_fabricates_correct_name(self):
        c=contract.derive(TEMPLATE,'task')
        for wrong in ('旧石器时代','晋'):
            ok,exact,reason=contract.validate(answer(wrong),c,[data_doc()])
            self.assertFalse(ok);self.assertEqual(exact,answer(wrong));self.assertIn('记录名称',reason)
        self.assertTrue(contract.validate(answer('遗址甲'),c,[data_doc()])[0])
        # Presence alone is not a ranking oracle; this module does not assert correctness.
        self.assertTrue(contract.validate(answer('未知但可能有效名称'),c,[])[0])
        era_contract=contract.derive(TEMPLATE.replace('年代最早的遗产名称','最早的朝代名称'),'task')
        self.assertTrue(contract.validate(answer('晋'),era_contract,[data_doc()])[0], 'a period name is not an entity name')

    def test_field_renaming_and_changed_records_do_not_use_historical_answers(self):
        c=contract.derive(TEMPLATE.replace('oldest_era','earliest_record'),'task')
        doc=data_doc();obj=json.loads(doc['text']);obj['data']['data']['records']=[{'name':'新地点','era':'时代Q'},{'name':'另地点','era':'时代Z'}]
        doc['text']=json.dumps(obj)
        proposed=json.loads(answer('时代Q'));proposed['earliest_record']=proposed.pop('oldest_era')
        self.assertFalse(contract.validate(json.dumps(proposed),c,[doc])[0])
        proposed['earliest_record']='新地点'
        self.assertTrue(contract.validate(json.dumps(proposed),c,[doc])[0])

    def test_partial_failed_truncated_and_unverified_data_do_not_become_oracles(self):
        c=contract.derive(TEMPLATE,'task')
        cases=[data_doc(12),{**data_doc(),'verified':False},{**data_doc(),'exit_code':1},
               {**data_doc(),'truncated':True},{**data_doc(),'command':'cat /tmp/example.json'}]
        for doc in cases:
            self.assertTrue(contract.validate(answer('旧石器时代'),c,[doc])[0])
            self.assertTrue(contract.validate(answer('旧石器时代'),c,[data_doc(),doc])[0], 'newer uncertainty supersedes old complete data')

    def test_only_current_named_task_document_can_supply_meanings(self):
        doc={'id':'api','path':'/tmp/API_DOCS.md','text':TEMPLATE,'verified':True,'truncated':False,'exit_code':0}
        self.assertIsNone(contract.for_task('请阅读task_new.md，获取任务信息',[doc]))
        doc.update(id='task',path='/tmp/task_new.md')
        self.assertIn('semantics',contract.for_task('请阅读task_new.md，获取任务信息',[doc]))

    def test_current_check_token_required_only_for_check_contract(self):
        c=contract.derive('运行./check 验证。输出格式：{"token":"xxx"}','task')
        doc={'command':task_tools.command('check',{'path':'/tmp/current/check'}),'verified':True,
             'exit_code':0,'truncated':False,'text':json.dumps({'tool':'task-check/1','exit_code':0,
             'truncated':False,'stdout':'[ OK ] 全部通过 (6/6)\nTOKEN: fresh-fixture-789\n'})}
        self.assertFalse(contract.validate('{"token":"historic-fixture-123"}',c,[doc])[0])
        self.assertFalse(contract.validate('{"token":"fresh-fixture-789"}',c,[])[0])
        self.assertTrue(contract.validate('{"token":"fresh-fixture-789"}',c,[doc])[0])
        generic=contract.derive('输出格式：{"token":"xxx"}','task')
        self.assertTrue(contract.validate('{"token":"from_other_authorized_source"}',generic,[])[0])


class TaskSemanticsPipelineTests(unittest.TestCase):
    setUp=support.PipelineTests.setUp
    step=support.PipelineTests.step
    answer=support.PipelineTests.answer

    def test_both_sides_prompt_pins_semantics_from_actual_task_file(self):
        for side in ('challenger','defender'):
            self.state,self.round=support.planner.PlannerState(),1
            self.fixture['files']['/tmp/unseen/work/task_varied.md']=TEMPLATE
            first=self.step(side=side)
            response=self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True),side=side)
            self.assertIn('"oldest_era": "年代最早的遗产名称"',response['prompt'])
            self.assertIn('不能只按英文键名猜语义',response['prompt'])
            self.assertLessEqual(len(response['prompt']),12000)

    def test_both_sides_block_wrong_period_then_submit_current_record_name(self):
        for side in ('challenger','defender'):
            self.state,self.round=support.planner.PlannerState(),1
            self.fixture['files']['/tmp/unseen/work/task_varied.md']=TEMPLATE
            first=self.step(side=side)
            self.step(cmd=local_task_sandbox.execute(first['executeCmd'],self.fixture,active=True),side=side)
            doc=data_doc()
            query=self.step(llm=self.answer('run',doc['command']),side=side)
            self.assertEqual(query['executeCmd'],doc['command'])
            self.step(cmd='[exitCode:0]\n'+doc['text'],side=side)
            rejected=self.step(llm=self.answer('answer',answer('旧石器时代')),side=side)
            self.assertFalse(any(c['action']=='submitAnswer' for c in rejected['roleCommandMap'].values()))
            self.assertIn('记录名称',rejected['prompt'])
            submitted=self.step(llm=self.answer('answer',answer('遗址甲')),side=side)
            outputs=[c['taskAnswer'] for c in submitted['roleCommandMap'].values() if c['action']=='submitAnswer']
            self.assertEqual(outputs,[answer('遗址甲')])


if __name__=='__main__':unittest.main()
