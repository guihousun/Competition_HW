"""Independent observational milestones, never a synthetic official win signal."""
import json
import unittest
from copy import deepcopy
from test_task_journal import TaskJournal, request
from agent import task_progress


PASS='[exitCode:0]\n[ OK ] 全部通过 (6/6)\nTOKEN: fixture-token-123\n'


def content(rows, kind):
    return [json.loads(row['content']['text']) for row in rows if row['kind']==kind]


def req(round_no, text='任务', side='challenger', gold=10, score=20, **fields):
    return {**request(round_no, phaseTask=text),
            'teamOur':{'type':side,'goldNum':gold,'totalScore':score},**fields}


class ProgressTests(unittest.TestCase):
    def test_check_submit_and_increased_score_do_not_invent_success(self):
        for side in ('challenger','defender'):
            with self.subTest(side=side):
                journal=TaskJournal()
                sequence=[(req(1,side=side),{'executeCmd':'cd /tmp/ws && ./check'}),
                    (req(2,side=side,lastCmdResult=PASS),{'roleCommandMap':{'42':{'action':'submitAnswer','taskAnswer':'{"token":"fixture-token-123"}'}}}),
                    (req(3,text='',side=side,gold=90,score=105,lastRoundRoleActionResults={'42':True}),{})]
                frozen=deepcopy(sequence);rows=[]
                for r,a in sequence:rows+=journal.observe(r,a,stream=side)
                self.assertEqual(sequence,frozen)
                check=content(rows,'task_check_result')[0]
                self.assertEqual(check['status'],'passed')
                self.assertTrue(check['token_seen'])
                self.assertNotIn('fixture-token-123',json.dumps(check))
                outcome=content(rows,'task_outcome_summary')[0]
                self.assertEqual(outcome['status'],'submitted_unconfirmed')
                self.assertFalse(outcome['official_success_confirmed'])
                self.assertTrue(outcome['last_submission']['matches_last_check_token'])
                self.assertTrue(outcome['submission_feedback']['action_legal'])
                self.assertEqual(outcome['submission_feedback']['stats_delta'],{'gold':80,'totalScore':85})
                self.assertEqual(outcome['counts']['checks_passed'],1)
                self.assertEqual(outcome['counts']['submissions'],1)
                self.assertNotEqual(outcome['reward_attribution'],'confirmed')
                self.assertEqual(content(journal.observe(req(4,text='',side=side),{},stream=side),'task_outcome_summary'),[])

    def test_failed_answer_then_resubmit_records_both_attempts(self):
        journal=TaskJournal()
        submit={'roleCommandMap':{'42':{'action':'submitAnswer','taskAnswer':'15'}}}
        journal.observe(req(1),submit)
        rows=journal.observe(req(2,errors=[{'errorCode':2,'description':'键值比对不通过: $: 值不符'}]),submit)
        feedback=content(rows,'task_submission_feedback')[0]
        self.assertEqual(feedback['answer_error_kind'],'value_mismatch')
        rows=journal.observe(req(3,text='',lastRoundRoleActionResults={42:False}),{})
        outcome=content(rows,'task_outcome_summary')[0]
        self.assertEqual(outcome['status'],'submission_action_illegal')
        self.assertEqual(outcome['counts']['answer_errors'],1)
        self.assertEqual(outcome['counts']['submissions'],2)

    def test_final_answer_error_is_visible_without_changing_legacy_unknown(self):
        journal=TaskJournal()
        journal.observe(req(1),{'roleCommandMap':{'42':{'action':'submitAnswer','taskAnswer':'bare'}}})
        rows=journal.observe(req(2,text='',errors=[{'errorCode':2,'description':'答案不是合法 JSON'}]),{})
        summary=content(rows,'task_outcome_summary')[0]
        self.assertEqual(summary['status'],'answer_error_observed')
        self.assertEqual(summary['submission_feedback']['answer_error_kind'],'invalid_json')
        legacy=next(r for r in rows if r['kind']=='task_text_ended')
        self.assertIn('unknown_without_judge_feedback',legacy['content']['text'])

    def test_check_failure_timeout_and_truncation_never_pass(self):
        cases=[('[exitCode:1]\n'+PASS,'failed'),('[TIMEOUT]\n'+PASS,'timeout'),
               (PASS+'[TRUNCATED]','truncated'),('[JUDGER_ERROR]\nfault','judger_error'),
               ('[exitCode:0]\nTOKEN: fixture-token-123\n','exit_ok_without_pass_marker'),
               ('[exitCode:0]\n[ OK ] 全部通过 (5/6)\n','exit_ok_without_pass_marker')]
        for raw,status in cases:
            with self.subTest(status=status):
                journal=TaskJournal();journal.observe(req(1),{'executeCmd':'./check'})
                rows=journal.observe(req(2,text='',lastCmdResult=raw),{})
                self.assertEqual(content(rows,'task_check_result')[0]['status'],status)
                self.assertEqual(content(rows,'task_outcome_summary')[0]['counts']['checks_passed'],0)

    def test_structured_check_uses_inner_exit_and_output(self):
        for code,truncated,passed in [(0,False,True),(1,False,False),(0,True,False)]:
            journal=TaskJournal();journal.observe(req(1),{'executeCmd':'# task-check/1\npython3 helper'})
            raw='[exitCode:0]\n'+json.dumps({'tool':'task-check/1','exit_code':code,'truncated':truncated,'stdout':PASS.split('\n',1)[1]})
            check=content(journal.observe(req(2,lastCmdResult=raw),{}),'task_check_result')[0]
            self.assertEqual(check['all_checks_passed'],passed)

    def test_cat_echo_llm_claims_and_stale_outputs_are_not_checks(self):
        for command in ('cat /tmp/check',"echo './check'",'python3 -c "print(123)"',"cat <<EOF\n./check\nEOF"):
            self.assertFalse(task_progress.is_check(command),command)
            journal=TaskJournal();journal.observe(req(1),{'executeCmd':command})
            self.assertEqual(content(journal.observe(req(2,lastCmdResult=PASS,llmResp=PASS),{}),'task_check_result'),[])
        journal=TaskJournal();journal.observe(req(1),{'executeCmd':'./check'})
        self.assertEqual(len(content(journal.observe(req(2,lastCmdResult=PASS),{'executeCmd':'./check'}),'task_check_result')),1)
        self.assertEqual(len(content(journal.observe(req(3,lastCmdResult=PASS),{}),'task_check_result')),1)
        self.assertEqual(content(journal.observe(req(4,lastCmdResult=PASS),{}),'task_check_result'),[])
        summary=content(journal.observe(req(5,text=''),{}),'task_outcome_summary')[0]
        self.assertEqual(summary['counts']['checks_passed'],2)
        self.assertEqual(summary['status'],'check_passed_not_submitted')

    def test_gap_replacement_and_streams_do_not_transfer_check_success(self):
        for later,text in [(4,''),(2,'another task')]:
            journal=TaskJournal();journal.observe(req(1),{'executeCmd':'./check'},stream='a')
            self.assertEqual(content(journal.observe(req(1,lastCmdResult=PASS),{},stream='b'),'task_check_result'),[])
            rows=journal.observe(req(later,text=text,lastCmdResult=PASS),{},stream='a')
            self.assertEqual(content(rows,'task_check_result')[0]['status'],'unattributed')
            summary=content(rows,'task_outcome_summary')[0]
            self.assertEqual(summary['counts']['checks_passed'],0)
            self.assertEqual(summary['observation_gap'],later==4)
            if text:
                summary=content(journal.observe(req(3,text=''),{},stream='a'),'task_outcome_summary')[0]
                self.assertIsNone(summary['last_check'])

    def test_reset_duplicate_and_idle_do_not_create_extra_summaries(self):
        journal=TaskJournal();journal.observe(req(2),{'executeCmd':'./check'})
        self.assertEqual(journal.observe(req(2),{}),[])
        rows=journal.observe(req(1,text='new task',lastCmdResult=PASS),{})
        self.assertEqual(content(rows,'task_check_result'),[])
        self.assertEqual(content(rows,'task_outcome_summary'),[])


if __name__=='__main__':unittest.main()
