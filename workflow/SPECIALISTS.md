# DeepSeek 专员派单与同会话续聊

更新：2026-09-12。类别：工程工具；不更改官方协议/游戏规则。

## 谁来做

| 情况 | 主执行者 | 明确例子 |
|---|---|---|
| 输入、范围、预期结果清楚，能独立测试 | DeepSeek | 修复已定位的字段解析；按已确认数值实现校验；补边界测试；指定页面交互；日志工具；文档同步 |
| 任务较复杂，但可拆成边界清楚的子任务 | Codex设计 + DeepSeek实现 | 我先设计状态机/接口与验收，专员分别完成具体模块；我负责集成和审核 |
| 规则来源冲突、关键架构取舍、难以复现的跨模块错误、安全/数据边界不明 | Codex直接处理 | 官方行为与模拟器矛盾；并发/跨回合一致性；设计路线选择；缺少可靠验收依据 |
| 同一输入版本两次实现仍不通过、执行超时、越界修改或反复误解Spec | Codex接手 | 停止重复收费/派单，保留现场，自行定位并实现，或向用户请求缺失的内网证据 |

“可以交给DeepSeek”不等于“可以让它自行合并”。所有实现均由Codex审核；涉及比赛行为仍需要用户
对候选head SHA的内网PASS。困难Issue由Codex完成时，也使用独立工作树、Spec、测试与同样的合并条件。

## 四个专员

| specialist | 工作范围 |
|---|---|
| `rules-engine` | 已明确的协议解析、规则校验、模拟结算修复 |
| `strategy` | 已设计的寻路、策略、任务求解子模块 |
| `frontend` | 地图渲染、交互、动画、调试界面 |
| `qa-tooling` | 回归测试、工具链、诊断、文档 |

全部固定 `deepseek-flash` / `max`，按需启动。同类型工作优先路由到同一个活跃原生session；
同一Issue返工直接发送修改意见。跨Issue也可复用同类型session，但每次都以当前批准Spec为任务边界。
专员之间不共享对话历史。当前仍最多一个实际执行任务，并不默认并行修改项目。

## 不接管DSH的上下文

这里只保存 session ID、进程、工作树、任务状态和审核所需代码指纹。
不压缩、不改写、不拼接历史消息，不自建记忆/摘要/恢复机制，不改DSH全局模型或上下文设置。
DSH负责其原生对话、记忆、压缩和持久化。一次任务结束不再shutdown原生SDK进程，下一次发送
`session/prompt` 到同一session。没任务时不调用模型。

已核对的DSH 0.1.5-rc.1 SDK仅提供initialize、session/prompt和shutdown；进程内同ID会复用agent，
进程重启后的同ID不等价于原生resume。若进程丢失/停止，本工具拒绝假装恢复或悄悄换session。
需要DSH原生恢复入口时由Codex核对支持的接口再接入；不得自行搬运对话日志。
`stop`是停止本地连接进程，不删除DSH历史。恢复不了时可由Codex直接完成当前工作。

## 工作树与返工

session固定绑定一个专属工作树，例如 `.workflow/worktrees/ds-qa-tooling`。
同一类型的不同Issue在该工作树依次执行；Codex只在前项变更已提交/保存且审核完成后切换候选分支。
不能让同一活跃session偷偷切换cwd。每次派单带当前完整HEAD、Spec哈希和工作树指纹，
既支持干净基线，也支持Codex已审阅的未提交返工现场。

manifest格式：

```json
{
  "specialist": "qa-tooling",
  "worktree": "<absolute dedicated git worktree>",
  "base_sha": "<current complete HEAD SHA>",
  "workspace_sha256": "<reviewed current workspace fingerprint>",
  "spec_path": "<absolute current spec or follow-up instructions file>",
  "spec_sha256": "<file SHA256>",
  "approved_by": "codex"
}
```

```powershell
python workflow/dsh_sessions.py fingerprint --worktree <absolute-worktree>
python workflow/dsh_sessions.py send --manifest .workflow/issue-2/review-v2.json --output .workflow/issue-2/run-2 --wait
python workflow/dsh_sessions.py status --specialist qa-tooling
```

默认使用 `send --wait`：调用进程持续等待本轮结果，将DSH的答案和状态作为工具结果返回后退出，
原生session进程继续保留。Codex收到工具结果即可审核或在同session追问，不等30分钟调度。
工具执行时间较长而返回进程/session ID时，Codex应继续等待该进程，不主动结束当前工作轮次。
`send`不加 `--wait` 仍支持异步立即返回。两种方式均保存result.json、answer.md。
status.json和started.json用于定位排队/在执行状态；主控轮询这些状态，不将无输出误判为已结束。
再次调用send会复用同一专员session；有新文件变更、HEAD或Spec变化时拒绝旧授权指纹。
多个排队任务在执行前各自复核指纹；前一任务修改了代码，后一旧Spec必须重新审核。

全局执行锁在 `.workflow/dsh-sessions/active.lock`；锁残留不得按年龄自动清除，须核对PID与任务状态。
不同时使用旧一次性runner和新session路由器派发任务；旧runner只保留兼容/诊断，不参与默认监测。

```powershell
python workflow/dsh_sessions.py stop --specialist qa-tooling
```

停止请求在当前轮结束后处理；单次模型任务仍限20分钟。通信超时会关闭该SDK连接并标记失败，
不自动重放可能已执行的任务。中途崩溃的started记录必须先人工审核，不能重复派单。

这套机制是本机编排与版本校验，不是操作系统沙箱；仍由Codex审核改动路径、测试和实机证据。

## 等待器中断或超时

等待器默认1250秒超时，独立于DSH单次1200秒的执行期限；排队较长也可能使等待器先超时。
等待超时返回退出码2，不取消DSH、不删除任务、不重复派单。恢复时等待原输出目录：

```powershell
python workflow/dsh_sessions.py wait --output .workflow/issue-2/run-2 --timeout 1250
```

结果成功返回退出码0，模型失败返回1；session身份不匹配、进程已退出等情况明确报错。
等待器内部每0.2秒检查原子结果文件；这是当前工具调用及时返回，不是向已结束的Codex会话推送通知。
30分钟heartbeat仍负责发现新Issue以及用户中断后续接未完成工作。
