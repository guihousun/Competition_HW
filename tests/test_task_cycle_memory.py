"""Task lifecycle evidence must survive the official request/JSON boundary."""
import json
import unittest
from unittest.mock import patch
from test_treasure_night_staging import board, NOTES
from agent import brain, planner
from agent.protocol import Turn
from agent.tasks import TaskCycle


class TaskCycleMemoryTests(unittest.TestCase):
    def test_reward_answer_provenance_and_terminal_time_survive_json(self):
        state = planner.PlannerState()
        state.note_round(100)
        cycle = TaskCycle({'x': 10, 'y': 5}, 80, description='已发布题目',
                          timeout_rounds=20, score_reward=60, gold_reward=25)
        cycle.record(85, '42', 'model')
        cycle.end(86, 'completed')
        state.tasks['cycle'] = cycle
        restored = planner.PlannerState.load(json.loads(json.dumps(state.dump())))
        actual = restored.tasks['cycle']
        self.assertEqual((actual.score_reward, actual.gold_reward), (60, 25))
        self.assertEqual((actual.last_answer, actual.answer_source), ('42', 'model'))
        self.assertEqual((actual.phase, actual.ended_round, actual.end_reason), ('ended', 86, 'completed'))

    def test_legacy_ended_phase_is_not_an_active_commitment(self):
        state = planner.PlannerState()
        state.note_round(100)
        cycle = TaskCycle({'x': 10, 'y': 5}, 80, description='已结束题目')
        cycle.end(86, 'completed')
        state.tasks['cycle'] = cycle
        raw = state.dump()
        for key in ('score_reward', 'gold_reward', 'answer_source', 'ended_round', 'end_reason'):
            raw['tasks']['cycle'].pop(key)
        restored = planner.PlannerState.load(raw)
        self.assertEqual(restored.tasks['cycle'].phase, 'ended')
        turn, commands = Turn.load(board()), {}
        with patch.object(brain, '_treasure_notes', return_value=NOTES):
            self.assertIsNotNone(brain._night(turn, commands, board(), restored))
        self.assertEqual(commands[3]['action'], 'move')


if __name__ == '__main__':
    unittest.main()
