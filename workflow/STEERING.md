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

## 原生恢复（resume）

worker已失败/停止后，`send`一律拒绝并提示检查会话，不会静默新建同ID会话。恢复用独立命令：

```powershell
python workflow/dsh_sessions.py resume --pool .workflow/dsh-sessions-v2 --specialist strategy `
  --manifest .workflow/metal-economy/manifest-followup.json --previous .workflow/metal-economy/run-1 `
  --expect-session competition-strategy-1af920579f54486caf5d8a623c45343a --output .workflow/metal-economy/run-2 --wait
```

判定条件（任一不满足即拒绝，不新建会话、不改旧证据）：

- 槽位identity与status的session相同且等于 `--expect-session`；status必须为 `failed`/`stopped`；identity/status中的worker PID必须已不存在。
- specialist、worktree、provider、model、reasoningEffort与原identity一致；新manifest仍是Codex批准、base/指纹/Spec哈希匹配的独立worktree。
- 旧输出目录只读校验：`request.json` 的 specialist/job_id/worktree/base_sha 必须存在；原会话ID从 `started.json`、`result.json`（worker自己写的执行证据）与 `request.json` 中读取。凡是写有会话ID的证据必须一致，且 `started.json`/`result.json` 至少有一个带ID——只有 `request.json` 自称不算证据（旧版submit不写该字段，兼容该形状）。冲突或缺失即拒绝。
- 新输出目录必须不存在；`--previous` 的 result/answer/steering 只读保留，原job不会自动重排，started.json存在的新任务仍拒绝重放。

`resume` 返回 `resume_requested: true` 只表示已排队：它先写identity（`native_resume`、`resumed_from`、`resume_evidence`）、新输出目录和job，再启动固定supervisor；只有supervisor成功启动才清除旧 `STOP` 标记（停止中的槽位不会因清除标记而立即退出）。真正的原生确认由worker完成：

1. worker以 `competition/resume`（原生 `ctx.agents.resume({resumeSessionId})`，cold-read持久化日志、追加原生interruptedTurnClosers）恢复原会话ID；缺失日志、未配置持久化、模型/cwd不一致或已在运行的agent都会明确失败。
2. 确认写入新输出的 `native-resume.json` 与identity的 `native_resumed` 后，才发送本轮follow-up prompt。确认失败时不发送prompt，只记录failed，`started.json`不产生。
3. 之后仍走同一 `session/prompt`、`session.event`、`steer` 通道；不设执行期限，`--timeout`仍只是观察者。
4. 恢复后的 worker 只有再次失败才需要新的恢复；不会自动重放原job。

手工核对示例：`Get-Content .workflow/metal-economy/run-2/native-resume.json`（必须有 `resumed: true` 且 `sessionId` 等于原ID）。

2026-09-13 真实目标 `competition-strategy-1af920579f54486caf5d8a623c45343a` 的 run-1 证据：
`result.json` 是旧版监督器的 `DSH deadline exceeded` 失败，`started.json` 记录同一会话ID与PID 51704，
原生日志位于 `%USERPROFILE%\.dsh\sessions\--D-Research_vault-work-projects-Code_HW-.workflow-worktrees-ds-strategy--\<id>\session.v3.jsonl.zstd`。
本次只实现并测试了适配器，真实恢复由Codex审核后执行。

## 实现范围

DSH 0.1.5-rc.1的标准SDK只暴露普通prompt。本项目通过每个新worker的局部 `--patch` 加载
`dsh_steering_bridge.mjs`，使用已安装DSH的公开SDK server/transport类和原生agent.steer/agent.resume方法。
`competition/resume` 把原生 `ctx.agents.resume()` 返回的handle放进已安装server的会话表，使随后的
`session/prompt` 走 `getOrCreateSession` 的既有记录，而不是 `agents.create`；会话表字段缺失时明确报错，
不退回新建。只替换此SDK进程的传输入口，不编辑全局安装、不改模型设置、不处理对话压缩和恢复。
模型仍固定 `deepseek-flash / max`。

为避免破坏既有对话，旧worker不强制重启。其identity中没有 `native_steer: true` 时，CLI会明确拒绝steer；
普通send/续聊照常。新worker会校验原生插入能力后才接受此命令，恢复前还会校验 `nativeResume` 能力。

需要硬停止、撤销已执行的操作，或者目标原生会话已经丢失时，不能用steer冒充处理成功。
由Codex检查实际执行状态再决定取消/人工恢复，仍不自行重写DSH历史。

## 验证记录

2026-09-12实际测试：让规则专员执行等待任务后答 `BEFORE_STEER`，运行中插入“改答AFTER_STEER”。
原生回执为 `delivery: next-step`，随后 `consumed: true`；最终答案为 `AFTER_STEER`。
原任务、插入回执和最终结果均为同一session、第1轮，未创建第二轮；工作树无改动，原worker继续idle。
另有33项工具测试通过，覆盖迟到拒绝、错session/轮次、重复ID、回执晚于轮次完成及拒绝不排队等。
该验证不涉及官方游戏行为或公司内网平台。

2026-09-13原生恢复适配器测试（无模型调用）：`workflow/test_resume.py` 19项（假原生协议断言resume只带原会话ID、
确认前不发prompt、缺日志/无持久化/模型或cwd不一致/存活worker即失败、旧输出与旧result只读、STOP清理时机、
队列返回 `resume_requested`、无轮次期限）；`workflow/test_resume.mjs` 9项（假原生registry驱动真实
HarnessSdkJsonRpcServer，断言`agents.create`从未被调用、恢复后的prompt落在被恢复的agent上、事件/steer通道不变）。
真实原生恢复不在本次范围内。
