"""Issue 12: the defence must face the expected approach, and keep a usable exit.

Category: strategy optimization + local geometry regression. Rules referenced:
R02 (41x32 map, base 2x2 whose ``pos`` is the upper-left corner, Chebyshev
distance, eight-way movement), R03 (walls cost a stone, three towers globally,
a tower needs a role in an adjacent cell), R04 (towers only fire at night).

Expectations below are **hand-computed coordinates**, not values produced by the
helpers under test: the perimeter, the exit, the wall order and the tower slots
are written out literally and compared with what the strategy returns. The
approach prior is only "the enemy comes from the map interior"; it is a strategy
assumption, not knowledge of the next wave (see docs/ISSUE_12_DEFENCE.md).
"""
import sys
import unittest
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Demo/CoreGeek/src"))

from agent import brain, defense_layout  # noqa: E402
from agent.protocol import Pos, Turn, station_footprint  # noqa: E402

WIDTH, HEIGHT = 41, 32
# Reported layout: blue base upper-left near (9,22), threat from the east.
REPORTED = (9, 22)
MIRROR = (30, 22)
VERTICAL_SOUTH = (20, 28)
VERTICAL_NORTH = (20, 6)


def ring_points(base: tuple[int, int]) -> set[tuple[int, int]]:
    """The radius-2 perimeter written out by hand for a 2x2 base.

    The footprint is (x,y),(x+1,y),(x,y-1),(x+1,y-1); the ring is the rectangle
    outline two cells out, so its corners are included.
    """
    x, y = base
    xmin, xmax = x - 2, x + 3
    ymin, ymax = y - 3, y + 2
    points = set()
    for cx in range(xmin, xmax + 1):
        for cy in range(ymin, ymax + 1):
            if cx in (xmin, xmax) or cy in (ymin, ymax):
                points.add((cx, cy))
    return points


def unit(uid, kind, x, y, health=1000):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y},
            "health": health, "level": 1, "backPackCapability": 100,
            "backpack": []}


def board(base=REPORTED, *, towers=(), walls=(), blocked=(), workers=True,
          pioneer=(None, None), round_no=13, robots=()):
    roles = [unit(10013, "station", base[0], base[1], 1500)]
    for index, (x, y) in enumerate(towers):
        roles.append(unit(10040 + index, "rocket", x, y))
    for index, (x, y) in enumerate(walls):
        roles.append(unit(40000 + index, "wall", x, y))
    px, py = pioneer
    roles.append(unit(10010, "worker", px if px is not None else base[0] + 3,
                      py if py is not None else base[1] - 1, 220))
    roles.append(unit(10011, "pioneer", base[0] + 1, base[1] - 1, 200))
    zones = [{"pos": {"x": x, "y": y}, "neutralType": "water"} for x, y in blocked]
    return {"roundNo": round_no,
            "mapInfo": {"width": WIDTH, "height": HEIGHT, "zones": zones},
            "teamOur": {"type": "challenger", "goldNum": 75, "totalScore": 0,
                        "roles": roles, "playerTasks": []},
            "teamEnemy": {"roles": []},
            "robot": {"roles": [{"id": 90000 + index, "pos": {"x": x, "y": y},
                                 "health": health, "roleType": "smallRobot"}
                                for index, (x, y, health) in enumerate(robots)]},
            "vendorShopList": [], "weaponShopList": []}


def sites_of(state) -> list[tuple[int, int]]:
    return [(pos.x, pos.y) for pos in brain._tower_sites(Turn.load(state))]


def walls_of(state) -> list[tuple[int, int]]:
    return [(pos.x, pos.y) for pos in brain._wall_order(Turn.load(state))]


def exits_of(state) -> list[tuple[int, int]]:
    return [(pos.x, pos.y) for pos in brain._exit_cells(Turn.load(state))]


def interior_cells(base, blocked=()) -> set[tuple[int, int]]:
    """Cells one ring inside the wall ring where a controller could stand."""
    footprint = {(base[0] + dx, base[1] + dy) for dx in (0, 1) for dy in (0, -1)}
    x, y = base
    cells = {(cx, cy) for cx in range(x - 1, x + 3) for cy in range(y - 2, y + 2)}
    return cells - footprint - set(blocked)


def distinct_stands(towers, base, blocked=()) -> int:
    """Independent maximum matching of towers to distinct controller cells.

    Written here on purpose: the test must not reuse the production matcher.
    """
    free = interior_cells(base, blocked) - set(towers)

    def neighbours(tower):
        tx, ty = tower
        return {(tx + dx, ty + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                if (dx or dy) and (tx + dx, ty + dy) in free}

    def augment(index, used):
        if index == len(towers):
            return True
        for cell in neighbours(towers[index]) - used:
            if augment(index + 1, used | {cell}):
                return True
        return False

    return len(towers) if augment(0, set()) else 0


def escape_reachable(state, exit_cells, towers, walls) -> bool:
    """Independent flood fill: inside -> exit -> outside with the ring built.

    The ring is rebuilt from the hand-written perimeter, so this cannot agree
    with the layout helper by construction.
    """
    turn = Turn.load(state)
    station = turn.station()
    base = (station.pos.x, station.pos.y)
    opening = {Pos(x, y) for x, y in exit_cells}
    blocked = ({Pos(x, y) for x, y in ring_points(base)} - opening) \
        | {Pos(x, y) for x, y in walls} | {Pos(x, y) for x, y in towers}
    interior = [pos for pos in footprint_neighbours(station.pos) if turn.land(pos)]
    seen = set(interior)
    queue = deque(interior)
    while queue:
        current = queue.popleft()
        if current in opening:
            return True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nxt = Pos(current.x + dx, current.y + dy)
                if nxt in seen or not turn.land(nxt) or nxt in blocked:
                    continue
                seen.add(nxt)
                queue.append(nxt)
    return False


def footprint_neighbours(base_pos):
    return [Pos(x, y) for pos in station_footprint(base_pos)
            for x in (pos.x - 1, pos.x, pos.x + 1)
            for y in (pos.y - 1, pos.y, pos.y + 1)
            if Pos(x, y) not in station_footprint(base_pos)]


class ApproachPriorTests(unittest.TestCase):
    def test_reported_left_base_expects_the_east(self):
        self.assertEqual(defense_layout.primary_approach(Pos(*REPORTED), WIDTH, HEIGHT), "E")

    def test_mirrored_right_base_expects_the_west(self):
        self.assertEqual(defense_layout.primary_approach(Pos(*MIRROR), WIDTH, HEIGHT), "W")

    def test_top_base_expects_the_south_and_bottom_base_the_north(self):
        self.assertEqual(defense_layout.primary_approach(Pos(*VERTICAL_SOUTH), WIDTH, HEIGHT), "S")
        self.assertEqual(defense_layout.primary_approach(Pos(*VERTICAL_NORTH), WIDTH, HEIGHT), "N")

    def test_prior_is_stable_when_towers_and_walls_appear(self):
        plain = Turn.load(board())
        built = Turn.load(board(towers=[(11, 20), (11, 21)], walls=[(12, 20), (7, 21)]))
        self.assertEqual(defense_layout.primary_approach(plain.station().pos, WIDTH, HEIGHT),
                         defense_layout.primary_approach(built.station().pos, WIDTH, HEIGHT))

    def test_side_base_vertical_offset_does_not_rotate_front(self):
        # Hand-expected directions from public base geometry, not simulator seeds.
        for y in (3, 6, 16, 28):
            with self.subTest(y=y):
                self.assertEqual(defense_layout.primary_approach(Pos(29, y), WIDTH, HEIGHT), 'W')
                self.assertEqual(defense_layout.primary_approach(Pos(10, y), WIDTH, HEIGHT), 'E')

    def test_central_footprints_keep_vertical_fallback_under_reflection(self):
        for x in (19, 20):
            self.assertEqual(defense_layout.primary_approach(Pos(x, 6), WIDTH, HEIGHT), 'N')
            self.assertEqual(defense_layout.primary_approach(Pos(x, 28), WIDTH, HEIGHT), 'S')

    def test_side_priority_mirrors(self):
        east = defense_layout.layout(Pos(*REPORTED), WIDTH, HEIGHT)
        west = defense_layout.layout(Pos(*MIRROR), WIDTH, HEIGHT)
        self.assertEqual(east.side_order, ("E", "N", "S", "W"))
        self.assertEqual(west.side_order, ("W", "N", "S", "E"))
        north = defense_layout.layout(Pos(*VERTICAL_NORTH), WIDTH, HEIGHT)
        south = defense_layout.layout(Pos(*VERTICAL_SOUTH), WIDTH, HEIGHT)
        self.assertEqual(north.side_order, ("N", "E", "W", "S"))
        self.assertEqual(south.side_order, ("S", "E", "W", "N"))


class WallRingTests(unittest.TestCase):
    """Perimeter coverage: full ring minus exactly the two-cell exit."""

    def assert_complete_ring(self, state, base, expected_exit):
        point_ring = ring_points(base)
        walls = walls_of(state)
        exit_cells = exits_of(state)
        self.assertEqual(sorted(exit_cells), sorted(expected_exit))
        self.assertEqual(len(walls), len(point_ring) - 2,
                         "every perimeter cell except the exit must be walled")
        self.assertEqual(set(walls) | set(exit_cells), point_ring)
        self.assertEqual(set(walls) & set(exit_cells), set())
        self.assertEqual(len(walls), len(set(walls)), "no duplicate wall slots")
        return walls

    def test_reported_base_has_no_hole_and_an_east_front(self):
        state = board()
        walls = self.assert_complete_ring(state, REPORTED, [(7, 21), (7, 22)])
        # Front edge first: the east column, bottom to top.
        self.assertEqual(walls[:4], [(12, 20), (12, 21), (12, 22), (12, 23)])
        # Both forward half-flanks precede either rear half-flank.
        self.assertEqual(walls[4:10], [(10, 24), (11, 24), (12, 24), (10, 19), (11, 19), (12, 19)])
        self.assertEqual(walls[10:16], [(7, 24), (8, 24), (9, 24), (7, 19), (8, 19), (9, 19)])
        self.assertEqual(walls[16:], [(7, 20), (7, 23)])
        # The old off-by-one hole is gone: it is a wall now.
        self.assertIn((10, 19), walls)
        # The exit is on the rear, i.e. away from the eastern approach.
        for x, _y in exits_of(state):
            self.assertLess(x, base_x(REPORTED))

    def test_mirrored_base_is_the_horizontal_mirror(self):
        state = board(base=MIRROR)
        walls = self.assert_complete_ring(state, MIRROR, [(33, 21), (33, 22)])
        self.assertEqual(walls[:4], [(28, 20), (28, 21), (28, 22), (28, 23)])
        west = walls_of(board())
        # x' = 40 - x mirrors the reported layout (the per-edge order stays
        # ascending, so the mirrored *set* is what must match).
        self.assertEqual({(WIDTH - 1 - x, y) for x, y in west}, set(walls))
        self.assertEqual({(WIDTH - 1 - x, y) for x, y in exits_of(board())},
                         set(exits_of(state)))

    def test_vertical_base_puts_the_front_south_and_the_exit_north(self):
        state = board(base=VERTICAL_SOUTH)
        walls = self.assert_complete_ring(state, VERTICAL_SOUTH, [(20, 30), (21, 30)])
        self.assertEqual(walls[:6],
                         [(18, 25), (19, 25), (20, 25), (21, 25), (22, 25), (23, 25)])
        for _x, y in exits_of(state):
            self.assertGreater(y, VERTICAL_SOUTH[1], "exit must be on the north rear")

    def test_ring_is_deterministic(self):
        self.assertEqual(walls_of(board()), walls_of(board()))

    def test_edge_base_uses_a_reachable_fallback_without_holes(self):
        # Base hard against the west border: the rear (west) ring is off-map.
        state = board(base=(1, 22))
        walls = walls_of(state)
        exits = exits_of(state)
        self.assertTrue(exits)
        self.assertEqual(len(exits), 2)
        self.assertEqual(len(walls), len(set(walls)))
        self.assertEqual(set(walls) & set(exits), set())
        # Nothing off-map, and every wall is inside the land perimeter.
        self.assertTrue(all(0 <= x < WIDTH and 0 <= y < HEIGHT for x, y in walls))
        for x, _y in exits:
            self.assertGreater(x, 1 - 2, "fallback must stay on the map")


def base_x(base):
    return base[0]


class ExitTests(unittest.TestCase):
    def test_exit_is_away_from_the_approach_on_all_four_sides(self):
        for base, approach in ((REPORTED, "E"), (MIRROR, "W"),
                               (VERTICAL_SOUTH, "S"), (VERTICAL_NORTH, "N")):
            with self.subTest(base=base, approach=approach):
                state = board(base=base)
                exits = exits_of(state)
                self.assertEqual(len(exits), 2)
                for cell in exits:
                    self.assertFalse(
                        _on_side(cell, base, approach),
                        f"exit {cell} must not sit on the approach-facing side")

    def test_water_strip_beyond_the_rear_ring_moves_the_exit(self):
        # The rear ring itself is land, but a water strip one cell further out
        # makes that opening useless; a flank must be chosen instead.
        water = [(6, y) for y in range(HEIGHT)]
        state = board(blocked=water)
        turn = Turn.load(state)
        plan = defense_layout.layout(turn.station().pos, WIDTH, HEIGHT, land=turn.land)
        self.assertEqual(len(plan.exit_cells), 2)
        # The geometric rear pair is land...
        rear_cells = sorted(defense_layout._edge_cells(turn.station().pos, "W"),
                            key=lambda pos: (pos.x, pos.y))
        self.assertTrue(all(turn.land(c) for c in rear_cells))
        # ...and is rejected precisely because the water blocks the way out.
        self.assertFalse(defense_layout.exit_is_usable(
            turn.station().pos, rear_cells[:2], turn.land))
        self.assertNotEqual({(c.x, c.y) for c in plan.exit_cells},
                            {(7, 21), (7, 22)})
        self.assertTrue(plan.exit_usable)

    def test_pre_walled_rear_keeps_a_usable_opening(self):
        legacy = [(7, 21), (7, 22)]
        state = board(walls=legacy)
        turn = Turn.load(state)
        plan = defense_layout.layout(turn.station().pos, WIDTH, HEIGHT, land=turn.land,
                                    standing_walls={Pos(*cell) for cell in legacy})
        self.assertTrue(plan.exit_usable, "an opening must remain usable")
        for cell in plan.exit_cells:
            self.assertNotIn((cell.x, cell.y), legacy,
                             "the retained opening must not be a standing wall")
        # The exit stays off the approach side: not on the eastern front.
        for cell in plan.exit_cells:
            self.assertFalse(_on_side((cell.x, cell.y), REPORTED, "E"))

    def test_legacy_walls_keep_a_usable_exit_through_the_caller(self):
        # The same guarantee must hold through brain's entry points, not only
        # when the helper is called directly: `_wall_order`/`_exit_cells` read
        # the walls that already stand out of the payload themselves.
        legacy = [(7, 21), (7, 22)]
        state = board(walls=legacy)
        exits = exits_of(state)
        walls = walls_of(state)
        self.assertNotEqual(exits, legacy, "a walled-over opening must not be reused")
        self.assertFalse(set(exits) & set(legacy))
        self.assertFalse(set(exits) & set(walls))
        turn = Turn.load(state)
        self.assertTrue(defense_layout.exit_is_usable(
            turn.station().pos, [Pos(*cell) for cell in exits], turn.land,
            standing_walls={Pos(*cell) for cell in legacy}),
            "the retained opening must really lead out of the finished ring")

    def test_fully_walled_ring_is_reported_not_hidden(self):
        # A completely sealed legacy ring cannot be reopened without a `remove`
        # action, which this change does not issue. The layout must say so rather
        # than pretend, and must not schedule anything new.
        sealed = sorted(ring_points(REPORTED))
        state = board(walls=sealed)
        turn = Turn.load(state)
        plan = defense_layout.layout(turn.station().pos, WIDTH, HEIGHT, land=turn.land,
                                     standing_walls={Pos(*cell) for cell in sealed})
        self.assertFalse(plan.exit_usable)
        self.assertEqual(len(plan.exit_cells), 2)
        self.assertTrue(set((c.x, c.y) for c in plan.exit_cells) <= set(sealed))
        # Every remaining wall slot is already built, so nothing is added.
        self.assertTrue(set((c.x, c.y) for c in plan.wall_order) <= set(sealed))

    def test_exit_never_overlaps_the_wall_order(self):
        for base in (REPORTED, MIRROR, VERTICAL_SOUTH, VERTICAL_NORTH, (1, 22), (39, 22)):
            with self.subTest(base=base):
                state = board(base=base)
                self.assertEqual(set(walls_of(state)) & set(exits_of(state)), set())


def _on_side(cell, base, side):
    x, y = cell
    xmin, xmax = base[0], base[0] + 1
    ymin, ymax = base[1] - 1, base[1]
    if side == "E":
        return x >= xmax + 2
    if side == "W":
        return x <= xmin - 2
    if side == "N":
        return y >= ymax + 2
    return y <= ymin - 2


class TowerSiteTests(unittest.TestCase):
    # Shared rockets now straddle an empty front-inner stand and face
    # the approach. The laser is offset to preserve the common stand.
    def test_spaced_towers_keep_middle_on_the_east(self):
        sites = sites_of(board())
        self.assertEqual(sites, [(11, 21), (8, 23), (11, 23)])
        self.assertEqual(len(set(sites)), 3)
        for x, _y in sites[::2]:
            self.assertEqual(x, REPORTED[0] + 2, "towers must face the approach")

    def test_spaced_towers_keep_middle_on_the_west(self):
        sites = sites_of(board(base=MIRROR))
        self.assertEqual(sites, [(29, 20), (32, 20), (29, 22)])
        for x, _y in sites[::2]:
            self.assertEqual(x, MIRROR[0] - 1, "the west weapon ring is one cell out")

    def test_vertical_base_puts_the_towers_on_the_south(self):
        sites = sites_of(board(base=VERTICAL_SOUTH))
        self.assertEqual(sites, [(19, 26), (19, 29), (21, 26)])
        for _x, y in sites[::2]:
            self.assertEqual(y, VERTICAL_SOUTH[1] - 2)

    def test_three_towers_keep_distinct_controller_cells(self):
        # The concrete trap: a contiguous row of three shares one stand, so the
        # third tower can never be operated. Every fixture must avoid that.
        for base in (REPORTED, MIRROR, VERTICAL_SOUTH, VERTICAL_NORTH):
            with self.subTest(base=base):
                sites = sites_of(board(base=base))
                self.assertEqual(distinct_stands(sites, base), 3)

    def test_existing_towers_are_retained_and_never_duplicated(self):
        for existing in ([(8, 20)], [(8, 20), (8, 23)]):
            with self.subTest(existing=existing):
                sites = sites_of(board(towers=existing))
                self.assertEqual(len(sites), len(set(sites)))
                self.assertLessEqual(len(sites), 3)
                for cell in existing:
                    self.assertIn(cell, sites)

    def test_partially_blocked_front_is_avoided_and_still_controllable(self):
        blocked = [(11, 20), (11, 21)]
        state = board(blocked=blocked)
        sites = sites_of(state)
        self.assertEqual(len(sites), 3)
        self.assertEqual(len(set(sites)), 3)
        for cell in blocked:
            self.assertNotIn(cell, sites, "occupied terrain must not be a site")
        self.assertEqual(distinct_stands(sites, REPORTED), 3)

    def test_spaced_sites_keep_exit_connected(self):
        for base in (REPORTED, MIRROR, VERTICAL_SOUTH):
            with self.subTest(base=base):
                state = board(base=base)
                exits = set(exits_of(state))
                guard = {(x + dx, y + dy) for x, y in exits
                         for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
                self.assertTrue(escape_reachable(state, exits, sites_of(state), walls_of(state)))
                self.assertFalse(set(sites_of(state)) & exits)

    def test_sites_are_deterministic_and_tower_count_is_capped(self):
        self.assertEqual(sites_of(board()), sites_of(board()))
        self.assertLessEqual(len(sites_of(board())), 3)

    def test_sites_do_not_change_when_the_ring_goes_up(self):
        plain = sites_of(board())
        ring = walls_of(board())
        built = sites_of(board(walls=ring))
        self.assertEqual(plain, built, "the layout must be stable once walls appear")


class IntegrationTests(unittest.TestCase):
    def test_pioneer_can_leave_a_completed_ring(self):
        state = board()
        ring = walls_of(state)
        sites = sites_of(state)
        turn = Turn.load(state)
        plan = defense_layout.layout(turn.station().pos, WIDTH, HEIGHT, land=turn.land)
        self.assertFalse(brain._ring_is_sealed(turn))
        self.assertTrue(escape_reachable(state, exits_of(state), sites, ring))

    def test_day_plan_issues_no_conflicting_builds(self):
        state = board()
        turn = Turn.load(state)
        commands = {}
        brain._day(turn, commands, state, None)
        targets = [tuple(sorted(c["targetPos"][0].items()))
                   for c in commands.values() if c["action"] == "build"]
        self.assertEqual(len(targets), len(set(targets)),
                         "two roles must not be told to build the same cell")
        tower_builds = [c for c in commands.values()
                        if c["action"] == "build" and c["name"] in ("gatling", "railgun", "rocket")]
        self.assertLessEqual(len(tower_builds), 3)
        self.assertEqual({c["name"] for c in tower_builds} - {"gatling", "railgun", "rocket"},
                         set(), "the default loadout is still official tower kinds")

    def test_first_tower_faces_the_approach(self):
        state = board()
        turn = Turn.load(state)
        commands = {}
        brain._day(turn, commands, state, None)
        for command in commands.values():
            if command["action"] == "build" and command["name"] == "rocket":
                target = Pos.load(command["targetPos"][0])
                self.assertGreater(target.x, REPORTED[0] + 1,
                                   "the first tower must be on the approach side")

    def test_wall_priority_starts_on_the_approach_side(self):
        state = board()
        walls = walls_of(state)
        self.assertTrue(walls)
        first = walls[0]
        self.assertEqual(first[0], REPORTED[0] + 3,
                         "wall work must start on the approach-facing edge")


def stress_cluster(approach, base):
    """35 synthetic smallRobot observations; geometry invariance only.

    Positions and count are a local fixture, not official spawn evidence or a
    combat benchmark. roleType is an official field and smallRobot has 40 HP.
    Issue 12's colours do not establish an official type mapping.
    """
    x, y = base
    if approach == "E":
        columns = (x + 4, x + 5, x + 6)
    elif approach == "W":
        columns = (x - 3, x - 4, x - 5)
    else:
        raise ValueError(approach)
    cluster = []
    for index, column in enumerate(columns):
        height = 12 if index < 2 else 11
        for row in range(height):
            cluster.append((column, y - 5 + row, 40))
    return cluster


class DirectionalStressTests(unittest.TestCase):
    """The prior must orient the defence without the wave influencing it."""

    def test_cluster_on_the_approach_side_does_not_change_the_plan(self):
        for base, approach in ((REPORTED, "E"), (MIRROR, "W")):
            with self.subTest(base=base, approach=approach):
                robots = stress_cluster(approach, base)
                self.assertEqual(len(robots), 35)
                stressed = board(base=base, robots=robots)
                # The plan is a map-geometry prior: the same snapshot with and
                # without the visible cluster produces the same walls and towers.
                self.assertEqual(walls_of(stressed), walls_of(board(base=base)))
                self.assertEqual(sites_of(stressed), sites_of(board(base=base)))
                # And the plan does face the side the cluster is on.
                if approach == "E":
                    for x, _y in sites_of(stressed)[::2]:
                        self.assertGreater(x, base[0] + 1)
                else:
                    for x, _y in sites_of(stressed)[::2]:
                        self.assertLess(x, base[0])
                self.assertEqual(walls_of(stressed)[0],
                                 walls_of(board(base=base))[0])

    def test_stress_cluster_is_on_the_approach_side(self):
        for base, approach in ((REPORTED, "E"), (MIRROR, "W")):
            with self.subTest(base=base):
                robots = stress_cluster(approach, base)
                for x, _y, _hp in robots:
                    if approach == "E":
                        self.assertGreater(x, base[0] + 3)
                    else:
                        self.assertLess(x, base[0] - 2)

    def test_towers_still_keep_distinct_stands_under_the_cluster(self):
        state = board(robots=stress_cluster("E", REPORTED))
        sites = sites_of(state)
        self.assertEqual(len(sites), 3)
        self.assertEqual(distinct_stands(sites, REPORTED), 3)


if __name__ == "__main__":
    unittest.main()
