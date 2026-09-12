# 等待进程链路验收

日期：2026-09-12；工程工具变更，无官方比赛行为变化。

- 29项工具测试通过，新增迟到结果、等待超时后重接、失败结果、session错配和失去worker检测。
- 实际向既有 qa-tooling 原生session发送两次消息，均由 `send --wait` 在同一工具调用内返回完整答案。
- 两轮分别为该原生session的第5、6轮；等待进程PID不同，session ID相同。第二轮未重发测试标签，仍正确回答。
- 两次调用约7秒/6秒返回，无需等待30分钟heartbeat。
- 等待进程结束后，原有会话worker仍存活且idle；未shutdown或重写DSH上下文。
- 超时仅结束等待器，原任务不被取消/重发；可用 `wait --output` 重接。
- git diff --check通过；官方源码、样例及游戏实现未改动。

测试命令：`python -m unittest discover -s workflow -p "test_*.py" -v`
