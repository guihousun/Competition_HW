"""External judge-response settlement, without a second local policy (R01/R07)."""
from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch
from test_metal_economy import board
from agent import brain, debug, local_scripted_model, local_task_sandbox, planner, scenarios, simulator


class ExternalAgentSimulatorTests(unittest.TestCase):
    def test_external_commands_settle_without_local_policy_or_preview(self):
        state = board(ores=(('copper', (15, 24)),), backpack=[])
        response = {'roleCommandMap': {'10010': {'action': 'collect', 'targetPos': [{'x': 15, 'y': 24}]}},
                    'prompt': 'external prompt', 'executeCmd': 'pwd'}
        before = deepcopy(state)
        with patch.object(simulator, 'plan_for_state', side_effect=AssertionError('local planning forbidden')), \
             patch.object(planner.PlannerState, 'note_results', side_effect=AssertionError('external agent owns receipts')):
            result = simulator.step(state, external_response=response)
        self.assertEqual(state, before)
        worker = next(r for r in result['state']['teamOur']['roles'] if r['id'] == 10010)
        self.assertEqual(['copper'], worker['backpack'])
        self.assertEqual(response['roleCommandMap'], result['executed'])
        self.assertEqual({'prompt': 'external prompt', 'executeCmd': 'pwd'}, result['judgeRequest'])
        self.assertEqual({}, result['roleCommandMap'], 'no locally invented preview')
        self.assertIsNone(result['state']['_demo']['planner'])

    def test_external_agent_completes_task_through_public_observations(self):
        with patch.dict(os.environ, {brain.TASK_AGENT_ENV: 'on', brain.WORLD_AGENT_ENV: 'off'}):
            planner.reset()
            self.addCleanup(planner.reset)
            state = debug.llm_scenario_payload(1, 'challenger', 'tasks', 'scripted')['state']
            prompts = commands = 0
            with patch.object(simulator, 'plan_for_state', side_effect=AssertionError('second policy must not run')):
                for _ in range(30):
                    observation = scenarios.observation(state)
                    self.assertNotIn('_demo', observation)
                    response = brain.respond(observation)
                    result = simulator.step(state, external_response=response)
                    state = result['state']
                    if response.get('prompt'):
                        prompts += 1
                        state['llmResp'] = local_scripted_model.complete(response['prompt'])['answer']
                    if response.get('executeCmd'):
                        commands += 1
                        state['lastCmdResult'] = local_task_sandbox.execute(response['executeCmd'],
                            local_task_sandbox.active_task_fixture(state), active=True)
                    state = json.loads(json.dumps(state))
                    reward = state['_demo'].get('task_report', {}).get('rewards')
                    if reward:
                        break
            self.assertIsNotNone(reward)
            self.assertEqual(1.0, reward['rate'])
            self.assertEqual({'city': '北京', 'temperature': 23}, json.loads(reward['answer']))
            self.assertGreater(prompts, 0)
            self.assertGreater(commands, 0)

    def test_ambiguous_or_malformed_external_envelopes_are_rejected(self):
        state = board()
        for value in ({}, {'roleCommandMap': []}, {'roleCommandMap': {}, 'private': 'not official'},
                      {'roleCommandMap': {}, 'prompt': 5}, {'roleCommandMap': {1: {}}}):
            with self.assertRaises(ValueError):
                simulator.step(state, external_response=value)
        with self.assertRaises(ValueError):
            simulator.step(state, {}, external_response={'roleCommandMap': {}})


if __name__ == '__main__':
    unittest.main()
