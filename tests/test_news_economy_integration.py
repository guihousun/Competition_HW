"""Public news -> real planner -> current-price trade, with independent board."""
from copy import deepcopy
from unittest.mock import patch
from test_news_ledger import LedgerHarness
from test_metal_economy import board, VENDOR
from agent import brain, planner
from agent.news_economy import sale_signals
from agent.protocol import Turn
from agent.simulator import step


class NewsEconomyIntegration(LedgerHarness):
    TEXT = '游戏第1天，明天铜价下降到1金币。'

    def news(self):
        world, sid = self.agent_with(self.TEXT, round_no=1)
        self.submit(world, [self.news_event(sid, self.TEXT, resource='copper', availability='available',
                    startDay=2, endDay=2, priceDirection='down', priceAmount=1, priceBasis='absolute')])
        return world

    def bind(self, state, world):
        turn = Turn.load(state)
        forecast = world.economic_view(131)
        signals = sale_signals(brain.vendor_prices(state), 1, **forecast)
        token = brain._WORLD_VIEW.set((turn, {'unavailable': [], 'sale_signals': signals}))
        self.addCleanup(brain._WORLD_VIEW.reset, token)
        return turn

    def test_one_unit_gets_a_vendor_mission_only_after_validated_news(self):
        state = board(backpack=['copper'])
        turn = Turn.load(state)
        self.assertFalse(brain._should_sell(turn, turn.workers()[0], state))
        turn = self.bind(state, self.news())
        self.assertTrue(brain._forecast_sale_trip_fits(turn, turn.workers()[0]))
        self.assertTrue(brain._should_sell(turn, turn.workers()[0], state))
        commands, errands = {}, {}
        self.assertTrue(brain._start_errand(turn, turn.workers()[0], commands, state, errands))
        self.assertEqual(errands, {'10010': {'goal': 'vendor'}})
        self.assertEqual(commands[10010]['action'], 'move')

    def test_real_planner_populates_advice_from_public_memory_for_both_sides(self):
        for side in ('challenger', 'defender'):
            state = board(backpack=['copper'])
            state['teamOur']['type'] = side
            state['worldNews']['officialNews'] = self.TEXT
            memory = planner.PlannerState()
            memory.ensure_team_agent(state).world = self.news()
            with patch.dict('os.environ', {brain.WORLD_AGENT_ENV: 'on'}):
                result = brain.plan_for_state(state, memory, judge_tasks=False).build()
            self.assertEqual(result['roleCommandMap']['10010']['action'], 'move')
            self.assertIn('copper', brain.decision_report()['news_economy'])
            self.assertIn('copper', memory.dump()['tasks']['supervisor']['news_economy'])
            self.assertLessEqual(len(result['roleCommandMap']), 3)

    def test_return_deadline_missing_towers_or_walls_and_unreachable_vendor_keep_baseline(self):
        cases = [board(backpack=['copper'], round_no=55), board(backpack=['copper'], towers=2),
                 board(backpack=['copper'], walls='missing'), board(backpack=['copper'], vendor=None),
                 board(backpack=['copper'], vendor=(40, 0), round_no=45)]
        for state in cases:
            turn = self.bind(state, self.news())
            self.assertFalse(brain._should_sell(turn, turn.workers()[0], state))

    def test_settlement_uses_current_quote_and_preserves_stone(self):
        state = board(backpack=['iron', 'copper', 'stone', 'stone'])
        worker = state['teamOur']['roles'][1]
        worker['pos'] = {'x': VENDOR[0] + 1, 'y': VENDOR[1]}
        turn = self.bind(state, self.news())
        commands = {}
        self.assertTrue(brain._try_trade(turn, turn.workers()[0], commands, state))
        self.assertEqual(commands[10010], {'action': 'sell', 'name': 'copper', 'num': 1})
        result = step(deepcopy(state), external_response={'roleCommandMap': {str(k): v for k, v in commands.items()}})
        next_state = result[0] if isinstance(result, tuple) else result['state']
        self.assertEqual(next_state['teamOur']['goldNum'], 5)
        next_worker = next(r for r in next_state['teamOur']['roles'] if r['id'] == 10010)
        self.assertEqual(next_worker['backpack'].count('stone'), 2)
        self.assertNotIn('copper', next_worker['backpack'])

    def test_news_changes_alphabetical_sale_order_at_vendor(self):
        text = '游戏第1天，明天铁价下降到1金币。'
        world, sid = self.agent_with(text, round_no=1)
        self.submit(world, [self.news_event(sid, text, startDay=2, endDay=2,
                    availability='available', priceDirection='down', priceAmount=1, priceBasis='absolute')])
        state = board(backpack=['iron', 'copper'])
        state['teamOur']['roles'][1]['pos'] = {'x': VENDOR[0] + 1, 'y': VENDOR[1]}
        ordinary = Turn.load(state)
        old_commands = {}
        brain._try_trade(ordinary, ordinary.workers()[0], old_commands, state)
        self.assertEqual(old_commands[10010]['name'], 'copper')
        turn = self.bind(state, world)
        new_commands = {}
        brain._try_trade(turn, turn.workers()[0], new_commands, state)
        self.assertEqual(new_commands[10010]['name'], 'iron')

    def test_corrected_forecast_and_other_turn_context_do_not_trigger_sale(self):
        world = self.news()
        correction = '游戏第1天，更正：第2天铜价维持5金币，不下降。'
        cid = self.add_source(world, correction, 2)
        old_id = world.news_events[0]['id']
        self.submit(world, [self.news_event(cid, correction, resource='copper', availability='available',
                    startDay=2, endDay=2, priceDirection='unchanged',
                    resolution={'kind': 'corrected', 'targetId': old_id,
                                'evidence': [{'sourceId': cid, 'quote': correction}]})])
        state = board(backpack=['copper'])
        turn = self.bind(state, world)
        self.assertFalse(brain._should_sell(turn, turn.workers()[0], state))
        self.bind(state, self.news())
        separate = Turn.load(state)
        self.assertFalse(brain._should_sell(separate, separate.workers()[0], state))
