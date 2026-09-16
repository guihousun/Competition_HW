"""工具实现（`cmd.executeCmd` / `pyexec.run` / `sop.store`）—— 都是无状态的纯逻辑。

注册表不在这里，在 `agent.agent.Agent.__init__`：`SOP2Prompt` 要写 `Agent` 的状态
⇒ 只能是绑定方法，模块级常量表达不了这件事。
"""
