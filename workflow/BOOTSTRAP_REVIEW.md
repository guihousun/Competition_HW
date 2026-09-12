# 工作流启动验收

类别：工程工具/文档，不改变官方游戏行为。日期：2026-09-12。

- GitHub连接可读目标仓库；初次扫描尚无用户Issue。
- dsh 0.1.5-rc.1：SDK显式初始化 deepseek-official / deepseek-flash / max，真实最小任务返回 DSH_READY。
- Codex批准有限测试Spec → dsh仅创建test_workflow.py → Codex检查范围并独立复跑。
- dsh报告重复Issue快照可能回退版本；Codex修复为相同内容去重、冲突内容拒绝整批写入，新增回归。
- 总计15项测试通过，包括锁、去重、部分结果保留、模型强度握手、失败不降级/不误报成功。
- 测试命令：python -m unittest discover -s workflow -p "test_*.py" -v
- git diff --check通过，官方原始文档及示例未修改。
- 尚未发生真实内网反馈修复；这次只证明监测入口和执行/审核工具可运行。

## 审核源码 SHA256（工作树字节）

- `state.py`: `40ac3050287bf47323220b2502d513ebc1320c30f1bfcba27031490ef5ff9544`
- `dsh_runner.py`: `0193e6f4700290d4f43f1265587a0bbf6bec32d81c78b5cb9cb259492baf656c`
- `test_workflow.py`: `e9e336598b6859c49724670b0629feb3e82c64d7494dda4d9f308b5c1f3ddeaf`
- `test_runner.py`: `e57858371935c8c4bb5d94802dd6f8182e69402abe38e70dd25e5aeb0cf7f7b0`
