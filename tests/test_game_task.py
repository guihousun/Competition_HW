"""game/task.py 任务线的用例：接取 / 被钉住 / `task_channel` 判据链
（两条通道互斥是唯一不变量）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _terrain  # noqa: E402
from coregeek.agent import AGENT, Agent, cmd_explore  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.game.grid import Pos  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.path import step_toward  # noqa: E402
from coregeek.game.planner import plan  # noqa: E402
from coregeek.game.task import task_channel  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Error, Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402

#: 沙盒探查那条命令的回执形状：`[exitCode:0]` + 每份一段 `@@@FILE <路径>@@@` 与正文 + 末尾剩余数
PROBE_RESULT = "[exitCode:0]\n@@@FILE /opt/task/rescue.md@@@\n# 任务\n正文\n@@@MORE 0@@@\n"


def _submit(answer: str) -> str:
    """交卷的工具块 —— 答案的唯一入口（`submitAnswer` 把参数值写进 `AGENT` 的答卷变量）。"""
    return (
        "<tool><tool_name>submitAnswer</tool_name>"
        f"<tool_param><answer>{answer}</answer></tool_param></tool>"
    )


class TaskAcceptTest(unittest.TestCase):
    """白天：开拓者走到最近一个能接的任务点旁边，贴着就 `acceptTask`。

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
        """贴着任务点 ⇒ `acceptTask`，报文里没有 `targetPos`
        （逐字对 `docs/response.txt` L51：`{"action":"acceptTask"}`）。"""
        cmds = plan(self._turn(self.P1, pioneer=Pos(13, 13)))
        self.assertEqual(cmds, {"10011": {"action": "acceptTask"}})

    def test_a_pioneer_two_cells_away_keeps_walking(self):
        """"周围一格"是切比雪夫 1（8 邻格、含对角）—— 差一格都不算贴着。"""
        cmds = plan(self._turn(self.P1, pioneer=Pos(12, 12)))
        self.assertEqual(cmds["10011"]["action"], "move")

    def test_no_ready_point_means_no_command(self):
        """一个能接的点都没有 ⇒ 待命（空指令合法且不计异常）。

        不走去冷却中的点蹲守：白天走过去、夜里回炮位、第二天再走过去，白白耗掉整天。
        """
        self.assertEqual(plan(self._turn()), {})

    def test_distance_ties_break_by_position(self):
        """并列时的先后不能取决于 payload 里的顺序，否则用例复现不了
        （与 `_defend` 的 `(dist, id)` 同一条理由）。"""
        left, right = Pos(18, 20), Pos(22, 20)
        cmds = plan(self._turn(right, left, pioneer=Pos(20, 20)))
        point = cmds["10011"]["targetPos"][0]
        step = Pos(point["x"], point["y"])
        self.assertLess(step.dist(left), step.dist(right), "同距离该挑坐标小的那个")


class TaskHoldTest(unittest.TestCase):
    """开拓者一旦领到任务就被钉死（任务书 L379：离开任务点周围一格内任务立即作废）。

    钉不住的话它会照旧走向炮位，任务当场作废，而本地全绿：报文挑不出毛病、
    "行为也正常"，只是任务一次都没做完。
    """

    DAY, NIGHT = 1, 85
    GUN = Pos(12, 25)
    REACH = 4  # 加特林 L1 的射程，取自样例 payload（任务书表格写的是 3）
    TASK = "请查询北京天气"

    def setUp(self) -> None:
        # 答卷变量是单实例上的状态：上一类用例交了一半的那份会在这里被"钉住 ⇒ 交答案"认领
        AGENT.take_answer()

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
        """人手够时夜里也不回炮位（`_short_handed` 为假）。

        开拓者已经贴着炮、射程内还有敌人 —— 不钉住的话它会开炮，而开炮要"走过去"，
        任务当场作废。判据是"没钉住的人够不够操满炮群"：两个工人 + 一座炮 ⇒ 够，
        开拓者一条指令都不发。`phaseTask` 一空（下面那条）同样的局面就必须开炮，两相对照。
        """
        turn = self._turn(
            Pioneer(10011, Pos(12, 24)), self.NIGHT, self.TASK, robots=(Robot(Pos(12, 27), 40),)
        )
        cmds = plan(turn._replace(roles=(Pioneer(10011, Pos(12, 24)), Worker(1, Pos(40, 40)), Worker(2, Pos(40, 41)))))
        self.assertNotIn("10011", cmds, "被钉着 ⇒ 一条指令都不发（含那条会作废任务的 move）")
        self.assertNotIn("10020", cmds, "那一炮不是它开的（`attack` 的 key 是武器 id）")

    def test_a_free_pioneer_still_mans_a_weapon_at_night(self):
        """"所有角色都操炮"不能被任务线吃掉 —— 与上一条（钉住的开拓者不发炮）对照。"""
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

    def _two_guns(self, *roles: BaseRole, round_no: int = NIGHT) -> Turn:
        """两座炮、各自成组（都不在 `weapon_sites` 上 ⇒ `_weapon_groups` 一人操一座）、
        开拓者手里有答案 —— "弃任务 / 钉住"两种结果在指令上分得开。"""
        turn = self._turn(Pioneer(10011, Pos(20, 20)), round_no, self.TASK)
        return turn._replace(
            roles=roles,
            weapons=(
                turn.weapons[0],
                Weapon(id=10021, kind="gatling", pos=Pos(9, 22), attack_range=self.REACH, cooldown=0),
            ),
            robots=(Robot(Pos(12, 27), 40),),
            llm_resp=_submit("晴 26 度"),
        )

    def test_a_dead_worker_at_night_pulls_the_pioneer_back_to_the_guns(self):
        """工人阵亡（只可能在夜里）⇒ 被任务钉死的开拓者也得弃任务回炮位（用户口径"生存第一"）。

        判据 = **没被任务钉住的角色数 < 武器组数**（一人只能操一组，少一个就有一组整夜空着）：
        场上只剩开拓者一个人、两座炮 ⇒ 0 < 2。不放开的话这一夜一门火力都没有，而本地全绿
        —— 报文合法、"行为也正常"，只是任务照做、炮没人操。
        """
        turn = self._two_guns(Pioneer(10011, Pos(20, 20)))
        task_channel(turn)  # 答卷变量在 `tool_calls` 那一行被写（与 `app.handle` 同序）
        cmds = plan(turn)
        self.assertEqual(list(cmds), ["10011"])
        self.assertEqual(cmds["10011"]["action"], "move", "弃任务 ⇒ 回炮位（这一回合先走一格）")

    def test_two_live_workers_keep_the_pioneer_on_its_task(self):
        """人手够（两个工人 + 两座炮）⇒ 开拓者照旧钉在任务上。

        与上一条对照：判据是"没钉住的人够不够操满炮"，不是"有没有工人死过" —— 2 < 2 不成立，
        两门炮都有人操，任务就该接着做。边界写成 `<=` 或只数工人都会在这里翻车。
        """
        turn = self._two_guns(
            Pioneer(10011, Pos(20, 20)), Worker(1, Pos(12, 24)), Worker(2, Pos(9, 21))
        )
        task_channel(turn)
        cmds = plan(turn)
        self.assertEqual(cmds["10011"]["action"], "submitAnswer", "钉住 ⇒ 交答案、不挪窝")

    def test_a_pinned_pioneer_never_leaves_for_the_post_in_the_day(self):
        """白天放人也不许弃任务：白天不能开火、回炮位没有意义（`_short_handed` 恒假）。

        夜里人手不够与白天收工是两回事。收工闸门恰好在这条用例的回合号上允许回程
        （`within=69` ⇒ 白天只剩 2 回合），漏掉昼夜那一半就会把开拓者从任务上拽走。
        """
        turn = self._two_guns(Pioneer(10011, Pos(30, 30)), round_no=69)
        task_channel(turn)
        cmds = plan(turn)
        self.assertEqual(cmds["10011"]["action"], "submitAnswer", "白天照旧钉着")


class TaskChannelTest(unittest.TestCase):
    """任务线的对外通道：`task_channel` 的判据链 + `submitAnswer` 那条独立的线。

    两条通道各判各的：`task_channel` 产出响应顶层的 `prompt` / `executeCmd`（对判题器的 LLM
    与它的沙盒），`plan` 里的 `answer_task` 产出 `submitAnswer`（对判分）。所以"这一轮在提问"
    与"这一轮在提交"可以同时成立，那是有利的（接口文档 L140 取"通过率最高"，重交零成本）。
    两者的交汇点是答卷变量：`task_channel` 在派工具那一行写下它、`answer_task` 取走它 ——
    所以"一回合"= `_round`：先 `task_channel`、后 `plan`（与 `app.handle` 同序）。
    """

    def setUp(self) -> None:
        # SOP 是单实例上的跨回合状态，不清就会跨用例串味。
        AGENT.reset()
        # 沙盒探查同理（状态在模块里），而且它每回合都会把空着的命令槽吃掉 ⇒ 先隔离、再静音；
        # 要看它的用例自己 `reset()` 打开。落盘目录不动：这些用例一条文件都不取。
        cmd_explore.reset()
        cmd_explore.mute()

    DAY = 1
    TASK = "请查询北京天气"
    ANSWER = "晴 26 度"
    #: 交卷的那条回复（答案经 `submitAnswer` 的参数进来）—— `ANSWER` 是它里面的值
    ANSWER_REPLY = _submit(ANSWER)
    #: 沉淀请求的回复（裸 `<sop>` 块，第 144 步）—— 交卷轮的下一轮落进暂存表
    SOP_REPLY = "<sop><name>找任务书</name>先看目录</sop>"
    #: 回灌那两段的分界符，用来断言"该出现 / 不该出现"。拿分界符而不是某句话当判据：模板
    #: 正文里也有"沙盒的执行结果原文"这种普通词、会把自己绊倒；而 `【` 整个模板里一个都没有，
    #: 只属于注入的两段 —— 沙盒输出与 LLM 回复都是任意文本，没分界符就分不清哪段是题目。
    RESULT_MARK = "【上一条命令的执行结果"
    RETRY_MARK = "【你上一次提交的答案"

    def _turn(
        self,
        phase_task: str = "",
        llm_resp: str = "",
        cmd_result: str = "",
        errors: tuple[Error, ...] = (),
        roles: tuple[BaseRole, ...] = (Pioneer(10011, Pos(13, 13)),),
        news: str = "",
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
            news=news,
        )

    def _round(self, turn: Turn) -> dict:
        """这一回合交上去的指令：`task_channel` 先（工具在这里派出去、答卷变量在这一行被写）、
        `plan` 后（`answer_task` 取走那份答卷）—— 与 `app.handle` 同序。"""
        task_channel(turn)
        return plan(turn)

    def _material(self, prompt: str) -> list:
        """沉淀请求的原料（= 会话全文）解成消息表。

        原料是 `json.dumps` 出来的一层、后面还粘着 `SOP_TAIL` 那一句（不是 JSON）⇒
        `raw_decode` 从串首解一条、不管尾巴。
        """
        content = json.loads(prompt)[-1]["content"]
        messages, _ = json.JSONDecoder().raw_decode(content)
        return messages

    def _body(self, prompt: str) -> str:
        """原料里全部正文拼成的明文，用来判"某段话在不在会话里"（直接查原文会被
        转义过的换行绊倒 —— `\\n`）。"""
        return "\n".join(m["content"] for m in self._material(prompt))

    def test_the_question_carries_the_task_text(self):
        """第一次提问 = 段模板（prompt.py 六段）+ 题目原文，不带任何回灌。

        断言用 `assertNotIn` 而不是"等于生成函数的返回值"：后者是同义反复
        （模板与断言一起改，永远过得去），而"第一次问不该有任何回灌"才是真要求。
        """
        prompt, execute = task_channel(self._turn(self.TASK))
        self.assertIn(self.TASK, prompt)
        self.assertEqual(execute, "")
        self.assertNotIn(self.RESULT_MARK, prompt)
        self.assertNotIn(self.RETRY_MARK, prompt)
        for header in (
            "# 【ROLE定位】",
            "# 【工作原则】",
            "# 【工具描述】",
            "# 【沉淀的SOP】",
            "# 【注意事项】",
        ):
            self.assertIn(header, prompt)

    def test_the_news_question_is_asked_when_idle(self):
        """没任务 + 有官方消息 ⇒ 发新闻查价 prompt（任务线之外的 3 次/日额度）。
        判据链①的"两个都不发"在此开口子。"""
        prompt, execute = task_channel(self._turn(news="北部铁矿区塌方，明日停工"))
        self.assertIn("【市场情报】", prompt)
        self.assertIn("北部铁矿区塌方", prompt)
        self.assertEqual(execute, "", "沙盒仅任务期间可用，新闻轮不发命令")

    def test_the_same_news_is_asked_only_once(self):
        """同一份 news 用指纹去重 —— 额度 3 次/日，重复问是白烧。"""
        task_channel(self._turn(news="北部铁矿区塌方"))
        prompt, _ = task_channel(self._turn(news="北部铁矿区塌方"))
        self.assertEqual(prompt, "")

    def test_a_prices_reply_is_routed_into_hints(self):
        """查价回复（裸 `<prices>` 块）⇒ 进价格期望表、不进会话表、当轮不再重问。
        与裸摘要同一条路由纪律。"""
        AGENT.reset()
        task_channel(self._turn(news="北部铁矿区塌方"))
        prompt, _ = task_channel(
            self._turn(news="北部铁矿区塌方", llm_resp="<prices>iron up\\ncopper flat</prices>")
        )
        self.assertEqual(prompt, "", "回复轮不重问（指纹已记）")
        self.assertEqual(AGENT.price_hint("iron"), 2.0)
        self.assertEqual(AGENT.price_hint("copper"), 1.0)

    def test_a_python_exec_round_feeds_the_output_back_at_once(self):
        """本地计算：LLM 调 `python_exec` ⇒ 当回合执行、产出直接进
        下一份 prompt（tool 消息），不走 `executeCmd`（那要一整个沙盒往返）。
        判据链落 ③′→⑥：调用成了、但不产命令。

        prompt 停在产出上、不补「请继续。」（第 111 步）：尾巴是 tool 消息 ⇒ `Context.nudge`
        不落（与沙盒回执那一轮的判据 ② 同形）。
        """
        AGENT.reset()
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>python_exec</tool_name>"
                "<tool_param><code>print(6*7)</code></tool_param></tool>",
            )
        )
        self.assertEqual(execute, "", "本地计算不产命令")
        self.assertIn("【本地 python 的执行结果", prompt)
        self.assertIn("42", prompt)
        self.assertNotIn("请继续。", prompt, "产出已经在尾巴上，不用再催一句")
        self.assertEqual(json.loads(prompt)[-1]["role"], "tool")

    def test_nothing_is_sent_without_a_task(self):
        """不在任务里一次都不发（`prompt` 也不行、`executeCmd` 更不行）。

        `prompt`：每游戏日只有 3 次 LLM 额度（接口文档 L198），那是任务线之外的资源。
        `executeCmd`：接口文档 L208 写明沙盒"仅在执行任务期间才能使用"。
        """
        for llm_resp in ("", self.ANSWER_REPLY, "<tool>rm -rf /</tool>"):
            with self.subTest(llm_resp=llm_resp):
                self.assertEqual(
                    task_channel(self._turn(llm_resp=llm_resp)), ("", ""),
                )

    def test_a_dead_pioneer_never_touches_the_sandbox(self):
        """开拓者阵亡 ⇒ 两个通道都停，哪怕 `phaseTask` 还没清干净。

        `submitAnswer` 走 `roleCommandMap`，阵亡的人不在 `model._character` 给出的 `roles` 里
        ⇒ `_answer_task` 天生进不来；但 `executeCmd` 是响应顶层字段，不经过角色循环、也不经过
        `Action` 的权限闸门 ⇒ 那条白送的闸门对它不存在，得手写补上。
        """
        self.assertEqual(
            task_channel(self._turn(self.TASK, "<tool>ls</tool>", roles=())), ("", ""),
        )

    def test_no_question_once_the_llm_answered(self):
        """回复调了 `submitAnswer` ⇒ 不提问、也不压缩 —— 压缩回复会占住下一轮的 llmResp 槽，
        答案被判错时纠错分支就拿不到答案原文（第 47 步）。

        第 141 步起那个空出来的 prompt 槽归**沉淀请求**（交卷触发 SOP 沉淀；判题器在任务
        期间不计数 ⇒ 这次开口不吃额度）。它不是任务提问、也不是压缩请求，三种各有各的段头。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问：会话从这道题开始
        prompt, execute = task_channel(self._turn(self.TASK, self.ANSWER_REPLY))
        self.assertEqual(execute, "")
        self.assertTrue(
            json.loads(prompt)[0]["content"].startswith("# 【SOP 沉淀】"),
            "交卷轮：prompt 槽 = 沉淀请求",
        )
        self.assertNotIn("【上下文压缩】", prompt, "交卷轮不压缩（第 47 步）")

    def test_the_deposit_rides_every_submission(self):
        """沉淀的触发条件只有"这一轮调了 `submitAnswer`"：回执到没到、判题器报没报错，都不
        影响它当轮发出去（用户口径：不能在收到回执的时候才进行沉淀 —— 那要把它推到后面
        某一轮，白占一个 LLM 回合）。

        反向验证：给这道闸门加回"判题器没报错"那个条件，最后两格必挂。
        """
        cases = (
            ("", ()),  # 干净的交卷轮
            ("[exitCode:0]\nok", ()),  # 回执同轮到达
            ("[exitCode:0]\nok", (Error(2, "答案不正确"),)),  # 回执与判错一起来
            ("", (Error(2, "答案不正确"),)),
        )
        for i, (result, errors) in enumerate(cases):
            with self.subTest(i=i):
                AGENT.reset()  # 每格独立会话：上一格的纠错段别串进下一格的原料里
                task_channel(self._turn(self.TASK))  # ⑥ 首问
                prompt, execute = task_channel(
                    self._turn(self.TASK, self.ANSWER_REPLY, cmd_result=result, errors=errors)
                )
                self.assertEqual(execute, "")
                self.assertTrue(
                    json.loads(prompt)[0]["content"].startswith("# 【SOP 沉淀】"),
                    "交卷 ⇒ 沉淀请求当轮发出",
                )

    def test_the_deposit_reply_lands_even_after_the_task_ended(self):
        """沉淀回复回来时任务往往已经结束（答对了 ⇒ 下一轮 `phaseTask` 空了）——
        那个 `<sop>` 块照样得落库并转正，而命令槽一个字节都不发。

        落库压在"没任务"早返回之前就是为这一轮（第 141 步）。丢的后果不是报错，是沉淀
        悄悄作废：判题器不评它对错、日志上任务行也照打，只有"沉淀率永远是 0"这一个症状。
        反向验证：把落库放回"没任务"早返回之后，这条必挂。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # 交卷轮（没报错 ⇒ 答对了）
        prompt, execute = task_channel(
            self._turn("", "<sop><name>查天气的流程</name>先问接口再读 temperature</sop>")
        )
        self.assertEqual(
            AGENT.sop, {"查天气的流程": "先问接口再读 temperature"}, "任务结束之后也照收"
        )
        self.assertEqual(prompt, "", "没任务 ⇒ 不问：沉淀是单向广播，回复下一轮顺手吃掉")
        self.assertEqual(execute, "", "沙盒仅任务期间可用 ⇒ 命令一律丢弃")

    def test_the_command_comes_out_of_the_tool_markup(self):
        """工具调用里 `<tool_param>` 内层的 `<cmd>` 就是那条命令，两侧空白去掉、内部原样保留。

        只认嵌套形状（旧形状落重问，见 `test_the_old_shapes_fall_back_to_reasking`）。
        """
        def call(cmd: str) -> str:
            return (
                "<tool><tool_name>executeCmd</tool_name>"
                f"<tool_param><cmd>{cmd}</cmd></tool_param></tool>"
            )

        cases = {
            call("ls -la"): "ls -la",
            f"  {call('  ls -la  ')}  ": "ls -la",
            call('python -c "print(1)"\nprint(2)'): 'python -c "print(1)"\nprint(2)',
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply)), ("", expected))

    def test_two_commands_in_one_reply_void_the_round(self):
        """一条回复里两个工具 ⇒ 整轮作废（一条命令都不发、落重问）。

        用户口径：一个回合只调一个工具（第 144 步删掉了"沉淀 + 交卷"那个白名单 —— 沉淀
        已经不是工具了）。`executeCmd` 一回合只跑得了一条（接口文档 L210），挑一条发等于
        替 LLM 做选择。
        """
        def call(cmd: str) -> str:
            return (
                "<tool><tool_name>executeCmd</tool_name>"
                f"<tool_param><cmd>{cmd}</cmd></tool_param></tool>"
            )

        prompt, execute = task_channel(
            self._turn(self.TASK, f"{call('first')} 然后 {call('second')}")
        )
        self.assertEqual(execute, "", "两条都别发")
        self.assertIn(self.TASK, prompt, "落重问（会话里另有那条'整轮不成立'的说明）")

    def test_the_old_shapes_fall_back_to_reasking(self):
        """严格模式的降级方向：旧形状既取不出命令、也不许被当成答案 ⇒ 重问。

        丢的是一回合（任务期间 prompt 不限量、不碰红线），换来的是解析只有一种形状。若哪一条
        被判成"取不出命令 = 这是答案"，提问与提交就会同时哑火（`_answer_task` 跳过工具回复）。
        每条都留一行「形状没写对」（它是"这一轮又白丢了"的唯一线索）。
        """
        with self.assertLogs(level="INFO") as caught:
            for reply in (
                "<tool>ls -la</tool>",
                '<tool><tool_name>executeCmd</tool_name><tool_param name="cmd">ls</tool_param></tool>',
                "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            ):
                with self.subTest(reply=reply):
                    prompt, execute = task_channel(self._turn(self.TASK, reply))
                    self.assertEqual(execute, "")
                    self.assertIn(self.TASK, prompt, "落重问、不是当答案")
        misses = [r.getMessage() for r in caught.records if "形状没写对" in r.getMessage()]
        self.assertEqual(len(misses), 3)

    def test_a_failed_call_is_told_why_in_the_next_prompt(self):
        """「调用不成立」不再是黑洞（第 110 步）：缺参数 / 未知工具 / 形状没写对 ⇒
        落重问的那份 prompt 里带一条"这次调用没有发出去 + 原因"（tool 消息，跟在
        LLM 自己那条回复后面）。没有它，这一轮它什么新东西都看不到，会以为命令已在跑、
        等一个不会来的回执，逐字重发同一条失败调用就原地空转。

        说明本身就是"该你改了"的指令 ⇒ prompt 停在它上面、不补「请继续。」（第 111 步）。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问：会话从这道题开始
        for reply in (
            "<tool><tool_name>executeCmd</tool_name></tool>",  # 缺参数
            "<tool><tool_name>查不到的工具</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<tool>ls -la</tool>",  # 旧形状：连工具名都取不出
        ):
            with self.subTest(reply=reply):
                prompt, execute = task_channel(self._turn(self.TASK, reply))
                self.assertEqual(execute, "")
                self.assertIn("【工具调用：这次调用不成立】", prompt)
                self.assertNotIn("请继续。", prompt)
                self.assertEqual(json.loads(prompt)[-1]["role"], "tool")

    def test_a_broken_tool_tag_yields_no_command(self):
        """凑不齐的标签 ⇒ 没有命令可发。别把半截标签当命令丢进沙盒。

        后面几条是隐式子路径 ③′：调用完整、工具给不出命令（未知工具 / 缺参数），与"畸形"
        落同一个出口：重问。沉淀那条回复（裸 `<sop>` 块）压根不是工具调用，但它同样不产
        命令 —— 一起钉在这里。
        """
        replies = (
            "<tool ls",
            "<tool>ls",
            "</tool>",
            "<tool></tool>",
            "<tool>  </tool>",
            "<tool><tool_name>executeCmd</tool_name></tool>",
            "<tool><tool_name>没这个工具</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<sop><name>方法</name>正文</sop>",
            "<sop>只有正文没有名字</sop>",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply))[1], "")

    def test_a_tool_that_yields_no_command_is_asked_again(self):
        """③′：工具调用成了、但工具不产出命令 ⇒ 重问（既不提交、也不发命令）。

        `python_exec` 最典型：它的产出当回合就进会话（`tool` 消息），当回合没有别的回执。
        丢的只是那一回合 —— 这条路径不会活锁：任务期间 prompt 不限量不计数（接口文档
        L198），出口有"LLM 改口 / 纠错段 / 会话末尾那条产出"三条。
        "沉淀块 + 同轮作答"见 `test_sinking_the_sop_rides_along_with_the_answer`。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问（本地工具的产出进会话，会话得先在）
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>python_exec</tool_name>"
                "<tool_param><code>1+1</code></tool_param></tool>",
            )
        )
        self.assertEqual(execute, "")
        self.assertIn(self.TASK, prompt, "没作答 ⇒ 这一回合还得问")
        self.assertIn("【本地 python 的执行结果（原文）】", prompt, "产出当回合进会话")

    def test_sinking_the_sop_rides_along_with_the_answer(self):
        """一条回复里同时有沉淀块与交卷 ⇒ 两个都不丢：答案当回合就交上去，块照存。

        两条通道互不干扰是结构性的：`<sop>` 是裸块（`sops_of` 解）、`submitAnswer` 是工具
        （`tool_of` 解），一个不产命令、另一个也不产命令。第 141 步起 prompt **不再教**这个
        形状（沉淀搬到交卷之后独立的一轮）⇒ 这条钉的是那条退路，不是被教出来的主路径。
        """
        reply = "<sop><name>找文件</name>先找文件</sop>\n" + _submit("晴 26 度")
        AGENT.reset()
        task_channel(self._turn(self.TASK))  # ⑥ 首问（会话开着才测得到"该不该重问"）
        prompt, execute = task_channel(self._turn(self.TASK, reply))
        self.assertEqual(execute, "", "两个通道都不产命令")
        self.assertEqual(AGENT.pre_sop, {"找文件": "先找文件"}, "沉淀照样落地（进暂存表）")
        self.assertTrue(
            json.loads(prompt)[0]["content"].startswith("# 【SOP 沉淀】"),
            "不再是重问：交卷 ⇒ 链尾那道沉淀闸门接管 prompt 槽（第 141 步）",
        )
        self.assertEqual(
            plan(self._turn(self.TASK, reply)).get("10011"),
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
            "同一个回合里开拓者已经把答案交上去了",
        )

    def test_the_sop_body_keeps_its_literal_answer_tags(self):
        """SOP 正文里写着 `<answer>…</answer>` 不再需要任何清洗 —— 它只是正文。

        旧通道（`answer_of` 扫原文、`strip_answers` 入库前挖标签）整套删掉之后，"答案"只从
        `submitAnswer` 的 `answer` 参数里来，正文里出现什么都不可能被当成答案交上去。
        这条是反向验证：谁再把"扫回复原文"那套加回来，这里立刻挂。
        """
        reply = (
            "<sop><name>答题格式</name>答案要写成 <answer>假答案</answer> 的形状</sop>\n"
            + _submit("晴 26 度")
        )
        AGENT.reset()
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        _, execute = task_channel(self._turn(self.TASK, reply))
        self.assertEqual(execute, "")
        self.assertEqual(
            AGENT.pre_sop, {"答题格式": "答案要写成 <answer>假答案</answer> 的形状"}, "逐字入库"
        )
        self.assertEqual(
            plan(self._turn(self.TASK, reply)).get("10011"),
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
            "交的是 answer 参数里那份，不是 SOP 里的示例",
        )

    def test_the_stored_sop_rides_along_in_every_later_prompt(self):
        """自进化的可观测证据：转正之后，后面每一份 prompt 都带着它 ——
        包括"回灌沙盒结果"与"带纠错重问"这两条分支。

        走完整的三回合（首问 → 交卷 → 沉淀回复 + 干净判决）才转正：交卷轮那一份 prompt 是
        沉淀请求，不算"后来的提问"。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # ⑤ 交卷 + 链尾发沉淀请求
        task_channel(self._turn(self.TASK, self.SOP_REPLY))  # 沉淀落地 + 判决干净 ⇒ 转正
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"}, "回执干净 ⇒ 转正")
        for turn in (
            self._turn(self.TASK),
            self._turn(self.TASK, cmd_result="[exitCode:0]\nok"),
            self._turn(self.TASK, errors=(Error(2, "x"),), llm_resp=self.ANSWER_REPLY),
        ):
            with self.subTest(turn=turn):
                self.assertIn("先看目录", task_channel(turn)[0])

    def test_the_singleton_carries_the_sop_across_turns(self):
        """单实例的接线证据：这道题转正的 SOP，换一道题之后还在 prompt 里带着。

        两回合之间没有任何东西被传过去（上一回合的 `llm_resp` 没进 payload、也没有返回值被
        接收 —— `task_channel` 的返回值由 `app` 直接拼进报文），能接起来的只有"两次调用用的是
        同一个 `AGENT`"。所以它是 `from ..agent import AGENT` 那个注入点的守门员：改回"每次新建
        一个 `Agent()`"这里立刻挂（实盘症状 = 永远学不会，而日志上看不出来：SOP 每回合都是空的）。
        """
        task_channel(self._turn(self.TASK))
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))
        task_channel(self._turn(self.TASK, self.SOP_REPLY))  # 转正
        prompt, execute = task_channel(self._turn("另一道题"))
        self.assertEqual(execute, "")
        self.assertIn("先看目录", prompt)

    def test_a_broken_tool_reply_is_asked_again(self):
        """半截工具调用要落回"重问"，不能两边都哑火。

        `<tool` 有开无闭 ⇒ 取不出命令、判据 3 不命中；若再按"取不出命令 = 这是答案"落到判据 5，
        就会 `("", "")`，而 `_answer_task` 又跳过工具回复 ⇒ 提问与提交同时哑火；`llm_resp` 若
        粘住就永久空转，日志上什么都看不出来（"提问：无 ｜ 提交：有"看着完全正常）。
        """
        prompt, execute = task_channel(self._turn(self.TASK, "<tool ls -la"))
        self.assertEqual(execute, "")
        self.assertIn(self.TASK, prompt)

    def test_the_sandbox_result_goes_back_verbatim(self):
        """沙盒输出全文回灌（不截断），而且这一轮绝不发命令。"""
        output = "[exitCode:0]\n" + "y" * 5000
        prompt, execute = task_channel(self._turn(self.TASK, cmd_result=output))
        self.assertEqual(execute, "")
        messages = json.loads(prompt)
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertIn(output, messages[-1]["content"])

    def test_an_idle_command_slot_carries_the_sandbox_probe(self):
        """命令槽空着的回合全部拿去摸沙箱环境（问模型这一轮就是），prompt 那半边照旧是题目。"""
        cmd_explore.reset()
        prompt, execute = task_channel(self._turn(self.TASK))
        self.assertIn(self.TASK, prompt)
        self.assertEqual(execute, cmd_explore._command())

    def test_a_deposit_reply_leaves_the_command_slot_to_the_probe(self):
        """沉淀回复不产命令 ⇒ 那个空槽照旧归探查（"空槽填满"的第三条落点：交卷轮、压缩轮、
        沉淀轮各占 prompt 槽，命令槽一直由探查接）。任务还在时才是这一支 —— 任务结束了就没
        探查这回事（`test_the_deposit_reply_lands_even_after_the_task_ended`）。"""
        cmd_explore.reset()
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        _, execute = task_channel(self._turn(self.TASK, "<sop><name>读题</name>先读题</sop>"))
        self.assertEqual(execute, cmd_explore._command())
        self.assertEqual(AGENT.pre_sop, {"读题": "先读题"}, "沉淀照样落进暂存表")

    def test_the_deposit_is_staged_until_the_verdict_clears(self):
        """沉淀先进暂存表、判题器没报错才转正（用户口径）：交卷的下一轮判决干净 ⇒ 当场转正；
        带 `code 2` ⇒ 压着，等下一次交卷之后没报错的那一轮；换题 ⇒ 压着的作废。

        只看 system（`json.loads(prompt)[0]`）：会话往来里带着 LLM 自己写的那段沉淀原文，
        整份 prompt 查分不出"SOP 段里有没有它"。
        反向验证：把 `settle_deposit` 的判据放宽成"交了卷就转正"，第二段必挂。
        """
        # 主路径：交卷 → 沉淀回复（判决干净）⇒ 落地即转正
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # ⑤ 交卷 + 链尾发沉淀请求
        task_channel(self._turn(self.TASK, self.SOP_REPLY))  # 沉淀落地 + 判决干净
        self.assertEqual(AGENT.pre_sop, {}, "转正后暂存清空")
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"})
        system = json.loads(task_channel(self._turn(self.TASK))[0])[0]["content"]
        self.assertIn("## SopName - 找任务书\n先看目录", system, "转正之后任务 prompt 里看得见")

        # 判错 ⇒ 落地那一轮压着，不进 SOP 段
        AGENT.reset()
        task_channel(self._turn(self.TASK))
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))
        prompt, _ = task_channel(self._turn(self.TASK, self.SOP_REPLY, errors=(Error(2, "x"),)))
        self.assertEqual(AGENT.pre_sop, {"找任务书": "先看目录"}, "没转正 ⇒ 留在暂存表")
        self.assertEqual(AGENT.sop, {})
        self.assertNotIn("先看目录", json.loads(prompt)[0]["content"], "暂存的进不了 SOP 段")

        # 再交一次卷、下一轮判题器没报错 ⇒ 压着的那条照样转正（不会永远压着）
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))
        task_channel(self._turn(self.TASK))
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"}, "干净回执一到就结账")
        self.assertEqual(AGENT.pre_sop, {})

        # 换题 ⇒ 没转正的作废（转正过的照旧在）
        AGENT.deposit("另一条", "只在暂存")
        task_channel(self._turn("另一道题"))
        self.assertEqual(AGENT.pre_sop, {}, "没转正的沉淀不跨题")
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"})

    def test_an_unrelated_verdict_error_does_not_block_the_promotion(self):
        """`accepted` 只看 `code 2`（答案不对）—— 别的码（如 4 指令错误）不挡转正。

        判据在 `task_channel` 这一侧（码表的含义不进 `Agent`）。写成"`turn.errors` 非空就不算成"
        的话，一次无关的报错会把这道题的沉淀白压一轮 —— 而沙盒超时、指令执行失败那一类
        报错在长任务里是常态。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # ⑤ 交卷
        task_channel(self._turn(self.TASK, self.SOP_REPLY, errors=(Error(4, "指令错误"),)))
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"}, "非 2 的码不挡转正")
        self.assertEqual(AGENT.pre_sop, {})

    def test_a_late_deposit_still_lands_after_the_verdict(self):
        """沉淀回复晚几轮才回来也照样转正 —— 这是"粘性待判标志"的全部意义。

        交卷轮发出去的那条请求，回复可能掉在后面任何一轮（模型先接着干活、或者干脆隔一轮
        才答）。只盯"交卷的下一轮"会把它整条错过：`_submitted` 交过卷就挂着，等到暂存表里
        真有东西可转正才结账。
        反向验证：把 `<sop>` 落库与 `settle_deposit` 挪到"没任务"早返回之后，最后一段必挂
        （答对了的那一轮 `phase_task` 已经空了）。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # ⑤ 交卷 + 链尾发沉淀请求
        task_channel(self._turn(self.TASK, cmd_result="[exitCode:0]\nok"))  # 判决干净、没沉淀
        self.assertEqual(AGENT.sop, {}, "还没东西可转正")
        # 回复晚到，而且落在题目已经空掉的那一轮（答对了、判题器不再发任务）
        task_channel(self._turn("", self.SOP_REPLY))
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"}, "任务结束那一轮也照收")

    def test_an_unusable_call_keeps_the_slot_from_the_probe(self):
        """LLM 点名调了 `executeCmd` 却发不出命令（参数没给全 / 值是空白）⇒ 槽空着也不给探查占。

        用户口径：这个字段的第一优先级是 LLM 那条调用。两种情形都让位 —— `tool_of` 认得名字，
        `tool_call` 因"声明参数一个不少且非空"拒收。代价只是少探一趟（探查每回合都重走）。
        形状整条没写对（`tool_of` 为 None）时名字取不到，那就让不了这个位：放宽解析去猜参数名
        是另一码事（`test_the_old_shapes_fall_back_to_reasking` 钉着"落重问"）。
        """
        for reply in (
            "<tool><tool_name>executeCmd</tool_name></tool>",  # 缺参数
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>  </cmd></tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                cmd_explore.reset()
                task_channel(self._turn(self.TASK))  # ⑥ 首问（会话从这道题开始）
                _, execute = task_channel(self._turn(self.TASK, reply))
                self.assertEqual(execute, "", "这个槽归它，探查让位")

        cmd_explore.reset()
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
            )
        )
        self.assertEqual(execute, cmd_explore._command(), "形状没写对 ⇒ 取不到名字，探查照发")

    def test_the_probed_paths_reach_the_next_prompt(self):
        """探查的**产出**从下一轮起现挂在 `readSandboxFile` 的描述里 —— 命令槽那条边只出命令，
        清单走的是 `Agent.chat` 每轮现刷 system 这条路（与 SOP 段同源，第 104 步从独立一段
        挪进工具块）。

        一轮都不落下：发命令那轮 prompt 里还没有（回执这轮才回来），认领之后立刻就有。
        """
        cmd_explore.reset()
        first, _ = task_channel(self._turn(self.TASK))
        self.assertIn("## ToolName - readSandboxFile", first, "工具块恒在（第 107 步）")
        self.assertNotIn("/opt/task", first, "回执还没回来，不能凭空断言沙盒里有什么")
        prompt, _ = task_channel(
            self._turn(self.TASK, cmd_result=PROBE_RESULT)
        )
        self.assertIn("## ToolName - readSandboxFile", prompt)
        self.assertIn("- /opt/task/rescue.md，rescue.md", prompt)

    def test_the_inventory_stays_and_the_new_task_walks_the_sandbox_again(self):
        """一趟存档 = 一道题（第 113 步，用户口径"k 个任务启动 k 次"）：**走完就停**，
        换任务（含"任务结束"那个空轮）**重开一趟**；**探明的成果只累积不清**。

        三件事缺一不可：只留清单不重走 = 新沙盒的文件永远发现不了；只重走不留 = 每道题都得从零
        再摸一遍；不停 = 同一个沙盒被反复摸（白跑，虽然槽本来是空的）。
        """
        cmd_explore.reset()
        task_channel(self._turn(self.TASK))
        prompt, _ = task_channel(self._turn(self.TASK, cmd_result=PROBE_RESULT))
        self.assertIn("/opt/task/rescue.md", prompt, "先真探出一条，否则下面全空过")
        _, execute = task_channel(self._turn(self.TASK))
        self.assertEqual(execute, "", "这道题的沙盒摸完了 ⇒ 空槽不再占（`MORE 0` ⇒ `_done`）")
        task_channel(self._turn(news="北部铁矿区塌方"))  # 任务结束这一轮
        self.assertEqual(cmd_explore.known_paths(), ["/opt/task/rescue.md"], "探明的成果留着")
        again, execute = task_channel(self._turn(self.TASK))
        self.assertIn("/opt/task/rescue.md", again, "上个任务的路径照旧带进新任务")
        self.assertEqual(execute, cmd_explore._command(), "新任务从头上再走一趟沙盒")

    def test_no_task_means_no_probe(self):
        """没任务 ⇒ 一条都不发：`executeCmd` 文档说它"仅在执行任务期间才能使用"。"""
        cmd_explore.reset()
        self.assertEqual(task_channel(self._turn(news="北部铁矿区塌方"))[1], "")

    def test_our_own_result_is_not_fed_back_and_does_not_block_the_llm_command(self):
        """两本账一轮一条交替跑：探查的回执不回灌（判据 ② 不命中），LLM 那条命令照发。

        漏掉"认领"这半边，回灌分支会把 LLM 的命令整轮挤掉 —— 探查队列排空前它一条都发不出去。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        cmd_explore.reset()
        task_channel(self._turn(self.TASK))  # 问模型那轮：题目走 prompt，槽给探查去列清单
        prompt, execute = task_channel(
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\n  420 /opt/task/x.md\n")
        )
        self.assertEqual(execute, "ls", "探查的回执不该把 LLM 的命令挤掉")
        self.assertNotIn(self.RESULT_MARK, prompt, "探查的回执不回灌给 LLM")

    def test_the_reply_is_remembered_even_on_command_rounds(self):
        """发命令那一轮也记回复（`AGENT.hear` 的存在理由）：③ 那轮没有 prompt，但它的工具
        调用必须进会话 —— 否则回灌那轮 LLM 看见的是"题目 → 莫名其妙的结果"，它自己要的命令
        凭空消失。粘住的 `llmResp` 顺带被去重（assistant 只出现一次）。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        task_channel(self._turn(self.TASK))  # ⑥ 首问（会话从这道题开始）
        prompt, execute = task_channel(self._turn(self.TASK, call))  # ③ 发命令（prompt=压缩请求）
        self.assertEqual(execute, "ls")
        self.assertIn("【上下文压缩】", prompt, "命令轮的 prompt 槽捎上压缩请求（第 41 步）")
        prompt, execute = task_channel(
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\n2")  # ② 回灌（llmResp 粘住）
        )
        self.assertEqual(execute, "")
        self.assertEqual(
            [(m["role"], m["content"]) for m in json.loads(prompt)][1:],
            [
                ("user", self.TASK),
                ("assistant", call),
                ("tool", "【上一条命令的执行结果（原文）】\n[exitCode:0]\n2"),
            ],
        )

    def test_the_compression_material_is_the_original_context(self):
        """压缩原料 = 原始上下文全文：窗口外的旧回合原文仍在压缩请求里
        —— 压缩总从原文重来、不从旧摘要叠（避免多次压缩的失真累积）。给任务 LLM 的
        才是压缩后的（摘要 + 窗口）。"""
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(self._turn(self.TASK, call))  # ③ 命令轮 a1
        task_channel(self._turn(self.TASK, call, cmd_result="[exitCode:0]\n1"))  # ② 回灌
        task_channel(self._turn(self.TASK, call, cmd_result="[exitCode:0]\n1"))  # ③ a2（粘住）
        task_channel(self._turn(self.TASK, call, cmd_result="[exitCode:0]\n1"))  # ② 回灌
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>pwd</cmd></tool_param></tool>",
                # `lastCmdResult` 已清（上一轮是回灌、没发命令）—— 否则判据 ② 先命中
            )
        )
        self.assertEqual(execute, "pwd")
        self.assertIn("【上下文压缩】", prompt)
        self.assertIn("pwd", prompt, "最近一条工具调用在原料里")
        # 此刻窗口只盖最近 2 轮 assistant —— 但原料是原文，更早的往来照样在
        self.assertIn("ls", prompt, "窗口外的旧回合原文仍在压缩原料里（原文永久保留）")

    def test_a_bare_summary_reply_is_routed_not_heard(self):
        """裸 `<summary>` 回复 = 压缩轮的产物：进 `Context.summary`、不进会话表
        （它不是 LLM 在任务上说过的话，进表会污染窗口、与【历史摘要】双份），
        任务判据按"没回复"走 —— 下一轮判据 ② 照常回灌结果。"""
        AGENT.reset()
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        task_channel(self._turn(self.TASK))
        task_channel(self._turn(self.TASK, call))  # ③：命令 + 压缩请求同发
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<summary>【总目标】交 token</summary>",  # 压缩回复（随 cmd_result 一起到）
                cmd_result="[exitCode:0]\n2",
            )
        )
        self.assertEqual(execute, "")
        self.assertIn("【总目标】交 token", prompt, "摘要已就位")
        contents = [m["content"] for m in json.loads(prompt)]
        self.assertNotIn(
            True, ["<summary>" in c for c in contents], "裸摘要不进会话表"
        )

    def test_a_summary_reply_round_goes_back_to_the_task(self):
        """压缩回复到达、又没有别的回执 ⇒ 判据按"没回复"走 → ⑥ 重问。

        压缩只跟在 ③ 命令轮后面（交卷轮不压缩，两件事互斥）⇒ 命令轮与压缩轮交替。
        这一轮不是压缩轮 —— nudge 是模型请求、闸门不落；摘要照样进（`【历史摘要】` 可见）。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            )
        )  # ③ 命令轮（prompt = 压缩请求）
        prompt, execute = task_channel(
            self._turn(self.TASK, "<summary>【总目标】交 token</summary>")
        )
        self.assertEqual(execute, "")
        self.assertNotIn("【上下文压缩】", prompt, "nudge 是模型请求，闸门不落")
        self.assertIn("请继续。", prompt)
        self.assertIn("【总目标】交 token", prompt, "摘要已进【历史摘要】")

    def test_a_fresh_call_owns_the_slot_even_when_a_result_arrives(self):
        """回执与命令同轮到达 ⇒ 两个字段各归各的：结果进 prompt、命令照发。

        用户口径：LLM 调了 `executeCmd` ⇒ 这个字段归它，它才是第一优先级。旧口径在这一轮把
        命令整个丢掉（判据 2 压在 3 前），症状是 LLM 要的命令永远跑不了、下一轮也没有回执。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        task_channel(self._turn(self.TASK))  # ⑥ 首问：会话从这道题开始
        prompt, execute = task_channel(
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\nok")
        )
        self.assertEqual(execute, "ls", "命令归 `executeCmd` 字段")
        self.assertIn(self.RESULT_MARK, prompt, "结果照样回灌（渲染里带着回执原文）")
        self.assertIn("[exitCode:0]\nok", json.loads(prompt)[-1]["content"])

    def test_a_sticky_reply_never_runs_the_same_command_twice(self):
        """粘住的 `llmResp` ⇒ 同一条命令不再发第二遍（旧口径"判据 2 压在 3 前"防的就是这个）。

        `lastCmdResult` 文档写了"未发命令时为空字符串"（L33），`llmResp` 一个字没写（L31）⇒
        它还停在上轮那条 `<tool>…</tool>` 上、而沙盒正跑着它 ⇒ 再发一遍就是同一条命令跑两次。
        判据是"这条回复我们上一轮已经行动过了"（`Agent.hear` 只看最后一条消息：中间隔了回执
        或重问 ⇒ 算又说了，照发 —— 命令因此不会被永久压住）。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        task_channel(self._turn(self.TASK))  # ⑥ 首问
        prompt, execute = task_channel(self._turn(self.TASK, call))  # ③ 命令轮：跑起来了
        self.assertEqual(execute, "ls")
        prompt, execute = task_channel(
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\nok")
        )
        self.assertEqual(execute, "", "同一条命令已经在沙盒里跑了")
        self.assertIn("[exitCode:0]\nok", json.loads(prompt)[-1]["content"], "结果照样回灌")

    def test_a_result_and_a_rejection_come_back_together(self):
        """沙盒结果与"答错了"是同一个分支的两面，不能互相吞掉。

        `errors` 说的是本轮产生的错误，与我们上轮发了什么并不同步：第 R 轮发命令（那轮没有
        `prompt`）⇒ 第 R+1 轮 `cmd_result` 非空，而 `code 2` 说的是更早那次 `submitAnswer` 的
        判决 —— 两者同时命中是真实可达的。把纠错做成独立分支就会把它整段吞掉，所以它是修饰符。

        第 142 步起这一轮的 prompt 槽归沉淀请求（调了 `submitAnswer` 就发）⇒ 结局是"两样都
        落进会话"，断言因此下移到沉淀请求的原料（`Context.material` = 会话全文）上。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                llm_resp=self.ANSWER_REPLY,
                cmd_result="[exitCode:0]\n晴",
                errors=(Error(code=2, description="答案不正确"),),
            )
        )
        self.assertEqual(execute, "")
        self.assertTrue(
            json.loads(prompt)[0]["content"].startswith("# 【SOP 沉淀】"), "交卷轮 ⇒ 沉淀请求"
        )
        body = self._body(prompt)
        self.assertIn("[exitCode:0]\n晴", body, "回执照样进会话")
        self.assertIn(self.ANSWER, body)
        self.assertIn(self.RETRY_MARK, body, "纠错块也进会话")

    def test_a_rejection_blames_even_when_the_answer_is_not_in_hand(self):
        """判据 ④ 不要求手上真有那个答案：判题器给了错误提示就要装进提示词，绝不落「请继续。」

        反馈-only 的重问里没有答案文本，无从踩"拿带标签的原文当答案骂"的雷；会话里最后一条
        assistant 正是它上一次说过的话，反馈紧跟着落。仍不骂的只剩取得了命令的工具回复 ——
        它在判据 3 就走了（有 error 2 也不该妨碍"该跑的命令照跑"；这轮反馈落空没关系，
        code 2 会连着报几轮）。
        """
        nobody_to_blame = task_channel(self._turn(self.TASK, errors=(Error(2, "x"),)))
        self.assertIn(self.RETRY_MARK, nobody_to_blame[0], "反馈照样装进提示词（第 47 步）")

        broken = task_channel(
            self._turn(self.TASK, llm_resp="<tool ls", errors=(Error(2, "x"),))
        )
        self.assertIn(self.RETRY_MARK, broken[0], "半条命令不妨碍把反馈带到")

        # 独立会话：这轮走 ③ ⇒ prompt 是压缩请求，而原料是原始上下文全文 —— 不清会话的话
        # 上面两个 case 的纠错块会被原料带出来，"不骂"就断言不出来了。
        AGENT.reset()
        replied_a_command = task_channel(
            self._turn(
                self.TASK,
                llm_resp="<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
                errors=(Error(2, "x"),),
            )
        )
        self.assertNotIn(self.RETRY_MARK, replied_a_command[0])
        # 顺带钉住：有 error 2 也不该妨碍"该跑的命令照跑"（判据 3 排在判据 4 前面）
        self.assertEqual(replied_a_command[1], "ls")

    def test_the_retry_blames_exactly_what_we_submitted(self):
        """纠错段里带的必须是"我们交上去的那一份"（`answer` 参数的值），不是回复原文。

        交的是参数里的 `晴 26 度`、骂的却是整个 `<tool>…</tool>` 块的话，LLM 会以为
        自己交了一堆标签、去改一个并不存在的问题。两处（`answer_task` 提交、判据 ④ 回灌）
        读的是同一个答卷变量就是为了这件事，这条用例把它钉死：骂的 = 交的。

        第 142 步起这一轮（交卷 + 判错）的 prompt 槽归沉淀请求 ⇒ 纠错段整段原文在会话里、
        随原料交出去，断言的落点跟着下移一格。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                llm_resp=self.ANSWER_REPLY,
                errors=(Error(2, "答案不正确"),),
            )
        )
        self.assertEqual(execute, "")
        self.assertEqual(
            self._material(prompt)[-1]["content"],
            f"【你上一次提交的答案被判定为不正确】\n{self.ANSWER}"
            "\n【判题器反馈】：答案不正确\n请重新作答。",
            "纠错段整段原文：骂的就是 `answer` 参数里那一份",
        )

    def test_the_two_call_sites_agree_on_what_the_answer_is(self):
        """该交的字节由测试自己写死 —— 交付（`plan` → `answer_task`）与它被骂时回灌的
        （判据 ④）必须是同一份。

        两处在判题器那侧是同一件事："你上次答的 X 不对"里的 X 就是我们上次交的。第 136 步起
        这件事是**结构性**的（`submitAnswer` 写答卷变量、`task_channel` 读它、`answer_task`
        取走它），表里右边那一列因此由测试自己填、不借生产代码算期望值。
        """
        cases = [
            (_submit("晴 26 度"), "晴 26 度"),
            (_submit("  晴 26 度  "), "晴 26 度"),  # 值两侧空白由 `tool_of` 去掉
            ("晴 26 度", ""),  # 裸文本：不再是答案（旧通道的"原文即答案"已删）
            ("<answer>晴 26 度</answer>", ""),  # 旧标签通道同样不再是答案
            (_submit("晴 26 度") + "\n补充一句", "晴 26 度"),  # 块外的话不进答案
            # 沉淀块搭车：答案只认 `submitAnswer` 那块的参数（`<sop>` 走另一条通道）
            (
                "<sop><name>答题格式</name>先看目录</sop>\n" + _submit("晴 26 度"),
                "晴 26 度",
            ),
            # 两个工具 ⇒ 整轮作废：`submitAnswer` 根本没被派到
            (
                "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>\n"
                + _submit("晴 26 度"),
                "",
            ),
            ("<tool>ls</tool>", ""),
            ("", ""),
        ]
        for reply, expected in cases:
            with self.subTest(reply=reply):
                submitted = self._round(self._turn(self.TASK, reply))
                if expected:
                    self.assertEqual(
                        submitted,
                        {"10011": {"action": "submitAnswer", "taskAnswer": expected}},
                    )
                else:
                    self.assertEqual(submitted, {}, "空/工具的回复不该被交上去")
                if expected:
                    AGENT.reset()  # 会话是跨回合状态：不清的话上一条 subTest 的回复会进这条的 prompt
                    prompt = task_channel(
                        self._turn(self.TASK, llm_resp=reply, errors=(Error(2, "答案不正确"),))
                    )[0]
                    # 钉纠错块整块原文：会话里可以有原来那条工具调用的 assistant 消息（那是它
                    # 真说过的话），但骂的必须是交上去的那一份（`answer` 参数的值），后面挂着判题器原话。
                    self.assertIn(
                        f"【你上一次提交的答案被判定为不正确】\n{expected}"
                        "\n【判题器反馈】：答案不正确\n请重新作答。",
                        self._body(prompt),
                    )

    def test_the_verdict_rides_back_verbatim(self):
        """判题器说"哪里不对"的原话（`errorCode 2` 的 `description`）要回到 LLM 手里。

        只回灌"我们自己上次交的答案"的话，LLM 知道错了、不知道错在哪，只能把同一份答案再交
        一遍。那句话是黑盒里最接近"哪一项不对"的信息，只进日志不进 prompt 就等于白拿。
        这一轮（交卷 + 判错）的 prompt 槽归沉淀请求（第 142 步）⇒ 原话落在会话里、随原料
        交出去 —— 落点与 `test_the_retry_blames_exactly_what_we_submitted` 同一条。
        """
        prompt, _ = task_channel(
            self._turn(
                self.TASK,
                llm_resp=self.ANSWER_REPLY,
                errors=(Error(2, "第 3 项应为整数"),),
            )
        )
        material = json.loads(prompt)[-1]["content"]
        self.assertIn(self.ANSWER, material)
        self.assertIn("【判题器反馈】：第 3 项应为整数", material)

    def test_only_the_answer_error_triggers_the_retry(self):
        """只有 `code 2`（答案不正确）才重问。1 与 5 是终局、3/4 重问也救不回来。

        交卷轮的 prompt 槽归链尾那道沉淀闸门（判据 ⑤ 只交答案）⇒ 判"重问没重问"看**会话里
        有没有那段纠错**（`feed` 出来的 user 消息，随沉淀请求的原料交出去），而不是查整份
        prompt —— 原料是会话全文，整份查的话非 2 的码也会被上一轮的残留带出假阳性。
        """
        task_channel(self._turn(self.TASK))  # ⑥ 首问：会话从这道题开始
        prompt, execute = task_channel(
            self._turn(self.TASK, llm_resp=self.ANSWER_REPLY, errors=(Error(2, "x"),))
        )
        self.assertEqual(execute, "")
        self.assertIn(self.RETRY_MARK, self._body(prompt), "code 2 才纠错")
        for code in (0, 1, 3, 4, 5):
            with self.subTest(code=code):
                # 每格独立会话：纠错段留在会话里，下一格会被原料带出来（假阳性）
                AGENT.reset()
                task_channel(self._turn(self.TASK))
                prompt, execute = task_channel(
                    self._turn(self.TASK, llm_resp=self.ANSWER_REPLY, errors=(Error(code, "x"),))
                )
                self.assertEqual(execute, "")
                self.assertTrue(
                    json.loads(prompt)[0]["content"].startswith("# 【SOP 沉淀】"),
                    "非 2 的码不重问：prompt 槽归沉淀闸门",
                )
                self.assertNotIn(self.RETRY_MARK, self._body(prompt), "非 2 的码不纠错")

    def test_the_error_feedback_enters_the_prompt_even_without_the_answer(self):
        """答案轮之后判题器报 `code 2`、而这轮回复里拿不到答案原文 ⇒ 错误反馈照样装进提示词，
        绝不落成一句「请继续。」—— 那等于没告诉它答案错了。

        `llmResp` 文档没写 ⇒ 必须按可能不粘设计。会话里最后一条 assistant 正是它上一次的答案，
        反馈紧跟着落，语义完整；判题器没给 `description` 时，纠错块的外壳（"被判定为不正确／
        请重新作答"）自己就是事实陈述，占位一句即可。
        """
        task_channel(self._turn(self.TASK, self.ANSWER_REPLY))  # ⑤ 交卷轮：只交答案（prompt 空）
        prompt, execute = task_channel(
            self._turn(self.TASK, errors=(Error(2, "第 3 项应为整数"),))
        )
        self.assertIn(self.RETRY_MARK, prompt)
        self.assertIn("第 3 项应为整数", prompt, "判题器的 description 原话进了提示词")
        self.assertNotIn("请继续", prompt)
        self.assertEqual(execute, "")
        prompt, _ = task_channel(self._turn(self.TASK, errors=(Error(2, ""),)))
        self.assertIn(self.RETRY_MARK, prompt)
        self.assertIn("不正确", prompt, "纠错块外壳自带的事实陈述还在")
        self.assertNotIn("请继续", prompt)

    def test_a_timeout_result_still_goes_back(self):
        """`[TIMEOUT]` / `[JUDGER_ERROR]` 打头的回执照样原样回灌 —— 那是沙盒侧的
        失败，让 LLM 自己看着重试（自进化任务的主要恢复路径）。

        我们不解析那行状态：解析它就是又多一份会跟判题器漂移的真相。
        """
        for output in ("[TIMEOUT]\n部分输出", "[JUDGER_ERROR]\n沙盒挂了", "[exitCode:127]\nno"):
            with self.subTest(output=output):
                prompt, execute = task_channel(self._turn(self.TASK, cmd_result=output))
                messages = json.loads(prompt)
                self.assertIn(output, messages[-1]["content"])
                self.assertEqual(execute, "")

    def test_a_command_never_rides_with_a_task_question(self):
        """命令轮的 prompt 只能是空、压缩请求、或**本轮回执的回灌** —— 不许是凭空的提问。

        提问若单独与命令同轮（没有本轮新到的回执），LLM 会拿着过期结果作答 ⇒ 又要一遍同一条
        命令 ⇒ 活锁。回灌那一路不算：它带着本回合刚到的回执原文，是"该它答的那一轮"，不是
        无中生有地又问一遍；同轮的命令也归 `executeCmd` 字段（用户口径：它才是第一优先级）。
        沉淀请求（交卷轮那份）到不了这里：它要 `answer` 非空，而那需要同一轮里调了
        `submitAnswer`，而"两个工具同轮 ⇒ 整轮作废"（第 144 步）⇒ 有命令的那一轮 `answer`
        必空，两者互斥。所以白名单还是三项，别顺手加第四项。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
        turns = [
            self._turn(),
            self._turn(self.TASK),
            self._turn(self.TASK, self.ANSWER_REPLY),
            self._turn(self.TASK, "晴 26 度"),  # 裸文本：既不产命令也不交卷
            self._turn(self.TASK, call),
            self._turn(self.TASK, "<tool>ls</tool>"),
            self._turn(self.TASK, "<tool ls", cmd_result="[exitCode:0]\nok"),
            self._turn(self.TASK, call, cmd_result="[exitCode:0]\nok"),  # 发过命令又拿到结果
            self._turn(self.TASK, cmd_result="[TIMEOUT]\n…"),
            self._turn(self.TASK, self.ANSWER_REPLY, errors=(Error(2, "x"),)),
            self._turn(self.TASK, "<tool ls", errors=(Error(2, "x"),)),
            # ③′：工具调用成了但工具不产出命令（含沉淀那条会写状态的）
            self._turn(self.TASK, "<sop><name>方法</name>正文</sop>"),
            self._turn(self.TASK, "<tool><tool_name>未知</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"),
            self._turn(self.TASK, "<tool><tool_name>executeCmd</tool_name></tool>"),
        ]
        for i, turn in enumerate(turns):
            with self.subTest(i=i):
                # 这道题问的是"命令与提问不同轮"，探查不在这条契约里（它就是在空槽里发命令的）
                # ⇒ `setUp` 里已经静音。
                prompt, execute = task_channel(turn)
                if execute:
                    self.assertTrue(
                        prompt == ""
                        or "【上下文压缩】" in prompt
                        or "[exitCode:0]" in prompt,  # 本轮回执的回灌（i=7）
                        f"命令轮的 prompt 只能是空、压缩请求或本轮回执：{prompt[:80]}",
                    )

    def test_the_answer_is_submitted_verbatim(self):
        """`answer` 参数的值原文进、原文出（逐字对 `docs/response.txt` L54）：
        我们不知道判题器要什么格式，加工只会引入自己的假设。"""
        cmds = self._round(self._turn(self.TASK, self.ANSWER_REPLY))
        self.assertEqual(cmds, {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}})

    def test_the_answer_is_the_param_value_alone(self):
        """交上去的字节 = `answer` 参数里那一段，只有两侧空白被去掉。

        多字段结构化答案（通过率按"字段个数"算）落在这里：交上去的字节里不能混进任何脚手架
        （工具标签、参数名、块外的补充说明），但答案自己的换行与内部空白一律原样保留。
        """
        cases = {
            _submit("  晴 26 度  "): "晴 26 度",
            _submit("城市 北京\n温度 26"): "城市 北京\n温度 26",
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(
                    self._round(self._turn(self.TASK, reply)),
                    {"10011": {"action": "submitAnswer", "taskAnswer": expected}},
                )

    def test_a_malformed_submit_answer_submits_nothing(self):
        """调用不成立（缺参数 / 值是空白）⇒ 这一回合不提交。

        不回落成回复原文是有意的：交一串光秃秃的标签只会被判一次错。判题器取"通过率最高的
        一份"（接口文档 L140）⇒ 少交一次不扣分，而不交永远比交错的好。
        """
        for reply in (
            "<tool><tool_name>submitAnswer</tool_name></tool>",  # 一个参数都没给
            _submit("   "),  # 值是空白 ⇒ 与缺参数同一条闸门
            "<tool><tool_name>submitAnswer</tool_name><tool_param>晴 26 度</tool_param></tool>",
            "<tool><tool_name>submitAnswer</tool_name><tool_param><answer>晴</tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                self.assertEqual(self._round(self._turn(self.TASK, reply)), {})

    def test_a_blank_reply_is_not_submitted(self):
        """空白回复不是答案 —— 空答案不发，那可能被判成"字段缺失"，
        正是红线里的"指令非法"。"""
        for reply in ("", "   ", "\n"):
            with self.subTest(reply=reply):
                self.assertEqual(self._round(self._turn(self.TASK, reply)), {})

    def test_a_tool_call_is_never_submitted_as_an_answer(self):
        """没交卷的回复绝不能当答案交上去。

        `answer_task` 交的就是 `task_channel` 判据 ⑤ 读的那一个变量 —— 一边当命令、一边当答案
        就是第二份真相。结构上也堵死了：答案只从 `submitAnswer` 的 `answer` 参数里来，工具调用
        怎么畸形都写不进它。交上去的话，那条命令会被判题器当成一次错误答案（`errorCode 2`）。
        """
        replies = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<tool>ls</tool>",
            "<tool>ls</tool>\n记住这个",
            "<tool ls",
            "<tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param>",
            # 白名单外的并列 ⇒ 整轮作废，连里面那块 `submitAnswer` 也不作数
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>\n"
            + _submit("晴 26 度"),
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(self._round(self._turn(self.TASK, reply)), {})

    def test_the_answer_is_resubmitted_every_round(self):
        """同一份 payload 连打几次都要交 —— 这不是 bug，是无状态设计的正面确认。

        接口文档 L140：「以之前提交过的通过率最高的答案计算积分与金币」——
        判题器专门为"反复交、取最好"设计了这个字段。别把它"优化"成"只交一次"：
        那要记住交没交过，而卡住的状态会静默关掉整条任务线。答卷变量不留痕（取走即清），
        重交靠 `llmResp` 粘住时把同一条 `submitAnswer` 再派一遍 —— 零新状态。
        """
        turn = self._turn(self.TASK, self.ANSWER_REPLY)
        for _ in range(3):
            self.assertEqual(
                self._round(turn), {"10011": {"action": "submitAnswer", "taskAnswer": self.ANSWER}}
            )

    def test_a_stale_answer_dies_at_the_round_boundary(self):
        """答卷变量只活一回合：上一回合没能交出去的那份，这一回合不再算数。

        交不出去的只剩一种情形 —— 夜里一个工人都没有（`_short_handed`）：开拓者被派回炮位、
        `answer_task` 不被调到，那份答卷就留在变量里。不清的话下一个回合会拿它当**这一回合
        的**答案：判据 ⑤ 以为已经答过（于是不再提问），而开拓者一旦重新入环，它会把这句
        上一道题的话**提交给另一道题**。
        """
        # 第一回合：夜里一个工人都没有 ⇒ 开拓者弃任务回炮位，答卷没人取走（留在变量里）
        night = self._turn(self.TASK, self.ANSWER_REPLY)._replace(round_no=85)
        self.assertEqual(
            [c for c in self._round(night).values() if c.get("action") == "submitAnswer"],
            [],
            "弃任务那一回合不交卷",
        )
        # 第二回合（白天）：回复换了一句不产命令的话 —— 上一份答卷不许补交、也不许顶掉提问
        day = self._turn(self.TASK, "晴 26 度")
        prompt, _ = task_channel(day)
        self.assertIn(self.TASK, prompt, "这一回合没有新答卷 ⇒ 判据 ⑥ 照常提问")
        self.assertEqual(plan(day), {}, "上一回合那份答卷在这里作废，不许补给这一回合")



if __name__ == "__main__":
    unittest.main()
