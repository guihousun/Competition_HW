# P0b Codex独立集成审核

类别：内部架构/官方接口实现修复，依据R01/R07。基础源码c53478b；本报告不代表三类LLM任务或整局Agent已完成。未进行内网测试，未发布新的参赛包。

## 原DSH结果与反例

原生session competition-qa-tooling-54e89da068914efeb2f1865df33e1bd9 的turn14在原工作树完成，base为34f838a，原报告590项通过。没有终止重派；父级审核保存了原工作树结果。

original-dsh-counterexamples.json记录了独立调用brain得到的三个真实反例：news请求留在队列；stale=999被隔离后仍成为submitAnswer；未知恢复与新scenario混用导致首日额度记满。原DSH组件通过不能证明这些集成行为正确。

## 本轮修复

- 共享回执入口对任务原始输入和JudgeState工具结果同时进行归属过滤；其他owner的结果保留在其router收件记录，不能流入旧task solver。
- 公共选择完成后直接发送被选中的owner请求，不要求它等于当前task提案；发送日记账与同轮通道限制经过JSON保存后仍生效。
- 新scenario显式创建fresh PlannerState；未知/损坏存档保守保留当前日额度已用状态。
- respond按队伍身份串行完成一次规划事务；普通旧回合不清空额度；新回合1清空当前认知记忆。官方缺少match_id，迟到旧回合1与真正新对局仍是接口层歧义。
- 官方errorCode5记录rejected并对发送日/任务代际退避；次日普通调用恢复是工程约定，尚无真实平台恢复证据。
- 预览使用独立规划记忆，同样不显示未经关联的答案；题目原文首尾空白进入generation身份，不先strip。
- 前端交互断言更新为当前“等待模型”的实际状态，并继续核验等待时不推进游戏回合。

## 验证范围

test_router_boundary.py提供15项父级独立回归；已有router组件/集成测试48项，合计63项。协调测试21项和前端交互20项检查通过。完整测试以本目录最终validation.json及full-suite-summary.txt为准；缺少终态文件时不得当作完整通过。

最终固定源码验证：Python3.11.10，650项unittest全部通过，耗时231.188秒，exit0，stable_sources=true。源码及测试文件哈希登记在validation.json。测试范围仍是当前项目实现，不代表完整三类Agent或官方验证。

bounded-simulation.json与三个actions文件记录seed3/90601、challenger/defender，每组24轮的实际执行动作和JSON运输。新router关闭时与基础源码动作完全一致；开启时这些短场景也能推进。没有真实API调用，这些轨迹没有覆盖多步Agent、长对局收益或完整防守难度。

第一轮全套643项中前端旧文案断言失败；第二轮新增旧回合检查不兼容精简规划状态而产生7个协调测试错误。分别修正后重新冻结源码验证，早期失败证据保存在本地p0b/parent-review-1和parent-review-2，未覆盖改写。

下一步按specs/LLM_TASKS_AGENT_INTEGRATION.md连接TaskAgent、共享上下文和方法库，再完成新闻/宝藏推理与整局对照。不得把本轮路由审核或此前真实模型9个组件试验替代目标整体的验收。
