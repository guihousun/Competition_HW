"""沙盒环境探查：一条 python 命令把沙盒里**带 task 的** md 正文摸回来，只留在本模块的 dict 里。

任务期间 `executeCmd` 不限量、不计异常，而这个槽**好多回合是空着的**（问模型、回灌结果、
纠错、交答案那几轮我们本来一条命令都不发）。这里就把那些空槽拿来跑探查：一条
`python3 -c '<脚本>' <skip>`，脚本自己 `os.walk` 沙箱根、只认**全路径含 `task` 的** md
（口径写在脚本里 —— 改口径改 `_NEEDLE`），按 `FETCH_MAX` 的字节预算贪心取一批，每个文件打一段
`@@@FILE <路径>@@@` 标记，末尾报还剩几份（`@@@MORE <n>@@@`）。清单与正文**同一条命令**拿到，
比"先列清单、再按批取"少一趟；一次取不完就带 `skip` 下一回合接着取。

回执**不给任务线看**：`observe` 把它收走、返回 `""` ⇒ 判据 ② 走"没有回执"那一支 —— LLM 看不到
它没要过的输出、它自己的工具命令也不会被挤掉（两本账一轮一条交替跑）。收工之后一条都不发，
任务线那边什么都察觉不到。

正文进 `_files`（全局路径 → 正文，**不落盘**）；认出来的**路径**现挂在 LLM 那个
`readSandboxFile` 工具的描述尾部（`Agent.prompt_tools` 取 `known_paths`）—— 那张表就是那个
`path` 参数的合法取值表，正文由 `body_of` 按需交给它。
⚠️ **探明的东西整场存活**（第 104 步，用户口径"一次找到、整个进程生命周期保存"）：任务换了
也不复位，清单与正文一直用到进程结束。代价是**清单可能过期** —— 若沙箱按任务换了文件，我们
既不重新走一遍、`readSandboxFile` 还可能把上一道题的正文当这一道题的正文交出去；首场看日志里
`@@@FILE` 的路径是否跨任务重现即知。

跨回合状态（住在本模块）：`_phase` / `_files` / `_skip` / `_waiting`。退化路径：沙盒没跑起来
（回执里一个标记都切不出来）⇒ 直接收工，只丢这一次探查。
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

#: 还没发过 / 正在一趟趟取 / 收工
_IDLE, _FETCH, _DONE = "idle", "fetch", "done"

_phase = _IDLE
#: 探明的沙箱路径 → 正文（`known_paths` 取键挂进 `readSandboxFile` 的描述，整场存活）
_files: dict[str, str] = {}
#: 下一条命令从第几份开始取（脚本按路径排序，取回来的都是队首那几份）
_skip = 0
#: 上回合发的是探查命令 ⇒ 这回合的回执归我们
_waiting = False


def known_paths() -> list[str]:
    """探明的沙箱 md 路径（正文取回来的那些）。还没探查过 ⇒ 空表。

    **整场有效**：不随任务复位（第 104 步），只在进程重开时清空。
    """
    return list(_files)


def body_of(path: str) -> str:
    """按全路径取已探明的正文；手边没这份 ⇒ `""`。

    命中与否只认**精确的全路径**（描述里列的就是它）—— 不做短名/后缀匹配：那会把
    "哪一份"变成需要猜的事。这张表就是 `readSandboxFile` 的**枚举值**：没命中 = 那次调用
    不成立（不转沙盒，见 `Agent.read_sandbox_file`）。
    """
    return _files.get(path, "")


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
    """这回合要发的探查命令；还没轮到我 / 取完了 ⇒ `""`。

    调用方只在命令槽空着时调它 —— 槽被 LLM 的工具命令占着就顺延，两本账不抢。
    """
    global _phase, _waiting
    if _phase == _DONE or _waiting:
        return ""
    _phase = _FETCH
    _waiting = True
    return _command(_skip)


def stop() -> None:
    """收工：此后的空槽一条都不发（取完了走这条路，用例也用它让探查闭嘴）。"""
    global _phase, _waiting
    _phase = _DONE
    _waiting = False


def reset() -> None:
    """回到"一次都没探查过"。

    ⚠️ **生产代码不调它**（第 104 步起探明的东西整场存活，任务结束不复位）：状态在模块里、
    同一个测试进程里会跨用例串味，这条只为用例隔离而留 —— 与 `Agent.reset` 同类。
    """
    global _phase, _skip, _waiting
    _phase, _skip, _waiting = _IDLE, 0, False
    _files.clear()


def _command(skip: int) -> str:
    """拼一条命令：脚本 + 从第几份开始取（= 已经取回的文件数）。"""
    return f"{_INTERPRETER} -c '{_script()}' {skip}"


def _script() -> str:
    """把 `_SCRIPT` 填成一行（用例也调它，拿去本地真跑一遍）。"""
    filled = _SCRIPT.format(root=_ROOT, needle=_NEEDLE, budget=FETCH_MAX)
    return ";".join(line.strip() for line in filled.splitlines() if line.strip())


def _take(result: str) -> None:
    """一趟的回执 ⇒ 正文进 `_files`，再按脚本末尾那个剩余数决定收不收工。

    标记按**顺序**定位，每段正文取到下一个标记之前、末段取到回执末尾（回执被截断时那正是被
    切掉的那条的残余）；正文里的换行与任何内容都不影响切分。
    - `@@@MORE 0@@@` ⇒ 全部取完，收工。
    - `@@@MORE n@@@`（n>0）⇒ `_skip` 前进，下一回合接着取。
    - **没拿到剩余数**（判题器 64KB 截断把它吃掉了）⇒ 本趟取到正文就保守继续，一份都没有才
      收工 —— 后者同时兜住"命令没跑起来"与"沙盒里真没有含 task 的 md"，靠日志里回执前 200 字
      分辨。截断的残文照旧留下、**不重试**（重试又要两个回合）。
    `_skip` 单调递增 ⇒ 最坏取完全部即终止，不会空转。
    """
    global _skip
    if "[TRUNCATED]" in result:
        LOGGER.info("【沙盒探查】：回执被 64KB 截断")
    # 脚本按约定把它打在**最后** ⇒ 取最后一个：正文里万一出现同样的串，不至于把后面的正文切掉
    found = list(_RE_MORE.finditer(result))
    more = found[-1] if found else None
    text = result[: more.start()] if more else result
    marks = list(_RE_FILE.finditer(text))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        body = text[mark.end():end]
        # 脚本在标记后与正文尾巴上各补了一个换行（起手下一段标记），两头各去掉一个
        body = body[1:] if body.startswith("\n") else body
        body = body[:-1] if body.endswith("\n") else body
        _files[mark.group(1)] = body
        LOGGER.info("【沙盒探查】：%s 取回 %d 字：%s", mark.group(1), len(body), _clip(body, BODY_LOG_MAX))
    if not marks:
        LOGGER.info("【沙盒探查】：一份都没取到（「%s」）⇒ 收工", result[:200])
        stop()
        return
    if more is None:
        LOGGER.info("【沙盒探查】：本趟取回 %d 份，没拿到剩余数（被截断？）⇒ 接着取", len(marks))
    elif more.group(1) == "0":
        LOGGER.info("【沙盒探查】：本趟取回 %d 份，全部取完 ⇒ 收工", len(marks))
        stop()
        return
    else:
        LOGGER.info("【沙盒探查】：本趟取回 %d 份，还剩 %s 份", len(marks), more.group(1))
    _skip += len(marks)


def _clip(text: str, limit: int) -> str:
    """超长截断留痕（与 `utils._clip` 同形；agent 是叶子包，这条规则各存一份）。"""
    return text if len(text) <= limit else text[:limit] + f"…（共 {len(text)} 字）"
