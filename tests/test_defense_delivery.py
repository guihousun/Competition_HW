"""Delivery checks: cache invalidation and the actual web simulator API."""
import json
import threading
import unittest
from urllib.request import Request, urlopen

from test_defense_layout import board
from agent import brain, server
from agent.protocol import Turn


class DefenceDeliveryTests(unittest.TestCase):
    def test_terrain_change_invalidates_cached_rear_exit(self):
        original=Turn.load(board())
        self.assertEqual({(p.x,p.y) for p in brain._exit_cells(original)}, {(7,21),(7,22)})
        flooded=Turn.load(board(blocked=[(7,y) for y in range(19,25)]))
        self.assertEqual({(p.x,p.y) for p in brain._exit_cells(flooded)}, {(9,19),(10,19)})
        self.assertEqual({(p.x,p.y) for p in brain._exit_cells(original)}, {(7,21),(7,22)})

    def test_added_existing_tower_invalidates_cached_sites(self):
        brain._tower_sites(Turn.load(board()))
        sites=brain._tower_sites(Turn.load(board(towers=[(8,20)])))
        self.assertEqual(len(sites),3)
        self.assertIn((8,20),[(p.x,p.y) for p in sites])

    def test_web_simulator_builds_approach_towers_and_front_walls_on_both_sides(self):
        http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{http.server_port}'
        try:
            with urlopen(url+'/',timeout=5) as response:
                self.assertIn(b'<html',response.read().lower())
            for side in ('challenger','defender'):
                with self.subTest(side=side):
                    with urlopen(url+f'/debug/scenario?seed=1&side={side}',timeout=5) as response:
                        state=json.load(response)['state']
                    base=next(r for r in state['teamOur']['roles'] if r['roleType']=='station')['pos']
                    first_wall=None
                    for _ in range(70):
                        with urlopen(Request(url+'/debug/step',data=json.dumps(state).encode(),
                                             headers={'Content-Type':'application/json'}),timeout=5) as response:
                            result=json.load(response)
                        state=result['state']
                        for command in result.get('roleCommandMap',{}).values():
                            if command.get('action')=='build' and command.get('name')=='wall' and first_wall is None:
                                first_wall=command['targetPos'][0]
                    towers=[r for r in state['teamOur']['roles'] if r['roleType'] in ('rocket','gatling','railgun')]
                    self.assertEqual(len(towers),3)
                    self.assertIsNotNone(first_wall)
                    if side=='challenger':
                        self.assertTrue(all(r['pos']['x']>base['x']+1 for r in towers if r['roleType']=='railgun'))
                        self.assertEqual(first_wall['x'],base['x']+3)
                    else:
                        self.assertTrue(all(r['pos']['x']<base['x'] for r in towers if r['roleType']=='railgun'))
                        self.assertEqual(first_wall['x'],base['x']-2)
        finally:
            http.shutdown();http.server_close();thread.join(timeout=3)
