# 在正在执行的原生 DSH 轮次中插入要求

`send`是普通下一轮消息；`steer`调用DSH原生 `agent.steer()`，进入当前轮次的next-step。
它不调用followup、不取消并重建会话，也不修改上下文/历史。

## 生效边界

- 正在生成或执行工具时可提交；DSH在原生执行边界读取新要求。
- 当前工具调用可能继续完成。文字插入不等于强杀进程，已发生的写入不会回滚。
- 不承诺任意CPU指令级抢占。若当前轮次已经结束，明确拒绝，不偷偷排到下一轮。
- 先返回 `accepted: true, delivery: next-step`；原生user/message事件实际进入当前轮次后，记录 `consumed: true`。
  只有接收回执不能宣称已经生效。
- 最终审核同时检查原Spec及全部已插入的补充要求，旧验收结论不能覆盖新要求。

## 命令

从目标专员status.json读取活跃session、active_turn和output，从目标任务request.json读取job_id、base_sha。
由Codex批准补充Spec，生成manifest：

```json
{
  "approved_by": "codex",
  "specialist": "rules-engine",
  "job_output": "<absolute current job output directory>",
  "job_id": "<existing request.json job_id>",
  "session_id": "<current native session ID>",
  "expected_turn": 1,
  "base_sha": "<current HEAD>",
  "request_id": "revise-target-v1",
  "spec_path": "<absolute supplementary requirements.md>",
  "spec_sha256": "<SHA256 of that file>"
}
```

```powershell
python workflow/dsh_sessions.py steer --manifest .workflow/issue-2/steer-v1.json --wait
```

此命令不取得全局执行锁、不创建第二个任务；它通过控制通道把要求交给正在执行的session。
session、job、当前轮次、HEAD和补充Spec哈希必须匹配；运行中的工作树会变化，因此不要求冻结整个树。
相同request_id重试只读取原回执，不重复插入；不同内容必须用新的ID。

返回receipt和consumed文件路径，均保存在本地任务目录下。原任务继续由 `send --wait` 或 `wait --output`
接收最终答案。回执超时先检查原receipt，不盲目再发。当前轮次结束后的迟到要求被拒绝。

## 实现范围

DSH 0.1.5-rc.1的标准SDK只暴露普通prompt。本项目通过每个新worker的局部 `--patch` 加载
`dsh_steering_bridge.mjs`，使用已安装DSH的公开SDK server/transport类和原生agent.steer方法。
只替换此SDK进程的传输入口，不编辑全局安装、不改模型设置、不处理对话压缩和恢复。
模型仍固定 `deepseek-flash / max`。

为避免破坏既有对话，旧worker不强制重启。其identity中没有 `native_steer: true` 时，CLI会明确拒绝steer；
普通send/续聊照常。新worker会校验原生插入能力后才接受此命令。

需要硬停止、撤销已执行的操作，或者目标原生会话已经丢失时，不能用steer冒充处理成功。
由Codex检查实际执行状态再决定取消/人工恢复，仍不自行重写DSH历史。

## 验证记录

2026-09-12实际测试：让规则专员执行等待任务后答 `BEFORE_STEER`，运行中插入“改答AFTER_STEER”。
原生回执为 `delivery: next-step`，随后 `consumed: true`；最终答案为 `AFTER_STEER`。
原任务、插入回执和最终结果均为同一session、第1轮，未创建第二轮；工作树无改动，原worker继续idle。
另有33项工具测试通过，覆盖迟到拒绝、错session/轮次、重复ID、回执晚于轮次完成及拒绝不排队等。
该验证不涉及官方游戏行为或公司内网平台。
