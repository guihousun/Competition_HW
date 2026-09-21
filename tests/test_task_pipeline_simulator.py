"""15-round nested-file scenario through the simulator, with held-out maps."""
import os
import unittest
from unittest.mock import patch
import test_team_agent_simulator as suite
from agent import brain, local_task_cases, scenarios, taskworld


class PipelineSimulatorTests(unittest.TestCase):
    def test_nested_case_completes_on_three_maps_and_both_teams(self):
        plans = [('run','python3 /svc/catalog.py --place 丙地'),
                 ('answer','{"city":"丙地","value":17}')]
        with patch.dict(suite.PLANS, {'nested-api-15-rounds': plans}), patch.dict(
                os.environ, {brain.TASK_AGENT_ENV:'on'}):
            for seed in (1, 6173, 90601):
                for side in ('challenger','defender'):
                    with self.subTest(seed=seed, side=side):
                        result=suite.exercise('nested-api-15-rounds', side, seed=seed)
                        self.assertEqual(result['report']['ended'],'completed')
                        self.assertEqual(result['report']['rewards']['rate'],1)
                        self.assertEqual((result['calls'],result['tools']),(2,2))
                        self.assertLessEqual(len(result['trace']),7)
                        self.assertEqual(result['state']['_demo']['planner']['judge']['llmUsedToday'],0)

    def test_scenario_reward_and_timeout_are_public_and_not_global(self):
        state=scenarios.scenario(6173,'defender')
        local_task_cases.install(state,['nested-api-15-rounds'])
        rows=taskworld.player_tasks(state,state['_demo']['task_world'],'defender')
        self.assertEqual([(r['scoreReward'],r['goldReward'],r['timeoutRounds']) for r in rows],[(80,80,15)]*2)
        old=scenarios.scenario(6173,'defender')
        rows=taskworld.player_tasks(old,old['_demo']['task_world'],'defender')
        self.assertEqual(rows[0]['scoreReward'],80)
        self.assertEqual(rows[0]['timeoutRounds'],taskworld.DEFAULT_TIMEOUT)
