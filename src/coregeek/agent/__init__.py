"""Agent 包：与判题器的 LLM、与它的沙盒打交道的全部。

唯一的实例 = `AGENT`：跨回合状态（SOP 流程表、新闻指纹与价格期望、任务内会话）都在它
身上 ⇒ `planner.task_channel` 每次都得用同一个它，每回合新建等于失忆。单实例放在
包根：这是唯一不依赖调用方的归处；构造发生在 import 期，`Agent.__init__` 只赋空值、
不读文件不起线程。
"""

from .agent import Agent

#: 全项目唯一的 Agent 实例：跨回合、跨任务地活着。
AGENT = Agent()
