"""Independent forecast advice tests; integration/returns not claimed here."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Demo/CoreGeek/src'))
from agent.news_economy import sale_signals


def event(**updates):
    row = dict(id='n1', kind='inferred', resource='copper', startDay=2, endDay=2,
               priceDirection='down', priceAmount=1, priceBasis='absolute',
               evidence=[{'sourceId': 'public1', 'quote': '第二天铜价降为1金币'}])
    return dict(row, **updates)


class NewsEconomyTests(unittest.TestCase):
    def test_explicit_drop_produces_advice_without_rewriting_price(self):
        prices = {'copper': 5}
        events = [event()]
        before = deepcopy((prices, events))
        signal = sale_signals(prices, 1, events)['copper']
        self.assertEqual((signal['observed_price'], signal['inferred_price']), (5, 1))
        self.assertEqual(signal['source_ids'], ['public1'])
        self.assertEqual((prices, events), before)

    def test_current_observation_overrides_inconsistent_absolute_forecast(self):
        self.assertEqual(sale_signals({'copper': 1}, 1, [event(priceAmount=5)]), {})

    def test_direction_only_does_not_invent_absolute_price(self):
        signal = sale_signals({'copper': 5}, 1, [event(priceAmount=None, priceBasis='unknown')])
        self.assertIsNone(signal['copper']['inferred_price'])
        signal = sale_signals({'copper': 5}, 1, [event(priceAmount=20, priceBasis='percent')])
        self.assertIsNone(signal['copper']['inferred_price'])

    def test_uncertain_dates_missing_sources_and_nonforecast_days_do_not_accelerate(self):
        for change in ({'startDay': None}, {'endDay': None}, {'startDay': 1}, {'startDay': 3, 'endDay': 3},
                       {'evidence': []}, {'kind': 'observed'}, {'priceDirection': 'up'}):
            self.assertEqual(sale_signals({'copper': 5}, 1, [event(**change)]), {}, change)
        self.assertEqual(sale_signals({'copper': 5}, 10, [event(startDay=11, endDay=11)]), {})

    def test_conflicts_and_gap_ranges_suppress_only_affected_signals(self):
        rows = [event(), event(resource='iron')]
        signals = sale_signals({'copper': 5, 'iron': 3}, 1, rows, conflicts=[{'resource': 'copper'}])
        self.assertEqual(set(signals), {'iron'})
        self.assertEqual(sale_signals({'copper': 5}, 1, rows, gaps=[{'resource': 'copper', 'startDay': 2, 'endDay': 3}]), {})
        self.assertIn('copper', sale_signals({'copper': 5}, 1, rows, gaps=[{'resource': 'copper', 'startDay': 3, 'endDay': 4}]))

    def test_stone_reserve_unsellable_and_invalid_prices_are_not_overridden(self):
        self.assertEqual(sale_signals({'stone': 5}, 1, [event(resource='stone')]), {})
        for value in (0, -1, True, float('nan'), float('inf')):
            self.assertEqual(sale_signals({'copper': value}, 1, [event()]), {})
        self.assertEqual(sale_signals({}, 1, [event()]), {})
