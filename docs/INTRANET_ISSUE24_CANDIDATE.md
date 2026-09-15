# #24 候选包回测

本次已实现API恢复工具、兼容CRLF的check执行、文档绑定的方法记忆和提交后的证据日志。

- 固定分支：`codex/sgh`，`git pull --ff-only`后上传仓库根目录`CoreGeek.tar.gz`。
- 包内源码：`9f1a2c82ed09126db35abb4799cd74e3ce9e2e3c`。
- SHA256：`4d35c693893100a328b0f641380d9a7898c8f73e519f8f6ea84e0fbfd810066e`。
- [验证结果与限制](../reports/issue-24-implementation/README.md)。

优先复测API和工程题，并左右换位。模型选用新工具时，日志executeCmd会以`# task-http/1`或`# task-check/1`开头。HTTP工具会显示尝试状态、参数名、JSON实际形状；check会显示CRLF处理和真实退出码。普通run仍保留，未知工具场景可继续交给LLM。

任务结束日志中的`unknown_without_judge_feedback`仍表示**尚无明确判题确认**；现会同时记录最近提交、积分/金币变化和原始动作回执。不要单凭它认定失败，也不要把金币变化自动当作任务奖励。

回复#24时附本次包内源码SHA、任务提交数/判题通过数、失败回合与相应工具结果。原始日志路径仍可补充；现有摘录已用于这批实现，无需先自行排查服务。
