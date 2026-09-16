"""
构建system prompt的地方
调试只用调这个，分为六段：role定位、工具描述、输出格式、示例、沉淀的SOP、注意事项

⚠️ **任务 prompt 专注任务**（第 41 步）：压缩教学的搭车协议已作废 —— 摘要由
**命令轮同发的压缩请求**产出（`COMPRESSION_PROMPT`，只进压缩 prompt、不进 system）。

⚠️ **两个带 `{}` 槽的模板（`TOOL_PROMPT` 的 `{tool_desc}`、`SOP_PROMPT` 的 `{sop}`）正文里
不许出现别的裸 `{}`**：`str.format` 会把它当占位符 ⇒ 运行期 `KeyError` ⇒ 整回合退化成
空指令（第 35 步传下来的规矩）。替换值（工具描述、流程正文）里的 `{}` 不会被二次扫描
—— python 片段太常见了。命令范式用 `$(...)`，安全。
"""

import json

# 1. role定位
ROLE_PROMPT = """
# 【ROLE定位】
你是一个自主任务执行Agent，能根据用户的任务基于现有的工具了解任务并理解任务，理解任务后严格按照任务要求完成任务；当认为解题流程值得沉淀时，用 SOP2Prompt 把方法沉淀下来

探索中拿到的**环境知识也必须沉淀**（第 46 步，用户口径）：接口怎么调、返回什么形状、文件与路径在哪、格式约定是什么——一律用 SOP2Prompt 存一条（name 写清是什么，如「接口-查询」）。会话窗口只留最近两轮，早先的发现不沉淀就会丢；沉淀过的每一条在后续每一份 prompt 里都看得见。

当手上的信息不足时，就调用工具去取；认定完成任务后，就直接作答。
一回合只输出一样东西：一次工具调用，或者一个答案。唯一的例外是 SOP2Prompt —— 它不产出命令，所以调用它的那一回合照样是你的作答回合：工具块后面再跟一个 `<answer>`。
"""

# 2. 工具描述
TOOL_PROMPT = """
# 【工具描述】
请使用工具解决问题！工具调用请使用指定的xml格式，注意确保回答中XML的所有特殊字符都被正确转义，以避免解析错误

工具描述如下
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
"""

# 3. 输出格式
OUTPUT_PROMPT = """
# 【输出约定】
严格按照以下格式进行输出，不允许采用其他格式
1. 只进行工具调用时：严格按照工具调用格式输出，用<tool></tool>块包裹
2. 无需再进行任何工具调用，已经完成了任务，可以提交答案，用<answer>任务答案</answer>格式提交答案
3. 特殊格式：当完成任务且认为流程可沉淀时，同时采用sop沉淀工具格式和答案输出格式，例如
    <tool>
        <tool_name>SOP2Prompt</tool_name>
        <tool_param>
            <name> 流程名 </name>
            <sop> xxx </sop>
        </tool_param>
    </tool>
    <answer>答案本身</answer>
`sop` 的正文里**不要出现 `<answer>` 与 `</answer>` 这对标签**（讲答案格式时换个说法，比如"把答案用 answer 标签包起来"）。
"""

# 4. 沉淀的SOP
SOP_PROMPT = """
# 【沉淀的SOP】
下面是之前沉淀的条目（解题流程与环境知识），若任务执行过程中有符合的场景，可以参考下面的SOP执行
{sop}
"""

# 5. 示例（第 38 步用户启用：一段"从 problem.txt 到 token"的四步范式）——
#    它进 `gen_system_prompt` 的 sections，代价是每回合 prompt 多这一段字节（记在字节表里）
EXAMPLE_PROMPT = """
# 【输出示例】
用于需求：解决promble.txt的任务

step1. 通过shell命令找寻promble.txt的位置并获取文件内容
    假设文件内容为：
    需求描述文档spec.md中描述了需求要求，完成需求，并通过check.sh验证是否通过，全部通过后会返回一个token，token即为答案内容
step2. 读取spec.md的内容了解需求要求
step3. 按照需求要求完成需求，并验证是否通过
step4. 通过则返回token，未通过则参考报错和spec.md的内容回到第三步
"""

# 6. 注意事项
ATTENTION = """
# 【注意事项】
1. 任务信息里给的往往只是一个文件名、不是完整路径。
2. 每条命令要花一个回合，不要拆成两回合。例如把「找文件在哪」和「读文件内容」合成一条：
   f=$(find / -maxdepth 6 -name '*任务书*' -print -quit 2>/dev/null); echo "FILE=$f"; cat "$f"
3. 读完任务书后，**先把它要求的「要交什么、什么格式」抄进回复里**，再动手去做；规格没看清楚就不要猜。
4. 提交答案前，逐条对照任务书核对一遍，不允许跳过任务书里的任何一条要求。
5. **命令输出已经包含任务所要求的答案时（如 token 已拿到、校验已通过、目标内容已读到），立即提交答案，不要为"再确认一下"执行多余命令。**
"""


def gen_system_prompt(tools, sop) -> str:
    """组装整份 system 消息：六段生效

    `tools` = `Agent` 的工具注册表（名 → (实现, 描述, 参数表)），`sop` = 流程表
    `{流程名: 正文}`（第 37 步起），两个槽分别填进工具段与 SOP 段。
    各段 `strip()` 后再拼 —— 三引号串首尾各带一个换行，直接 join 会出现三连空行。
    """
    sections = [
        ROLE_PROMPT,
        gen_all_tool_prompt(tools=tools),
        OUTPUT_PROMPT,
        gen_sop_prompt(sop=sop),
        EXAMPLE_PROMPT,
        ATTENTION,
    ]
    return "\n\n".join(section.strip() for section in sections)


def gen_all_tool_prompt(tools) -> str:
    """「工具描述」整段：每个工具一块，块间空一行（`##` 标题摆在那里，不空行会黏成一坨）。"""
    tools_desc = "\n\n".join(
        gen_tool_prompt((name, desc, params))
        for name, (_, desc, params) in tools.items()
    )
    return TOOL_PROMPT.format(tool_desc=tools_desc)


def gen_tool_prompt(tool) -> str:
    """一个工具一块（**由注册表生成、不手写第二份** —— 手写的描述迟早与调度分家，
    而那种不一致只有实盘上 LLM 报错才看得出来）：

        ## ToolName - {toolname}
        - Description: {description}
        - Params:
            - parma1: {parma1 description}
            - parma2: {parma2 description}

    `tool` = `(名字, 描述, ((参数名, 用途), …))` —— 实现那一元在 `gen_all_tool_prompt`
    里剥掉（描述段用不上可调用对象）。无参数打 `- Params: （无参数）`。
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
        ## SopName - sop name2
        流程描述

    空表打占位 —— **段头永远都在**（哪怕一条都没沉淀过）：那个槽是 LLM 自己写的
    目标，看不见槽就不会去用它（第 18 步传下来的规矩）。
    """
    if not sop:
        return SOP_PROMPT.format(sop="（暂无沉淀）")
    flows = "\n\n".join(f"## SopName - {name}\n{text}" for name, text in sop.items())
    return SOP_PROMPT.format(sop=flows)


#: 压缩请求的产出（`COMPRESSION_PROMPT`，只进压缩 prompt、不进 system）——
#: 第 43 步起在**回合最末尾的压缩闸门**发（任务线没产模型请求的轮：③ 命令轮 /
#: ⑤ 答案轮），任务 prompt 专注任务（卸掉了第 39 步搭车的 881 字节教学）。
#: 四槽照第 39 步的口径（总目标/关键数据/已完成未完成/下一步），加两条输出纪律：
#: 只输出摘要块；摘要里不带 `<answer>` 对（`answer_of` 挖块之外的又一道保险）。
COMPRESSION_PROMPT = """# 【上下文压缩】
你是上下文压缩器。把接下来的对话压成一份摘要，供后续回合替代完整历史使用。摘要必须包含：
【总目标】要交什么、什么格式（照抄任务书原文）
【关键数据】对话中出现过的、后续作答要用到的原文（token、数字、文件内容要点），宁全勿缺
【已完成】【未完成】各一行
【下一步】只写一条
只输出一个 <summary>…</summary> 块，不要输出任何别的内容；摘要里不要出现 <answer> 与 </answer> 这对标签。
"""


def gen_compression_prompt(material: str) -> str:
    """压缩轮的整份 prompt（第 41 步）：**独立指令 + 原始上下文全文**。

    形状与任务 prompt 同构（标准 messages JSON）—— 判题器的 LLM 按同一种读法处理两者。
    `material` 由 `Context.material()` 给出：题目 + 全部往来逐字（**原文永久保留**，
    压缩总从原文重来、不从旧摘要叠；给任务 LLM 的才是压缩后的）。
    """
    return json.dumps(
        [
            {"role": "system", "content": COMPRESSION_PROMPT.strip()},
            {"role": "user", "content": material},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


#: 新闻查价的指令（第 42 步）：**只进新闻 prompt、不进任务 system** —— 没任务时
#: 发出去问"官方消息对矿价的影响"，用任务线之外每游戏日 3 次的额度（Agent 指纹去重）。
#: 回复约定**裸 `<prices>` 块**（每行 `矿种 方向`），结构化才能进价格期望表。
NEWS_PROMPT = """# 【市场情报】
读下面的官方消息，判断它对矿产（stone / iron / copper）收购价的影响。只输出一个 <prices> 块，每行一条、格式为 `矿种 方向`（矿种用英文小写；方向只能是 up / down / flat）；消息没提到的矿也要给一行 flat。不要输出任何别的内容。
"""


def gen_news_prompt(news: str) -> str:
    """新闻查价的整份 prompt（第 42 步）：指令 + 官方消息原文。标准 messages JSON。"""
    return json.dumps(
        [
            {"role": "system", "content": NEWS_PROMPT.strip()},
            {"role": "user", "content": news},
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
