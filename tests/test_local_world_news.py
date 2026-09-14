"""Independent day/price/outage expectations for the opt-in news environment."""
from copy import deepcopy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import local_world_news, treasure
from agent.scenarios import scenario, observation, prepare_round
from agent.simulator import step
from agent.brain import plan_for_state
from agent.planner import PlannerState


class DailyNewsFixtureTests(unittest.TestCase):
    def test_default_scenario_is_unchanged_without_opt_in(self):
        state = scenario(90317)
        before = deepcopy(state)
        local_world_news.publish(state, [])
        self.assertEqual(state, before)
        self.assertFalse(local_world_news.resource_paused(state, 'iron', 131))

    def test_day_boundaries_and_price_restoration(self):
        state = local_world_news.install(scenario(90317))
        for round_no, price, paused in ((1, 3, False), (130, 3, False), (131, 6, True),
                                         (260, 6, True), (261, 6, True), (390, 6, True),
                                         (391, 3, False)):
            state['roundNo'] = round_no
            local_world_news.publish(state, [])
            actual = next(r['price'] for r in state['vendorShopList'] if r['name'] == 'iron')
            self.assertEqual(actual, price, round_no)
            self.assertEqual(local_world_news.resource_paused(state, 'iron', round_no), paused)
            self.assertFalse(local_world_news.resource_paused(state, 'copper', round_no))

    def test_no_future_clue_or_private_conditions_in_observations_or_decisions(self):
        for side in ('challenger', 'defender'):
            state = local_world_news.install(scenario(90601, side))
            public = observation(state)
            self.assertIn('地点', public['worldNews']['folkLegends'])
            self.assertNotIn('用品】', public['worldNews']['folkLegends'])
            self.assertNotIn('news_fixture', json.dumps(public))
            self.assertNotIn('outages', json.dumps(public))
            original = plan_for_state(deepcopy(public), PlannerState(), judge_tasks=False).build()
            state['_demo']['news_fixture']['publications'][1]['folkLegends'] = 'future changed'
            state['_demo']['treasure']['items'] = ['future-secret']
            self.assertEqual(observation(state), public)
            self.assertEqual(plan_for_state(observation(state), PlannerState(), judge_tasks=False).build(), original)

    def test_three_days_publish_only_the_current_piece_and_survive_json_transport(self):
        state = local_world_news.install(scenario(90317))
        texts = []
        for round_no, label in ((1, '地点'), (131, '用品'), (261, '时间')):
            state = json.loads(json.dumps(state))
            state['roundNo'] = round_no
            events = []
            prepare_round(state, events)
            text = state['worldNews']['folkLegends']
            self.assertIn(label, text)
            self.assertNotIn('future', text)
            texts.append(text)
            before = deepcopy(state['worldNews'])
            local_world_news.publish(state, events)
            self.assertEqual(state['worldNews'], before)
        self.assertEqual(len(set(texts)), 3)
        rite = treasure.rite_of(state)
        self.assertEqual((rite.opens_at, rite.closes_at), (261, 290))
        self.assertNotIn(texts[0], texts[2], 'the agent must actually retain prior clues')

    def test_shifted_start_day_uses_absolute_game_dates(self):
        state = scenario(90601)
        state['roundNo'] = 391  # day4
        local_world_news.install(state, start_day=4)
        rite = treasure.rite_of(state)
        self.assertEqual((rite.opens_at, rite.closes_at), (651, 680))  # day6
        self.assertTrue(local_world_news.resource_paused(state, 'iron', 521))  # day5
        self.assertTrue(local_world_news.resource_paused(state, 'iron', 651))  # day6
        self.assertFalse(local_world_news.resource_paused(state, 'iron', 781))  # day7

    def test_actual_collect_action_is_blocked_only_during_the_outage(self):
        for side in ('challenger', 'defender'):
            initial = local_world_news.install(scenario(90317, side))
            mine = next(z for z in initial['mapInfo']['zones'] if z['neutralType'] == 'iron')
            for round_no, should_collect in ((130, True), (131, False), (261, False), (391, True)):
                state = deepcopy(initial)
                state['roundNo'] = round_no
                local_world_news.publish(state, [])
                worker = next(r for r in state['teamOur']['roles'] if r['roleType'] == 'worker')
                worker['pos'] = {'x': mine['pos']['x'] - 1, 'y': mine['pos']['y']}
                worker['backpack'] = []
                result = step(state, {str(worker['id']): {'action': 'collect', 'targetPos': [mine['pos']]}})
                self.assertEqual(result['state']['lastRoundRoleActionResults'][str(worker['id'])], should_collect)
                bag = next(r for r in result['state']['teamOur']['roles'] if r['id'] == worker['id'])['backpack']
                self.assertEqual(bag, ['iron'] if should_collect else [])


if __name__ == '__main__':
    unittest.main()
