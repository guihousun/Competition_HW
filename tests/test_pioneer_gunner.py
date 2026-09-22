"""Observed pioneer handover cases for the single three-rocket post."""
import copy
import unittest
from unittest.mock import patch

from test_single_operator_rockets import fixture
from agent import brain, night_gunner, planner, rocket_post
from agent.protocol import Turn, Pos


def board():
    p = fixture()
    p['roundNo'] = 90
    p['robot'] = {'roles': [dict(id=90, roleType='largeRobot', health=500,
                                 pos=dict(x=15, y=21), targetTeam='challenger')]}
    p['teamOur']['roles'][1]['pos'] = dict(x=9, y=20)
    p['teamOur']['roles'][2]['pos'] = dict(x=8, y=20)
    p['teamOur']['roles'][3]['pos'] = dict(x=11, y=21)
    p['teamOur']['roles'][3]['backpack'] = []
    p['phaseTask'] = ''
    p['lastRoundRoleActionResults'] = {}
    return p


class PioneerGunnerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(brain._STRATEGY)
        self.patch = patch.object(brain.strategy_config, 'get', return_value=self.cfg)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_idle_pioneer_on_common_post_fires_before_workers(self):
        p = board(); state = planner.PlannerState(); turn = Turn.load(p)
        commands = {}; report = night_gunner.plan(turn, p, state, commands, set(), brain._aim_points)
        self.assertEqual(report['owner'], 4)
        self.assertEqual(report['owner_kind'], 'pioneer')
        self.assertEqual(report['handover'], 'complete')
        self.assertEqual(next(c for c in commands.values() if c['action'] == 'attack')['controllerId'], '4')

    def test_pioneer_with_active_task_cannot_take_post(self):
        p = board(); state = planner.PlannerState()
        state.tasks['cycle'] = type('Cycle', (), {'phase':'solve','ended_round':0})()
        eligible, reason = night_gunner.pioneer_status(Turn.load(p), p, state)
        self.assertFalse(eligible); self.assertEqual(reason, 'task_completion_not_confirmed')
        commands = {}; brain._coordinate_rockets(Turn.load(p), commands, set())
        attack = [c for c in commands.values() if c.get('action') == 'attack']
        self.assertTrue(attack)
        self.assertEqual(attack[0]['controllerId'], '2')

    def test_pioneer_not_at_post_approaches_without_displacing_worker(self):
        p = board(); p['teamOur']['roles'][3]['pos'] = dict(x=10, y=22)
        state = planner.PlannerState(); commands = {}
        report = night_gunner.plan(Turn.load(p), p, state, commands, set(), brain._aim_points)
        self.assertIn(report['handover'], ('approaching', 'pioneer_entering_empty_post', 'waiting_for_safe_bay'))
        self.assertTrue(any(c.get('action') == 'attack' and c.get('controllerId') == '2'
                            for c in commands.values()))
        self.assertTrue(any(uid == 4 and c['action'] == 'move' for uid, c in commands.items()))

    def test_missing_pioneer_or_receipt_keeps_worker_fallback(self):
        p = board(); p['teamOur']['roles'][3]['health'] = 0
        state = planner.PlannerState(); commands = {}
        eligible, _ = night_gunner.pioneer_status(Turn.load(p), p, state)
        self.assertFalse(eligible)
        result = brain._coordinate_rockets(Turn.load(p), commands, set())
        self.assertEqual(result['owner'], 2)
        p = board(); state = planner.PlannerState(); state.judge.pending_cmd = object()
        eligible, reason = night_gunner.pioneer_status(Turn.load(p), p, state)
        self.assertFalse(eligible); self.assertEqual(reason, 'task_receipt_in_flight')

    def test_full_night_does_not_allow_handover(self):
        p = board(); p['roundNo'] = 500
        state = planner.PlannerState(); commands = {}
        with patch.object(brain, '_productive_night', return_value=False):
            brain._night(Turn.load(p), commands, p, state)
        attacks = [c for c in commands.values() if c.get('action') == 'attack']
        self.assertTrue(attacks)
        self.assertEqual(attacks[0]['controllerId'], '2')

    def test_full_night_emergency_handover_when_gunner_is_critically_wounded(self):
        p = board(); p['roundNo'] = 500
        # A full-night reserve must not prevent the pioneer from taking over
        # when the only active worker gunner is about to die.
        p['teamOur']['roles'][2]['health'] = 40
        state = planner.PlannerState(); commands = {}
        with patch.object(brain, '_productive_night', return_value=False):
            brain._night(Turn.load(p), commands, p, state)
        self.assertTrue(any(uid == 4 and command.get('action') == 'move'
                            for uid, command in commands.items())
                        or any(command.get('controllerId') == '4'
                               for command in commands.values()))

    def test_diagnostic_marks_real_pioneer_controller(self):
        p = board(); state = planner.PlannerState()
        response = brain.plan_for_state(p, state, judge_tasks=False)
        attack = next(c for c in response.commands.values() if c.get('action') == 'attack')
        self.assertEqual(attack['controllerId'], '4')
        report = brain.decision_report()
        self.assertEqual(report['shared_rocket_control']['owner'], 4)


if __name__ == '__main__':
    unittest.main()
