"""game/planner.py **任务线**的用例：接取 / 被钉住 / `task_channel` 判据链
（两条通道互斥是唯一不变量）。

跑法：`PYTHONUTF8=1 py -m unittest discover -s tests -v`（单文件：`py tests/<本文件>`）。⚠️ 用 `py`——本地 `python` 是 3.7.1；不加 PYTHONUTF8 中文会乱码。
"""

import json
import sys
import unittest
from pathlib import Path

# tests/ 给 `_fixtures` 用（discover 不一定把它放进 sys.path）；src/ 给 coregeek 用
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _fixtures import _terrain  # noqa: E402
from coregeek.agent import AGENT, Agent  # noqa: E402
from coregeek.agent.chat import answer_of, looks_like_tool, tool_of  # noqa: E402
from coregeek.agent.tools import sop  # noqa: E402
from coregeek.game.grid import Pos, step_toward  # noqa: E402
from coregeek.game.map import Map  # noqa: E402
from coregeek.game.planner import plan, task_channel  # noqa: E402
from coregeek.game.roles import BaseRole, Pioneer, Worker  # noqa: E402
from coregeek.game.world import Error, Robot, Turn, Weapon  # noqa: E402
from coregeek.protocol import model  # noqa: E402


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
        """第一次提问 = **段模板（prompt.py 六段）+ 题目原文**，不带任何回灌。

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
            "# 【工具描述】",
            "# 【输出约定】",
            "# 【沉淀的SOP】",
            "# 【注意事项】",
        ):
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
        """工具调用里 `<tool_param>` 内层的 `<cmd>` **就是那条命令**，两侧空白去掉、内部原样保留。

        第 37 步起**只认嵌套形状**（用户拍板"严格只认新形状"）：旧形状（属性式 /
        裸参数 / 裸工具块）不再是命令 —— 它们落重问，见
        `test_the_old_shapes_fall_back_to_reasking`。
        多标签只取第一条：`executeCmd` 只有一个字段，一回合只跑得了一条（接口文档 L210）。
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
            f"{call('first')} 然后 {call('second')}": "first",
        }
        for reply, expected in cases.items():
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply)), ("", expected))

    def test_the_old_shapes_fall_back_to_reasking(self):
        """⚠️ **严格模式的降级方向**（第 37 步用户拍板）：旧形状既取不出命令、也不许被
        当成答案 ⇒ `tool_of` 给 `None`、`looks_like_tool` 给真 ⇒ **重问**。

        丢的是一回合（任务期间 prompt 不限量、不碰红线），换来的是解析只有一种形状。
        若哪一条被判成"取不出命令 = 这是答案"，就会出现**提问与提交同时哑火**
        （`_answer_task` 跳过工具回复）—— 那才是事故。
        """
        for reply in (
            "<tool>ls -la</tool>",
            '<tool><tool_name>executeCmd</tool_name><tool_param name="cmd">ls</tool_param></tool>',
            "<tool><tool_name>executeCmd</tool_name><tool_param>ls</tool_param></tool>",
        ):
            with self.subTest(reply=reply):
                prompt, execute = task_channel(self._turn(self.TASK, reply))
                self.assertEqual(execute, "")
                self.assertIn(self.TASK, prompt, "落重问、不是当答案")

    def test_a_broken_tool_tag_yields_no_command(self):
        """凑不齐的标签 ⇒ **没有命令可发**。别把半截标签当命令丢进沙盒。

        ⚠️ 后面几条是**隐式子路径 ③′**：调用是完整的，但工具给不出命令
        （`SOP2Prompt` / 未知工具 / 缺参数）。它们与"畸形"落同一个出口：**重问**。
        ⚠️ `SOP2Prompt` 那条的形状是**合法**的（第 37 步起声明 `name` + `sop`），
        "给不出命令"与"调用作废"由此分家：前者照旧重问，后者见
        `AgentToolCallTest.test_sop2prompt_stores_the_flow_and_yields_no_command`。
        """
        replies = (
            "<tool ls",
            "<tool>ls",
            "</tool>",
            "<tool></tool>",
            "<tool>  </tool>",
            "<tool><tool_name>executeCmd</tool_name></tool>",
            "<tool><tool_name>没这个工具</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>方法</name><sop>正文</sop></tool_param></tool>",
            "<tool><tool_name>SOP2Prompt</tool_name><tool_param><sop>正文</sop></tool_param></tool>",
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.assertEqual(task_channel(self._turn(self.TASK, reply))[1], "")

    def test_a_tool_that_yields_no_command_is_asked_again(self):
        """**③′：工具调用成了、但工具不产出命令 ⇒ 重问**（既不提交、也不发命令）。

        `SOP2Prompt` 是最典型的一个 —— 它**当回合没有别的回执**，而下一轮 prompt 里
        那段「沉淀的 SOP」就是"调用成功了"的凭证（它自己看得见）。
        ⚠️ 第 36 步起**沉淀与作答分家**：这条回复只沉淀、没作答 ⇒ **SOP 照样落库**
        （`assertEqual(AGENT.sop, …)` 那一行就是那件事），丢的只是那一回合（重问）。
        第 35 步那版把 `answer` 写成必需参数，于是这里曾经是"整次调用作废、SOP 不落库" ——
        代价是 LLM 把答案写成块外 `<answer>` 时 SOP **静默**丢掉（判据 ⑤ 命中 ⇒ 连重问都没有）。
        那是条死路：**"声明即必需"的参数没法同时又是一个可选的作答通道**。
        **"沉淀 + 同轮作答"那条路**见 `test_sinking_the_sop_rides_along_with_the_answer`。
        ⚠️ 这条路径**不会活锁**：任务期间 prompt 不限量不计数（接口文档 L198），
        而出口有"LLM 改口 / `code 2` 带来的纠错段 / 它看见 SOP 段"三条。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>SOP2Prompt</tool_name>"
                "<tool_param><name>找任务书</name><sop>先看目录</sop></tool_param></tool>",
            )
        )
        self.assertEqual(execute, "")
        self.assertIn(self.TASK, prompt, "没作答 ⇒ 这一回合还得问")
        self.assertIn("先看目录", prompt, "沉淀不许因为没作答就作废")
        self.assertEqual(AGENT.sop, {"找任务书": "先看目录"})

    def test_sinking_the_sop_rides_along_with_the_answer(self):
        """⚠️ **沉淀 SOP 不许独占一回合** —— 它单独来一趟就得重问一次，等于白花一回合。

        任务是**按回合计分**的（`5 × 标准回合数 / (完成回合 − 接取回合)`），所以白花一回合
        直接掉分。而代码侧本来就支持"同一条回复里既沉淀又作答"：工具块沉淀、**块外**的
        `<answer>` 作答（`tool_of` 只认第一个块、`answer_of` 先把它整段挖掉再扫）⇒
        `task_channel` 走**判据 ⑤**（`("", "")`，不是 ③′ → ⑥ 的重问）；同时 `plan` 里
        `_answer_task` 独立用**同一个谓词**取答案，当回合就 `submitAnswer`。
        ⇒ 唯一的阻塞是 prompt 措辞（那条用例见
        `ChatPromptTest.test_the_sop_round_must_carry_the_answer`）。

        ⚠️ 第 35 步曾把答案挪进**工具参数**（`<tool_param name="answer">`）来防 SOP 污染，
        **第 36 步整个作废**（用户口径："SOP 工具只有一个参数，提交统一走 `<answer>`"）——
        防污染改由两道闸门接管，见
        `test_a_literal_in_the_sop_does_not_poison_the_submitted_answer`。
        """
        reply = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>找文件</name><sop>先找文件</sop></tool_param></tool>"
            "\n<answer>晴 26 度</answer>"
        )
        AGENT.reset()
        self.assertEqual(
            task_channel(self._turn(self.TASK, reply)), ("", ""), "既不该重问、也不该发命令"
        )
        self.assertEqual(AGENT.sop, {"找文件": "先找文件"}, "沉淀照样生效")
        self.assertEqual(
            plan(self._turn(self.TASK, reply)).get("10011"),
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
            "同一个回合里开拓者已经把答案交上去了",
        )

    def test_a_literal_in_the_sop_does_not_poison_the_submitted_answer(self):
        """⚠️ **端到端的污染守门员**（第 35 步那个真实缺陷的正面防线）。

        SOP 正文里写着"答案要写成 `<answer>假答案</answer>` 的形状"—— 旧判据会把这个**示例**
        当成答案交上去，而日志上完全看不出来（任务行只打原文）。现在两道闸门各管一头：

        ① `answer_of` **先挖掉整个工具块** ⇒ 块内那些字面量够不着（交的是块外的 `晴 26 度`）；
        ② `SOP2Prompt` **入库前挖掉成对的 `<answer>` 段** ⇒ 存下来的 SOP 也不会带着这对串
        进后续每一份 prompt（`assertEqual(AGENT.sop, …)` 就是那件事）。

        反向验证：① 退成"扫整条回复" ⇒ 交的是 `假答案`（挂）；② 去掉 `strip_answers` ⇒
        存下来的 SOP 里带着 `假答案`（挂）。
        """
        reply = (
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>答题格式</name>"
            "<sop>答案要写成 <answer>假答案</answer> 的形状</sop></tool_param></tool>"
            "\n<answer>晴 26 度</answer>"
        )
        AGENT.reset()
        self.assertEqual(task_channel(self._turn(self.TASK, reply)), ("", ""))
        self.assertEqual(AGENT.sop, {"答题格式": "答案要写成  的形状"}, "入库的那份里不许留这对标签")
        self.assertEqual(
            plan(self._turn(self.TASK, reply)).get("10011"),
            {"action": "submitAnswer", "taskAnswer": "晴 26 度"},
            "交的是块外那份，不是 SOP 里的示例",
        )

    def test_the_stored_sop_rides_along_in_every_later_prompt(self):
        """**自进化的可观测证据**：存过一条流程之后，后面每一份 prompt 都带着它 ——
        包括"回灌沙盒结果"与"带纠错重问"这两条分支。"""
        AGENT.SOP2Prompt("找文件", "先 ls 再算")
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
        task_channel(
            self._turn(
                self.TASK,
                "<tool><tool_name>SOP2Prompt</tool_name>"
                "<tool_param><name>找任务书</name><sop>先看目录</sop></tool_param></tool>",
            )
        )
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
        messages = json.loads(prompt)
        self.assertEqual(messages[-2]["role"], "tool")
        self.assertIn(output, messages[-2]["content"])

    def test_the_reply_is_remembered_even_on_command_rounds(self):
        """**发命令那一轮也记回复**（第 25 步 `AGENT.hear` 的存在理由）：③ 那轮没有
        prompt，但它的工具调用必须进会话 —— 否则回灌那一轮 LLM 看见的是
        "题目 → 莫名其妙的结果"，它自己要的命令凭空消失。粘住的 `llmResp`
        顺带被去重（assistant 只出现一次）。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
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
                ("tool", "【上一条命令的执行结果（原文）】\n[exitCode:0]\n2"),
                (
                    "user",
                    "——请判断：以上输出是否已满足任务要求？若已满足，请直接提交答案，"
                    "不要再执行多余命令；若信息仍不足，请说明还缺什么，然后只执行下一步命令。",
                ),
            ],
        )

    def test_a_result_already_in_hand_blocks_the_next_command(self):
        """⚠️ **沙盒刚交作业这一轮，绝不能再发命令** —— 判据 2 必须压在判据 3 前面。

        动机是 `llmResp` **可能粘住**：文档给 `lastCmdResult` 写了"未发命令时为空字符串"
        （L33）、对 `llmResp` **一个字没写**（L31）。万一它还停在上轮那条 `<tool>…</tool>`
        上，判据 3 先命中就会**同一条命令反复丢进沙盒**。附带挡住"结果延迟两回合"。
        """
        prompt, execute = task_channel(
            self._turn(
                self.TASK,
                llm_resp="<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
                cmd_result="[exitCode:0]\nok",
            )
        )
        self.assertEqual(execute, "", "沙盒刚交作业，这轮不许再发命令")
        messages = json.loads(prompt)
        self.assertIn("[exitCode:0]\nok", messages[-2]["content"])

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
        messages = json.loads(prompt)
        self.assertIn("[exitCode:0]\n晴", messages[-3]["content"])
        users = [m["content"] for m in messages if m["role"] == "user"]
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
            self._turn(
                self.TASK,
                llm_resp="<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
                errors=(Error(2, "x"),),
            )
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
            #: SOP 正文里的字面量 `<answer>` 不算答案（第 36 步：工具块先整段挖掉），
            #: 真答案在块外 —— 这条同时钉"两个调用点都别去认块内那份"
            "<tool><tool_name>SOP2Prompt</tool_name>"
            "<tool_param><name>答题格式</name>"
            "<sop>答案写成 <answer>假答案</answer> 的形状</sop></tool_param></tool>"
            "\n<answer>晴 26 度</answer>",
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
                    #: 真说过的话），但骂的必须是**交上去的那一份**（`answer_of` 解包后的），
                    #: 而且后面挂着**判题器自己的原话**（第 35 步）。
                    users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
                    self.assertIn(
                        f"【你上一次提交的答案被判定为不正确】\n{expected}"
                        "\n【判题器反馈】：答案不正确\n请重新作答。",
                        users[-1],
                    )

    def test_the_verdict_rides_back_verbatim(self):
        """判题器说"哪里不对"的**原话**（`errorCode 2` 的 `description`）要回到 LLM 手里。

        第 35 步之前回灌的只有"我们自己上次交的答案" —— LLM 知道错了、**不知道错在哪**，
        只能把同一份答案再交一遍。那句话是黑盒里最接近"哪一项不对"的信息，
        只进日志不进 prompt 就等于白拿。

        ⚠️ **骂的必须与交的同源**：`why` 为空（判题器没给描述）时**整段不纠错**、
        退回普通重问 —— 判据 ④ 的定义（见
        `test_a_rejection_needs_an_answer_to_blame`），别把它做成独立分支。
        """
        prompt, _ = task_channel(
            self._turn(
                self.TASK,
                llm_resp=self.ANSWER,
                errors=(Error(2, "第 3 项应为整数"),),
            )
        )
        users = [m["content"] for m in json.loads(prompt) if m["role"] == "user"]
        self.assertIn(self.ANSWER, users[-1])
        self.assertIn("【判题器反馈】：第 3 项应为整数", users[-1])

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
                messages = json.loads(prompt)
                self.assertIn(output, messages[-2]["content"])
                self.assertEqual(execute, "")

    def test_prompt_and_command_are_never_both_set(self):
        """**两条通道互斥** —— 这是整个状态机唯一的不变量，遍历所有分支钉一遍。

        "同一轮既提问又发命令"会让 LLM 在没看到结果的情况下作答 ⇒ 又要一遍同一条命令
        ⇒ 活锁。它也是 `task_channel` 之所以合成一个函数、而不是 `prompt_for` +
        `execute_for` 的全部理由（拆开就要把这条链写两遍）。
        """
        call = (
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"
        )
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
            self._turn(self.TASK, "<tool><tool_name>SOP2Prompt</tool_name><tool_param><name>方法</name><sop>正文</sop></tool_param></tool>"),
            self._turn(self.TASK, "<tool><tool_name>未知</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>"),
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
            "<tool><tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param></tool>",
            "<tool>ls</tool>",
            "<tool>ls</tool>\n记住这个",
            "<tool ls",
            "<tool_name>executeCmd</tool_name><tool_param><cmd>ls</cmd></tool_param>",
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


if __name__ == "__main__":
    unittest.main()
