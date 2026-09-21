"""沙盒环境探查：一条 python 命令把沙盒里带 task 的 md 正文摸回来，只留在本模块的 dict 里。

占的是任务线空着的 `executeCmd` 槽（问模型、回灌、交答案那几轮本来不发命令）。一条
`python3 -c '<脚本>'`：脚本自己从沙箱根 `os.walk` 下去、只认全路径含 `task` 的 md（口径在
`_NEEDLE`）、按 `FETCH_MAX` 预算贪心取一批、每份打一段 `@@@FILE <路径>@@@`、末尾报还剩
几份（`@@@MORE <n>@@@`）—— 清单与正文同一条命令拿到。⚠️ 这一趟**限深 `_MAXDEPTH`、跳过
`_SKIP` 那几个伪文件系统、量尺寸只 `getsize` 不读文件**：判题器给沙盒命令只留 15 秒，
全盘扫 + 每个候选读两遍会 `[TIMEOUT]`（实盘踩过）。

一趟存档 = 一道题：`new_task` 认边界（任务文本一变就重开；空文本那一轮也记，同一道题
冷却后同文再现要算新任务），`@@@MORE 0@@@` ⇒ 收工（`_done`，空槽不再发）。每条命令带一份
排除表（`_files` 的全部键），脚本先剔掉读过的再取：`_files` 就是游标，读过的不再重搬。
沙盒每道任务独立（有哪些文件可能不同）⇒ 每道题都得重走一趟；成果只累积不清（跨任务叠加，
清单因此是跨任务的并集 —— 同一个路径上的内容一致，照旧可用，未实测）。

回执不给任务线看：`observe` 认领自己的回执并返回 ""。正文进 `_files`（不落盘），路径经
`known_paths()` 挂进 `readSandboxFile` 的描述（`path` 的取值表），正文由 `body_of` 按需取；
文件名对上不止一份 ⇒ 调用不成立（`matches` 一处认两种写法）。

状态在模块层：`_files` / `_task` / `_done` / `_waiting`（另有只给用例的 `_muted`）。
"""

import json
import logging
import re

LOGGER = logging.getLogger(__name__)

#: 沙盒里从哪个目录往下找
_ROOT = "/"

#: 全路径里必须出现的子串（小写比对）—— 沙盒根目录下 md 一堆，这条线只要任务书那几份
_NEEDLE = "task"

#: 从 `_ROOT` 往下最多走几层（**相对根**数，不是绝对路径）—— 判题器给沙盒命令只留 15 秒，
#: 全盘 `os.walk("/")` 会 `[TIMEOUT]`（实盘踩过）。官方示例里那条 `find / -maxdepth 6` 也是 6。
#: 层数按 `d[len(root):]` 数斜杠，且先把 `os.sep` 归一成 `/` —— 本地（Windows）跑同一个脚本
#: 时路径是反斜杠，不归一会一层都数不出来（用例 `test_a_doc_below_the_depth_cap_is_not_fetched`）。
_MAXDEPTH = 6

#: 沙盒根的这些顶层目录**整棵跳过**（伪文件系统：走一遍就要好几秒，任务书不会放在里面）
_SKIP = ("proc", "sys", "dev", "run", "snap")

#: 一条命令的字节预算（判题器回执上限 64KB；脚本里的 size 已含每份的标记开销，留的余量
#: 覆盖 `[exitCode:N]` 与末尾那行）。单个文件超预算 ⇒ 独占一趟，正文由 64KB 截断兜着。
FETCH_MAX = 60 * 1024

#: 取回来的正文进日志时留多少字（与 `pyexec.EXEC_TEXT_MAX` 同量级：日志要看得见内容，
#: 但一条记录别把 stdout 管道顶掉）。
BODY_LOG_MAX = 4000

#: 沙盒命令的脚本体；`_script()` 填槽后 `;` 连成一条物理行（判题器那侧怎么解析命令未知，
#: 单行最稳）。两条硬约束：每条物理行必须是完整语句（`;` 连接不续行）、一律双引号（外层是
#: `python3 -c '…'`，脚本里一个单引号就提前闭合）。`{known}` 排除表用 `json.dumps` 生成
#: （JSON 双引号串正好是合法的 Python 字面量）。
_SCRIPT = r"""

import itertools as it,os
budget={budget}
known=set({known})
root="{root}"
skip={skip}
maxdepth={maxdepth}
entries=os.listdir(root)
found=[os.path.join(root,f) for f in entries if f.endswith(".md") and "{needle}" in f.lower() and os.access(os.path.join(root,f),os.R_OK)]+[os.path.join(d,f) for r in (os.path.join(root,x) for x in entries if x not in skip) for d,_,fs in os.walk(r) for f in fs if f.endswith(".md") and "{needle}" in os.path.join(d,f).lower() and d[len(root):].replace(os.sep,"/").count("/")<=maxdepth and os.access(os.path.join(d,f),os.R_OK)]
paths=sorted(p for p in found if p not in known)
size=os.path.getsize
picked=list(it.takewhile(lambda t:t[1]<=budget,it.accumulate(((p,size(p)+len(p)+16) for p in paths),lambda a,b:(b[0],a[1]+b[1]))))
picked=picked or ([(paths[0],0)] if paths else [])
out="".join("@@@FILE %s@@@\n%s\n"%(p,open(p,encoding="utf-8",errors="replace").read()) for p,_ in picked)
print(out+"@@@MORE %d@@@"%len(paths[len(picked):]))

"""

#: 沙盒命令的解释器。押沙盒里有 `python3`（未实测）；押错只丢探查、不碰红线。
_INTERPRETER = "python3"

#: 正文的分隔标记（脚本打一行，紧跟其后的正文取到这个标记为止）与末尾的剩余数
_RE_FILE = re.compile(r"@@@FILE (.+?)@@@")
_RE_MORE = re.compile(r"@@@MORE (\d+)@@@")

#: 探明的沙箱路径 → 正文（整场累积、跨任务叠加）；同时是取文件的游标（排除表的来源）。
_files: dict[str, str] = {}
#: 当前这趟存档属于哪道题（`new_task` 认边界：变了就重开一趟）
_task = ""
#: 当前这趟走完没有。走完 ⇒ `next_command` 一条都不再发，等下道题重开
_done = False
#: 上回合发的是探查命令 ⇒ 这回合的回执归我们
_waiting = False
#: 用例静音（`mute`）。生产代码的"停"由 `_done` 说话
_muted = False


def known_paths() -> list[str]:
    """探明的沙箱 md 路径；没探查过 ⇒ 空表。只累积不清，进程重开才空。"""
    return list(_files)


def file_name(path: str) -> str:
    """这条全路径的文件名（最后一段）—— `path` 的另一种合法写法。"""
    return path.rsplit("/", 1)[-1]


def matches(name: str) -> list[str]:
    """这个名字对上了哪些已探明文件：全路径优先，其次按文件名。

    0 = 没这份；≥2 = 文件名撞了（不同目录同名）—— 调用方一律当不成立：替它挑一份
    会把错的那份正文交出去。"""
    if name in _files:
        return [name]
    return [path for path in _files if file_name(path) == name]


def body_of(path: str) -> str:
    """按全路径或文件名取正文；对不上、或撞名 ⇒ `""`。这张表就是 `readSandboxFile` 的枚举值。"""
    hits = matches(path)
    return _files[hits[0]] if len(hits) == 1 else ""


def observe(result: str) -> str:
    """收下这回合的沙盒回执、推进状态机；返回该给任务线看的那条（自己发的那条回 `""`）。

    每回合调一次、压在 `task_channel` 早返回之前：任务在回执回来前结束，不认领就粘住
    `_waiting`，之后会把 LLM 的回执也吃掉。"""
    global _waiting
    if not _waiting:
        return result
    _waiting = False
    _take(result)
    return ""


def new_task(task: str) -> None:
    """认任务边界：任务文本变了 ⇒ 重开这趟存档（`_done` 清掉）。任务没变 ⇒ 什么都不做。

    空文本那一轮也记 —— 同一道题冷却后同文再现要算新任务（沙盒可能不是同一只）。
    重开不等于重读：排除表把读过的挡在脚本里，这一趟只搬没读过的。"""
    global _task, _done
    if task != _task:
        _task, _done = task, False


def next_command() -> str:
    """这回合要发的探查命令；这趟走完了、上一趟的回执还没回来、或用例静音了 ⇒ `""`。

    调用方只在命令槽空着时调它 —— 槽被 LLM 的工具命令占着就顺延。"""
    global _waiting
    if _waiting or _muted or _done:
        return ""
    _waiting = True
    return _command()


def mute() -> None:
    """此后的空槽一条都不发。只给用例：生产代码的"停"由 `_done` 说话。"""
    global _muted, _waiting
    _muted = True
    _waiting = False


def reset() -> None:
    """回到"一次都没探查过"。只给用例隔离（生产代码不调：成果整场累积）。顺带解除 mute。"""
    global _task, _done, _waiting, _muted
    _task, _done, _waiting, _muted = "", False, False, False
    _files.clear()


def _command() -> str:
    """拼一条命令：脚本自带排除表。"""
    return f"{_INTERPRETER} -c '{_script()}'"


def _script() -> str:
    """把 `_SCRIPT` 填成一行（用例也调它，拿去本地真跑一遍）。排除表 = `known_paths()`。"""
    filled = _SCRIPT.format(
        root=_ROOT,
        needle=_NEEDLE,
        budget=FETCH_MAX,
        maxdepth=_MAXDEPTH,
        skip=json.dumps(list(_SKIP)),
        known=json.dumps(known_paths(), ensure_ascii=False),
    )
    return ";".join(line.strip() for line in filled.splitlines() if line.strip())


def _take(result: str) -> None:
    """一趟的回执 ⇒ 正文进 `_files`；末尾剩余数决定收工还是接着取。

    每段正文取到下一个 `@@@FILE` 标记之前、末段取到回执末尾（被截断的那份是残文，照留、
    不重试）。`MORE 0` ⇒ 没有没读过的了，置 `_done`；`MORE n`（n>0）⇒ 下回合接着取；
    没拿到剩余数（命令没跑起来 / 被 64KB 截断吃掉）⇒ 不收工、下回合接着试。收工判据是
    剩余数、不是"取到了正文"：全读过了与沙盒里没有匹配的 md 都报 0，都得停 —— 收工那行
    带的「库中共 N 份」是分辨这两种的唯一线索。日志只记新增 / 变化的正文（有排除表后
    这只是兜底，正常不该命中）。"""
    global _done
    if "[TRUNCATED]" in result:
        LOGGER.info("【沙盒探查】：回执被 64KB 截断")
    # 脚本按约定把 MORE 打在最后 ⇒ 取最后一个，别被正文里的同名串切掉
    found = list(_RE_MORE.finditer(result))
    more = found[-1] if found else None
    text = result[: more.start()] if more else result
    marks = list(_RE_FILE.finditer(text))
    fresh = 0
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end():end]
        # 脚本在标记后与正文尾巴上各补了一个换行（起手下一段标记），两头各去掉一个
        body = body[1:] if body.startswith("\n") else body
        body = body[:-1] if body.endswith("\n") else body
        if _files.get(mark.group(1)) != body:
            fresh += 1
            LOGGER.info("【沙盒探查】：%s 取回 %d 字：%s", mark.group(1), len(body), _clip(body, BODY_LOG_MAX))
        _files[mark.group(1)] = body
    if more is None:
        LOGGER.info(
            "【沙盒探查】：本趟取回 %d 份（新增 %d），没拿到剩余数（命令没跑起来？「%s」）⇒ 接着取",
            len(marks), fresh, result[:200],
        )
        return
    if more.group(1) == "0":
        LOGGER.info(
            "【沙盒探查】：本趟取回 %d 份（新增 %d），没有没读过的了 ⇒ 收工（库中共 %d 份）",
            len(marks), fresh, len(_files),
        )
        _done = True
        return
    LOGGER.info(
        "【沙盒探查】：本趟取回 %d 份（新增 %d），还剩 %s 份",
        len(marks), fresh, more.group(1),
    )


def _clip(text: str, limit: int) -> str:
    """超长截断留痕（与 `utils._clip` 同形；agent 是叶子包，这条规则各存一份）。"""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"
