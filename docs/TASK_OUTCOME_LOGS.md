# 怎样判断LLM任务做到了哪一步

默认控制台日志新增三种 `task_event`。它们只在有实际操作回执或任务结束时打印，不每轮重复输出空闲摘要。

| kind | 看什么 |
|---|---|
| `task_check_result` | check命令轮次、退出码、是否明确“全部通过”、TOKEN是否出现及哈希。失败、超时、截断、无法关联分别记录。 |
| `task_submission_feedback` | 提交轮次→观测轮次、提交角色动作是否合法、是否观察到答案错误、错误类型，以及提交前后金币/积分变化。 |
| `task_outcome_summary` | 每题结束/替换一次：起止轮、模型/工具/检查/提交次数、最后check和提交摘要、错误次数、金币/积分总变化、观测是否缺轮。 |

摘要保存在事件的 `content.text` 中（JSON字符串），外层包含阵营、stream、episode和回合。`meaning` 提供中文说明。

## 结束状态

| status | 含义 |
|---|---|
| `check_passed_not_submitted` | 观察到check明确全部通过，但没观察到答案提交。 |
| `submitted_unconfirmed` | 已提交，官方判题结果仍未确认。 |
| `answer_error_observed` | 最后一次提交后的相邻观测中有答案错误，详情见 `answer_error_kind`。 |
| `submission_action_illegal` | 最后一次提交角色的动作回执为false。 |
| `unfinished` | 尚未观察到check明确通过或答案提交。复杂check调用未被识别也可能落入此类，需查原始事件。 |

`last_submission.matches_last_check_token=true` 说明提交的token对象与最近明确通过的check输出token相同。新增摘要不重复打印token原文。`checks_passed` 是观察到的check通过次数，不是官方完成任务数。

例如：r157 check通过、r158提交、r159题文消失，会得到 `submitted_unconfirmed`，同时保留check通过事实、提交轮次与token匹配证据。即使动作合法、金币+80、积分+80，仍不自动记成官方成功：同期其他操作也可能影响收入。

当前官方接口没有已核验的明确任务成功字段，因此 `official_success_confirmed` 保持false。它表示“缺少确认”，不等于“官方判定失败”。`unknown_without_judge_feedback` 是旧日志保留的本地观测标签，不是官方errorCode；errorCode 1的timeout也不自动撤销此前观察到的check通过事实。

## 快速筛选

```powershell
Select-String -Path .\teamA.log -Pattern 'task_check_result|task_submission_feedback|task_outcome_summary'
```

Linux：

```bash
grep -E 'task_check_result|task_submission_feedback|task_outcome_summary' teamA.log
```

只看每题结论可只筛 `task_outcome_summary`。若有完整trace，原有 `python tools/trace_tool.py tasks <日志目录> --output <新文件.jsonl>` 也会生成这些摘要；需从任务开始附近读取才能关联命令和回执。

## 覆盖范围

只关联连续下一轮、同题或题文刚结束的回执；换题、缺轮、晚到输出标为无法关联。记录的金币/积分差是观测窗口差额，不能直接作为任务奖励或强化学习真值。复杂shell、没有被采集的命令、进程退出后未再收到请求，都不能凭空补出任务结束结论。双方各自隔离；不改变比赛响应、不增加LLM调用、不执行日志中的命令。
