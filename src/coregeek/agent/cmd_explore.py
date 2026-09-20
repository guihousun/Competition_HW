"""沙盒环境探查：一条 python 命令把沙盒里**带 task 的** md 正文摸回来，只留在本模块的 dict 里。

任务期间 `executeCmd` 不限量、不计异常，而这个槽**好多回合是空着的**（问模型、回灌结果、
纠错、交答案那几轮我们本来一条命令都不发）。这里就把那些空槽拿来跑探查：一条
`python3 -c '<脚本>' <skip>`，脚本自己 `os.walk` 沙箱根、只认**全路径含 `task` 的** md
（口径写在脚本里 —— 改口径改 `_NEEDLE`），按 `FETCH_MAX` 的字节预算贪心取一批，每个文件打一段
`@@@FILE <路径>@@@` 标记，末尾报还剩几份（`@@@MORE <n>@@@`）。清单与正文**同一条命令**拿到，
比"先列清单、再按批取"少一趟；一次取不完就带 `skip` 下一回合接着取。

回执**不给任务线看**：`observe` 把它收走、返回 `""` ⇒ 判据 ② 走"没有回执"那一支 —— LLM 看不到
它没要过的输出、它自己的工具命令也不会被挤掉（两本账一轮一条交替跑）。任务线那边什么都察觉不到。

正文进 `_files`（全局路径 → 正文，**不落盘**）；认出来的**路径**现挂在 LLM 那个
`readSandboxFile` 工具的描述尾部（`Agent.prompt_tools` 取 `known_paths`）—— 那张表就是那个
`path` 参数的合法取值表，正文由 `body_of` 按需交给它。取值表有两种写法：**整条全路径**与
**它的文件名**（`file_name`），`matches` 是这套对法的唯一一处；两种写法都落在同一条路径上才算
命中，**文件名撞了（不同目录下的同名文件）一律不成立** —— 替它挑一份就是把错的那份正文交出去。

⚠️ **沙盒每道任务是独立的**（用户口径，第 106 步）：同一个路径上的文件跨沙盒一致，但每道沙盒
里**有哪些**文件可能不同 ⇒ 两半：**每回合都重走一趟**（走完不停，`_skip` 归零从头再来，新沙盒
的文件下一回合就现取）、**探明的成果一直留着**（`_files` 只累积不清）。代价 = 每回合一条沙盒
命令（任务期间不限量、不计异常），以及清单是**跨任务的并集**（某一份在当前沙盒里可能并不存在
—— 同一个路径上的内容一致，所以照旧可用）。

跨回合状态（住在本模块）：`_files` / `_skip` / `_waiting`（另有只给用例的 `_muted`）。退化路径：
沙盒没跑起来（回执里一个标记都切不出来）⇒ 只丢这一趟，下一回合从头再来。
"""

import logging
import re

LOGGER = logging.getLogger(__name__)

#: 沙盒里从哪个目录往下找
_ROOT = "/"

#: 全路径里必须出现的子串（小写比对）—— 沙盒根目录下 md 一堆，这条线只要任务书那几份
_NEEDLE = "task"

#: 一条命令的字节预算。判题器的沙盒输出上限 64KB，这里留 ~8KB 给 `[exitCode:N]` 前缀与每份
#: 正文的标记行；单位是字节，与脚本里那个 `size` 同量纲。
#: 单个文件就超预算 ⇒ 它独占一趟，正文由判题器的 64KB 截断兜着
FETCH_MAX = 56 * 1024

#: 取回来的正文进日志时留多少字。与 `pyexec.EXEC_TEXT_MAX` 同量级：日志要看得见内容，
#: 但一条记录别把 stdout 管道（64KB）顶掉
BODY_LOG_MAX = 4000

#: 沙盒命令里的脚本体：`_script()` 填槽之后 `;` 连成**一条物理行**（判题器那侧怎么解析命令
#: 未知，单行最稳）。⚠️ 写它时有两条约束：**每条物理行必须是完整的语句**（`;` 连接不续行，
#: 在括号里折行会拼出语法错）、**一律用双引号**（整条命令是 `python3 -c '…'`，脚本里出现一个
#: 单引号就把 shell 的引号提前闭合了）。
_SCRIPT = r"""
import itertools as it,os,sys
skip=int(sys.argv[1])
budget={budget}
paths=sorted(os.path.join(d,f) for d,_,fs in os.walk("{root}") for f in fs if f.endswith(".md") and "{needle}" in os.path.join(d,f).lower() and os.access(os.path.join(d,f),os.R_OK))
size=lambda p:len(open(p,"rb").read())+len(p)+16
picked=list(it.takewhile(lambda t:t[1]<=budget,it.accumulate(((p,size(p)) for p in paths[skip:]),lambda a,b:(b[0],a[1]+b[1]))))
picked=picked or ([(paths[skip],0)] if skip<len(paths) else [])
out="".join("@@@FILE %s@@@\n%s\n"%(p,open(p,encoding="utf-8",errors="replace").read()) for p,_ in picked)
print(out+"@@@MORE %d@@@"%(len(paths)-skip-len(picked)))
"""

#: 每条命令发的解释器。⚠️ 押的是沙盒里有 `python3`（任务书只说"能执行 python 指令"，
#: 那份《编译运行环境说明》我们手上没有）⇒ 押错了探查线整场不工作，只丢探查、不碰红线
_INTERPRETER = "python3"

#: 正文的分隔标记（脚本打一行，紧跟其后的正文取到这个标记为止）与末尾的剩余数
_RE_FILE = re.compile(r"@@@FILE (.+?)@@@")
_RE_MORE = re.compile(r"@@@MORE (\d+)@@@")

#: 探明的沙箱路径 → 正文（`known_paths` 取键挂进 `readSandboxFile` 的描述，整场累积）
_files: dict[str, str] = {}
#: 下一条命令从第几份开始取（脚本按路径排序，取回来的都是队首那几份；走完一趟归零）
_skip = 0
#: 上回合发的是探查命令 ⇒ 这回合的回执归我们
_waiting = False
#: 用例静音（`mute`）。生产代码没有"收工"这回事：探查每回合都跑
_muted = False


def known_paths() -> list[str]:
    """探明的沙箱 md 路径（正文取回来的那些）。还没探查过 ⇒ 空表。

    **只累积不清**：任务换了照旧留着（第 106 步，用户口径"持续保留"），进程重开才空。
    """
    return list(_files)


def file_name(path: str) -> str:
    """这条全路径的文件名（最后一段）—— `path` 的另一种合法写法。"""
    return path.rsplit("/", 1)[-1]


def matches(name: str) -> list[str]:
    """这个名字对上了哪些已探明的文件：整条全路径优先，其次按文件名对。

    0 个 = 没这份；1 个 = 就是它；≥2 个 = 文件名撞了（不同目录下的同名文件）—— 调用方
    一律当"不成立"：替它挑一份会把错的那份正文交出去，而它看不出拿错了。
    """
    if name in _files:
        return [name]
    return [path for path in _files if file_name(path) == name]


def body_of(path: str) -> str:
    """按全路径或文件名取已探明的正文；对不上、或文件名对上不止一份 ⇒ `""`。

    这张表就是 `readSandboxFile` 的**枚举值**（两种写法都列在那个工具的描述里）：没命中 =
    那次调用不成立（不转沙盒，见 `Agent.read_sandbox_file`）。
    """
    hits = matches(path)
    return _files[hits[0]] if len(hits) == 1 else ""


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
    _take(result)
    return ""


def next_command() -> str:
    """这回合要发的探查命令；上一趟的回执还没回来、或用例静音了 ⇒ `""`。

    调用方只在命令槽空着时调它 —— 槽被 LLM 的工具命令占着就顺延，两本账不抢。**走完一趟不停**：
    沙盒每道任务独立，下一回合从头上再走一遍（`_skip` 已在 `_take` 里归零）。
    """
    global _waiting
    if _waiting or _muted:
        return ""
    _waiting = True
    return _command(_skip)


def mute() -> None:
    """此后的空槽一条都不发。

    ⚠️ **只为用例**（要断言"这一轮不该发命令"的那些先静音）：生产代码不调它 —— 探查每回合都跑，
    没有"收工"这回事。
    """
    global _muted, _waiting
    _muted = True
    _waiting = False


def reset() -> None:
    """回到"一次都没探查过"。

    ⚠️ **生产代码不调它**（探明的成果整场累积，任务结束不复位）：状态在模块里、同一个测试进程
    里会跨用例串味，这条只为用例隔离而留 —— 与 `Agent.reset` 同类。顺带解除 `mute`。
    """
    global _skip, _waiting, _muted
    _skip, _waiting, _muted = 0, False, False
    _files.clear()


def _command(skip: int) -> str:
    """拼一条命令：脚本 + 从第几份开始取（= 已经取回的文件数）。"""
    return f"{_INTERPRETER} -c '{_script()}' {skip}"


def _script() -> str:
    """把 `_SCRIPT` 填成一行（用例也调它，拿去本地真跑一遍）。"""
    filled = _SCRIPT.format(root=_ROOT, needle=_NEEDLE, budget=FETCH_MAX)
    return ";".join(line.strip() for line in filled.splitlines() if line.strip())


def _take(result: str) -> None:
    """一趟的回执 ⇒ 正文进 `_files`，再按脚本末尾那个剩余数决定下一趟从第几份开始。

    标记按**顺序**定位，每段正文取到下一个标记之前、末段取到回执末尾（回执被截断时那正是被
    切掉的那条的残余）；正文里的换行与任何内容都不影响切分。
    - `@@@MORE 0@@@` ⇒ 这一趟走完了，`_skip` 归零：下一回合从头上再走一趟（沙盒可能换了文件）。
    - `@@@MORE n@@@`（n>0）⇒ `_skip` 前进，下一回合接着取。
    - **没拿到剩余数**（判题器 64KB 截断把它吃掉了）⇒ 本趟取到正文就保守继续；一份都没有也归零
      —— 后者同时兜住"命令没跑起来 / 超时"与"沙盒里真没有含 task 的 md"，靠日志里回执前 200 字
      分辨。截断的残文照旧留下、**不重试**（重试又要两个回合）。
    日志只记**新增 / 变化的**正文：每回合都重走一趟，逐趟重抄会把 stdout 管道顶掉。
    """
    global _skip
    if "[TRUNCATED]" in result:
        LOGGER.info("【沙盒探查】：回执被 64KB 截断")
    # 脚本按约定把它打在**最后** ⇒ 取最后一个：正文里万一出现同样的串，不至于把后面的正文切掉
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
    if not marks:
        LOGGER.info("【沙盒探查】：一份都没取到（「%s」）⇒ 下一回合从头再来", result[:200])
        _skip = 0
        return
    if more is None:
        LOGGER.info(
            "【沙盒探查】：本趟取回 %d 份（新增 %d），没拿到剩余数（被截断？）⇒ 接着取",
            len(marks), fresh,
        )
    elif more.group(1) == "0":
        LOGGER.info(
            "【沙盒探查】：本趟取回 %d 份（新增 %d），沙盒走完一遍 ⇒ 下一回合从头再来",
            len(marks), fresh,
        )
        _skip = 0
        return
    else:
        LOGGER.info(
            "【沙盒探查】：本趟取回 %d 份（新增 %d），还剩 %s 份",
            len(marks), fresh, more.group(1),
        )
    _skip += len(marks)


def _clip(text: str, limit: int) -> str:
    """超长截断留痕（与 `utils._clip` 同形；agent 是叶子包，这条规则各存一份）。"""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"
