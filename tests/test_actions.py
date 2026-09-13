"""权限闸门与报文的用例。

**为什么值得单独写**（`CLAUDE.md` 编码规则第 3 条）：权限类 bug 的特征是**本地全绿** ——
格式完全合法、自检全过，只有判题器会说"不"，而那条路直通红线（累计 5 次异常即整场不再被调度）。
所以"格式对"证明不了"这角色有权这么做"，必须单独钉一遍。

用标准库 `unittest`，不引依赖。跑法（**用 `py`，本地 `python` 是 3.7.1**）：

    py -m unittest discover -s tests -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.app import LOG_TEXT_MAX, handle  # noqa: E402
from coregeek.game.grid import (  # noqa: E402
    Pos,
    base_cells,
    step_toward,
    wall_cells,
    weapon_cells,
    weapon_sites,
)
from coregeek.game.map import (  # noqa: E402
    LEGEND,
    _NAMES,
    _RENDER_NEUTRAL,
    _RENDER_SIDED,
    Map,
    _char,
)
from coregeek.game.planner import (  # noqa: E402
    TIME_MARGIN,
    WALL,
    WEAPONS_BY_SITE,
    plan,
    task_channel,
)
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Error, Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import actions, model  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"

#: 官方样例（roundNo=85）的落点。**样例是夜里**，所以三个角色都朝最近的一座炮走一格：
#: 10010 在 (5,23) → (9,24)（切比雪夫 4）；10012 在 (10,16) → (9,24) 已被认领 ⇒ (10,25)（9）；
#: 10011 在 (10,12) → 前两座都被认领 ⇒ (9,25)（13）。夜里 `build`/`collect` 一条都不该有。
#: ⚠️ 这条断言**按设计改过两次**：第 7 步夜里是"朝石矿走"，第 8 步改成"回基地操炮"
#: （当时开拓者还不发指令），**第 10 步起开拓者也上炮位** ⇒ 两条变三条。
EXPECTED_MOVES = {"10010": [6, 22], "10012": [9, 17], "10011": [9, 13]}


def _records(kinds: dict[Pos, str], attack_range: int = 4) -> tuple[Weapon, ...]:
    """`{落点: 类别}` → 武器名册。**夹具的唯一真相是记录，地形由它推。**

    方向与 `protocol.model` 一致（单位 → 网格），不是反过来从网格里的类别串凭空造记录 ——
    第 8 步踩过"夹具与真实路径不同形、于是连旧代码都放过去"的坑。
    """
    return tuple(
        Weapon(id=100 + i, kind=kind, pos=pos, attack_range=attack_range, cooldown=0)
        for i, (pos, kind) in enumerate(kinds.items())
    )


def _terrain(weapons: tuple[Weapon, ...], *layers: dict[Pos, str]) -> dict[Pos, str]:
    """名册 → 网格里那几格（武器一样挡路）。与 `_records` 配套，方向只有这一个。"""
    grid: dict[Pos, str] = {w.pos: w.kind for w in weapons}
    for layer in layers:
        grid.update(layer)
    return grid


class MoveWireTest(unittest.TestCase):
    def test_wire_shape_is_the_flat_record(self):
        self.assertEqual(
            actions.Move("worker", Pos(1, 2)).to_wire(),
            {"action": "move", "targetPos": [{"x": 1, "y": 2}]},
        )

    def test_move_is_allowed_for_both_roles(self):
        """§4.4 里 `move` 的可用角色是"全部" —— 别把两种角色误拦了。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(actions.Move(role_type, Pos(0, 0)).to_wire()["action"], "move")

    def test_build_wire_shape(self):
        """`name` 是**建筑名**（与 `roleType` 同名），不是动作名；`targetPos` 永远是数组。

        字段名照 `docs/response.txt` 里唯一那条 `build` —— 形状写错就是一次"指令非法"。
        """
        self.assertEqual(
            actions.Build("worker", "gatling", Pos(9, 23)).to_wire(),
            {"action": "build", "name": "gatling", "targetPos": [{"x": 9, "y": 23}]},
        )

    def test_collect_wire_shape(self):
        """`collect` 的 `targetPos` 是**矿的坐标**，形状与 `move` 同（只有 action + targetPos）。

        ⚠️ 这一条**没有实证报文**：`docs/response.txt` 里只有 `move`/`build`/`remove`。
        形状是从接口文档 §2.2/§2.3 推的，写错就是一次"指令非法"（直通红线）。
        """
        self.assertEqual(
            actions.Collect("worker", Pos(4, 24)).to_wire(),
            {"action": "collect", "targetPos": [{"x": 4, "y": 24}]},
        )

    def test_attack_wire_shape(self):
        """逐字对照 `docs/response.txt` 里那条实证报文（**唯一一条 `attack`**）。

        与其它动作**反着来**：这个对象是"武器 10020 由角色 10010 操控"，
        而 `to_wire()` 编出来的是**挂在武器 id 下的那条记录**（key 由 `planner._emit` 给）。
        字段名 `controllerId` 写错（`controllerID` / `roleId`）就是一次"指令非法"。
        """
        wire = actions.Attack("worker", "10010", Pos(29, 7)).to_wire()
        self.assertEqual(
            wire,
            {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 29, "y": 7}]},
        )
        self.assertIsInstance(wire["controllerId"], str, "接口文档标的是 String")

    def test_attack_target_pos_is_always_one_point(self):
        """`targetPos` 数量 = 武器**等级数**。我们的武器永远是 L1（没有升级券这条线）。"""
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(len(actions.Attack(role_type, "7", Pos(1, 1)).to_wire()["targetPos"]), 1)


class GateTest(unittest.TestCase):
    """`BaseAction` 的校验机制本身 —— 这一步的主要交付物。"""

    def test_roles_classvar_rejects_unauthorized_role(self):
        class WorkerOnly(actions.BaseAction):
            code, roles = "build", actions.WORKER

            def to_wire(self):
                return {"action": self.code}

        self.assertEqual(WorkerOnly("worker").to_wire(), {"action": "build"})
        with self.assertRaises(PermissionError):
            WorkerOnly("pioneer")

    def test_buildings_cannot_act(self):
        """基地/武器/围墙不是角色，产生不了指令（payload 里它们和角色同列，别混）。"""
        for role_type in ("station", "gatling", "railgun", "rocket", "wall"):
            with self.subTest(role_type=role_type):
                with self.assertRaises(PermissionError):
                    actions.Move(role_type, Pos(0, 0))

    def test_pioneer_cannot_build(self):
        """**闸门第一次真的挡住东西**：`build` 仅在工人那一行（任务书 §4.4 最右列）。

        开拓者误发 `build` 是典型的"本地全绿"bug —— 报文格式挑不出毛病，只有判题器
        会说"不"，而那条路直通红线（5 次异常即整场不再被调度）。
        """
        with self.assertRaises(PermissionError):
            actions.Build("pioneer", "gatling", Pos(9, 23))

    def test_pioneer_cannot_collect(self):
        """`collect` 同样只在工人那一行（任务书 §4.4 最右列）。

        ⚠️ `collect` 的昼夜限制**表里没写**（`build`/`remove`/`attack` 都写了），
        所以这里只拦角色、不拦时段 —— 闸门只管"谁"。**每加一个受限动作都要补一条**，
        否则开拓者每天吃一个异常，而红线只有 5 次。
        """
        with self.assertRaises(PermissionError):
            actions.Collect("pioneer", Pos(4, 24))

    def test_typo_role_type_is_rejected(self):
        """角色类型拼错 → 造不出动作，而不是发一条判题器认不出的指令。"""
        with self.assertRaises(PermissionError):
            actions.Move("workre", Pos(0, 0))

    def test_attack_is_allowed_for_both_roles(self):
        """§4.4 里 `attack` 的可用角色是**全部** —— 开拓者也上炮位，别误窄成工人。

        反过来的那种 bug（把开拓者挡在门外）本地全绿，只是整夜少一门火力。
        """
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(actions.Attack(role_type, "10010", Pos(1, 1)).to_wire()["action"], "attack")

    def test_worker_cannot_accept_a_task(self):
        """`acceptTask` 只在开拓者那一行（任务书 §4.4 最右列）。

        ⚠️ 这条最容易"看起来没事"：它的报文**只有一个 `action` 字段**，格式挑不出毛病，
        所以本地全绿、只有判题器会说"不" —— 而那条路直通红线。**每加一个受限动作
        都要补一条**，否则工人每天误吃一个异常，而红线只有 5 次。
        """
        with self.assertRaises(PermissionError):
            actions.AcceptTask("worker")

    def test_worker_cannot_submit_an_answer(self):
        """`submitAnswer` 同样只在开拓者那一行（任务书 §4.4 最右列）。"""
        with self.assertRaises(PermissionError):
            actions.SubmitAnswer("worker", "晴 26 度")


class HandleTest(unittest.TestCase):
    """端到端：`app.handle` 是红线所在，改坏了要立刻知道。"""

    def _handle(self, raw: bytes) -> dict:
        return json.loads(handle(raw).decode("utf-8"))

    def test_sample_payload_moves_all_three_roles_to_a_weapon(self):
        body = self._handle(SAMPLE.read_bytes())
        self.assertEqual(set(body), {"roleCommandMap", "prompt", "executeCmd"})
        cmds = body["roleCommandMap"]
        self.assertEqual(
            {k: [v["targetPos"][0]["x"], v["targetPos"][0]["y"]] for k, v in cmds.items()},
            EXPECTED_MOVES,
        )
        # 样例是**夜里**：三个角色都只走一格，没有 build / collect（`build` 仅白天）
        self.assertEqual({v["action"] for v in cmds.values()}, {"move"})
        # 三个角色离三座炮都还有十几格 ⇒ 本回合**一发都不该有**（没人贴着炮，
        # `_fire` 根本不会被调到），于是 key 全落在角色 id 上、没有一个是武器 id
        self.assertEqual(set(cmds), {"10010", "10011", "10012"})

    def test_bad_json_falls_back_to_empty_commands(self):
        """红线兜底：任何失败都退化成**合法空指令**（空指令合法且不计异常）。"""
        body = self._handle(b"{oops")
        self.assertEqual(body, {"roleCommandMap": {}, "prompt": "", "executeCmd": ""})

    def test_every_round_logs_the_map_then_the_actions(self):
        """每回合的复盘日志：**先局面、再动作、再判题器的回执**（顺序是重点）。

        判题器是黑盒、只给我们这一个视角，出事故时得能看见当时的局面 ——
        只看见一条 `move` 是没法回答"为什么走了这一格"的。
        `assertLogs` 拦到的正是 `main3.py` 重定向到 stdout 的那几条。

        **局面那三块拼成一条记录**（摘要 / 图例 / 地图）：`logging` 的时间戳前缀
        只加在第一条物理行上 —— 拆成三条的话那几十行地图就没有时间戳了，
        而按时间翻日志时正是这些行要定位。

        ⚠️ **样例自带一条假错误与两条假未通过**（`request.txt` 的 `errors` 是
        `[{"errorCode": 2, "description": "xxx"}]`、`lastRoundRoleActionResults` 里
        10010/10030 是 false）。`CLAUDE.md` 已声明**别把样例的这两个值当真实信号读**，
        但**这里的记录条数是真实断言**：回执那两条各自"有事才吭声"，
        所以样例这种局面是 4 条，而一个干净回合只有 2 条（下面那条用例）。
        """
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(SAMPLE.read_bytes())
        head, acts, errors, failed = (r.getMessage() for r in caught.records)
        lines = head.splitlines()
        #: 摘要 3 行 + 图例 3 行 + 地图 34 行（41×32 的图，行自上而下 = y 由大到小）
        self.assertEqual(len(lines), 40)
        #: 回合号在最前 —— 时间戳就加在这一行上
        self.assertEqual(lines[0].split("｜")[0], "回合 85（夜里）")
        self.assertIn("金币 20", lines[0])
        #: 图例在摘要与地图之间（紧挨着图，看着图例看图）
        self.assertTrue(lines[3].startswith("图例："), lines[3])
        #: 标尺两行 + 行号槽 —— 图从第 7 行开始
        self.assertTrue(lines[8].startswith("31 │ "), lines[8])
        self.assertEqual(acts, "动作：10010 move(6,22)；10012 move(9,17)；10011 move(9,13)")
        self.assertEqual(errors, "判题器报错：2：xxx")
        #: **按 id 排序**（不照 payload 的顺序）：`{10010: false, 10030: false}` 在样例里
        #: 恰好就是升序，靠样例**测不出**这一条 —— 所以下面那条解析用例专门打乱一次顺序。
        self.assertEqual(failed, "上回合未通过：10010 10030")

    def test_a_clean_round_logs_only_the_map_and_the_actions(self):
        """回执那两条**有事才吭声** —— 干净回合一条都不该多打（日志字节是有预算的）。

        与上面那条用例合起来才钉得住"触发条件"：只测样例的话，全打也算过。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        self.assertEqual(len(caught.records), 2, [r.getMessage()[:40] for r in caught.records])

    def test_the_failed_ids_are_sorted_not_payload_ordered(self):
        """未通过那几个 id **按升序打**，不照 payload 里的顺序。

        样例那份恰好就是升序，所以上面那条用例**测不出**这一点 —— 而一旦顺序随
        payload 走，同一种局面会打出两种日志，翻日志时对不上号（`_defend` 里
        "并列按 id 排"是同一条理由：先后不能取决于报文给的顺序）。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {"10030": False, "10011": True, "10010": False}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        self.assertEqual(caught.records[-1].getMessage(), "上回合未通过：10010 10030")

    def test_the_task_line_shows_the_whole_text_and_marks_any_truncation(self):
        """任务日志**必须能看见全文** —— 这正是第 14 步的来由。

        原来那版是 `phase_task[:120]` 的**静默**截断：任务一长，日志里就是一段没头没尾的
        文字，看不出后面还有没有内容，于是"任务一直失败"根本无从查起。
        现在：短文本原样打全；超长时截到 `LOG_TEXT_MAX` 并**明说被截了、原文共多少字**。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "短题目"
        raw["llmResp"] = ""  # 判题器还没回话 ⇒ 这一回合会提问

        def line_after_sending(**overrides):
            raw.update(overrides)
            with self.assertLogs("coregeek.app", level="INFO") as caught:
                self._handle(json.dumps(raw).encode("utf-8"))
            return caught.records[-1].getMessage()

        line = line_after_sending()
        self.assertIn("任务：短题目", line)
        self.assertIn("提交：无", line)
        #: `prompt` **不打原文**（它就是任务原文再抄一遍）：只报"这一回合问没问"
        self.assertIn("提问：有", line)

        #: 判题器答了 ⇒ 提交那一格才有内容，而且**不再提问**（省 LLM 额度）
        line = line_after_sending(llmResp="答案")
        self.assertIn("提交：答案", line)
        self.assertIn("提问：无", line)

        long_text = "题" * (LOG_TEXT_MAX + 7)
        line = line_after_sending(phaseTask=long_text)
        self.assertIn("题" * LOG_TEXT_MAX, line)
        self.assertNotIn("题" * (LOG_TEXT_MAX + 1), line)
        self.assertIn(f"共 {LOG_TEXT_MAX + 7} 字", line)

        #: **任务刚结束的那一回合**是唯一一次能看见"判题器最后答了什么"的机会
        #: （`phase_task` 已经空了）—— 所以触发条件里带着 `llm_resp`，不能只判任务。
        line = line_after_sending(phaseTask="")
        self.assertIn("任务：无", line)
        self.assertIn("提交：答案", line)

    def test_attack_through_the_real_payload_path(self):
        """端到端**唯一**一条：从真实 payload 到线上报文。

        用样例改成"夜里、一个工人贴着炮、射程内一只机器人"，验证两件只有整条链路才看得见的事：
        `roleCommandMap` 的 **key 是武器 id**（不是角色 id），`controllerId` 才是角色 id。
        写反的话——`{"10010": {...}}`——判题器看到的是"角色 10010 在操炮"，
        而角色 id 根本不是武器，属于"指令非法"（红线）。单测 `to_wire()` 看不出这一点。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 85  # 夜
        # 10010 挪到加特林 (9,24) 旁边；射程内放一只机器人（加特林射程 4 ⇒ 放 (9,21)）
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10010:
                node["pos"] = {"x": 9, "y": 23}
        raw["robot"]["roles"] = [{"id": 9001, "pos": {"x": 9, "y": 21}, "health": 30}]

        cmds = self._handle(json.dumps(raw).encode("utf-8"))["roleCommandMap"]
        self.assertIn("10020", cmds, "加特林 10020 该由 10010 开火 —— key 是武器 id")
        self.assertEqual(
            cmds["10020"],
            {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 9, "y": 21}]},
        )
        self.assertNotIn("10010", cmds, "角色 id 不能当 attack 的 key")

    def test_describe_survives_a_command_without_a_target(self):
        """`acceptTask` / `submitAnswer` **没有 `targetPos`**，而 `describe` 原来硬读它。

        这不是显示问题：`describe` 跑在 `app.handle` 的 `try` 里（`app._log`），
        一个 `IndexError` 会让**整回合退化成空指令** —— 开拓者领了任务却什么都没答，
        现象与"任务线根本没做"一模一样，极难排查。
        """
        self.assertEqual(actions.describe({"10011": {"action": "acceptTask"}}), "10011 acceptTask")
        self.assertEqual(
            actions.describe({"10011": {"action": "submitAnswer", "taskAnswer": "x"}}),
            "10011 submitAnswer",
        )

    def test_describe_keeps_the_attack_arrow(self):
        """改 `describe` 时不能把既有格式改坏 —— `attack` 的 key 是武器 id，
        不带上操控者就看不出来是谁在开炮。"""
        self.assertEqual(
            actions.describe(
                {"10020": {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 4, "y": 4}]}}
            ),
            "10020 attack←10010(4,4)",
        )

    def test_the_task_loop_through_handle(self):
        """端到端走完整条**工具调用回路** —— 问 → 跑命令 → 回灌结果 → 交答案。

        这条是把整台状态机钉死的**唯一**一条：它同时钉住 `model` 读对了三个顶层字段
        （`phaseTask` / `llmResp` / `lastCmdResult`）、`app` 把 `prompt` 与 `executeCmd`
        **分头**装进了响应、以及开拓者服任务期间**一步不动**（动了任务就作废）。
        单看某一条判据的用例都测不出"三个字段的接线到底通没通"。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1  # 白天：开拓者本来在正常地去任务点，现在应当被钉住
        raw["phaseTask"] = "请查询北京天气"
        raw["lastCmdResult"] = ""
        raw["errors"] = []  # 样例自带一条**假**错误（`CLAUDE.md` 已声明别当真实信号读）
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10011:
                node["pos"] = {"x": 13, "y": 13}  # 贴着任务点1 (14,14)

        def ask() -> dict:
            body = self._handle(json.dumps(raw).encode("utf-8"))
            #: 开拓者被钉死在任务点上：它可以 `submitAnswer`，但**一步都不能挪**
            #: （离开任务点周围一格任务立刻作废，任务书 L379）
            pioneer = body["roleCommandMap"].get("10011")
            self.assertNotEqual(pioneer and pioneer["action"], "move", pioneer)
            return body

        # ① 判题器还没回话 ⇒ 提问，而且这一轮不发命令
        raw["llmResp"] = ""
        body = ask()
        self.assertIn("请查询北京天气", body["prompt"])
        self.assertEqual(body["executeCmd"], "")

        # ② LLM 要一条命令 ⇒ 命令进 `executeCmd`，而**不是**当答案交上去
        raw["llmResp"] = "<tool>python -c \"print(1+1)\"</tool>"
        body = ask()
        self.assertEqual(body["executeCmd"], 'python -c "print(1+1)"')
        self.assertEqual(body["prompt"], "")

        # ③ 沙盒交作业 ⇒ 结果**全文**回灌，这一轮绝不重复发命令
        raw["lastCmdResult"] = "[exitCode:0]\n2"
        body = ask()
        self.assertIn("[exitCode:0]\n2", body["prompt"])
        self.assertEqual(body["executeCmd"], "")

        # ④ LLM 给出答案 ⇒ 原样提交，而且不再提问
        #    （清掉 `lastCmdResult`：文档说"未发命令时为空字符串"，上一轮我们没发命令）
        raw["lastCmdResult"] = ""
        raw["llmResp"] = "晴 26 度"
        body = ask()
        self.assertEqual(
            body["roleCommandMap"]["10011"],
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
        )
        self.assertEqual((body["prompt"], body["executeCmd"]), ("", ""))

        # ⑤ 判题器说答错了 ⇒ 带着"上次交的是什么"再问一遍，**同时照旧提交**
        #    （两条通道独立：提问在推进，而按接口文档 L140 取"通过率最高"、重交零成本）
        raw["errors"] = [{"errorCode": 2, "description": "答案不正确"}]
        body = ask()
        self.assertIn("晴 26 度", body["prompt"])
        self.assertIn("被判定为不正确", body["prompt"])
        self.assertEqual(body["executeCmd"], "")
        self.assertEqual(
            body["roleCommandMap"]["10011"]["action"], "submitAnswer", "提交不该被提问挤掉"
        )

    def test_the_sandbox_line_only_appears_with_a_result(self):
        """沙盒行**只在真有回执时出现** —— 「没发命令就一定是空串」是文档写死的
        （接口文档 L33），所以"有沙盒行" ⟺ "上一轮真跑过一条命令"，这个对账关系要守住。

        反例（提问回合、发命令那一回合）**一条都不该多打**：日志字节是有预算的，
        而这条线每回合都可能触发。⚠️ 发命令那一回合之所以不打，是因为命令原文已经在
        上面任务行的"提交："里了 —— 同一回合、同一条字符串，抄第二遍是纯浪费。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "请查询北京天气"
        raw["llmResp"] = "<tool>ls</tool>"
        raw["lastCmdResult"] = ""

        def messages() -> list[str]:
            with self.assertLogs("coregeek.app", level="INFO") as caught:
                self._handle(json.dumps(raw).encode("utf-8"))
            return [r.getMessage() for r in caught.records]

        #: 发命令那一回合：任务行说明了一切，没有沙盒行
        self.assertFalse([m for m in messages() if m.startswith("沙盒：")])

        #: 有回执那一回合：多出**一条**沙盒行，且回执原文在里面
        raw["llmResp"] = ""
        raw["lastCmdResult"] = "[exitCode:0]\nhello"
        sandbox = [m for m in messages() if m.startswith("沙盒：")]
        self.assertEqual(len(sandbox), 1, sandbox)
        self.assertIn("hello", sandbox[0])

    def test_a_long_sandbox_result_marks_the_truncation(self):
        """沙盒输出可能到 64KB（接口文档 L33），日志这边必须截断**并留痕**。

        回灌给 LLM 的是全文，这里才是截断 —— 两个下游要的东西不同：一个要正确性、
        一个要人眼看得下。`…（共 N 字）` 那句把"命令没输出"与"命令吐了 64KB、
        你只看得到头"分开，后者正是最该立刻看见的事故形态。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "请查询北京天气"
        raw["llmResp"] = ""
        raw["lastCmdResult"] = "[exitCode:0]\n" + "y" * 9000
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        sandbox = [r.getMessage() for r in caught.records if r.getMessage().startswith("沙盒：")]
        self.assertEqual(len(sandbox), 1)
        self.assertIn(f"（共 {len(raw['lastCmdResult'])} 字）", sandbox[0])
        self.assertLess(len(sandbox[0]), 1000, "截断之后这行必须是有界的")

    def test_the_worst_round_stays_under_the_budget(self):
        """**硬约束 5 的直接守卫**：最坏的一回合，日志总量不得超预算。

        原来只数行数（42/45 行），行数**测不出字节**——而管道缓冲 64KB 是字节。
        三个文本字段同时顶到 `LOG_TEXT_MAX`（中文 1 字 = 3 字节）就是最坏局面：
        任务原文（判题器给的）、LLM 回复、沙盒输出 —— **实测 6002 字节**
        （干净回合 2294 + 三个 400 字的中文块 × 1200）。上限取 6500 而不是 6002：
        它要抓的是**结构性的膨胀**（少了一个 `_clip`、或者又加进来一个顶格的大字段），
        不是几个标签的字节抖动 —— 沙盒输出现实里基本是 ASCII（1 字 = 1 字节），
        真到 6002 这个数是中文任务原文 + 中文 LLM 回复 + 中文沙盒输出同时出现。

        数字与 `app._log` 的 docstring、`CLAUDE.md` 硬约束 5 三处一致。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "题" * LOG_TEXT_MAX
        raw["llmResp"] = "答" * LOG_TEXT_MAX
        raw["lastCmdResult"] = "出" * LOG_TEXT_MAX
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        total = sum(len(r.getMessage().encode("utf-8")) for r in caught.records)
        self.assertLess(total, 6500, f"最坏回合 {total} 字节，超了硬约束 5 的预算")


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

        `attackRange` 取**样例**的 4/7/INT_MAX，而不是任务书 §4.5.1 表格里的 3/6/10 ——
        两处矛盾，以 payload 为准（见 `CLAUDE.md`）。`cooldown` 三座炮**都没有这个字段**
        ⇒ 全 -1 ⇒ 不当成"冷却中"（否则火箭整晚一炮不开）。

        顺带钉一件事：**地图网格里还留着这三格**（武器一样挡路），
        但"哪座炮能开火"只认这份名册 —— 第 10 步把 `Map.weapons` 删掉之后，
        网格那份旧真相与这份新真相在这里对一次。
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

    def test_vendor_prices_come_from_the_payload_verbatim(self):
        """价目**照抄载荷**，不写死 —— 样例是 1/3/5，事件期间会变（任务书 L386）。

        字段坏掉的整条丢掉，**尤其不能把"解析不出来"的 -1 当成一个价格** ——
        `_int` 对缺字段/类型不对给 -1，而负的收购价不存在。混进来会让挑矿那一步
        选出一座**倒贴钱**的矿，或者反过来把整张表判成"没有价"（`_pick_ore` 滤掉 <= 0）。
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
        """`health == 0` 的炮**丢掉** —— 已毁的炮不该再被操控（demo 的 `alive()` 也是 `> 0`）。

        样例三座炮的 `health` 都是 1000，这里手改成 0 复现"被打掉一座"。
        `map.cells` 里它还在（地形由地图层管），但**名册里没有它** ⇒ 不会被发 `attack`。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10020:
                node["health"] = 0
        turn = model.load(raw)
        self.assertEqual({w.id for w in turn.weapons}, {10030, 10040})

    def test_a_destroyed_role_gets_no_command(self):
        """**阵亡的角色不该再收到指令**（`health == 0`）—— 与"操纵已毁的炮"同一类风险。

        死单位可能仍留在 `teamOur.roles` 里（官方 demo 的 `alive()` 就为此而写）。
        给尸体发 `move` / `collect` 判题器会怎么算文档没写，但没必要赌。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            if node["id"] == 10012:
                node["health"] = 0
        turn = model.load(raw)
        self.assertEqual({r.id for r in turn.roles}, {10010, 10011})

    def test_a_missing_health_is_not_a_death(self):
        """`health` **字段缺失**（`_int` 给 -1）与"声明阵亡"（0）要分开 —— 别用 `<= 0`。"""
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for node in raw["teamOur"]["roles"]:
            node.pop("health", None)
        turn = model.load(raw)
        self.assertEqual(len(turn.roles), 3)
        self.assertEqual(len(turn.weapons), 3)

    def test_ores_carry_their_kind_but_everything_blocks(self):
        """石/铁/铜**各自带矿种**进 `ores`；小贩 / 武器商店 / 任务点不是矿，却一样挡路。

        `ores` 装的是"矿点 → 矿种"而不是坐标集：选矿那一步要在三种矿之间按**收购价**排
        （铜未必比铁贵，见 `Turn.vendor_prices`），只留坐标就答不出"这是哪种矿"。

        后四项**不是矿**（任务书 L85）—— 只挑矿会让工人一头撞上去，这是"以为能走"的典型。
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


class GridTest(unittest.TestCase):
    """格子矩阵本身 —— `Turn.map` 这一步的主要交付物。

    **`cells` 才是真相，`render()` 只是给人看的**（那张字符表是有损的）。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.grid = model.load(json.loads(SAMPLE.read_text(encoding="utf-8"))).map

    def test_station_is_expanded_to_four_cells(self):
        """基地 2×2，`pos` 给的是**左上角** ⇒ 占 `y` 和 `y-1`。

        接口文档写的是"**双方**基地大小为 2*2"，所以敌我都要展。只标一格，角色会一头
        撞进基地里 —— 那是一条判题器不收的指令。**旧实现只展了我方**，敌方基地只挡了 1 格。
        """
        for corner, kind in ((Pos(10, 24), "station"), (Pos(30, 10), "enemy:station")):
            with self.subTest(corner=corner):
                for cell in base_cells(corner):
                    self.assertEqual(
                        self.grid.cells[cell.y][cell.x], kind, f"{cell} 应是基地的一部分"
                    )

    def test_enemy_is_prefixed_and_ours_is_not(self):
        """两方都有 `wall` / `station`，不区分敌我在网格里就撞车。"""
        self.assertEqual(self.grid.cells[20][5], "wall")  # 我方 (5,20)
        self.assertEqual(self.grid.cells[7][28], "enemy:wall")  # 敌方 (28,7)
        self.assertEqual(self.grid.cells[24][10], "station")  # 我方 (10,24)
        self.assertEqual(self.grid.cells[10][30], "enemy:station")  # 敌方 (30,10)

    def test_blocking_is_exactly_non_empty(self):
        """**这一步的核心回归**：`blocked` 必须恰好等于"非空格子"。

        `expected` 在这里**独立按任务书 L85 重算一遍**（四路来源 + 基地 2×2），完全不走
        `Map` 的代码 —— 迁移中漏掉任何一类挡路物，症状都是"以为能走、其实撞墙"。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        expected: set[Pos] = set()

        def add(node: dict) -> None:
            pos = Pos(node["pos"]["x"], node["pos"]["y"])
            # 基地 2×2 而 pos 只给左上角：与实现用的是同一个约定，但独立写一遍
            expected.update(base_cells(pos) if node["roleType"] == "station" else (pos,))

        for node in raw["mapInfo"]["zones"]:
            expected.add(Pos(node["pos"]["x"], node["pos"]["y"]))
        for path in ("teamOur", "teamEnemy", "robot"):
            for node in raw[path]["roles"]:
                add(node)

        self.assertEqual(self.grid.blocked, expected)
        # 手数一遍的交叉核对：14 zones + (我方 8 单位 + 基地 4 格) + (敌方 1 墙 + 基地 4 格) + 4 机器人
        self.assertEqual(len(self.grid.blocked), 35)

    def test_render_shape(self):
        """打印出来的图给调试用 —— 错了最坑：**y 翻反了图上照样"像张地图"**。

        布局：2 行列标尺 + 32 行网格，每行 **46 列** = 行号槽 `len("31")` + `" │ "` + 41 列。
        下面保留**裸下标**断言（而不是全靠行号槽）—— 下标把"字符落在第几列"钉死，
        行号槽只能证明"这一行的标号是几"。
        """
        lines = self.grid.render().splitlines()
        self.assertEqual(len(lines), 34)
        self.assertEqual({len(line) for line in lines}, {46})
        # 行号 = height-1-y（y 向上、终端从上往下印）；**小写 = 我方，大写 = 敌方**
        self.assertEqual(lines[9][15], "s")  # 我方基地左上角 (10,24)
        self.assertEqual(lines[10][16], "s")  # 我方基地右下角 (11,23)
        self.assertEqual(lines[9][14], "g")  # 加特林 (9,24)，与基地同一行
        self.assertEqual(lines[8][15], "r")  # 电磁狙击炮 (10,25)，在基地上方一行
        self.assertEqual(lines[23][35], "S")  # 敌方基地左上角 (30,10) → 第 31-10=21 行
        self.assertEqual(lines[9][9], "o")  # 石矿 (4,24)
        self.assertEqual(lines[26][33], "%")  # 敌方围墙 (28,7) —— 墙是大小写规则的例外
        self.assertEqual(lines[29][9], "x")  # 机器人 (4,4)
        self.assertEqual(lines[33][5], ".")  # (0,0) 空地 —— 最后一行是最底下的 y=0
        # 最上一行网格的行号必须是 height-1 —— 槽宽与网格对得上（差一列就全错位）
        self.assertTrue(lines[2].startswith("31 │ "), lines[2])

    def test_render_labels_every_row_with_its_y(self):
        """每行行号自 `height-1` **递减到 0**。

        这是 y 翻转最直接的守卫 —— `lines[31][0] == "."` 只能证明"最后一行是 y=0"，
        中间那些行翻反了它照样过。
        """
        labels = [
            int(line.split(" │ ")[0])
            for line in self.grid.render().splitlines()[2:]
        ]
        self.assertEqual(labels, list(range(31, -1, -1)))

    def test_render_degrades_when_there_are_no_cells(self):
        """没有格子 ⇒ 空串，`blocked` 也为空 ⇒ **不挡路也不动**（见 `Map.__init__`）。

        判据是 `cells` 为空而不是"尺寸非法"：高/宽为 0 时 `size` 看着合法，
        但同样一格都没有 —— 两种情形走的是同一条早返回。
        """
        for size in ((-1, -1), (41, 0), (0, 32)):
            with self.subTest(size=size):
                empty = Map(size, {Pos(0, 0): "wall"})
                self.assertEqual(empty.cells, ())
                self.assertEqual(empty.blocked, frozenset())
                self.assertEqual(empty.render(), "")

    def test_the_legend_covers_every_category(self):
        """图例必须覆盖字符表里的**每一个**类别。

        它守的是"加了新中立元素却忘了往 `_NAMES` 里补" —— 漏掉的症状是复盘时
        把新元素看成 `?`，而 `?` 在地图上到处都是（空地旁边就是），很难注意到。
        集合相等比"循环 assertIn"更强：多一个、少一个都挂。
        """
        self.assertEqual(set(_NAMES), set(_RENDER_SIDED) | set(_RENDER_NEUTRAL))
        for kind in _NAMES:
            self.assertIn(f"{_char(kind)}=", LEGEND)
        # 这三样不在 `_NAMES` 里 —— 它们不是"某个类别"，而是 `_char` 的兜底与大小写规则
        for token in ("x=机器人", ".=空地", "?=未知", "大写=敌方", "%=敌方围墙"):
            self.assertIn(token, LEGEND)

    def test_the_legend_names_the_task_points_by_faction(self):
        """`1`-`4` 是**阵营**的任务点，不是"我方/敌方"。

        它们来自 `zones` 的 `challengerTaskPoint*` / `defenderTaskPoint*`，两队**同时存在**；
        样例里我方恰好是挑战者、两套重合，所以**写错也测不出来**。
        我们**可接**的那两个点不在字符表里（它们是 `Turn.task_points`，来自
        `teamOur.playerTasks`，阵营已滤好）—— 别把两者混为一谈。
        """
        self.assertIn("挑战方任务点", LEGEND)
        self.assertIn("防守方任务点", LEGEND)
        self.assertNotIn("我方任务点", LEGEND)
        self.assertNotIn("敌方任务点", LEGEND)


class TurnSummaryTest(unittest.TestCase):
    """`Turn.summary()` 的三行摘要。

    它跑在 `app.handle` 的 `try` 里 —— **抛异常 = 整回合退化成空指令**，
    所以"空局面不炸"与"长度有上界"和内容一样重要。
    """

    def _turn(self, **kw) -> Turn:
        base = dict(
            round_no=85,  # 夜里
            map=Map((41, 32), {Pos(0, 0): "stone"}),
            roles=(),
            gold=20,
        )
        base.update(kw)
        return Turn(**base)

    def test_a_full_turn_reports_every_fact(self):
        turn = self._turn(
            roles=(
                Worker(10010, Pos(5, 23), 1),
                Worker(10012, Pos(10, 16), 1),
                Pioneer(10011, Pos(10, 12)),
            ),
            gold=20,
            weapons=(
                Weapon(10020, "gatling", Pos(9, 24), 4, 0),
                Weapon(10030, "railgun", Pos(10, 25), 7, 0),
                # 火箭刚打完一发，样例里没有 `cooldown` 字段 ⇒ 这一条只能合成
                Weapon(10040, "rocket", Pos(9, 25), 2**31 - 1, 3),
            ),
            robots=(Robot(Pos(4, 4), 40), Robot(Pos(5, 5), 800)),
            task_points=(Pos(14, 14), Pos(17, 17)),
        )
        lines = turn.summary().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[0],
            "回合 85（夜里）｜ 金币 20 ｜ 武器 3/3："
            "10020 gatling(9,24)r4 10030 railgun(10,25)r7 10040 rocket(9,25)r∞c3",
        )
        self.assertEqual(
            lines[1],
            "我方 10010 worker(5,23)石1 ｜ 10012 worker(10,16)石1 ｜ 10011 pioneer(10,12)石0",
        )
        self.assertEqual(
            lines[2],
            "机器 2 台：(4,4)h40 (5,5)h800 ｜ 可接任务点 (14,14) (17,17)",
        )

    def test_an_empty_turn_still_prints_three_lines(self):
        """空局面：一条事实都没有，但**每一格都得有字**（`无` / `0 台`），不能是空行。"""
        lines = self._turn(roles=(Worker(10010, Pos(5, 23)),)).summary().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("武器 0/1：无", lines[0])
        self.assertIn("机器 0 台：无", lines[2])
        self.assertIn("可接任务点 无", lines[2])

    def test_missing_fields_are_not_reported_as_zero(self):
        """`-1` 是"字段缺失"，不是 0 金 / 第 -1 回合 —— 打成 `?`，别让它看着像个事实。"""
        line = self._turn(gold=-1, round_no=-1).summary().splitlines()[0]
        self.assertIn("回合 ?（夜里）", line)
        self.assertIn("金币 ?", line)

    def test_the_unknown_range_is_not_printed_as_a_negative_number(self):
        """射程 -1 = 字段缺失 ⇒ 够不着 ⇒ `?`；0 也照原样打 0（不做特殊处理）。"""
        turn = self._turn(
            roles=(Worker(10010, Pos(5, 23)),),
            weapons=(
                Weapon(1, "gatling", Pos(9, 24), -1, -1),
                Weapon(2, "gatling", Pos(9, 25), 0, 0),
            ),
        )
        line = turn.summary().splitlines()[0]
        self.assertIn("1 gatling(9,24)r?", line)
        self.assertIn("2 gatling(9,25)r0", line)
        # 冷却 -1 与 0 都是"没有冷却"，都不该出现 `c`
        self.assertNotIn("c-1", line)
        self.assertNotIn("r0c0", line)

    def test_long_lists_are_capped(self):
        """机器人是逐回合**全量**推送的 ⇒ 摘要长度必须有上界，超出的只报个数。"""
        turn = self._turn(
            roles=(Worker(10010, Pos(5, 23)),),
            robots=tuple(Robot(Pos(i, 5), 10 * i) for i in range(11)),
        )
        line = turn.summary().splitlines()[2]
        self.assertIn("机器 11 台：", line)
        self.assertIn("…+3", line)  # 11 - SUMMARY_MAX_ITEMS(8) = 3
        self.assertNotIn("(10,5)", line)  # 第 11 台没打出来


class BuildGeometryTest(unittest.TestCase):
    """可建造区与武器落点。

    ⚠️ **公式的来源是图不是正文**（`docs/pic/build_map.png`）：任务书没写坐标公式，
    接口文档的 `mapInfo` 里也没有可建造区字段。算错 ⇒ `build` 落点非法 ⇒ 那 25 金币白花。
    """

    BASE = Pos(10, 24)  # 样例里的我方基地

    def test_weapon_cells_are_the_ring_around_the_base(self):
        """12 格 = 基地外圈 4×4 减去基地自己，且**一格都不和基地重叠**。"""
        cells = weapon_cells(self.BASE)
        own = base_cells(self.BASE)
        self.assertEqual(len(cells), 12)
        self.assertEqual(set(cells) & own, set(), "武器格不能落在基地身上")
        self.assertEqual({c.x for c in cells}, {9, 10, 11, 12})
        self.assertEqual({c.y for c in cells}, {22, 23, 24, 25})
        # 样例那三座武器都该落在这个环上 —— 唯一的"外部"交叉验证
        for pos in (Pos(9, 24), Pos(9, 25), Pos(10, 25)):
            self.assertIn(pos, cells)

    def test_sites_face_away_from_the_robots(self):
        """基地在左半 ⇒ 后列取 `bx-1`、前排两角取 `bx+2`；在右半镜像。

        机器人从基地**面向地图中心**的那一侧来（`docs/pic/大致地图信息.png`）。判反了
        武器就摆在迎着机器人的一侧 —— 不报错、不违规，只是白建三座。
        """
        left = weapon_sites(Pos(10, 24), 41)  # 左半 ⇒ 后列 x=9、前排 x=12
        self.assertEqual([c.x for c in left], [9, 12, 12])
        right = weapon_sites(Pos(30, 10), 41)  # 右半 ⇒ 后列 x=32、前排 x=29
        self.assertEqual([c.x for c in right], [32, 29, 29])
        self.assertEqual(len(left), len(right), "两侧都该是整整齐齐三个落点")

    def test_the_three_sites_are_the_back_cell_and_the_two_front_corners(self):
        """后列**贴基地下沿**那一格 + 前排两角，顺序即建造顺序。"""
        self.assertEqual(
            weapon_sites(Pos(10, 24), 41),
            (Pos(9, 23), Pos(12, 22), Pos(12, 25)),
        )
        #: 换边后整套落点自动跟着翻 —— 按**基地坐标**判而不用 `teamOur.type`
        self.assertEqual(
            weapon_sites(Pos(30, 10), 41),
            (Pos(32, 9), Pos(29, 8), Pos(29, 11)),
        )

    def test_every_site_touches_the_base(self):
        """**这才是这个阵形的理由**：三个落点各自都与基地的一格切比雪夫距离 1。

        升级券/维修包必须在**目标建筑周围一格内**使用（任务书 L292 / L314），而 `attack`
        也要求角色站在炮旁。于是同一个角色站在落点上，**脚下的炮和旁边的基地一够就是两个**。
        顺带钉住"落点在武器环上" —— 不在环上的话 `build` 落点非法、那 25 金币白花。
        """
        for base, width in ((Pos(10, 24), 41), (Pos(30, 10), 41)):
            with self.subTest(base=base):
                own = base_cells(base)
                ring = weapon_cells(base)
                for cell in weapon_sites(base, width):
                    self.assertIn(cell, ring, "落点必须在武器环上")
                    self.assertEqual(
                        min(cell.dist(b) for b in own),
                        1,
                        f"{cell} 该贴着基地的一角，否则升级/维修券够不着",
                    )


class DayNightTest(unittest.TestCase):
    """日历：`build` 仅白天，判反了就会在夜里发 `build`（一次执行失败）。"""

    def _turn(self, round_no: int) -> Turn:
        return Turn(round_no=round_no, map=Map((41, 32), {}), roles=(), gold=0)

    def test_is_day_boundaries(self):
        """130 回合 1 天 = 白 70 + 夜 60（任务书 L90），`within % 130` 从 1 起数。"""
        for round_no, day in ((1, True), (70, True), (71, False), (130, False), (131, True)):
            with self.subTest(round_no=round_no):
                self.assertIs(self._turn(round_no).is_day, day)
        self.assertFalse(self._turn(-1).is_day, "roundNo 缺失 ⇒ 判成夜里 ⇒ 不建造")


class MineApproachTest(unittest.TestCase):
    """合成局面：工人真的能走到矿边、开始采，而且不来回抖。**

    **必须有基地**：矿只是砌墙的原料，基地没了就没有围墙环 ⇒ 采了也没用，工人干脆不动
    （`_ring` 返回空）。这个降级方向是有意的，所以 `_turn` 里摆了一个基地。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def _turn(self, worker_pos: Pos, mine_kind: str = "stone", stone: int = 0) -> Turn:
        return Turn(
            round_no=1,
            map=Map((41, 32), {self.BASE: "station", self.MINE: mine_kind}),
            roles=(Worker(1, worker_pos, stone),),
            gold=0,
        )

    def test_worker_walks_to_the_mine_and_then_harvests_in_place(self):
        """把回合串起来跑，看它**收敛**：走得到矿边，到了就原地采，不再挪。

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


class BuildWeaponTest(unittest.TestCase):
    """合成开局：白天、0 武器、基地在左半 —— 工人真的会建满三座、建在该建的地方、然后收手。

    **把回合串起来跑**：单帧"目标格算得对"证明不了收不收得住（`MineApproachTest` 同理）。
    结算照判题器的口径来 —— 一回合一步，建起来的武器下一回合就挡路、也占掉那个格子。
    """

    #: 三个落点 = (9,23) 后列 + (12,22)/(12,25) 前排两角（见 `weapon_sites`）
    BASE = Pos(10, 24)
    MINE = Pos(4, 24)

    def setUp(self) -> None:
        self.gold = 75  # 开局：恰好买满三座（25×3）
        #: 已建成的武器**名册** —— 唯一真相，地形由它推（见 `_terrain`）。
        #: 直接往 `entries` 里塞一把 `"gatling"` 的话，`turn.weapons` 是空的、
        #: 名额永远算成"还差三座"，而那一格又挡路 —— 夹具与真实局面不同形。
        self.weapons: list[Weapon] = []
        self.walls: dict[Pos, str] = {}
        #: 静态地形（基地 + 矿），删掉基地就等于"基地没了"那个降级局面
        self.ground: dict[Pos, str] = {self.BASE: "station", self.MINE: "stone"}
        self.roles: dict[int, BaseRole] = {
            1: Pioneer(1, Pos(20, 20)),
            # 两个工人都**贴着**后方那一列（各差一格），第 1 回合就能动手
            2: Worker(2, Pos(8, 23)),
            3: Worker(3, Pos(8, 24)),
        }
        self.builds: list[tuple[str, Pos]] = []

    def _turn(self, round_no: int = 1) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map((41, 32), _terrain(tuple(self.weapons), self.walls, self.ground)),
            roles=tuple(self.roles.values()),
            gold=self.gold,
            weapons=tuple(self.weapons),
        )

    def _weapons(self) -> list[str]:
        return [name for name, _ in self.builds if name != WALL]

    def _settle(self, round_no: int = 1, limit: int = 20, want: int | None = None) -> None:
        """跑到**建满 `want` 座**为止（默认三座都建上）。墙不归这个类管（见 `BuildWallTest`），
        但会顺路砌起来 —— 所以 `builds` 里混着墙，统计武器时要滤掉，金币也只按武器扣。"""
        want = len(WEAPONS_BY_SITE) if want is None else want
        for _ in range(limit):
            if len(self._weapons()) >= want:
                return
            cmds = plan(self._turn(round_no))
            if not cmds:
                return
            for key, cmd in cmds.items():
                role_id = int(key)
                target = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                if cmd["action"] == "build":
                    self.builds.append((cmd["name"], target))
                    if cmd["name"] == WALL:
                        self.walls[target] = WALL
                    else:
                        self._add_weapon(cmd["name"], target)
                        self.gold -= 25
                else:
                    self.roles[role_id] = type(self.roles[role_id])(role_id, target)
        self.fail(f"{limit} 回合还没把 {want} 座武器建完，说明在原地绕圈")

    def _add_weapon(self, kind: str, cell: Pos) -> None:
        """照格式记一座建成的武器（id 递增即可 —— 这里没人按 id 排序）。"""
        self.weapons.append(
            Weapon(id=200 + len(self.weapons), kind=kind, pos=cell, attack_range=4, cooldown=0)
        )

    def _builds(self, cmds: dict) -> list[dict]:
        return [c for c in cmds.values() if c["action"] == "build"]

    def test_builds_the_three_weapons_on_their_own_sites(self):
        """三座各自落在**自己的**落点上：火箭进后列、加特林与电磁炮分居两个前角。

        断言用**集合**而不是建成的先后 —— 两个工人谁先走到哪一格是路径决定的，
        那不是这条用例要钉的东西；要钉的是"**种类 ↔ 落点**"这层绑定。
        """
        self._settle()
        self.assertEqual(len(self._weapons()), 3)
        self.assertEqual(
            {(name, cell) for name, cell in self.builds},
            set(zip(WEAPONS_BY_SITE, weapon_sites(self.BASE, 41))),
        )
        self.assertEqual(self.gold, 0)

    def test_stops_at_three_even_with_lots_of_gold(self):
        """停手是因为**份额**（角色数），不是因为钱花光了 —— "建立多了没有意义"。"""
        self.gold = 200
        self._settle()
        self.assertEqual(len(self._weapons()), len(WEAPONS_BY_SITE))
        self.assertGreater(self.gold, 0, "这次不是钱见底才停的")

    def test_a_short_budget_builds_only_one(self):
        """只有 25 金：**只建一座**（后列排第一的火箭），另一个工人转去采矿。

        金币按**递减预算**扣。写成 `gold >= 25 * 待建数`（25 < 75 ⇒ 一座都不建）就全错了。
        """
        self.gold = 25
        cmds = plan(self._turn())
        builds = self._builds(cmds)
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0]["name"], "rocket")
        self.assertEqual({c["action"] for c in cmds.values()}, {"build", "move"})

    def test_a_blocked_site_drops_only_its_own_weapon(self):
        """后列那一格被占了 ⇒ **只有火箭不建**，两座前角照落在各自的位置上。

        这条钉的是 `_slots` 里"**先按种类配对、再滤**"的顺序。反过来写（先滤种类、
        再 `zip` 落点）的话，"少一座火箭"会让 `zip` 整体前移 —— 加特林落到 (12,22)、
        电磁炮落到 (12,25)，**每个种类都挪到了别人家的落点上**，而报文完全合法、本地全绿。
        炮落在自己落点上时由 `have` 那道滤网挡住（同种类不会重建）；这里挡住它的是**墙**，
        所以走的是 `blocked` 那一道 —— 顺带守住"覆盖会把原武器打成 level1"（§4.5.1 补充说明）。
        """
        self.walls[Pos(9, 23)] = WALL
        self._settle(want=2)
        self.assertEqual(
            {(name, cell) for name, cell in self.builds},
            {("gatling", Pos(12, 22)), ("railgun", Pos(12, 25))},
        )
        self.assertNotIn("rocket", self._weapons(), "落点被占 ⇒ 那一座就是不建，不换地方")

    def test_night_builds_nothing(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 0 武器、75 金也一座都不许建。"""
        self.assertNotIn("build", {c["action"] for c in plan(self._turn(round_no=85)).values()})

    def test_no_base_means_no_build(self):
        """基地没了就没有可建造区（坐标全由它推）⇒ 不建，而不是瞎猜一个坐标。"""
        del self.ground[self.BASE]
        self.assertNotIn("build", {c["action"] for c in plan(self._turn()).values()})


class WallRingTest(unittest.TestCase):
    """围墙环的几何与建造顺序。

    ⚠️ **公式的来源是图不是正文**（`docs/pic/build_map.png`）：任务书只说了"蓝色区域只能建造武器、
    黄色区域只能建造围墙"，没有任何坐标。算错 ⇒ `build` 落点非法。
    """

    BASE = Pos(10, 24)

    def test_ring_is_sixteen_cells_free_of_the_base_and_the_weapons(self):
        cells = wall_cells(self.BASE, 41)
        self.assertEqual(len(cells), 16, "6×6 边框 20 格减去缺口的 4 格")
        self.assertEqual(len(set(cells)), 16, "不该有重复格")
        self.assertEqual(set(cells) & base_cells(self.BASE), set(), "不能落在基地身上")
        self.assertEqual(set(cells) & set(weapon_cells(self.BASE)), set(), "不能占武器环")

    def test_the_gap_is_the_back_columns_middle_four(self):
        """缺口 = **背面那一列**（背离机器人的一侧）的中间 4 格，上下两角仍要砌。

        用户选定：环一旦闭合，工人就进出不得了 —— 既采不了矿，也回不到环内操炮。
        """
        cells = set(wall_cells(self.BASE, 41))
        back = {Pos(8, y) for y in range(21, 27)}  # 基地在左半 ⇒ 背面是 x = bx-2
        self.assertEqual(back - cells, {Pos(8, y) for y in range(22, 26)}, "缺口是中间四格")
        self.assertEqual(cells & back, {Pos(8, 21), Pos(8, 26)}, "背面只留上下两角")

    def test_the_first_six_cells_face_the_robots(self):
        """前 6 格 = **迎着机器人**的那一列，且正对基地纵深中心的两格最先。

        策略指导：「墙建立在**面向机器人进攻的方向**（保护基地）」。判反了墙就砌在机器人
        不来的一侧 —— 不报错、不违规，只是整段白砌，而且石头是工人一块块背回来的。
        """
        first = wall_cells(self.BASE, 41)[:6]
        self.assertEqual(set(first), {Pos(13, y) for y in range(21, 27)}, "左半 ⇒ 正面是 bx+3")
        self.assertEqual(first[:2], (Pos(13, 23), Pos(13, 24)), "正对基地纵深的两格先砌")

    def test_the_ring_mirrors_for_a_right_half_base(self):
        """基地在右半 ⇒ 正面是 `bx-2`、背面是 `bx+3`（换边后自动跟着翻）。

        按**基地坐标**判而不用 `teamOur.type` —— 下半场换边后队伍身份不变、基地会挪。
        """
        ring = wall_cells(Pos(30, 10), 41)
        self.assertEqual({c.x for c in ring[:6]}, {28}, "右半 ⇒ 正面是 bx-2")
        self.assertEqual(ring[-2:], (Pos(33, 7), Pos(33, 12)), "背面 = bx+3 那一列的上下角")


class BuildWallTest(unittest.TestCase):
    """合成开局：白天、武器已建满 → 工人去采石、回来砌墙，落点严格按优先级。

    **把白天串起来跑**：单帧看不出"采够了没有、砌到哪一格、会不会来回抖"。
    每回合重算预算，所以必须真的过一遍时间。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)  # 基地左侧的石矿
    #: 三座武器先摆好 —— 否则金币/名额会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）。
    #: **记录是唯一真相**（`_records`），地形由 `_terrain` 推 —— 只有网格没有名册的话，
    #: "还差几座"会算成还差三座，而降级方向恰好也是"不建"（金币 0），症状就藏起来了。
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})

    def setUp(self) -> None:
        self.entries: dict[Pos, str] = _terrain(
            self.WEAPONS, {self.BASE: "station", self.MINE: "stone"}
        )
        self.worker = Worker(1, Pos(6, 24))

    def _turn(self, round_no: int = 1, stone: int = 0, pos: Pos | None = None) -> Turn:
        self.worker = Worker(1, pos or self.worker.pos, stone)
        return Turn(
            round_no=round_no,
            map=Map((41, 32), self.entries),
            roles=(self.worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_day_one_mines_then_walls_the_whole_ring_in_order(self):
        """把白天串起来跑到砌满：**16 格全砌上，且顺序与 `wall_cells` 逐格一致**。

        这一条把"回合预算 → 采矿 → 砌墙"整条线钉在一起：预算算大了天黑砌不完，
        算小了石头不够、工人在工地干等；顺序错了则会先把背面砌满、正面空着。
        """
        built: list[Pos] = []
        stone = 0
        for _ in range(70):  # 一个白天 70 回合
            cmd = plan(self._turn(stone=stone)).get("1")
            if cmd is None:
                break
            cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
            if cmd["action"] == "collect":
                self.assertEqual(cell, self.MINE, "只能采石头，且 `collect` 给的是矿的坐标")
                stone += 1
            elif cmd["action"] == "build":
                self.assertEqual(cmd["name"], WALL)
                # 石头是**攒够了才回来砌**的（这正是回合预算要的效果：少跑冤枉路），
                # 所以这里只要求"有得扣"，扣多少由下面每砌一座减 1 来核。
                self.assertGreaterEqual(stone, 1, "砌一座墙的代价是**石头×1**，背包里得有")
                self.entries[cell] = WALL
                built.append(cell)
                stone -= 1
                self.worker = Worker(1, self.worker.pos, stone)
            else:
                self.worker = Worker(1, cell, stone)

        self.assertEqual(built, list(wall_cells(self.BASE, 41)), "顺序必须与优先级表一致")
        self.assertEqual(stone, 0, "别多采 —— 白天总共就 16 格墙可砌")

    def test_a_late_start_stops_mining_and_goes_to_build(self):
        """白天快过完了（roundNo=60 ⇒ 只剩 11 回合）⇒ **不再采矿**，拿着手里的石头直接去工地。

        「必须在晚上到来前将墙建好，注意计算回合数」（策略指导）—— 这是唯一的时间硬约束。
        两个局面只差 `roundNo`，走的方向必须反过来。
        """
        pos, stone = Pos(6, 24), 3
        early = plan(self._turn(round_no=1, stone=stone, pos=pos))["1"]
        late = plan(self._turn(round_no=60, stone=stone, pos=pos))["1"]
        self.assertEqual({early["action"], late["action"]}, {"move"})
        early_cell = Pos(early["targetPos"][0]["x"], early["targetPos"][0]["y"])
        late_cell = Pos(late["targetPos"][0]["x"], late["targetPos"][0]["y"])
        self.assertLess(early_cell.dist(self.MINE), pos.dist(self.MINE), "白天还长 ⇒ 继续朝矿走")
        self.assertGreater(late_cell.dist(self.MINE), pos.dist(self.MINE), "时间不够 ⇒ 掉头去工地")

    def test_a_finished_ring_stops_the_stone_mining(self):
        """16 格都砌满了 ⇒ 不再采**石头**（多采的只会压在背包里）。

        这一支现在会转去采最值钱的矿（`SpareOreTest`），而本夹具**没有价目表**
        （`vendor_prices` 缺省为空）⇒ 挑不出"最值钱的矿" ⇒ 一条都不发。这是有意的降级方向：
        没有价格就无从挑，宁可不动，与 `_gold` / `_size` 同源。
        """
        self.entries.update({c: WALL for c in wall_cells(self.BASE, 41)})
        self.assertEqual(plan(self._turn(stone=0)), {})

    def test_no_base_means_no_wall_and_no_mining(self):
        """基地没了就没有围墙环（坐标全由它推）⇒ 连矿都不去采，而不是瞎找一个坐标。"""
        del self.entries[self.BASE]
        self.assertEqual(plan(self._turn(stone=0)), {})


class SpareOreTest(unittest.TestCase):
    """墙砌满之后的白天：**去采收购价最高的矿**（`docs/策略指导.md` 那条的后半句）。

    「手里面保持能建造墙的石头量就行，**然后**选择价格最高的矿」—— 那个"然后"是**顺序**：
    砌墙阶段只认石矿（铜再贵也砌不了墙），**砌完之后**才轮到按价格挑。顺序反了的话墙永远
    砌不上，而症状是"工人一直在采铜、围墙一格没有"。

    价目取自载荷 `vendorShopList`（`Turn.vendor_prices`），**不写死"铜 > 铁 > 石头"**：
    样例那三档 1/3/5 只是**样例**，任务书 L386 明说官方消息会让价格波动（铁矿塌方 ⇒
    铁稀缺 ⇒ 收购价上涨）。所以这里专门把顺序翻过来测 —— 谁把铜写死在最前，哪一条就挂。
    """

    BASE = Pos(10, 24)
    #: 三座武器先摆好 —— 否则名额/金币会先把工人抽去建武器（那是 `BuildWeaponTest` 的事）
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 16 格全砌满 ⇒ `_ring` 空 ⇒ 进入"墙砌完了"那一支
    RING = {c: WALL for c in wall_cells(Pos(10, 24), 41)}
    #: 一近一远两座矿，**近的便宜、远的贵** —— 远近与贵贱分开，才测得出按哪个排
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
            roles=(Worker(1, pos, 0),),
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
        """本回合那条 `collect` 瞄的是哪座矿 —— **比"朝哪边走一格"结实得多**。"""
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
        """**铁矿塌方 ⇒ 铁稀缺 ⇒ 收购价涨过铜**：同一个局面，走的方向必须反过来。

        谁把"铜 > 铁 > 石头"当常量写进代码，这一条就挂 —— 而事件期间那个常量恰好是错的。
        """
        start = Pos(30, 24)
        step = self._move_to(self._turn(start, {"stone": 1, "iron": 9, "copper": 5}))
        self.assertLess(step.dist(self.NEAR_IRON), start.dist(self.NEAR_IRON), "该朝铁矿走")

    def test_a_mine_the_vendor_does_not_buy_is_not_worth_a_step(self):
        """小贩不收的矿**一步都不为它走**（查不到的名字按 0 算）：宁可多走几步去那座收的。

        近的那座铜矿不在价目表里 ⇒ 它的价是 0，而 `_pick_ore` 把价 0 的整座丢掉 ——
        "离得近"不构成理由，卖不出钱的矿走过去也是白走。三座都不收 ⇒ 一条指令都不发
        （与 `_gold` / `_size` / `_stone` 同一条降级方向：宁可少做）。
        """
        start = Pos(30, 24)
        step = self._move_to(self._turn(start, {"iron": 3}))  # 只有铁有价，铜按 0 算
        self.assertLess(step.dist(self.NEAR_IRON), start.dist(self.NEAR_IRON), "该舍近求远")
        for prices in ({}, {"stone": 1}):  # 场上这两种矿，小贩一种都不收
            with self.subTest(prices=prices):
                self.assertEqual(plan(self._turn(start, prices)), {}, "谁也不收 ⇒ 哪儿也不去")

    def test_too_late_in_the_day_to_walk_there_and_back(self):
        """白天不够走个来回了 ⇒ 不动身。参照点是**基地**（夜里的炮位就在它四周）。

        临走一头扎进远处的矿、黑天里还在赶路 = 拿火力换矿石。同一个局面只差 `roundNo`，
        `roundNo=60` 时 `day_rounds_left - TIME_MARGIN` = 6，而这一趟来回要 20 回合。
        """
        start = Pos(30, 24)
        early = plan(self._turn(start, self.SAMPLE_PRICES, round_no=1))
        late = plan(self._turn(start, self.SAMPLE_PRICES, round_no=60))
        self.assertIn("1", early, "白天还长 ⇒ 该动身")
        self.assertEqual(late, {}, "时间不够来回 ⇒ 哪儿也不去")

    def test_the_ring_decides_whether_price_gets_a_vote(self):
        """**同一个局面**，只差围墙砌没砌满：砌着 ⇒ 只认石矿，砌完了 ⇒ 才按价格挑。

        这就是「手里面保持能建造墙的石头量就行，**然后**选择价格最高的矿」里那个"然后"。
        两座矿**都贴在工人身边**（一边一座），所以两种口径给的是**一左一右**、无从含糊 ——
        比"朝哪边走一格"结实：走一格常常同时靠近两座矿，那种写法会**假通过**。
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


class TwoWallBuildersTest(unittest.TestCase):
    """两个工人同时在环上：**不能对着改目标来回踱步**。

    实测过的死循环（第 8 步）：`_ring` 原本把"工人脚下那一格"也当成挡路的格划掉，
    于是另一个工人的 `free[0]` 整体后移一格、掉头去砌更靠后的墙；等前一个工人一挪窝，
    目标又变回来 —— 两人在两格之间一直转到天黑，**一格墙都没砌**（端到端局面上 26 回合 0 座）。
    单帧断言看不出来，必须把回合串起来跑。

    ⚠️ **角色必须自己铺进地图**：真实路径里 `protocol.model._entries` 会把 `teamOur.roles`
    写进网格，于是"谁站在哪"进 `blocked`。手搓 `Map` 时不铺角色就复现不出这个循环 ——
    本用例第一版正是这么写的，于是它连旧代码都放过去了。
    """

    BASE = Pos(10, 24)
    MINE = Pos(4, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: 死循环当时的局面：正面列已砌三格，两个工人卡在基地与正面列之间的夹缝里
    FRONT_BUILT = {Pos(13, 22): WALL, Pos(13, 23): WALL, Pos(13, 24): WALL}

    def test_two_workers_keep_building_in_the_pocket(self):
        entries = _terrain(
            self.WEAPONS, {self.BASE: "station", self.MINE: "stone"}, self.FRONT_BUILT
        )
        roles = {
            10010: Worker(10010, Pos(13, 21), 14),
            10012: Worker(10012, Pos(11, 22), 13),
        }
        built: list[Pos] = []
        for rnd in range(1, 7):  # 6 回合足够砌完剩下的正面列（死循环下一次都砌不上）
            # 角色压在静态层之上 —— 与 `model._entries` 的写入顺序一致（单位盖过地形）
            grid = {**entries, **{r.pos: "worker" for r in roles.values()}}
            turn = Turn(
                round_no=rnd,
                map=Map((41, 32), grid),
                roles=tuple(roles.values()),
                gold=0,
                weapons=self.WEAPONS,
            )
            for key, cmd in plan(turn).items():
                role_id = int(key)
                cell = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
                role = roles[role_id]
                if cmd["action"] == "build":
                    self.assertEqual(cmd["name"], WALL)
                    entries[cell] = WALL
                    built.append(cell)
                    roles[role_id] = Worker(role_id, role.pos, role.stone - 1)
                elif cmd["action"] == "collect":
                    roles[role_id] = Worker(role_id, role.pos, role.stone + 1)
                else:
                    roles[role_id] = Worker(role_id, cell, role.stone)

        self.assertGreaterEqual(len(built), 2, "两个工人在原地打转 ⇒ 一座墙都砌不上")
        self.assertEqual({c.x for c in built}, {13}, "先砌的必须是**正面**那一列")
        # 只钉"这两格必须补上"：再往后铺到 (13,26) 也是对的（同属正面列），不必锁死集合
        self.assertTrue({Pos(13, 21), Pos(13, 25)} <= set(built), "夹缝里那两格得砌上")


class NightWeaponTest(unittest.TestCase):
    """夜里：**所有角色**（含开拓者）回炮位、开火打**射程内血最少**的机器人。

    `attack` **仅黑夜**可用、`build`/`collect` 仅工人（任务书 §4.4），所以夜里
    除了 `move` 就只该有 `attack`。
    """

    BASE = Pos(10, 24)
    NIGHT = 85
    NEAR, FAR = Pos(12, 25), Pos(9, 22)  # 两座加特林
    REACH = 4  # 加特林 L1 的射程，**取自样例 payload**（任务书表格写的是 3）
    GUN = 10020  # `_manned` 那座炮的 id —— `attack` 的 key 就是它

    def _turn(
        self,
        *roles: BaseRole,
        weapons: tuple[Weapon, ...] | None = None,
        robots: tuple[Robot, ...] = (),
        round_no: int | None = None,
    ) -> Turn:
        # `weapons=None` 才是"用默认的两座"；**不能用 `or`** —— 空元组也是假值，
        # 那样 `test_no_weapons_means_nothing_to_do` 会静默拿到两座武器、白测一场。
        weapons = self._guns(self.NEAR, self.FAR) if weapons is None else weapons
        return Turn(
            round_no=self.NIGHT if round_no is None else round_no,
            map=Map((41, 32), _terrain(weapons, {self.BASE: "station"})),
            roles=roles,
            gold=0,
            weapons=weapons,
            robots=robots,
        )

    def _guns(self, *cells: Pos) -> tuple[Weapon, ...]:
        """按样例的 L1 加特林造记录（射程 4、无冷却），id 从 `GUN` 起编号 ——
        断言里要能一眼看出"开火的是哪一座"，所以第一座就取 `GUN`。"""
        return tuple(
            Weapon(id=self.GUN + i, kind="gatling", pos=cell, attack_range=self.REACH, cooldown=0)
            for i, cell in enumerate(cells)
        )

    def _manned(
        self,
        *robots: Robot,
        reach: int | None = None,
        cooldown: int = 0,
        round_no: int | None = None,
    ) -> Turn:
        """一名工人**已经贴着**那座加特林（切比雪夫 1）—— 开火与否只看目标与冷却。"""
        gun = Weapon(
            id=self.GUN,
            kind="gatling",
            pos=self.NEAR,
            attack_range=self.REACH if reach is None else reach,
            cooldown=cooldown,
        )
        return self._turn(
            Worker(1, Pos(12, 24)), weapons=(gun,), robots=robots, round_no=round_no
        )

    def _only_cmd(self, turn: Turn) -> dict:
        cmds = plan(turn)
        self.assertEqual(len(cmds), 1, f"应当恰好一条指令，实际 {cmds}")
        return next(iter(cmds.values()))

    def test_walks_toward_the_nearest_weapon_and_never_builds(self):
        """夜里 `build` 不可用（任务书 §4.4）—— 一条 `build`/`collect` 都不该有。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26))))
        self.assertEqual(set(cmds), {"1"})
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(cell, Pos(13, 25), "朝最近的 (12,25) 迈一步，停在它旁边")

    def test_two_roles_do_not_share_a_weapon(self):
        """一人只能操一座武器 —— 两个角色都挤同一座，等于白白少一门火力。"""
        cmds = plan(self._turn(Worker(1, Pos(14, 26)), Worker(2, Pos(14, 24))))
        second = Pos(cmds["2"]["targetPos"][0]["x"], cmds["2"]["targetPos"][0]["y"])
        # 两个角色到 (12,25) 都是 2 格，最近的**都是它**；没有去重的话 2 号也会奔它去
        self.assertGreater(second.dist(self.NEAR), 1, "2 号必须换一座，不能也去挤 (12,25)")
        self.assertLess(second.dist(self.FAR), 5, "换的那座该是下一近的 (9,22)")

    def test_no_weapons_means_nothing_to_do(self):
        """武器全没了 ⇒ 不动（空指令合法），而不是瞎走。"""
        self.assertEqual(plan(self._turn(Worker(1, Pos(14, 26)), weapons=())), {})

    def test_the_pioneer_also_mans_a_weapon(self):
        """**开拓者也在炮位上**（§4.4 里 `attack` 的可用角色是"全部"）。

        用户这一步的原话是"**所有**角色（上限三个）回来操作武器"。开拓者漏掉的话
        本地全绿，只是整夜少一门火力 —— 而它恰恰是离炮最远的那个。
        """
        cmds = plan(
            self._turn(
                Pioneer(1, Pos(12, 24)),  # 贴着 NEAR
                Worker(2, Pos(9, 21)),  # 贴着 FAR（射程内没有敌人 ⇒ 它不开火）
                robots=(Robot(Pos(12, 27), 40),),
            )
        )
        self.assertEqual(set(cmds), {str(self.GUN)}, "开拓者开的那一炮在不在？")
        self.assertEqual(cmds[str(self.GUN)]["controllerId"], "1", "操控者是开拓者")

    def test_the_command_hangs_on_the_weapon_id(self):
        """⚠️ **`attack` 的 key 是武器 id**，操控角色在 `controllerId` 里（`docs/response.txt`）。

        写成 `{"1": …}`（角色 id）本地照样绿，判题器看到的却是"角色 1 在操炮" —— 一次"指令非法"。
        """
        cmd = self._only_cmd(self._manned(Robot(Pos(12, 27), 40)))
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["controllerId"], "1", "操控者是角色 id（字符串）")
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 27}])

    def test_picks_the_weakest_not_the_nearest(self):
        """用户选定：**射程内血最少的**（补刀优先）—— 不是最近的那只。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 900), Robot(Pos(14, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}], "该打 40 血那只，哪怕它更远")

    def test_distance_breaks_the_health_tie(self):
        """血量并列时打**近**的（同血量下先打完近的，远处的下一回合再补）。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(15, 25), 40), Robot(Pos(13, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 26}])

    def test_ignores_the_weakest_when_it_is_out_of_range(self):
        """**先按射程过滤、再取血最少的** —— 顺序写反就成了"拿全场最弱、但打不着的当目标"。"""
        weak = Robot(Pos(12, 31), 10)  # 距 (12,25) 是 6，够不着
        cmd = self._only_cmd(self._manned(weak, Robot(Pos(13, 25), 900)))
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "10 血那只够不着，只能打 900 的")

    def test_range_boundary_is_chebyshev(self):
        """射程用**切比雪夫**（任务书 L230），边界取 `<=`：对角 4 格打得到，5 格打不到。

        写成欧氏/曼哈顿，或把 `<=` 写成 `<`，都会在边界上静静地少打一发。
        """
        edge = self._manned(Robot(Pos(16, 29), 40))  # 对角 (+4,+4) ⇒ 切比雪夫 4
        self.assertEqual(self._only_cmd(edge)["targetPos"], [{"x": 16, "y": 29}])
        self.assertEqual(plan(self._manned(Robot(Pos(17, 30), 40))), {}, "5 格超出射程")

    def test_attack_range_comes_from_the_payload(self):
        """射程取自记录（payload 的 `attackRange`），**不是代码里的常数**。

        同一个目标：射程 4 够不着、射程 7 够得着 —— 任务书 §4.5.1 的表格写 3/6/10，
        与样例 payload 的 4/7/INT_MAX 矛盾，**以 payload 为准**。
        """
        far = Robot(Pos(17, 30), 40)  # 切比雪夫 5
        self.assertEqual(plan(self._manned(far, reach=4)), {})
        self.assertEqual(self._only_cmd(self._manned(far, reach=7))["targetPos"], [{"x": 17, "y": 30}])

    def test_a_cooling_rocket_holds_fire(self):
        """火箭发射台发射后有 3 回合空窗（`cooldown`）⇒ 冷却中**一炮不发**，
        而且**不换炮**（换炮会让角色在炮位之间周期性来回走）。"""
        robot = Robot(Pos(12, 26), 40)
        self.assertEqual(plan(self._manned(robot, cooldown=2)), {})
        self.assertEqual(self._only_cmd(self._manned(robot, cooldown=0))["action"], "attack")

    def test_a_missing_cooldown_means_no_cooldown(self):
        """`cooldown` 缺失时 `model` 给 -1 ⇒ **不算冷却中**。

        样例三座炮都没有这个字段 —— 把缺省值写成"冷却中"的话，火箭整晚一炮不开，
        而日志上什么都看不出来。
        """
        self.assertEqual(self._only_cmd(self._manned(Robot(Pos(12, 26), 40), cooldown=-1))["action"], "attack")

    def test_a_dead_robot_is_not_worth_a_shot(self):
        """`health == 0` 的机器人（尸体）不占目标 —— 打空处白耗一次冷却。"""
        self.assertEqual(plan(self._manned(Robot(Pos(12, 26), 0))), {})

    def test_no_robot_in_range_means_hold_fire(self):
        """用户选定：机器人还没走进射程时**站在炮位待命**（不发指令，空指令合法）。"""
        self.assertEqual(plan(self._manned()), {}, "没敌人 ⇒ 什么都不发")
        self.assertEqual(
            plan(self._manned(Robot(Pos(12, 31), 40))), {}, "敌人还在射程外 ⇒ 也不发"
        )

    def test_missing_round_no_never_fires(self):
        """`roundNo` 缺失 ⇒ `within = 129` ⇒ `is_day` 判成**夜里**。

        那个降级方向对"白天不许建造"是安全的，对 `attack` 就反了：**白天开火是非法的**。
        所以 `round_no < 0` 时一条都不发 —— 代价只是这一个回合不动。
        """
        turn = self._manned(Robot(Pos(12, 26), 40), round_no=-1)
        self.assertEqual(plan(turn), {})


class PathTest(unittest.TestCase):
    """寻路：BFS 最短路。**这两个局面上贪心版都会挂** —— 换掉它的理由就在这。"""

    #: 合成局面里也得摆个基地，否则 `_ring` 是空的、工人压根不动（矿只服务于砌墙）
    BASE = Pos(20, 20)

    def _walk(self, turn: Turn, limit: int) -> tuple[int, Turn]:
        """把回合串起来走，返回 `(实际走了几步, 走完的局面)`。

        **发 `collect` 就算走到了** —— 那正是"贴着矿"的唯一标志。
        """
        for moves in range(limit + 1):
            cmds = plan(turn)
            if not cmds:
                return moves, turn
            cmd = cmds["1"]
            if cmd["action"] == "collect":
                return moves, turn
            t = cmd["targetPos"][0]
            turn = turn._replace(roles=(Worker(1, Pos(t["x"], t["y"])),))
        self.fail(f"{limit} 回合还没走到，说明在原地绕圈或卡死")

    def test_walks_around_a_wall(self):
        """一堵横墙把直路封死，只能绕到墙的右端过去。

        贪心会在 `(4,4)` 停死：那里**没有任何一格更近**（更近的三格全在 y=3 的墙上），
        而它只会挑"确实更近"的格子。BFS 绕得过。
        """
        wall = {Pos(x, 3) for x in range(8)}  # x=0..7 一整行
        mine = Pos(5, 1)
        turn = Turn(
            round_no=1,
            map=Map((41, 32), {self.BASE: "station", mine: "stone", **{p: "wall" for p in wall}}),
            roles=(Worker(1, Pos(5, 5)),),
            gold=0,
        )

        # 最短路 5 步：(5,5)→(6,4)→(7,4)→(8,3)[绕过墙]→(7,2)→(6,2)，(6,2) 距矿 1
        moves, final = self._walk(turn, 20)
        self.assertEqual(moves, 5, "步数不等于最短路 ⇒ 找的不是最短路")
        self.assertEqual(final.roles[0].pos.dist(mine), 1, "绕过去了但没停在矿边")

    def test_never_leaves_the_map(self):
        """地图边界**不在任务书 L85 的阻挡清单里**，得自己挡。

        整列 `x=1` 封死，工人被关在 `x=0` 这一列；能到达的格子没有一个贴着目标。
        **去掉边界检查这里会返回 `(-1,4)`** —— 一条走出地图的指令。
        """
        goal = Pos(0, 1)
        blocked = {Pos(1, y) for y in range(10)}  # 整列封死
        blocked |= {Pos(0, y) for y in (2, 3, 4)}  # 再堵掉本列的直路
        blocked.add(goal)

        self.assertIsNone(step_toward(Pos(0, 5), goal, frozenset(blocked), (10, 10)))


class TaskParseTest(unittest.TestCase):
    """`teamOur.playerTasks` → `Turn.task_points`，以及顶层的 `phaseTask` / `llmResp`。

    **`playerTasks` 是任务点的权威来源**：它只含**我方**那 2 个点（阵营已按 `teamOur.type`
    滤好），所以不必去 `mapInfo.zones` 里认 `challengerTaskPoint*`，也不必读 `teamOur.type`。
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
        """逐字读原始样例：两个点，坐标取的是 **`taskPosition`**（不是 `pos`）。"""
        turn = model.load(json.loads(SAMPLE.read_text(encoding="utf-8")))
        self.assertEqual(turn.task_points, (Pos(14, 14), Pos(17, 17)))
        self.assertEqual(turn.phase_task, "", "样例里没接任务")

    def test_a_cooling_task_point_is_not_offered(self):
        """冷却中的点接不了（接口文档 L136：`coldDownRounds` = 还有多少回合就绪）。"""
        self.assertEqual(self._load({**self.POINT, "coldDownRounds": 30}).task_points, ())

    def test_an_invalid_task_point_is_not_offered(self):
        """`isValid` 为 false = 冷却中**或**这个点的任务已做完（接口文档 L139）。"""
        self.assertEqual(self._load({**self.POINT, "isValid": False}).task_points, ())

    def test_a_missing_cold_down_rounds_still_offers_the_point(self):
        """缺字段的降级方向**故意不是“少做”**（与 `_gold` / `_size` / `_stone` 相反）。

        误接一个冷却中的点只是**指令执行失败**（任务书 L508，不计异常）；
        误判成"永远接不了"却会让整条任务线**静默作废**。两害相权取前者。
        """
        point = {k: v for k, v in self.POINT.items() if k != "coldDownRounds"}
        self.assertEqual(self._load(point).task_points, (Pos(14, 14),))

    def test_a_missing_is_valid_still_offers_the_point(self):
        """同上：只有**明确**的 `false` 才算接不了。"""
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

        **不解析那行状态**（`[exitCode:N]` / `[TIMEOUT]` / `[JUDGER_ERROR]`）：
        非空即原文回灌，让 LLM 自己读 —— 解析它就是又多一份会跟判题器漂移的真相。
        字段缺失 ⇒ 空串（文档 L33："未发命令时为空字符串"）。
        """
        turn = self._load(lastCmdResult="[exitCode:0]\n晴 26 度")
        self.assertEqual(turn.cmd_result, "[exitCode:0]\n晴 26 度")
        self.assertEqual(self._load(lastCmdResult=None).cmd_result, "")


class JudgeReceiptTest(unittest.TestCase):
    """顶层 `errors` / `lastRoundRoleActionResults` → `Turn.errors` / `Turn.action_results`。

    **这两个字段是"任务为什么一直失败"唯一的答案来源**，而第 14 步之前它们
    一个都没被读进来过 —— 判题器的判决从来没进过日志。
    """

    def _load(self, **top) -> Turn:
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw.update(top)
        turn = model.load(raw)
        self.assertIsNotNone(turn)
        return turn

    def test_errors_come_from_the_payload(self):
        """逐字读原始样例：`[{"errorCode": 2, "description": "xxx"}]`。

        ⚠️ **样例那条是假错误**（`CLAUDE.md`：`request.txt` 是手工示意数据）——
        这里钉的是**解析路径通不通**，不是"真的一直在报答案错误"。
        """
        self.assertEqual(self._load().errors, (Error(code=2, description="xxx"),))

    def test_a_missing_description_still_keeps_the_code(self):
        """码是读日志时的第一眼信息 —— 缺 `description` 不该把整条丢掉。"""
        turn = self._load(errors=[{"errorCode": 5}])
        self.assertEqual(turn.errors, (Error(code=5, description=""),))

    def test_an_error_without_a_code_is_dropped(self):
        """降级方向**不是"少做"**：一条 `-1：xxx` 会被当成"未知错误 0"去查一个不存在的问题，
        比不打印更糟。同理 `errors` 本身缺失 ⇒ 空元组（"本轮没报错"是天然的安全值）。"""
        self.assertEqual(self._load(errors=[{"description": "x"}, "junk"]).errors, ())
        self.assertEqual(self._load(errors="boom").errors, ())
        self.assertEqual(self._load(errors=None).errors, ())

    def test_action_results_keep_both_verdicts(self):
        """**两种判决都要留**（`false` 才是信号，`true` 是它的参照）—— 只留 `false` 的话，
        "判题器压根没提这个单位" 与 "这个单位通过了" 就分不出来了。"""
        turn = self._load(lastRoundRoleActionResults={"10011": True, "10010": False})
        self.assertEqual(dict(turn.action_results), {10011: True, 10010: False})

    def test_action_results_only_trust_real_booleans(self):
        """JSON 里的 `"false"` 是个**非空字符串** ⇒ `bool("false") == True`。

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


class TaskAcceptTest(unittest.TestCase):
    """白天：开拓者走到最近一个**能接**的任务点旁边，贴着就 `acceptTask`。

    与 `build` / `collect` 同一条契约 —— 任务点挡路（任务书 L85），`step_toward`
    天然停在贴着它的那一格，而那正是"周围一格内"（§4.6.2）：不用先挪开再领。
    """

    DAY = 1
    P1, P2 = Pos(14, 14), Pos(17, 17)

    def _turn(self, *points: Pos, pioneer: Pos = Pos(10, 10)) -> Turn:
        return Turn(
            round_no=self.DAY,
            map=Map((41, 32), {p: "challengerTaskPoint1" for p in points}),
            roles=(Pioneer(10011, pioneer),),
            gold=0,
            task_points=points,
        )

    def test_the_pioneer_walks_to_the_nearest_task_point(self):
        cmds = plan(self._turn(self.P1, self.P2))
        self.assertEqual(set(cmds), {"10011"})
        self.assertEqual(cmds["10011"]["action"], "move")
        point = cmds["10011"]["targetPos"][0]
        self.assertEqual((point["x"], point["y"]), (11, 11), "朝最近的 (14,14) 迈一步")

    def test_the_pioneer_accepts_next_to_the_point(self):
        """贴着任务点 ⇒ `acceptTask`，**报文里没有 `targetPos`**
        （逐字对 `docs/response.txt` L51：`{"action":"acceptTask"}`）。"""
        cmds = plan(self._turn(self.P1, pioneer=Pos(13, 13)))
        self.assertEqual(cmds, {"10011": {"action": "acceptTask"}})

    def test_a_pioneer_two_cells_away_keeps_walking(self):
        """"周围一格"是**切比雪夫 1**（8 邻格、含对角）—— 差一格都不算贴着。"""
        cmds = plan(self._turn(self.P1, pioneer=Pos(12, 12)))
        self.assertEqual(cmds["10011"]["action"], "move")

    def test_no_ready_point_means_no_command(self):
        """一个能接的点都没有 ⇒ 待命（空指令合法且不计异常）。

        **不走去冷却中的点蹲守**：白天走过去、夜里回炮位、第二天再走过去 ——
        第 8 步 `_ring` 那个"两格之间对着改目标转到天黑"是同一类坑。
        """
        self.assertEqual(plan(self._turn()), {})

    def test_distance_ties_break_by_position(self):
        """并列时的先后**不能取决于 payload 里的顺序**，否则用例复现不了
        （与 `_defend` 的 `(dist, id)` 同一条理由）。"""
        left, right = Pos(18, 20), Pos(22, 20)
        cmds = plan(self._turn(right, left, pioneer=Pos(20, 20)))
        point = cmds["10011"]["targetPos"][0]
        step = Pos(point["x"], point["y"])
        self.assertLess(step.dist(left), step.dist(right), "同距离该挑坐标小的那个")


class TaskHoldTest(unittest.TestCase):
    """开拓者一旦领到任务就被**钉死**（任务书 L379：离开任务点周围一格内任务立即作废）。

    这是本步最危险的一条 —— 钉不住的话它会照旧走向炮位，任务当场作废，
    而**本地全绿**：报文挑不出毛病、"行为也正常"，只是任务一次都没做完。
    """

    DAY, NIGHT = 1, 85
    GUN = Pos(12, 25)
    REACH = 4  # 加特林 L1 的射程，**取自样例 payload**（任务书表格写的是 3）
    TASK = "请查询北京天气"

    def _turn(self, role: BaseRole, round_no: int, phase_task: str = "", **kw) -> Turn:
        guns = (Weapon(id=10020, kind="gatling", pos=self.GUN, attack_range=self.REACH, cooldown=0),)
        return Turn(
            round_no=round_no,
            map=Map((41, 32), _terrain(guns, {Pos(20, 20): "station"})),
            roles=(role,),
            gold=0,
            weapons=guns,
            phase_task=phase_task,
            task_points=(Pos(30, 30),),
            **kw,
        )

    def test_a_pinned_pioneer_does_not_move_in_the_day(self):
        """`phaseTask` 非空时它一步都不走 —— 哪怕任务点就在旁边（走开一格就作废）。"""
        turn = self._turn(Pioneer(10011, Pos(30, 29)), self.DAY, self.TASK)
        self.assertEqual(plan(turn), {})

    def test_a_pinned_pioneer_does_not_man_a_weapon_at_night(self):
        """**夜里也不回炮位**（这一条是本步最危险的）。

        开拓者已经贴着炮、射程内还有敌人 —— 不钉住的话它会开炮，而开炮要"走过去"，
        任务当场作废。`phaseTask` 一空（下面那条）同样的局面就必须开炮，两相对照。
        """
        turn = self._turn(
            Pioneer(10011, Pos(12, 24)), self.NIGHT, self.TASK, robots=(Robot(Pos(12, 27), 40),)
        )
        self.assertEqual(plan(turn), {})

    def test_a_free_pioneer_still_mans_a_weapon_at_night(self):
        """第 10 步的回归："所有角色都操炮"不能被任务线吃掉。"""
        turn = self._turn(
            Pioneer(10011, Pos(12, 24)), self.NIGHT, "", robots=(Robot(Pos(12, 27), 40),)
        )
        cmds = plan(turn)
        self.assertEqual(
            cmds,
            {"10020": {"action": "attack", "controllerId": "10011", "targetPos": [{"x": 12, "y": 27}]}},
        )

    def test_the_workers_are_not_pinned_by_the_pioneers_task(self):
        """钉的是开拓者一个人 —— 别连坐（工人该开炮照开）。"""
        turn = self._turn(Pioneer(10011, Pos(40, 40)), self.NIGHT, self.TASK)
        turn = turn._replace(
            roles=(Pioneer(10011, Pos(40, 40)), Worker(1, Pos(12, 24))),
            robots=(Robot(Pos(12, 27), 40),),
        )
        cmds = plan(turn)
        self.assertEqual(list(cmds), ["10020"], "只有工人那一炮")
        self.assertEqual(cmds["10020"]["controllerId"], "1")


class TaskChannelTest(unittest.TestCase):
    """任务线的对外通道：`task_channel` 的五条判据 + `submitAnswer` 那条独立的线。

    **两条通道是独立的**：`task_channel` 产出响应顶层的 `prompt` / `executeCmd`
    （跟判题器的 LLM 与它的沙盒打交道），`plan` 里的 `_answer_task` 产出 `submitAnswer`
    （跟判分打交道）—— 各自判各自的，不共享判据。所以"这一轮在提问"与"这一轮在提交"
    **可以同时成立**，那是有利的（接口文档 L140 取"通过率最高"，重交零成本）。
    """

    DAY = 1
    TASK = "请查询北京天气"
    ANSWER = "晴 26 度"
    #: 回灌那两段的**分界符**，用来断言"该出现 / 不该出现"。
    #: 拿"分界符"而不是"某句话"当判据：模板正文里也有一句"沙盒的执行结果原文"，
    #: 用普通词当判据会把自己绊倒；而 `【` 整个模板里一个都没有，**只属于注入的两段**
    #: —— 沙盒输出与 LLM 回复都是任意文本，没有分界符档着就分不清哪段是题目。
    RESULT_MARK = "【上一条命令的执行结果"
    RETRY_MARK = "【你上一次提交的答案"

    def _turn(
        self,
        phase_task: str = "",
        llm_resp: str = "",
        cmd_result: str = "",
        errors: tuple[Error, ...] = (),
        roles: tuple[BaseRole, ...] = (Pioneer(10011, Pos(13, 13)),),
    ) -> Turn:
        return Turn(
            round_no=self.DAY,
            map=Map((41, 32), {Pos(14, 14): "challengerTaskPoint1"}),
            roles=roles,
            gold=0,
            phase_task=phase_task,
            llm_resp=llm_resp,
            cmd_result=cmd_result,
            errors=errors,
            task_points=(Pos(14, 14),),
        )

    def test_the_question_carries_the_task_text(self):
        """第一次提问 = **任务原文 + 怎么要命令 + 什么时候直接作答**，不带任何回灌。

        断言用 `assertNotIn` 而不是"等于 `TASK_PROMPT.format(...)`"：后者是同义反复
        （模板与断言一起改，永远过得去），而"第一次问不该有任何回灌"才是真要求。
        """
        prompt, execute = task_channel(self._turn(self.TASK))
        self.assertIn(self.TASK, prompt)
        self.assertEqual(execute, "")
        self.assertNotIn(self.RESULT_MARK, prompt)
        self.assertNotIn(self.RETRY_MARK, prompt)

    def test_nothing_is_sent_without_a_task(self):
        """**不在任务里一次都不发**（`prompt` 也不行、`executeCmd` 更不行）。

        `prompt`：每游戏日只有 3 次 LLM 额度（接口文档 L198），那是任务线之外的资源。
        `executeCmd`：接口文档 L208 写明沙盒"**仅在执行任务期间才能使用**"。
        """
        for llm_resp in ("", self.ANSWER, "<tool>rm -rf /</tool>"):
            with self.subTest(llm_resp=llm_resp):
                self.assertEqual(
                    task_channel(self._turn(llm_resp=llm_resp)), ("", ""),
                )

    def test_a_dead_pioneer_never_touches_the_sandbox(self):
        """**开拓者阵亡 ⇒ 两个通道都停**，哪怕 `phaseTask` 还没清干净。

        这条闸门是**显式补的**，理由是一个对称性缺口：`submitAnswer` 走 `roleCommandMap`，
        而阵亡的人根本不在 `model._character` 给出的 `roles` 里 ⇒ `_answer_task`
        **天生**就进不来；但 `executeCmd` 是**响应顶层字段**，不经过角色循环、也不经过
        `Action` 的权限闸门 ⇒ 那条白送的闸门对它**完全不存在**。
        同一件事在一条通道上有闸门、在另一条上没有，迟早出事。
        """
        self.assertEqual(
            task_channel(self._turn(self.TASK, "<tool>ls</tool>", roles=())), ("", ""),
        )

    def test_no_question_once_the_llm_answered(self):
        """回复是答案 ⇒ 不再提问（同一个 prompt 问两遍不会得到更好的答案）。"""
        self.assertEqual(task_channel(self._turn(self.TASK, self.ANSWER)), ("", ""))

    def test_the_command_comes_out_of_the_tool_markup(self):
        """`<tool>…</tool>` 里的东西**就是那条命令**，两侧空白去掉、内部原样保留。

        多标签只取第一条：`executeCmd` 只有一个字段，一回合只跑得了一条（接口文档 L210）。
        """
        cases = {
            "<tool>ls -la</tool>": "ls -la",
            "  <tool>  ls -la  </tool>  ": "ls -la",
            "<tool>python -c \"print(1)\"\nprint(2)</tool>": 'python -c "print(1)"\nprint(2)',
            "<tool>first</tool> 然后 <tool>second</tool>": "first",
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply)), ("", expected))

    def test_a_broken_tool_tag_yields_no_command(self):
        """凑不齐的标签 ⇒ **没有命令可发**。别把半截标签当命令丢进沙盒。"""
        for reply in ("<tool ls", "<tool>ls", "</tool>", "<tool></tool>", "<tool>  </tool>"):
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply))[1], "")

    def test_a_broken_tool_reply_is_asked_again(self):
        """⚠️ **半截工具调用要落回"重问"，不能两边都哑火。**

        `<tool` 有开无闭时取不出命令 ⇒ 判据 3 不命中；若再按"取不出命令 = 这是答案"
        落到判据 5，就会 `("", "")` —— 而 `_answer_task` 又跳过工具回复，
        于是**提问与提交同时哑火**。`llm_resp` 若粘住，下一回合还是同一条畸形回复、
        **永久空转，且日志上什么都看不出来**（"提问：无 ｜ 提交：有"看着完全正常）。
        """
        prompt, execute = task_channel(self._turn(self.TASK, "<tool ls -la"))
        self.assertEqual(execute, "")
        self.assertIn(self.TASK, prompt)

    def test_the_sandbox_result_goes_back_verbatim(self):
        """沙盒输出**全文**回灌（用户拍板不截断），而且这一轮**绝不发命令**。"""
        output = "[exitCode:0]\n" + "y" * 5000
        prompt, execute = task_channel(self._turn(self.TASK, cmd_result=output))
        self.assertEqual(execute, "")
        self.assertIn(output, prompt)

    def test_a_result_already_in_hand_blocks_the_next_command(self):
        """⚠️ **沙盒刚交作业这一轮，绝不能再发命令** —— 判据 2 必须压在判据 3 前面。

        动机是 `llmResp` **可能粘住**：文档给 `lastCmdResult` 写了"未发命令时为空字符串"
        （L33）、对 `llmResp` **一个字没写**（L31）。万一它还停在上轮那条 `<tool>…</tool>`
        上，判据 3 先命中就会**同一条命令反复丢进沙盒**。附带挡住"结果延迟两回合"。
        """
        prompt, execute = task_channel(
            self._turn(self.TASK, llm_resp="<tool>ls</tool>", cmd_result="[exitCode:0]\nok")
        )
        self.assertEqual(execute, "", "沙盒刚交作业，这轮不许再发命令")
        self.assertIn("[exitCode:0]\nok", prompt)

    def test_a_result_and_a_rejection_come_back_together(self):
        """⚠️ **沙盒结果与"答错了"是同一个分支的两面，不能互相吞掉。**

        `errors` 说的是**本轮产生**的错误，与我们上轮发了什么并不同步：第 R 轮发命令
        （那轮没有 `prompt`）⇒ 第 R+1 轮 `cmd_result` 非空，而 `errors` 里的 `code 2`
        说的是**更早**那次 `submitAnswer` 的判决 —— 两者同时命中是**真实可达的**。
        把纠错做成独立分支就会把它整段吞掉，所以它是**修饰符**。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                llm_resp=self.ANSWER,
                cmd_result="[exitCode:0]\n晴",
                errors=(Error(code=2, description="答案不正确"),),
            )
        )
        self.assertEqual(execute, "")
        self.assertIn("[exitCode:0]\n晴", prompt)
        self.assertIn(self.ANSWER, prompt)
        self.assertIn(self.RETRY_MARK, prompt)

    def test_a_rejection_needs_an_answer_to_blame(self):
        """带纠错那一段的**前提是"手上真有一个被否掉的答案"**，两个反例都要挡住。

        - `llm_resp` 为空：任务刚换（上一条超时结束、开拓者立刻接了新任务）时
          `errors` 里那个 2 是**旧账** —— 拿它去骂新任务，只会把 LLM 带偏。
        - `llm_resp` 是工具调用：否则会塞进"你上一次的答案是 `<tool>ls</tool>` 被判错了"。
          **两种工具回复都要挡**：取得出命令的（`<tool>ls</tool>`）在判据 3 就走了，
          取不出的（`<tool ls`）会落到判据 4 —— 后者才是这条判据真正的守门员。
        """
        nobody_to_blame = task_channel(self._turn(self.TASK, errors=(Error(2, "x"),)))
        self.assertNotIn(self.RETRY_MARK, nobody_to_blame[0])

        replied_a_command = task_channel(
            self._turn(self.TASK, llm_resp="<tool>ls</tool>", errors=(Error(2, "x"),))
        )
        self.assertNotIn(self.RETRY_MARK, replied_a_command[0])
        # 顺带钉住：有 error 2 也不该妨碍"该跑的命令照跑"（判据 3 排在判据 4 前面）
        self.assertEqual(replied_a_command[1], "ls")

        broken = task_channel(
            self._turn(self.TASK, llm_resp="<tool ls", errors=(Error(2, "x"),))
        )
        self.assertNotIn(self.RETRY_MARK, broken[0], "半条命令不是'上次交的答案'")

    def test_only_the_answer_error_triggers_the_retry(self):
        """只有 `code 2`（答案不正确）才重问。1 与 5 是终局、3/4 重问也救不回来。"""
        for code in (0, 1, 3, 4, 5):
            with self.subTest(code=code):
                prompt, execute = task_channel(
                    self._turn(self.TASK, llm_resp=self.ANSWER, errors=(Error(code, "x"),))
                )
                self.assertEqual((prompt, execute), ("", ""))

    def test_a_timeout_result_still_goes_back(self):
        """`[TIMEOUT]` / `[JUDGER_ERROR]` 打头的回执**照样原样回灌** —— 那是沙盒侧的
        失败，让 LLM 自己看着重试（自进化任务的主要恢复路径）。

        我们不解析那行状态：解析它就是又多一份会跟判题器漂移的真相。
        """
        for output in ("[TIMEOUT]\n部分输出", "[JUDGER_ERROR]\n沙盒挂了", "[exitCode:127]\nno"):
            with self.subTest(output=output):
                prompt, execute = task_channel(self._turn(self.TASK, cmd_result=output))
                self.assertIn(output, prompt)
                self.assertEqual(execute, "")

    def test_prompt_and_command_are_never_both_set(self):
        """**两条通道互斥** —— 这是整个状态机唯一的不变量，遍历所有分支钉一遍。

        "同一轮既提问又发命令"会让 LLM 在没看到结果的情况下作答 ⇒ 又要一遍同一条命令
        ⇒ 活锁。它也是 `task_channel` 之所以合成一个函数、而不是 `prompt_for` +
        `execute_for` 的全部理由（拆开就要把这条链写两遍）。
        """
        turns = [
            self._turn(),
            self._turn(self.TASK),
            self._turn(self.TASK, self.ANSWER),
            self._turn(self.TASK, "<tool>ls</tool>"),
            self._turn(self.TASK, "<tool ls", cmd_result="[exitCode:0]\nok"),
            self._turn(self.TASK, cmd_result="[TIMEOUT]\n…"),
            self._turn(self.TASK, self.ANSWER, errors=(Error(2, "x"),)),
        ]
        for i, turn in enumerate(turns):
            with self.subTest(i=i):
                prompt, execute = task_channel(turn)
                self.assertFalse(prompt and execute, (prompt, execute))

    def test_the_answer_is_submitted_verbatim(self):
        """答案**原文进、原文出**（逐字对 `docs/response.txt` L54）：
        我们不知道判题器要什么格式，加工只会引入自己的假设。"""
        cmds = plan(self._turn(self.TASK, self.ANSWER))
        self.assertEqual(cmds, {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}})

    def test_a_blank_answer_is_not_submitted(self):
        """空答案不发 —— 那可能被判成"字段缺失"，正是红线里的"指令非法"。
        （顺带钉住 `.strip()`：只有空白也必须当成空。）"""
        self.assertEqual(plan(self._turn(self.TASK, "   ")), {})

    def test_a_tool_call_is_never_submitted_as_an_answer(self):
        """⚠️ **没取到命令的回复也绝不能当答案交上去。**

        `_answer_task` 与 `task_channel` 判据 5 用的是**同一个谓词**
        （`_is_tool_reply`）—— 一边当命令、一边当答案就是第二份真相。
        交上去的话，`<tool>ls</tool>` 会被判题器当成一次错误答案（`errorCode 2`）。
        """
        for reply in ("<tool>ls</tool>", "<tool>ls</tool>\n记住这个", "<tool ls"):
            with self.subTest(reply=reply):
                self.assertEqual(plan(self._turn(self.TASK, reply)), {})

    def test_the_answer_is_resubmitted_every_round(self):
        """**同一份 payload 连打几次都要交** —— 这不是 bug，是无状态设计的正面确认。

        接口文档 L140：「以之前提交过的**通过率最高的**答案计算积分与金币」——
        判题器专门为"反复交、取最好"设计了这个字段。⚠️ 别把它"优化"成"只交一次"：
        那要记住交没交过，而**卡住的状态会静默关掉整条任务线**。
        """
        turn = self._turn(self.TASK, self.ANSWER)
        for _ in range(3):
            self.assertEqual(
                plan(turn), {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}}
            )


if __name__ == "__main__":
    unittest.main()
