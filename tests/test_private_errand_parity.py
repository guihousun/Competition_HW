"""A retained local errand must not create a second, viewer-only strategy."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import os

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Demo/CoreGeek/src'))
from agent import brain,planner


class PrivateErrandParityTests(unittest.TestCase):
    def fixture(self):
        data=json.loads((Path(__file__).parent/'fixtures/navigation/seed7-r50-private-errand-parity.json').read_text(encoding='utf8'))
        self.assertNotIn('_demo',data['payload'])
        return data

    def test_private_vendor_ledger_cannot_change_direct_or_viewer_commands(self):
        data=self.fixture();plain=deepcopy(data['payload']);rich=deepcopy(plain)
        rich['_demo']={'errands':{'10010':{'goal':'vendor'}}}
        with patch.dict(os.environ,{brain.TASK_AGENT_ENV:'off',brain.WORLD_AGENT_ENV:'off',brain.ROUTER_ENV:'off'}):
            left=brain.plan_for_state(plain,planner.PlannerState.load(deepcopy(data['planner'])),judge_tasks=False).commands
            right=brain.plan_for_state(rich,planner.PlannerState.load(deepcopy(data['planner'])),judge_tasks=False).commands
        self.assertEqual(left,right)

    def test_private_ledger_is_ignored_without_erasing_public_trip_memory(self):
        data=self.fixture();plain=deepcopy(data['payload']);rich=deepcopy(plain)
        rich['_demo']={'errands':{'10010':{'goal':'shop'},'10011':{'goal':'altar','site':{'x':1,'y':1}}}}
        initial=deepcopy(data['planner']);a=planner.PlannerState.load(initial);b=planner.PlannerState.load(deepcopy(initial))
        with patch.dict(os.environ,{brain.TASK_AGENT_ENV:'off',brain.WORLD_AGENT_ENV:'off',brain.ROUTER_ENV:'off'}):
            left=brain.plan_for_state(plain,a,judge_tasks=False).commands
            right=brain.plan_for_state(rich,b,judge_tasks=False).commands
        self.assertEqual(left,right)
        self.assertEqual(a.dump(),b.dump())

    def test_json_preview_keeps_memory_and_ignores_private_ledger(self):
        data=self.fixture();a=planner.PlannerState.load(data['planner']);b=planner.PlannerState.load(json.loads(json.dumps(data['planner'])))
        plain=deepcopy(data['payload']);rich=deepcopy(plain);rich['_demo']={'errands':{'10010':{'goal':'vendor'}}}
        before_a=deepcopy(a.dump());before_b=deepcopy(b.dump());private_before=deepcopy(rich['_demo'])
        with patch.dict(os.environ,{brain.TASK_AGENT_ENV:'off',brain.WORLD_AGENT_ENV:'off',brain.ROUTER_ENV:'off'}):
            left=brain.plan_for_state(plain,a,commit=False,judge_tasks=False).commands
            right=brain.plan_for_state(rich,b,commit=False,judge_tasks=False).commands
        self.assertEqual(left,right)
        self.assertEqual(a.dump(),before_a);self.assertEqual(b.dump(),before_b)
        self.assertEqual(rich['_demo'],private_before)


if __name__=='__main__':unittest.main()
