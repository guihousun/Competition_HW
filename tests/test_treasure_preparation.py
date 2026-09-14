"""Prepare supported items before a late window announcement, with guardrails."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent import brain, planner, simulator
from agent.world_agent import WorldAgent
from test_metal_economy import board


def situation(*, gold=100, towers=3, health=1500, round_no=132, shop=(15, 23)):
    payload = board(gold=gold, towers=towers, round_no=round_no)
    pioneer = next(r for r in payload['teamOur']['roles'] if r['roleType'] == 'pioneer')
    pioneer['pos'] = {'x': 14, 'y': 23}
    payload['teamOur']['roles'][0]['health'] = health
    payload['mapInfo']['zones'].append({'pos': {'x': shop[0], 'y': shop[1]}, 'neutralType': 'weaponShop'})
    payload['weaponShopList'] += [{'name': 'AcientTablet', 'price': 15}, {'name': 'IronWhistle', 'price': 15}]
    site_text = '祭坛位于(3,4)，时间未知。'
    items_text = '祭坛需要AcientTablet和IronWhistle各一个，未公布时间。'
    records = [{'id': hashlib.sha256(t.encode()).hexdigest(), 'firstRound': n, 'text': t, 'truncated': False}
               for t, n in ((site_text, 1), (items_text, 131))]
    world = WorldAgent()
    world.sources['treasure'] = records
    world.hypothesis = {'site': {'x': 3, 'y': 4}, 'items': ['AcientTablet', 'IronWhistle'],
                        'opensAt': None, 'closesAt': None, 'uncertain': True,
                        'evidence': {'site': [{'sourceId': records[0]['id'], 'quote': site_text}],
                                     'items': [{'sourceId': records[1]['id'], 'quote': items_text}], 'window': []}}
    world.resolved['treasure'] = world.version('treasure')
    world = WorldAgent.load(world.dump())
    assert not world.degraded
    payload['worldNews']['folkLegends'] = items_text
    state = planner.PlannerState()
    state.ensure_team_agent(payload).world = world
    return payload, state


class TreasurePreparationTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {brain.WORLD_AGENT_ENV: 'on', brain.TASK_AGENT_ENV: 'off'})
        env.start()
        self.addCleanup(env.stop)

    def test_items_can_be_bought_without_inventing_a_summon_time(self):
        payload, state = situation(gold=55)
        response = brain.plan_for_state(payload, state, judge_tasks=False)
        self.assertEqual(response.commands['10011'], {'action': 'buy', 'name': 'AcientTablet'})
        notes = state.team_agent.world.policy_view(132)['treasure']
        self.assertFalse(notes['known'])
        self.assertFalse(notes['open'])
        self.assertTrue(notes['preparable'])
        self.assertIsNone(notes['opensAt'])

    def test_defence_cash_dusk_and_running_task_stop_preparation(self):
        for options in ({'gold': 54}, {'towers': 2}, {'health': 999}, {'round_no': 184}):
            payload, state = situation(**options)
            response = brain.plan_for_state(payload, state, judge_tasks=False)
            self.assertNotEqual(response.commands.get('10011', {}).get('action'), 'buy')
        payload, state = situation()
        payload['phaseTask'] = '当前尚未完成的任务'
        response = brain.plan_for_state(payload, state, judge_tasks=False)
        self.assertNotEqual(response.commands.get('10011', {}).get('action'), 'buy')

    def test_preparation_walk_is_not_overwritten_by_task_walk(self):
        payload, state = situation(shop=(18, 23))
        payload['mapInfo']['zones'].append({'pos': {'x': 6, 'y': 5}, 'neutralType': 'challengerTaskPoint1'})
        payload['teamOur']['playerTasks'] = [{'taskType': '自进化类1', 'taskPosition': {'x': 6, 'y': 5},
                                              'isValid': True, 'coldDownRounds': 0, 'timeoutRounds': 20}]
        response = brain.plan_for_state(payload, state, judge_tasks=False)
        move = response.commands['10011']
        self.assertEqual(move['action'], 'move')
        self.assertGreater(move['targetPos'][0]['x'], 14)

    def test_purchase_settles_in_actual_simulator(self):
        payload, state = situation(gold=55)
        payload['_demo']['planner'] = state.dump()
        result = simulator.step(payload)
        pioneer = next(r for r in result['state']['teamOur']['roles'] if r['roleType'] == 'pioneer')
        self.assertIn('AcientTablet', pioneer['backpack'])
        self.assertNotEqual(result['executed'].get('10011', {}).get('action'), 'summonTreasure')


if __name__ == '__main__':
    unittest.main()
