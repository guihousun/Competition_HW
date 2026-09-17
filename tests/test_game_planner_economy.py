"""game/planner.py 经济线的用例：采矿（可行矿筛选、顺路卖）/ 卖矿 / 升级券线。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _records, _terrain  # noqa: E402
from coregeek.agent import AGENT  # noqa: E402
from coregeek.app import handle  # noqa: E402
from coregeek.game.grid import Pos, step_toward, wall_cells  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import TIME_MARGIN, WALL, plan  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Turn, Weapon  # noqa: E402


class MineApproachTest(unittest.TestCase):
    """合成局面：工人真的能走到矿边、开始采，而且不来回抖。

    必须有基地：矿只是砌墙的原料，基地没了就没有围墙环 ⇒ 采了也没用、工人干脆不动
    （`_ring` 返回空）—— 这个降级方向是有意的，所以 `_turn` 里摆了一个基地。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def _turn(self, worker_pos: Pos, mine_kind: str = "stone", stone: int = 0) -> Turn:
        return Turn(
            round_no=1,
            map=Map((41, 32), {self.BASE: "station", self.MINE: mine_kind}),
            roles=(Worker(1, worker_pos, {"stone": stone}),),
            gold=0,
        )

    def test_worker_walks_to_the_mine_and_then_harvests_in_place(self):
        """把回合串起来跑，看它收敛：走得到矿边，到了就原地采，不再挪。

        单帧"目标格算得对"证明不了这件事 —— 走歪、绕圈、贴住后反复抖动都是单帧看不出的。
        """
        turn = self._turn(Pos(20, 20))
        for _ in range(30):
            cmd = plan(turn).get("1")
            self.assertIsNotNone(cmd, "工人不该空着手不动：矿还在，也没到没时间的时候")
            if cmd["action"] == "collect":
                break
            spot = cmd["targetPos"][0]
            turn = turn._replace(roles=(Worker(1, Pos(spot["x"], spot["y"])),))
        else:
            self.fail("30 回合还没走到矿边，说明在原地绕圈")

        self.assertEqual(
            (cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"]),
            self.MINE,
            "`collect` 的 targetPos 是**矿的坐标**，不是自己的站位（接口文档 §2.3）",
        )
        self.assertEqual(turn.roles[0].pos.dist(self.MINE), 1, "应该停在贴着矿的那一格")
        # 贴住之后就该一直采：采一下、挪一下地反复抖 = 白白浪费白天
        for _ in range(3):
            self.assertEqual(plan(turn)["1"]["action"], "collect", "贴住矿之后不该再挪")

    def test_no_stone_mine_means_no_action(self):
        """场上只有铁矿 → 工人原地不动，而不是随便找个矿走过去（墙只吃石头）。"""
        self.assertEqual(plan(self._turn(Pos(20, 20), "iron")), {})


class SpareOreTest(unittest.TestCase):
    """墙砌满之后的白天：去采收购价最高的矿。

    「手里面保持能建造墙的石头量就行，然后选择价格最高的矿」—— 那个"然后"是顺序：
    砌墙阶段只认石矿（铜再贵也砌不了墙），砌完之后才轮到按价格挑；顺序反了的症状是
    "工人一直在采铜、围墙一格没有"。

    价目逐回合从载荷 `vendorShopList` 读（`Turn.vendor_prices`），不写死"铜 > 铁 > 石"：
    样例那三档 1/3/5 只是样例，任务书 L386 明说官方消息会让价格波动。所以这里专门把顺序
    翻过来测 —— 谁把铜写死在最前，哪一条就挂。
    """

    BASE = Pos(10, 24)
    #: 三座武器先摆好 —— 否则名额/金币会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 14 格全砌满 ⇒ `_ring` 空 ⇒ 进入"墙砌完了"那一支
    RING = {c: WALL for c in wall_cells(Pos(10, 24), 41)}
    #: 一近一远两座矿，近的便宜、远的贵 —— 远近与贵贱分开，才测得出按哪个排
    NEAR_IRON = Pos(34, 24)
    FAR_COPPER = Pos(20, 24)
    #: 样例的价目（`vendorShopList`）：铜 5 > 铁 3 > 石 1
    SAMPLE_PRICES = {"stone": 1, "iron": 3, "copper": 5}

    def _turn(
        self,
        pos: Pos,
        prices: dict[str, int],
        *,
        ring: bool = True,
        round_no: int = 1,
        ores: dict[Pos, str] | None = None,
    ) -> Turn:
        entries = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            self.RING if ring else {},
            ores if ores is not None else {self.NEAR_IRON: "iron", self.FAR_COPPER: "copper"},
        )
        return Turn(
            round_no=round_no,
            map=Map((41, 32), entries),
            roles=(Worker(1, pos, {}),),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=prices,
        )

    def _move_to(self, turn: Turn) -> Pos:
        """本回合那条 `move` 的落点。不是 `move` 就挂 —— 这几条只关心往哪边走。"""
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "move", "这一回合该是赶路，不是别的")
        spot = cmd["targetPos"][0]
        return Pos(spot["x"], spot["y"])

    def _collect_at(self, turn: Turn) -> Pos:
        """本回合那条 `collect` 瞄的是哪座矿 —— 比"朝哪边走一格"结实得多。"""
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "collect", "这一回合该是采集，不是别的")
        spot = cmd["targetPos"][0]
        return Pos(spot["x"], spot["y"])

    def test_the_pricier_ore_wins_over_the_nearer_one(self):
        """铜 5 > 铁 3 ⇒ 走远的铜，不走近的铁。按"最近"挑（砌墙阶段的口径）会挑中铁。"""
        start = Pos(30, 24)
        step = self._move_to(self._turn(start, self.SAMPLE_PRICES))
        self.assertLess(step.dist(self.FAR_COPPER), start.dist(self.FAR_COPPER), "该朝铜矿走")
        self.assertGreater(step.dist(self.NEAR_IRON), start.dist(self.NEAR_IRON), "不该朝铁矿走")

    def test_a_market_flip_changes_which_mine_we_walk_to(self):
        """铁矿塌方 ⇒ 铁稀缺 ⇒ 收购价涨过铜：同一个局面，走的方向必须反过来。

        谁把"铜 > 铁 > 石头"当常量写进代码，这一条就挂 —— 而事件期间那个常量恰好是错的。
        """
        start = Pos(30, 24)
        step = self._move_to(self._turn(start, {"stone": 1, "iron": 9, "copper": 5}))
        self.assertLess(step.dist(self.NEAR_IRON), start.dist(self.NEAR_IRON), "该朝铁矿走")

    def test_a_mine_the_vendor_does_not_buy_is_not_worth_a_step(self):
        """小贩不收的矿一步都不为它走（查不到的名字按 0 算）：宁可多走几步去那座收的。

        近的铜矿不在价目表里 ⇒ 价是 0，而 `_pick_ore` 把价 0 的整座丢掉 —— "离得近"不构成
        理由。三座都不收 ⇒ 一条指令都不发，与 `_gold` / `_size` / `_stone` 同一条降级方向。
        """
        start = Pos(30, 24)
        step = self._move_to(self._turn(start, {"iron": 3}))  # 只有铁有价，铜按 0 算
        self.assertLess(step.dist(self.NEAR_IRON), start.dist(self.NEAR_IRON), "该舍近求远")
        for prices in ({}, {"stone": 1}):  # 场上这两种矿，小贩一种都不收
            with self.subTest(prices=prices):
                self.assertEqual(plan(self._turn(start, prices)), {}, "谁也不收 ⇒ 哪儿也不去")

    def test_too_late_in_the_day_to_walk_there_and_back(self):
        """白天不够走个来回了 ⇒ 不去矿上，回炮位（收工闸门）。

        临走一头扎进远处的矿、黑天里还在赶路 = 拿火力换矿石。同一局面只差 `roundNo`：
        `roundNo=60` 时 `day_rounds_left - TIME_MARGIN` = 6，而这一趟来回要 20 回合，
        环又砌满了 ⇒ 收工闸门开着，远矿不去了、人往回赶。
        """
        start = Pos(30, 24)
        early = plan(self._turn(start, self.SAMPLE_PRICES, round_no=1))
        late = plan(self._turn(start, self.SAMPLE_PRICES, round_no=60))["1"]
        self.assertIn("1", early, "白天还长 ⇒ 该动身")
        step = Pos(late["targetPos"][0]["x"], late["targetPos"][0]["y"])
        self.assertLess(step.dist(self.BASE), start.dist(self.BASE), f"该往回赶：{late}")

    def test_the_ring_decides_whether_price_gets_a_vote(self):
        """同一个局面，只差围墙砌没砌满：砌着 ⇒ 只认石矿，砌完了 ⇒ 才按价格挑。

        两座矿都贴在工人身边（一边一座），两种口径给的是一左一右、无从含糊 —— 比"朝哪边
        走一格"结实：走一格常常同时靠近两座矿，那种写法会假通过。
        """
        stone, copper = Pos(21, 24), Pos(19, 24)
        start = Pos(20, 24)
        self.assertEqual(start.dist(stone), 1, "石矿得贴着工人")
        self.assertEqual(start.dist(copper), 1, "铜矿也得贴着 —— 两边都有得选才测得出东西")
        prices = {"stone": 1, "copper": 5}  # 铜更贵
        ores = {stone: "stone", copper: "copper"}

        self.assertEqual(
            self._collect_at(self._turn(start, prices, ring=False, ores=ores)),
            stone,
            "墙还没砌完 ⇒ 只认石矿，铜再贵也不看一眼",
        )
        self.assertEqual(
            self._collect_at(self._turn(start, prices, ring=True, ores=ores)),
            copper,
            "墙砌完了 ⇒ 才轮到最值钱的铜",
        )

    def test_a_feasible_cheap_mine_beats_an_infeasible_pricy_one(self):
        """先把"走得动、回得来"的矿筛出来、再按价挑 —— 只按价挑的话，挑中最贵的又走不
        回来就整段放弃、工人闲置。回程参照 = 最近的武器位（夜里得在炮前站岗）。"""
        cheap, pricy = Pos(14, 24), Pos(34, 24)
        ores = {cheap: "stone", pricy: "copper"}
        # round_no=55 ⇒ 白天剩 16，扣余量 5 ⇒ 11：近处石头 1+5=6 走得动，
        # 远处铜 19+25=44 走不动 —— 不筛可行性的话会因此整段放弃（{}）
        turn = self._turn(Pos(15, 24), self.SAMPLE_PRICES, ores=ores, round_no=55)
        self.assertEqual(self._collect_at(turn), cheap, "铜来不及回 ⇒ 就近采石头，别闲置")

    def test_sells_on_the_way_when_the_vendor_is_close_to_the_route(self):
        """顺路卖矿：去矿的路上，小贩绕路 ≤ 2 格 ⇒ 先绕去卖（贴上它的那回合 `_sell_ore`
        自然出手），之后再继续去矿。

        夹具故意让 `_sell_ore` 的"够本门"不成立（1 块铜值 5 < 2×4），顺路这条才会被单独
        点亮。小贩在东南、矿在正东：绕路 4+6-10=0 格，正"在路上"。"""
        mine = Pos(30, 24)
        vendor = Pos(24, 28)
        entries = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            self.RING,
            {mine: "iron"},
            {vendor: "vendor"},
        )
        turn = Turn(
            round_no=1,
            map=Map((41, 32), entries),
            roles=(Worker(1, Pos(20, 24), {"copper": 1}),),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=self.SAMPLE_PRICES,
        )
        step = self._move_to(turn)
        self.assertLess(
            step.dist(vendor),
            Pos(20, 24).dist(vendor),
            "该朝小贩（东南）绕一步，不是直奔矿去",
        )

    def test_the_reserved_stone_is_not_worth_a_detour(self):
        """保底留的那 1 块石头不值得绕路去卖：`_detour_sell` 与 `_sell_ore` 共用
        `_best_load` 一个口径 —— 否则会"绕到小贩旁边才发现自己不肯卖那 1 块石头"，白绕一趟，
        而且两处口径迟早分家。

        同一局面只差背包里 1 块还是 2 块：2 块 ⇒ 多出那块可卖、顺路绕小贩；1 块 ⇒
        那块留着封正面那个口、谁也不卖，于是直奔矿去。"""
        mine, vendor, start = Pos(30, 24), Pos(24, 28), Pos(20, 24)

        def step_for(stone: int) -> Pos:
            entries = _terrain(
                self.WEAPONS,
                {self.BASE: "station"},
                self.RING,
                {mine: "iron"},
                {vendor: "vendor"},
            )
            turn = Turn(
                round_no=1,
                map=Map((41, 32), entries),
                roles=(Worker(1, start, {"stone": stone}),),
                gold=0,
                weapons=self.WEAPONS,
                vendor_prices=self.SAMPLE_PRICES,
            )
            return self._move_to(turn)

        detour, straight = step_for(2), step_for(1)
        self.assertLess(detour.dist(vendor), start.dist(vendor), "有货可卖 ⇒ 顺路绕小贩")
        self.assertNotEqual(straight, detour, "留作封口的 1 块石头不该把人带去绕路")

    def test_a_colleague_in_the_pocket_never_stops_the_other_worker(self):
        """同事停在盒子里 ⇒ 另一个工人照样出门采矿（估算距离不算自己人）。

        环砌满之后盒子内只剩几条一格宽的走廊：`model._entries` 把我方角色写进网格 ⇒ 旧口径
        下"矿 → 最近的炮位"（`_mine_spare_ore` 的 `back`）对**每一座矿**都是 -1 ⇒ 全不可行
        ⇒ 整段放弃、这一回合一条指令都不发。同事也正忙着自己的事不动 ⇒ 两人一起卡在原地，
        一整天一格都没挪。自己人在"路有多远"里只是路过，实际走路那一套（`_Queue.step`）照旧
        把他当硬障碍 —— 两套口径分工不同。
        """
        weapons = _records(
            {Pos(12, 24): "rocket", Pos(12, 25): "rocket", Pos(12, 22): "gatling"}
        )
        mine = Pos(4, 24)
        a, b = Worker(10012, Pos(11, 22), {}), Worker(10013, Pos(11, 25), {})
        grid = _terrain(weapons, {self.BASE: "station"}, self.RING, {mine: "stone"})
        grid |= {a.pos: "worker", b.pos: "worker"}
        turn = Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(a, b),
            gold=0,
            weapons=weapons,
            vendor_prices=self.SAMPLE_PRICES,
        )
        cmd = plan(turn)["10012"]
        self.assertEqual(cmd.get("action"), "move", f"该出门采矿，不是原地不动：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(mine), a.pos.dist(mine), "朝矿走")

    def test_standing_next_to_the_shop_never_blocks_the_mine_trip(self):
        """已经贴着商店 ⇒ 顺路买那一支直接放弃，别把它当成"绕一步"。

        `_detour_buy` 的绕法是"朝商店迈一步"，而贴着商店时那一步根本不存在（`step_toward`
        返回 None）⇒ 意图排了、第二段发不出任何东西、`_mine_spare_ore` 又已经早返回 ⇒ 整个
        回合空指令。买券那条线自己有跑腿者（`_upgrade_line`），这里排不上就不是它的事。
        """
        shop, mine = Pos(24, 28), Pos(30, 24)
        weapons = _records(
            {Pos(12, 24): "rocket", Pos(12, 25): "rocket", Pos(12, 22): "gatling"}
        )
        a = Worker(10012, Pos(24, 27), {})  # 贴着商店，也是名册第二个 ⇒ 跑腿轮不到它
        b = Worker(10013, Pos(20, 20), {})
        grid = _terrain(
            weapons, {self.BASE: "station"}, self.RING, {mine: "iron"}, {shop: "weaponShop"}
        )
        grid |= {a.pos: "worker", b.pos: "worker"}
        turn = Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(b, a),  # 名册第一个是 b ⇒ `_upgrade_line` 的跑腿者不是 a
            gold=100,
            weapons=weapons,
            vendor_prices=self.SAMPLE_PRICES,
            shop_prices={"WeaponUpgradeVoucher1": 100},
        )
        cmd = plan(turn)["10012"]
        self.assertEqual(cmd.get("action"), "move", f"该继续去矿，不是空着不动：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(mine), a.pos.dist(mine), "朝矿走")


class SellOreTest(unittest.TestCase):
    """墙砌满之后的白天：把矿背到小贩跟前卖掉。

    与 `SpareOreTest` 是同一条支路上的先后：砌满 ⇒ 先卖（`_sell_ore`），卖不动才去采
    （`_mine_spare_ore`）。所以每个局面都砌满，而且必须能说清"为什么没去卖" —— 四条门
    各有一条用例（没货 / 没人收 / 不够本 / 回不来）。

    站位与小贩的关系是核心事实（任务书 §4.4：「在小贩周围一格内使用」），而小贩格本身
    挡路 ⇒ `step_toward` 撞上它自然停在"周围一格"，与采矿同一条契约。

    阈值口径：货值 ≥ 往返回合数（≈ 每回合至少换 1 金币）才动身 —— "1 金币 ≈ 1 回合"是
    拍的、没有文档依据，用例把它钉成可测的行为：小贩距离 10 ⇒ 阈值 20 ⇒ 铜要攒 4 块。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    RING = {c: WALL for c in wall_cells(Pos(10, 24), 41)}
    #: 小贩 (20,24)：与基地切比雪夫距离 10 ⇒ 来回 20 回合，阈值 20 金币
    VENDOR = Pos(20, 24)
    #: 卖不动时工人转去采的那座矿 —— 故意放在小贩的反方向：
    #: 否则"朝矿走"在距离上也"朝小贩走"，那条用例会假通过
    ORE = Pos(36, 24)
    #: 样例的价目：铜 5 > 铁 3 > 石 1
    SAMPLE_PRICES = {"stone": 1, "iron": 3, "copper": 5}
    #: 离小贩 10 格 ⇒ 阈值 20 金币 ⇒ 4 块铜
    FAR = Pos(30, 24)

    def _turn(
        self,
        pos: Pos,
        bag: dict[str, int],
        *,
        prices: dict[str, int] | None = None,
        vendor: Pos | None = VENDOR,
        round_no: int = 1,
        roles: tuple[BaseRole, ...] | None = None,
    ) -> Turn:
        entries = _terrain(
            self.WEAPONS,
            {self.BASE: "station"},
            self.RING,
            {self.ORE: "stone"},
            {} if vendor is None else {vendor: "vendor"},
        )
        return Turn(
            round_no=round_no,
            map=Map((41, 32), entries),
            roles=roles if roles is not None else (Worker(1, pos, bag),),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=(
                self.SAMPLE_PRICES if prices is None else prices
            ),
        )

    def _sold(self, turn: Turn) -> dict:
        """本回合那条指令 —— 不是 `sell` 就挂（这几条只关心卖给谁、卖多少）。"""
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "sell", f"这一回合该是卖矿，实际是 {cmd}")
        return cmd

    def test_standing_next_to_the_vendor_sells_the_whole_load(self):
        """贴着小贩 ⇒ 一次性卖光手上那种矿（`num` = 全部件数）。

        `num` 报成 1 等于一块一块地卖：一回合一条指令，卖 4 块要 4 个回合，而任务书
        §4.4 明说"支持批量贩卖"。
        """
        cmd = self._sold(self._turn(Pos(20, 23), {"copper": 4}))  # 小贩正下方，切比雪夫 1
        self.assertEqual(cmd, {"action": "sell", "name": "copper", "num": 4})

    def test_the_pricier_ore_is_sold_first(self):
        """一次只卖一种 ⇒ 卖收购价最高的那种（同价才看件数）。

        手上铁铜都有时卖铜（5 > 3）。谁把"铜 > 铁 > 石头"写死都恰好对得上样例 ——
        所以下面还有一条把价目翻过来的用例。
        """
        cmd = self._sold(self._turn(Pos(20, 23), {"iron": 9, "copper": 2}))
        self.assertEqual(cmd["name"], "copper")
        self.assertEqual(cmd["num"], 2, "卖的是铜这一堆的**全部**件数，不是最多的那一堆")

    def test_a_market_flip_changes_which_ore_is_sold(self):
        """铁矿塌方 ⇒ 铁涨到 9 ⇒ 卖铁不卖铜。写死的排序在事件期间恰好是错的。"""
        cmd = self._sold(
            self._turn(Pos(20, 23), {"iron": 3, "copper": 2}, prices={"stone": 1, "iron": 9, "copper": 5})
        )
        self.assertEqual(cmd["name"], "iron")

    def test_spare_stone_gets_sold_too(self):
        """砌满之后多余的石头也卖，但保底留 1 块。

        这是"石头为什么敢进 `SELLABLE`"的实证：调用点只在"墙砌完了"那一支（`_build_walls`
        的 `if not free:`），而墙没砌完时手里的石头一律有用。留下的 1 块由 `_best_load` 扣
        —— 收工时得封上正面那个口（`wall_cells` 的最后一格），封不上就是整夜的一道门。
        """
        cmd = self._sold(self._turn(Pos(20, 23), {"stone": 6}))
        self.assertEqual(cmd, {"action": "sell", "name": "stone", "num": 5})

    def test_a_lone_stone_is_kept_for_the_seal(self):
        """背包里只有 1 块石头 ⇒ 一件都不卖（那一块得留着封正面那个口）。

        没有别的货 ⇒ `_best_load` 挑不出来 ⇒ 这一回合不去小贩那儿（改去干别的）。
        """
        turn = self._turn(Pos(20, 23), {"stone": 1})
        cmd = plan(turn)["1"]
        self.assertNotEqual(cmd.get("action"), "sell", f"那 1 块得留着封口：{cmd}")
        self.assertNotEqual(cmd.get("name"), "stone", f"更不该指名卖石头：{cmd}")

    def test_an_idle_pioneer_with_goods_goes_selling(self):
        """任务点全空（都在冷却/做完）⇒ 开拓者去卖矿（`sell` 可用角色是"全部"）。

        规则限制了这条线的上限：`collect` 仅工人、没有转移物品的指令 ⇒ 开拓者背包里通常
        没矿 —— 结构留着，等它从任务/宝藏拿到可卖物才真正跑得起来。
        """
        turn = self._turn(
            Pos(30, 24),
            {"copper": 4},
            roles=(Pioneer(10011, Pos(30, 24), {"copper": 4}),),
        )
        cmd = plan(turn)["10011"]
        self.assertEqual(cmd["action"], "move", "没任务可接 ⇒ 去卖矿")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.VENDOR), Pos(30, 24).dist(self.VENDOR), "朝小贩走")

    def test_a_full_load_that_does_not_pay_for_the_trip_is_not_worth_walking(self):
        """货不够本 ⇒ 一步都不走，留在矿边接着采（阈值 = 2 × 距离 = 20 金币）。

        一块铜值 5，走 10 格过去加回来共 20 回合，收益抵不上在那儿多采 3 回合。行为上要
        看得出来"它没往小贩那儿走"：这一回合是朝矿去，不是朝小贩。
        """
        turn = self._turn(self.FAR, {"copper": 1})
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.ORE), self.FAR.dist(self.ORE), "该朝矿走，不是朝小贩")
        self.assertGreater(step.dist(self.VENDOR), self.FAR.dist(self.VENDOR), "不该朝小贩走")

    def test_a_load_worth_the_trip_gets_walked_to_the_vendor(self):
        """攒够了（4 块铜 = 20 金币 ≥ 阈值 20）⇒ 动身朝小贩走一格。

        阈值是 `>=` 不是 `>`：4 块铜正好 20，差的这一点会把"恰好攒够"的工人
        永远留在矿边（每一次采集都在重新计算，永远差一块）。
        """
        turn = self._turn(self.FAR, {"copper": 4})
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.VENDOR), self.FAR.dist(self.VENDOR), "该朝小贩走")

    def test_no_vendor_means_no_selling(self):
        """地图上没有小贩 ⇒ 谁也别卖（把矿卖了换不成钱，走这一趟纯亏）。

        同时钉住"没有小贩时不会崩"：`min(vendors)` 在空集上会 `ValueError`，
        而那跑在 `handle` 的 `try` 里 —— 代价是整回合空指令。
        """
        # 手上四块铜、价格也对，但没有小贩 ⇒ 这一回合只能去采矿
        cmd = plan(self._turn(Pos(20, 23), {"copper": 4}, vendor=None))["1"]
        self.assertNotEqual(cmd["action"], "sell", cmd)

    def test_nobody_buys_it_means_nothing_is_sold(self):
        """价目表为空 / 小贩不收这种矿 ⇒ 一件都不卖（与 `_pick_ore` 同一条口径）。

        空表意味着"这一回合什么都不收"，不是"按默认价收"。降级方向是少做：宁可多采一趟，
        不可白送一件出去（`num` 报出去就没了，而 `sell` 没有撤销）。
        """
        # 空表 ⇒ 一条指令都没有（不是"发条空指令"）：`_pick_ore` 也按 0 算，
        # 于是连"该去采哪座矿"都答不出来 —— 这正是不写死价格的代价与收益。
        self.assertEqual(plan(self._turn(Pos(20, 23), {"copper": 4}, prices={})), {})
        # 只收石头、而手上一块石头也没有 ⇒ 铜按 0 算 ⇒ 不是卖矿（转去采那座石矿）
        cmd = plan(self._turn(Pos(20, 23), {"copper": 4}, prices={"stone": 1}))["1"]
        self.assertNotEqual(cmd["action"], "sell", cmd)

    def test_a_worker_two_cells_from_the_vendor_walks_instead_of_selling(self):
        """差一格还不许卖：`steps_between` 的 **0** 才是"贴着小贩"（1 = 还要走一格）。

        `sell` 的站位是"小贩周围一格内"（§4.4）⇒ 在切比雪夫 2 的地方发出去就是非法指令。
        更糟的是每回合算出来都是 1 ⇒ 每回合原样再发一次：一次判错换算成几十次异常，而红线
        只有 5 次。
        """
        start = Pos(22, 24)  # 与小贩 (20,24) 切比雪夫 2 ⇒ 到"贴着"还差 1 步
        self.assertEqual(start.dist(self.VENDOR), 2, "夹具前提：与小贩差一格")
        cmd = plan(self._turn(start, {"copper": 4}))["1"]
        self.assertEqual(cmd.get("action"), "move", f"该走过去，不是隔着一格卖：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.VENDOR), start.dist(self.VENDOR), "朝小贩走一格")

    def test_too_late_in_the_day_to_walk_there_and_back(self):
        """白天不够"走到小贩 + 从小贩回基地" ⇒ 不卖，改去回炮位（收工闸门）。

        夜里必须在炮位上，黑天还在赶路 = 拿火力换矿石。`roundNo=60` ⇒ 白天剩 11 回合，减
        `TIME_MARGIN` 5 只剩 6，而这一趟（10 + 10）走不完。本夹具的环砌满（`RING`）⇒ 收工
        闸门这一回合开着（`_leave_for_the_post`），它没生效时才是"连矿也不去"（空指令）。
        """
        sell_early = plan(self._turn(self.FAR, {"copper": 9}))
        self.assertEqual(sell_early["1"]["action"], "move", sell_early)
        late = plan(self._turn(self.FAR, {"copper": 9}, round_no=60))["1"]
        self.assertNotEqual(late["action"], "sell", f"时间不够，不该去卖：{late}")
        step = Pos(late["targetPos"][0]["x"], late["targetPos"][0]["y"])
        self.assertLess(step.dist(self.BASE), self.FAR.dist(self.BASE), f"该往回赶：{late}")

    def test_the_wall_comes_first(self):
        """墙没砌完 ⇒ 一块矿都不卖（哪怕人已经站在小贩旁边）。

        这是 `SELLABLE` 敢把石头收进来的全部理由：砌墙那一支里不存在"多余的石头"
        （`_stones_to_mine` 的上限正是"还差几格墙"）。
        """
        turn = Turn(
            round_no=1,
            map=Map((41, 32), _terrain(self.WEAPONS, {self.BASE: "station"}, {self.VENDOR: "vendor"})),
            roles=(Worker(1, Pos(20, 23), {"copper": 4, "stone": 3}),),
            gold=0,
            weapons=self.WEAPONS,
            vendor_prices=self.SAMPLE_PRICES,
        )
        cmd = plan(turn)["1"]
        self.assertNotEqual(cmd["action"], "sell", cmd)
        self.assertEqual(cmd["action"], "move", cmd)


class UpgradeLineTest(unittest.TestCase):
    """买得起就优先升级武器 —— 买券 → 走到目标武器 → 用券。

    优先链按群体打击判：加特林 > 火箭 > 电磁。加特林 +1 颗子弹 = 每回合 +10、无冷却、
    弹道必命中，两颗可分打两台（90° 锥内），一夜 60 回合最多 +600；火箭 +1 枚对簇约
    +30/齐射，但 3 回合冷却一夜只 ~20 轮、依赖扎堆；电磁单目标、能量对满血机器人不穿透。
    链：gatling→2 → rocket→2 → gatling→3 → railgun→2 → …

    无状态：拿没拿券看背包（买完金变少、包里多一张，两阶段天然可分）；跑腿者 = 持券的
    工人，没有持券者 ⇒ 名册上第一个工人（别人照常采/卖）。
    """

    BASE = Pos(10, 24)
    WEAPONS = (
        Weapon(10020, "gatling", Pos(12, 22), 4, 0),
        Weapon(10030, "railgun", Pos(12, 25), 7, 0),
        Weapon(10040, "rocket", Pos(9, 25), 2**31 - 1, 0),
    )
    RING = {c: WALL for c in wall_cells(BASE, 41)}
    SHOP = Pos(25, 20)  # 样例的武器商店位
    PRICES = {"WeaponUpgradeVoucher1": 100, "WeaponUpgradeVoucher2": 150}
    ORE = Pos(36, 24)  # 跑腿之外工人该去采的那座矿

    def _turn(
        self,
        *,
        gold: int = 0,
        bag: dict[str, int] | None = None,
        pos: Pos = Pos(15, 24),  # 盒子外面（穿门绕行会把第一步甩向反方向）
        round_no: int = 1,
        weapons: tuple[Weapon, ...] | None = None,
        roles: tuple[BaseRole, ...] | None = None,
        shop: bool = True,
    ) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map(
                (41, 32),
                _terrain(
                    weapons if weapons is not None else self.WEAPONS,
                    {self.BASE: "station", **({self.SHOP: "weaponShop"} if shop else {})},
                    self.RING,
                    {self.ORE: "copper"},
                ),
            ),
            roles=roles if roles is not None else (Worker(1, pos, bag or {}),),
            gold=gold,
            weapons=weapons if weapons is not None else self.WEAPONS,
            vendor_prices={"stone": 1, "iron": 3, "copper": 5},
            shop_prices=self.PRICES,
        )

    def test_walks_to_the_shop_when_the_upgrade_is_affordable(self):
        """金够、加特林还是 L1 ⇒ 墙砌完后第一件事是跑商店（优先于采矿 ——
        场上明明有矿也不去）。"""
        cmd = plan(self._turn(gold=100))["1"]
        self.assertEqual(cmd["action"], "move", "该朝武器商店走，不是去采矿")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), Pos(15, 24).dist(self.SHOP), "朝商店方向")

    def test_buys_the_voucher_when_adjacent_to_the_shop(self):
        """贴着商店 ⇒ `buy`（一回合一条指令，一次只买一张）。"""
        self.assertEqual(
            plan(self._turn(gold=100, pos=Pos(25, 21)))["1"],
            {"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 1},
        )

    def test_a_worker_two_cells_from_the_shop_walks_instead_of_buying(self):
        """差一格还不许买：`steps_between` 的 **0** 才是"贴着商店"（1 = 还要走一格）。

        与 `sell` 同一条契约：`buy` 要在商店周围一格内。站在切比雪夫 2 的地方发出去是非法
        指令，而且每回合重复 —— 红线只有 5 次。
        """
        start = Pos(27, 20)  # 与商店 (25,20) 切比雪夫 2 ⇒ 到"贴着"还差 1 步
        self.assertEqual(start.dist(self.SHOP), 2, "夹具前提：与商店差一格")
        cmd = plan(self._turn(gold=100, pos=start))["1"]
        self.assertEqual(cmd.get("action"), "move", f"该走过去，不是隔着一格买：{cmd}")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.SHOP), start.dist(self.SHOP), "朝商店走一格")

    def test_the_holder_walks_to_the_gatling(self):
        """持券者直奔目标武器（优先链第一个：加特林）—— 终点就是炮位，
        用完券正好站岗，不用留回程。"""
        cmd = plan(self._turn(bag={"WeaponUpgradeVoucher1": 1}, pos=Pos(20, 20)))["1"]
        self.assertEqual(cmd["action"], "move")
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(Pos(12, 22)), Pos(20, 20).dist(Pos(12, 22)), "朝加特林走")

    def test_uses_the_voucher_when_adjacent_to_the_target(self):
        """贴着目标武器 ⇒ `use`，`targetPos` = 目标武器的位置（任务书 L292）。"""
        self.assertEqual(
            plan(self._turn(bag={"WeaponUpgradeVoucher1": 1}, pos=Pos(12, 23)))["1"],
            {"action": "use", "name": "WeaponUpgradeVoucher1", "targetPos": [{"x": 12, "y": 22}]},
        )

    def test_a_maxed_gatling_passes_the_ticket_to_the_rocket(self):
        """优先链顺延：加特林已 L2 ⇒ 目标换火箭（同是 L1 ⇒ 还是券1）。"""
        weapons = (
            Weapon(10020, "gatling", Pos(12, 22), 5, 0, 2),
            Weapon(10030, "railgun", Pos(12, 25), 7, 0),
            Weapon(10040, "rocket", Pos(9, 25), 2**31 - 1, 0),
        )
        self.assertEqual(
            plan(self._turn(bag={"WeaponUpgradeVoucher1": 1}, pos=Pos(9, 24), weapons=weapons))["1"],
            {"action": "use", "name": "WeaponUpgradeVoucher1", "targetPos": [{"x": 9, "y": 25}]},
        )

    def test_only_one_worker_runs_the_errand(self):
        """两个工人 ⇒ 只有跑腿者去商店，另一个照常采矿（火力/经济两不误）。"""
        roles = (Worker(1, Pos(15, 24)), Worker(2, Pos(15, 25)))
        cmds = plan(self._turn(gold=100, roles=roles))
        step1 = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertLess(step1.dist(self.SHOP), Pos(15, 24).dist(self.SHOP), "1 号（名册第一个）去商店")
        step2 = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        self.assertLess(step2.dist(self.ORE), Pos(15, 25).dist(self.ORE), "2 号去采矿")

    def test_gold_short_of_the_ticket_means_no_errand(self):
        """金不够（99 < 100）⇒ 不跑腿，去采矿 —— 价目逐回合从 `weaponShopList` 读。"""
        cmd = plan(self._turn(gold=99))["1"]
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.ORE), Pos(15, 24).dist(self.ORE), "钱不够 ⇒ 照常采矿")

    def test_no_errand_when_the_trip_does_not_fit_the_day(self):
        """整趟（商店 → 目标武器，含买/用两个动作回合）来不及 ⇒ 不跑腿，回炮位（收工闸门）。

        对照：同一局面白天还长时是动身的。本夹具的环是砌满的 ⇒ 收工闸门接管这一回合
        —— 不跑腿的人往回赶。
        """
        self.assertIn("1", plan(self._turn(gold=100, round_no=1)), "白天还长 ⇒ 动身")
        late = plan(self._turn(gold=100, round_no=66))
        self.assertNotIn("buy", [cmd["action"] for cmd in late.values()], "来不及 ⇒ 不跑腿")
        step = Pos(late["1"]["targetPos"][0]["x"], late["1"]["targetPos"][0]["y"])
        self.assertLess(step.dist(Pos(12, 25)), Pos(15, 24).dist(Pos(12, 25)), "该往炮位赶")

    def test_no_errand_without_a_shop_or_a_target(self):
        """降级方向：地图上没商店 / 武器全升满 ⇒ 不跑腿，照常采矿。"""
        weapons = (
            Weapon(10020, "gatling", Pos(12, 22), 7, 0, 3),
            Weapon(10030, "railgun", Pos(12, 25), 10, 0, 3),
            Weapon(10040, "rocket", Pos(9, 25), 2**31 - 1, 0, 3),
        )
        for name, turn in (
            ("没商店", self._turn(gold=999, shop=False)),
            ("全升满", self._turn(gold=999, weapons=weapons)),
        ):
            with self.subTest(case=name):
                cmd = plan(turn)["1"]
                step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                self.assertLess(step.dist(self.ORE), Pos(15, 24).dist(self.ORE), "照常采矿")


class OreClaimTest(unittest.TestCase):
    """矿格认领：A 这回合认领的矿，B 不会再奔它 —— 就近换一座。

    不认领的话两人都要石头时就是抢资源：B 明明有别的石矿，却跟着 A 奔同一座，路上互堵、
    到了白站。认领只是回合内的账本（`ore_taken`），不跨回合。"""

    BASE = Pos(10, 24)
    ORE1 = Pos(4, 24)
    ORE2 = Pos(8, 20)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def test_the_second_worker_never_chases_a_claimed_ore(self):
        grid = _terrain(
            self.WEAPONS, {self.BASE: "station", self.ORE1: "stone", self.ORE2: "stone"}
        )
        a = Worker(10010, Pos(4, 23), {})  # 贴着 ORE1
        b = Worker(10012, Pos(5, 24), {})  # 也贴着 ORE1 —— 不认领的话它会跟着采同一座
        grid |= {a.pos: "worker", b.pos: "worker"}
        turn = Turn(
            round_no=1, map=Map((41, 32), grid), roles=(a, b), gold=0, weapons=self.WEAPONS
        )
        cmds = plan(turn)

        self.assertEqual(cmds["10010"]["action"], "collect")
        self.assertEqual(
            Pos(cmds["10010"]["targetPos"][0]["x"], cmds["10010"]["targetPos"][0]["y"]),
            self.ORE1,
        )
        b_cmd = cmds["10012"]
        self.assertNotEqual(
            b_cmd["action"], "collect", "B 不该跟着 A 采同一座矿 —— 目标格认领了"
        )
        step = Pos(b_cmd["targetPos"][0]["x"], b_cmd["targetPos"][0]["y"])
        self.assertLess(
            step.dist(self.ORE2), b.pos.dist(self.ORE2), "B 换奔另一座石矿"
        )


class OreValueTest(unittest.TestCase):
    """挖矿的性价比判据：单位回合价值 = 价 × 新闻修正 ÷ (到矿+采+回炮位)。

    不是"价高优先" —— 远的贵矿可能跑不过近的贱矿。石头刚需支线不变（墙只吃石头）。
    """

    BASE = Pos(10, 24)
    IRON = Pos(14, 8)    # 近：BFS 代价小
    COPPER = Pos(30, 4)  # 远：BFS 代价大
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def setUp(self) -> None:
        # hint 是 AGENT 单实例上的跨回合状态 —— 不清就会泄进后面的用例
        # （SpareOreTest 的"市场翻转"会拿到本类留下的铜 hint）。
        AGENT.reset()
        self.addCleanup(AGENT.reset)

    def _turn(self, worker: Worker) -> Turn:
        ring = {c: WALL for c in wall_cells(self.BASE, 41)}  # 环砌满 ⇒ 工人走经济线
        grid = _terrain(
            self.WEAPONS,
            {self.BASE: "station", self.IRON: "iron", self.COPPER: "copper", **ring},
        )
        grid[worker.pos] = "worker"
        return Turn(
            round_no=1, map=Map((41, 32), grid), roles=(worker,), gold=0,
            weapons=self.WEAPONS,
            vendor_prices={"iron": 4, "copper": 5},
        )

    def test_a_nearby_cheap_ore_beats_a_far_pricy_one(self):
        """铁 4 近 vs 铜 5 远：性价比上近铁赢 —— 只按"价高优先"会奔铜。"""
        worker = Worker(10010, Pos(20, 6), {})
        step = Pos(*plan(self._turn(worker))["10010"]["targetPos"][0].values())
        self.assertLess(step.dist(self.IRON), step.dist(self.COPPER), "朝近铁走")

    def test_a_news_hint_can_flip_the_choice(self):
        """新闻说铜要涨（hint ×2）⇒ 性价比翻盘，宁可跑远路也采铜。"""
        AGENT.reset()
        AGENT.adopt_price_hints({"copper": "up"})
        worker = Worker(10010, Pos(20, 6), {})
        step = Pos(*plan(self._turn(worker))["10010"]["targetPos"][0].values())
        self.assertLess(
            step.dist(self.COPPER), worker.pos.dist(self.COPPER),
            "迈步朝远铜（hint ×2 翻盘）",
        )
        self.assertGreater(step.dist(self.IRON), worker.pos.dist(self.IRON), "背离近铁")


class PioneerErrandTest(unittest.TestCase):
    """开拓者的任务空隙差事：无可接任务 ⇒ 领"买券 → 用券"。

    跑腿者优先级 = 持券者（券在谁包里谁用，没有转移指令）> 真空闲的开拓者（无可接任务）
    > 第一个工人。"""

    BASE = Pos(10, 24)
    SHOP = Pos(20, 16)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def test_an_idle_pioneer_buys_the_voucher(self):
        grid = _terrain(self.WEAPONS, {self.BASE: "station", self.SHOP: "weaponShop"})
        worker = Worker(10010, Pos(12, 23), {"stone": 5})
        pioneer = Pioneer(10011, Pos(20, 15), {})  # 贴着商店
        grid |= {worker.pos: "worker", pioneer.pos: "worker"}
        turn = Turn(
            round_no=1,
            map=Map((41, 32), grid),
            roles=(worker, pioneer),
            gold=150,
            weapons=self.WEAPONS,
            shop_prices={"WeaponUpgradeVoucher1": 100},
        )
        cmds = plan(turn)

        self.assertEqual(
            cmds["10011"]["action"],
            "buy",
            "开拓者真空闲（无可接任务）⇒ 它去跑腿买券；旧口径下它什么都不发",
        )
        others = {v["action"] for k, v in cmds.items() if k != "10011"}
        self.assertNotIn("buy", others, "跑腿的只有一个 —— 工人不去重复买券")


if __name__ == "__main__":
    unittest.main()
