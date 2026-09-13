"""工具实现住在这里（`cmd.executeCmd` / `sop.store`）。

**注册表不在这里，在 `agent.agent.Agent.__init__`**（第 19 步搬的）：表里有一项是
`SOP2Prompt`，它要写 `Agent` 自己的状态 ⇒ **只能是绑定方法**，模块级常量已经表达不了这件事。
"prompt 里那段「可使用的工具」由表生成"这条性质照旧（加工具只改 `Agent.__init__` 一处）。

`cmd.py` 与 `sop.py` 都是**无状态的纯逻辑**，所以它们留在这个包里没有歧义。
"""
