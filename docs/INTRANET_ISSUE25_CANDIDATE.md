# #25 候选回测

包内源码：`2a95c26c715c22aedf3263dc95ccd64f7ea0dc60`。固定分支仍为codex/sgh，git pull后上传根目录CoreGeek.tar.gz。

SHA256：`e5f24932abfb8db539313835921ce4cc10bd42974da9ec2f73821d5996e5bfb4`。

本次重点观察：
- 模型完整JSON围栏回复是否直接进入后续校验与执行，不再等待到no_reply_within_wait。
- 明确token对象格式的题目，真实check返回的裸token是否被包装为JSON对象提交。
- 明确JSON对象任务是否阻止裸地名/示例数字；格式错误后能否在原episode内修正。
- heredoc错误是否在发送前指出，避免只改少量文字仍重犯相同语法错误。
- 实际判题反馈、任务结束和金币积分变化分别记录，不把unknown当失败，也不把格式通过当判题通过。

上一版HTTP恢复与CRLF check工具继续保留。两侧/不同题目均需复测；请在#25提供新源码SHA与日志。已有原始日志已经读取，无需重发旧文件。

[实现、证据与限制](../reports/issue-25-implementation/README.md)
