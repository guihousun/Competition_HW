"""protocol/model.py 的用例：payload → Turn 的容错解析（字段缺失的缺省方向、判题器回执
`errors` / `lastRoundRoleActionResults`、沙盒回执 `lastCmdResult`、任务点 `playerTasks`）。

跑法：`PYTHONUTF8=1 py tests/<本文件>`（全量：`py -m unittest discover -s tests -v`）。
必须用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用、src/ 给 coregeek 用（discover 跑时前者不一定在 sys.path 里）
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import SAMPLE  # noqa: E402
from coregeek.app import handle  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.path import step_toward  # noqa: E402
from coregeek.game.roles import BaseRole  # noqa: E402
from coregeek.game.world import Error, Turn  # noqa: E402
from coregeek.protocol import model  # noqa: E402


class ParseTest(unittest.TestCase):
    def _turn(self) -> Turn:
        turn = model.load(json.loads(SAMPLE.read_text(encoding="utf-8")))
        self.assertIsNotNone(turn)
        return turn

    def test_turn_holds_characters_only(self):
        """建筑（基地/武器/墙）不是可操控单位，不进 `roles` —— 它们只在地图网格里。"""
        turn = self._turn()
        self.assertEqual({r.type_name for r in turn.roles}, {"worker", "pioneer"})
        self.assertEqual(len(turn.roles), 3)

    def test_map_size_is_read(self):
        """寻路靠它挡界外，读错了不会有任何症状——只会静悄悄地一步不动或走出去。"""
        self.assertEqual(self._turn().map.size, (41, 32))

    def test_weapons_and_robots_come_from_the_payload(self):
        """武器名册与机器人来自 `teamOur.roles` / `robot.roles`（`turn.weapons` / `turn.robots`）。

        `attackRange` 取样例的 4/7/INT_MAX（与任务书 §4.5.1 表格的 3/6/10 矛盾，以 payload
        为准）；三座炮都没有 `cooldown` 字段 ⇒ 全 -1 ⇒ 不当成"冷却中"。地图网格里还留着这三格
        （武器一样挡路），但"哪座炮能开火"只认这份名册。
        """
        turn = self._turn()
        by_id = {w.id: w for w in turn.weapons}
        self.assertEqual(set(by_id), {10020, 10030, 10040})
        self.assertEqual(
            {i: (w.kind, w.pos, w.attack_range, w.cooldown) for i, w in by_id.items()},
            {
                10020: ("gatling", Pos(9, 24), 4, -1),
                10030: ("railgun", Pos(10, 25), 7, -1),
                10040: ("rocket", Pos(9, 25), 2**31 - 1, -1),
            },
        )
        for weapon in turn.weapons:
            self.assertEqual(turn.map.cells[weapon.pos.y][weapon.pos.x], weapon.kind)
        self.assertEqual(
            sorted((r.pos.x, r.pos.y, r.health) for r in turn.robots),
            [(4, 4, 40), (4, 5, 500), (5, 4, 60), (5, 5, 800)],
        )

    def test_robots_carry_their_kind(self):
        """机器人带 `roleType`（`kind`）—— 夜里"打谁"先按它排（低级优先，`night.TIER_VALUE`）。
        样例四台：小/中/大/BOSS 各一。字段缺失给空串 ⇒ 当最低级（照打，不静默少打）。"""
        self.assertEqual(
            {r.pos: (r.kind, r.health) for r in self._turn().robots},
            {
                Pos(4, 4): ("smallRobot", 40),
                Pos(5, 4): ("middleRobot", 60),
                Pos(4, 5): ("largeRobot", 500),
                Pos(5, 5): ("bossRobot", 800),
            },
        )

    def test_weapons_carry_their_level(self):
        """武器带 `level`（接口文档：仅建筑持有、初始 1）—— 升级线靠它认"还升得动"，
        摘要也打它（"升级成没成"只有日志能回答）。样例三座全 L1。"""
        self.assertEqual(
            {w.id: w.level for w in self._turn().weapons},
            {10020: 1, 10030: 1, 10040: 1},
        )

    def test_shop_prices_come_from_the_payload(self):
        """顶层 `weaponShopList` → `Turn.shop_prices`（与 `vendor_prices` 同一套解析；
        样例实证：升级券1=100、券2=150）。不写死价格 —— 与矿价同一条原则。"""
        self.assertEqual(self._turn().shop_prices.get("WeaponUpgradeVoucher1"), 100)
        self.assertEqual(self._turn().shop_prices.get("WeaponUpgradeVoucher2"), 150)

    def test_weapon_shops_are_an_index_of_their_own(self):
        """武器商店是格子（`zones` 的 `weaponShop`，挡路）—— 买券得走到它旁边。
        样例在 (25,20)。"""
        self.assertEqual(self._turn().map.shops, frozenset({Pos(25, 20)}))

    def test_vendor_prices_come_from_the_payload_verbatim(self):
        """价目照抄载荷，不写死 —— 样例是 1/3/5，事件期间会变（任务书 L386）。

        字段坏掉的整条丢掉，尤其不能把"解析不出来"的 -1 当成价格（`_int` 对缺字段/类型不对给
        -1，而负的收购价不存在）：混进来要么选出倒贴钱的矿，要么把整张表判成"没有价"
        （`_pick_ore` 滤掉 <= 0）。
        """
        self.assertEqual(
            dict(self._turn().vendor_prices), {"stone": 1, "iron": 3, "copper": 5}
        )
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["vendorShopList"] = [
            {"name": "stone", "price": 9},  # 价格会变
            {"name": "iron"},  # 没价格 ⇒ 丢
            {"name": "copper", "price": "5"},  # 类型不对 ⇒ 丢
            {"price": 2},  # 没名字 ⇒ 丢
            "不是对象",  # 整条不成形 ⇒ 丢
        ]
        self.assertEqual(dict(model.load(raw).vendor_prices), {"stone": 9})
        raw.pop("vendorShopList")
        self.assertEqual(dict(model.load(raw).vendor_prices), {}, "整份缺失 ⇒ 空表 ⇒ 不去采")

    def test_a_destroyed_weapon_is_not_operated(self):
        """`health == 0` 的炮丢掉 —— 已毁的炮不该再被操控。

        `map.cells` 里它还在（地形由地图层管），但名册里没有它 ⇒ 不会被发 `attack`。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10020:
                node["health"] = 0
        turn = model.load(raw)
        self.assertEqual({w.id for w in turn.weapons}, {10030, 10040})

    def test_a_destroyed_role_gets_no_command(self):
        """阵亡的角色不该再收到指令（`health == 0`）—— 与"操纵已毁的炮"同一类风险。

        死单位可能仍留在 `teamOur.roles` 里。给尸体发指令判题器会怎么算文档没写，没必要赌。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10012:
                node["health"] = 0
        turn = model.load(raw)
        self.assertEqual({r.id for r in turn.roles}, {10010, 10011})

    def test_a_missing_health_is_not_a_death(self):
        """`health` 字段缺失（`_int` 给 -1）与"声明阵亡"（0）要分开 —— 别用 `<= 0`。"""
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            node.pop("health", None)
        turn = model.load(raw)
        self.assertEqual(len(turn.roles), 3)
        self.assertEqual(len(turn.weapons), 3)

    def test_ores_carry_their_kind_but_everything_blocks(self):
        """石/铁/铜各自带矿种进 `ores`；小贩 / 武器商店 / 任务点不是矿，却一样挡路。

        `ores` 装的是"矿点 → 矿种"而不是坐标集：选矿那一步要在三种矿之间按收购价排（铜未必比
        铁贵，见 `Turn.vendor_prices`），只留坐标就答不出"这是哪种矿"。
        """
        grid = self._turn().map
        self.assertEqual(
            grid.ores,
            {
                Pos(4, 24): "stone",
                Pos(14, 3): "stone",
                Pos(25, 10): "iron",
                Pos(8, 28): "iron",
                Pos(22, 26): "copper",
                Pos(7, 2): "copper",
            },
        )
        for pos, what in (
            (Pos(20, 16), "小贩"),
            (Pos(25, 20), "武器商店"),
            (Pos(14, 14), "挑战者任务点1"),
            (Pos(23, 14), "防守方任务点1"),
        ):
            with self.subTest(what=what):
                self.assertIn(pos, grid.blocked, f"{what} 应当挡路")
                self.assertNotIn(pos, grid.ores, f"{what} 不是矿")

    def test_vendors_are_an_index_of_their_own(self):
        """小贩单独一张表（`Map.vendors`）：`sell` 那条线唯一的目标点。

        它和矿在网格里的形状一样（都是 `neutralType`）却不像矿那样带矿种 ⇒ 是
        `frozenset[Pos]` 而不是 `Mapping`。小贩不是 `teamOur.roles` 里的单位，"小贩在哪"只能
        从网格认；它照样挡路（`step_toward` 撞上它自然停在贴着一格 —— 那正是 `sell` 的站位）。
        """
        grid = self._turn().map
        self.assertEqual(grid.vendors, {Pos(20, 16)})
        self.assertIn(Pos(20, 16), grid.blocked)
        self.assertNotIn(Pos(20, 16), grid.ores)

    def test_a_size_less_map_has_no_vendors_either(self):
        """尺寸非法 ⇒ 整个矩阵为空 ⇒ 小贩也没有（`vendors` 得跟着 `ores` 一起空）。

        `frozenset()` 与"地图上没有小贩"在策略侧是同一件事（`_sell_ore` 的门 ②），但属性不
        存在会直接 `AttributeError` —— 它跑在 `handle` 的 `try` 里，代价是整回合空指令。
        """
        for size in ((-1, -1), (41, 0)):
            with self.subTest(size=size):
                empty = Map(size, {Pos(3, 3): "vendor"})
                self.assertEqual(empty.vendors, frozenset())
                self.assertEqual(empty.ores, {})
                self.assertEqual(empty.blocked, frozenset())

    def test_the_backpack_is_counted_by_name(self):
        """背包是物品名数组，重复即计数（接口文档 §1.3.1）—— `_bag` 收整张名字表，
        `BaseRole.stone` 是它的派生属性。

        非矿石（开拓者那个 `medicine`）也照样收进来 —— `_bag` 不认识矿，认矿是 `planner`
        的事（`SELLABLE`）。
        """
        by_id = {r.id: r for r in self._turn().roles}
        self.assertEqual(by_id[10010].bag, {"stone": 1, "iron": 1, "copper": 1})
        self.assertEqual(by_id[10010].stone, 1)
        self.assertEqual(by_id[10011].bag, {"medicine": 1})
        self.assertEqual(by_id[10011].stone, 0, "开拓者没有石头 —— 派生属性得跟着空")

    def test_a_missing_backpack_is_an_empty_bag(self):
        """背包缺失 / 不是数组 ⇒ 空表 ⇒ 石头 0 块、一件都卖不掉。

        降级方向是"少做"，与 `_gold` / `_size` 一致：宁可少采，不可对着空背包发 `build` 或
        `sell`（`num` 报大件数会不会被判"指令非法"文档没写）。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10010:
                node.pop("backpack", None)
            if node["id"] == 10011:
                node["backpack"] = "stone,stone"  # 不是数组（逗号串，看着很像"内容一样"）
        by_id = {r.id: r for r in model.load(raw).roles}
        self.assertEqual(by_id[10010].bag, {})
        self.assertEqual(by_id[10010].stone, 0)
        self.assertEqual(by_id[10011].bag, {})


class TaskParseTest(unittest.TestCase):
    """`teamOur.playerTasks` → `Turn.task_points`，以及顶层的 `phaseTask` / `llmResp`。

    `playerTasks` 是任务点的权威来源：它只含我方那 2 个点（阵营已按 `teamOur.type` 滤好），
    不必去 `mapInfo.zones` 里认 `challengerTaskPoint*`，也不必读 `teamOur.type`。
    """

    POINT = {
        "taskType": "自进化类1",
        "taskPosition": {"x": 14, "y": 14},
        "coldDownRounds": 0,
        "isValid": True,
    }

    def _load(self, *points: dict, **top) -> Turn:
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["teamOur"]["playerTasks"] = list(points)
        raw.update(top)
        turn = model.load(raw)
        self.assertIsNotNone(turn)
        return turn

    def test_task_points_come_from_player_tasks(self):
        """逐字读原始样例：两个点，坐标取的是 `taskPosition`（不是 `pos`）。"""
        turn = model.load(json.loads(SAMPLE.read_text(encoding="utf-8")))
        self.assertEqual(turn.task_points, (Pos(14, 14), Pos(17, 17)))
        self.assertEqual(turn.phase_task, "", "样例里没接任务")

    def test_a_cooling_task_point_is_not_offered(self):
        """冷却中的点接不了（接口文档 L136：`coldDownRounds` = 还有多少回合就绪）。"""
        self.assertEqual(self._load({**self.POINT, "coldDownRounds": 30}).task_points, ())

    def test_an_invalid_task_point_is_not_offered(self):
        """`isValid` 为 false = 冷却中或这个点的任务已做完（接口文档 L139）。"""
        self.assertEqual(self._load({**self.POINT, "isValid": False}).task_points, ())

    def test_tasks_are_exhausted_when_no_point_can_ever_return(self):
        """两个点都 `coldDownRounds == 0` 且 `isValid is False` ⇒ 任务没了（用户口径）。

        这是"无任务模式"的开关（`planner` 据此换一套白天/夜里的分工）。
        """
        dead = {**self.POINT, "coldDownRounds": 0, "isValid": False}
        self.assertTrue(self._load(dead, dict(dead)).tasks_exhausted)
        self.assertFalse(self._load(self.POINT, dead).tasks_exhausted, "还有一个点活着")
        self.assertFalse(
            self._load({**dead, "coldDownRounds": 30}).tasks_exhausted, "还带冷却 ⇒ 会回来"
        )
        self.assertFalse(self._load().tasks_exhausted, "`playerTasks` 缺失/为空 = 未知，不算没有")

    def test_walls_carry_their_health_and_level(self):
        """墙是实体（`teamOur.roles` 里 roleType=="wall"，id 40000 系、带 health/level）——
        修墙线的判据来源。已毁（health==0）⇒ 丢：那是一格缺口，归 `_ring` 的候选表管（重建），
        不该出现在修复名单里。"""
        turn = model.load(
            {
                "teamOur": {
                    "roles": [
                        {"id": 40000, "pos": {"x": 5, "y": 20}, "roleType": "wall", "health": 400, "level": 1},
                        {"id": 40001, "pos": {"x": 5, "y": 21}, "roleType": "wall", "health": 0, "level": 1},
                        {"id": 10013, "pos": {"x": 10, "y": 24}, "roleType": "station", "health": 300, "level": 2},
                    ]
                }
            }
        )
        self.assertEqual(turn.walls, ((40000, Pos(5, 20), 400, 1),))
        self.assertEqual(turn.station_health, 300)
        self.assertEqual(turn.station_level, 2)

    def test_the_official_news_is_carried(self):
        """`worldNews.officialNews`（矿产事件的原文）—— 新闻查价线的原料；`folkLegends` 是
        宝藏线索、不读。字段缺失 ⇒ 空串。"""
        turn = model.load({"worldNews": {"officialNews": "北部铁矿区塌方", "folkLegends": "石门三钥"}})
        self.assertEqual(turn.news, "北部铁矿区塌方")
        self.assertEqual(model.load({}).news, "")

    def test_a_missing_cold_down_rounds_still_offers_the_point(self):
        """缺字段的降级方向故意不是"少做"（与 `_gold` / `_size` / `_stone` 相反）。

        误接一个冷却中的点只是指令执行失败（任务书 L508，不计异常）；
        误判成"永远接不了"却会让整条任务线静默作废。两害相权取前者。
        """
        point = {k: v for k, v in self.POINT.items() if k != "coldDownRounds"}
        self.assertEqual(self._load(point).task_points, (Pos(14, 14),))

    def test_a_missing_is_valid_still_offers_the_point(self):
        """同上：只有明确的 `false` 才算接不了。"""
        point = {k: v for k, v in self.POINT.items() if k != "isValid"}
        self.assertEqual(self._load(point).task_points, (Pos(14, 14),))

    def test_a_task_point_without_a_position_is_dropped(self):
        """没有坐标 ⇒ 丢那一条，不能拿一个凭空造的 `Pos` 去发指令。"""
        self.assertEqual(self._load({**self.POINT, "taskPosition": None}).task_points, ())

    def test_phase_task_and_llm_resp_come_from_the_payload(self):
        turn = self._load(phaseTask="请查询北京天气", llmResp="晴 26 度")
        self.assertEqual(turn.phase_task, "请查询北京天气")
        self.assertEqual(turn.llm_resp, "晴 26 度")

    def test_a_non_string_phase_task_degrades_to_empty(self):
        """空串是这三个字段天然的安全值：`phase_task` 空 ⇒ 开拓者回落到"去任务点"、
        且任务线一次都不碰沙盒；`llm_resp` 空 ⇒ 不提交答案（空答案可能被判成"字段缺失"）；
        `cmd_result` 空 ⇒ 没有回执可回灌。"""
        turn = self._load(phaseTask={"text": "x"}, llmResp=42, lastCmdResult=["y"])
        self.assertEqual((turn.phase_task, turn.llm_resp, turn.cmd_result), ("", "", ""))

    def test_the_sandbox_result_comes_from_the_payload(self):
        """顶层 `lastCmdResult` 逐字读进来 —— 它是任务线唯一能看见沙盒的窗口。

        那行状态不解析（`[exitCode:N]` / `[TIMEOUT]` / `[JUDGER_ERROR]`）：非空即原文
        回灌，让 LLM 自己读 —— 解析它就是又多一份会跟判题器漂移的真相。
        字段缺失 ⇒ 空串（文档 L33："未发命令时为空字符串"）。
        """
        turn = self._load(lastCmdResult="[exitCode:0]\n晴 26 度")
        self.assertEqual(turn.cmd_result, "[exitCode:0]\n晴 26 度")
        self.assertEqual(self._load(lastCmdResult=None).cmd_result, "")


class JudgeReceiptTest(unittest.TestCase):
    """顶层 `errors` / `lastRoundRoleActionResults` → `Turn.errors` / `Turn.action_results`。

    这两个字段是"任务为什么一直失败"唯一的答案来源。
    """

    def _load(self, **top) -> Turn:
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw.update(top)
        turn = model.load(raw)
        self.assertIsNotNone(turn)
        return turn

    def test_errors_come_from_the_payload(self):
        """逐字读原始样例：`[{"errorCode": 2, "description": "xxx"}]`。

        样例那条是假错误（`request.txt` 是手工示意数据）—— 这里钉的是解析路径
        通不通，不是"真的一直在报答案错误"。
        """
        self.assertEqual(self._load().errors, (Error(code=2, description="xxx"),))

    def test_a_missing_description_still_keeps_the_code(self):
        """码是读日志时的第一眼信息 —— 缺 `description` 不该把整条丢掉。"""
        turn = self._load(errors=[{"errorCode": 5}])
        self.assertEqual(turn.errors, (Error(code=5, description=""),))

    def test_an_error_without_a_code_is_dropped(self):
        """降级方向不是"少做"：一条 `-1：xxx` 会被当成"未知错误 0"去查一个不存在的
        问题，比不打印更糟。同理 `errors` 本身缺失 ⇒ 空元组（"本轮没报错"是天然的安全值）。"""
        self.assertEqual(self._load(errors=[{"description": "x"}, "junk"]).errors, ())
        self.assertEqual(self._load(errors="boom").errors, ())
        self.assertEqual(self._load(errors=None).errors, ())

    def test_action_results_keep_both_verdicts(self):
        """两种判决都要留（`false` 才是信号，`true` 是它的参照）—— 只留 `false` 的话，
        "判题器压根没提这个单位" 与 "这个单位通过了" 就分不出来了。"""
        turn = self._load(lastRoundRoleActionResults={"10011": True, "10010": False})
        self.assertEqual(dict(turn.action_results), {10011: True, 10010: False})

    def test_action_results_only_trust_real_booleans(self):
        """JSON 里的 `"false"` 是个非空字符串 ⇒ `bool("false") == True`。

        那一步会把"不合法"读成"合法"，而这份回执的全部价值就在于"谁没通过" ——
        所以宁可丢掉也不猜（接口文档 §1.1 声明 value 就是 boolean）。
        """
        turn = self._load(lastRoundRoleActionResults={"10011": "false", "10012": True, "10010": 0})
        self.assertEqual(turn.action_results, ((10012, True),))

    def test_an_unparsable_key_is_dropped(self):
        """key 是 JSON 字符串：转不成 int 的那条丢掉，别在日志里报出一个不存在的 `-1` 号单位。"""
        turn = self._load(lastRoundRoleActionResults={"x": False, "10010": False})
        self.assertEqual(turn.action_results, ((10010, False),))

    def test_a_missing_action_results_map_is_empty(self):
        self.assertEqual(self._load(lastRoundRoleActionResults=None).action_results, ())
        self.assertEqual(self._load(lastRoundRoleActionResults=[1, 2]).action_results, ())


if __name__ == "__main__":
    unittest.main()
