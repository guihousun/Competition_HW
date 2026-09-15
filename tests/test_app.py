"""app.py 的用例：`handle` 的红线退化（任何异常 ⇒ 合法空指令）、每回合复盘日志的版面与
字节预算，以及**任务线的端到端**（handle 串起 web/protocol/game/agent，是唯一的集成面）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import SAMPLE  # noqa: E402
from coregeek.agent import AGENT  # noqa: E402
from coregeek.agent.chat import answer_of  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.app import LOG_PROMPT_MAX, _clip, handle  # noqa: E402
from coregeek.protocol import actions, model  # noqa: E402
from coregeek.utils import LOG_TEXT_MAX  # noqa: E402


#: 官方样例（roundNo=85）的落点。**样例是夜里**，所以三个角色都朝最近的一座炮走一格：
#: 10010 在 (5,23) → (9,24)（切比雪夫 4）；10012 在 (10,16) → (9,24) 已被认领 ⇒ (10,25)（9）；
#: 10011 在 (10,12) → 前两座都被认领 ⇒ (9,25)（13）。夜里 `build`/`collect` 一条都不该有。
#: ⚠️ 这条断言**按设计改过两次**：第 7 步夜里是"朝石矿走"，第 8 步改成"回基地操炮"
#: （当时开拓者还不发指令），**第 10 步起开拓者也上炮位** ⇒ 两条变三条。
EXPECTED_MOVES = {"10010": [6, 22], "10012": [9, 17], "10011": [9, 13]}


#: 任务线那两条日志的**行首标记**。它们由 `planner` 打（不是 `coregeek.app`）——
#: 用例按标记取行，既不依赖日志顺序，也不依赖"哪张日志归哪个 logger"。
ASK = "【本轮提问】："


SANDBOX = "【CMD命令执行结果】："


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
        raw = json.loads(SAMPLE.read_text(encoding="utf-8"))
        #: 第 42 步起"没任务 + 有官方消息"会发**新闻查价** prompt —— 本类测的是日志
        #: 版面，样例自带的 worldNews 清掉，"没有新闻的回合"才是这几条的本意。
        raw["worldNews"] = {"officialNews": ""}
        with self.assertLogs("coregeek.app", level="INFO") as caught:
            self._handle(json.dumps(raw).encode("utf-8"))
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
        raw["worldNews"] = {"officialNews": ""}  # 没新闻 ⇒ 查价分支不触发（见第 42 步）
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
        raw["worldNews"] = {"officialNews": ""}  # 没新闻 ⇒ 尾行还是回执（提问行恒在最后）
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
        for piece in (
            "# 【ROLE定位】",
            "# 【工具描述】",
            "## ToolName - SOP2Prompt",
            "# 【沉淀的SOP】",
            "# 【注意事项】",
        ):
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
        """提问行单独截在 `LOG_PROMPT_MAX` —— 第 26 步起它是**唯一**还截断的一行
        （`LOG_TEXT_MAX` 已放宽到 40000）。

        prompt 是拼出来的（`prompt.py` 的段模板 + 会话往来），会话部分在任务行里已有全文，
        这一行只需要看得见模板头与「沉淀的SOP」那个槽 ⇒ 就近取 100000。
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
        #:（第 37 步起各段 strip 后拼接，content 直接以 `# 【ROLE定位】` 开头）
        self.assertTrue(
            asked.startswith('[{"role":"system","content":"# 【ROLE定位】'), asked[:40]
        )
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

        # ② LLM 要一条命令（嵌套形状）⇒ 命令进 `executeCmd`，prompt 槽捎上压缩请求
        #    （第 41 步：那个槽本来空着，正好拿来压缩上下文）
        raw["llmResp"] = (
            '<tool><tool_name>executeCmd</tool_name>'
            '<tool_param><cmd>python -c "print(1+1)"</cmd></tool_param></tool>'
        )
        body = ask()
        self.assertEqual(body["executeCmd"], 'python -c "print(1+1)"')
        self.assertIn("【上下文压缩】", body["prompt"])

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
                '<tool_param><cmd>python -c "print(1+1)"</cmd></tool_param></tool>',
            },
        )
        self.assertEqual(messages[3]["role"], "tool")
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
        #    **交上去的那一份**（`answer_of` 解包后的 `晴 26 度`），后面还挂着判题器
        #    自己的原话（第 35 步）—— 前者带标签的话 LLM 会以为自己交了一堆标签，
        #    去改一个并不存在的问题。
        raw["errors"] = [{"errorCode": 2, "description": "答案不正确"}]
        body = ask()
        messages = json.loads(body["prompt"])
        self.assertIn(
            "<answer>晴 26 度</answer>",
            [m["content"] for m in messages if m["role"] == "assistant"],
        )
        self.assertIn(
            "【你上一次提交的答案被判定为不正确】\n晴 26 度"
            "\n【判题器反馈】：答案不正确\n请重新作答。",
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
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
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


if __name__ == "__main__":
    unittest.main()
