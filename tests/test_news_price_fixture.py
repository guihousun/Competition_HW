"""Local price experiment has independent settlement and public-text evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import local_world_news, local_scripted_model
from agent.scenarios import scenario, observation


class PriceDropFixtureTests(unittest.TestCase):
    def test_price_only_event_changes_quote_and_restores_it_without_pausing_copper(self):
        state = local_world_news.install(scenario(90317), price_drop=True)
        original = next(r['price'] for r in state['vendorShopList'] if r['name'] == 'copper')
        self.assertGreater(original, 1)
        for round_no, expected in ((1, original), (130, original), (131, 1), (260, 1), (261, original)):
            state['roundNo'] = round_no
            local_world_news.publish(state, [])
            self.assertEqual(next(r['price'] for r in state['vendorShopList'] if r['name'] == 'copper'), expected)
            self.assertFalse(local_world_news.resource_paused(state, 'copper', round_no))

    def test_price_schedule_stays_private_but_announcement_is_public(self):
        for side in ('challenger', 'defender'):
            state = local_world_news.install(scenario(90317, side), price_drop=True)
            public = observation(state)
            self.assertIn('第2天铜价下降到1金币', public['worldNews']['officialNews'])
            self.assertNotIn('price_events', json.dumps(public))
            changed = deepcopy(state)
            changed['_demo']['news_fixture']['price_events'][0]['price'] = 999
            self.assertEqual(observation(changed), public)

    def test_scripted_interpreter_reads_public_values_instead_of_a_hidden_answer(self):
        for day, material, expected, amount in ((2, '铜', 'copper', 1), (5, '铁', 'iron', 7)):
            text = f'游戏第1天公布：第{day}天{material}价下降到{amount}金币（仅当天）'
            prompt = '"request_id":"nonce" "events":\n公开来源：' + json.dumps([{'id': 'public-source', 'text': text}], ensure_ascii=False)
            result = json.loads(local_scripted_model.complete(prompt)['answer'])['events']
            self.assertEqual(len(result), 1)
            self.assertEqual((result[0]['resource'], result[0]['startDay'], result[0]['priceAmount']), (expected, day, amount))
            self.assertEqual(result[0]['evidence'][0]['quote'], text)
        empty = local_scripted_model.complete('"request_id":"nonce" "events":\n公开来源：[]')
        self.assertEqual(json.loads(empty['answer'])['events'], [])

    def test_existing_news_fixture_does_not_opt_into_new_price_scenario(self):
        state = local_world_news.install(scenario(90317))
        self.assertNotIn('price_events', state['_demo']['news_fixture'])
        self.assertNotIn('铜价下降', state['worldNews']['officialNews'])
