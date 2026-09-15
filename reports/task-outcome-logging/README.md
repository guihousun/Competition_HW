# 任务进展日志验证

日期：2026-09-16。类别：日志/调试工具。规则依据R01/R06、接口§1.1。包内源码 `65b03f995a448035b31c3001e72e5e3ec3ec2c9d`；运行策略、协议、计分规则没有变化。

新增 `task_check_result`、`task_submission_feedback`、`task_outcome_summary`。分别保留检查结果、提交后的相邻观测、每题结束摘要，避免把“全部通过6/6”“已提交”“动作合法”“金币增加”混写成官方任务成功。摘要仅保留token哈希，按stream/episode隔离，有缺轮或换题时拒绝强行关联。

## 验证

| 覆盖 | 结果 |
|---|---|
| Windows Python3.11.10全量unittest | 948项，261.286秒，OK；5项平台相关跳过 |
| Linux Python3.11.10进展/工具/定位/任务pipeline | 26项，无跳过，覆盖上述5项；pipeline含12个双阵营API/工程变体 |
| 实际tar.gz Windows双阵营check→提交→结束 | 12次POST通过，最高86.159ms；日志完整，check通过、token匹配、动作合法、金币积分各+80仍记未确认 |
| 同一实际包Linux双阵营check链 | 12次POST通过，最高84.729ms |
| 实际包Windows结构化HTTP恢复/围栏回归 | 10次POST通过，最高85.051ms |
| git diff --check及归档结构/清单/hash | 通过 |

HTTP探针用真实main3服务与脚本化模型/工具回执：证明传输、日志输出和归因边界，不是真实LLM解题率或官方判题。源码运行文件在全量测试后未修改。探针最初发现合成请求缺少gold字段，修正探针默认值后通过；该修正只在仓库工具中，不改变包内服务。

完整结果见本目录full-suite.log、linux-checks.log/json、package-check-windows.json、package-check-linux.json和package-api-windows.json。

## 旧日志示例

`historical-gamma-example.json` 从已提交598595场r150–159的task_event做局部观测重建：r155检查失败，r157明确通过，r158提交，r159结束汇总为 `submitted_unconfirmed`，同时保留1次check通过和1次提交。

这是旧66a4d9a运行的控制台片段，截断prompt等已跳过，模型次数等只能代表片段中可读取的事件；没有完整HTTP/地图/金币积分数据，未执行任何记录中的命令，不作为完整对局重放或新包实机证据。

## 使用与交付

固定codex/sgh，git pull后上传根目录CoreGeek.tar.gz。包SHA256：`12e996d59030f789430f76e55990de65f8b75c6a0dd151393702d25b44ee6fc7`。

筛选 `task_outcome_summary` 可直接看每题的中文结论、次数和最后证据；追踪细节同时筛 `task_check_result|task_submission_feedback`。[完整说明](../../docs/TASK_OUTCOME_LOGS.md)。

当前未核验到明确的官方任务成功字段，`official_success_confirmed`保持false。复杂check命令、丢失的观测、进程终止后的结果不被凭空补全；收入变化保留为观测差额，不直接作为任务奖励标签。下一次内网测试需日志identity显示上述新源码SHA，才能验证本次新增日志。
