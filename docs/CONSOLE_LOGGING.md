# 精简控制台日志（Issues 14–17）

变更类别：工程诊断工具（R01 接口文档 §1–2；R08 提交与测试）。不改变官方响应字段、协议、动作额度、策略或模拟器；仅调整本地控制台输出的**呈现**，并修正诊断字段的含义。

详细 JSONL trace 与本文档的控制台过滤器**相互独立**：控制台模式不改变逐轮 trace 的采集；trace 启用且未触发队列/磁盘限额时逐轮记录，达到限额时 status.json 明确登记丢失，不假装完整。完整回放、对比与导出见 `docs/TRACE_LOGGING.md`。

## 模式

环境变量 `COMPETITION_HW_CONSOLE`：

| 值 | 行为 |
|---|---|
| `compact`（默认，未知值也回退到此） | 启动身份一次、首回合、阶段切换、首个异常/转变、基地血量阈值、监督器状态转变、每 20 回合汇总 |
| `full` | 恢复旧的逐回合整行 JSON `response {…}`，用于深挖 |
| `off` | 不输出控制台概要行；JSONL trace 不受影响 |

```powershell
$env:COMPETITION_HW_CONSOLE = 'full'   # 诊断时
$env:COMPETITION_HW_CONSOLE = 'off'    # 只要文件 trace
Remove-Item Env:COMPETITION_HW_CONSOLE # 回到 compact
```

模式只过滤控制台概要行；`identity` 启动行仍只打印一次。

## compact 行

每行以 `digest` 开头，`key=value` 以空格分隔，便于 `grep` 与稳定解析：

```
digest s=<run6>/<base6> r=1 start=1 phase=day base=1500 lvl=1 workers=2 pioneers=1 weapons=0 walls=0 robots=0
digest phase r=71 now=night base=1500 robots=70
digest anomaly what=first_errors r=14 total=1
digest anomaly s=<run6>/<base6> what=first_empty r=53 reason=all_weapons_cooling base=1500 ready=0 robots=4
digest anomaly what=first_base_damage r=102 hp=1490 observed_damage=10
digest rollup r=1..20 turns=20 phase=day hp_min=1500 hp_last=1500 walls=0 robots=0 ready=1..3 cmds=51 actions=move:36,collect:10,build:3,acceptTask:1 accept=1 errors=1 plan_max=8.8
digest base_low r=244 hp=1000 threshold=1000
digest critical r=... base_observed_zero=1
digest supervisor r=12 mode=cautious reason=low_hp reserve_pioneer=1 return_steps=4
digest task s=<run6>/<base6> r=12 phase=idle accept=retry_backoff retry_after=14
```

- `s=` 是**进程内关联标签**：由运行前缀与阵营/基地/地图身份生成，不是官方对局 ID。
- `rollup` 每 20 个观测回合输出一次；`final=1` 表示正常退出时的最后一个不完整窗口。被强制杀掉的进程不保证输出 `final`。
- 汇总守恒：每个观测回合恰好进入一个 `rollup`；`errors`、`accept`、`submit` 在窗口间可累加核对。
- `actions=` 只列前 6 类，`accept`/`submit` 始终单列，避免重要计数被截断。
- 每行带 `s=` 区分对局/阵营。周期行还记录当轮金币、积分、武器类型/等级，以及窗口内各操控者的已发攻击次数；这些不代表命中。
- 事件行带回合号（异常行保留触发回合，用于与详细 trace 的 `event_id` 对照）。

## 观测语义

- 全局 `robots` 是**全局可见**的机器人，不等于威胁；`target_us` 仅在官方提供 `targetTeam` 时统计我方的目标数，缺失时保持 unknown。
- 角色消失只记为“不再观测到”，不等于死亡；`base_observed_zero` 只在官方健康值明确为 0 时输出。
- 已下发的 `attack` 不等于命中或造成伤害；`dmg` 只累计**观测到的**基地血量下降。
- 缺失/非法字段保持 unknown，不写成 0；`weapons_ready` 缺失时不推断为“已就绪”。
- 空指令分类只描述可观测状态：`observed_no_robots`、`all_weapons_cooling`、`ready_weapons_no_attack`、`no_live_controllers`、`no_weapons`、`channel_only`、`base_observed_dead`、`invalid_input`、`decision_exception`、`unclassified`。未知冷却不会编造战术理由。

## 错误分类

`build_summary` 现在给出精确分类：`judge_errors_total`、`errors_by_code`、`command_errors`(4)、`task_errors`(1+2)、`llm_quota_errors`(5)、`network_errors`(3)、`unknown_errors`（含 code 0 与非法/越界值）。

兼容字段 `protocol_errors` 现在**只**等于 `command_errors`（errorCode 4）。它不再是“所有错误数”，也不代表判题器判负；任务超时/答题错误不是协议违规。字段缺失时这些值均为 `null`（unknown），不是 0。

## 决策报告（可选，Codex 提供）

`build_summary(..., decision=...)` 接受可选的监督器报告，经白名单与长度限制后进入完整摘要；`None` 时保持历史结构不变。compact 在 `supervisor` 的 mode/reason/实际保留状态或任务阶段变化时输出转变；走一步造成的距离/机器人HP变化不单独刷行。`watch` / `recommended_reserve` 是建议，`reserve_pioneer` 是本轮实际强制回防。报告由 HTTP 处理线程显式传入，后台日志线程不会读取 ContextVar。

## 不应该出现在控制台的内容

不输出原始请求/任务/prompt/executeCmd/凭据，也不自动上传。需要完整字段时使用详细 trace（本地文件），并遵守公司数据外带规则。

## 复现与核对

```powershell
python tools/analyze_console.py reduce <rows.json|rows.jsonl> --dump .\evidence\compact-console.txt
```

工具把日志当作**数据**：拼接拆分日志、按 event/指纹去重、通过同一 digest 重放，并报告相对旧控制台的行数/字节缩减与 `accept`/`submit`/`errors` 守恒。第 14–17 号外部 362 行的聚合结果见 `reports/issue-14-17-logging/`。

控制台是本地工程观测，不代表内网或官方 PASS。

HTTP响应后的后台摘要可能乱序到达。已知较早事件号记为 `late` 并保留动作/错误计数，不冒充对局重启、不回退最新HP或策略状态；回放以完整trace事件索引对齐。
