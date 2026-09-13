"""工具实现住在这里（`cmd.executeCmd` / `sop.store`）。

**注册表不在这里，在 `agent.agent.Agent.__init__`**：表里有一项是 `SOP2Prompt`，
它要写 `Agent` 自己的状态 ⇒ 只能是绑定方法，模块级常量表达不了这件事。
`cmd.py` 与 `sop.py` 都是**无状态的纯逻辑**，所以它们留在这个包里没有歧义。
"""
