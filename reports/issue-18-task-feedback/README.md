# Issue #18 日志与反馈交付验证

2026-09-15，类别：诊断/反馈工具，依据R01/R06/R07。任务现象、证据限制及后续防守排查见 [Spec](../../specs/ISSUE_18_TASK_FEEDBACK.md)。日志改善不等于已修好内网解题或第三夜防守。

实现先在 ac0b8cb 上完成 c02d35b，运行 [753项完整回归](full-suite.log)（387.477秒），其中 [8项新测试](task-journal-tests.log)覆盖观测→模型→工具→提交→结束、去重、红蓝隔离、重置、长文、凭据过滤、磁盘失败时实际HTTP回包。测试时文件哈希见 [validation.json](validation.json)。整合到 `400e0ea4c4a682033fdb3e3a400e181b8e11d8f1`；两个提交的 agent 源码、trace_tool 和相关测试 Git 内容一致，另合入此前冻结评测文档。

## 正式上传包

使用 `tools/build_competition.py --ref 400e0ea --output .../CoreGeek.tar.gz`，保持原版 main3.py 和 pyproject.toml 的原始字节及 flat 目录布局。[构建记录](competition-build.json)确认52个文件、58个成员、218516字节、全部来源于确定提交。

包 SHA256：`da76b2a21998d6e06996d67f6bf3c6d2c858cebc7413ce5cee7f68d43c6d036a`。根部 CoreGeek.tar.gz、对应 SHA256 和 manifest 是本次交付物。另一次 full-viewer 构建仅作本地开发试验，**没有作为参赛包交付，也不混入下列比赛包验证结果**。

| 实际包环境/场景 | 轮数 | HTTP p99 / 最大 | 结果 |
|---|---:|---|---|
| [Windows，seed1 challenger，脚本模型](competition-http-windows/report.json) | 1300 | 411.53 / 507.81ms | 基地1500，18次任务完成，响应通过 |
| [Linux，seed90317 defender，脚本模型](competition-http-linux/report.json) | 1300 | 178.69 / 301.91ms | 基地1500，27次任务完成，响应通过 |
| [Windows，seed90601 defender，全程模型不可用](competition-http-unavailable/report.json) | 1300 | 103.64 / 157.07ms | 基地1500，任务0次完成，合法继续行动 |

均为Python3.11.10，比赛包独立HTTP进程，外部模拟器结算。三类请求共用实际调度、没有通道冲突；模型不可用案例没有制造假模型答案。它们不是内网PASS，脚本模型任务完成数也不代表真实模型成功率。

[Windows入口/日志故障检查](competition-smoke-windows/report.json)：main3/main/日志路径不可用/manifest缺失，4×5POST；[Linux](competition-smoke-linux/report.json)额外实际bash run.sh，5×5POST。全部通过。最终复制到根部的包再次校验通过。

Linux实际包控制台捕获684条认知事件，其中题目27、prompt91、沙盒57、答案提交27、文本结束27；64条长内容明确标记截断。使用新 `trace_tool.py tasks` 从对应磁盘trace实际导出了682条事件，[导出结果](export-result.json)标明 `capture_complete:false`：验证器在1300轮后terminate服务，落盘捕获1278轮、没有结束标记。导出没有把不完整记录冒充完整对局；控制台与异步磁盘捕获范围不同。原始题目/命令不会从旧摘要中凭空恢复。

Issue表单在仓库默认main分支同步单文件更新（提交50a13c6e10b3aa2d2c9fec88f1088bf5e536453d），因为GitHub新建Issue采用默认分支模板。比赛代码仍固定在codex/sgh；没有把开发代码整合到main。

新闻账本仍在独立DSH原会话修复，不含在本包。整体目标继续推进，#18保持打开。
