"""Local long-rumour fixture through real simulation settlement, both sides."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, debug, local_scripted_model, local_world_news, scenarios, simulator


class LongWorldSimulatorTests(unittest.TestCase):
    def test_opt_in_scene_exposes_only_current_long_publication(self):
        result = debug.llm_scenario_payload(90317, 'challenger', 'world-long', 'scripted')
        state = result['state']
        public = scenarios.observation(state)
        self.assertGreater(len(public['worldNews']['folkLegends']), 2000)
        self.assertIn('明确排除用品', public['worldNews']['folkLegends'])
        self.assertNotIn('前30个回合', public['worldNews']['folkLegends'])
        self.assertNotIn('news_fixture', json.dumps(public))
        self.assertEqual(state['_demo']['llm_mode'], 'scripted')

    def test_long_clues_lead_to_actual_purchase_travel_and_summon_on_both_sides(self):
        with patch.dict(os.environ, {brain.WORLD_AGENT_ENV: 'on', brain.TASK_AGENT_ENV: 'off'}):
            for side in ('challenger', 'defender'):
                with self.subTest(side=side):
                    # This test drives the treasure/travel pipeline, not the wave
                    # size: keep the small legacy waves so the pioneer is not
                    # overwhelmed. The observed wave table has its own tests.
                    state = local_world_news.install(
                        scenarios.scenario(90317, side, 1, profile='local-pressure',
                                           task_world_profile='legacy-local-v1'),
                        long_context=True)
                    daily, inspected, bought, moved, opened = {}, False, False, False, False
                    for _ in range(300):
                        result = simulator.step(state)
                        state = result['state']
                        memory = state['_demo']['planner']
                        world = memory.team_agent.world
                        day = (result['frame']['round'] - 1) // 130 + 1
                        daily[day] = max(daily.get(day, 0), memory.judge.llm_used_today)
                        inspected |= world.focus['treasure'] is not None
                        pioneer = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'pioneer')
                        action = result['executed'].get(str(pioneer['id']), {}).get('action')
                        bought |= action == 'buy'
                        moved |= action == 'move'
                        if day < 3:
                            self.assertNotEqual(action, 'summonTreasure')
                        if result['judgeRequest'].get('prompt'):
                            state['llmResp'] = local_scripted_model.complete(result['judgeRequest']['prompt'])['answer']
                        state['_demo']['planner'] = memory.dump()
                        state = json.loads(json.dumps(state))
                        if state.get('lastSummonTreasureResult') == 1:
                            opened = True
                            break
                    self.assertTrue(all(n <= 3 for n in daily.values()), daily)
                    self.assertTrue(inspected, 'the long public text must actually use bounded retrieval')
                    self.assertTrue(bought and moved, 'success must include purchases and travel')
                    self.assertTrue(opened, json.dumps({'status': world.status, 'hypothesis': world.hypothesis,
                                                       'daily': daily}, ensure_ascii=False))
                    self.assertTrue(any(c.get('polarity') == 'exclude' for c in world.hypothesis['candidates']))


if __name__ == '__main__':
    unittest.main()
