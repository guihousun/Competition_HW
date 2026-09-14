"""The visible demo uses the real debug pipeline and a clearly labelled model."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, debug, local_llm, local_scripted_model


class NeverCalled:
    configured = False
    def complete(self, prompt):
        raise AssertionError('scripted demo must not call a real provider')


class AgentDemoTests(unittest.TestCase):
    def test_task_and_long_document_demos_finish_through_debug_json(self):
        for kind in ('tasks', 'long'):
            with self.subTest(kind=kind), patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on'}):
                service = local_llm.LocalLLM(NeverCalled())
                with patch.object(local_llm, 'SERVICE', service):
                    state = debug.llm_scenario_payload(1, 'challenger', kind, 'scripted')['state']
                    rewards = None
                    for _ in range(35):
                        response = debug.step_payload(json.loads(json.dumps(state)))
                        state = response['state']
                        rewards = state['_demo'].get('task_report', {}).get('rewards')
                        if rewards:
                            break
                    self.assertIsNotNone(rewards)
                    self.assertEqual(rewards['rate'], 1)
                    self.assertEqual(service.calls, 0)
                    self.assertFalse(state['_demo']['llm_enabled'])
                    self.assertTrue(state['_demo']['llm_status']['scripted'])
                    if kind == 'long':
                        self.assertGreater(state['_demo']['planner']['teamAgent']['task']['inspections'], 0)

    def test_world_and_mixed_factories_only_publish_first_day_clues(self):
        for kind in ('world', 'mixed'):
            result = debug.llm_scenario_payload(90601, 'defender', kind, 'scripted')
            state = result['state']
            self.assertIn('地点', state['worldNews']['folkLegends'])
            self.assertNotIn('每种恰好一份', state['worldNews']['folkLegends'])
            self.assertNotIn('前30个回合', state['worldNews']['folkLegends'])
            self.assertEqual(bool(state['_demo']['task_world'].get('agent_cases')), kind == 'mixed')

    def test_setting_cost_limit_never_resets_consumed_calls(self):
        service = local_llm.LocalLLM(NeverCalled())
        service.calls = 7
        service.set_limit(30)
        self.assertEqual(service.calls, 7)
        self.assertEqual(service.max_calls, 30)
        service.set_limit(5)
        self.assertEqual(service.calls, 7)
        for invalid in (True, 0, 101, '20'):
            with self.assertRaises(ValueError):
                service.set_limit(invalid)

    def test_scripted_unknown_task_is_not_answered_from_a_private_oracle(self):
        result = local_scripted_model.complete('未知题目，任何数据都未提供。')
        self.assertIn('不支持', result['answer'])
        self.assertEqual(result['reported_model'], local_scripted_model.MODEL)


if __name__ == '__main__':
    unittest.main()
