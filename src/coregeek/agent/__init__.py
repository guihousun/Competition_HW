"""Agent 包：与判题器的 LLM、与它的沙盒打交道的全部。

**唯一的实例 = `AGENT`**，它身上带着全项目**唯一一处跨回合状态**（「沉淀的 SOP」）
⇒ `planner.task_channel` 每次都得用**同一个**它，不能每回合新建一个（新建就等于每次都失忆）。

```
agent/
├── agent.py    class Agent：状态（SOP）+ 工具表 + chat / tool_call / tool_desc
├── chat.py     四段模板 + 组装 + 三个谓词（**纯函数**，状态由 Agent 传进去）
└── tools/      cmd.executeCmd（原样搬命令）/ sop.store（存储规则）
```

⚠️ **这个 `__init__.py` 不是空的**：单实例必须有一个确定的归处，包根是唯一不依赖调用方的地方。
构造在 import 期发生，而 `Agent.__init__` 只赋值一个空串、不读文件不起线程。
"""

from .agent import Agent

#: **全项目唯一的 Agent 实例。** 跨回合、跨任务地活着；`planner` 每次都用它。
AGENT = Agent()
