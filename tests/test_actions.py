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
from collections.abc import Iterable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from coregeek.agent import AGENT, Agent  # noqa: E402
from coregeek.agent.chat import PROMPT, answer_of, looks_like_tool, tool_of  # noqa: E402
from coregeek.agent.context import Context  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.app import LOG_PROMPT_MAX, _clip, handle  # noqa: E402
from coregeek.utils import LOG_TEXT_MAX  # noqa: E402
from coregeek.game.grid import (  # noqa: E402
    Pos,
    STEPS,
    base_cells,
    box_cells,
    step_outside,
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
from coregeek.utils import LOG_TEXT_MAX  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "docs" / "request.txt"

#: 官方样例（roundNo=85）的落点。**样例是夜里**，所以三个角色都朝最近的一座炮走一格：
#: 10010 在 (5,23) → (9,24)（切比雪夫 4）；10012 在 (10,16) → (9,24) 已被认领 ⇒ (10,25)（9）；
#: 10011 在 (10,12) → 前两座都被认领 ⇒ (9,25)（13）。夜里 `build`/`collect` 一条都不该有。
#: ⚠️ 这条断言**按设计改过两次**：第 7 步夜里是"朝石矿走"，第 8 步改成"回基地操炮"
#: （当时开拓者还不发指令），**第 10 步起开拓者也上炮位** ⇒ 两条变三条。
EXPECTED_MOVES = {"10010": [6, 22], "10012": [9, 17], "10011": [9, 13]}

#: 任务线那两条日志的**行首标记**。它们由 `planner` 打（不是 `coregeek.app`）——
#: 用例按标记取行，既不依赖日志顺序，也不依赖"哪张日志归哪个 logger"。
TASK_LINE = "【本轮任务】："
ASK = "【本轮提问】："
SANDBOX = "【CMD命令执行结果】："


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


def _blocks(summary: str) -> list[str]:
    """`Turn.summary()` → **非空**行。

    摘要第 22 步起带**空行分段**（`【回合】 / 【我方】 / 【机器】 / 【可接任务点】` 各占一块，
    块间空一行），块数**固定**但物理行数是数据相关的（一块里放不下会换行）。
    断言块内容就够 —— 空行只是给人眼看的，钉住它反而会把"某块变长了"误报成格式错。
    """
    return [line for line in summary.splitlines() if line]


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

    def test_sell_wire_shape(self):
        """`sell` 是**第一个带 `num` 的动作** —— 逐字段钉死，别让 `str` 漏进 JSON。

        接口文档 §2.2 标的是 **Int**（不填默认 1），§2.3 给的形状是
        `{"action":"sell","name":<矿种>,"num":<件数>}`。`name` 取**矿种**而不是动作名，
        与 `build` 的 `name` 是建筑名同一个约定（与载荷里 `neutralType` /
        `vendorShopList.name` / 背包物品名**同一套词**）。

        ⚠️ **没有实证报文**：`docs/response.txt` 里只有 `move`/`build`/`remove`。
        形状是从接口文档推的 —— 这是这一步最大的单点风险，也只有这里能钉。
        `attack` 的 `controllerId` 标的是 String、这里标的是 Int，**两者相反**，
        所以两处各钉一次类型（抄错方向 = 一次"指令非法"，直通红线）。
        """
        wire = actions.Sell("worker", "copper", 4).to_wire()
        self.assertEqual(wire, {"action": "sell", "name": "copper", "num": 4})
        self.assertIsInstance(wire["num"], int)
        self.assertNotIsInstance(wire["num"], bool)


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

    def test_sell_is_allowed_for_both_roles(self):
        """§4.4 里 `sell` 的可用角色是**全部**（开拓者一样能卖），与 `build`/`collect` **相反**。

        反方向的那种 bug（把开拓者/别的角色挡在门外）本地**永远测不出来** ——
        报文格式完全合法，只有判题器会说"不"。所以这一条与上面那两条成对：
        `build`/`collect` 钉"窄得对"，这里钉"窄不得"。
        """
        for role_type in ("worker", "pioneer"):
            with self.subTest(role_type=role_type):
                self.assertEqual(
                    actions.Sell(role_type, "stone", 1).to_wire()["action"], "sell"
                )


class HandleTest(unittest.TestCase):
    """端到端：`app.handle` 是红线所在，改坏了要立刻知道。"""

    def setUp(self) -> None:
        #: SOP 是**单实例上的跨回合状态**（全项目唯一一处），不清就会跨用例串味：
        #: 前一条用例存进去的 SOP 会出现在后一条的 prompt 里。
        AGENT.reset()

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

    def test_every_round_logs_the_summary_then_the_actions(self):
        """每回合的复盘日志：**先局面（摘要）、再动作、再判题器的回执**（顺序是重点）。

        判题器是黑盒、只给我们这一个视角，出事故时得能看见当时的局面 ——
        只看见一条 `move` 是没法回答"为什么走了这一格"的。
        `assertLogs` 拦到的正是 `main3.py` 重定向到 stdout 的那几条。

        **第 26 步起局面 = 摘要单条**（用户手改：**图例与整张地图退出日志** —— 用 ~2.7KB/回合
        的观察面换 64KB 管道风险，拍板接受）。这里顺带钉住"地图确实不在了"。

        ⚠️ **样例自带一条假错误与两条假未通过**（`request.txt` 的 `errors` 是
        `[{"errorCode": 2, "description": "xxx"}]`、`lastRoundRoleActionResults` 里
        10010/10030 是 false）。`CLAUDE.md` 已声明**别把样例的这两个值当真实信号读**，
        但**这里的记录条数是真实断言**：回执那两条各自"有事才吭声"，
        所以样例这种局面是 **5** 条（**banner** + 摘要 + 动作 + 报错 + 回执），
        而一个干净回合只有 3 条（下面那条用例）。
        """
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(SAMPLE.read_bytes())
        #: 五条：**banner** + 摘要 + 动作 + 报错 + 回执。banner 是 `handle` 打的、
        #: 不归 `_log` 管 —— 数记录数时最容易漏的就是它。
        banner, head, acts, errors, failed = (r.getMessage() for r in caught.records)
        self.assertEqual(banner, f"{'#' * 35}第85回合{'#' * 35}")
        lines = head.splitlines()
        #: 摘要 4 块（各占一行）+ 摘要头前面留给 `logging` 前缀的那个空行
        self.assertEqual(len(lines), 1 + 4)
        #: 回合号在最前 —— 时间戳就加在这一行上（摘要头一块前面那个空行不带时间戳）
        self.assertEqual(lines[1].split("｜")[0].rstrip(), "【回合】 85（夜里）")
        self.assertIn("【金币】 20", lines[1])
        #: 地图与图例第 26 步退出日志：图例那行、以及地图那圈 `—` 边框都不该再出现
        self.assertNotIn("【图例】：", head)
        self.assertNotIn("—" * 43, head, "地图的标尺行不该再出现")
        self.assertEqual(
            acts, "【动作】：10010 move (6,22)；10012 move (9,17)；10011 move (9,13)"
        )
        self.assertEqual(errors, "【判题器报错】：2：xxx")
        #: **按 id 排序**（不照 payload 的顺序）：`{10010: false, 10030: false}` 在样例里
        #: 恰好就是升序，靠样例**测不出**这一条 —— 所以下面那条解析用例专门打乱一次顺序。
        #: 样例那份回执里有 7 个实体 —— 现在**全都打**（第 20 步），不再只列未通过的两个
        self.assertEqual(
            failed,
            "【上回合合法性】：10010=False | 10011=True | 10012=True | 10013=True"
            " | 10020=True | 10030=False | 10040=True",
        )

    def test_a_clean_round_logs_only_the_summary_and_the_actions(self):
        """回执那两条**有事才吭声** —— 干净回合一条都不该多打（日志字节是有预算的）。

        与上面那条用例合起来才钉得住"触发条件"：只测样例的话，全打也算过。

        **banner 是那第三条**（`handle` 打的，不归 `_log` 管）：它每回合都出现，
        是这个计数里唯一"不该省"的一条 —— 分段符省了，几 MB 的日志就没法按回合切。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        self.assertEqual(
            len(caught.records), 3, [r.getMessage()[:40] for r in caught.records]
        )
        self.assertTrue(caught.records[0].getMessage().startswith("###"), "banner 该在最前")

    def test_the_receipt_line_lists_every_entity_sorted(self):
        """回执那一行**列全部实体、按 id 升序**（含 `True` 的那些）。

        两件事分别钉住：

        - **全都打**（第 20 步）：判题器只回它**收到**的那几条，所以"这条压根没发指令"
          与"发了但没过"在只列未通过名单时长得一模一样，而下一步该怎么做完全相反。
          `10011=True` 必须出现在行里。
        - **升序**：样例那份恰好就是升序，上面那条用例**测不出**这一点 —— 顺序一旦随
          payload 走，同一种局面会打出两种日志，翻日志时对不上号（`_defend` 里
          "并列按 id 排"是同一条理由：先后不能取决于报文给的顺序）。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {"10030": False, "10011": True, "10010": False}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        self.assertEqual(
            caught.records[-1].getMessage(),
            "【上回合合法性】：10010=False | 10011=True | 10030=False",
        )
        #: **全通过也照样打**：触发条件是"有回执"，不是"有未通过" ——
        #: 一行全 `True` 正是"这回合发出去的都合法"的唯一证据（第 20 步的触发条件就在这里）。
        raw["lastRoundRoleActionResults"] = {"10010": True, "10011": True}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        self.assertEqual(
            caught.records[-1].getMessage(), "【上回合合法性】：10010=True | 10011=True"
        )

    def test_the_task_line_shows_the_whole_text_and_marks_any_truncation(self):
        """任务日志**必须能看见全文** —— 这正是第 14 步的来由。

        原来那版是 `phase_task[:120]` 的**静默**截断：任务一长，日志里就是一段没头没尾的
        文字，看不出后面还有没有内容，于是"任务一直失败"根本无从查起。
        现在：短文本原样打全；超长时截到 `LOG_TEXT_MAX` 并**明说被截了、原文共多少字**。

        ⚠️ **`assertLogs` 必须收 root**：任务行由 `planner` 自己打
        （`coregeek.game.planner`），只盯 `coregeek.app` 会把整块漏掉 —— 与 SOP 那条同一个坑。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "短题目"
        raw["llmResp"] = ""  # 判题器还没回话 ⇒ 这一回合会提问

        def asked_after_sending(**overrides) -> tuple[str, str]:
            """`(任务行, 提问行)` —— 两张日志归两个 logger，各自按前缀取。"""
            raw.update(overrides)
            with self.assertLogs(level="INFO") as caught:
                self._handle(json.dumps(raw).encode("utf-8"))
            messages = [r.getMessage() for r in caught.records]

            def pick(head: str) -> str:
                return next((m for m in messages if m.startswith(head)), "")

            return pick("【本轮任务】："), pick("【本轮提问】：")

        task, ask = asked_after_sending()
        self.assertIn("【本轮任务】：短题目", task)
        self.assertIn("【上一轮模型回复】：无", task)
        #: `prompt` **打出来**（第 20 步起全文、第 28 步起是 **messages JSON**）：
        #: 模板头、工具清单、「沉淀的 SOP」那个槽、题目原文全在里面 —— 这四样
        #: **实盘上只有这里看得见**（本地 e2e 的"LLM"是我们自己写的，只证明解析自洽）。
        self.assertTrue(ask.startswith(ASK), ask[:20])
        messages = json.loads(ask[len(ASK):])  # 短题 ⇒ 没到上限，整串都在这行里
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertEqual(messages[1]["content"], "短题目")
        for piece in ("# Agent定位", "# 可使用的工具", "- SOP2Prompt：", "# 沉淀的 SOP"):
            self.assertIn(piece, messages[0]["content"])

        #: 判题器答了 ⇒ 回复那一格才有内容，而且**不再提问**（省 LLM 额度）
        task, ask = asked_after_sending(llmResp="答案")
        self.assertIn("【上一轮模型回复】：答案", task)
        self.assertEqual(ask, "", "已经有答案了还提问 ⇒ 白烧一次 LLM 额度")

        long_text = "题" * (LOG_TEXT_MAX + 7)
        task, _ = asked_after_sending(phaseTask=long_text)
        self.assertIn("题" * LOG_TEXT_MAX, task)
        self.assertNotIn("题" * (LOG_TEXT_MAX + 1), task)
        self.assertIn(f"共 {LOG_TEXT_MAX + 7} 字", task)

        #: **任务刚结束的那一回合**是唯一一次能看见"判题器最后答了什么"的机会
        #: （`phase_task` 已经空了）—— 所以触发条件里带着 `llm_resp`，不能只判任务。
        task, _ = asked_after_sending(phaseTask="")
        self.assertIn("【本轮任务】：无", task)
        self.assertIn("【上一轮模型回复】：答案", task)

    def test_a_long_answer_in_the_actions_line_passes_through(self):
        """`submitAnswer` 的 `taskAnswer` 是**外侧（LLM）给的自由文本**：第 26 步起
        **基本不截**（`LOG_TEXT_MAX`=40000，用户拍板"观察优先"）—— 9000 字的答案
        **原文全量**进日志，只有过了 40000 的上限才截、且留痕。

        这条**走真链路**（`answer_of` → `submitAnswer` → `describe`）—— 上面那条用例
        证明"`describe` 会用递进来的 `clip`"，这条证明"`app` 递的是真的那个"。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "请查询北京天气"  # 有任务 ⇒ 开拓者 `_answer_task`（不管白天夜里）
        raw["llmResp"] = "<answer>" + "答" * 9000 + "</answer>"
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        acts = [
            r.getMessage() for r in caught.records if r.getMessage().startswith("【动作】：")
        ]
        self.assertEqual(len(acts), 1, acts)
        self.assertIn("taskAnswer=", acts[0], "答案得打出来，不然这条用例什么也没钉住")
        self.assertIn("答" * 9000, acts[0], "9000 字 < 40000 ⇒ 原文全量进日志")
        self.assertNotIn("（共", acts[0])

        #: 过了上限才截，而且必须留痕
        raw["llmResp"] = "<answer>" + "答" * (LOG_TEXT_MAX + 10) + "</answer>"
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        acts = [
            r.getMessage() for r in caught.records if r.getMessage().startswith("【动作】：")
        ]
        self.assertIn(f"（共 {LOG_TEXT_MAX + 10} 字）", acts[0])
        self.assertNotIn("答" * (LOG_TEXT_MAX + 1), acts[0], "截掉的是尾巴，不是头")

    def test_the_prompt_line_keeps_its_own_limit(self):
        """提问行单独截在 `LOG_PROMPT_MAX`（1000）—— 第 26 步起它是**唯一**还截断的一行
        （`LOG_TEXT_MAX` 已放宽到 40000）。

        prompt 是拼出来的（模板 591 字 + 会话往来），会话部分在任务行里已有全文，
        这一行只需要看得见模板头与「沉淀的 SOP」那个槽 ⇒ 就近取 1000。
        超长必须留痕、且有界。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["llmResp"] = ""  # 没答过 ⇒ 这一回合提问
        raw["phaseTask"] = "题" * LOG_PROMPT_MAX
        AGENT.reset()
        #: `planner` 组装出来的那一份（同一道题的**首问**）。算完再清一次，
        #: 让 `_handle` 里那次 chat 也是这道题的首问 —— 上下文是累积的（第 25 步），
        #: 不清的话它会多出一句「请继续。」，全长就对不上了。
        full = AGENT.chat(raw["phaseTask"])
        AGENT.reset()
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        line = caught.records[-1].getMessage()

        self.assertTrue(line.startswith(ASK), line[:20])
        asked = line[len(ASK):]  # 去掉行首标记（`】：` 里那个括号挡着，不能用 `split("提问：")`）
        self.assertEqual(
            len(asked),
            LOG_PROMPT_MAX + len(f"…（共 {len(full)} 字）"),
            "提问那一格该是『上限字 + 留痕那句话』—— 超长必须留痕、且有界",
        )
        #: 第 28 步起 prompt 是 **messages JSON**：截断从头截，开头一定是 system 消息
        self.assertTrue(asked.startswith('[{"role":"system","content":"# Agent定位'), asked[:40])
        self.assertIn(f"共 {len(full)} 字", asked)

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
        """`acceptTask` / `submitAnswer` **没有 `targetPos`** —— 而旧版硬读它。

        这不是显示问题：`describe` 跑在 `app.handle` 的 `try` 里（`app._log`），
        一个 `IndexError` 会让**整回合退化成空指令** —— 开拓者领了任务却什么都没答，
        现象与"任务线根本没做"一模一样，极难排查。

        第 20 步起字段是**通用摊开**的（有什么打什么），这类动作天然踩不到雷；
        这条用例留着当**守门员**：谁要回头去手写字段清单，先在这里挂一次。
        """
        self.assertEqual(
            actions.describe({"10011": {"action": "acceptTask"}}, clip=_clip), "10011 acceptTask"
        )
        self.assertEqual(
            actions.describe(
                {"10011": {"action": "submitAnswer", "taskAnswer": "晴 26 度"}}, clip=_clip
            ),
            "10011 submitAnswer taskAnswer=晴 26 度",
        )

    def test_describe_prints_every_field_of_every_command(self):
        """通用摊开：一条指令**有什么字段就打什么**，一个都不许漏。

        手写清单的坏处不是"少打几个字"，而是**漏字段不会有人发现** —— 日志少打一个
        `controllerId`，看着完全正常（与"`attack` 的 key 是武器 id"是同一类坑：
        一个会把炮打歪，一个只会让事后复盘瞎猜）。

        `attack` 那三个字段里，**武器 id 是 key**、操控者在 `controllerId` ——
        旧版用一个 `←` 箭头表示这件事，现在就是 `controllerId=值`，同样一眼能看出来。
        """
        self.assertEqual(
            actions.describe(
                {
                    "10020": {
                        "action": "attack",
                        "controllerId": "10010",
                        "targetPos": [{"x": 4, "y": 4}],
                    },
                    "10012": {
                        "action": "build",
                        "name": "wall",
                        "targetPos": [{"x": 13, "y": 23}],
                    },
                },
                clip=_clip,
            ),
            "10020 attack controllerId=10010 (4,4)；10012 build name=wall (13,23)",
        )
        #: `targetPos` 是**数组**（`attack` 的等级 >1 时多格）：多格用 `、` 连，别只打头一个
        self.assertEqual(
            actions.describe(
                {
                    "10020": {
                        "action": "attack",
                        "controllerId": "10010",
                        "targetPos": [{"x": 4, "y": 4}, {"x": 5, "y": 5}],
                    }
                },
                clip=_clip,
            ),
            "10020 attack controllerId=10010 (4,4)、(5,5)",
        )

    def test_describe_clips_free_text_through_the_injected_clip(self):
        """自由文本（`taskAnswer`）一律过**调用方递进来的** `clip`。

        `describe` 不需要知道哪个字段是自由文本 —— 字符串值全过一遍就够了。
        截断规则（上限 + `…（共 N 字）` 那句留痕）全项目只有 `app._clip` 一份：
        `protocol` 不能 import `app`（依赖方向反了），所以是**把规则递进来**、
        不是在这儿复制一份。这条用例传一个假 clip，正好钉住"没复制"。
        """
        seen = []

        def clip(text: str) -> str:
            seen.append(text)
            return f"<{text[:3]}>"

        line = actions.describe(
            {"10011": {"action": "submitAnswer", "taskAnswer": "答" * 9}}, clip=clip
        )
        self.assertEqual(seen, ["答" * 9], "原文该整个交给 clip，而不是自己先截")
        self.assertEqual(line, "10011 submitAnswer taskAnswer=<答答答>")

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

        # ② LLM 要一条命令（新形状）⇒ 命令进 `executeCmd`，而**不是**当答案交上去
        raw["llmResp"] = (
            '<tool><tool_name>executeCmd</tool_name>'
            '<tool_param>python -c "print(1+1)"</tool_param></tool>'
        )
        body = ask()
        self.assertEqual(body["executeCmd"], 'python -c "print(1+1)"')
        self.assertEqual(body["prompt"], "")

        # ③ 沙盒交作业 ⇒ 结果**全文**回灌，这一轮绝不重复发命令；它自己上一轮要的那条
        #    命令也在会话里（第 25 步：发命令那轮记下的回复，在这里第一次看得见）
        raw["lastCmdResult"] = "[exitCode:0]\n2"
        body = ask()
        messages = json.loads(body["prompt"])
        self.assertEqual(
            messages[2],
            {
                "role": "assistant",
                "content": '<tool><tool_name>executeCmd</tool_name>'
                '<tool_param>python -c "print(1+1)"</tool_param></tool>',
            },
        )
        self.assertEqual(messages[3]["role"], "user")
        self.assertIn("[exitCode:0]\n2", messages[3]["content"])
        self.assertEqual(body["executeCmd"], "")

        # ④ LLM 给出答案 ⇒ **只交 `<answer>` 里的内容**（不是整段回复）
        #    （清掉 `lastCmdResult`：文档说"未发命令时为空字符串"，上一轮我们没发命令）
        raw["lastCmdResult"] = ""
        raw["llmResp"] = "<answer>晴 26 度</answer>"
        body = ask()
        self.assertEqual(
            body["roleCommandMap"]["10011"],
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
        )
        self.assertEqual((body["prompt"], body["executeCmd"]), ("", ""))

        # ⑤ 判题器说答错了 ⇒ 带着"上次交的是什么"再问一遍，**同时照旧提交**
        #    （两条通道独立：提问在推进，而按接口文档 L140 取"通过率最高"、重交零成本）
        #    ⚠️ 第 25 步起会话里有两份"它说过的话"，各归各的：**assistant 消息**收整段
        #    原文（`<answer>晴 26 度</answer>` —— 它真说过的话），**纠错块**收的必须是
        #    **交上去的那一份**（`answer_of` 解包后的 `晴 26 度`）—— 后者带标签的话
        #    LLM 会以为自己交了一堆标签，去改一个并不存在的问题。
        raw["errors"] = [{"errorCode": 2, "description": "答案不正确"}]
        body = ask()
        messages = json.loads(body["prompt"])
        self.assertIn(
            "<answer>晴 26 度</answer>",
            [m["content"] for m in messages if m["role"] == "assistant"],
        )
        self.assertIn(
            "【你上一次提交的答案被判定为不正确】\n晴 26 度\n请重新作答。",
            [m["content"] for m in messages if m["role"] == "user"],
        )
        self.assertEqual(body["executeCmd"], "")
        self.assertEqual(
            body["roleCommandMap"]["10011"]["action"], "submitAnswer", "提交不该被提问挤掉"
        )

        # ⑥ 落回裸文本 ⇒ 照旧原文提交（兜底：判题器的 LLM 不按我们的形状回时唯一的退路）
        raw["llmResp"] = "晴 26 度"
        body = ask()
        self.assertEqual(
            body["roleCommandMap"]["10011"],
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
        )

    def test_the_sandbox_line_only_appears_with_a_result(self):
        """沙盒行**只在真有回执时出现** —— 「没发命令就一定是空串」是文档写死的
        （接口文档 L33），所以"有沙盒行" ⟺ "上一轮真跑过一条命令"，这个对账关系要守住。

        反例（提问回合、发命令那一回合）**一条都不该多打**：日志字节是有预算的，
        而这条线每回合都可能触发。⚠️ 发命令那一回合之所以不打，是因为命令原文已经在
        上面任务行的"上一轮模型回复"里了 —— 同一回合、同一条字符串，抄第二遍是纯浪费。
        ⚠️ 这条日志由 `planner` 打 ⇒ `assertLogs` 得收 root（见上一条用例）。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "请查询北京天气"
        raw["llmResp"] = (
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>"
        )
        raw["lastCmdResult"] = ""

        def messages() -> list[str]:
            with self.assertLogs(level="INFO") as caught:
                self._handle(json.dumps(raw).encode("utf-8"))
            return [r.getMessage() for r in caught.records]

        #: 发命令那一回合：任务行说明了一切，没有沙盒行
        self.assertFalse([m for m in messages() if m.startswith(SANDBOX)])

        #: 有回执那一回合：多出**一条**沙盒行，且回执原文在里面
        raw["llmResp"] = ""
        raw["lastCmdResult"] = "[exitCode:0]\nhello"
        sandbox = [m for m in messages() if m.startswith(SANDBOX)]
        self.assertEqual(len(sandbox), 1, sandbox)
        self.assertIn("hello", sandbox[0])

    def test_a_long_sandbox_result_only_clips_at_the_cap(self):
        """沙盒输出可能到 64KB（接口文档 L33）：第 26 步起**基本不截**
        （`LOG_TEXT_MAX`=40000，用户拍板"观察优先"）—— 9000 字原文全量进日志，
        只有过了 40000 的上限才截，且截断必须留痕。

        `…（共 N 字）` 那句把"命令没输出"与"命令吐了 6 万字、你只看得到头"分开，
        后者正是最该立刻看见的事故形态。回灌给 LLM 的仍是全文 —— 两个下游
        要的东西不同：一个要正确性，一个要人眼看得下。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        raw["phaseTask"] = "请查询北京天气"
        raw["llmResp"] = ""
        raw["lastCmdResult"] = "[exitCode:0]\n" + "y" * 9000
        with self.assertLogs(level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        sandbox = [
            r.getMessage() for r in caught.records if r.getMessage().startswith(SANDBOX)
        ]
        self.assertEqual(len(sandbox), 1)
        self.assertIn("y" * 9000, sandbox[0], "9000 字 < 40000 ⇒ 原文全量进日志")
        self.assertNotIn("（共", sandbox[0])

        #: 过了上限才截，而且必须留痕
        raw["lastCmdResult"] = "y" * (LOG_TEXT_MAX + 10)
        with self.assertLogs(level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        sandbox = [
            r.getMessage() for r in caught.records if r.getMessage().startswith(SANDBOX)
        ]
        self.assertIn(f"（共 {LOG_TEXT_MAX + 10} 字）", sandbox[0])
        self.assertNotIn("y" * (LOG_TEXT_MAX + 1), sandbox[0], "截掉的是尾巴，不是头")

    def test_a_clean_round_stays_small(self):
        """**硬约束 5 的结构性守卫**（第 26 步起）：字段基本不截（`LOG_TEXT_MAX`=40000）之后，
        "最坏回合 ≤ 9300"随策略一起撤销 —— 顶格大字段一回合 ≈ **363KB**，用户拍板接受
        （赌判题器读 stdout；若不读，**一回合**就写满 64KB 管道 ⇒ 阻塞到响应超时 = 红线第一条）。

        还守得住的只有**结构部分**：没有大字段的回合必须仍然小 —— 摘要有自己的上界
        （`SUMMARY_MAX_ITEMS`）、动作行一回合最多几个角色、banner 一行。实测干净回合
        **506 字节**（`app._log` 的 docstring 与 `CLAUDE.md` 硬约束 5 里有同一张表）；
        这个守卫抓的是"又加进来一个每回合都打的大块"—— 那类结构性膨胀一来就是几百字节
        起步（地图就是 ~2.7KB/回合，第 26 步刚被砍掉）。

        ⚠️ **`assertLogs` 必须收 root（不写 logger 名）**：SOP 那条走
        `coregeek.agent.tools.sop`、任务行与沙盒行走 `coregeek.game.planner`，
        只盯 `coregeek.app` 的话它们**绕开本守卫**。
        第 18 步（SOP）与第 23 步（任务线搬家）正是这么发现的。
        """
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        raw["roundNo"] = 1
        raw["errors"] = []
        raw["lastRoundRoleActionResults"] = {}
        with self.assertLogs(level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
        total = sum(len(r.getMessage().encode("utf-8")) for r in caught.records)
        self.assertLess(total, 1500, f"干净回合 {total} 字节 —— 结构部分不该这么大")


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

    def test_vendors_are_an_index_of_their_own(self):
        """小贩**单独一张表**（`Map.vendors`，第 22 步）：`sell` 那条线唯一的目标点。

        它和矿在网格里的形状一样（都是 `neutralType`）却**不打矿种** —— 所以是
        `frozenset[Pos]` 而不是 `Mapping`。卖矿要先知道"小贩在哪"，而这一点
        **只能从网格认**：小贩不是 `teamOur.roles` 里的单位，别处查不到。

        顺带钉住第 15 步那条不变量没被改写：小贩**照样挡路、也照样不是矿**
        （`step_toward` 撞上它自然停在贴着一格 —— 那正好是 `sell` 要求的站位）。
        """
        grid = self._turn().map
        self.assertEqual(grid.vendors, {Pos(20, 16)})
        self.assertIn(Pos(20, 16), grid.blocked)
        self.assertNotIn(Pos(20, 16), grid.ores)

    def test_a_size_less_map_has_no_vendors_either(self):
        """尺寸非法 ⇒ 整个矩阵为空 ⇒ **小贩也没有**（`vendors` 得跟着 `ores` 一起空）。

        漏了这一步的症状很隐蔽：`frozenset()` 与"地图上没有小贩"在策略侧是同一件事
        （`_sell_ore` 的门 ②），但**属性不存在**会直接 `AttributeError`
        —— 那跑在 `handle` 的 `try` 里，代价是整回合空指令。
        """
        for size in ((-1, -1), (41, 0)):
            with self.subTest(size=size):
                empty = Map(size, {Pos(3, 3): "vendor"})
                self.assertEqual(empty.vendors, frozenset())
                self.assertEqual(empty.ores, {})
                self.assertEqual(empty.blocked, frozenset())

    def test_the_backpack_is_counted_by_name(self):
        """背包是**物品名数组**，重复即计数（接口文档 §1.3.1）—— `_bag` 取代了"只数石头"。

        `sell` 要按矿种报件数，而三个散装的 int 才是绕的那个东西：`backpack` 本来的形状
        就是一张名字表，将来买的券和道具也只会往这张表里加。
        `BaseRole.stone` 现在是它的**派生属性**（既有调用点一字未改）。

        ⚠️ 背包里的**非矿石**（开拓者那个 `medicine`）也照样收进来 —— `_bag` 不认识矿，
        认矿是 `planner` 的事（`SELLABLE`）。
        """
        by_id = {r.id: r for r in self._turn().roles}
        self.assertEqual(by_id[10010].bag, {"stone": 1, "iron": 1, "copper": 1})
        self.assertEqual(by_id[10010].stone, 1)
        self.assertEqual(by_id[10011].bag, {"medicine": 1})
        self.assertEqual(by_id[10011].stone, 0, "开拓者没有石头 —— 派生属性得跟着空")

    def test_a_missing_backpack_is_an_empty_bag(self):
        """背包缺失 / 不是数组 ⇒ 空表 ⇒ 石头 0 块、**一件都卖不掉**。

        降级方向是"少做"，与 `_gold` / `_size` 一致：宁可少采，不可对着空背包发 `build`，
        也不可对着空背包发 `sell`（`num` 报大件数会不会被判"指令非法"文档没写）。
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

        布局：上下各一行 `—` 标尺 + 32 行网格，每行 **43 列** = `"│"` + 41 列 + `"|"`。
        下面全是**裸下标**断言：它们把"字符落在第几列、第几行"钉死。
        """
        lines = self.grid.render().splitlines()
        self.assertEqual(len(lines), 34)
        self.assertEqual({len(line) for line in lines}, {43})
        self.assertEqual(lines[0], "—" * 43, "上标尺")
        self.assertEqual(lines[-1], "—" * 43, "下标尺")
        # 行 = height-1-y（y 向上、终端从上往下印）；列 = 1+x（第 0 列是左边框）
        # **小写 = 我方，大写 = 敌方**
        self.assertEqual(lines[8][11], "s")  # 我方基地左上角 (10,24)
        self.assertEqual(lines[9][12], "s")  # 我方基地右下角 (11,23)
        self.assertEqual(lines[8][10], "g")  # 加特林 (9,24)，与基地同一行
        self.assertEqual(lines[7][11], "r")  # 电磁狙击炮 (10,25)，在基地上方一行
        self.assertEqual(lines[22][31], "S")  # 敌方基地左上角 (30,10)
        self.assertEqual(lines[8][5], "o")  # 石矿 (4,24)
        self.assertEqual(lines[25][29], "%")  # 敌方围墙 (28,7) —— 墙不走字母，是例外
        self.assertEqual(lines[28][5], "x")  # 机器人 (4,4)
        self.assertEqual(lines[32][1], " ")  # (0,0) 空地 —— 最后一行是最底下的 y=0

    def test_render_keeps_y_pointing_up(self):
        """**行自上而下 = y 由大到小**，且每行的列偏移 = `1+x`。

        这是 y 翻转最直接的守卫 —— 只钉"最后一行是 y=0"的话，中间那些行翻反了它照样过。
        没有行号槽之后本用例改用**斜线地图**：第 y 行只有 `(y,y)` 是矿 ⇒ 每行矿的列位置
        就等于那一行的 y，翻反或错位一格立刻挂。
        """
        width, height = 4, 3
        grid = Map((width, height), {Pos(y, y): "stone" for y in range(height)})
        lines = grid.render().splitlines()
        self.assertEqual(len(lines), height + 2)
        for i, line in enumerate(lines[1:-1]):
            y = height - 1 - i
            self.assertEqual(line.index("o"), 1 + y, line)
        # 左边框那一列是空的（x=-1 没有格子），最底下一行才是 (0,0)
        self.assertEqual(lines[-2][1], "o")

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
        # 这几样不在 `_NAMES` 里 —— 它们不是"某个类别"，而是 `_char` 的兜底与大小写规则
        for token in ("x=机器人", "空格=空地", "?=未知", "大写=敌方", "%=敌方围墙"):
            self.assertIn(token, LEGEND)

        # **一个字符只能代表一样东西。** 第 21 步空地填 `#` 时正撞上"围墙也是 `#`"
        # —— 两个类别共用一个字符的症状是**图上分不出来**（我方围墙整圈沉进空地背景），
        # 而图例看上去只是重复了一项，不像 bug。上面那些 `assertIn` 一条都不会挂，
        # 所以这一条要单独钉。（`_char` 恒返回 1 字符，所以比长度就够了。）
        # 第 22 步空地改成**空格**、墙收回 `#`，这一对不再撞 —— 守门员照旧留着，
        # 它管的是"任何两个类别都不许共用字符"，不是"某一对具体值"。
        chars = [_char(kind) for kind in _NAMES] + ["x", " ", "?"]
        self.assertEqual(len(set(chars)), len(chars), sorted(chars))

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
                Worker(10010, Pos(5, 23), {"stone": 1}),
                Worker(10012, Pos(10, 16), {"stone": 1}),
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
        lines = _blocks(turn.summary())
        self.assertEqual(len(lines), 4)
        self.assertEqual(
            lines[0],
            "【回合】 85（夜里） ｜ 【金币】 20 | 【武器】 3/3："
            "10020 gatling(9,24)r4 10030 railgun(10,25)r7 10040 rocket(9,25)r∞c3",
        )
        self.assertEqual(
            lines[1],
            "【我方】 10010 worker(5,23)石1铁0铜0 ｜ 10012 worker(10,16)石1铁0铜0"
            " ｜ 10011 pioneer(10,12)石0铁0铜0",
        )
        self.assertEqual(lines[2], "【机器】 2 台：(4,4)h40 (5,5)h800")
        self.assertEqual(lines[3], "【可接任务点】 (14,14) (17,17)")

    def test_an_empty_turn_still_prints_every_block(self):
        """空局面：一条事实都没有，但**每一块都得有字**（`无` / `0 台`），不能是空行。"""
        lines = _blocks(self._turn(roles=(Worker(10010, Pos(5, 23)),)).summary())
        self.assertEqual(len(lines), 4)
        self.assertIn("【武器】 0/1：无", lines[0])
        self.assertIn("【机器】 0 台：无", lines[2])
        self.assertIn("【可接任务点】 无", lines[3])

    def test_missing_fields_are_not_reported_as_zero(self):
        """`-1` 是"字段缺失"，不是 0 金 / 第 -1 回合 —— 打成 `?`，别让它看着像个事实。"""
        line = _blocks(self._turn(gold=-1, round_no=-1).summary())[0]
        self.assertIn("【回合】 ?（夜里）", line)
        self.assertIn("【金币】 ?", line)

    def test_the_unknown_range_is_not_printed_as_a_negative_number(self):
        """射程 -1 = 字段缺失 ⇒ 够不着 ⇒ `?`；0 也照原样打 0（不做特殊处理）。"""
        turn = self._turn(
            roles=(Worker(10010, Pos(5, 23)),),
            weapons=(
                Weapon(1, "gatling", Pos(9, 24), -1, -1),
                Weapon(2, "gatling", Pos(9, 25), 0, 0),
            ),
        )
        line = _blocks(turn.summary())[0]
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
        line = _blocks(turn.summary())[2]
        self.assertIn("【机器】 11 台：", line)
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

    def test_the_three_sites_are_the_rear_top_corner_and_the_two_front_corners(self):
        """后列**上角**那一格 + 前排两角，顺序即建造顺序（第 24 步：后列从贴基地下沿挪到上角）。"""
        self.assertEqual(
            weapon_sites(Pos(10, 24), 41),
            (Pos(9, 25), Pos(12, 22), Pos(12, 25)),
        )
        #: 换边后整套落点自动跟着翻 —— 按**基地坐标**判而不用 `teamOur.type`
        self.assertEqual(
            weapon_sites(Pos(30, 10), 41),
            (Pos(32, 11), Pos(29, 8), Pos(29, 11)),
        )

    def test_the_rear_sites_operator_stays_off_the_bottom_route(self):
        """后列落点的**环内站位全在顶线一侧** ⇒ 后列操作者堵不死前排（第 24 步改阵的动机）。

        底行路线（门 → (9,22) → (10,22) → (11,22)）是从门走到前下角、不绕顶线的通道。
        旧阵后列在 (9,23)：环内站位含 (9,22)/(10,22)（正在这条路上），加上前上角操作者
        站 (12,24)，前下角的两个站位 (11,22)/(12,23) 就成了孤岛 —— **操作者堵死操作者**。
        后列挪到上角 (9,25) 之后环内站位只剩 (9,24)/(10,25)（顶线一侧），底行路线谁也压不着。
        """
        rear = weapon_sites(self.BASE, 41)[0]
        neighbors = {Pos(rear.x + d.x, rear.y + d.y) for d in STEPS}
        #: 只看**环内**站位：门柱那三个在走廊外面，本来就压不着人
        inside = neighbors & set(weapon_cells(self.BASE))
        bottom_row = {Pos(x, self.BASE.y - 2) for x in range(self.BASE.x - 1, self.BASE.x + 3)}
        self.assertTrue(inside, "后列武器总得有环内站位，这条用例才钉得住东西")
        self.assertFalse(inside & bottom_row, f"后列站位压住了底行路线：{inside & bottom_row}")
        #: 门柱上也能操它（从外面回来的操作者 BFS 停在门柱、不进走廊）——"堵不着人"的另一半
        door = {Pos(self.BASE.x - 2, y) for y in range(self.BASE.y - 3, self.BASE.y + 3)}
        self.assertTrue(neighbors & door, "后列武器该贴着门柱：外面就能操，不必进走廊")

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
            roles=(Worker(1, worker_pos, {"stone": stone}),),
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

    #: 三个落点 = (9,25) 后列上角 + (12,22)/(12,25) 前排两角（见 `weapon_sites`）
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
            2: Worker(2, Pos(8, 25)),
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
        self.walls[Pos(9, 25)] = WALL
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

    def test_ring_is_fourteen_cells_free_of_the_base_and_the_weapons(self):
        cells = wall_cells(self.BASE, 41)
        self.assertEqual(len(cells), 14, "6×6 边框 20 格减去背面那一整列 6 格")
        self.assertEqual(len(set(cells)), 14, "不该有重复格")
        self.assertEqual(set(cells) & base_cells(self.BASE), set(), "不能落在基地身上")
        self.assertEqual(set(cells) & set(weapon_cells(self.BASE)), set(), "不能占武器环")

    def test_the_back_column_is_never_walled(self):
        """背面那一列（背离机器人的一侧）**整列 6 格一格都不砌** —— 它是那道永远开着的门。

        用户第 17 步选定（此前只留中间 4 格缺口、上下两角照砌）：环一旦闭合，工人就进出不得了
        —— 既采不了矿，也回不到环内操炮，而 `remove`（拆墙）**至今没实现**，关进去就是整场出不来。
        """
        cells = set(wall_cells(self.BASE, 41))
        back = {Pos(8, y) for y in range(21, 27)}  # 基地在左半 ⇒ 背面是 x = bx-2
        self.assertEqual(cells & back, set(), "背面那一列不许出现 —— 包括上下两角")
        # 反过来说：20 格的边框里少的正好是这 6 格
        ring4 = {Pos(x, y) for x in (8, 13) for y in range(21, 27)} | {
            Pos(x, y) for y in (21, 26) for x in range(9, 13)
        }
        self.assertEqual(len(ring4), 20)
        self.assertEqual(ring4 - cells, back, "少掉的正好是背面整列，不是别的")

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
        self.assertEqual(len(ring), 14)
        self.assertEqual({c.x for c in ring}, {28, 29, 30, 31, 32}, "背面 = bx+3 那一列整列缺席")
        self.assertEqual(ring[-4:], (Pos(32, 7), Pos(31, 7), Pos(30, 7), Pos(29, 7)), "收尾是底行")

    def test_the_box_is_the_whole_buildable_area(self):
        """盒子 = `base_cells` ∪ `weapon_cells` ∪ 完整 20 格围墙环 = **36 格**（蓝圈那一块）。

        闸门问的是"这个人在不在即将被墙围起来的那片区域里"，判据就是它。⚠️ **不取 `width`**：
        盒子在基地两侧各外扩 2，左右半场是**同一个矩形**（与正面/背面那两条镜像的边相反）。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                box = box_cells(base)
                ring20 = {
                    Pos(x, y)
                    for x in range(base.x - 2, base.x + 4)
                    for y in range(base.y - 3, base.y + 3)
                    if x in (base.x - 2, base.x + 3) or y in (base.y - 3, base.y + 2)
                }
                self.assertEqual(len(ring20), 20)
                self.assertEqual(len(box), 36)
                self.assertEqual(
                    box, base_cells(base) | set(weapon_cells(base)) | ring20, "三块拼起来正好是它"
                )


    def test_no_building_ever_stands_on_the_door_column(self):
        """门那一列（背面整列）里**没有任何建筑** —— 基地 / 武器 / 墙都不在。

        闸门"会不会把人关住"几乎打不着，靠的就是这一条：门那 6 格空着 ⇒ **只有单位**能堵门。
        ⚠️ `wall_cells` 的背面列与 `weapon_sites` 的后列是**两个不同的变量**
        （`near - 2d` vs `near - d`），第 17 步的计划草稿正是在这里写错过一次 ——
        钉成断言，别再靠脑补。
        """
        for base in (Pos(10, 24), Pos(30, 10)):
            with self.subTest(base=base):
                door_x = min(c.x for c in box_cells(base))
                if base.x * 2 >= 41:
                    door_x = max(c.x for c in box_cells(base))  # 右半场镜像：门在最外那一列
                door = {Pos(door_x, y) for y in range(base.y - 3, base.y + 3)}
                self.assertEqual(len(door), 6, "门是整列 6 格")
                built = set(wall_cells(base, 41)) | set(weapon_sites(base, 41)) | base_cells(base)
                self.assertEqual(built & door, set(), "门里不该有基地 / 武器 / 墙")


class StepOutsideTest(unittest.TestCase):
    """`step_outside` 的网格级契约 —— 闸门两半唯一的判据，脱离游戏局面单独钉一遍。

    它的两条 `None` 合流是**有意的**（调用方只问"这一步迈不迈得出去"），代价是
    **"谁在盒子里面"必须由调用方自己筛**（`planner._trapped` 在那里踩过）。三种返回值各钉一条。
    """

    #: 手搭的 2×2 小盒子 —— 与基地几何无关，测的是基元本身
    BOX = frozenset({Pos(1, 1), Pos(2, 1), Pos(1, 2), Pos(2, 2)})
    SIZE = (41, 32)

    def test_a_pos_already_outside_returns_none(self):
        """已经在外面 ⇒ `None`（**不是**"走不出去"）—— 所以调用方必须自己筛 `pos in box`。"""
        self.assertIsNone(step_outside(Pos(5, 5), self.BOX, set(), self.SIZE))

    def test_a_sealed_box_returns_none(self):
        """四面围死 ⇒ `None`。"""
        ring = {Pos(x, y) for x in range(0, 4) for y in range(0, 4)} - self.BOX
        self.assertIsNone(step_outside(Pos(1, 1), self.BOX, ring, self.SIZE))

    def test_the_step_lands_outside_and_moves_exactly_one_cell(self):
        """返回的是**从 pos 迈出的第一步**：与 pos 切比雪夫距离恒为 1、且落点在盒外。"""
        step = step_outside(Pos(1, 1), self.BOX, {Pos(1, 2)}, self.SIZE)
        self.assertIsNotNone(step)
        self.assertEqual(Pos(1, 1).dist(step), 1, "一步一格")
        self.assertNotIn(step, self.BOX, "迈出去的这一步必须是盒外的格")

    def test_the_map_edge_never_lets_a_step_off_the_map(self):
        """贴着地图角、又只有越界一条路 ⇒ `None`（**不许走出地图**）。

        边界不在任务书 L85 的阻挡清单里（越界算"指令非法"还是"执行失败"文档没写），
        不走一定安全 —— `step_toward` 就是这么挡的，这里是同一条。
        """
        box = frozenset({Pos(0, 0)})
        self.assertIsNone(step_outside(Pos(0, 0), box, {Pos(1, 0), Pos(0, 1), Pos(1, 1)}, (2, 2)))


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
        self.worker = Worker(1, pos or self.worker.pos, {"stone": stone})
        return Turn(
            round_no=round_no,
            map=Map((41, 32), self.entries),
            roles=(self.worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_day_one_mines_then_walls_the_whole_ring_in_order(self):
        """把白天串起来跑到砌满：**14 格全砌上，且顺序与 `wall_cells` 逐格一致**。

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
                self.worker = Worker(1, self.worker.pos, {"stone": stone})
            else:
                self.worker = Worker(1, cell, {"stone": stone})

        self.assertEqual(built, list(wall_cells(self.BASE, 41)), "顺序必须与优先级表一致")
        self.assertEqual(stone, 0, "别多采 —— 白天总共就 14 格墙可砌")

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
        """14 格都砌满了 ⇒ 不再采**石头**（多采的只会压在背包里）。

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


class WallGateTest(unittest.TestCase):
    """用户给的建墙原则：**不能把工人关起来**（第 17 步）。闸门两半，各测各的。

    - `_ring` 里"**会关人就一格都不砌**"（判据 = 砌满这一圈墙之后谁出不去）；
    - `plan` 里"**要被关住的人先走出来**"（仅白天）。

    ⚠️ **这个局面得手工搭**：36 格的盒子里不可能有矿（任务书 L78 说矿区不生成在可建造区域内），
    背面那 6 格的门里也没有任何建筑（后列炮位在**武器环**的后列，不在那一列）⇒
    现实里只有**单位**能把门堵满，而且得 6 格同时堵上。不摆 6 个机器人，这两条用例
    连旧代码都放得过去 —— 第 8 步"夹具与真实路径不同形"那个坑的同一类。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    #: **门** = 背面那一整列（基地在左半 ⇒ 背面是 x = bx-2）。`wall_cells` 一格都不砌它。
    DOOR = {Pos(8, y) for y in range(21, 27)}
    #: 盒内一格空地（武器环上、没摆炮）、盒外一格
    INSIDE, OUTSIDE = Pos(12, 24), Pos(5, 24)

    def _turn(
        self,
        walls: Iterable[Pos] = (),
        door_blocked: bool = False,
        *,
        at1: Pos | None = None,
        at2: Pos | None = None,
        round_no: int = 1,
        robots: tuple[Robot, ...] = (),
        ores: dict[Pos, str] | None = None,
        prices: dict[str, int] | None = None,
    ) -> Turn:
        """`at1` / `at2` = 两个工人的站位（默认 1 号在盒内、2 号在盒外**且手里有一块石头**）。

        站位是可以换到盒外的 —— "盒外的人不许否决这一圈墙"那条就得两个都在外面才测得出。
        """
        workers = (
            Worker(1, at1 or self.INSIDE, {}),
            Worker(2, at2 or self.OUTSIDE, {"stone": 1}),
        )
        robots_on_map = {c: "robot" for c in (self.DOOR if door_blocked else ())}
        return Turn(
            round_no=round_no,
            map=Map(
                (41, 32),
                # 角色铺进地形（`model._entries` 就是这么干的）—— "谁站在哪"是真实输入的一部分
                _terrain(
                    self.WEAPONS,
                    {self.BASE: "station"},
                    {c: WALL for c in walls},
                    robots_on_map,
                    ores or {},
                    {w.pos: "worker" for w in workers},
                ),
            ),
            roles=workers,
            gold=0,
            weapons=self.WEAPONS,
            robots=robots,
            vendor_prices=prices or {},
        )

    def test_a_walled_box_never_holds_a_worker_in(self):
        """14 格**全砌满**、门开着 ⇒ 盒里的人照样走得出去。这就是"正面砌满也关不住人"。

        对照的是反向验证里"把背面那一列加回 `wall_cells`"：那时门被算成墙，
        `step_outside` 没有出路、返回 None，这条立刻挂。

        ⚠️ **不能拿 `step_toward` 顶替**：它的契约是"贴着 goal 即到"（`dist(goal) <= 1 ⇒ None`），
        用来测"出不出得去"时，一个**贴着门口、而门那格被堵住**的角色会被判成"到不了"。
        """
        turn = self._turn(walls=wall_cells(self.BASE, 41))
        self.assertNotIn(Pos(8, 24), turn.map.blocked, "门那一列连砌满之后也不该有东西")

        step = step_outside(self.INSIDE, box_cells(self.BASE), turn.map.blocked, turn.map.size)
        self.assertIsNotNone(step, "14 格砌满 + 门开着 ⇒ 出得去")
        self.assertEqual(self.INSIDE.dist(step), 1, "返回的是从 pos 迈出的第一步")

    def test_a_blocked_door_holds_the_walls_back(self):
        """门被堵满 ⇒ 盒外那个工人**手里攥着石头也不许砌**（砌下去就把 1 号关死了）。

        少砌这一回合的代价是墙晚砌完；砌下去的代价是**整场出不来**（`remove` 还没实现）。
        """
        turn = self._turn(door_blocked=True)
        cmds = plan(turn)
        self.assertNotIn(
            "2", cmds, "盒外那个工人该原地不动（没矿可采、价目表也空着），而不是去砌墙"
        )
        # 顺带钉住"整面墙"这个判据：门一堵，**14 格一格都不该砌**
        self.assertNotIn("build", {c["action"] for c in cmds.values()})

    def test_a_blocked_door_sends_the_worker_out_first(self):
        """同上门被堵，但墙上**还一个缺口都没砌** ⇒ 被围的人趁缺口先出去。

        闸门两半的关键差别在**两套障碍**：`leaving` 按"假设墙砌满"判（砌完就真出不去了），
        而这迈出去的一步按"**现在**"的障碍算（缺口就是出路）。
        若这里也按砌满算，`step_outside` 必然返回 None ⇒ 这一支永远空转、白写。
        """
        cmds = plan(self._turn(door_blocked=True))
        self.assertEqual(set(cmds), {"1"}, "要被关住的人这一回合必须动，盒外那个该原地待命")
        self.assertEqual(cmds["1"]["action"], "move")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(self.INSIDE.dist(cell), 1, "一步一格，不能瞬移")
        # 门那 6 格全堵着 ⇒ 出路只能是**没砌的墙**那几条边（正面 x = bx+3 最直接）
        self.assertIn(cell, box_cells(self.BASE), "迈出去的这一步还在盒内，方向朝缺口")

    def test_the_night_guard_never_walks_off_its_cannon(self):
        """**夜里不送人出去**：夜里不砌墙（`build` 仅白天）⇒ 没有"会被关住"这回事，
        这一支唯一的效果是把人从炮位上拉走。判据里那个 `turn.is_day` 就是为它写的。

        局面：门被堵 + 盒内工人**贴着**一座炮，射程内有个机器人 ⇒ 该开火，不该挪窝。
        """
        turn = self._turn(
            door_blocked=True,
            at1=Pos(9, 25),  # 贴着 (9,24) 那座轨炮，且在盒内
            round_no=85,  # 夜里（白天 70 回合）
            robots=(Robot(pos=Pos(13, 24), health=10),),
        )
        cmds = plan(turn)
        # `attack` 的 key 是**武器 id**，操控者在 `controllerId` 里（与下面那些 `move` 不同）
        fired = [c for c in cmds.values() if c.get("controllerId") == "1"]
        self.assertEqual(len(fired), 1, f"1 号该开火，而不是往盒子外走：{cmds}")
        self.assertEqual(fired[0]["action"], "attack")

    def test_a_worker_outside_never_vetoes_the_ring(self):
        """两个工人**都在盒外** ⇒ 门堵着也照砌（没人会被关住）。

        ⚠️ 这条钉的是第 17 步第一版真栽过的那个坑：`step_outside` 对"本来就在外面"与
        "走不出去"都返回 `None`（它的契约），所以筛"谁在盒子里"时漏掉 `r.pos in box`，
        就会把**所有在盒外干活的人**判成要被关住 ⇒ 闸门永远合上、墙一格都砌不上。
        症状极安静：不报错、不违规，只是工人站在工地上一动不动。
        """
        turn = self._turn(door_blocked=True, at1=Pos(5, 24), at2=Pos(14, 23))
        cmds = plan(turn)
        self.assertEqual(cmds["2"]["action"], "build", "盒外那个手里有石头 ⇒ 该砌第一格")
        self.assertEqual(cmds["2"]["name"], WALL)
        self.assertNotIn("1", cmds, "1 号没石头、也没石矿可采 ⇒ 空指令")

    def test_two_workers_walking_out_never_aim_at_the_same_cell(self):
        """两个人都被围 ⇒ **各走各的格**：闸门 (2) 的障碍里必须含 `claimed`。

        漏了它，两条 `move` 会指向同一格，正好撞上任务书 §4.5.4 的"目标点争夺"——
        两个人**都不动**，而日志上只是"这回合没动"，看不出是撞车。

        ⚠️ 站位是挑过的：`(9,25)` 与 `(10,25)` 这两个格**按现在的障碍算，第一步都指向 `(9,26)`**
        （穷举盒内空地找出来的唯一一对可行站位）。随手摆两个人在盒内，两条指令本来就不会撞 ——
        那种用例连旧代码都放得过去。
        """
        cmds = plan(self._turn(door_blocked=True, at1=Pos(9, 25), at2=Pos(10, 25)))
        self.assertEqual(len(cmds), 2, f"两个人的处境一样，都该往外走：{cmds}")
        self.assertEqual({c["action"] for c in cmds.values()}, {"move"})
        cells = {Pos(c["targetPos"][0]["x"], c["targetPos"][0]["y"]) for c in cmds.values()}
        self.assertEqual(len(cells), 2, "落点必须分开")

    def test_a_gated_round_never_sends_a_worker_mining_far_away(self):
        """闸门挡住的那一回合是"**墙还没砌完**"，不是"砌完了" ⇒ 盒外那个**什么都不做**。

        两种"没得砌"混在一起的话，它会掉头奔向地图另一头（几十回合回不来、还得再走回来），
        或者干脆就地砌一格 —— 而那一格砌下去，正好把**这一回合往外走的 1 号**关在墙里。
        局面：门被 6 个机器人堵满（1 号出不去了，只能趁缺口走一格）+ 环上一格都没砌
        + 盒外那个手里有石头 + 地图另一头有一座贵的铜矿。
        """
        turn = self._turn(
            door_blocked=True,
            at1=Pos(12, 24),
            at2=Pos(14, 23),
            ores={Pos(36, 24): "copper"},
            prices={"stone": 1, "copper": 5},
        )
        cmds = plan(turn)
        self.assertEqual(set(cmds), {"1"}, f"盒外那个该原地待命：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "被围的那个趁缺口先出来")

    def test_a_colleague_in_the_door_still_holds_the_wall_back(self):
        """**自己人站的那格也算障碍**：门被 5 个机器人堵住、第 6 格站着同事 ⇒ 谁都砌不了。

        与 `_ring` 里"自己人算路过"故意相反。那处问的是"这一格要不要砌"（把路过的人排掉，
        否则两个工人对着改目标来回踱步，第 8 步实测过）；这里问的是"**会不会有人出不来**"——
        同事**此刻真的**堵在门那一列上，把他当路过而放行，砌下去就把里面那个封死了
        （`remove` 缺席 ⇒ 整场出不来）。宁可用一个回合的工期换这条不可逆的错。

        ⚠️ 光有同事挡路**测不出这条**：他站在待砌的墙格上时，那格本来就是"假设砌满"里的墙，
        两种口径下都是障碍（穷举过，见下方那条没抓住的突变）。必须是**门那一列**
        （永远不砌的 6 格）才算数 —— 所以让 2 号站到门那一列的最后一格上：
        `door_blocked` 铺的 6 个机器人里，那一格被**角色层覆盖**（`_turn` 里角色铺在最后，
        与 `model._entries` 一致）⇒ 门上 5 个机器人 + 我们自己人 1 个。
        """
        turn = self._turn(door_blocked=True, at1=Pos(12, 24), at2=Pos(8, 26))
        self.assertEqual(self.DOOR - turn.map.blocked, set(), "门那一列整列都该挡着")
        cmds = plan(turn)
        self.assertEqual(set(cmds), {"1"}, f"盒内那个趁缺口走，站门上的那个不许砌：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move")


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
    #: 14 格全砌满 ⇒ `_ring` 空 ⇒ 进入"墙砌完了"那一支
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


class SellOreTest(unittest.TestCase):
    """墙砌满之后的白天：**把矿背到小贩跟前卖掉**（第 22 步）。

    与 `SpareOreTest` 是同一条支路上的**先后**：砌满 ⇒ 先卖（`_sell_ore`），
    卖不动才去采（`_mine_spare_ore`）。所以这里每个局面都砌满，而且必须能说清
    "为什么没去卖" —— 四条门各有一条用例（没货 / 没人收 / 不够本 / 回不来）。

    站位与小贩的关系是这一步的核心事实（任务书 §4.4：「在小贩周围一格内使用」）——
    而小贩格本身挡路，`step_toward` 撞上它自然停在"周围一格"，与采矿同一条契约。

    阈值口径（用户拍板）：**货值 ≥ 往返回合数**（≈ 每回合至少换 1 金币）才动身。
    那个"1 金币 ≈ 1 回合"是**拍的**，没有文档依据，用例把它钉成可测的行为：
    小贩距离 10 ⇒ 阈值 20 ⇒ 铜（价 5）要攒 4 块。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(9, 24): "railgun", Pos(9, 22): "rocket"})
    RING = {c: WALL for c in wall_cells(Pos(10, 24), 41)}
    #: 小贩 (20,24)：与基地切比雪夫距离 10 ⇒ 来回 20 回合，阈值 20 金币
    VENDOR = Pos(20, 24)
    #: 卖不动时工人转去采的那座矿 —— **故意放在小贩的反方向**：
    #: 否则"朝矿走"在距离上也"朝小贩走"，那条用例会假通过（第 8 步踩过同款夹具坑）
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
            roles=(Worker(1, pos, bag),),
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
        """**贴着小贩 ⇒ 一次性卖光手上那种矿**（`num` = 全部件数）。

        `num` 报成 1（默认值）等于把背包里的铜一块一块地卖 —— 一回合一条指令，
        卖 4 块要 4 个回合，而任务书 §4.4 明说"**支持批量贩卖**"。
        """
        cmd = self._sold(self._turn(Pos(20, 23), {"copper": 4}))  # 小贩正下方，切比雪夫 1
        self.assertEqual(cmd, {"action": "sell", "name": "copper", "num": 4})

    def test_the_pricier_ore_is_sold_first(self):
        """一次只卖一种 ⇒ 卖**收购价最高的**那种（同价才看件数）。

        手上铁铜都有时卖铜（5 > 3）。谁把"铜 > 铁 > 石头"写死都**恰好**对得上样例 ——
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
        """砌满之后**多余的石头也卖**（用户选定：三种都卖）。

        这一条是"石头为什么敢进 `SELLABLE`"的实证：调用点只在"墙砌完了"那一支
        （`_build_walls` 的 `if not free:`），而墙没砌完时手里的石头一律有用。
        """
        cmd = self._sold(self._turn(Pos(20, 23), {"stone": 6}))
        self.assertEqual(cmd, {"action": "sell", "name": "stone", "num": 6})

    def test_a_full_load_that_does_not_pay_for_the_trip_is_not_worth_walking(self):
        """货**不够本** ⇒ 一步都不走，留在矿边接着采（阈值 = 2 × 距离 = 20 金币）。

        一块铜值 5，走 10 格过去要 10 回合、回来还要 10 —— 这一趟的收益还抵不上
        在那儿多采 3 回合。行为上要能看出来"它没在往小贩那儿走"：这一回合是
        `move`/`collect` 朝**矿**去，不是朝小贩。
        """
        turn = self._turn(self.FAR, {"copper": 1})
        cmd = plan(turn)["1"]
        self.assertEqual(cmd["action"], "move", cmd)
        step = Pos(cmd["targetPos"][0]["x"], cmd["targetPos"][0]["y"])
        self.assertLess(step.dist(self.ORE), self.FAR.dist(self.ORE), "该朝矿走，不是朝小贩")
        self.assertGreater(step.dist(self.VENDOR), self.FAR.dist(self.VENDOR), "不该朝小贩走")

    def test_a_load_worth_the_trip_gets_walked_to_the_vendor(self):
        """攒够了（4 块铜 = 20 金币 ≥ 阈值 20）⇒ 动身朝小贩走一格。

        ⚠️ 阈值是 `>=` 不是 `>`：4 块铜正好 20，差的这一点会把"恰好攒够"的工人
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
        #: 手上四块铜、价格也对，但没有小贩 ⇒ 这一回合只能去采矿
        cmd = plan(self._turn(Pos(20, 23), {"copper": 4}, vendor=None))["1"]
        self.assertNotEqual(cmd["action"], "sell", cmd)

    def test_nobody_buys_it_means_nothing_is_sold(self):
        """价目表为空 / 小贩不收这种矿 ⇒ **一件都不卖**（与 `_pick_ore` 同一条口径）。

        价目表是**逐回合**从 `vendorShopList` 读的：空表意味着"这一回合什么都不收"，
        而不是"按默认价收"。降级方向是少做 —— 宁可多采一趟，不可白送一件矿石出去
        （`num` 报出去就没了，而 `sell` 没有撤销）。
        """
        #: 空表 ⇒ **一条指令都没有**（不是"发条空指令"）：`_pick_ore` 也按 0 算，
        #: 于是连"该去采哪座矿"都答不出来 —— 这正是不写死价格的代价与收益。
        self.assertEqual(plan(self._turn(Pos(20, 23), {"copper": 4}, prices={})), {})
        #: 只收石头、而手上一块石头也没有 ⇒ 铜按 0 算 ⇒ 不是卖矿（转去采那座石矿）
        cmd = plan(self._turn(Pos(20, 23), {"copper": 4}, prices={"stone": 1}))["1"]
        self.assertNotEqual(cmd["action"], "sell", cmd)

    def test_too_late_in_the_day_to_walk_there_and_back(self):
        """白天不够"走到小贩 + 从小贩回基地" ⇒ 不卖，转去采矿。

        夜里必须在炮位上，黑天还在赶路 = 拿火力换矿石。`roundNo=60` ⇒ 白天还剩 11 回合，
        减去 `TIME_MARGIN` 5 只剩 6，而这一趟（10 + 10）根本走不完。
        """
        sell_early = plan(self._turn(self.FAR, {"copper": 9}))
        self.assertEqual(sell_early["1"]["action"], "move", sell_early)
        #: 同一个局面只差回合号：这时候连矿也不该去（`_mine_spare_ore` 有同款闸门）
        self.assertEqual(plan(self._turn(self.FAR, {"copper": 9}, round_no=60)), {})

    def test_the_wall_comes_first(self):
        """**墙没砌完 ⇒ 一块矿都不卖**（哪怕人已经站在小贩旁边）。

        这就是用户那句「手里面保持能建造墙的石头量就行，**然后**选择价格最高的矿」里
        那个"然后"，也是 `SELLABLE` 敢把石头收进来的全部理由：砌墙那一支里不存在
        "多余的石头"（`_stones_to_mine` 的上限正是"还差几格墙"）。
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
            10010: Worker(10010, Pos(13, 21), {"stone": 14}),
            10012: Worker(10012, Pos(11, 22), {"stone": 13}),
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
                    roles[role_id] = Worker(role_id, role.pos, {"stone": role.stone - 1})
                elif cmd["action"] == "collect":
                    roles[role_id] = Worker(role_id, role.pos, {"stone": role.stone + 1})
                else:
                    roles[role_id] = Worker(role_id, cell, {"stone": role.stone})

        self.assertGreaterEqual(len(built), 2, "两个工人在原地打转 ⇒ 一座墙都砌不上")
        self.assertEqual({c.x for c in built}, {13}, "先砌的必须是**正面**那一列")
        # 只钉"这两格必须补上"：再往后铺到 (13,26) 也是对的（同属正面列），不必锁死集合
        self.assertTrue({Pos(13, 21), Pos(13, 25)} <= set(built), "夹缝里那两格得砌上")


class StandingOnTheTargetTest(unittest.TestCase):
    """工人**站在目标格上**时不许对着它 `build`，先挪开一格（第 17 步 e2e 揪出来的死循环）。

    `model._entries` 把单位铺在**最后** ⇒ 人站在墙上时网格里只剩 `worker`，**墙被盖掉了**
    （那一格照旧在 `Map.blocked` 里，但看不出是墙还是空地）；而 `_ring` 的"自己人算路过"
    又会把它复活成候选 ⇒ `free[0]` 永远是脚下这一格，`dist == 0` 也是合法建造位
    ⇒ 每回合 `build` 同一格，**永远轮不到下一格**。

    真服务实测（40 回合预算、工人兜里 20 块石头）：第 36 回合站在 `(11,21)` 上砌了 `(12,21)`，
    第 37 回合砌脚下的 `(11,21)`（这一格是对的，它当时还没砌），**第 38~40 回合继续砌同一格**
    —— 最后停在 12/14，石头白花，而且 `free` 永不为空 ⇒ 连"砌完了去采矿"那一支都进不去。
    """

    BASE = Pos(10, 24)
    WEAPONS = _records({Pos(9, 23): "gatling", Pos(12, 22): "railgun", Pos(12, 25): "rocket"})
    #: 卡死当时已经砌好的那些（正面列 6 + 顶行 4 + 底行头一格），e2e 里第 36 回合的状态
    BUILT = {c for c in wall_cells(BASE, 41) if c.y == 26 or c.x == 13} | {Pos(12, 21)}
    ON = Pos(11, 21)  # 工人站的那一格 —— 也是 `wall_cells` 里下一个该砌的

    def _turn(self, walls: Iterable[Pos], worker: Worker, round_no: int = 40) -> Turn:
        return Turn(
            round_no=round_no,
            map=Map(
                (41, 32),
                # 角色压在静态层之上 —— 与 `model._entries` 的写入顺序一致（单位盖过地形）
                _terrain(
                    self.WEAPONS,
                    {self.BASE: "station"},
                    {c: WALL for c in walls},
                    {worker.pos: "worker"},
                ),
            ),
            roles=(worker,),
            gold=0,
            weapons=self.WEAPONS,
        )

    def test_a_worker_on_its_own_wall_steps_aside(self):
        """"墙就在脚下"（砌过、被自己盖住）⇒ 这一回合是 `move`，不是又一次 `build`。

        旧代码在这里发的是 `build (11,21)` —— 与上一回合**逐字段相同**，于是无限重复。
        """
        worker = Worker(1, self.ON, {"stone": 5})
        turn = self._turn(self.BUILT | {self.ON}, worker)
        cmds = plan(turn)
        self.assertEqual(len(cmds), 1, f"只有一个工人、只该有一条指令：{cmds}")
        self.assertEqual(cmds["1"]["action"], "move", "站着的那一格不许再砌一遍")
        cell = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])
        self.assertEqual(worker.pos.dist(cell), 1, "挪开是**一步一格**，不能瞬移")
        self.assertNotIn(cell, turn.map.blocked, "落点得是可走的格")

    def test_a_worker_on_the_last_cell_still_finishes_the_ring(self):
        """脚下是**最后一格**没砌的墙 ⇒ 这一回合让开、下一回合照样砌上（收敛，不是干等）。

        这一条钉的是"先挪开"没有把闸门变成拖延：挪开之后就**贴着**它了（`dist == 1`），
        下一回合 `free[0]` 还是它、位置却合法 ⇒ 稳稳砌完。
        """
        worker = Worker(1, self.ON, {"stone": 5})
        built = {c for c in wall_cells(self.BASE, 41) if c != self.ON}
        cmds = plan(self._turn(built, worker))
        self.assertEqual(cmds["1"]["action"], "move", "先让开")
        aside = Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"])

        worker = Worker(1, aside, {"stone": 5})  # 判题器照做，下一回合
        cmds = plan(self._turn(built, worker))
        self.assertEqual(cmds["1"]["action"], "build", "下一回合就该把这一格砌上，不能干等")
        self.assertEqual(cmds["1"]["name"], WALL)
        self.assertEqual(
            Pos(cmds["1"]["targetPos"][0]["x"], cmds["1"]["targetPos"][0]["y"]),
            self.ON,
            "砌的还是原来那一格（绕一圈回到同一格 ⇒ 不是收敛，是踱步）",
        )


class NightWeaponTest(unittest.TestCase):
    """夜里：**所有角色**（含开拓者）回炮位、开火打**最大伤害落点**（第 27 步起，
    用户改的方针：目标是**打死所有机器人**，不再是"补刀残血"）。

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
        kind: str = "gatling",
    ) -> Turn:
        """一名工人**已经贴着**那座炮（切比雪夫 1）—— 开火与否只看目标与冷却。"""
        gun = Weapon(
            id=self.GUN,
            kind=kind,
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

    def test_beams_tie_break_to_the_nearest(self):
        """第 27 步改方针：**不再挑血最少的**。L1 的 10 点伤害对满血机器人（≥40 血）
        等额 ⇒ 并列时打**近**的；"补刀优先"（第 10 步的旧方针）就此作废。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 900), Robot(Pos(14, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 12, "y": 26}], "伤害等额 ⇒ 打近的，不看血量")

    def test_a_dying_robot_is_not_worth_a_full_shot(self):
        """**有效伤害** = min(伤害, 剩余血)：将死者吸收不完一整发 ⇒ 打吸收得完的那台
        —— 目标是打死**所有**机器人，把整发浪费在只剩 4 血的人身上就是少打死一个。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(12, 26), 4), Robot(Pos(14, 26), 900))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 14, "y": 26}], "4 血的只值 4 点，900 血的值满 10 点")

    def test_distance_breaks_the_effective_tie(self):
        """有效伤害并列时打**近**的（同伤害下先打完近的，远处的下一回合再补）。"""
        cmd = self._only_cmd(
            self._manned(Robot(Pos(15, 25), 40), Robot(Pos(13, 26), 40))
        )
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 26}])

    def test_ignores_the_best_cell_when_it_is_out_of_range(self):
        """**先按射程过滤、再算最大伤害** —— 顺序写反就成了"拿最优落点、但够不着的当目标"。"""
        weak = Robot(Pos(12, 31), 10)  # 距 (12,25) 是 6，够不着
        cmd = self._only_cmd(self._manned(weak, Robot(Pos(13, 25), 900)))
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "10 血那只够不着，只能打 900 的")

    def test_the_rocket_lands_for_maximum_splash(self):
        """**火箭的最大伤害落点**（第 27 步的核心变化）：中心 20 + 周围 8 格溅射 10
        （任务书 §4.5.4）⇒ 落进机器人**簇**里、落在最肥的那台身上 —— 而不是挑残血的。

        簇：(13,25) 40 血与 (14,25) 8 血相邻。落 (13,25) = 20+8=28 分；落 (14,25) =
        8+10=18 分 ⇒ 落在 40 血那台身上（旧方针会去打 8 血的残血）。"""
        cmd = self._only_cmd(
            self._manned(
                Robot(Pos(13, 25), 40),
                Robot(Pos(14, 25), 8),
                kind="rocket",
            )
        )
        self.assertEqual(cmd["action"], "attack")
        self.assertEqual(cmd["targetPos"], [{"x": 13, "y": 25}], "落点该吃满中心+溅射")

    def test_the_rocket_prefers_a_cluster_over_a_lone_target(self):
        """同样的射程里，**簇**（两台相邻 = 30 分）优先于孤零零一台（20 分）——
        机器人成群来，溅射才是火箭的本职。"""
        cmd = self._only_cmd(
            self._manned(
                Robot(Pos(13, 25), 40),
                Robot(Pos(13, 26), 40),
                Robot(Pos(15, 25), 800),  # 孤台、血厚也一样：20 分 < 30 分
                kind="rocket",
            )
        )
        self.assertIn(
            cmd["targetPos"][0],
            [{"x": 13, "y": 25}, {"x": 13, "y": 26}, {"x": 14, "y": 25}],
            "该落进簇里（中心+溅射都吃得到），不打孤台",
        )

    def test_two_guns_do_not_pile_onto_a_dying_robot(self):
        """**同回合记账**：先开火的炮把伤害记在账上（`assigned`），后开的按**剩余血**挑
        —— 两座炮不挤同一个将死的目标（第 27 步撤掉了"集火补刀"，方针是打死**所有**）。

        两座加特林（射程 4）都能打到 (11,24) 12 血与 (9,26) 40 血；1 号炮先开（打近的
        (11,24)，记 10 点）⇒ 2 号炮看到它只剩 2 血（有效 2 < 10）⇒ 转打 40 血那只。
        没有记账的话两座都会去打 12 血的"残血"。"""
        guns = self._guns(Pos(12, 25), Pos(9, 22))
        turn = self._turn(
            Worker(1, Pos(12, 24)),  # 贴着 1 号炮
            Worker(2, Pos(9, 23)),  # 贴着 2 号炮
            weapons=guns,
            robots=(Robot(Pos(11, 24), 12), Robot(Pos(9, 26), 40)),
        )
        cmds = plan(turn)
        self.assertEqual(cmds[str(self.GUN)]["targetPos"], [{"x": 11, "y": 24}], "1 号先开，打近的")
        self.assertEqual(
            cmds[str(self.GUN + 1)]["targetPos"],
            [{"x": 9, "y": 26}],
            "2 号按剩余血算：(11,24) 只剩 2 血，转打 40 血的",
        )

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

    def setUp(self) -> None:
        #: SOP 是**单实例上的跨回合状态**（全项目唯一一处），不清就会跨用例串味。
        AGENT.reset()

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
        """第一次提问 = **四段模板（定位 / 工具 / 格式 / SOP）+ 题目原文**，不带任何回灌。

        断言用 `assertNotIn` 而不是"等于 `PROMPT.format(...)`"：后者是同义反复
        （模板与断言一起改，永远过得去），而"第一次问不该有任何回灌"才是真要求。
        """
        prompt, execute = task_channel(self._turn(self.TASK))
        self.assertIn(self.TASK, prompt)
        self.assertEqual(execute, "")
        self.assertNotIn(self.RESULT_MARK, prompt)
        self.assertNotIn(self.RETRY_MARK, prompt)
        for header in ("# Agent定位", "# 可使用的工具", "# 输出格式", "# 沉淀的 SOP"):
            self.assertIn(header, prompt)

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
        """工具调用里的 `<tool_param>` **就是那条命令**，两侧空白去掉、内部原样保留。

        **两种形状都收**：第 18 步的新形状（`<tool_name>` 点名工具）与第 16 步的旧形状
        （块里直接放命令 ⇒ 当 `executeCmd`）。兼容层是用户拍板的 —— 判题器的 LLM 认不认
        新形状**是黑盒**，旧形状是"不被认账"时唯一能让任务线继续跑下去的退路。

        多标签只取第一条：`executeCmd` 只有一个字段，一回合只跑得了一条（接口文档 L210）。
        """
        def call(param: str) -> str:
            return (
                f"<tool><tool_name>executeCmd</tool_name>"
                f"<tool_param>{param}</tool_param></tool>"
            )

        cases = {
            call("ls -la"): "ls -la",
            f"  {call('  ls -la  ')}  ": "ls -la",
            call('python -c "print(1)"\nprint(2)'): 'python -c "print(1)"\nprint(2)',
            f"{call('first')} 然后 {call('second')}": "first",
            # ↓ 第 16 步的旧形状（兼容层）
            "<tool>ls -la</tool>": "ls -la",
            "  <tool>  ls -la  </tool>  ": "ls -la",
            '<tool>python -c "print(1)"\nprint(2)</tool>': 'python -c "print(1)"\nprint(2)',
            "<tool>first</tool> 然后 <tool>second</tool>": "first",
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply)), ("", expected))

    def test_a_broken_tool_tag_yields_no_command(self):
        """凑不齐的标签 ⇒ **没有命令可发**。别把半截标签当命令丢进沙盒。

        ⚠️ 最后两条是第 18 步新增的**隐式子路径 ③′**：调用是完整的，但工具给不出命令
        （`SOP2Prompt` / 未知工具 / 有名字没参数）。它们与"畸形"落同一个出口：**重问**。
        """
        replies = (
            "<tool ls",
            "<tool>ls",
            "</tool>",
            "<tool></tool>",
            "<tool>  </tool>",
            "<tool><tool_name>executeCmd</tool_name></tool>",
            "<tool><tool_name>没这个工具</tool_name><tool_param>ls</tool_param></tool>",
            "<tool><tool_name>SOP2Prompt</tool_name><tool_param>方法</tool_param></tool>",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply))[1], "")

    def test_a_tool_that_yields_no_command_is_asked_again(self):
        """**③′：工具调用成了、但工具不产出命令 ⇒ 重问**（既不提交、也不发命令）。

        `SOP2Prompt` 是最典型的一个 —— 它**当回合没有别的回执**，而下一轮 prompt 里
        那段「沉淀的 SOP」就是"调用成功了"的凭证（它自己看得见）。
        ⚠️ 这条路径**不会活锁**：任务期间 prompt 不限量不计数（接口文档 L198），
        而出口有"LLM 改口 / `code 2` 带来的纠错段 / 它看见 SOP 段"三条。
        """
        prompt, execute = task_channel(self._turn(self.TASK, "<tool><tool_name>SOP2Prompt</tool_name><tool_param>先看目录</tool_param></tool>"))
        self.assertEqual(execute, "")
        self.assertIn(self.TASK, prompt)
        self.assertIn("先看目录", prompt)  # 存下的 SOP 立刻出现在同一份 prompt 里
        self.assertEqual(AGENT.sop, "先看目录")

    def test_the_stored_sop_rides_along_in_every_later_prompt(self):
        """**自进化的可观测证据**：存过一次之后，后面每一份 prompt 都带着它 ——
        包括"回灌沙盒结果"与"带纠错重问"这两条分支。"""
        AGENT.SOP2Prompt("先 ls 再算")
        for turn in (
            self._turn(self.TASK),
            self._turn(self.TASK, cmd_result="[exitCode:0]\nok"),
            self._turn(self.TASK, errors=(Error(2, "x"),), llm_resp=self.ANSWER),
        ):
            with self.subTest(turn=turn):
                self.assertIn("先 ls 再算", task_channel(turn)[0])

    def test_the_singleton_carries_the_sop_across_turns(self):
        """**单实例的接线证据**（第 19 步）：开拓者这一回合存下的 SOP，**下一回合**的提问里带着。

        两回合之间**没有任何东西被传过去** —— 上一回合的 `llm_resp` 没进 payload、
        也没有返回值被接收（`task_channel` 的返回值由 `app` 直接拼进报文）。
        能把它接起来的只有"两次调用用的是同一个 `AGENT`"，所以这条用例就是
        `from ..agent import AGENT` 那个注入点的守门员：
        把它改回"每次新建一个 `Agent()`"，这里立刻挂（症状在实盘上 = **永远学不会**，
        而日志上完全看不出来：每回合的 SOP 都恰好是空的）。
        """
        task_channel(self._turn(self.TASK, "<tool><tool_name>SOP2Prompt</tool_name><tool_param>先看目录</tool_param></tool>"))
        prompt, execute = task_channel(self._turn("另一道题"))
        self.assertEqual(execute, "")
        self.assertIn("先看目录", prompt)

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
        users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
        self.assertIn(output, users[-1])

    def test_the_reply_is_remembered_even_on_command_rounds(self):
        """**发命令那一轮也记回复**（第 25 步 `AGENT.hear` 的存在理由）：③ 那轮没有
        prompt，但它的工具调用必须进会话 —— 否则回灌那一轮 LLM 看见的是
        "题目 → 莫名其妙的结果"，它自己要的命令凭空消失。粘住的 `llmResp`
        顺带被去重（assistant 只出现一次）。
        """
        call = "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>"
        task_channel(self._turn(self.TASK))  # ⑥ 首问（会话从这道题开始）
        prompt, execute = task_channel(self._turn(self.TASK, call))  # ③ 发命令（无提问）
        self.assertEqual(execute, "ls")
        self.assertEqual(prompt, "")
        prompt, execute = task_channel(
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\n2")  # ② 回灌（llmResp 粘住）
        )
        self.assertEqual(execute, "")
        self.assertEqual(
            [(m["role"], m["content"]) for m in json.loads(prompt)][1:],
            [
                ("user", self.TASK),
                ("assistant", call),
                ("user", "【上一条命令的执行结果（原文）】\n[exitCode:0]\n2"),
            ],
        )

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
        users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
        self.assertIn("[exitCode:0]\nok", users[-1])

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
        users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
        self.assertIn("[exitCode:0]\n晴", users[-1])
        self.assertIn(self.ANSWER, users[-1])
        self.assertIn(self.RETRY_MARK, users[-1])

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

    def test_the_retry_blames_exactly_what_we_submitted(self):
        """⚠️ **纠错段里带的必须是"我们交上去的那一份"，不是回复原文。**

        交的是解包后的 `晴 26 度`，骂的却是 `<answer>晴 26 度</answer>` 的话，
        LLM 会以为自己交了一堆标签 —— 它会去改一个并不存在的问题。
        两处（`_answer_task` 提交、判据 ④ 回灌）共用 `answer_of` 就是为了这件事，
        这条用例把它钉死：**骂的 = 交的**。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                llm_resp=f"<answer>{self.ANSWER}</answer>",
                errors=(Error(2, "答案不正确"),),
            )
        )
        self.assertEqual(execute, "")
        self.assertIn(self.RETRY_MARK, prompt)
        self.assertIn(self.ANSWER, prompt)
        self.assertNotIn(f"<answer>{self.ANSWER}</answer>", prompt)

    def test_the_two_call_sites_agree_on_what_the_answer_is(self):
        """**期望值由测试自己算** —— 两个调用点必须落在同一份上（第 19 步的搬家守门员）。

        上一份答案的**交出**（`plan` → `_answer_task`）与它被骂时**回灌**的（判据 ④）
        在判题器那侧是**同一件事**："你上次答的 X 不对"里的 X，就是我们上次交的。
        两处各写一份判据的后果不是崩溃，而是**LLM 去改一个并不存在的问题**
        （它交的 `晴 26 度` 被骂成 `<answer>晴 26 度</answer>`，于是它开始往标签上使劲）。

        这里对每个回复**独立地**用 `answer_of` 算出期望值，再去比两个调用点的产出 ——
        所以 `answer_of` 若被搬进 `Agent` 自成一派（第 19 步最自然的手抖），
        提交与回灌至少有一边会与它分家，这里立刻挂。
        """
        for reply in (
            "<answer>晴 26 度</answer>",
            "晴 26 度",
            "  晴 26 度\n",
            "<answer>晴 26 度</answer>\n补充一句",
            "<tool>ls</tool>",
            "<tool ls",
            "",
        ):
            with self.subTest(reply=reply):
                expected = answer_of(reply)
                submitted = plan(self._turn(self.TASK, reply))
                if expected:
                    self.assertEqual(
                        submitted,
                        {"10011": {"action": "submitAnswer", "taskAnswer": expected}},
                    )
                else:
                    self.assertEqual(submitted, {}, "空/工具的回复不该被交上去")
                if expected:
                    AGENT.reset()  # 会话是跨回合状态（第 25 步）：不清的话上一条 subTest 的回复会进这条的 prompt
                    prompt = task_channel(
                        self._turn(self.TASK, llm_resp=reply, errors=(Error(2, "答案不正确"),))
                    )[0]
                    #: 钉**纠错块整块原文**：会话里可以有带标签的 assistant 消息（那是它
                    #: 真说过的话），但骂的必须是**交上去的那一份**（`answer_of` 解包后的）。
                    users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
                    self.assertIn(
                        f"【你上一次提交的答案被判定为不正确】\n{expected}\n请重新作答。",
                        users[-1],
                    )

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
                users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
                self.assertIn(output, users[-1])
                self.assertEqual(execute, "")

    def test_prompt_and_command_are_never_both_set(self):
        """**两条通道互斥** —— 这是整个状态机唯一的不变量，遍历所有分支钉一遍。

        "同一轮既提问又发命令"会让 LLM 在没看到结果的情况下作答 ⇒ 又要一遍同一条命令
        ⇒ 活锁。它也是 `task_channel` 之所以合成一个函数、而不是 `prompt_for` +
        `execute_for` 的全部理由（拆开就要把这条链写两遍）。
        """
        call = "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>"
        turns = [
            self._turn(),
            self._turn(self.TASK),
            self._turn(self.TASK, self.ANSWER),
            self._turn(self.TASK, f"<answer>{self.ANSWER}</answer>"),
            self._turn(self.TASK, call),
            self._turn(self.TASK, "<tool>ls</tool>"),
            self._turn(self.TASK, "<tool ls", cmd_result="[exitCode:0]\nok"),
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\nok"),  # 发过命令又拿到结果
            self._turn(self.TASK, cmd_result="[TIMEOUT]\n…"),
            self._turn(self.TASK, self.ANSWER, errors=(Error(2, "x"),)),
            self._turn(self.TASK, "<tool ls", errors=(Error(2, "x"),)),
            # ③′：工具调用成了但工具不产出命令（含 SOP 那条会写状态的）
            self._turn(self.TASK, "<tool><tool_name>SOP2Prompt</tool_name><tool_param>方法</tool_param></tool>"),
            self._turn(self.TASK, "<tool><tool_name>未知</tool_name><tool_param>ls</tool_param></tool>"),
            self._turn(self.TASK, "<tool><tool_name>executeCmd</tool_name></tool>"),
        ]
        for i, turn in enumerate(turns):
            with self.subTest(i=i):
                prompt, execute = task_channel(turn)
                self.assertFalse(prompt and execute, (prompt, execute))

    def test_the_answer_is_submitted_verbatim(self):
        """裸文本答案**原文进、原文出**（逐字对 `docs/response.txt` L54）：
        我们不知道判题器要什么格式，加工只会引入自己的假设。"""
        cmds = plan(self._turn(self.TASK, self.ANSWER))
        self.assertEqual(cmds, {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}})

    def test_a_wrapped_answer_is_submitted_without_the_markup(self):
        """`<answer>…</answer>` ⇒ 交的是**块里那一段**，不是整段回复。

        多字段结构化答案（通过率按"字段个数"算）落在这里：交上去的字节里不能混进标签。
        """
        for reply in ("<answer>晴 26 度</answer>", "  <answer> 晴 26 度 </answer>  "):
            with self.subTest(reply=reply):
                self.assertEqual(
                    plan(self._turn(self.TASK, reply)),
                    {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}},
                )

    def test_a_malformed_answer_wrapper_submits_nothing(self):
        """畸形 `<answer>`（空块 / 半截）⇒ **这一回合不提交**。

        不回落成原文是有意的：交一串光秃秃的标签只会被判一次错。判题器取"通过率最高的一份"
        （接口文档 L140）⇒ 少交一次不扣分，而**不交**永远比**交错的**好。
        """
        for reply in ("<answer></answer>", "<answer>   </answer>", "<answer>晴"):
            with self.subTest(reply=reply):
                self.assertEqual(plan(self._turn(self.TASK, reply)), {})

    def test_a_blank_answer_is_not_submitted(self):
        """空答案不发 —— 那可能被判成"字段缺失"，正是红线里的"指令非法"。
        （顺带钉住 `.strip()`：只有空白也必须当成空。）"""
        self.assertEqual(plan(self._turn(self.TASK, "   ")), {})

    def test_a_tool_call_is_never_submitted_as_an_answer(self):
        """⚠️ **没取到命令的回复也绝不能当答案交上去。**

        `_answer_task` 与 `task_channel` 判据 ⑤ 用的是**同一个谓词**（`answer_of`，
        内部就是 `looks_like_tool` 那一条）—— 一边当命令、一边当答案就是第二份真相。
        交上去的话，`<tool>ls</tool>` 会被判题器当成一次错误答案（`errorCode 2`）。
        """
        replies = (
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            "<tool>ls</tool>",
            "<tool>ls</tool>\n记住这个",
            "<tool ls",
            "<tool_name>executeCmd</tool_name><tool_param>ls</tool_param>",
        )
        for reply in replies:
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


# ── 第 18 步：Agent 系统（`agent/` + `tools/`）；第 19 步：单实例 + 状态在实例上 ──
class AgentToolCallTest(unittest.TestCase):
    """工具注册表与顶层调度 `Agent.tool_call`。

    ⚠️ **这一类的 `setUp` 造的是 `AGENT` 之外的实例**（用户拍板：Agent 是单实例，
    状态住在实例上 ⇒ 新实例天然干净）。所以这里的用例**不需要**复位任何东西 ——
    复位是给"必须走包根单例"的那些用例准备的（`HandleTest` / `TaskChannelTest`）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_execute_cmd_returns_the_command_verbatim(self):
        """`executeCmd` 就是"把命令搬进响应字段"这一步 —— **本地一个字都不执行**。

        换行、引号、重定向、中文原样过（沙盒那边才解释它）。所以这里**只能**断言原文返回：
        断言里出现任何"执行"的痕迹（`subprocess` / `os.system`）都说明这一步走错了。
        """
        for cmd in (
            "ls -la",
            'python -c "print(1+1)"',
            "cat < input.txt > out.txt",
            "grep -n '中文' a.txt\nwc -l a.txt",
        ):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.agent.tool_call("executeCmd", cmd), cmd)

    def test_sop2prompt_stores_the_method_and_yields_no_command(self):
        """`SOP2Prompt` 存下方法、**返回空串**（它不产出命令）。

        返回值直接进响应顶层的 `executeCmd` ⇒ 返回非空就是往沙盒里丢一条命令
        （而这条命令根本不存在，只会白烧一次沙盒执行）。
        """
        self.assertEqual(self.agent.tool_call("SOP2Prompt", "第一步：先 ls"), "")
        self.assertEqual(self.agent.sop, "第一步：先 ls")

    def test_an_unknown_tool_yields_no_command_and_no_exception(self):
        """未知工具 ⇒ 空串，**绝不抛**。

        它跑在 `app.handle` 的 `try` 里，抛出去会把**整回合所有角色的指令**一起带走
        （不只任务线）—— 合法、不计异常，但白白丢一个回合。LLM 编工具名是常态。
        """
        for name in ("nope", "", "execute_cmd", None):
            with self.subTest(name=name):
                self.assertEqual(self.agent.tool_call(name, "ls"), "")

    def test_a_blank_or_non_string_param_never_reaches_the_tool(self):
        """空参数 / 非字符串参数 ⇒ 空串，**而且 SOP 没被清空**（闸门在调用之前）。

        这条钉的是**闸门的位置**：`SOP2Prompt("")` 的语义是"清空"，
        如果校验下沉到工具里，一次空参数调用就会把整场攒下来的 SOP 抹掉。
        """
        self.agent.SOP2Prompt("保住我")
        for param in ("", "   ", "\n", None, 42, ["ls"]):
            with self.subTest(param=param):
                self.assertEqual(self.agent.tool_call("SOP2Prompt", param), "")
        self.assertEqual(self.agent.sop, "保住我")

    def test_every_registered_tool_is_described_and_callable(self):
        """`tool_desc()` 覆盖工具表里的每一个工具，且每个都能真的调起来。

        注册了却没进描述（LLM 永远不知道它存在），或者描述里有、注册表里没有
        （LLM 一调就落空）—— 两种都是**只有在实盘上才会暴露**的不一致。
        """
        desc = self.agent.tool_desc()
        for name in self.agent._tools:
            self.assertIn(name, desc)
        for name, (impl, text) in self.agent._tools.items():
            with self.subTest(name=name):
                self.assertTrue(callable(impl))
                self.assertTrue(text.strip())

    def test_a_newly_registered_tool_shows_up_everywhere(self):
        """**加一个工具只改一处**（`Agent.__init__` 里那张表）—— 描述与调度同时跟上。

        注入一个假工具来钉这条性质（把描述写死成字面量的实现会在这里露馅）：
        漏掉的症状是"**LLM 永远不知道它存在**"，本地全绿、实盘上只是"少用了一个工具"。

        ⚠️ **注入的是本类 setUp 里那个新实例的表**（第 19 步起工具表是实例属性）
        ⇒ 不需要 `finally: del` —— 实例是这条用例私有的，跑完就没人再看得见它。
        """
        self.agent._tools["测试用工具"] = (lambda param: f"命令:{param}", "只在这条用例里存在")
        self.assertIn("测试用工具", self.agent.tool_desc())
        self.assertIn("只在这条用例里存在", self.agent.tool_desc())
        self.assertEqual(self.agent.tool_call("测试用工具", "参数"), "命令:参数")
        self.assertNotIn("测试用工具", Agent().tool_desc())


class SopStateTest(unittest.TestCase):
    """SOP 的跨回合状态 —— **全项目唯一一处**，第 19 步起住在 `Agent` 实例上。

    用的是**每条用例自己的新实例**（不用包根单例）：状态在实例上 ⇒ 天然隔离，
    这也顺带把"状态确实在实例上而不是某个模块里"钉住了（见
    `test_a_fresh_agent_starts_with_no_sop` / `test_the_sop_never_leaks_between_instances`）。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_it_replaces_instead_of_appending(self):
        """**整段替换**，不是追加。

        追加没有遗忘机制：几百回合下来 prompt 会被旧套路撑爆，而且"上一版不对"这件事
        LLM 自己重写一遍就能表达。
        """
        self.agent.SOP2Prompt("第一版")
        self.agent.SOP2Prompt("第二版")
        self.assertEqual(self.agent.sop, "第二版")

    def test_it_survives_across_calls(self):
        """存下来之后**下一个调用者读得到** —— 这就是"跨回合"的全部含义。"""
        self.agent.SOP2Prompt("先看 ls 的输出再算")
        self.assertEqual(self.agent.sop, "先看 ls 的输出再算")
        self.assertEqual(self.agent.sop, "先看 ls 的输出再算")

    def test_a_fresh_agent_starts_with_no_sop(self):
        """新实例**不带**任何 SOP（第 19 步：状态是实例属性，不是模块里的变量）。

        退化的实现（状态写回某个模块级变量）会让这条挂 —— 而那种退化在实盘上的症状是
        "测试互相串味"，本地能看出来，但没这条用例就得等它串味了才知道。
        """
        self.agent.SOP2Prompt("甲的方法")
        self.assertEqual(Agent().sop, "")

    def test_the_sop_never_leaks_between_instances(self):
        """两个实例各存各的 —— 反向钉死"状态在模块级"那种退化。"""
        other = Agent()
        self.agent.SOP2Prompt("甲")
        other.SOP2Prompt("乙")
        self.assertEqual(self.agent.sop, "甲")
        self.assertEqual(other.sop, "乙")

    def test_each_instance_keeps_its_own_tool_table(self):
        """工具表也是实例的：`SOP2Prompt` 那一项是**绑定方法**，钉在各自的实例上。

        共享一张模块级工具表、而表里那个函数去写"某个全局 SOP"是很自然的退化写法，
        症状是"两个 Agent 的 SOP 互相覆盖" —— 这条用例用**走工具表**的路径（不是直接
        调方法）把它挡住。
        """
        other = Agent()
        self.agent.tool_call("SOP2Prompt", "甲走工具表")
        other.tool_call("SOP2Prompt", "乙走工具表")
        self.assertEqual(self.agent.sop, "甲走工具表")
        self.assertEqual(other.sop, "乙走工具表")

    def test_reset_clears_this_instance(self):
        """`reset()` 是**整个测试文件赖以隔离的那个机制** —— 它必须真的清掉**自己**。

        `AGENT` 是模块级的：`HandleTest` / `TaskChannelTest` 的 `setUp` 全靠它才不跨用例串味。
        所以退化的写法有两种，这条各挡一半：
        ① **不生效**（`return` 掉）⇒ 上一个用例存的 SOP 会灌进下一个用例的 prompt；
        ② **清错了对象**（写成 `Agent()._sop = ""`，即清一个刚造出来的新实例）⇒ 看着像清了，
           自己身上那份一点没动。两种在实盘上的症状都是"**改了一处代码，另一处跟着变**"，
           而本地只有这条用例会先叫。
        """
        self.agent.SOP2Prompt("要清掉的东西")
        self.agent.reset()
        self.assertEqual(self.agent.sop, "")
        self.assertNotIn("要清掉的东西", self.agent.chat("题目"))
        #: 复位之后还能重新存（别把 reset 写成"把实例锁死"）
        self.agent.SOP2Prompt("第二版")
        self.assertIn("第二版", self.agent.chat("题目"))

    def test_an_overlong_sop_is_truncated_and_says_so(self):
        """超上限 ⇒ **保头截断**，而且日志同时报"收到多少 / 存了多少"。

        静默截断正是第 14 步被叫醒的那个坑：LLM 灌进来 9000 字，日志上只看见"存 1000 字"，
        下一个人会以为是它只写了 1000 字。上限存在的理由是硬约束 5（prompt 每回合都发）。
        """
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("长" * 9000)
        self.assertEqual(len(self.agent.sop), sop.SOP_MAX)
        line = caught.records[0].getMessage()
        self.assertIn("9000", line)
        self.assertIn(str(sop.SOP_MAX), line)

    def test_storing_the_same_text_again_is_silent(self):
        """同样的内容存第二遍**不打日志**。

        `llm_resp` 粘住时 LLM 会把同一段 SOP 反复喂进来，每回合打一行是白花 stdout 预算
        （硬约束 5），而"又存了一遍同样的东西"不算"有事"。
        """
        self.agent.SOP2Prompt("一样的内容")
        with self.assertNoLogs(sop.__name__, level="INFO"):
            self.agent.SOP2Prompt("一样的内容")

    def test_a_multiline_sop_is_logged_as_one_line(self):
        """SOP 必然是多行的 ⇒ 日志必须**打成一行**（换行转义）。

        不转义的话一条记录变几十行，而 `logging` 的时间戳前缀只加在**第一条物理行**上
        （与 `app._log` 的"三块拼成一条"同一条理由）。
        """
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("第一步：ls\r\n第二步：cat")
        message = caught.records[0].getMessage()
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertIn("\\n", message)

    def test_clearing_is_silent_about_content(self):
        """清空（= 整段替换成空串）也要留一行 —— 否则"怎么没了"无从查起。"""
        self.agent.SOP2Prompt("有内容")
        with self.assertLogs(sop.__name__, level="INFO") as caught:
            self.agent.SOP2Prompt("")
        self.assertEqual(self.agent.sop, "")
        self.assertEqual(len(caught.records), 1)

    def test_the_logger_name_does_not_depend_on_the_agent(self):
        """SOP 那一行的 logger 名**仍然是 `coregeek.agent.tools.sop`**（第 19 步搬状态时的取舍）。

        名字变了就会连带改三份东西：`app._log` 的字节表、"唯一一条不在 `app` 名下的日志"
        那条守卫用例、`CLAUDE.md` 硬约束 5。这条用例是那个取舍的守门员
        （把 `store` 挪进 `agent/agent.py` 就会在这里挂）。
        """
        #: 断的是 **`LOGGER` 的名字**（不是模块的 `__name__` —— 那个是同义反复）：
        #: 它必须与 `app._log` 的字节表、`CLAUDE.md` 硬约束 5 里写的那个字面量一致。
        #: 把 `LOGGER` 挪进 `agent/agent.py` ⇒ 这里 `AttributeError`（挪走了），
        #: 只改名字 ⇒ 断言的字符串对不上。两种都挂。
        self.assertEqual(sop.LOGGER.name, "coregeek.agent.tools.sop")


class ChatPromptTest(unittest.TestCase):
    """prompt 的组装 —— 四段模板（system）+ **累积的**会话记录（第 25 步起）。

    断言**逐字**钉住四个小节标题与两个形状的示例：它们是"LLM 照不照抄"的唯一杠杆，
    而 `str.format` 漏填一个占位符会让整段变成 `{tool_desc}` 这种字面量出现在 prompt 里
    —— 那种错在实盘上表现为"LLM 完全不按格式回"，本地却什么都看不出来。
    """

    def setUp(self) -> None:
        self.agent = Agent()

    def test_the_placeholders_are_all_filled(self):
        prompt = self.agent.chat("题目")
        for header in ("# Agent定位", "# 可使用的工具", "# 输出格式", "# 沉淀的 SOP"):
            self.assertIn(header, prompt)
        #: 会话记录是**拼接**出来的（不走 `str.format`），会漏的只有模板自己那两个槽
        for leftover in ("{tool_desc}", "{sop}"):
            self.assertNotIn(leftover, prompt)

    def test_every_tool_appears_in_the_prompt(self):
        """prompt 里的工具清单由**实例的工具表**生成 ⇒ 每个注册的工具都得在。"""
        prompt = self.agent.chat("题目")
        for name in self.agent._tools:
            self.assertIn(name, prompt)

    def test_both_output_shapes_are_shown_verbatim(self):
        """两个形状（工具调用 / `<answer>`）**逐字**出现在输出格式那一段。

        这是唯一能提高"LLM 照抄概率"的杠杆：描述得含糊一点，它就自己发明第三种形状，
        而那种失败**本地测不出来**（我们的解析自洽，判题器认不认只有实盘知道）。
        """
        prompt = self.agent.chat("题目")
        self.assertIn("<tool><tool_name>工具名</tool_name><tool_param>参数原文</tool_param></tool>", prompt)
        self.assertIn("<answer>答案本身</answer>", prompt)

    def test_the_task_text_is_there(self):
        self.assertIn("请查询北京天气", self.agent.chat("请查询北京天气"))

    def test_the_two_injections_only_appear_when_given(self):
        """回灌那两段"没有就不占地方"，而且**在第一次提问里一个都看不到**。"""
        plain = self.agent.chat("题目")
        self.assertNotIn("【上一条命令的执行结果", plain)
        self.assertNotIn("【你上一次提交的答案", plain)

        with_result = json.loads(self.agent.chat("题目", result="[exitCode:0]\nok"))
        self.assertIn("【上一条命令的执行结果（原文）】\n[exitCode:0]\nok", with_result[-1]["content"])

        with_retry = json.loads(self.agent.chat("题目", retry="晴 26 度"))
        self.assertIn("【你上一次提交的答案被判定为不正确】\n晴 26 度", with_retry[-1]["content"])

    def test_a_stored_sop_shows_up_in_the_next_prompt(self):
        """**这就是"自进化"的全部可观测证据**：存过一次之后，后面每一份 prompt 都带着它。

        第 19 步起这条证据是**两个方法之间的接线**（`SOP2Prompt` 写 `self._sop`，
        `chat` 读 `self._sop`）—— 走的是实例，不是"某个模块变量还在"
        （跨回合那条更强的证据在 `TaskChannelTest.test_the_singleton_carries_the_sop_across_turns`）。
        """
        self.agent.SOP2Prompt("先看目录再动手")
        self.assertIn("先看目录再动手", self.agent.chat("另一道题"))

    def test_the_same_task_accumulates_its_conversation(self):
        """**同一个 task = 同一个上下文**（第 25 步的立身之本）：第二次提问里看得见
        题目、它自己的回复与回灌；第一次提问里则什么回复都还没有。"""
        first = json.loads(self.agent.chat("题"))
        self.agent.hear("<tool>ls</tool>")
        second = json.loads(self.agent.chat("题", result="[exitCode:0]\nok"))
        self.assertEqual([m["role"] for m in first], ["system", "user"])
        self.assertEqual(
            [(m["role"], m["content"]) for m in second][1:],
            [
                ("user", "题"),
                ("assistant", "<tool>ls</tool>"),
                ("user", "【上一条命令的执行结果（原文）】\n[exitCode:0]\nok"),
            ],
        )

    def test_a_different_task_starts_a_fresh_conversation(self):
        """换题 ⇒ 新会话：旧题的往来一个字都不带过来（身份判据 = **题目原文**）。"""
        self.agent.chat("甲题")
        self.agent.hear("甲题的回复")
        prompt = self.agent.chat("乙题")
        self.assertIn("乙题", prompt)
        self.assertNotIn("甲题", prompt)

    def test_a_reask_round_appends_the_standing_line(self):
        """没有新内容的重问轮（畸形回复 / `SOP2Prompt` 之后）⇒ 追加「请继续。」：
        判题器的 LLM 是黑盒，会话停在它自己的输出上是个含糊指令。首问则不需要。"""
        first = self.agent.chat("题")
        self.assertNotIn("请继续。", first)
        self.agent.hear("<tool ls")
        self.assertIn("请继续。", self.agent.chat("题"))

    def test_reset_clears_the_conversation_too(self):
        """`reset` 是用例隔离的唯一手段（单实例换不掉）⇒ 会话必须一起清，
        否则上一用例的往来会灌进这一用例的 prompt。"""
        self.agent.chat("题")
        self.agent.hear("上一场的回复")
        self.agent.reset()
        self.assertEqual(
            [m["role"] for m in json.loads(self.agent.chat("题"))], ["system", "user"]
        )

    def test_the_system_is_refreshed_every_round(self):
        """system（四段 header）**每轮现刷**：同一道题进行中沉淀的 SOP，下一轮就看得见
        —— 这是 ③′（`SOP2Prompt` 不产出命令）"调用成功"的回执，冻在构造时就没了。"""
        first = self.agent.chat("题")
        self.agent.SOP2Prompt("先 ls")
        second = self.agent.chat("题")
        self.assertNotIn("先 ls", first)
        self.assertIn("先 ls", second)

    def test_braces_in_the_values_are_not_scanned_again(self):
        """`str.format` **只做一次** —— 替换值里的 `{}` 不能被当成占位符。

        题目原文与沙盒输出都是**任意文本**，python 代码片段里 `{}` 太常见了；
        二次扫描会在 `chat` 里直接抛 `KeyError`/`IndexError` ⇒ 整回合退化成空指令。
        SOP 是第三个替换值（由 `Agent` 传进 `PROMPT.format`），三处一起钉；
        会话正文则走 `json.dumps`，与 `format` 无关。"""
        self.agent.SOP2Prompt("SOP 里有 {sop} 和 {0}")
        messages = json.loads(self.agent.chat("题目 {task} {0} {}", result="{'a': 1}"))
        contents = [m["content"] for m in messages]
        self.assertIn("题目 {task} {0} {}", contents)
        self.assertIn("【上一条命令的执行结果（原文）】\n{'a': 1}", contents)
        self.assertIn("SOP 里有 {sop} 和 {0}", messages[0]["content"])


class ContextTest(unittest.TestCase):
    """`Context` —— 任务内全量会话上下文（第 25 步；第 28 步起渲染成**标准 messages JSON**）。

    判题器的 LLM 每回合只看到我们发出的 `prompt` 一段字符串；第 28 步起它是一个
    JSON 数组 `[{"role": "system"/"user"/"assistant", "content": ...}]`（标准 chat 格式 ——
    自造的文本版式用户实测**效果非常差**，已弃）。这里钉 Context 本身：构造即问、进表规则、
    粘住去重、全量保真。跨回合接线在 `ChatPromptTest`，判据链接线在 `TaskChannelTest`，
    端到端在 `HandleTest.test_the_task_loop_through_handle`。
    """

    SYSTEM = "# Agent定位\n（占位 header）"

    def setUp(self) -> None:
        self.ctx = Context("请查询北京天气")
        #: system 由 Agent 每次发送前刷新（四段 header），这里给个占位证明它进 JSON
        self.ctx.system = self.SYSTEM

    def messages(self) -> list[dict]:
        return json.loads(self.ctx.render())

    def test_a_fresh_context_opens_with_the_task(self):
        """**构造即问**：首条 user 消息 = 题目**原文** —— 结构由 role 表达，
        正文不再加 `【题目】` 这类包装（那是文本版式的补丁）。"""
        self.assertEqual(self.ctx.task, "请查询北京天气")
        self.assertEqual(
            self.messages(),
            [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": "请查询北京天气"},
            ],
        )

    def test_hear_records_the_reply_verbatim(self):
        """回复**原文**进 assistant 消息 —— 会话记的是它真说过的话
        （纠错块才收 `answer_of` 解包后的那份）。"""
        self.ctx.hear("<tool>ls</tool>")
        self.assertEqual(
            self.messages()[-1], {"role": "assistant", "content": "<tool>ls</tool>"}
        )

    def test_a_sticky_reply_is_heard_only_once(self):
        """`llmResp` 可能粘住（接口文档对它一个字没写、对 `lastCmdResult` 却写明不粘）
        ⇒ 与**最后一条消息**相同的回复不进表第二遍。"""
        self.ctx.hear("同一条回复")
        self.ctx.hear("同一条回复")
        self.assertEqual(
            [m["role"] for m in self.messages()], ["system", "user", "assistant"]
        )

    def test_a_repeat_after_another_message_is_heard_again(self):
        """中间隔了别的消息之后又来同文 ⇒ **记**：那不是粘住，是真的又说了。"""
        self.ctx.hear("同一句话")
        self.ctx.nudge()
        self.ctx.hear("同一句话")
        self.assertEqual(
            [m["role"] for m in self.messages()],
            ["system", "user", "assistant", "user", "assistant"],
        )

    def test_feed_adds_the_two_titled_blocks(self):
        """回灌轮的 user 消息：沙盒结果与（或）纠错 —— 标题留在 content 里当内容标签
        （沙盒输出是任意文本，没标签分不清哪段是什么）。"""
        self.ctx.feed("[exitCode:0]\n2", "晴 26 度")
        content = self.messages()[-1]["content"]
        self.assertIn("【上一条命令的执行结果（原文）】\n[exitCode:0]\n2", content)
        self.assertIn("【你上一次提交的答案被判定为不正确】\n晴 26 度\n请重新作答。", content)

    def test_nudge_appends_the_standing_line(self):
        """无新内容的重问轮 ⇒ 一句固定收尾（会话不能停在它自己的输出上）。"""
        self.ctx.hear("<tool ls")
        self.ctx.nudge()
        self.assertEqual(self.messages()[-1], {"role": "user", "content": "请继续。"})

    def test_every_message_survives_verbatim_and_in_order(self):
        """**全量、不截断、逐字**（用户拍板"先不压缩"）：题目/回复/结果里的 `{}`、
        换行、标签一个都不许动，顺序就是进表的顺序 —— `json.dumps`/`loads` 负责转义与还原。"""
        self.ctx.hear("回复 {'a': 1}")
        self.ctx.feed("结果 {task} {0}", "")
        self.ctx.hear("<answer>答案</answer>")
        self.assertEqual(
            [m["content"] for m in self.messages()],
            [
                self.SYSTEM,
                "请查询北京天气",
                "回复 {'a': 1}",
                "【上一条命令的执行结果（原文）】\n结果 {task} {0}",
                "<answer>答案</answer>",
            ],
        )


class ToolReplyParseTest(unittest.TestCase):
    """`tool_of`：解析工具调用。**严格**（与 `looks_like_tool` 故意相反）。"""

    def test_a_full_call_gives_the_name_and_the_param(self):
        reply = "<tool><tool_name>executeCmd</tool_name><tool_param>cat /tmp/a.txt</tool_param></tool>"
        self.assertEqual(tool_of(reply), ("executeCmd", "cat /tmp/a.txt"))

    def test_the_param_keeps_its_inner_newlines(self):
        """参数**内部**的换行原样保留（只去首尾空白）—— 多行命令、带缩进的 python 都合法。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name>"
            "<tool_param>\nls -la\n  wc -l a.txt\n</tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), ("executeCmd", "ls -la\n  wc -l a.txt"))

    def test_only_the_first_call_is_taken(self):
        """只取第一条：`executeCmd` 只有一个字段，一回合只跑得了一条（接口文档 L210）。"""
        reply = (
            "<tool><tool_name>executeCmd</tool_name><tool_param>first</tool_param></tool>"
            "<tool><tool_name>executeCmd</tool_name><tool_param>second</tool_param></tool>"
        )
        self.assertEqual(tool_of(reply), ("executeCmd", "first"))

    def test_the_old_bare_shape_still_works(self):
        """**兼容层**（用户拍板全覆盖）：块里没有 `<tool_name>` ⇒ 整块正文就是一条命令。

        第 16 步的形状就是这个。判题器的 LLM 认不认新形状是**黑盒**，
        这是"不被认账"时唯一能让任务线继续跑下去的退路。
        ⚠️ **不加"含 `<` 就当畸形"的守卫**：那会误杀 `cat < input.txt`。
        """
        for reply in ("<tool>ls -la</tool>", "<tool  >  ls -la  </tool>", "<tool >ls -la</tool>"):
            with self.subTest(reply=reply):
                self.assertEqual(tool_of(reply), ("executeCmd", "ls -la"))

    def test_partial_markup_is_not_a_call(self):
        """半截的都不算 —— **不猜半个调用**，让调用方落到"重问"那一支。

        `cat /tmp/x` 这种没有 `<tool>` 的裸参数也不认（那只是普通文本）。
        """
        cases = (
            "<tool><tool_name>executeCmd</tool_name></tool>",  # 有名字没参数
            "<tool><tool_param>ls</tool_param></tool>",  # 有参数没名字
            "<tool></tool>",  # 空块
            "<tool>   </tool>",  # 只有空白
            "<tool>ls",  # 有开无闭
            "<tool><tool_name>executeCmd</tool_name>",  # 外层没闭合
            "<tool_name>executeCmd</tool_name><tool_param>ls</tool_param>",  # 没有外层
            "ls -la",  # 裸文本（那不是工具调用，是答案）
        )
        for reply in cases:
            with self.subTest(reply=reply):
                self.assertIsNone(tool_of(reply))

    def test_a_broken_block_still_counts_as_a_tool_reply(self):
        """⚠️ **`looks_like_tool` 宽、`tool_of` 严，这个差是承重的。**

        半截的工具回复既要"取不出命令"（`tool_of` 返回 `None`）又要"不能被当成答案"
        （`looks_like_tool` 返回真）—— 两个谓词里任何一个判反，都会出现
        "提问与提交同时哑火、永久空转、日志上什么都看不出来"（第 16 步踩过）。
        """
        for reply in ("<tool ls -la", "<tool>ls", "<tool_name>executeCmd</tool_name>"):
            with self.subTest(reply=reply):
                self.assertTrue(looks_like_tool(reply))
                self.assertIsNone(tool_of(reply))

    def test_a_plain_answer_is_not_a_tool_reply(self):
        for reply in ("晴 26 度", "<answer>晴 26 度</answer>", ""):
            with self.subTest(reply=reply):
                self.assertFalse(looks_like_tool(reply))


class AnswerParseTest(unittest.TestCase):
    """`answer_of`：**该提交什么**（`_answer_task` 与 `task_channel` 判据 ④/⑤ 共用的谓词）。"""

    def test_a_wrapped_answer_is_unwrapped(self):
        self.assertEqual(answer_of("<answer>晴 26 度</answer>"), "晴 26 度")

    def test_an_empty_block_does_not_fall_back_to_the_raw_text(self):
        """空块 ⇒ `""`（**不回落成原文**）。

        那一回合宁可不提交（判题器按"通过率最高的一份"算分，少交一次不扣分），
        也不要把 `<answer></answer>` 这串标签当成答案交上去。
        """
        self.assertEqual(answer_of("<answer></answer>"), "")
        self.assertEqual(answer_of("<answer>   </answer>"), "")

    def test_a_half_written_answer_is_not_submitted(self):
        """半截 `<answer>`（有开无闭 / 闭标签写错）⇒ `""` —— 不拿半截标记去凑答案。"""
        for reply in ("<answer>晴", "<answer>晴</answer", "<answer>晴<answer>"):
            with self.subTest(reply=reply):
                self.assertEqual(answer_of(reply), "")


    def test_a_tool_call_is_never_an_answer(self):
        """工具回复（新形状 / 旧形状 / 畸形）一个都不能当答案 —— 判**宽**（`looks_like_tool`）。

        用 `tool_of` 判就会漏掉畸形那种，然后它既不被提交、又不会被重问。
        """
        for reply in (
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            "<tool>ls -la</tool>",
            "<tool ls -la",
            "<tool_name>executeCmd</tool_name>",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(answer_of(reply), "")

    def test_bare_text_is_the_answer(self):
        """**兜底，逐字不变**（第 16 步的行为）：判题器的 LLM 是黑盒，
        它认不认 `<answer>` 我们没得选 —— 这是不被认账时唯一的退路。

        ⚠️ 孤立的 `</answer>` 也落在这里（判据认的是**开**标签，它一个都没有）。
        结果是把这串标签当答案交上去 —— 与任何一段普通文本同一条路径，
        代价是被判一次错（零成本，不扣分），而不值得为它加一条判据。
        """
        self.assertEqual(answer_of("晴 26 度"), "晴 26 度")
        self.assertEqual(answer_of("  晴 26 度\n"), "晴 26 度")
        self.assertEqual(answer_of("</answer>"), "</answer>")
        self.assertEqual(answer_of(""), "")


if __name__ == "__main__":
    unittest.main()
