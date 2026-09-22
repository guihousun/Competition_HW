"""构建 system prompt 的地方，六段，按四层排：决策（ROLE定位 / 工作原则）→ 工具（工具描述）
→ 知识（沉淀的SOP）→ 输出（注意事项 / 推理引导）。同一条规则只写一处，段名即唯一入口 ——
顺序就是 `gen_system_prompt` 里那份字面量列表，改序改那里。末段没有段头，是收尾的 COT
触发语。

**"怎么用工具、什么时候沉淀"这类操作细则不在本模块**（第 107 步，用户重写的 prompt）：成本
模型、合并命令、泛化与去重、以实测为准 —— 全在各工具的**描述**里（`Agent.__init__` 的注册表，
`gen_all_tool_prompt` 生成），这里只剩段头与两个槽。

任务 prompt 专注任务：摘要由命令轮同发的压缩请求产出（`COMPRESSION_PROMPT`，只进压缩
prompt、不进 system）。两个带 `{}` 槽的模板（`TOOL_PROMPT` 的 `{tool_desc}`、`SOP_PROMPT`
的 `{sop}`）正文里不许出现别的裸 `{}`：`str.format` 会把它当占位符 ⇒ 运行期 `KeyError` ⇒
整回合退化成空指令。替换值（工具描述、流程正文）里的 `{}` 不会被二次扫描。

**探明的沙箱路径不在本模块**（第 104 步）：它们是 `readSandboxFile` 那个参数的取值表，跟着
**工具描述**走（`Agent.prompt_tools` 现刷），不单独占一段。
"""

import json

# 1. role定位：身份 + 核心目标 + 证据等级 + 优先级。**一个字的沉淀都不要有**（用户口径：
# 任务阶段只感知"可以参考下面的 SOP"，沉淀的提示全在 `SOP_REQUEST` 那一份里）。
ROLE_PROMPT = """
# 【ROLE定位】
你是一个高自主性、高效率的任务执行Agent。

你的核心目标不是解释“应该怎么做”，而是用尽可能少的工具调用回合完成当前任务 —— 回合数直接决定得分。

当前任务的正确完成是第一优先级；在保证正确性的前提下，尽可能减少回合。

信息可信度：实际执行结果 > 实际读到的环境 > 文档描述 > 推测。文档可能过时、也可能写错，冲突时以实测为准。

优先级：完成任务 > 正确性 > 实测证据 > 复用已有条目 > 最少回合。
"""

# 2. 核心工作原则
WORK_RULES = """
# 【工作原则】
## 1. 先复用已有经验，再探索

每次开始任务前，先检查【沉淀的SOP】：

* 当前任务是否属于某个已经沉淀的 SOP？
* 当前任务执行过程中是否遇到了某个已经沉淀的环境知识？
* 如果匹配，直接按照 SOP 执行。
* **不要为了“重新确认”而重复探索已经验证过的内容。**

只有在现有 SOP 无法解决、信息不足、或实际执行结果与 SOP 不一致时，才进行新的探索。

## 2. 以实际执行结果为准

SOP、任务文档、接口文档都属于**参考信息**，不是绝对事实。
信任优先级：实际执行结果 > 已验证的历史经验 > 当前文档描述 > 自己的猜测

例如：

文档写：参数名为 destination

实际调用发现：unknown parameter destination, expected target

那么应该以实际运行结果为准。

特别注意：接口真实路径、参数名称、参数类型、参数取值、返回值结构、文件位置、工具行为、错误信息揭示的约束。这些都是非常有价值的环境知识。

## 3. 处理任何任务时，按照下面的循环执行：
    (1)理解任务
    (2)检查已有 SOP
    (3)确定任务需要的输入、操作、验证方式和最终输出
    (4)检查当前环境中已有的资源
    (5)尽可能合并操作，减少工具调用
    (6)执行
    (7)分析结果 / 错误
        (7.1)如果未完成:根据结果调整方案并继续执行
        (7.2)如果完成：提交最终答案

## 4. 任务理解要求

拿到任务后，首先明确：
1. 任务目标是什么
2. 最终需要提交什么
3. 答案格式是什么
4. 任务要求的验证标准是什么
5. 任务中明确提供了哪些线索
6. 当前 SOP 是否已经覆盖该任务

特别注意：任务要求的“最终交付物”与“中间过程”必须区分。

例如任务要求最终提交 token，那么：
* 读取文件不是答案
* 调接口不是答案
* 验证成功不是答案
* **token 才是答案**

一旦最终答案已经明确获得，不要继续执行没有必要的工具调用。

## 5. 任务完成提交

任务完成时，必须调用 `submitAnswer` 工具提交任务结果，最终答案写在它的 `answer` 参数里，其他的任务结果提交方式均被禁止。
"""

# 3. 工具描述
TOOL_PROMPT = """
# 【工具描述】
请使用工具解决问题！工具调用请使用指定的xml格式，注意确保回答中XML的所有特殊字符都被正确转义，以避免解析错误

当前可用的工具描述如下
{tool_desc}

## 【工具调用格式】
工具调用使用 XML风格标签：工具名和每个工具参数各用一对标签包裹
要调工具时，用 `<tool>` 包住，里面写工具名与参数：
<tool>
    <tool_name>工具名</tool_name>
    <tool_param>
        <param1_name>工具参数1值</param1_name>
        <param2_name>工具参数2值</param2_name>
        ……
    </tool_param>
</tool>

例：
<tool>
    <tool_name>executeCmd</tool_name>
    <tool_param>
        <cmd> cat /tmp/a.txt </cmd>
    </tool_param>
</tool>

无参数的工具不用写 `<tool_param>`；
一个回合只做一次工具调用 —— 工具回执下一回合才回来，同回合再发一条没有意义。
"""

# 4. 沉淀的SOP
SOP_PROMPT = """
# 【沉淀的SOP】
下面是之前沉淀的条目（各类问题的解法与环境知识）。动手前先看这里：任务落在哪一类上、或者执行中撞上符合的场景，就直接照它执行，不必重新探索一遍。
{sop}
"""

# 5. 注意事项
ATTENTION_PROMPT = """
# 【注意事项】
1. 任务的答案必须用 submitAnswer 工具提交，其他的提交方式都不允许。
2. 任务的答案必须是最终结果，不能是中间过程。
"""

# 6. 推理引导：收尾的 COT 触发语（**没有段头**，整份 system 的最后一段，别挪到前面去）。
COT_PROMPT = """
    每次行动前判断信息缺口和行动价值，只执行能推进任务的最小必要动作。
"""

def gen_system_prompt(tools, sop) -> str:
    """组装整份 system 消息：六段，全是固定段（没有"有内容才占位"的段）。

    下面这份列表**就是段的顺序**（同一条规则只写一处：段名不在这之外再声明一遍）。
    首段必须是固定段头（现在是 `# 【ROLE定位】`）：日志链路（`app._log` 的
    【本轮提问】）按"prompt 以段头 `# 【` 开头"断言过，见 `test_app.HandleTest`；
    末段的 COT 触发语没有段头（收尾用，位置就是它的意思）。

    `tools` = `Agent` 的工具注册表（名 → (实现, 描述, 参数表)），`sop` = 流程表
    `{流程名: 正文}` —— 两个值分别填进工具段与 SOP 段。各段 `strip()` 后再拼 ——
    三引号串首尾各带一个换行，直接 join 会出现三连空行（空段也要在这里滤掉）。
    """
    sections = [
        ROLE_PROMPT,
        WORK_RULES,
        gen_all_tool_prompt(tools=tools),
        gen_sop_prompt(sop=sop),
        ATTENTION_PROMPT,
        COT_PROMPT,
    ]
    return "\n\n".join(text for text in (section.strip() for section in sections) if text)


def gen_all_tool_prompt(tools) -> str:
    """「工具描述」整段：每个工具一块，块间空一行（`##` 标题摆在那里，不空行会黏成一坨）。"""
    tools_desc = "\n\n".join(
        gen_tool_prompt((name, desc, params))
        for name, (_, desc, params) in tools.items()
    )
    return TOOL_PROMPT.format(tool_desc=tools_desc)


def gen_tool_prompt(tool) -> str:
    """一个工具一块（由注册表生成、不手写第二份 —— 手写的描述迟早与调度分家，而那种
    不一致只有实盘上 LLM 报错才看得出来）：

        ## ToolName - {toolname}
        ### Description: {description}
        ### Params:
            - parma1: {parma1 description}

    `tool` = `(名字, 描述, ((参数名, 用途), …))` —— 实现那一元在 `gen_all_tool_prompt` 里
    剥掉（描述段用不上可调用对象）。无参数打 `- Params: （无参数）`。
    """
    name, desc, params = tool
    lines = [f"## ToolName - {name}", f"### Description: {desc}"]
    if params:
        lines.append("### Params:")
        lines += [f"    - {pname}: {pdesc}" for pname, pdesc in params]
    else:
        lines.append("### Params: （无参数）")
    return "\n".join(lines)


def gen_sop_prompt(sop) -> str:
    """「沉淀的SOP」整段：流程表（`{流程名: 正文}`）逐条组装。

        ## SopName - sop name1
        流程描述

    空表打占位 —— 段头永远都在（哪怕一条都没沉淀过）：那个槽是 LLM 自己写的目标，
    看不见槽就不会去用它。
    """
    if not sop:
        return SOP_PROMPT.format(sop="（暂无沉淀）")
    flows = "\n\n".join(f"## SopName - {name}\n{text}" for name, text in sop.items())
    return SOP_PROMPT.format(sop=flows)


#: 压缩请求的指令 —— 在回合最末尾的压缩闸门发（只剩命令轮；交卷轮不压缩）。四槽：
#: 总目标 / 关键数据 / 已完成未完成 / 下一步，加一条输出纪律：只输出一个摘要块。
#: 【关键数据】那句里的"试过并失败的也要列"是试错类任务的记忆 —— 渲染窗口只留最近两轮，
#: 失败的尝试掉出窗口就只能靠摘要兜住。
COMPRESSION_PROMPT = """
# 【上下文压缩】
你是上下文压缩器。把接下来的对话压成一份摘要，供后续回合替代完整历史使用。摘要必须包含：
【总目标】要交什么、什么格式（照抄任务书原文）
【关键数据】对话中出现过的、后续作答要用到的原文（token、数字、文件内容要点），宁全勿缺；**已经试过并且失败的做法也逐条列上**（试了什么、结果如何、错在哪），否则后面会反复试同一条死路
【已完成】【未完成】各一行
【下一步】只写一条

# 【注意事项】
1. 只输出一个 <summary>…</summary> 块，不要输出任何别的内容；
2. 总目标中，凡是涉及到提交答案，其描述必须是"调用 submitAnswer 工具提交"，没有其他的提交答案方式，禁止误导采用其他方式提交答案；
"""


def gen_compression_prompt(material: str) -> str:
    """压缩轮的整份 prompt：独立指令 + 原始上下文全文。

    形状与任务 prompt 同构（标准 messages JSON）。`material` 由 `Context.material()` 给出：
    题目 + 全部往来逐字（原文永久保留，压缩总从原文重来、不从旧摘要叠；给任务 LLM 的才是
    压缩后的）。
    """
    return json.dumps(
        [
            {"role": "system", "content": COMPRESSION_PROMPT.strip()},
            {"role": "user", "content": material},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


#: 沉淀请求的指令 —— 交卷那一轮发（判据链尾的沉淀闸门：这一轮调了 `submitAnswer` 就发）。
#: 只说"什么时候问、要不要存"："存什么 / 怎么泛化 / 同名更新"的细则留在 `SOP2Prompt` 的
#: 描述里（同一条规则只写一处），而现在的 SOP 与本题的完整记录由生成函数填进来。
#: **沉淀这件事的全部提示都在这一份里**（用户口径）：任务阶段一个字都不提它。
SOP_REQUEST = """
# 【SOP 沉淀】
任务已交卷。把经过实际验证、对以后同类任务有复用价值的经验沉淀下来：
回顾下面的完整记录，判断有没有值得沉淀的东西 ——
有 ⇒ 用工具存下来（流程类与知识类可以各存一条）；
没有 ⇒ 只回一句「无需沉淀」。
"""


def gen_sop_request(sop, tool, material: str) -> str:
    """沉淀阶段的整份 prompt：指令 + 现在的 SOP + 那个工具块 + 本次任务的完整记录。

    `sop` = 流程表（复用【沉淀的SOP】整段 —— 判"存什么、哪条过时了"得先看见现在有什么）；
    `tool` = `(名字, 描述, 参数表)`（`gen_tool_prompt` 那一套，名字由调用方给）；
    `material` = `Context.material()`。**必须带记录**：判题器的 LLM 只看得到这一条 prompt。
    形状与 `gen_compression_prompt` 同构（标准 messages JSON，system = 指令、user = 原料）。
    """
    return json.dumps(
        [
            {
                "role": "system",
                "content": "\n\n".join(
                    [SOP_REQUEST.strip(), gen_sop_prompt(sop).strip(), gen_tool_prompt(tool)]
                ),
            },
            {"role": "user", "content": material},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


#: 新闻查价的指令：只进新闻 prompt、不进任务 system —— 没任务时发出去问"官方消息对矿价的
#: 影响"，用任务线之外每游戏日 3 次的额度（`Agent` 指纹去重）。回复约定裸 `<prices>` 块
#: （每行 `矿种 方向`），结构化才能进价格期望表。
NEWS_PROMPT = """# 【市场情报】
读下面的官方消息，判断它对矿产（stone / iron / copper）收购价的影响。只输出一个 <prices> 块，每行一条、格式为 `矿种 方向`（矿种用英文小写；方向只能是 up / down / flat）；消息没提到的矿也要给一行 flat。不要输出任何别的内容。
"""


def gen_news_prompt(news: str) -> str:
    """新闻查价的整份 prompt：指令 + 官方消息原文。标准 messages JSON。"""
    return json.dumps(
        [
            {"role": "system", "content": NEWS_PROMPT.strip()},
            {"role": "user", "content": news},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )

if __name__ == "__main__":
    # 直跑本文件时 `src/` 不在 sys.path 上、`agent.py` 里又全是相对导入
    # ⇒ 必须按包导入，先把 `src/` 塞进去（与 tests/ 的 bootstrap 同一个套路）。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from coregeek.agent import Agent

    AGENT = Agent()
    print(gen_system_prompt(AGENT.prompt_tools(), AGENT.sop))