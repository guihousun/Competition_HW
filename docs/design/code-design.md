# 《未来战争》参赛 Agent 代码设计

> 本文是 `docs/design/strategy.md`（策略稿 v0.3）的**工程落地设计**，用于实现前审核。
> 策略稿回答"打什么"，本文回答"怎么写"。
> 文中标注 `✅实测` 的结论均由 `py -3.13` 解析官方样例/源码得出，非推断。

| 版本 | 描述 | 日期 |
| ---- | ---- | ---- |
| v1 | 分层架构 + 加密日志方案 | 2026-09-13 |
| v2 | 纳入架构评审：样例数据契约实测、任务时序纠错、断路器、日志落盘解耦 | 2026-09-13 |

---

## 0. 一句话结论

保留官方 demo 的入口形态（`python main.py <port>` → HTTP Server），将其**完全重写**为四层架构：

```
transport/  →  protocol/  →  domain/  →  infra/
```

`domain/` 承载策略稿的全部决策逻辑，`protocol/` 是唯一接触线上 JSON 的地方，`infra/` 提供加密日志与跨回合状态。
**第 4 步之后任意时刻的程序都是"可提交"的。**

---

## 1. 为什么不能直接用 demo

`example/CoreGeek/CoreGeek/` 的 demo 证明了入口形态，但其策略是玩具级：

| demo 行为 | 问题 |
|---|---|
| 只建 3 座塔 + 围一圈墙 | 忽略武器升级、火力分配、装备采购 |
| 不做任务（`acceptTask`/`submitAnswer`/`executeCmd` 全未使用） | **放弃 `score₁` 这个最大得分项** |
| 不使用 LLM（`prompt` 恒为空） | 自进化任务无法求解 |
| 不卖矿、不买升级、不读新闻 | 经济线完全缺失 |
| 决策全写在 `brain.py` 一个函数里 | 无分层，无法增量调试 |
| 响应只发 `roleCommandMap` 一个字段 | 与样例不符，需补齐三字段 |
| `_tower_pairs = zip(controllable(), weapons())` | 角色↔武器分配不粘住，每晚重排会白烧动作 |

**入口照抄，策略推倒重写。**

### 1.1 值得保留的 demo 实现（评审确认）

| 位置 | 保留理由 |
|---|---|
| `brain.py::_wall_order` | 一份**已验证**的围墙环枚举。✅实测：在样例基地 (10,24) 上返回 **19 格 = 20 格环 − 入口 (13,22)**；环本身（6×6 − 4×4）恰为 20 格，与策略稿 §6.1 一致。注意它的门是 `(xmax+2, ymin-1)` 这一硬编码，**不是**策略稿要的"背面门" |
| `protocol.py::Unit.range_of_attack()` | **payload 优先、等级表兜底**——样例 payload 与文档矛盾，这个优先级是必须的（见 §2） |
| `brain.py` 的保守移动策略 | 天然规避"目标点争夺"与"位置互换"两类碰撞（碰撞虽不计异常，但浪费整个角色的回合） |
| `grid.py` 的 A* | 逻辑可复用，但需改成"每回合计一次距离场"（见 §3.2 `path.py`） |
| `brain.py::_night` 的 `commands[tower.unit_id] = attack_command(role.unit_id, target)` | **attack 指令的 key 是武器 id，`controllerId` 才是操控角色**——这是最易搞错的一条（见 §6.2） |

---

## 2. 样例实测发现的数据契约缺口（✅实测，P0）

用 `py -3.13` 解析两个样例的结果：

### 2.1 `docs/response.txt` 不是合法 JSON

```
Expecting ',' delimiter: line 66 column 13 (char 1207)   ← 第 66 行 ] 后缺逗号
```

且即使补成合法 JSON，它里面**同一角色 id 重复出现多次**（目录式写法），`json.load` 只会保留最后一条，语义上就是错的。

> **结论：`response.txt` 只能当"字段形状参考"，绝不能当 fixture 去 `json.load`。**
> 唯一合法的 fixture 是 `docs/request.txt`（已实测通过 `json.loads`）。

### 2.2 接口文档 vs 样例的字段缺口

必须容错处理，**缺失时返回默认值并记录异常，而不是崩**：

| 字段 | 文档 | 样例 | 工程动作 |
|---|:--:|:--:|---|
| `playerTasks[].timeoutRounds` | 有 | **无** | 默认值可用；它决定 `score₁` 的标准回合数与保底提交时点 |
| `robot.roles[].targetTeam` | 有 | **无** | 策略稿 §9.1 第 4 条情报通道**不可用** → 降级为"用敌方围墙/基地血量差反推对手 DPS"（这两个全局可见、必定存在） |
| `Role.cooldown` | 有 | **无** | 默认 `0`（安全方向：demo 的 `if tower.cooldown > 0: continue` 依赖它） |
| `attackRange` 取值 | 加特林 L1=3 / 电磁 L1=6 / 火箭 L1=10 | **4 / 7 / 2147483647** | **绝不要把等级-射程表当作权威**；payload 优先、表兜底，并记录不一致日志 |
| `zones` 中 `challengerTaskPoint2` | — | **出现 2 次** | 索引必须是 `dict[str, list[Pos]]`，**不能是 `dict[str, Pos]`** |

`challengerTaskPoint2` 占两格这一点有实际后果：开拓者的"相邻"判定必须取**两格的并集**。例如站在 `(15,16)` 时，到 `(17,17)` 的切比雪夫距离是 2，到 `(16,17)` 才是 1 —— 只记录一个格会误判为"不相邻"，进而触发"离开任务点"导致任务结束。

### 2.3 `request.txt` 里的几何是示意数据，不可用于校准（✅实测）

按策略稿 §6.1 推导挑战者基地 `(10,24)` 的 6×6 环 = `x∈[8,13] × y∈[21,26]`，而样例里挑战者的墙在 `(5,20)`/`(5,21)` —— **不在环上**。
反倒是敌方基地 `(30,10)` 与其墙 `(28,7)` 严格落在环角上（`28=xmin, 7=ymin`）。

> **结论：样例中挑战者那一段是手工示意，敌方那一段才是真实生成的。**
> 不要用 `request.txt` 校准几何锚定；`lastRoundRoleActionResults` 里的 `10030:false` 之类值也不要当真实信号解读。

**正向验证**：`roundNo=85` → `within=(85-1)%130+1=85 > 70` → 夜晚，而样例里恰好有机器人、`phaseTask=""`、`lastSummonTreasureResult=0`。这交叉验证了策略稿 §8 的日历公式，**保持不要改**。

---

## 3. 目录结构

```
main.py                            入口（对齐 demo main3.py）
run.sh                             对齐接口文档的 `bash run.sh port` 启动约定
pyproject.toml                     仅标准库，requires-python >=3.11
src/coregeek/
├── app.py                         组装根：依赖装配 + 单实例 MatchState + 安全响应管线
│
├── transport/                     ── L1 接入层（只管 HTTP 与兜底）
│   └── server.py                  ThreadingHTTPServer；deadline 硬闸；任何异常返回合法空指令
│
├── domain/                        ── L2 领域层（纯策略，只依赖 infra）
│   ├── intent.py                  Intent 语义对象（Move/Build/Attack/AcceptTask…）
│   ├── grid.py                    Pos / 8 向邻居 —— 纯值对象（见 §15.1：Pos 归 domain）
│   ├── calendar.py                昼夜/within/回合预算（roundNo 的纯函数）
│   ├── geometry.py                固定 36 格盒子：基地 2×2 / 武器环 12 格 / 围墙环 20 格 / 背面门
│   ├── path.py                    Board + 8 向 BFS 距离场 + next_step_toward；回防可行性校验
│   ├── world.py                   回合视图 + 派生量（威胁、墙体完好度、现金、可用动作数）
│   ├── history.py                 逐回合时序记录器（波次曲线/对手 DPS/通过率反解的唯一数据源）
│   ├── intel.py                   对 history 的纯分析（策略稿 §9.1）
│   ├── economy.py                 采矿 / 批量贩卖 / 批量采购 / 升级优先级 / 石头账本
│   ├── assignment.py              角色↔武器**粘性**分配 + 单动作仲裁（唯一全局约束求解点）
│   ├── defense.py                 建墙顺序、武器选点、夜间火力分配、修复优先级、应急道具
│   ├── planner.py                 昼夜调度 + 动作 ROI 排序 + 每角色意图分配
│   └── tasks/
│       ├── pipeline.py            任务状态机（纯函数 reduce，不做 I/O）
│       ├── llm.py                 prompt 构造 + 每日配额台账 + 回复解析（严格 JSON）
│       ├── sandbox.py             executeCmd 编排与结果解析
│       ├── skillbook.py           技能库落盘 + 题型指纹检索 + 通过率标签
│       ├── news.py                官方消息规则解析 → 矿价模型（策略稿 §4.4）
│       └── treasure.py            民间传闻台账（占位 + 情报累积）
│
├── protocol/                      ── L3 协议层（唯一接触线上 JSON 的地方）
│   ├── model.py                   Pos/Role/Robot/PlayerTask/WorldNews/Turn，容错解析 + 异常清单
│   └── commands.py                Intent → 线上指令 + 合法性校验器（全函数，永不抛异常）
│
└── infra/                         ── L4 基础设施（横切，不依赖任何上层）
    ├── config.py                  不可变阈值常量
    ├── crypto.py                  加解密（stdlib，encrypt-then-MAC）
    ├── logstore.py                后台线程落盘：压缩+加密 JSONL、轮转
    ├── state.py                   跨回合状态：echo 单槽、会话键、持久化 KV
    └── clock.py                   仅单调时钟：deadline / 耗时预算

tools/
├── decrypt_log.py                 本地解密 CLI（复用 infra/crypto.py）
├── smoke.py                       离线 fixture 驱动器（不起服务，直接调 App.handle）
├── replay.py                      读解密日志离线重跑 planner 做回归比对
└── selfcheck.py                   无 pytest 环境下的断言驱动 + 分层 import-lint（ast）
```

### 3.1 层间契约（硬规则，依赖方向一律由外向内）

| 层 | 允许 import | 禁止 |
|---|---|---|
| `infra/` | 仅标准库 | 任何游戏概念、任何上层 |
| `domain/` | `infra/` | `transport/`、`protocol/`、`infra/logstore` |
| `protocol/` | `domain/`、`infra/` | `transport/` |
| `transport/` | `protocol/`、`domain/`、`infra/` | — |

**跨层禁令（写成 `tools/selfcheck.py` 里的 `ast` import-lint 自动检查）**：

- `domain/**` 不得 import `infra/logstore`（领域层不落盘；日志由 `app.py` 统一收集）
- `domain/tasks/**` 不得 import `domain/defense.py` / `domain/economy.py`
- `protocol/commands.py` 不得 import `transport/**`
- `infra/state.py` 不得 import `domain/**`（否则就变成第二个 pipeline 了）

**关键决策**：`Intent` 定义在 `domain/intent.py` 而**不是** `protocol/`。若放在协议层，领域层就必须反向依赖协议层，分层立即失效。

**收益**：`protocol/commands.py` 是唯一把 `Intent` 变成线上报文的地方，也是唯一做字段校验的地方 → **异常计数红线只有一个入口可控**。

### 3.2 三处评审指出的分层问题（已修）

1. **`clock.py` 名不副实、装了两件事** → 拆为 `domain/calendar.py`（昼夜是 `roundNo` 的纯函数，`pipeline` 算任务剩余回合也要用）与 `infra/clock.py`（只看 deadline）。否则 `domain` 会为了拿"白天/夜晚"去 import infra，破坏上表。
2. **`intel.py` 混了"纯派生"与"时序累积"** → 追加型时序拆到 `domain/history.py`（每回合无条件运行的记录器），`intel.py` 只保留纯分析。策略稿 §11 的 ❓1/3/9 全靠这个记录器。
3. **`planner.py` 三合一** → 抽 `domain/assignment.py` 承载"单动作仲裁 + 角色↔武器粘性分配"，这是唯一的全局约束求解点。

---

## 4. 入口与运行约定

对齐 demo `main3.py` 的形态（已被官方验证可用）：

```python
# main.py
def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main.py <port>")
    port = int(sys.argv[1])
    root = Path(__file__).resolve().parent
    os.chdir(root)
    sys.path.insert(0, str(root / "src"))
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(asctime)s | %(message)s")
    from coregeek.app import run
    run(port)
```

`run.sh` 必须能在判题环境的 **Linux + `python3`** 上跑（本地是 `py -3.13`）：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PY=python3; command -v "$PY" >/dev/null 2>&1 || PY=python
export PYTHONUNBUFFERED=1
exec "$PY" main.py "$1"
```

响应格式（**三字段永远齐备**，样例 `response.txt` 也是三个）：

```json
{
  "roleCommandMap": {"10020": {"action": "attack", "controllerId": "10010", "targetPos": [{"x": 29, "y": 7}]}},
  "prompt": "",
  "executeCmd": ""
}
```

- `roleCommandMap` 的 key 是 **JSON 字符串形式的 id**
- 顶层 `prompt` / `executeCmd` 无条件发送，无内容时发 `""`（**demo 只发了一个字段，这里必须改**）
- **先绑定端口再打印 ready**，避免判题器连上来还没 listen

---

## 5. 安全响应管线（红线 A 的唯一工程保证）

这是本设计的核心。`app.py` 中每回合的固定链路：

```
① 解析 payload（容错，产出 ParseAnomaly 清单）
② 造【最小合法基线】（空 roleCommandMap + 空 prompt/executeCmd）—— 此时已可安全返回
③ 限时富化：planner 产出 Intent → commands 组装
④ 对【组装完成的 roleCommandMap】全量校验，失败的条目逐条剔除并记日志
⑤ 若 deadline 剩余不足 / 校验器剔除了条目 / 观测到 errorCode 4 → 触发 SafetyMode 降级
⑥ 序列化返回（保证合法 JSON + 三字段齐）
```

### 5.1 三条硬保证

1. **请求路径零 IO**：`decide()` 是纯计算。LLM 与沙盒都只是响应里的字符串字段，**永不在请求线程里等待**。
2. **校验器是全函数（total）**：返回 `(ok, reason)`，**任何情况下不抛异常**。"唯一异常来源的守门人"这个定位，正确实现是"守门人永不失控"，而不是"守门人会报警"。
3. **兜底**：`server.py` 用工作线程 + deadline 包裹；超时或异常时返回 `{"roleCommandMap": {}, "prompt": "", "executeCmd": ""}`——空指令集合法且不计异常。

### 5.2 SafetyMode 断路器（由观测驱动，而非"算不出来才降级"）

`errors[]` 是"本轮产生"的错误数组，其中 `errorCode 4`（指令错误）就是异常信号。

```
NORMAL → CONSERVATIVE → MINIMAL → EMPTY
触发条件（任一）：首次见到 errorCode 4 / 校验器剔除了条目 / 响应逼近 deadline
MINIMAL：每角色一条"绝对合法"的指令（move 到相邻空格；无空格则不发）
EMPTY  ：三字段全空
```

`EMPTY` 永远不会造成异常（无指令是合法的），所以它是最强兜底。

---

## 6. 指令校验器

### 6.1 全局约束

| 约束 | 说明 |
|---|---|
| `action` ∈ 11 个动作码白名单 | 精确小写字符串，见 §6.2 |
| key ∈ `str(teamOur.roles[].id)` | 不发敌方 id、不发 `health<=0` 的单位 |
| **同一 id 只允许出现一次** | dict 会静默吞掉重复，必须显式拒绝并记日志 |
| 顶层三字段永远齐 | `roleCommandMap` / `prompt` / `executeCmd` |
| `targetPos` 每元素恰好含 `x`,`y` 两个 int | 不透传 payload 原对象（可能带多余/缺失字段） |
| `num` 是 `int` 且 `>=1` | 不是 bool、不是字符串 |

**校验时机有两道**：Intent→指令时校验一次；**组装完成的最终报文再全量校验一次**。后者同时覆盖红线 A 的"格式错误"与"指令错误"两类。

### 6.2 逐动作约束表

| action | key 指向 | 必填 | 取值约束 |
|---|---|---|---|
| `move` | 角色 | `targetPos`(len=1) | 目标在 `[0,width)×[0,height)` 内；切比雪夫距离 ≤1；目标格当前无单位/建筑/矿/中立 |
| `attack` | **武器** | `targetPos`、`controllerId` | **仅夜晚（硬门）**；见下方专表 |
| `sell` | 角色 | `name`,`num` | `name ∈ {stone,iron,copper} ∩ 背包`；`num = clamp(1, 背包内该矿数量)`，为 0 则不发 |
| `buy` | 角色 | `name`,`num` | `name ∈ weaponShopList[].name`（加静态白名单兜底）；`num = clamp(1, min(可负担, 背包余量))`，为 0 则不发 |
| `build` | 角色 | `name`,`targetPos`(len=1) | **仅白天**；`name ∈ {wall,gatling,railgun,rocket}`；`wall` 需背包有 `stone`；武器需金币 ≥ 成本且武器数 <3 |
| `remove` | 角色 | `targetPos`(len=1) | 该格存在己方 `wall`；距离 ≤1 |
| `acceptTask` | **仅开拓者** | 无 | `phaseTask == ""`；目标任务点 `isValid && coldDownRounds==0`；开拓者与**该任务点任一格**距离 ≤1 |
| `submitAnswer` | **仅开拓者** | `taskAnswer`(非空) | `phaseTask != ""`；SafetyMode 未禁止 |
| `summonTreasure` | **仅开拓者** | `targetPos`(len=1), `item` | `item` ⊆ 背包且**与推断集合完全一致**（错一件=结果码3 但物品照样消耗）；本轮不启用 |
| `drop` | 角色 | `name` | `name ∈ 背包` |
| `collect` | **仅工人** | `targetPos`(len=1) | 该格 `neutralType ∈ {stone,iron,copper}`；距离 ≤1；背包未满 |

**`attack` 专表**（最易踩坑，也是最可能产生异常的位置）：

| 项 | 规则 |
|---|---|
| 相位门 | **仅夜晚可发**。`commands.py` 的 `attack` 构造函数**要求显式传入 `is_night=True`** 才能通过——不依赖 defense 模块自觉 |
| 落点数 | **电磁狙击炮恒为 1**（"电磁狙击炮只能攻击一个目标"）；**加特林/火箭 = 当前 level**（L1→1、L2→2、L3→3） |
| 锥形约束 | **加特林**：任意两落点相对武器的方向夹角 ≤90°，否则**整次攻击非法** → 兜底用"全部填同一坐标" |
| 射程 | 用 payload 的 `attackRange`；payload 缺失才查等级表（`gatling:(3,5,7) / railgun:(6,8,10) / rocket:(10,15,∞)`） |
| `controllerId` | 指向己方存活 `worker`/`pioneer`，且该角色本回合未被分配其他动作或另一座武器 |
| `/key` | **key 是武器 id**（demo：`commands[tower.unit_id] = attack_command(role.unit_id, target)`） |

**`use` 的 `targetPos` 需求表**（任务书 §八 明确把"眩晕法宝/范围炸弹未指定 `targetPos`"列为异常样例）：

- **必需 `targetPos`**：`WallFixer`、`DizzyWeapon`、`Bomb`、`WeaponUpgradeVoucher1/2`、`WallUpgradeVoucher1/2`、`StationUpgradeVoucher1/2`
- **不需要**：`Medicine`、`*-RobotSummonOrder`（四种召唤令）
- **未知物品 → 一律不发**。`name` 词表来源 = `weaponShopList[].name` ∩ 背包现有项，加静态白名单兜底；**不凭中文名猜**

### 6.3 最可能产生异常的位置（按风险排序）

1. **白天发 `attack`** —— 防守模块算落点时忘看相位，或 `within==70/71` 边界 off-by-one → 已用 §6.2 的硬门封死
2. **落点数 ≠ 规定数** —— level 取 payload，不用本地表推
3. **`controllerId` 缺失/指向阵亡角色/一角色控两武器**
4. **`use` 高级物品缺 `targetPos`**
5. **无任务时发 `submitAnswer` 或 `executeCmd`** —— 沙盒命令的门是 `phaseTask != ""`，不是"我想探测"
6. **`prompt` 超配额**（每日 3 次，任务期间不计）—— `errorCode 5` 是否计入 5 次异常**文档没写**，保守当作会计入；配额台账每回合用 `errors[]` 对账
7. **响应超 5 秒** —— 现实威胁排序：请求线程内做日志加密+fsync ≫ 组合爆炸枚举 > 循环内反复跑 A*

---

## 7. 并发安全

### 7.1 一把全局锁 + 响应缓存

`decide()` 是纯计算（沙盒与 LLM 都只是字符串字段），所以：

- **一把 `threading.RLock` 罩住整个"读状态→算→写状态"临界区**。41×32 上的 A*/BFS 是微秒级，锁的代价可忽略；细粒度锁在这里只会制造 bug
- **获取锁要带超时**：`lock.acquire(timeout=deadline_remaining)` 失败即返回缓存响应。否则第一个卡住的请求会把重试请求一起拖死
- 线程池有界（`ThreadingHTTPServer` 无界起线程），`daemon_threads = True`

### 7.2 同回合重复请求：幂等，且用缓存而非重算

- 保存 `last_round_no` + `last_response_bytes`
- `roundNo == last_round_no` → **原样返回缓存字节**。重算会二次消费 echo slot，且状态已推进，结果会不同
- `roundNo < last_round_no` → 返回上一轮缓存；缓存已滚掉则返回兜底响应，绝不重算
- **不变量：每个不同的 roundNo，echo slot 只推进一次**
- **业务层同样要幂等**：重放一轮时任务线不能再发 `acceptTask`/`executeCmd`（会重复消耗每回合 1 条命令的预算与任务点冷却）。所以"本回合已产出决策"必须落在 `state` 里

### 7.3 `roundNo` 跳号

判题器可能因超时/重试让你看到 `R` 然后 `R+2`，此时"延迟恰好 1 回合"的假设破了。规则：**回显按"到达即生效"处理，并校验偏斜**：

| `roundNo - pending.issued_round` | 处理 |
|---|---|
| `==1` | 正常 |
| `>1` | 标记 `STALE_SKEW`，**降低该答案置信度、不因它做"立即提交"决策**，但**仍作为技能库证据保留**（免费信息，别丢） |
| `llmResp` 到达时 `phaseTask` 已变 | 一律只当证据，不当答案 |

### 7.4 HTTP 层隐患（demo 抄过来会带病）

- `self.rfile.read(length)` 会永久阻塞 → `self.connection.settimeout(...)`
- 未设 `protocol_version = "HTTP/1.1"` → 判题器多半用 keep-alive 连接池，1.0 会每次断连，放大"建立连接 10 秒超时"的风险
- `Content-Length` 缺失/非法 → `read(0)` 后 `json.loads(b"")` 抛异常；已有 `try` 兜住，但要确保**兜住后仍是 200 + 合法 JSON**
- 显式实现 GET/HEAD 返回空指令（否则默认 501 HTML，污染日志）
- **`do_POST` 最外层再包一层 try**：`send_response` 之后抛异常 = 半截响应 = 判题器侧格式错误
- body 大小上限（如 4MB），防单次巨包拖死

---

## 8. 跨回合状态

### 8.1 三类状态，别混

| 类别 | 例子 | 归属 | 落盘 |
|---|---|---|---|
| **世界派生量** | 敌方围墙、金币、机器人、昼夜 | **不持有**，每回合从 payload 重算（纯函数） | 否 |
| **回显态** | `prompt→llmResp`、`executeCmd→lastCmdResult` | `infra/state.py` 单槽 registry，按 roundNo 键控 | 否 |
| **习得态** | 技能库、宝藏三要素、波次曲线、锚定校验结果、价格模型、观测到的 `attackRange`、角色↔武器分配 | `infra/state.py` 持久化 KV，**按 `teamOur.type` 分键** | 是 |

### 8.2 `infra/state.py` 与 `domain/tasks/pipeline.py` 的切分

> **`infra/state.py` 持有字节与轮次，不懂语义；`domain/tasks/pipeline.py` 持有解释与策略，不做 I/O。**

`infra/state.py` 只提供：`begin_round(round_no, session_key)`、`issue_prompt(text, tag)`、`pending_prompt()`、`issue_cmd(cmd, tag)`、`pending_cmd()`、`take_echo(llm_resp, cmd_result) -> EchoPair`、持久化 KV 读写。**`take_echo` 只做"打包 + 轮次戳"，不做任何"这是不是我想要的答案"的判断。**

`pipeline.py` 只提供纯函数：`reduce(runtime: TaskRuntime, turn: Turn, echo: EchoPair) -> (TaskRuntime, list[Intent])`。

### 8.3 echo 单槽的三个必做约束

`llmResp` 和 `lastCmdResult` **没有关联 id**，唯一可用的关联是"我上回合发的那一条"。所以：

1. **绝不并发两条在途 prompt**（同时只允许一个未回填的 slot）
2. **消费恰好一次**：`if round_no <= self.applied_round: return cached`，否则重复 POST 会把同一条 `llmResp` 匹配两次，把答案"吃掉"
3. **"上回合发了 prompt 但本回合 `llmResp` 为空"必须显式处理**（大概率是配额超限，对应 `errorCode 5`）：slot 标记 `DROPPED`，且**不允许后续 `llmResp` 再回填到这个老 slot** —— 否则会把 A 问题的答案当成 B 问题的答案。**这是最容易埋的雷。**

### 8.4 会话键与半场边界

**`roundNo` 会在下半场从 1 重新开始，所以 `roundNo` 不是会话键。**

```
session_key = (teamId, teamOur.type, roundNo)
```

- `roundNo` 回退（或 `==1`）→ **会话切换**：清空所有"本场态"（echo slot、任务运行时、对手基线曲线）
- **保留**所有"跨场态"（技能库、宝藏三要素、观测到的几何/射程/波次拟合参数）——对应策略稿 §12.4
- **波次曲线与几何锚定按 `type` 分键**（上下半场换边，出生区相对基地的方位会变）
- **技能库按任务指纹分键**（与阵营无关）

---

## 9. 任务线

### 9.1 时序纠错（P0，影响任务线全部设计）

策略稿 §5.2 写"回合 R: `acceptTask` + 同时提交 prompt；回合 R+1: 读 `llmResp` → 同回合 `submitAnswer` → 实际回合 = 1"。

**这在时序上不可能**：任务原文只从 `phaseTask` 来，而 `phaseTask` 是"**当前已领取**任务的原文描述"。你在 R 回合**发** `acceptTask`，R 回合的请求里 `phaseTask` 必然还是空；任务原文最早在 **R+1** 的请求里才可见。所以：

```
R    : acceptTask                 （+ 一条与题目无关的通用环境探测 executeCmd 合法）
R+1  : 读到 phaseTask（首次拿到题面）→ 发 prompt（+ 必要的 executeCmd）
R+2  : 读到 llmResp → submitAnswer        ← LLM 路径下界 = 2
```

**实际回合 = 1 只有一条路：R+1 直接用本地技能库/规则解提交。**

代入 `score₁ = 奖励 + 5 × timeoutRounds / (完成回合 − 接取回合)`，设 `timeoutRounds=30`：
- LLM 路径（2 回合）：`5×30/2 = 75`
- 技能库路径（1 回合）：`5×30/1 = 150`

**技能库不是"优化项"，它是唯一能吃到满时间项的路径。** 相应地策略稿 §8 的阶段目标应从"交付回合稳定 ≤3"改为"**技能库命中率**"。

### 9.2 部分提交不终止任务（✅由文档推出）

任务书 §五 列出的**任务结束的 4 种行为**是：任务被完成 / 超时 / 离开任务点周围一格 / 开拓者死亡。**"提交答案"不在其中**。且接口文档说"任务超时后……以**之前提交过的通过率最高的答案**计算积分与金币"——"之前提交过的"（复数）说明允许并保留多次提交。

> **推论（高置信）：部分提交不终止任务，且判题器保留历史最高通过率。**
> 待首局 `errors[]` 与 `phaseTask` 变化实测确认。

**战略含义（重要）**：
- 部分完成分 = `奖励 × 通过率`，**没有时间项** → 低置信度答案"早交"和"晚交"分数完全等价
- 结合"保留最高通过率" → **重复提交在当前答案上是弱优势策略**（单调不减，最坏情况等于不交）
- 策略稿 §5.2 第 3 条的直觉是对的，但理由应从"保底"改为"**零机会成本的迭代**"

### 9.3 状态机

| 状态 | 每回合动作 | 退出条件 |
|---|---|---|
| `IDLE` | 任务点 `isValid && coldDownRounds==0` 且余下白天回合够往返 → `acceptTask` | → `ACCEPT_PENDING` |
| `ACCEPT_PENDING` | 等待 `phaseTask` 非空 | 有描述 → `SOLVING`（**记下 `accept_round`**）；冷却 >0 且始终为空 → `ABORTED` |
| `SOLVING` | ①技能库命中 → 直接组答案 ②发 `prompt`（LLM）③发 `executeCmd` 取证（1 条/回合） | 得到答案 → `SUBMITTING` |
| `SUBMITTING` | 提交当前最佳答案；等 `lastRoundRoleActionResults` 与 `errors[]` | 见 §9.4 |
| `COOLDOWN` | 记录 `coldDownRounds`（刷新 30 回合），做采购/宝藏/回防 | `coldDownRounds == 0` → `IDLE` |
| `ABORTED` | 清理在途异步调用并记录损失 | → `IDLE` |

### 9.4 必须处理的竞态

| # | 竞态 | 处理 |
|---|---|---|
| 1 | 提交回合收到的是上一条命令的结果，而任务已结束 | 门：`phaseTask != ""` **且** `pending_cmd.issued_round + 1 == roundNo` |
| 2 | `executeCmd` 结果在任务结束后才到 | 不丢：写入技能库，标注"该题未在有效期内解开" |
| 3 | deadline 估算偏移导致最后一轮还在跑沙盒 | **剩余回合 ≤2 时不发 `executeCmd`**（结果来不及用） |
| 4 | 重复提交 | 只允许 1 次在途提交；提交后到"确认任务未结束"之前不再提交 |
| 5 | `acceptTask` 结果未知就发 `submitAnswer` | 门：`lastRoundRoleActionResults[str(pioneer_id)] == true` 且 `phaseTask != ""`。⚠️ **该 map 的 key 是字符串**，用 int 查会全 miss，误判成"所有动作都失败" |
| 6 | 开拓者阵亡 | 用"上次出现在 `roles` 中的回合"区分"阵亡"与"不可见"。阵亡 → 清运行时、记失败、停止对该 id 发任何指令 |
| 7 | **主动离开任务点导致任务结束** | 开拓者在任务期间**必须驻留**在任务点任一格的切比雪夫距离 ≤1 内。任何移动前必须校验"移动后仍相邻"——这是 `path.py` 距离场的用途之一 |
| 8 | 任务点 2 有两格 | 相邻判定用两格并集 |
| 9 | 两个任务点冷却交替 | 状态按任务点分槽；`coldDownRounds` 是"点"的冷却，任务超时是任务的寿命，不可混用 |
| 10 | 重放/重复请求 | 见 §7.2，任务动作每回合只产出一次 |

### 9.5 免费的监督信号：用 `totalScore`/`goldNum` 反解通过率

`submitAnswer` 只回 `errorCode 2`（对/错），不告诉你通过率。但请求里每回合都有 `teamOur.totalScore` 与 `teamOur.goldNum`：

```
Δgold (提交回合 → 下回合)  = goldReward × 通过率
Δscore(提交回合 → 下回合)  = scoreReward × 通过率      （部分完成）
                       或  = scoreReward + 5×std/Δrounds  （完全正确）
```

两条独立方程 → **可同时解出"是否完全正确"与"通过率"**。`domain/history.py` 记录每回合 `(roundNo, totalScore, goldNum, phaseTask, playerTasks[*].coldDownRounds/isValid)`，任务线在提交后下一回合做差分解算，把 `(任务指纹, 答案, 通过率)` 写进技能库。

> **这等于给技能库配了有监督标签**，比"猜格式对不对"高一个量级，且零成本。

### 9.6 prompt 契约与沙盒编排

- prompt 末尾强制**严格可解析的输出契约**（"只输出一个 JSON 对象，键名与题目要求完全一致，不要解释"）；解析只用 `json.loads` + 正则提取，**绝不 eval/exec**
- prompt 内嵌 `roundNo` + 任务指纹 + "第几次尝试" → 回复可校验是否对应当前任务
- 技能库按"题型指纹（题面模板 + 参数形态）"索引，**不按原文哈希**；`answer_format` 假设也进技能库并被通过率反馈修正
- 沙盒结果解析：`[exitCode:N]\n<输出>` / `[TIMEOUT]\n<部分输出>` / `[JUDGER_ERROR]\n<原因>` / 超 64KB 末行 `[TRUNCATED]`。**前缀只在开头匹配**（否则输出内容里出现同形字符串会误判）
- `[TIMEOUT]` 与 `[JUDGER_ERROR]` **不计队伍异常**（接口文档明说）→ 可以放心用得激进一些
- 命令自身加超时（`timeout 12 python3 -u - <<'EOF' … EOF`），脚本内输出机器可读哨兵（`###BEGIN k###`/`###END k###`）
- **不要假设沙盒文件跨回合持久**：必须在"无持久化"假设下也能 2 回合解完；持久化只当加速项。第一条命令永远是**廉价通用探测**（shell 方言、python 版本、可用标准库），结论缓存整场

---

## 10. 加密日志

### 10.1 方案（仅标准库）

```
master = pbkdf2_hmac('sha256', 内置口令, 内置salt, iters)     # 密钥内置在代码里
k_enc  = hmac(master, b'enc')      # 两个独立密钥，绝不复用
k_mac  = hmac(master, b'mac')
每条记录：
  nonce     = secrets.token_bytes(16)                          # 每条随机，不用全局计数器
  keystream = concat( hmac(k_enc, nonce + i.to_bytes(4,'big')) for i in 0,1,2,… )[:len(pt)]
  ct        = pt XOR keystream
  tag       = hmac(k_mac, header + nonce + ct)                  # encrypt-then-MAC
```

**关键点，逐条对应要避开的经典错误**：

| 经典错误 | 这里的规避 |
|---|---|
| 全局计数器做 nonce → 重启/多写者后 nonce 复用 → 密钥流复用 | **每条记录 `secrets.token_bytes(16)`**，天然多进程/多重启安全 |
| 同一把密钥既加密又认证 | `k_enc`/`k_mac` 由 master 派生分离 |
| MAC 只覆盖密文、不覆盖头部 | `tag` 覆盖 `header`（含版本/kdf/iters/salt）+ nonce + ct |
| MAC-then-encrypt | 严格 encrypt-then-MAC |
| 自研 AES | **不做**。HMAC-SHA256 当 PRF 是标准做法 |
| PBKDF2 迭代数堆极高 | **密钥内置在代码里，PBKDF2 对读源码的攻击者毫无意义**（这是"日志混淆"不是安全边界）。迭代数写进头部、取小值保速度 |

**容器格式**：首行明文元数据 `{"v":1,"kdf":"pbkdf2-hmac-sha256","iters":N,"salt":"b64"}`，其余每行一条 base64 密文记录（`nonce|ct|tag`）→ 解密工具不依赖进程状态，跨机器可解。

**压缩再加密**：`zlib.compress(request_json)`。JSON 重复度高、压缩比大，顺带把请求体大小从加密开销里省回来。

**其他**：MAC 校验失败 → 记一条明文哨兵并继续，**不抛异常**（否则日志模块会拖死对局）；轮转按大小 + 按天双条件；`infra/config.py` 加 `LOG_ENCRYPT=True/False` 开关，本地开发走明文迭代快得多。

### 10.2 落盘必须在请求线程之外（P1）

每回合对约 20KB 明文做 HMAC 流加密 + `fsync`，是 §6.3 第 7 项里最大的现实超时威胁。

> **请求线程只往 `queue.Queue` 投一条记录，单个后台 writer 线程消费并落盘。**
> 队列有界，满则丢弃并计数（**丢日志远好过丢比赛**）。`main.py` 注册 `SIGTERM`/`atexit` 钩子做最终 flush（判题器会杀进程）。

### 10.3 本地解密

```bash
py -3.13 tools/decrypt_log.py logs/match-20260913.cgl              # 全部解密到 stdout
py -3.13 tools/decrypt_log.py logs/x.cgl --round 85                # 只看第 85 回合
py -3.13 tools/decrypt_log.py logs/x.cgl --grep attack --out d.jsonl
```

### 10.4 诚实的边界

密钥内置在代码中意味着这**只防"顺手翻看"，不防"拿到代码的人"**。这是需求阶段明确选定的方案，此处如实标注，不宣称它是真正的保密手段。

---

## 11. 日志字段（为赛后复盘服务）

**核心原则：日志记"意图 + 派生量"，而不只是"指令"。** 指令可以从意图重放，意图不能从指令反推。

| 分组 | 字段 |
|---|---|
| 身份 | `ts`、`session_key(roundNo,type)`、`within`、`day`、`phase` |
| 输入 | `req_sha256` + 原始 request（压缩加密）、**解析异常清单**（缺失字段/类型容错/未知枚举） |
| 世界快照 | `goldNum`、`totalScore`、双工人背包按矿种计数、基地 hp/level、**每面墙 `(pos, level, hp, 本回合预计承伤)`**、每座武器 `(type, level, hp, cooldown, controller)`、机器人按类型计数 + 距基地最近距离、**威胁估值 vs 吸收估值** |
| 决策 | 每角色 `(id, intent_kind, 选择原因枚举, 参数, 最终 action)`；**被仲裁否决的意图**（最有价值——它解释了为什么某角色没去修墙） |
| 异步态 | `pending_prompt`(issued_round, 指纹, 全文)、`llmResp` 原文与解析结果、`pending_cmd` 命令串、`lastCmdResult` 原文与解析结果（含 `[TIMEOUT]`/`[TRUNCATED]` 标志） |
| 任务时间线 | `accept`/`submit`/`end` 事件、结束原因推断（超时/离开/阵亡/完成）、deadline 估算 vs 实际、**由 Δscore/Δgold 反解出的通过率** |
| 红线账本 | `errors[]` 原文、`errorCode 4` 累计数、当前 `SafetyMode`、**校验器剔除的候选指令（近失事故，全日志最有价值的行）**、返回前 deadline 余量(ms) |
| 校准 | payload `attackRange` 与本地表的差异、锚定校验结果、加特林锥形合法性、`targetPos` 个数 |
| 性能 | `decide_ms`、`lock_wait_ms`、日志队列深度 |

外加 **stdout 每回合一行明文摘要**（崩溃后能看最后状态）：

```
R85 D2 夜 w5 | gold 20 | station 1500 | walls 18/20 hp92% | threat 340 vs absorb 610 | task T1 exec rem24 | mode NORMAL | 4ms
```

---

## 12. 防御与经济

### 12.1 几何：已用 demo 实测验证（✅）

以样例基地 `(10,24)` 跑 demo 自己的 `station_footprint` / `_tower_sites` / `_wall_order`：

```
基地 footprint = (10,23),(10,24),(11,23),(11,24)   → 证实 pos 是"左上角"（x 最小、y 最大）
武器环（4×4 去掉基地）        = 12 格   ✓
围墙环（6×6 去掉 4×4）        = 20 格   ✓
demo _wall_order 产出         = 19 格 = 20 格环 − 门 (13,22)
```

门的位置是唯一自由参数。demo 开在 +x 侧（朝向地图中心），策略稿要求开在**背向机器人出生区**那一面——需实测出生区后确定，落成 `config.py` 的可切项。

**一切坐标由 `teamOur.type` + 己方 station pos 推导，禁止绝对坐标。**

### 12.2 机器人规则（✅任务书 §4.7）

- 夜晚**第一个回合统一出现**，数量随天数增加（官方未给增长曲线）
- 会攻击阻挡其移动的单位（角色/建筑）
- **黑夜结束后，第二天早上第一个回合残余机器人自动清除** → 危险只在夜间；夜间打坏的墙必须白天补
- 召唤令叠加在基础浪潮之上

### 12.3 夜间动作优先序

```
① 是否有墙"本回合会被打爆" → 最高优先，派最合适角色用 WallFixer
   （build 仅限白天，夜间不可重建，破口整夜有效）
② 火力分配：优先"本轮可击杀"，再按距离基地最近 / DPS 最高
③ 必须道具介入的场景（BOSS 贴墙 → 眩晕；小中集群 → 炸弹）
④ 剩余动作：火箭冷却空窗时该角色转为修复 / 待机
```

### 12.4 武器与经济的量化依据（✅任务书）

| 武器 | 机制 | 关键数字 |
|---|---|---|
| 加特林 | 多目标须在**同一 90° 锥形**内；每颗子弹沿自身弹道飞行，命中弹道上**最近**的一台机器人即消耗 | 10 伤害/颗 |
| 电磁狙击炮 | **恒 1 个目标**；能量沿弹道**穿透**，路径上每只存活机器人受 `min(剩余能量, 当前血量)`，能量按造成伤害扣减 | 升级增加能量 |
| 火箭发射台 | **指哪打哪不被阻挡**；中心 20，周围 8 格溅射 = 中心伤害一半；多枚落点重叠**叠加** | 发射后 3 回合冷却空窗 |

机器人：小型(攻5/距3/血40/1分)、中型(10/3/60/2分)、大型(20/3/500/4分)、BOSS(40/3/800/10分)。

### 12.5 需要补的经济机制

- **石头账本**：每面墙被拆后重建需 1 石，20 面 = 20 石。`economy.py` 显式维护 `stone_target = 当前缺口面数 + 缓冲`，采集优先级由它驱动，而不是笼统的"石矿优先"
- **动作 ROI 排序表**：窗口期最贵的资源是"工人动作"而非金币。控武器 / 修墙 / 用道具 / 移动 / 采集 各自的机会成本要有一个统一排序函数，否则每个模块都觉得自己最优先
- **两条红线的优先级**：必须在"保住红线 A"与"拿任务分"之间选时，**红线 A 优先**——写成 `planner` 的一行硬规则

### 12.6 门的代价模型（策略稿 §6.3 修正）

留门 = 少 1 面墙（1000 血），开门/关门各花 1 个工人动作 + 1 石头。但**封门在夜间做不了（`build` 仅白天）**，所以"夜间是否封门"实际是"白天封不封"的决策。

> 把封门决策挪到白天末尾（`within 64~70`），或干脆**常闭 + 用 `remove` 开门**（`remove` 无白天限制，且夜里不需要进出）。

---

## 13. 策略稿 → 模块的落地映射

| 策略稿章节 | 落地位置 |
|---|---|
| §6.1 固定 36 格盒子 | `domain/geometry.py` |
| §6.2 武器选型 / §6.3 建墙顺序与门 / §6.4 火力分配 / §6.5 动作预算 / 预防性修复 | `domain/defense.py` |
| §4 经济线与采购优先级 | `domain/economy.py` |
| §5.2 自进化任务 | `domain/tasks/*` + `infra/state.py` |
| §5.3 宝藏 / §4.4 新闻 | `tasks/treasure.py`（占位）/ `tasks/news.py` |
| §8 日程模板与回防倒计时 | `domain/planner.py` + `domain/calendar.py` |
| §9.1 情报通道 / §11 波次曲线 | `domain/history.py`（记录）+ `domain/intel.py`（分析） |
| §12.3 阈值速查 | `infra/config.py` |
| §12.4 跨半场复用清单 | `infra/state.py` 的会话键机制（§8.4） |

---

## 14. 本轮不做（明确划界）

- **机器人召唤令的破局决策**（§9.2）——依赖对手临界判断，无实盘数据无法标定，留接口
- **宝藏三要素的 LLM 推断**——只做台账累积与接口占位
- **跨半场资产复用的自动迁移**（§12.4）——先保证单半场正确
- **模拟判题器**——需求阶段明确选定不写

> 注意：约束禁的是**模拟判题器**，没禁**fixture 驱动与回放器**。`tools/smoke.py`（fixture 驱动）与 `tools/replay.py`（离线重跑）是允许且必需的。

---

## 15. 实现顺序（每步都有可验证的检查点）

| 步 | 内容 | 检查点 | 状态 |
|---|---|---|---|
| 1 | `infra/config.py` + `infra/crypto.py` + `infra/logstore.py` + `tools/decrypt_log.py` | 加解密往返一致；篡改 1 字节必须解密失败（MAC 生效） | ✅ 完成 |
| 2 | `protocol/model.py` + `tools/selfcheck.py` 骨架 | `docs/request.txt` 解析出的回合/角色/机器人数量与样例一致；缺字段不崩且产出异常清单 | ✅ 完成 |
| 3 | `domain/grid.py` + `domain/geometry.py` + `domain/calendar.py` + `domain/path.py` | 以 (10,24) 锚定断言 4/12/20 格与门位；round 85=黑夜、71/131 边界 | ✅ 完成 |
| 4 | `domain/intent.py` + `protocol/commands.py` + `transport/server.py` + `app.py` + `main.py` + `run.sh` | **此时已可起服务**：POST 样例返回 200、三字段齐、报文合法、耗时 <5s | ⬜ 下一步 |
| 5 | `domain/world.py` + `domain/planner.py` + `domain/economy.py` + `domain/assignment.py` | 白天能产出建墙/采矿/买卖/升级指令；角色↔武器分配跨回合粘住 | ⬜ |
| 6 | `domain/defense.py` | 黑夜能产出合法 attack（锥形/落点数/相位门全过）、建墙顺序、修复优先序 | ⬜ |
| 7 | `domain/tasks/*` + `infra/state.py` + `domain/history.py` | 任务状态机全路径走通（含 ABORTED、竞态 10 项）；通过率反解可用 | ⬜ |
| 8 | `domain/intel.py` + `tools/replay.py` + README | 波次曲线与对手情报落盘；README 写清启动与解密用法 | ⬜ |

**第 4 步是关键分水岭**：此后任意时刻的程序都是"可提交"的，后续步骤只是把空指令替换成真策略。

### 15.1 第 3 步落地时新增的已确认事实

这一轮把 demo 的 `grid.py` / `protocol.py` / `brain.py` 读透并用真实样例数据交叉验证，得到 5 条**会影响策略**的事实：

| # | 事实 | 依据 | 影响 |
|---|---|---|---|
| 1 | **开局已送 3 座武器**（gatling (9,24) / railgun (10,25) / rocket (9,25)），而"武器工事全局同时最多 3 座" | 样例 `teamOur.roles` + 任务书 L205 | ⚠️ **`WEAPON_BUILD_COST=25` 这条支出线实际上在武器被打掉前是死的**。金币优先级必须重排：升级券 ≫ 新武器 |
| 2 | `attack.controllerId` 是**字符串**（demo 写 `str(controller_id)`） | demo `protocol.attack_command` | 校验器必须按字符串处理，否则 5 次异常红线 |
| 3 | `build` **仅工人 + 仅白天**；`attack` **仅黑夜**；`sell` 需在小贩周围一格内 | 任务书 L137 / L122 / L132 | 佐证策略稿 §6.3「夜间封门不可执行」 |
| 4 | 阻挡移动的格子 = 双方全部建筑 + 双方全部角色 + 机器人 + 中立单位 + 4 个任务点 + 矿区 | 任务书 L85 | 与 demo 的 `land()`（要求 `neutralType=="land"`）完全一致 → **矿区/商店/任务点必须"走到相邻格再动作"** |
| 5 | 己方单位视野 = 4；机器人**全图可见** | 任务书 L95 | 情报线只能拿到视野内的敌方建筑；但机器人波次可全图统计 → 波次曲线可靠 |

另外两条实现层面的修正：

- **`Pos` 从 `protocol/` 迁到 `domain/grid.py`**。原方案让 `domain/geometry.py` 去 import `protocol.model.Pos`，直接违反 §3.1 的依赖方向（被 import-lint 抓到）。`Pos` 是纯值对象，归 domain；`protocol/model.py` 改为 re-export，`model.Pos` 仍可用。
- **半场口径改为取模**：任务书只说"下半场互换位置"，未说 `roundNo` 是否重置。`within_half = (roundNo-1) % 1300 + 1` 在两种语义下答案相同，因此不必赌。原 `halves_remaining()` 算的其实是"剩余天数"，已改名 `days_remaining_in_half()`。

---

## 16. 验证方式

1. `py -3.13 -m compileall src tools main.py` —— 语法与导入完整性
2. **`tools/smoke.py --selftest`**（纯函数自检）：
   - 几何：以样例 station (10,24) 锚定，断言武器环 = 12 格、围墙环 = 20 格、门在背面
   - 指令：对每个 Intent 断言产出的报文含全部必填字段、动作码在枚举内、`attack` 落点数与相位合法
   - 加密：加密→解密往返一致；篡改任意一字节必须解密失败
   - 日历：round 85 → 黑夜，round 71/131 边界正确
3. **fixture 驱动器**（不起服务，直接调 `App.handle(payload_bytes) -> response_bytes`），覆盖：
   白天无任务 / 夜晚 BOSS 贴墙 / 任务中收到 `llmResp` / 任务中收到 `lastCmdResult` / `gold=0` / 背包满 /
   `playerTasks` 缺 `timeoutRounds` / `robot` 缺 `targetTeam` / `roundNo=1` / `roundNo=131`（半场边界）/ 字段大面积缺失 /
   **同回合重复 POST**（幂等）
   断言：响应是合法 JSON、三字段齐、每个动作通过校验器、无异常抛出、耗时 < 50ms
4. **`tools/selfcheck.py`**：无 pytest 环境下的断言驱动 + 分层 import-lint（`ast` 解析各模块 import，检查 §3.1 规则）
5. **`tools/replay.py`**：读解密日志里的原始 request 离线重跑 planner，比对输出 —— **唯一的回归测试手段**（每改一次防御逻辑，无法通过"打一局"验证）
6. **起服务**：`py -3.13 main.py 8080` + 手工 POST 一次样例

> **注意**：这套验证只能保证"不异常、不超时、报文合法"，**无法验证策略在真实对局中的效果**。策略效果需要真实判题器回放才能评估。

---

## 17. 风险与未决

| 项 | 状态 | 处理 |
|---|---|---|
| 几何锚定口径 | demo 已佐证左上角口径 | 仍建议第 1 回合用一次 `build wall` 探测确认。⚠️ **该实验有至少 4 种失败原因**：格子不是围墙区 / 距离 >1 / 背包没石头 / 当回合是夜晚。**必须同时排除后三种**，否则会把"实验设计错误"误读成"几何假设错误"——这是最容易踩空的一个实验 |
| 部分提交是否终止任务 | ✅文档推出"不终止" | 首局用 `Δscore`/`Δgold` + `phaseTask` 变化实测确认 |
| 波次增长曲线 | 未确认（官方未给出） | 防御投入节奏先用策略稿阶段目标表；`domain/history.py` 逐夜拟合，回写 `config.py` |
| 机器人出生区域 | 未确认 | 决定门的朝向；暂按"背向地图中心"实现，可配置 |
| 双方是否共享矿石 | 未确认 | 采矿区选点暂按"远离敌方基地"实现 |
| `errorCode 2/5` 是否计入 5 次异常 | 文档只说"异常"含三类，未明确 | 保守当作计入；配额台账每回合用 `errors[]` 对账 |
| 沙盒是否跨回合持久 | 未确认 | 设计上按"不持久"实现，持久化只当加速项 |

---

## 18. 已回写策略稿的修正项（✅ 全部完成）

`docs/design/strategy.md` 已升级为 **v0.4**，以下 7 项均已落笔：

| # | 修正 | 策略稿位置 |
|---|---|---|
| 1 | **任务时序纠错**：LLM 路径下界是 2 回合不是 1；技能库是唯一能吃到满时间项的路径；阶段目标改为"技能库命中率" | §0 第 2 点、**§5.2**（新增"❌ v0.2 的时序错误"小节）、§8 阶段目标表、§12.3 |
| 2 | **提交策略**：部分提交不终止任务 + 保留最高通过率 → 每回合交当前最佳答案，理由是"零机会成本的迭代"；新增**通过率反解** | **§5.2**（新增"提交策略"与"通过率反解"两小节） |
| 3 | **`targetTeam` 不可用** → 情报通道降级为"对手围墙/基地血量差反推 DPS" | **§9.1** |
| 4 | **门的代价模型**：`build` 仅白天 → "夜间封门"不可执行 → 改为"常闭 + `remove` 开门"（推荐），或白天末尾 `within 64~70` 决策 | **§6.3**、§8 日程表、§12.3 |
| 5 | **石头账本**：由 `stone_target = 缺口面数 + 3` 驱动，取代笼统的"石矿优先" | **§4.4** |
| 6 | **锚定实验的 4 种排除项**：目标格不在围墙环 / 距离 >1 / 背包无石 / 当回合是夜晚 | **§7.1** |
| 7 | **动作 ROI 排序表** + **红线 A 优先于任务分**的硬规则 | **§8.1**（新增小节） |

顺带修正的 3 处口径错误（v0.3 写错）：

- `attack` 的落点数组长度：**电磁狙击炮恒为 1**，只有加特林/火箭 = 当前等级（原写"一律等于等级数"）→ §6.4、§10
- `attack` 的 key 是**武器 id**，`controllerId` 才是操控角色 → §6.4、§10
- 顶层三字段必须齐备（demo 只发一个）→ §10

---

## 附录：与 demo 的对应关系

| demo 文件 | 本设计对应 | 说明 |
|---|---|---|
| `main3.py` | `main.py` | 入口形态照抄 |
| `agent/server.py` | `transport/server.py` | HTTP 骨架照抄，增加 deadline、幂等、HTTP/1.1、socket 超时、body 上限 |
| `agent/protocol.py` | `protocol/model.py` + `domain/geometry.py` | 解析与几何拆到两层；`range_of_attack` 的 payload 优先必须保留 |
| `agent/brain.py` | `domain/planner.py` + `defense.py` + `economy.py` + `assignment.py` | 玩具策略替换为真策略；`_wall_order` 移植为参考实现 |
| `agent/grid.py` | `domain/path.py` | 改成每回合计一次距离场 |
