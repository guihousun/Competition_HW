import os
import unittest
from unittest.mock import patch
from test_llm_router_integration import task_memory, task_payload
from agent import brain


class AgentModeTests(unittest.TestCase):
    def test_task_only_keeps_its_router_and_acknowledgements(self):
        state = task_memory()
        payload = task_payload()
        payload['worldNews']['officialNews'] = '铁矿明日停工。'
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on', brain.WORLD_AGENT_ENV: 'off'}):
            response = brain.plan_for_state(payload, state, judge_tasks=False)
            self.assertTrue(brain.llm_router_enabled())
            self.assertFalse(brain.world_agent_enabled())
        self.assertIsNotNone(response.prompt)
        self.assertEqual(state.team_agent.task.prompts, 1)
        self.assertEqual(state.team_agent.world.sources['news'], [])

    def test_default_full_mode_and_explicit_world_only_are_independent(self):
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on'}, clear=True):
            self.assertTrue(brain.world_agent_enabled())
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'off', brain.WORLD_AGENT_ENV: 'on'}, clear=True):
            self.assertFalse(brain.task_agent_enabled())
            self.assertTrue(brain.world_agent_enabled())
            self.assertTrue(brain.llm_router_enabled())


if __name__ == '__main__':
    unittest.main()
