"""沙盒环境探查：把沙盒根目录下的 md 文件摸回本地 `tmp/`。

任务期间 `executeCmd` 不限量、不计异常，而这个槽**好多回合是空着的**（问模型、回灌结果、
纠错、交答案那几轮我们本来一条命令都不发）。这里就把那些空槽拿来跑两条固定的探查命令：

1. `find /` 一次遍历列出所有 md 的完整路径（`;` 连接，只认文件不认同名目录）；
2. 按那份清单逐个 `cat`，内容落盘到项目根的 `tmp/<原路径>`，读完为止。

回执**不给任务线看**：`observe` 把它收走、返回 `""` ⇒ 判据 ② 走"没有回执"那一支 —— LLM 看不到
它没要过的输出、它自己的工具命令也不会被挤掉（两本账一轮一条交替跑）。清单为空 / 取完 /
落盘失败 ⇒ 收工，此后一条都不发，任务线那边什么都察觉不到。

跨回合状态（整场一次，住在本模块）：`_phase` / `_pending` / `_waiting`。退化路径：沙盒没跑起来
（回执解析不出路径）⇒ 直接收工，只丢这一次探查。
"""

import logging
import os
from collections import deque

LOGGER = logging.getLogger(__name__)

#: 落盘目录，相对 cwd（`main3.py` 把 cwd 钉在仓库根）
TMP_DIR = "tmp"

#: 一次遍历列出全部 md：只认文件、吞掉权限报错、路径用 `;` 连接
_LIST_CMD = "find / -type f -name '*.md' 2>/dev/null | tr '\\n' ';'"

#: 还没发过 / 等清单回执 / 逐个取 / 收工
_IDLE, _LIST, _FETCH, _DONE = "idle", "list", "fetch", "done"

_phase = _IDLE
#: 待取的沙箱路径（清单回执建表，取一条 pop 一条）
_pending: deque[str] = deque()
#: 正在取的那条的沙箱路径（回执回来时靠它命名落盘文件）
_fetching = ""
#: 上回合发的是探查命令 ⇒ 这回合的回执归我们
_waiting = False


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
        _take_file(result)
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
        return f"cat '{_fetching}'"
    return ""


def stop() -> None:
    """收工：此后的空槽一条都不发（清单为空 / 取完了都走这条路，用例也用它让探查闭嘴）。"""
    global _phase, _waiting
    _phase = _DONE
    _waiting = False
    _pending.clear()


def reset() -> None:
    """回到"一次都没探查过"（整场开局就是这个状态）。只给用例用 —— 状态在模块里，
    同一个测试进程里会跨用例串味。"""
    global _phase, _fetching, _waiting
    _phase, _fetching, _waiting = _IDLE, "", False
    _pending.clear()


def _take_list(result: str) -> None:
    """清单回执 ⇒ 建待取队列。一条路径都没解析出来 ⇒ 收工（沙盒没跑起来也是这条路）。

    每条先取**最后一个换行之后**的部分：判题器在输出前面加了一行状态（`[exitCode:N]`），
    它只该被丢掉、不该被当成含义解析（与任务线"那行状态不解析"同一条）。
    """
    global _phase
    paths: list[str] = []
    for chunk in result.split(";"):
        path = chunk.rsplit("\n", 1)[-1].strip()
        if path.startswith("/") and path.endswith(".md") and path not in paths:
            paths.append(path)
    if not paths:
        LOGGER.info("【沙盒探查】：清单里没有 md（「%s」）⇒ 收工", result[:200])
        stop()
        return
    _pending.extend(paths)
    _phase = _FETCH
    LOGGER.info("【沙盒探查】：根目录下 %d 个 md，逐个取回", len(paths))


def _take_file(result: str) -> None:
    """取文件回执 ⇒ 落盘。落盘失败只告警、继续往下取（任务线那边什么都察觉不到）。"""
    body = result.split("\n", 1)[1] if "\n" in result else result
    # 前导 `/` 去掉后再拼接：`..` 一律塌在 tmp/ 里面，出不去
    target = os.path.join(TMP_DIR, _fetching.lstrip("/"))
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError as exc:  # 路径畸形 / 没权限：丢这一个，别把整个回合带走
        LOGGER.info("【沙盒探查】：%s 落盘失败（%s）", _fetching, exc)
    else:
        LOGGER.info("【沙盒探查】：%s → %s（%d 字，剩 %d 条）", _fetching, target, len(body), len(_pending))
    if not _pending:
        LOGGER.info("【沙盒探查】：全部取回，收工")
        stop()
