"""Structured strategy reports must never erase cognitive event rows."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, planner, task_journal
from agent.task_journal import TaskJournal
from test_tasks import observation


class JournalShapeTests(unittest.TestCase):
    def test_dictionary_reports_do_not_swallow_task_events(self):
        for field in ('pioneer_safety', 'shared_rocket_control', 'worker_shelter'):
            with self.subTest(field=field), patch.object(task_journal, '_journal', TaskJournal()), \
                 patch.dict('os.environ', {'COMPETITION_HW_CONSOLE': 'compact'}):
                req = observation(round_no=43, phase_task='new task')
                response = {'roleCommandMap': {}, 'prompt': 'inspect current task'}
                decision = {field: {'reason': 'safe', 'position': {'x': 3, 'y': 4}}}
                original = deepcopy((req, response, decision))
                lines = []
                rows = task_journal.emit(req, response, decision=decision, emitter=lines.append)
                self.assertIn('task_text_observed', [r['kind'] for r in rows])
                self.assertIn('issued_prompt', [r['kind'] for r in rows])
                self.assertIn(field, [r['kind'] for r in rows])
                self.assertTrue(lines)
                self.assertEqual((req, response, decision), original)

    def test_submission_end_and_new_task_after_round43_both_teams(self):
        j = TaskJournal()
        for side in ('challenger', 'defender'):
            events = []
            for n, question in ((40,'taskA'),(41,'taskA'),(42,''),(43,'taskB'),(44,'taskB')):
                req = observation(round_no=n, team=side, phase_task=question)
                req['lastRoundRoleActionResults'] = {'10011': True}
                response = {'roleCommandMap': {'10011': {'action':'submitAnswer','taskAnswer':'{"a":1}'}}} if n==41 else {'roleCommandMap':{}}
                decision = {'weapon_readiness':[{'id':'gun','cooldown':n%4}],
                            'pioneer_safety':{'reason':'retreat' if n==42 else 'safe'}}
                events += j.observe(req,response,decision=decision,stream=side)
            starts = [r['round'] for r in events if r['kind']=='task_text_observed']
            self.assertEqual(starts,[40,43])
            summary = next(r for r in events if r['kind']=='task_outcome_summary')
            value = json.loads(summary['content']['text'])
            self.assertEqual(summary['round'],42)
            self.assertEqual(value['counts']['submissions'],1)
            self.assertFalse(value['official_success_confirmed'])
            self.assertIn('task_submission_feedback',[r['kind'] for r in events])

    def test_only_targeted_volatile_fields_are_suppressed(self):
        j = TaskJournal()
        decision = {'weapon_readiness':[{'id':'gun','cooldown':3}],
                    'upgrade_itinerary':{'state':'walking','gold_available':40},
                    'pioneer_safety':{'reason':'safe','cooldown':2}}
        j.observe(observation(round_no=1),{},decision=decision)
        decision['weapon_readiness'][0]['cooldown']=2
        decision['upgrade_itinerary']['gold_available']=41
        self.assertEqual(j.observe(observation(round_no=2),{},decision=decision),[])
        decision['pioneer_safety']['cooldown']=1
        rows=j.observe(observation(round_no=3),{},decision=decision)
        self.assertEqual([r['kind'] for r in rows],['pioneer_safety'])

    def test_non_dictionary_optional_entries_do_not_break_task_logging(self):
        for field,value in [('weapon_readiness',['unknown',{'cooldown':3}]),
                            ('upgrade_itinerary',['unknown']),('worker_shelter',[1,'unknown'])]:
            rows=TaskJournal().observe(observation(round_no=1,phase_task='A'),{},decision={field:value})
            self.assertIn('task_text_observed',[r['kind'] for r in rows])

    def test_real_planner_report_reaches_emit_without_mutating_actions(self):
        req = observation(round_no=71, role_pos=(2,2), task_points=[])
        req['mapInfo']['zones']=[]
        req['robot']['roles']=[{'id':30001,'roleType':'smallRobot','health':40,'pos':{'x':5,'y':2}}]
        with patch.dict('os.environ', {'COMPETITION_HW_WORLD_AGENT':'off','COMPETITION_HW_TASK_AGENT':'off',
                                      'COMPETITION_HW_CONSOLE':'compact'}), \
             patch.object(task_journal, '_journal', TaskJournal()):
            response=brain.plan_for_state(req,planner.PlannerState()).build()
            decision=brain.decision_report()
            self.assertIsInstance(decision.get('pioneer_safety'),dict)
            frozen=deepcopy((req,response,decision))
            rows=task_journal.emit(req,response,decision=decision,emitter=lambda line:None)
            self.assertIn('pioneer_safety',[r['kind'] for r in rows])
            self.assertEqual((req,response,decision),frozen)


if __name__=='__main__':
    unittest.main()
