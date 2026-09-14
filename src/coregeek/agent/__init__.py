"""Agent 包：与判题器的 LLM、与它的沙盒打交道的全部。

**唯一的实例 = `AGENT`**，它身上带着跨回合状态（**两处**，都在实例上：「沉淀的 SOP」
整场存活；「任务内会话」`Context` 题目变了即换新，第 25 步）
⇒ `planner.task_channel` 每次都得用**同一个**它，不能每回合新建一个
（新建就等于每次都失忆）。

```
agent/
├── agent.py    class Agent：状态（SOP + Context）+ 工具表 + chat / hear / tool_call / tool_desc
├── chat.py     四段模板（system header）+ 三个谓词（**纯函数**）
├── context.py  Context：任务内会话的存储与渲染（**零包内 import**，措辞在这里、状态在 Agent）
└── tools/      cmd.executeCmd（原样搬命令）/ sop.store（存储规则）
```

⚠️ **这个 `__init__.py` 不是空的**：单实例必须有一个确定的归处，包根是唯一不依赖调用方的地方。
构造在 import 期发生，而 `Agent.__init__` 只赋值空值、不读文件不起线程。
"""

from .agent import Agent

#: **全项目唯一的 Agent 实例。** 跨回合、跨任务地活着；`planner` 每次都用它。
AGENT = Agent()
