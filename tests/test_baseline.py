import copy
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import socket

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))
from agent.brain import decide
from agent import brain
from agent.grid import next_step
from agent.protocol import Pos, Turn


def fixture():
    return json.loads((ROOT / "docs/request.txt").read_text(encoding="utf-8"))


class BaselineTests(unittest.TestCase):
    def test_day_budget_and_tower_limit(self):
        p = fixture()
        p["roundNo"] = 1
        p["teamOur"]["goldNum"] = 25
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"]
                                      if r["roleType"] in ("station", "worker")]
        workers = [r for r in p["teamOur"]["roles"] if r["roleType"] == "worker"]
        workers[0]["pos"] = {"x": 8, "y": 21}
        workers[1]["pos"] = {"x": 8, "y": 23}
        turn = Turn.load(p)
        commands = decide(p)
        # 25 gold buys at most one tower, and only an official tower kind.
        builds = [c for c in commands.values() if c["action"] == "build"]
        self.assertLessEqual(len(builds), 1)
        for build in builds:
            self.assertIn(build["name"], ("gatling", "railgun", "rocket"))
        # Current three-rocket strategy prefers a common post over front bias.
        # Budget and max-tower invariants still apply while workers approach.
        sites = brain._tower_sites(turn)
        self.assertTrue(sites, "a missing tower must still be planned")
        station = turn.station().pos
        inner = {Pos(x,y) for x in range(station.x-1,station.x+3)
                 for y in range(station.y-2,station.y+2)} - set(sites)
        inner -= set(turn.footprint(turn.station()))
        self.assertTrue(any(all(max(abs(p.x-g.x),abs(p.y-g.y))==1 for g in sites)
                            for p in inner), 'planned rockets share a legal inner post')
        if not builds:
            self.assertTrue(any(c["action"] == "move" for c in commands.values()),
                            "with a tower planned but not yet adjacent, a worker moves")
        p = fixture()
        p["roundNo"] = 1
        self.assertFalse(any(c["action"] == "build" and c["name"] != "wall"
                             for c in decide(p).values()))

    def test_enemy_obstacles_and_noop_path(self):
        p = fixture()
        enemy = copy.deepcopy(p["teamOur"]["roles"][0])
        enemy["pos"] = {"x": 6, "y": 23}
        p["teamEnemy"] = {"roles": [enemy]}
        turn = Turn.load(p)
        worker = turn.workers()[0]
        self.assertIn(Pos(7, 22), turn.blocked(worker))
        self.assertIsNone(next_step(turn, worker, worker.pos))
        self.assertIsNone(next_step(turn, worker, Pos(6, 23)))

    def test_night_attack_and_cooldown(self):
        p = fixture()
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"]
                                      if r["roleType"] in ("station", "rocket", "worker")][:3]
        worker = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "worker")
        tower = next(r for r in p["teamOur"]["roles"] if r["roleType"] == "rocket")
        worker["pos"] = {"x": 9, "y": 24}  # inside the planned ring, adjacent to rocket
        tower.update(level=3, cooldown=0)
        p["robot"] = {"roles": [{"id": 1, "pos": {"x": 7, "y": 25}, "health": 100}]}
        command = decide(p)[str(tower["id"])]
        self.assertEqual(command["action"], "attack")
        self.assertEqual(command["controllerId"], str(worker["id"]))
        self.assertEqual(len(command["targetPos"]), 3)
        tower["cooldown"] = 2
        self.assertEqual(decide(p), {})

    def test_real_http_entrypoint(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        process = subprocess.Popen([sys.executable, str(ROOT / "main.py"), str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}/"
            for attempt in range(100):
                if process.poll() is not None:
                    self.fail("HTTP server exited during startup")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.1):
                        break
                except OSError:
                    time.sleep(.05)
            with urlopen(url, timeout=5) as response:
                self.assertIn('策略调试台', response.read().decode('utf-8'))
            with urlopen(url + 'sample', timeout=5) as response:
                self.assertEqual(json.load(response), fixture())
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(url + 'debug/decide', data=b'invalid'), timeout=5)
            self.assertEqual(error.exception.code, 400)
            self.assertIn('error', json.load(error.exception))
            with self.assertRaises(HTTPError) as error:
                urlopen(url + '../main.py', timeout=5)
            self.assertEqual(error.exception.code, 404)
            for round_no in (1, 70, 71, 85, 130, 131):
                p = fixture()
                p["roundNo"] = round_no
                req = Request(url, data=json.dumps(p).encode(),
                              headers={"Content-Type": "application/json"})
                with urlopen(req, timeout=5) as response:
                    result = json.load(response)
                    self.assertEqual(response.status, 200)
                self.assertEqual(result["roleCommandMap"], decide(p))
                self.assertTrue(result["roleCommandMap"])
            with urlopen(Request(url, data=b"invalid json"), timeout=5) as response:
                self.assertEqual(json.load(response), {"roleCommandMap": {}})
        finally:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
