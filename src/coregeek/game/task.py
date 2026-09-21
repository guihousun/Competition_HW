"""任务线：跟判题器的 LLM 说什么、接任务、交答案 —— 响应顶层 `(prompt, executeCmd)` 的唯一出产点。

`task_channel` 是一条判据链（②–⑥，先命中先定夺）+ 回合末尾两道闸门（压缩、沙盒探查），
逐条契约见 `strategy.md` §4。"跟 LLM 说什么、怎么解析回复"在 `coregeek/agent/`（包根那个
单实例 `AGENT`）—— 本模块只管**什么时候**开口，以及把工具返回的命令放进 `executeCmd`。

`acceptTask` / `submitAnswer` **仅开拓者**（§4.4）：`_emit` 里的权限闸门就是这道墙，越权
只丢这一条、不连坐同回合其他角色。
"""

import logging
from typing import Any

from ..agent import AGENT, cmd_explore  # 与 LLM 说什么不在策略层
from ..agent.chat import is_prices_reply, is_summary_reply, looks_like_tool, tool_of
from ..protocol import actions  # 指令只能经 Action 产出
from ..utils import _clip  # 日志的截断规则在叶子模块里
from .roles import BaseRole, Pioneer
from .core import _Queue, _emit
from .world import Turn

LOGGER = logging.getLogger(__name__)


def task_channel(turn: Turn) -> tuple[str, str]:
    """处理本回合任务的 `(prompt, executeCmd)` —— 响应顶层那两个字段的唯一来源。"""

    # 打印任务信息和模型回复的日志
    if turn.phase_task or turn.llm_resp:
        LOGGER.info(
            "【本轮任务】：%s ｜ 【上一轮模型回复】：%s",
            _clip(turn.phase_task) or "无",
            _clip(turn.llm_resp) or "无",
        )
    # 上回合若是探查命令，回执归探查收走（自己发的那条）；压在早返回之前 —— 不认领就粘住 `_waiting`
    result = cmd_explore.observe(turn.cmd_result)
    # 认任务边界：任务文本变了（含任务结束那个空轮）⇒ 这趟沙箱存档重开
    cmd_explore.new_task(turn.phase_task)
    # 打印CMD执行结果日志（探查自己那份不在这儿再抄一遍：取回时【沙盒探查】已留痕）
    if result:
        LOGGER.info("【CMD命令执行结果】：「%s」", _clip(result))

    llmReply = turn.llm_resp.strip()
    # 获取摘要
    summary = is_summary_reply(llmReply)
    # 价格影响信息
    prices = is_prices_reply(llmReply)
    sticky = False
    if summary is not None:
        # 摘要进 AGENT（不进会话表）
        AGENT.adopt_summary(summary)
        llmReply = ""
    elif prices is not None:
        # 价格期望进 AGENT（不进会话表）
        AGENT.adopt_price_hints(prices)
        llmReply = ""
    else:
        # 其余回复记进会话；`hear` 返回 False = 与上一条 assistant 同文（粘住，不是新话）
        sticky = not AGENT.hear(llmReply)

    # 没任务 ⇒ 问一次新闻查价（额度 3/日，指纹去重）；探查不发命令（沙盒仅任务期间可用）
    if not turn.phase_task:
        return AGENT.news_question(turn.news), ""

    # 任务回合，但没有开拓者参与 ⇒ 不发 prompt（任务线只在开拓者身上）；命令槽交给探查。
    if not any(isinstance(r, Pioneer) for r in turn.roles):
        return "", cmd_explore.next_command()
    # 解析任务答案（调了 submitAnswer 才有）
    answer = AGENT.submitted_answer(llmReply)
    # 解析工具调用
    calls = tool_of(llmReply)
    if calls is None and looks_like_tool(llmReply):
        # 想调工具但形状没写对（严格解析取不出名字）⇒ 记日志 + 说明回灌进会话，这轮落重问
        AGENT.reject_shape()
    command = AGENT.tool_calls(calls) if calls else ""  # 工具调度：副作用只发生在这一行
    # 判题器本轮报的"答案不对"（code 2）—— 判据 ④ 的触发条件；原话一并带回（黑盒里唯一
    # 能回答"错在哪一项"的东西）
    rejected = any(e.code == 2 for e in turn.errors)
    why = "；".join(e.description for e in turn.errors if e.code == 2 and e.description)

    # 构建错误信息提示
    retry = ""
    if rejected and answer:
        retry = f"{answer}\n【判题器反馈】：{why}" if why else answer
    elif rejected:
        retry = f"【判题器反馈】：{why}" if why else "（判题器未说明错在哪一项）"

    if command and not sticky:  # ③ 命令：这个字段归它（回执同轮到达也照发）；粘住的重发不算
        # （同一条命令已在沙盒里跑）。同轮有回执 ⇒ 先回灌进 prompt，两个字段各装各的
        prompt = AGENT.chat(turn.phase_task, result=result, retry=retry) if result else ""
        cmd = command
    elif result:  # ② 只有回执（或粘住的重发）⇒ 回灌结果、这轮不发 LLM 的命令
        prompt, cmd = AGENT.chat(turn.phase_task, result=result, retry=retry), ""
    elif retry:
        # 答错了 ⇒ 带纠错重问
        prompt, cmd = AGENT.chat(turn.phase_task, retry=retry), ""
    elif answer:
        # 拿到了答案 ⇒ 只交答案：不发模型请求、也不压缩
        prompt, cmd = "", ""
    else:
        # 首问：把题目问出去
        prompt, cmd = AGENT.chat(turn.phase_task), ""

    # 链尾压缩闸门：交卷轮不压缩（压缩会占住下一轮的回复槽），只剩命令轮会填上
    if not answer and prompt == "":
        prompt = AGENT.compression_request()
    # 链尾探查闸门：命令槽还空着 ⇒ 拿去摸沙箱环境（回执归探查自己收，不回灌）。
    # 例外：这轮 LLM 点名调了 `executeCmd` 却发不出命令（参数没给全 / 值空白）⇒
    # 槽空着也不给探查占（它才是这个字段的第一优先级）
    if cmd == "" and not any(name == "executeCmd" for name, _ in calls or []):
        cmd = cmd_explore.next_command()
    return prompt, cmd


# ── 开拓者的任务线 ──────────────────────────────────────────────────
def take_task(
    role: BaseRole, turn: Turn, q: _Queue
) -> None:
    """走到最近一个能接的任务点旁边，贴着就 `acceptTask`（`acceptTask` 没有昼夜限制，
    白天与夜里清场后的开拓者都走这里）。

    与 `build` / `collect` 同一条契约：任务点挡路，`step_toward` 天然停在贴着它的一格。
    一个能接的点都没有 ⇒ 什么都不发，不去蹲守。领到之后本函数进不来了（`planner` 两条链
    最前面那道分支先一步接管）。"""
    if not turn.task_points:
        return
    # "已经贴着就领"与"走过去"分两步判：合成一步会在站在两个任务点中间时舍近求远
    if min(role.pos.dist(p) for p in turn.task_points) <= 1:
        _emit(q.cmds, role, actions.AcceptTask)
        return
    # 并列按坐标排：先后不能取决于 payload 里的顺序，否则用例复现不了
    target = min(turn.task_points, key=lambda p: (role.pos.dist(p), p))
    q.step(role, target)


def answer_task(role: BaseRole, turn: Turn, cmds: dict[str, dict[str, Any]]) -> None:
    """服任务中：把手上的答案原样交上去。这里从来不移动（挪出去任务即作废）。

    答案 = `AGENT.submitted_answer(llmResp)`，与 `task_channel` 判据 ④ 骂的那份同源。空答案
    不发（可能被判成"字段缺失" = 指令非法，红线）。每回合都交（`llmResp` 粘住就自然重交）：
    判题器按"通过率最高的答案"算分。"""
    answer = AGENT.submitted_answer(turn.llm_resp)
    if answer:
        _emit(cmds, role, actions.SubmitAnswer, answer)
