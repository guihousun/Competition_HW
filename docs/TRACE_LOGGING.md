# 比赛日志、策略回放与模拟器对齐

变更类别：UI/工程工具。依据 DEVELOPMENT_RULES.md R01（接口文档 §1–2）、R02（任务书 §4.1–4.3、§4.5.4）。不增加官方响应字段，不改变动作、数值或视野规则。

## 默认记录什么

正常使用 `python main.py 8080`、包内 `python main3.py 8080` 或 `bash run.sh <port>` 启动后，比赛 POST 自动记录到项目/解包根目录的 `logs/<UTC时间-随机后缀>/`。每次启动独立目录，不覆盖旧局。

控制台默认只保留启动 `identity` 行与精简 `digest` 行（首回合、阶段切换、首个异常、基地血量阈值、监督器状态转变、每 20 回合汇总），不再逐回合打印整段 JSON；`COMPETITION_HW_CONSOLE=full` 恢复旧的逐回合 `response` 行，`=off` 只关控制台。`response`/`digest` 行中的回合号对应详细日志的回合，`event` 对应 `event_id`。模式与字段含义见 `docs/CONSOLE_LOGGING.md`。控制台过滤与详细记录相互独立：任何模式下每个收到的回合都会写入 trace。详细文件包括：

| 内容 | 用途 |
|---|---|
| `start`：代码/包身份、实际 agent 源文件哈希、记录格式与规则基线 | 确认具体测试版本和未提交文件差异，避免官方示例和候选包混淆 |
| `turn.request`：收到的完整 JSON，经凭据过滤 | 保留地图、角色、背包、任务、平台回执及未知新增字段 |
| `turn.response`：实际返回的完整 JSON，经凭据过滤 | 对照所有动作及 `prompt`、`executeCmd` 内容 |
| 请求/响应字节数与原始字节 SHA256 | 核对报文身份；JSON 空白不保留，不能从文件还原原始字节 |
| `plan_ms`、`http_elapsed_ms`、`response_sent` | 区分决策耗时、HTTP 处理耗时与写出失败；写出不代表平台已执行 |
| `summary`、异常类别/消息/代码位置 | 定位空指令、人员和武器状态、策略异常 |
| 连续回合的 `previous_event_id`、`observed_delta`、`previous_action_feedback` | 关联上轮动作与本轮观测/回执 |
| `status.json`、分段 JSONL、正常退出的 `end` | 检查断档、丢弃、磁盘额度、异常中断 |

角色消失只记为“不再观测到”，不能直接断言死亡；分数、血量变化也不自动归因于某个动作。当前没有逐条启发式选择的内部推理日志，不把事后猜测写成决策理由。

`stream_key` 是阵营、基地和地图尺寸生成的关联键，**不是官方对局 ID**。回合重置、跳号、乱序单独标记；同一进程复用同样地图的多局，仍应分局启动或检查回合边界。

## 性能、容量和完整性

JSON 清理、差异计算和文件写入在独立线程运行，HTTP 返回后才入队。日志异常不改变比赛响应。
默认每段约 16 MiB、每次启动最多约 256 MiB，队列最多 64 条/8 MiB，单条请求加响应上限 2 MiB。超限不删除已有证据，记录丢弃原因；少量启动/结束元数据可能超出容量额度。

每条记录 flush，但不承诺断电持久性。强制杀进程可能损失队列或最后一行；无 `end`、丢弃或损坏都不能称为完整采集。多次启动目录累计占用需操作者归档/清理。

启动前可配置（PowerShell）：

```powershell
$env:COMPETITION_HW_TRACE_DIR = 'D:\competition-traces'
$env:COMPETITION_HW_TRACE_MAX_MB = '512'  # 8–4096，仅每次启动上限
python main.py 8080
```

关闭详细采集：`$env:COMPETITION_HW_TRACE = 'off'`。恢复默认：`Remove-Item Env:COMPETITION_HW_TRACE`。
Linux 使用 `COMPETITION_HW_TRACE_MAX_MB=512 bash run.sh 8080`。平台若不提供文件导出，只能先取得控制台身份和概要行；不能承诺我们可访问其容器文件。

## 一次测试后的操作

以下命令在仓库或解包后的 `CoreGeek` 根目录执行。将 `$traceDir` 替换成实际目录；输出文件已存在时工具拒绝覆盖。

```powershell
$traceDir = '.\logs\实际目录名'
python tools/trace_tool.py inspect $traceDir
python tools/trace_tool.py export $traceDir --from-round 65 --to-round 85 --output .\evidence\rounds-65-85.zip
```

窗口导出额外保留前一轮，便于关联失败前动作；完整导出省略两个 round 参数。
ZIP 包含 `events.jsonl` 和清单，命令返回 SHA256。`inspect` 也可直接读导出 ZIP，窗口包不会冒充完整对局。

默认日志过滤常见密钥字段和令牌字符串，不记录 HTTP 请求头。过滤不是完整的公司数据脱敏器，任务正文、地图、日志结果仍可能属于公司数据。导出完全在本地完成，不自动上传；先确认内容允许带出，再在 Issue 附文件或摘录。过滤过的记录明确标记，不能用于声称精确复现。

建议 Issue 附：候选完整 SHA、平台版本、实际结果/覆盖范围、失败回合、`event_id`、导出包 SHA256，以及允许分享的窗口文件。没有某项也可先提交；有证据的修复先推进，不要求用户先定位代码。

## 回放策略与提取学习样本

```powershell
python tools/trace_tool.py replay $traceDir --output .\evidence\policy-replay.jsonl
python tools/trace_tool.py transitions $traceDir --output .\evidence\transitions.jsonl
```

`replay` 在新的 CLI 进程中按原顺序把已记录观测交给**当前代码**，逐轮比较动作。它不执行输出的 `executeCmd`，不发送 `prompt`，不推进模拟器，也不调用网络。回放沿用正式入口的私有字段剥离与视野过滤。策略存在内部记忆，缺少前缀、断档、多局混合或代码版本变化都可能导致差异；差异本身不是策略错误。

`transitions` 仅导出连续、顺序正确、未过滤且已写出响应的观测对：`observation → response → next_observation`，同时带平台回执与事件 ID。它提供数据飞轮的样本基础，不自动训练、修改参数或认定动作成功。

## 模拟器与官方观测对齐

将同一回合的官方请求和本地预测观测分别保存为 JSON：

```powershell
python tools/trace_tool.py compare --actual .\evidence\official-round-72.json --predicted .\evidence\sim-round-72.json
```

支持原始请求或 `request` / `observation` / `state` 包装。回合不同会拒绝标为已对齐；当前差异比较覆盖共同观测的角色位置、血量、背包、等级、冷却及金币/分数字段，并报告出现/消失与重复 ID。完整地图和未知字段仍保存在原请求中，当前工具不做全地图差异报告。

不能从有限观测恢复隐藏敌人、随机刷新和未来波次。正确闭环是：找到最早分歧 → 核对官方规则和版本 → 提炼独立预期回归用例 → 修复模拟器或策略 → 发布新 SHA → 同 SHA 内网复测。日志采集完整、本地回放一致，都不是官方 PASS。
