# Issue 38 证据范围

详见 [实施Spec](../../specs/ISSUE_38_CHECK_AND_RESULT_RECONCILIATION.md)。#38所报20场2胜18负与#37六条同场记录冲突，汇总状态为 **disputed**；不是已确认的策略回归。此前主线程直接称“严重回归”的表述撤回。

已完成本地实际包检查及现有CRLF回归：

- `package-check-probe.json`：实际包SHA、源码、check文件不存在、两模块与归档一致、三个命令选择器预期结果。
- Windows Python3.11.10：`test_issue27_feedback_loop.py` 10通过、1跳过（真实POSIX）。
- Linux/WSL Python：同文件11项全部通过，1.122秒，退出0；含真实脚本CRLF处理、非重复修改前缀与原check不变测试。

这些是**已有功能验证和缺口复现**，没有修改任务执行代码，没有修复官方四场的声明。裸`./check`兼容分支和可关联日志是下一阶段实施项。当前根目录参赛包未变。
