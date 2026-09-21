"""Explicit old-policy regression scope; new default is tested separately.

These fixtures assert historical R/E/R placement, three-gunner arbitration and
uncapped weapon-first purchases. Preserve those rollback guarantees without
mistaking their expected decisions for the new user-selected three-rocket plan.
"""
from contextlib import ExitStack
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch


class LegacyStrategyCase(TestCase):
    def run(self, result=None):
        from agent import brain, strategy_config
        legacy=deepcopy(strategy_config.DEFAULTS)
        legacy['enabled']=False
        with ExitStack() as stack:
            stack.enter_context(patch.object(strategy_config,'get',return_value=legacy))
            stack.enter_context(patch.object(brain,'_STRATEGY',legacy))
            stack.enter_context(patch.object(brain,'TOWER_LOADOUT',('rocket','railgun','rocket')))
            return super().run(result)
