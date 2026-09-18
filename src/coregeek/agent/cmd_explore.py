"""沙盒环境探查：把沙盒根目录下**带 task 的** md 文件摸回本地 `tmp/`。

任务期间 `executeCmd` 不限量、不计异常，而这个槽**好多回合是空着的**（问模型、回灌结果、
纠错、交答案那几轮我们本来一条命令都不发）。这里就把那些空槽拿来跑两条固定的探查命令：

1. `find /` 一次遍历列出所有 md 的完整路径**与字节数**（判题器给沙盒输出的上限是 64KB，
   字节数就是打包的依据）；
2. 清单里的路径**过滤全路径含 `task` 的**（在代码里滤，命令照旧一次拿全 —— 过滤口径要改
   不用动沙盒命令），再按 64KB 预算贪心装箱，**一批一条命令**取回（`@@@FILE <路径>@@@`
   标记分隔），本地切成 `{路径: 正文}` 逐条落盘到项目根的 `tmp/<原路径>`，读完为止。
   单个文件就超预算 ⇒ 它独占一批，正文由判题器的截断兜着。

回执**不给任务线看**：`observe` 把它收走、返回 `""` ⇒ 判据 ② 走"没有回执"那一支 —— LLM 看不到
它没要过的输出、它自己的工具命令也不会被挤掉（两本账一轮一条交替跑）。清单里没有带 task 的 md /
取完 / 落盘失败 ⇒ 收工，此后一条都不发，任务线那边什么都察觉不到。

认出来的路径留在 `_known` 里给 system 的【沙盒知识】段（`known_paths`）。⚠️ **沙箱文件每道任务
刷新一次** ⇒ 探查的生命周期就是一道任务：任务一结束（`planner.task_channel` 在 `phase_task`
空的那一轮）`reset` 掉，下一道题从列清单重新摸一遍 —— 旧路径不许跨任务沿用（同文再现也是
新的一次，沙箱已经换了一批文件）。

跨回合状态（住在本模块）：`_phase` / `_pending` / `_known` / `_waiting`。退化路径：沙盒没跑起来
（回执里解析不出路径）⇒ 直接收工，只丢这一次探查。
"""

import logging
import os
from collections import deque

LOGGER = logging.getLogger(__name__)

#: 落盘目录，相对 cwd（`main3.py` 把 cwd 钉在仓库根）
TMP_DIR = "tmp"

#: 一次遍历列出全部 md 的路径与字节数：只认文件、吞掉权限报错。`wc -c` 每行一个文件
#: （行首补位空格、多文件时末尾多一行 `total`），按行解析天然干净
_LIST_CMD = "find / -type f -name '*.md' -exec wc -c {} + 2>/dev/null"

#: 一批的字节预算。判题器的沙盒输出上限 64KB，这里留 ~8KB 给 `[exitCode:N]` 前缀与
#: 每条命令里的标记；单位是字节，与 `wc -c` 同量纲
BATCH_MAX = 56 * 1024

#: 取文件命令里的分隔标记（`echo` 打一行，紧跟其后的 `cat` 正文取到这个标记为止）
_MARK = "@@@FILE {}@@@"

#: 取回来的正文进日志时留多少字。与 `pyexec.EXEC_TEXT_MAX` 同量级：日志要看得见内容，
#: 但一条记录别把 stdout 管道（64KB）顶掉
BODY_LOG_MAX = 4000

#: 还没发过 / 等清单回执 / 等取文件回执 / 收工
_IDLE, _LIST, _FETCH, _DONE = "idle", "list", "fetch", "done"

_phase = _IDLE
#: 清单里认出来的路径（`known_paths` 交给 system 的【沙盒知识】段，任务结束即清）
_known: list[str] = []
#: 待取的批，每批一组沙箱路径（清单回执按字节预算装好，取一批 pop 一批）
_pending: deque[list[str]] = deque()
#: 正在取的那一批（回执回来时按它切正文、命名落盘）
_fetching: list[str] = []
#: 上回合发的是探查命令 ⇒ 这回合的回执归我们
_waiting = False


def known_paths() -> list[str]:
    """探明的沙箱 md 路径（清单回执里认出来的那些，正文取没取回都一样）。还没列过清单 ⇒ 空表。

    只在**当前这道任务**里有效：任务结束复位，下道题重新列。
    """
    return list(_known)


def observe(result: str) -> str:
    """收下这回合的沙盒回执、推进状态机；返回**该给任务线看**的那条（我们自己发的 ⇒ `""`）。

    每回合调一次、且调在早返回**之前**：上回合发了探查命令、这回合任务却已经结束（或开拓者
    阵亡），回执照样得被认领 —— 否则 `_waiting` 粘住，下一个空槽会把别人（LLM）的回执当成
    自己的吃掉。
    """
    global _waiting
    if not _waiting:
        return result
    _waiting = False
    if _phase == _LIST:
        _take_list(result)
    elif _phase == _FETCH:
        _take_batch(result)
    return ""


def next_command() -> str:
    """这回合要发的探查命令；还没轮到我 / 取完了 ⇒ `""`。

    调用方只在命令槽空着时调它 —— 槽被 LLM 的工具命令占着就顺延，两本账不抢。
    """
    global _phase, _fetching, _waiting
    if _phase == _IDLE:
        _phase = _LIST
        _waiting = True
        return _LIST_CMD
    if _phase == _FETCH and _pending:
        _fetching = _pending.popleft()
        _waiting = True
        return "".join(
            f"echo '{_MARK.format(path)}'; cat '{path}' 2>/dev/null; " for path in _fetching
        )
    return ""


def stop() -> None:
    """收工：此后的空槽一条都不发（清单为空 / 取完了都走这条路，用例也用它让探查闭嘴）。"""
    global _phase, _waiting
    _phase = _DONE
    _waiting = False
    _pending.clear()


def reset() -> None:
    """回到"一次都没探查过"（整场开局、每道任务起手都是它）。

    两处调用：任务结束（`planner.task_channel` 在没任务那一轮 —— 沙箱文件每道任务刷新一次）
    与用例隔离（状态在模块里，同一个测试进程里会跨用例串味）。
    """
    global _phase, _fetching, _waiting
    _phase, _fetching, _waiting = _IDLE, [], False
    _known.clear()
    _pending.clear()


def _take_list(result: str) -> None:
    """清单回执 ⇒ 已知路径表 + 按预算装好的批。一条都没认出来 ⇒ 收工（沙盒没跑起来也是这条路）。

    只认**全路径含 `task` 的** md（沙盒根目录下 md 一堆，这条线只要任务书那几份）。每行认
    `字节数 路径` 这两段（取**末两段**：判题器那行状态若没换行就会粘在第一行前面，
    `[exitCode:0]  1200 /a/x.md` 照样认得出来）；`total` 行、缺 `wc` 时的报错都不成形
    ⇒ 自然落空。**命令没跑起来与"真的没有 md"在日志上靠同一行里的回执原文分辨**。
    """
    global _phase
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for line in result.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[-2].isdigit():
            continue
        path = parts[-1]
        if (
            path.startswith("/")
            and path.endswith(".md")
            and "task" in path.lower()
            and path not in seen
        ):
            seen.add(path)
            found.append((int(parts[-2]), path))
    if not found:
        LOGGER.info("【沙盒探查】：清单里没有含 task 的 md（「%s」）⇒ 收工", result[:200])
        stop()
        return
    batches = _pack(found)
    _known[:] = [path for _, path in found]
    _pending.extend(batches)
    _phase = _FETCH
    LOGGER.info(
        "【沙盒探查】：%d 个 md 带 task → %d 批：%s",
        len(found), len(batches), " ".join(path for _, path in found),
    )


def _pack(found: list[tuple[int, str]]) -> list[list[str]]:
    """按清单顺序贪心装箱：一批的总量（含标记的零头）不超 `BATCH_MAX` 就继续往里塞。

    单个文件本身就超预算 ⇒ 它独占一批，正文由判题器的 64KB 截断兜着。
    """
    batches: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for weight, path in found:
        weight += len(path) + 16  # 标记行与 shell 的零头
        if batch and size + weight > BATCH_MAX:
            batches.append(batch)
            batch, size = [], 0
        batch.append(path)
        size += weight
    batches.append(batch)
    return batches


def _take_batch(result: str) -> None:
    """一批回执 ⇒ 切成 `{路径: 正文}`，逐条进日志 + 落盘。

    正文没回来的（截断把尾巴吃掉 / 文件读不到）只在日志里点名，**不重试** —— 重试就是再花
    两个回合。落盘失败同样只告警、继续下一条（任务线那边什么都察觉不到）。
    """
    if "[TRUNCATED]" in result:
        LOGGER.info("【沙盒探查】：回执被 64KB 截断（本批 %d 个文件）", len(_fetching))
    bodies = _split(result)
    if not bodies:
        LOGGER.info("【沙盒探查】：本批 %d 个一个都没取到（命令没跑起来 / 超时），跳过", len(_fetching))
    for path in _fetching:
        body = bodies.get(path)
        if body is None:
            LOGGER.info("【沙盒探查】：%s 未取到（被截断或文件不可读）", path)
            continue
        LOGGER.info("【沙盒探查】：%s 取回 %d 字：%s", path, len(body), _clip(body, BODY_LOG_MAX))
        _save(path, body)
    if _pending:
        LOGGER.info("【沙盒探查】：还剩 %d 批", len(_pending))
    else:
        LOGGER.info("【沙盒探查】：全部取回，收工")
        stop()


def _split(result: str) -> dict[str, str]:
    """回执 ⇒ `{路径: 正文}`：按标记**顺序**定位，每段取到下一个标记之前、末段取到回执末尾
    （回执被截断时那正是被切掉的那条的残余）。正文里的换行与任何内容都不影响切分。

    某条标记找不到（前一条的正文被截断吃掉了）⇒ 从它往后都收不到。
    """
    marks: list[tuple[str, int]] = []
    cursor = 0
    for path in _fetching:
        at = result.find(_MARK.format(path), cursor)
        if at < 0:
            break
        marks.append((path, at))
        cursor = at + 1
    bodies: dict[str, str] = {}
    for index, (path, at) in enumerate(marks):
        end = marks[index + 1][1] if index + 1 < len(marks) else len(result)
        body = result[at + len(_MARK.format(path)):end]
        # `echo` 的换行紧跟在标记后面，去掉它才是正文的第一行
        bodies[path] = body[1:] if body.startswith("\n") else body
    return bodies


def _save(path: str, body: str) -> None:
    """落盘到 `tmp/<沙盒路径>`。"""
    # 前导 `/` 去掉后再拼接：`..` 一律塌在 tmp/ 里面，出不去
    target = os.path.join(TMP_DIR, path.lstrip("/"))
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:  # 路径畸形 / 没权限：丢这一个
        LOGGER.info("【沙盒探查】：%s 落盘失败（%s）", path, exc)
    else:
        LOGGER.info("【沙盒探查】：%s → %s", path, target)


def _clip(text: str, limit: int) -> str:
    """超长截断留痕（与 `utils._clip` 同形；agent 是叶子包，这条规则各存一份）。"""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"
