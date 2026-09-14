# 三类LLM任务 / P0a 确定性基础 Spec v1

- 来源：本对话用户要求逐步实现，明确允许DSH做确定性修改和测试。
- 总Spec：[LLM_TASKS_ROADMAP.md](LLM_TASKS_ROADMAP.md)。本文仅授权P0a。
- 调研基线：80525916de8b4cd775fa207fbe601806cf50a2f8；实际派单base以manifest为准。
- 类别：内部调度实现修复与测试；依据R01/R07，接口§1.7、§2.1，任务书§5.3。不更改官方值。
- 负责人：Codex设计/审核；DSH qa-tooling实施，模型按workflow/policy.json。

## 证据与问题

tasks.SolverRegistry.register接受priority，但当前排序键只返回本次调用传入的priority，未保存每个求解器的优先级，因此实际保留插入顺序。默认注册顺序恰好为keyword-fill、llm-ask、probe-command，所以现有默认路径未暴露此缺陷；任意顺序插入未来新闻/任务求解器时，排序承诺会失效。

JudgeState已有普通3次、任务内豁免和跨日重置。首批只验证这些原语及PlannerState跨回合/JSON记忆，不声称已解决“官方任务有效性确认后如何分类额度”——后者属于P0b。

## 目标与范围

1. 为每个注册项保存自身priority，按数值升序；相同priority保持稳定注册顺序。replace=True重注册视为新的注册顺序位置；以测试固定这一内部行为。
2. 保持register/unregister/names/solve的现有公共调用形式、替换与重复名错误语义、线程锁。默认注册顺序与默认比赛响应不变。不可修成按名字排序或修改默认优先级来绕过问题。
3. 使用独立哨兵求解器证明高优先级先执行、返回None后才落到下一个；只有一个实际返回计划；低优先级不被无故调用。覆盖逆序注册、同优先级稳定、替换改变优先级、注销及重复名拒绝。
4. 新增确定性额度测试：任务外3次可用、第四次不可用；任务内连续多次仍可用且不改普通计数；结束后原有普通余额保留；round130→131重置；两个PlannerState彼此独立。consume_llm是记账原语，不是强制拒绝接口，测试需通过llm_available守卫再消费。
5. 新增状态测试：pending_prompt/pending_cmd、当前任务原文与提交记录、solver_notes经dump→JSON→load后保持；前一回合结果不被同轮误当新结果（按既有接口语义）。不把LLM平台自动记忆作为测试前提。

## 允许/禁止文件

允许：Demo/CoreGeek/src/agent/tasks.py内SolverRegistry部分；新增tests/test_solver_registry.py、tests/test_llm_budget_contract.py；必要时在tests/test_tasks.py补充相关测试；reports/llm-tasks-p0a/小型聚合报告。可运行现有benchmark/打包测试，但不得覆盖旧报告。

只读：brain.py、planner.py、sandbox.py、其他策略/模拟器/前端、workflow所有脚本与配置。额度/状态测试若发现其他真正缺陷，先提交最小复现和说明给Codex，不扩大实现范围或修改断言隐藏失败。

禁止：真实LLM/外部API调用、执行模型生成Shell、修改官方原文/样例/原始Demo、改参赛压缩包、改根目录其他工作、commit/push/合并/发布评论、启动新agent。不要重新做上一轮日志改造；它已经由Codex集成。

## 验收与回报

- 新注册器/额度/状态测试通过，给出数目及结果；不使用真实网络或真实LLM。
- python -m unittest discover -s tests -v通过；git diff --check通过。
- 默认三个求解器names顺序不变。用现有官方样例和两种阵营的公开观测，比较修复前后默认响应，不能因修复注册器改变默认排序行为；差异须解释给Codex。
- 如需代表性基准，先报告可用现有工具与成本；Codex负责最终固定种子左右换边和打包交付，避免双方重复跑同一长基准。
- 报告实际base SHA、修改文件、测试命令与退出结果、仍待P0b解决的事项。代码留在指定工作树，由Codex审核。
- 全套测试发现无关失败时记录证据，不擅改其他模块；等审核意见。wait超时不等于执行失败。
