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

保留官方 demo 的入口形态（`python main3.py <port>` → HTTP Server），将其**完全重写**为四层架构：

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
main3.py                           入口（**文件名必须叫这个**，平台按此拉起；见 §19.1）
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
│   ├── entities.py                Role/Robot/PlayerTask/ShopItem/NativeError + 类型常量 ✅
│   ├── grid.py                    Pos / 8 向邻居 —— 纯值对象（见 §15.1：Pos 归 domain）✅
│   ├── calendar.py                昼夜/within/回合预算（roundNo 的纯函数）✅
│   ├── geometry.py                固定 36 格盒子：基地 2×2 / 武器环 12 格 / 围墙环 20 格 / 背面门 ✅
│   ├── path.py                    Board + 8 向 BFS 距离场 + next_step_toward ✅
│   ├── world.py                   回合视图 Turn + 派生量（阻挡集 / 缺口 / 墙体完好度 / 物品总账）✅
│   ├── economy.py                 采矿 / 批量贩卖 / 采购阶梯 / 升级目标排序 / 石头账本 ✅
│   ├── assignment.py              角色↔武器**粘性**分配 + 落脚点求解 ✅
│   ├── planner.py                 昼夜调度 + 岗位分工 + 回防 + 每角色意图分配 ✅
│   ├── history.py                 逐回合时序记录器（波次曲线/对手 DPS/通过率反解的唯一数据源）
│   ├── intel.py                   对 history 的纯分析（策略稿 §9.1）
│   ├── defense.py                 建墙顺序、武器选点、夜间火力分配、修复优先级、应急道具
│   └── tasks/
│       ├── pipeline.py            任务状态机（纯函数 reduce，不做 I/O）
│       ├── llm.py                 prompt 构造 + 每日配额台账 + 回复解析（严格 JSON）
│       ├── sandbox.py             executeCmd 编排与结果解析
│       ├── skillbook.py           技能库落盘 + 题型指纹检索 + 通过率标签
│       ├── news.py                官方消息规则解析 → 矿价模型（策略稿 §4.4）
│       └── treasure.py            民间传闻台账（占位 + 情报累积）
│
├── protocol/                      ── L3 协议层（唯一接触线上 JSON 的地方）
│   ├── model.py                   容错解析 payload → domain 实体 + 异常清单；再 re-export 实体 ✅
│   └── commands.py                Intent → 线上指令 + 合法性校验器（全函数，永不抛异常）✅
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
└── selfcheck.py                   无 pytest 环境下的断言驱动 + 分层 import-lint（ast）✅
```

> `✅` = 已落地（截至第 5 步，selfcheck 50/50 通过）。无标记的模块尚未开工，
> 但接口形状已在第 3~7 节定稿，后续步骤只是往里填实现。

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
# main3.py
def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python main3.py <port>")
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
exec "$PY" main3.py "$1"
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
> 队列有界，满则丢弃并计数（**丢日志远好过丢比赛**）。`main3.py` 注册 `SIGTERM`/`atexit` 钩子做最终 flush（判题器会杀进程）。

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

⚠️ **开局事实（任务书 §4.5.1 / §4.5.3），支出顺序全由它决定：**

    三种武器**初始数量都是 0**（上限 3 座，单价 25 金）；**开局金币 75**。

75 / 25 = **恰好 3 座** —— 第 1 天白天的第一优先动作就是把 3 座建满，
否则当天入夜三名角色**无武器可操控**，基地赤手空拳挨整晚。
建满 3 座之后金币才轮到升级券。类型按 `config.WEAPON_LOADOUT = (gatling, railgun, rocket)`
给（射程短的靠正面），位置取 `Box.weapon_sites()` 的第一个空位。

⚠️ 本节早期写反过：把 `docs/request.txt`（`roundNo=85` 的**中局**快照，已有 3 座武器 + 20 金）
当成开局状态，于是判定"开局送满 3 座、造武器是死支出线"，把预算全推给升级券 ——
**第 1 天一座武器都不造**。`docs/request.txt` 是**中局**，不是开局，这个前提错误
传染进了 `config.py` / `economy.py` / 策略稿 §6.3，三处都已改正。

| 武器 | 机制 | 关键数字 |
|---|---|---|
| 加特林 | 多目标须在**同一 90° 锥形**内；每颗子弹沿自身弹道飞行，命中弹道上**最近**的一台机器人即消耗 | 10 伤害/颗 |
| 电磁狙击炮 | **恒 1 个目标**；能量沿弹道**穿透**，路径上每只存活机器人受 `min(剩余能量, 当前血量)`，能量按造成伤害扣减 | 升级增加能量 |
| 火箭发射台 | **指哪打哪不被阻挡**；中心 20，周围 8 格溅射 = 中心伤害一半；多枚落点重叠**叠加** | 发射后 3 回合冷却空窗 |

机器人：小型(攻5/距3/血40/1分)、中型(10/3/60/2分)、大型(20/3/500/4分)、BOSS(40/3/800/10分)。

### 12.5 需要补的经济机制

- **石头账本**：每面墙被拆后重建需 1 石，20 面 = 20 石。`economy.py` 显式维护 `stone_target = 当前缺口面数 + 缓冲`，采集优先级由它驱动，而不是笼统的"石矿优先"。**并且必须按批攒**（`STONE_STOCK=6`）：逐块往返会让 19 面墙变成 38 趟，实测第 40 回合才立起 4 面（见 §15.3 ④ bug 4）
- **选最近的矿**：`_best_mine` 用 **BFS 实际步数**打分（`mine_priority − 步数`），不是 `Pos.distance`。矿区本身不可通行（任务书 L85），所以比的是"走到相邻一格"的步数（`_stand_steps`，注意 `Pos.neighbours()` **含自己**，必须排掉）
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
| 4 | `domain/intent.py` + `protocol/commands.py` + `transport/server.py` + `app.py` + `main3.py` + `run.sh` | **此时已可起服务**：POST 样例返回 200、三字段齐、报文合法、耗时 <5s | ✅ 完成 |
| 5 | `domain/world.py` + `domain/planner.py` + `domain/economy.py` + `domain/assignment.py` | 白天能产出建墙/采矿/买卖/升级指令；角色↔武器分配跨回合粘住 | ✅ 完成 |
| 6 | `domain/defense.py` | 黑夜能产出合法 attack（锥形/落点数/相位门全过）、建墙顺序、修复优先序 | ⬜ |
| 7 | `domain/tasks/*` + `infra/state.py` + `domain/history.py` | 任务状态机全路径走通（含 ABORTED、竞态 10 项）；通过率反解可用 | ⬜ |
| 8 | `domain/intel.py` + `tools/replay.py` + README | 波次曲线与对手情报落盘；README 写清启动与解密用法 | ⬜ |

**第 4 步是关键分水岭**：此后任意时刻的程序都是"可提交"的，后续步骤只是把空指令替换成真策略。

### 15.1 第 3 步落地时新增的已确认事实

这一轮把 demo 的 `grid.py` / `protocol.py` / `brain.py` 读透并用真实样例数据交叉验证，得到 5 条**会影响策略**的事实：

| # | 事实 | 依据 | 影响 |
|---|---|---|---|
| 1 | ❌ ~~**开局已送 3 座武器**（gatling (9,24) / railgun (10,25) / rocket (9,25)），而"武器工事全局同时最多 3 座"~~ → ✅ **开局武器为 0 座，金币 75** | ❌ 样例 `teamOur.roles` —— **那是 `roundNo=85` 的中局快照**；✅ 任务书 §4.5.1 / §4.5.3 | ⚠️ 这条曾推出"`WEAPON_BUILD_COST=25` 是死支出线、升级券 ≫ 新武器"的结论，是**完全错误**的：25×3 = 75 = 初始金币，第 1 天第一优先就是建满 3 座。详见 §12.4 与 §15.3 ④ |
| 2 | `attack.controllerId` 是**字符串**（demo 写 `str(controller_id)`） | demo `protocol.attack_command` | 校验器必须按字符串处理，否则 5 次异常红线 |
| 3 | `build` **仅工人 + 仅白天**；`attack` **仅黑夜**；`sell` 需在小贩周围一格内 | 任务书 L137 / L122 / L132 | 佐证策略稿 §6.3「夜间封门不可执行」 |
| 4 | 阻挡移动的格子 = 双方全部建筑 + 双方全部角色 + 机器人 + 中立单位 + 4 个任务点 + 矿区 | 任务书 L85 | 与 demo 的 `land()`（要求 `neutralType=="land"`）完全一致 → **矿区/商店/任务点必须"走到相邻格再动作"** |
| 5 | 己方单位视野 = 4；机器人**全图可见** | 任务书 L95 | 情报线只能拿到视野内的敌方建筑；但机器人波次可全图统计 → 波次曲线可靠 |

另外两条实现层面的修正：

- **`Pos` 从 `protocol/` 迁到 `domain/grid.py`**。原方案让 `domain/geometry.py` 去 import `protocol.model.Pos`，直接违反 §3.1 的依赖方向（被 import-lint 抓到）。`Pos` 是纯值对象，归 domain；`protocol/model.py` 改为 re-export，`model.Pos` 仍可用。
- **半场口径改为取模**：任务书只说"下半场互换位置"，未说 `roundNo` 是否重置。`within_half = (roundNo-1) % 1300 + 1` 在两种语义下答案相同，因此不必赌。原 `halves_remaining()` 算的其实是"剩余天数"，已改名 `days_remaining_in_half()`。

### 15.2 第 4 步落地时抓到的一个"静默归零"级 bug

回合闸门（`app.MatchState`）最初写成"`roundNo` 比已处理的小 → 过期回合，回空指令"。
这条规则在 `roundNo` 连续时完全正确，**但如果下半场重置 `roundNo`**，下半场的每一次请求
都会被判过期 → **整半场一枪不开**。而且：

- 不抛异常、不超时、报文合法 → **判题器侧看不到任何问题**；
- 日志里只有一行行 `fallback at round 1 (stale round)`，除非专门去看否则发现不了。

修法是**三条独立的重置信号**，任一条成立就开新一场：

| # | 信号 | 覆盖的语义 |
|---|---|---|
| 1 | 会话键变了（`teamId` / `teamOur.type`） | 换边 → `type` 必然变（最可靠） |
| 2 | `roundNo == 1` | 明确的"从头开始" |
| 3 | 倒退幅度 ≥ `MATCH_RESET_GAP`(65) | 兜底：重置了但 `type` 恰好没变 |

只有"倒退很少"才当过期重放。另外用 `epoch` 丢弃上一场遗留的在途线程结果，
否则迟到的旧场决策会写进新半场的状态。

**教训（为什么值得写进文档）**：这类 bug 的特征是"错得很安静"。
本项目的验证手段（fixture 驱动器 / selfcheck）只能证明"不异常、不超时、报文合法"，
**证明不了策略在跑**。因此凡是"会让输出变空"的分支，都必须有一条断言盯着它的**反面**
——`tools/smoke.py::_state_cases` 里的用例 ④ 就是干这个的（判据用 `match==2`，
不是比对响应字节：planner 为空时正常响应与兜底响应**字节完全相同**，字节比对测不出来）。

### 15.3 第 5 步（经济线）落地时的修正

第 5 步产出了 `domain/{world,economy,assignment,planner}.py`，自检从 38 条涨到 **55 条**，
其中 `planner_one_round_produces_build_sell_and_buy_together` 是本步的验收用例
（一个白天回合同时产出建墙 / 贩卖 / 采购三条指令），
`economy_day_one_builds_weapons_before_anything_else` 是**开局行为**的验收用例。
落地时改掉了三处原设计，外加 ④ 中四个由实盘反馈查出来的 bug：

#### ① 回防判据：固定 `within ≥ 52` 被否决，改为**按实际路长**

策略稿 §8 写的是"`within` 到 52 就停止一切非回防行动"。它有两个毛病，且方向相反：

- **对近处的人太早**：多数角色本来就在基地附近，52 一到就全体停摆发呆。
  代价是每天 18 个白天回合 × 10 天 = **180 回合，占全部白天回合的 26%**。
- **对远处的人太晚**：地图 41×32，从最远的矿区回门要 30+ 回合，而 52 只留了 18 回合
  —— 被闸门"叫醒"时**已经来不及了**，代价是一整个夜晚站在门外（夜里角色不能攻击，
  却会被机器人当障碍物打，工人 220 血撑不过几回合）。

新判据是 `实际步数 + RETREAT_MARGIN(2) ≥ 白天剩余回合数`，外加 `RETREAT_TAIL(5)`
作为"最后 5 回合无条件收队"的尾巴。

⚠️ **必须用 BFS 实际步数而不是 `Pos.distance`**：切比雪夫距离会**低估**绕行代价，
在围墙/建筑/中立单位挡路时会算出一份"来得及"的假账。一次全图 BFS 只有 1312 格，
每回合每人一次，实测总计划耗时仍在 35ms 量级（硬闸门 5000ms）。

#### ② 选矿评分必须**摊薄运费**，否则金币永远停在开局那 20

原式 `价值 − 距离`（1:1）在样例地图上直接失效——实测数据：

| 矿 | 位置 | 距经济手 (10,16) | `价值 − 距离` |
|---|---|---|---|
| stone | (4,24) | 8 | 1−8 = **−7** |
| iron | (25,10) | 15 | 3−15 = −12 |
| copper | (22,26) | 12 | 5−12 = **−7** |

铜和石头并列，按距离破平 → 工人去挖 **1 金的石头**，整天挖石料，
金币永远停在开局那 20，升级券一张也买不起。

根因是**采矿是长期行为**：一旦站定就每回合稳定 +1，路上多走 5 格只亏 5 回合。
所以评分改为 `价值 × MINE_HORIZON(20) − 距离`，让价值差压得过距离差。

#### ③ 采购与应用**必须是同一个人**（否则券会烂在背包里）

游戏里**没有转移物品的指令**（`drop` 只把东西丢在地上，没有拾取动作）。
所以"让经济手去买、靠近基地的军需官去用"是行不通的——券只能由买家自己使用，
而升级券又要求**站在目标建筑周围一格内**。

于是分工定为（`planner._assign_jobs`，跨回合粘住）：

| 角色 | 岗位 | 职责 |
|---|---|---|
| 工人 A | `fortify` | **先建满 3 座武器** → 按批攒石 → 按 `wall_order()` 补围墙盒（正面 → 两侧 → 背面，门除外） |
| 工人 B | `economy` | 挖铁/铜（**不挖石头**）→ 满载 85% 后去小贩处清仓 |
| 开拓者 | `pioneer` | 去武器商店采购 → 回基地用券 → 空闲时顺路采矿（第 7 步接管任务线） |

经济手刻意**不挖石头**：石头只值 1 金、只用于建墙，让现金来源也变成建材是纯亏。

#### ④ 开局武器数是 0（不是 3），以及由此查出来的两个 bug

用户实盘反馈"第一天只建墙、不建武器，这样一定过不了第一天"后回查任务书纠正的。
本条同时修掉了三个**互相独立**的错误，每一个都足以让第 1 天报废：

**bug 1 — 前提错误（§12.4 / §15.1 第 1 行的根因）。**
`docs/request.txt` 是 `roundNo=85` 的**中局**快照，被当成了开局状态，
于是"造武器"被标成死支出线并**从采购阶梯里删掉了**（原 `economy._build_weapon`）。
更要命的是删掉之后**没有任何角色负责造武器**——军需官拿到的是 Idle，
筑墙手又没有这个分支，所以第 1 天真的是一座都不造。
现在武器由 `economy.next_weapon_build()` 出计划、筑墙手执行 `build <武器名>`
（`build` 本来就是**工人 + 白天**限定的动作，任务书 L137，商店里根本不卖武器）。
`planner._assign_jobs` 的 `need_builder` 判据也从 `bool(gaps)` 改成
`bool(gaps) or next_weapon_build(...) is not None` —— 只看缺口的话，
第 1 天围墙盒还没缺口，`_assign_jobs` 会**一个建造手都不派**。

**bug 2 — 采购阶梯会把武器预算花掉。**
第 1 天 `gold=75`、武器 0 座，⑥ 档围墙升级券的门槛是 `gold >= 60 + 10 = 70`
——成立，于是军需官去买 20 金的券，剩 55 金**只够再建 2 座武器**。
一张闲券挤掉一座开局火力。修法是采购前先扣掉武器预留
（`economy._weapon_reserve = 25 × min(还差几座, 环上空位)`），
环满了或推不出盒子时预留为 0，金币不会锁成死水。

**bug 3 — 正面先验方向反了。**
`geometry._infer_front` 原本取"朝向地图中心"，对基地 (10,24) 得到 **+x** ——
正好把 3 座武器和正面墙全砌在敌人来路的背面。改为**背向地图中心**
（朝最近的那条地图边缘），得到 **-x**，与三条独立证据一致（§6.3 策略稿）。
**这条先验是决定性的**：机器人只在夜晚出现（任务书 L350），第 1 天白天
完全观测不到威胁来向，`world.box_of` 只能回落到它。新增 `front_from_positions()`
让首个夜晚起用实测机器人来向覆盖先验。

**bug 4 — 石料逐块往返。**
"手上石头少于 N 就去挖"这种无状态判据会退化成"挖一块→走回去砌一块→再走回来"：
砌掉一块后 `held` 立刻又低于阈值，人当场掉头回矿。实测**第 40 回合才立起 4 面墙**，
正面在第 71 回合入夜前根本合不拢。改为跨回合记住的两段式状态机
（`PlanMemory.build_mode`：攒够 `STONE_STOCK=6` 块才切"砌墙"档，砌到一块不剩再切回），
19 面墙的往返次数从 38 趟降到 ~4 趟。

另外三处较小的实现决定：

- **`our_wall_alive` 滤掉血量归零的残骸**。判题器是否会把已摧毁的围墙从列表摘掉
  **没有明文保证**；不滤的话会把空地当成已完工的墙 → 永远不去补 → 整晚开着破口。
- **`use` 的升级目标排序**：武器券先升**火箭**（伤害 20×等级、升级还加落点个数），
  再加特林（同样吃落点数），电磁狙击炮永远只有 1 个目标故排最后；围墙券只升**正面**。
- **`_step` 的"≤1 且 ≠ 目标格"契约**：`stand_cell` 一律不返回目标格本身。
  若某张地图上武器格恰好可通行（武器血量归零已从 payload 消失、位置却还在环上），
  选它会让角色朝一个永远走不到的格子走一整晚——指令合法、不记异常、极难发现。

---

## 16. 验证方式

1. `py -3.13 -m compileall src tools main3.py` —— 语法与导入完整性
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
6. **起服务**：`py -3.13 main3.py 8080` + 手工 POST 一次样例

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

### 18.1 待回写（第 5 步新增，尚未落进 strategy.md）

⚠️ 下列第 8~12 项**仍未落笔**。其中第 8 项（回防判据）在
`docs/design/strategy.md` 里**还是错的**：§8 的节拍红线（L505）与 §12.3 速查（L663）
仍写着"`within ≥ 52` 后停止一切采集/采购"——该判据已被实测否决（见 ③ ①），
代码里是 `planner._should_retreat` 的 BFS 步数判据。**回写时以代码为准。**
第 9 项（选矿评分）策略稿里没有直接写错的公式，但 §4.4 只讲了 `stone_target`、
没讲"评分要摊薄运费"，补一句即可。

| # | 修正 | 应改的策略稿位置 |
|---|---|---|
| 8 | **回防判据**：固定 `within ≥ 52` 被否决（对近处的人太早、对远处的人太晚，两个方向的代价分别是 26% 的白天回合与"整晚被关在门外"），改为按 **BFS 实际步数 + 余量** 判断，另留最后 5 回合无条件收队 | §8 日程表、§12.3 阈值速查 |
| 9 | **选矿评分要摊薄运费**：`价值 × 20 − 距离`，不是 `价值 − 距离`。1:1 的写法在样例地图上会让铜与石头并列、工人去挖 1 金的石头，金币永远停在开局那 20 | §4.4 石头账本（与第 5 项并列） |
| 10 | **采购与应用必须同一人**：游戏无物品转移指令，而升级券要求站在目标建筑旁 → 分工定为「开拓者 = 军需官（买 + 用）」，经济手只管挖与卖 | §4.3、§8 日程表 |
| 11 | **经济手不挖石头**：石头只值 1 金且只用于建墙，让现金来源也变建材是纯亏 | §4.4 |
| 12 | **`our_wall_alive` 必须滤掉血量归零的残骸**：判题器是否摘除已毁围墙无明文保证，不滤会把破口当完工 | §11 待验证清单（新增一项"判题器是否保留已摧毁建筑的条目"） |

### 18.2 已回写（第 5 步后的实盘纠错，✅ 全部完成）

用户实盘反馈"第一天只建墙、不建武器，这样一定过不了第一天"后，回查任务书
纠正了 §15.3 ④ 的四个 bug，并把结论写回 `docs/design/strategy.md`：

| # | 修正 | 策略稿位置 |
|---|---|---|
| 13 | **第 1 天先建满 3 座武器**（75 金 → 0），再砌墙。含"机器人只在夜晚出现 → 正面方位只能靠先验"的完整说明 | **§6.3** 新增 v0.4 修正块、§8 日程表 `1–8` 行 |
| 14 | **正面方位 = 背向地图中心**（基地 (10,24) → `-x`；换边后 (30,10) → `+x`）+ 三条独立证据 | **§6.3** |
| 15 | **石料按批攒**（`STONE_STOCK`，跨回合记住档位），不逐块往返；附"实测第 40 回合才 4 面墙"的反例 | **§6.3**、**§4.4** |
| 16 | **选最近的矿看真实步数**（矿区不可通行，比"走到相邻一格"的 BFS 步数） | **§4.4** |
| 17 | **角色分工表改写**：工人 A 从"专职砌墙"改为"先建武器 → 攒石 → 砌正面墙" | §3 角色分工表 |


---

## 19. 实盘接入踩坑（按发生顺序，每条都有代价）

### 19.1 入口文件名是平台契约，不是风格选择（✅ 已修复）

**现象**：接入平台后"出现了异常，压根儿不动"——所有单位一枪不发、一步不挪。

**根因**：平台按**官方 demo 的约定**拉起入口 `main3.py`（demo 根目录里**只有**
`main3.py` 这一个入口文件，没有 `main.py`）。本设计在"入口形态照抄"时把文件
命名成了 `main.py`，于是平台拉不到入口，**我们的进程从未启动过**。

**为什么这次难查**（值得记住的失效特征）：
- 进程从未启动 → **不产生任何日志、任何 traceback、任何 `errors[]`**。
  我们所有排查素材都来自"服务在跑"这个前提，而这个前提本身是错的。
- 本地 `run.sh` / `python main.py 8080` **完全正常**，POST 样例 18~36ms、
  selfcheck 50/50、smoke ALL PASS —— 于是排查被带向**策略层**
  （几何？可达性？回防判据？），方向全错。
- `code-design.md` 附录里**明确写着** `main3.py → main.py`，即"我们主动改了名"
  这件事是记录在案的；当时把它当成了风格选择，而不是潜在致命项。

**修复**：`git mv main.py main3.py`（保留历史），并同步 `run.sh`、`smoke.py`
与本文档的全部引用。文件头加了 `⚠️ 不要重命名这个文件`。

**推广规则**：凡 demo 里**只存在一个**的东西（入口文件名、启动方式），
照抄不是"参考"而是"必须"。可以重写内部实现，**不要动平台看得见的那一层**。

### 19.2 待办：静默失效必须有告警（源自 19.1 的排查体验）

19.1 之所以烧掉大量时间，是因为**"回空指令"和"正常回合"在我们日志里长得一样**，
只有 `commandCount: 0` 这一个区别。`app.py` 里至少三条路径会静默产出空指令：

| 路径 | 触发条件 | 当前可见性 |
|---|---|---|
| `model.load()` 返回 None | `roundNo` 非数值 / `teamOur.roles` 缺失 | 仅 `_fallback` 一行 console |
| `_parse_role` 丢弃全部角色 | `pos`/`id` 解析失败 | 只在 `anomalies` 记一笔，**不告警** |
| planner 产出 0 条 Intent | 任何策略分支 | **完全静默** |

更糟的是 `MatchState` 的初始 `key = ("", "")` 与"解析失败"的键**相同**，
所以一条完全无法解析的请求看起来就像一次正常的同场对局。

**待补**（下一步动手）：
1. `our_roles` 非空却产出 0 条指令 → **高亮告警**；
2. 把 `turn.errors[]`（判题器直接告诉我们哪里错了）与
   `lastRoundRoleActionResults` 写进每回合日志；累计异常数 ≥3 时告警
   （红线是 5 次即不再调度）；
3. 解析失败时把 `key` 设为一个哨兵值（如 `("<unparsed>", "")`），
   使其不可能伪装成正常会话。

---

## 附录：与 demo 的对应关系

| demo 文件 | 本设计对应 | 说明 |
|---|---|---|
| `main3.py` | `main3.py` | 入口形态照抄，**文件名也必须照抄**（见 §19.1） |
| `agent/server.py` | `transport/server.py` | HTTP 骨架照抄，增加 deadline、幂等、HTTP/1.1、socket 超时、body 上限 |
| `agent/protocol.py` | `protocol/model.py` + `domain/geometry.py` | 解析与几何拆到两层；`range_of_attack` 的 payload 优先必须保留 |
| `agent/brain.py` | `domain/planner.py` + `defense.py` + `economy.py` + `assignment.py` | 玩具策略替换为真策略；`_wall_order` 移植为参考实现 |
| `agent/grid.py` | `domain/path.py` | 改成每回合计一次距离场 |
