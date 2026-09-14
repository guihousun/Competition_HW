"""Independent two-field fixture: partial answers must permit correction.

R07/R08, official task book chapters five/six and interface 1.7. Field grading
is explicitly a local fixture. Expectations below are hand calculated.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import taskworld
from agent.scenarios import scenario
from agent.simulator import step
from agent.local_task_cases import install
from agent.scenarios import observation
from agent.local_llm import after_step
from agent.sandbox import parse_command_result
from agent.tasks import TaskCycle
from agent.planner import PlannerState


class TaskFeedbackTests(unittest.TestCase):
    def test_room_fixture_description_matches_its_independent_expected_room(self):
        class FixedDraws:
            def choice(self, values):
                return values[0] if isinstance(values[0], tuple) else 'B7'

            def randint(self, lower, upper):
                return 20

        description, expected = taskworld._payload_for(FixedDraws())
        self.assertIn('机房：B7', description)
        self.assertEqual(expected, '机房=B7; 温度=20; 湿度=20')

    def fixture(self, side='challenger'):
        state = scenario(90317, side, 1)
        world = state['_demo']['task_world']
        kind = side + 'TaskPoint1'
        point = next(z for z in state['mapInfo']['zones'] if z['neutralType'] == kind)
        pioneer = next(u for u in state['teamOur']['roles'] if u['roleType'] == 'pioneer')
        pioneer['pos'] = {'x': point['pos']['x'] - 1, 'y': point['pos']['y']}
        state['roundNo'] = 10
        state['phaseTask'] = 'Return a=4;b=8'
        world['points'][kind]['active'] = {
            'type': '自进化类1', 'accepted': 5, 'deadline': 30, 'timeout': 25,
            'description': state['phaseTask'], 'answer': 'a=4;b=8',
            'score': 50, 'gold': 30, 'best_rate': 0.0, 'best_answer': '',
            'pending_answer': None,
        }
        return state, world, world['points'][kind], pioneer

    def judge(self, state, world, answer=None):
        state['errors'] = []
        state['lastRoundRoleActionResults'] = {}
        if answer is not None:
            book = world['points'][state['teamOur']['type'] + 'TaskPoint1']
            book['active']['pending_answer'] = answer
        return taskworld.advance(state, world, [])

    def test_wrong_partial_then_correct_both_sides(self):
        for side in ('challenger', 'defender'):
            with self.subTest(side=side):
                state, world, book, _ = self.fixture(side)
                for round_no, answer, rate in ((10, 'a=0;b=0', 0), (11, 'a=4;b=0', .5)):
                    state['roundNo'] = round_no
                    report = self.judge(state, world, answer)
                    self.assertIsNone(report['ended'])
                    self.assertIsNone(report['rewards'])
                    self.assertEqual(report['submission']['rate'], rate)
                    self.assertEqual(state['errors'][0]['errorCode'], 2)
                    self.assertEqual(state['teamOur']['goldNum'], 75)
                    self.assertEqual(state['teamOur']['totalScore'], 0)
                    self.assertEqual(book['tasks_left'], 30)
                    self.assertEqual(state['phaseTask'], 'Return a=4;b=8')
                state['roundNo'] = 12
                report = self.judge(state, world, 'a=4;b=8')
                self.assertEqual(report['ended'], 'completed')
                # floor(50 + 5*25/(12-5)) = 67 in the existing local score model.
                self.assertEqual(state['teamOur']['totalScore'], 67)
                self.assertEqual(state['teamOur']['goldNum'], 105)
                self.assertEqual(book['tasks_left'], 29)
                self.assertEqual(book['cooldown'], 30)
                self.assertIsNone(book['active'])
                self.assertEqual(state['phaseTask'], '')
                taskworld.advance(state, world, [])
                self.assertEqual(state['teamOur']['totalScore'], 67)
                self.assertEqual(book['cooldown'], 30)

    def test_best_answer_survives_worse_answer_and_json_roundtrip(self):
        state, world, book, _ = self.fixture()
        self.judge(state, world, 'a=4;b=0')
        state['roundNo'] = 11
        self.judge(state, world, 'a=0;b=0')
        state = json.loads(json.dumps(state))
        world = state['_demo']['task_world']
        state['roundNo'] = 31
        report = self.judge(state, world)
        self.assertEqual(report['ended'], '超时')
        self.assertEqual(report['rewards']['answer'], 'a=4;b=0')
        self.assertEqual(report['rewards']['rate'], .5)
        self.assertEqual(state['teamOur']['totalScore'], 25)
        self.assertEqual(state['teamOur']['goldNum'], 90)
        self.assertEqual(state['errors'][0]['errorCode'], 1)

    def test_partial_settles_once_on_death_or_leaving(self):
        for mode in ('death', 'leave'):
            state, world, book, pioneer = self.fixture()
            self.judge(state, world, 'a=4;b=0')
            state['roundNo'] = 11
            if mode == 'death':
                pioneer['health'] = 0
            else:
                pioneer['pos'] = {'x': 0, 'y': 0}
            report = self.judge(state, world)
            self.assertIn(report['ended'], ('开拓者死亡', '离开任务点范围'))
            self.assertEqual(state['teamOur']['totalScore'], 25)
            self.assertEqual(state['teamOur']['goldNum'], 90)
            state['roundNo'] = 12
            self.judge(state, world)
            self.assertEqual(state['teamOur']['totalScore'], 25)
            self.assertEqual(book['tasks_left'], 29)

    def test_zero_credit_timeout_has_no_reward(self):
        state, world, book, _ = self.fixture()
        self.judge(state, world, 'nonsense')
        state['roundNo'] = 31
        report = self.judge(state, world)
        self.assertEqual(report['ended'], '超时')
        self.assertIsNone(report['rewards'])
        self.assertEqual(state['teamOur']['totalScore'], 0)
        self.assertEqual(state['teamOur']['goldNum'], 75)
        self.assertIsNone(book['active'])

    def test_real_step_transports_partial_feedback_then_correction(self):
        for side in ('challenger', 'defender'):
            state, _, _, pioneer = self.fixture(side)
            pid = str(pioneer['id'])
            state = step(state, {pid: {'action': 'submitAnswer', 'taskAnswer': 'a=4;b=0'}})['state']
            state = step(state, {})['state']
            self.assertEqual([e['errorCode'] for e in state['errors']], [2])
            self.assertEqual(state['teamOur']['totalScore'], 0)
            self.assertTrue(state['phaseTask'])
            state = step(state, {pid: {'action': 'submitAnswer', 'taskAnswer': 'a=4;b=8'}})['state']
            self.assertEqual(state['errors'], [], 'old task errors must not repeat forever')
            state = step(state, {})['state']
            self.assertEqual(state['phaseTask'], '')
            self.assertEqual(state['_demo']['task_report']['ended'], 'completed')
            self.assertEqual(state['teamOur']['goldNum'], 105)

    def test_json_contract_rejects_duplicate_keys_extra_fields_and_wrong_types(self):
        active = {'answer': '{"city":"北京","temperature":23}', 'grading': 'json_fields'}
        for answer in ('{"city":"北京","temperature":"23"}', '{"city":"北京"}'):
            self.assertEqual(taskworld._grade(answer, active), .5)
        for answer in ('{"city":"北京","temperature":23,"extra":1}',
                       '{"city":"上海","city":"北京","temperature":23}',
                       '{"city":"北京","temperature":NaN}', 'not json'):
            self.assertEqual(taskworld._grade(answer, active), 0)
        self.assertEqual(taskworld._grade('{"temperature":23,"city":"北京"}', active), 1)

    def test_submission_memory_preserves_whitespace_inside_json_strings(self):
        answer = '{\n  "value": "a  b"\n}\n'
        cycle = TaskCycle(point={'x': 5, 'y': 5}, accepted_round=1)
        cycle.record(2, answer, 'fixture')
        state = PlannerState()
        state.tasks['cycle'] = cycle
        restored = PlannerState.load(json.loads(json.dumps(state.dump())))
        self.assertEqual(restored.tasks['cycle'].submissions[0].answer, answer)
        self.assertEqual(cycle.last_answer, answer)

    def test_installed_suite_accepts_reads_and_grades_via_simulator(self):
        for side in ('challenger', 'defender'):
            state = scenario(90601, side, 1)
            install(state, ['weather-doc-v1-beijing'])
            point = next(z for z in state['mapInfo']['zones'] if z['neutralType'] == side + 'TaskPoint1')
            pioneer = next(u for u in state['teamOur']['roles'] if u['roleType'] == 'pioneer')
            pioneer['pos'] = {'x': point['pos']['x'] - 1, 'y': point['pos']['y']}
            pid = str(pioneer['id'])
            state = step(state, {pid: {'action': 'acceptTask'}})['state']
            self.assertIn('/brief', state['phaseTask'])
            public = observation(state)
            self.assertNotIn('sandbox_fixture', str(public))
            self.assertNotIn('"temperature":23', str(public))
            for command, expected in [('ls /brief', 'README.txt'),
                                      ('cat /brief/README.txt', '/svc/weather.py'),
                                      ('python3 /svc/weather.py --city 北京', '23')]:
                result = after_step({'state': step(state, {})['state'],
                                     'judgeRequest': {'executeCmd': command}})
                state = result['state']
                response = parse_command_result(state['lastCmdResult'])
                self.assertTrue(response.ok)
                self.assertIn(expected, response.output)
            state = step(state, {pid: {'action': 'submitAnswer',
                                       'taskAnswer': '{"city":"北京","temperature":23}'}})['state']
            state = step(state, {})['state']
            self.assertEqual(state['_demo']['task_report']['ended'], 'completed')
            self.assertEqual(state['teamOur']['goldNum'], 105)


if __name__ == '__main__':
    unittest.main()
