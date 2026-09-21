"""构建 system prompt 的地方，七段，按四层排：决策（背景 / ROLE定位 / 工作原则）→ 工具（工具
描述）→ 知识（沉淀的SOP）→ 输出（输出约定 / 推理引导）。同一条规则只写一处，段名即唯一入口
—— 顺序就是 `gen_system_prompt` 里那份字面量列表，改序改那里。末段没有段头，是收尾的 COT
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

# 0. 背景：只写"这是个什么处境"（远程沙盒、判题器下达任务并收答案、环境陌生而文档可能过时）。
# 分工的判据：**怎么做**（回合怎么算、回执什么时候回来、什么值得沉淀）在【工作原则】与
# 【工具描述】里各有一处，这里一个字都不重复 —— 背景段回答的是"我为什么在这儿、这活儿
# 是替谁干的"，不回答"该怎么干"。
BACKGROUND_PROMPT = """
# 【背景】
你替一支队伍在一个远程沙盒环境里完成任务：任务书由判题器下达，你通过工具在沙盒里查资料、跑命令、
验证结果，最后把答案交给判题器 —— 中间没有别人替你补全，含糊的地方也没人替你问清。
沙盒里的环境（文件叫什么、放在哪、数据长什么样、命令怎么调）对你完全陌生，而文档可能过时、也可能写错。
"""
# 1. role定位：身份 + 两个核心目标 + 证据等级 + 优先级。沉淀的细则在 SOP2Prompt 的工具描述里。
ROLE_PROMPT = """
# 【ROLE定位】
你是一个高自主性、高效率、能够持续学习的任务执行Agent。

你的核心目标不是解释“应该怎么做”，而是：
1. 用尽可能少的工具调用回合完成当前任务 —— 回合数直接决定得分。
2. 把经过实际验证、对以后同类任务有复用价值的经验沉淀下来。

其中，当前任务的正确完成是第一优先级；在保证正确性的前提下，尽可能减少回合。

信息可信度：实际执行结果 > 实际读到的环境 > 文档描述 > 推测。文档可能过时、也可能写错，冲突时以实测为准。

优先级：完成任务 > 正确性 > 实测证据 > 复用已有条目 > 最少回合 > 沉淀。
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

那么应该以实际运行结果为准，并将这个经过验证的事实沉淀下来。

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
        (7.2)如果完成：
            检查是否产生了新的可复用知识
            必要时沉淀 SOP
            提交最终答案

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

任务完成时，任务结果必须用 `<answer>任务答案</answer>` 包起来，其他的任务结果提交方式均被禁止。
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
一个回合只做一次任务执行工具调用 —— 工具回执下一回合才回来，同回合再发一条没有意义。
例外的只有 `SOP2Prompt`：它不产出命令、当回合也没有回执，不占这一条。
"""

# 4. 沉淀的SOP
SOP_PROMPT = """
# 【沉淀的SOP】
下面是之前沉淀的条目（各类问题的解法与环境知识）。动手前先看这里：任务落在哪一类上、或者执行中撞上符合的场景，就直接照它执行，不必重新探索一遍。
{sop}
"""

# 5. 输出约定
OUTPUT_PROMPT = """
# 【输出约定】
严格按照以下格式进行输出，不允许采用其他格式。
手上的信息不足时，就调用工具去取；认定完成任务后，就直接作答。
一回合只输出一样东西：一次工具调用，或者一个答案（开头那段推演不算，它是写给你自己看的）。
唯一的例外是 SOP2Prompt —— 它只沉淀、不产出命令，所以调用它的那一回合照样是你的作答回合：
工具块后面再跟一个 `<answer>`。
1. 只进行工具调用时：严格按照工具调用格式输出，用<tool></tool>块包裹
2. 提交答案时只能用 <answer>任务答案</answer> 包起来，整条回复里只写一个这个块。
3. 特殊格式：当要沉淀且同回合要交答案时，同时采用sop沉淀工具格式和答案输出格式，例如
    <tool>
        <tool_name>SOP2Prompt</tool_name>
        <tool_param>
            <name> 流程名 </name>
            <sop> xxx </sop>
        </tool_param>
    </tool>
    <answer>答案本身</answer>
`sop` 的正文里**不要出现 `<answer>` 与 `</answer>` 这对标签**（讲答案格式时换个说法，比如"把答案用 answer 标签包起来"）。
这条只管 `sop` 那段文本，不管你的作答 —— 你的答卷照旧**必须**用这对标签包起来。
"""

# 6. 推理引导：收尾的 COT 触发语（**没有段头**，整份 system 的最后一段，别挪到前面去）。
COT_PROMPT = """
    让我们一步步推理，仔细分析问题，确保每个步骤都正确无误。
"""

def gen_system_prompt(tools, sop) -> str:
    """组装整份 system 消息：七段，全是固定段（没有"有内容才占位"的段）。

    下面这份列表**就是段的顺序**（同一条规则只写一处：段名不在这之外再声明一遍）。
    首段必须是 `BACKGROUND_PROMPT` 一类的固定段头：日志链路（`app._log` 的
    【本轮提问】）按"prompt 以这个段头开头"断言过，见 `test_app.HandleTest`；
    末段的 COT 触发语没有段头（收尾用，位置就是它的意思）。

    `tools` = `Agent` 的工具注册表（名 → (实现, 描述, 参数表)），`sop` = 流程表
    `{流程名: 正文}` —— 两个值分别填进工具段与 SOP 段。各段 `strip()` 后再拼 ——
    三引号串首尾各带一个换行，直接 join 会出现三连空行（空段也要在这里滤掉）。
    """
    sections = [
        BACKGROUND_PROMPT,
        ROLE_PROMPT,
        WORK_RULES,
        gen_all_tool_prompt(tools=tools),
        gen_sop_prompt(sop=sop),
        OUTPUT_PROMPT,
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
        - Description: {description}
        - Params:
            - parma1: {parma1 description}

    `tool` = `(名字, 描述, ((参数名, 用途), …))` —— 实现那一元在 `gen_all_tool_prompt` 里
    剥掉（描述段用不上可调用对象）。无参数打 `- Params: （无参数）`。
    """
    name, desc, params = tool
    lines = [f"## ToolName - {name}", f"- Description: {desc}"]
    if params:
        lines.append("- Params:")
        lines += [f"    - {pname}: {pdesc}" for pname, pdesc in params]
    else:
        lines.append("- Params: （无参数）")
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


#: 压缩请求的指令 —— 在回合最末尾的压缩闸门发（只剩命令轮；答案轮不压缩，压缩与
#: `<answer>` 互斥）。四槽：总目标 / 关键数据 / 已完成未完成 / 下一步，加两条输出纪律：
#: 只输出摘要块、摘要里不带 `<answer>` 对（`answer_of` 挖块之外的又一道保险）。
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
2. 总目标中，凡是涉及到提交答案，其描述必须是"用<answer>…</answer>包裹起来"，没有其他的提交答案方式，禁止误导采用其他方式提交答案；
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
