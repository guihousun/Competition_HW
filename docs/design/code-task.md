# 代码迭代任务留痕

> 重写工程（基于 `example/` 的形态）的**逐步记录**。每完成一步追加一节，不删旧节。
> 编码规则见 `CLAUDE.md` §编码规则，其中第一条是 **禁止冗余设计** —— 加任何东西之前先问"这一步用得上吗"。
>
> 每节固定五段：**目标 / 产出 / 不做什么 / 验证（实际跑过的命令与输出）/ 下一步**。

---

## 第 1 步：能跑的空框架（HTTP 进出 + 空动作）

### 目标

基于 `example/CoreGeek/CoreGeek/` 的入口形态，起一个"能收请求、能回合法响应"的最小骨架。
**不含任何策略**，目的是先把"平台 ↔ 我们"这条链路焊死，后面的每一步都往这个骨架里填。

### 产出

| 文件 | 为什么存在 |
|---|---|
| `main3.py` | 平台按官方 demo 的约定拉起这个名字。只做：读端口 → `chdir` → `src/` 进 `sys.path` → 起服务 |
| `run.sh` | 接口文档规定的 `bash run.sh <port>` 启动方式。判题机是 `python3`、本地是 `py`（本地 `python` 是 3.7），所以要**逐个试并验版本** |
| `pyproject.toml` | 声明 `src/` 布局；`dependencies = []` —— 判题环境只有标准库 |
| `src/coregeek/__init__.py` | 包标记 |
| `src/coregeek/server.py` | HTTP 层：收字节 → 交给 `app` → 回字节。不认识任何游戏概念 |
| `src/coregeek/app.py` | 组装点：报文 ↔ 决策。**红线所在** —— 任何异常都退化成合法空指令，绝不冒泡 |
| `src/coregeek/planner.py` | 决策入口。当前是 4 行的空实现，返回 `{}`；后续每一步往这里填 |

三个模块的边界不是为"以后可能要分层"预留的，而是这一步就已经是三件事：
HTTP 收发 / 报文与红线 / 策略。**没有第四件事，所以没有第四个模块。**

### 不做什么（本步明确划掉）

- ❌ 加密日志、跨回合状态、回合幂等、并发锁、决策预算线程 —— 等真需要时再加（第 1 步的 `handle` 是纯函数，没有状态可保护）
- ❌ `Intent` / 指令校验器 / 分层 import-lint —— 现在一条指令都不发，没有可校验的对象
- ❌ 离线测试工具（`smoke` / `selfcheck`）—— 用 `curl` 就能验完这一步；等有真策略再上工具
- ❌ 解析 payload 的 `model.py` —— 本步不需要读取任何字段（只在日志里取了个 `roundNo`）

### 验证

```bash
bash run.sh 18081                       # 起服务
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18081/
# → {"roleCommandMap":{},"prompt":"","executeCmd":""}     HTTP 200, 3~12ms
```

| 用例 | 结果 |
|---|---|
| 官方样例 `docs/request.txt`（roundNo=85） | 200 + 合法三字段，耗时 3ms（红线 5000ms） |
| 空 body | 200 + 合法空指令 |
| 坏 JSON `{oops` | 200 + 合法空指令，日志 `fallback 空指令：...` |
| `GET /` | 501（`BaseHTTPRequestHandler` 默认）。判题器只发 POST，不处理 |

服务端日志（`round 85 → 0 条指令` / `fallback 空指令：...`）。

**过程中修掉的一个问题**：日志里的中文和 `→` 在 Windows 控制台被转成乱码
（控制台默认 GBK）。日志是本地唯一的观测手段，因此 `run.sh` 里加 `PYTHONUTF8=1`
（判题机是 Linux，本就 UTF-8，不受影响）。

### 下一步

读 payload、认出我方角色与武器，夜间能发 `attack`、白天能发 `move`——
即"能看见局面、能动一个单位"。

---

## 第 2 步：看见局面，第一次发出真指令（解析 + 移动）

### 目标

读 payload、认出我方角色，并发出**真实**指令，把"解析 → 决策 → 编码 → 响应"整条管线跑通。
行为本身刻意简单：**所有角色朝我方基地走一格**。

**与第 1 步"下一步"的差异**：原计划这一步还要"夜间能发 `attack`"。实际拆到第 3 步——
`attack` 需要"哪个角色去操控哪座武器"的配对（一人只能操一座，且要先站到武器旁边），
那是岗位分配，混进来会让这一步既大又难验证。宁可多走一步。

### 产出

| 文件 | 为什么存在 |
|---|---|
| `src/coregeek/model.py`（新增） | payload → `Turn`。**容错解析**：字段缺失/类型不对就丢掉那一条或退化，绝不抛异常 |
| `src/coregeek/commands.py`（新增） | 唯一知道线上指令长什么样的地方。目前只有 `move()` |
| `src/coregeek/planner.py` | 由空实现改为：每个可移动角色朝基地走一格 |

> ⚠️ 这三个路径在下一步的「重构：按层归位」里挪进了子包，见那一节。内容未变。

两个刻意的取舍：

- **只解析这一步真的会用到的字段**：`roundNo` / 我方角色的 `id`+`pos`+`roleType` / 阻挡格。
  血量、等级、背包、冷却、机器人类型…… 等用到时再加（第 1 步的规则：现在没有第二个使用者就不写）。
- **阻挡格**（任务书 L85）= 我方单位 + 敌方**可见**单位 + 中立元素（矿/小贩/商店/任务点）+ 机器人，
  外加**基地 2×2 的另外三格** —— `pos` 是左上角（接口文档 §1.3.1 注），只标一格会让角色
  一头撞进基地里白扔一个回合。

模块数仍没有膨胀：`server`(HTTP) / `app`(报文与红线) / `planner`(策略) 三件事不变，
`model` 与 `commands` 分别是"读线上格式"和"写线上格式"——它们本来就是两个方向。

### 不做什么

- ❌ `attack`（需要角色↔武器配对，第 3 步）
- ❌ BFS 寻路：贪心 8 邻居够用，绕不过去的障碍就先停下
- ❌ `Intent` 类型体系 / 角色权限闸门：目前只有 `move`，它对全部角色可用，
  没有可拦的东西。闸门等第一个受限动作（`build` / `collect`）落地时在 `commands.py` 加
- ❌ 跨回合状态、回合幂等、并发处理 —— `handle()` 仍是纯函数

### 验证

服务实测（`bash run.sh` + `curl`）：

```bash
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18083/
```
```json
{"roleCommandMap":{"10010":{"action":"move","targetPos":[{"x":6,"y":22}]},
                   "10011":{"action":"move","targetPos":[{"x":9,"y":13}]},
                   "10012":{"action":"move","targetPos":[{"x":9,"y":17}]}},
 "prompt":"","executeCmd":""}
```
HTTP 200，2.1ms。三个工人/开拓者各一条 `move`，**建筑没有出现在指令里**（基地/武器/围墙不是可操控单位）。

边界用例（直接调 `planner.plan`）：

| 用例 | 结果 |
|---|---|
| 最小报文（只有工人 + 基地） | 发出 `move`，朝基地走 |
| 工人已贴着基地 `(9,24)` | `{}` —— 基地 4 格全被占，没有更近的落脚点，**不动作而不是撞进去** |
| 没有 `teamOur` / 没有基地 | `{}` |
| 两个工人相邻、目标同向 | 各自认领不同格（`claimed` 生效，否则按"目标点争夺"双方都会停住） |
| `pos` 缺失 / `id` 非整数 / `mapInfo.zones` 不是 list | `{}`，不崩 |
| 矿挡在必经路上 | 绕开矿格走 |

### 过程中抓到的

- **`collections.abc.AbstractSet` 在 Python 3.13 已被移除** → `ImportError`，进程直接起不来。
  改成 `collections.abc.Set`。这个只有**起真服务**才会暴露（局部调用不会走到那行 import），
  再次印证：每一步都必须真的把服务跑起来打一次。

### 下一步

夜间 `attack`：角色↔武器配对 + 站到操控位 + 按武器等级给 `targetPos`。
注意两处最易写反的地方：`roleCommandMap` 的 **key 是武器 id**、`controllerId` 才是操控角色。

---

## 重构：按层归位（第 2 步之后，无行为变化）

### 为什么

用户要求"分层一下，别挤在一个目录下面"。5 个模块平铺在 `src/coregeek/` 下，
`server / app / model / commands / planner` 之间看不出"谁能依赖谁"。
本次只搬位置、不改行为 —— 响应报文与重构前**逐字节一致**（下面有验证）。

### 产出

```
src/coregeek/
├── app.py                  组装根：handle(bytes) + run(port)。**红线所在**
├── web/server.py           HTTP：收字节 → handler → 回字节
├── protocol/               线上格式：读与写
│   ├── model.py            payload → Turn（唯一读线上格式的地方）
│   └── commands.py         指令编码（唯一写线上格式的地方）
└── game/                   策略
    ├── grid.py             Pos / 8 方向 / 切比雪夫距离 / 朝目标走一格
    ├── world.py            Role / Turn
    └── planner.py          决策（策略只写在这里）
```

三件事：**怎么收（web）/ 报文长什么样（protocol）/ 打什么（game）**，`app` 把它们接起来。
`grid` 从原 `model.py` 里拆出来 —— `Pos`/方向/距离是几何基元，后面建墙、寻路、算矿区距离都要用，
不该和"解析 payload"混在一起。

### 依赖方向

```
app  →  web          （app 把 handle 注入给 web，web 不认识 app）
app  →  protocol     （读 payload、写响应）
app  →  game         （决策）
protocol  →  game    （解析后构造 Role / Turn / Pos）
game/planner  →  protocol/commands   ← 唯一一条"由内往外"的依赖
```

最后一条是刻意的取舍：`game` 只在"把决策编成指令"时经过 `protocol/commands`。
也可以改成"`planner` 产出 Intent、`protocol` 再编码"来彻底解耦，但那要多一层
Intent 类型体系，而现在**只有 `move` 一个动作**——按本项目的规则（禁止冗余设计），
等动作多到值得抽象时再拆。已在 `planner.py` 里标注了这一条。

### 不做什么

- ❌ **不加分层 import-lint**。上一版为此写了 ast 检查脚本，而当时一共就 4 层、
  依赖违规肉眼可见。等目录多到自己管不住再说。
- ❌ 不提前建空子包（`infra/`、`tasks/` 之类）。**目录也是要等到有东西放才建。**

### 验证

- `curl` 打 `docs/request.txt`：响应与重构前**逐字节一致**（三个 `move`，同样的坐标），HTTP 200，5.9ms
- 坏 JSON → 合法空指令 + `fallback 空指令：...` 日志
- 第 2 步的 7 个边界用例全过（贴着基地不动作 / 两工人不抢同一格 / 乱字段不崩 / 矿挡路会绕），
  外加 `model.load("不是对象") → None`
- 起真服务时 `run.sh → main3.py → app.run → web.serve` 这条链路正常（分层最容易错的就是 import 环，
  只有真的起服务才验得到）

### 下一步

同上：夜间 `attack`。

---

## 第 3 步：Action 基类（创建即校验）+ 两个角色类

### 目标

把任务书 §4.4 表**最右列**（可用角色）变成可执行的东西，让**角色无权执行的动作根本构造不出来**。

动机是红线：§8 规定单队累计 5 次异常即整场不再被调度，而 §8 的注解把"指令错误"收窄为
「字段缺失」或「动作码非法」——**它没说"角色没权限"算异常，也没说算"指令执行失败"（不计）**。
口径不明，就只能让它发不出去。

形态由用户选定：**建 `BaseAction` 基类 + "创建时必须传 `roleType` 并在创建时校验"的规范，
但只建已实现动作的子类**（当前只有 `Move`），不预建 11 个空壳。

> ⚠️ **必须说清的局限**：`Move` 对全部角色合法，所以**这一步的校验在真实运行中拦不到任何东西**。
> 交付的是机制、两个角色类、和把机制钉死的用例。第一次真正拦住东西要等第一个受限动作
> （`build`/`collect`）落地。如实记下来，免得日后误以为这层已经在保护我们。

> 📌 这个设计与被推倒的上一版里的 `domain/intent.py` **同构**（`Intent` 基类 + 12 子类，
> 权限校验在旧版 `protocol/commands.py::validate()` 里也有一份）。那次推倒的理由是**整体**
> 过度设计，不单是这些类；而且那套校验是**一次真实出局事故之后补的**（开拓者被派去 `collect`，
> 每天吃一个异常）。所以重建这层有正当理由——折中方案就是"要那个规范、不要 11 个空壳"。

### 产出

| 文件 | 为什么存在 |
|---|---|
| `src/coregeek/protocol/actions.py`（新增） | `BaseAction` + `Move`。**唯一写线上格式的地方**（`commands.py` 并进来了）。创建即校验 |
| `src/coregeek/game/roles.py`（新增） | §4.5.2 的 `Pioneer` / `Worker` + `make()` 工厂。**只认角色，建筑返回 None** |
| `src/coregeek/protocol/commands.py`（**删除**） | `move()` 的编码并入 `Move.to_wire()`。留着就是**两处写线上格式** |
| `src/coregeek/game/world.py` | 删 `Role` NamedTuple 与 `MOVERS`；`Turn.roles` 改为 `tuple[BaseRole, ...]` |
| `src/coregeek/protocol/model.py` | 角色走 `roles.make()`（只收角色）；**`station` 改为单独扫原始单位列表** |
| `src/coregeek/game/planner.py` | per-role 循环里构造 `Move`；`PermissionError` → **丢那一条 + 告警**，不连坐 |
| `src/coregeek/app.py` | 局部变量 `commands` → `cmds`（模块没了，名字不再贴切） |
| `tests/test_actions.py`（新增） | 权限与报文的 8 条用例。**`tests/` 首次出现** |

两个刻意的取舍：

- **放 `protocol/` 而不是 `game/`**：Action 的全部意义就是"要发出去的那条指令"，让它自己
  编码才能守住"只有 `protocol/` 知道线上字段名"。代价是 §4.4 的权限表（游戏规则）也在这个文件里。
- **角色类不带 `can()`**：校验已经在 Action 构造时做了，角色侧再放一份就是**第二份真相**。
  也不带 §4.5.2 的 HP(200/220) 与背包容量(40格/100格)——`payload` 里的 `health`/`backpack`
  才是权威的**当前值**，把上限写进类常量只会制造矛盾。等真有决策读血量/背包时从 payload 读。

**最易改错的一处**：`station` 原本是从角色列表里推导的，而角色列表现在只剩角色了。
改成单独扫原始 `teamOur.roles` 找 `roleType == "station"`——它同时喂给 `blocked`（`base_cells`）
和寻路目标，扫漏了角色会一头撞进基地。

### 不做什么

- ❌ **11 个未实现动作的子类**（`Attack`/`Sell`/`Buy`/`Build`/`Remove`/`Collect`/`AcceptTask`/
  `SubmitAnswer`/`SummonTreasure`/`Use`/`Drop`）。每个落地时补 `code`/`roles`/参数/`to_wire` 四样
- ❌ §4.5.2 的 HP 与背包容量（没有使用者；且 payload 里的当前值才是权威）
- ❌ 昼夜（时间窗）限制：`attack` 仅黑夜、`build` 仅白天是**另一个维度**（看 `roundNo` 而非
  `roleType`），随 `attack` 一起做
- ❌ 给建筑建模（§4.5.1）：建筑继续以原始形态参与 `blocked`
- ❌ 解析 `errors` / `lastRoundRoleActionResults`（判题器给我们的**唯一反馈通道**，也是"距红线
  还剩几条命"的唯一度量）——本地触发不到，加了没有可断言的验证。**触发条件见"下一步"**

### 验证

**1. 单测**（8 条全过）

```bash
PYTHONUTF8=1 py -m unittest discover -s tests -v
# Ran 8 tests in 0.058s / OK
```

| 用例 | 钉住什么 |
|---|---|
| `test_wire_shape_is_the_flat_record` | 报文形状 |
| `test_move_is_allowed_for_both_roles` | "全部"角色没被误拦 |
| `test_roles_classvar_rejects_unauthorized_role` | **`BaseAction` 校验机制本身**（测试内定义 `roles = WORKER` 的子类） |
| `test_buildings_cannot_act` | 基地/武器/围墙产生不了指令（**真实可触发**） |
| `test_typo_role_type_is_rejected` | 角色类型拼错 → 造不出动作 |
| `test_sample_payload_produces_three_moves` | 端到端没被改坏 |
| `test_bad_json_falls_back_to_empty_commands` | 红线兜底仍有效 |
| `test_turn_holds_characters_only_and_still_finds_station` | 解析拆分正确（`station == (10,24)`，角色恰好 3 个） |

**2. 逐字节回归**（行为不变 —— 这一步唯一的行为变化只发生在本地走不到的越权分支上）

```bash
bash run.sh 18086
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18086/
```
与重构前**逐字节一致（213 字节）**，三个 `move`，坐标 `(6,22)` / `(9,17)` / `(9,13)`；
服务端日志 `round 85 → 3 条指令`（**闸门对合法指令零副作用**）。

**3. 红线两条退化路径**（起真服务打出来的）

| 请求 | 响应 | 日志 |
|---|---|---|
| 坏 JSON `{oops` | `{"roleCommandMap":{},"prompt":"","executeCmd":""}` | `fallback 空指令：...` |
| 空 body | 同上（三个字段都在） | `round -1 → 0 条指令` |

> 起真服务这一步不能省 —— 本项目两次事故（`collections.abc.AbstractSet` 在 3.13 被移除、
> Windows 控制台 GBK 乱码）都只有真的把服务跑起来才暴露。

### 下一步

**第一个受限动作（`collect` 或 `build`）落地那一步，必须同时做三件事，缺一不可**：

1. 加子类（`roles = WORKER`，构造即校验）——**这一步才第一次真正拦住东西**；
2. 读 `errors` 与 `lastRoundRoleActionResults` 进日志（`handle` 是纯函数，**不做跨回合计数**）；
3. 补一条"开拓者构造 `Collect` 抛异常"的用例。

其余按原计划：夜间 `attack` —— 角色↔武器配对 + 站到操控位 + 按武器等级给 `targetPos`。

> ⚠️ **留给 `attack` 的警告**：`roleCommandMap` 的 key 是**武器 id**，而"谁能攻击"要看
> `controllerId`。动作改成"自己编码"之后，key 由**调用点**决定（`cmds[str(weapon.id)] = ...`），
> 这个不变量**没有第二个地方能替你守住**。`docs/response.txt` 里
> `"10020": {"action":"attack","controllerId":"10010"}` 就是官方证据。

## 第 4 步：解析地图中立元素，工人走向石矿（跑通全流程）

### 目标

**把"解析 → 决策 → 构造 Action → 校验 → 编码 → 响应"整条管线用真指令跑一次。**

第 2 步发的指令是"所有角色朝基地走"，那**不读地图**——`Turn` 里除了角色坐标什么都没有，
等于管线只通了一半（写了半个 `blocked`）。这一步把**地图中部**接上：解析中立元素，
让工人奔一个**真实存在于地图上**的目标（石矿），而不是一个写死的坐标。

策略上这只是临时一步：**先跑通，不谈收益**。石矿是第 1 天防御线的料源
（§4.4 `build` 需背包里有石头），所以从它起头，方向不算错。

### 产出

**1. `game/world.py`：`Turn` 新增 `mines: dict[Pos, str]`（矿点 → 矿种）**

为什么放 `Turn` 而不是让 `planner` 自己去 payload 里刨：**只有 `protocol/` 知道字段名**
（`CLAUDE.md` 里的分层）。planner 拿到的是"地图上有哪些矿"，不是 `neutralType` 这个字符串。

**2. `protocol/model.py`：`_MINE_KINDS` + `_mines(zones)`**

⚠️ 字段名是 **`neutralType`**，不是 `zoneType`。第一次探测时写的是 `zoneType`，
拿到的是 `KeyError`/全空——`docs/接口文档.md` §1.2.1 写得很清楚，但直觉会先摸错。
**同一张表里还有小贩 / 武器商店 / 任务点，它们不是矿**，靠 `_MINE_KINDS` 白名单滤掉；
不过它们**照旧挡路**（`_BLOCKING` 那条路没动）。

**3. `game/planner.py`：重写为"工人各自朝最近的石矿走一格"**

关键是**它没有"停在矿边"这段逻辑**：矿格本身在 `blocked` 里（任务书 L85），
所以 `step_toward(pos, goal=矿, blocked)` 走到**贴着矿的那一格**就自然返回 `None`，
而那正好就是 `collect` 的站位（§4.4：矿周围一格内）。**走位和站位是同一件事，不用各写一遍。**

`claimed` 防的是"两个人冲同一个空格"——任务书 §4.5.4 规定目标点争夺时**双方都停住**。
**不认领矿点**：两个工人挤同一座矿的不同邻格**都能采**，只有冲进同一格才是白扔动作。

**4. `tests/test_actions.py`：8 条 → 12 条**

第 3 步里叫 `test_sample_payload_produces_three_moves` 的那条**随行为变化改名收窄**
为 `test_sample_payload_moves_only_the_far_worker`——现在不再是 3 条 `move`。

### 不做什么

- ❌ **不发 `collect`。** 工人这一步只是**走到**矿边。加 `Collect` 子类会正好触发第 3 步
  留的"三件事"清单（见"下一步"），那是独立的一步。
- ❌ **开拓者不发指令。** 开拓者的动作（`acceptTask`/`submitAnswer`/`summonTreasure`）
  一个都没实现，此时让它乱走只是白送对手靶子。**代码里是 `isinstance(role, Worker)` 挡的**，
  不是"忘了"。
- ❌ **不追铁/铜。** `mines` 里三种矿都解析进来了，但 planner 只读 `stone`。
  见下方"一处要说明的"。
- ❌ **不做寻路。** `step_toward` 是**贪心一格**，绕不过凹形障碍。这个地图的开阔度下够用；
  真卡住了再加，别提前上 A*。
- ❌ **不解析 `errors` / `lastRoundRoleActionResults`。** 理由同第 3 步，触发条件不变。

### 一处要说明的（诚实起见）

**`mines` 存了三种矿，但这一步只有 `stone` 有使用者。** 严格按规则第 1 条，
本可以只留 `stones: frozenset[Pos]`。留着矿种的理由是：`_MINE_KINDS` 为了把矿
从"小贩/武器商店/任务点"里区分出来，**本来就必须列出三种名字**，矿种是这张表
**自带的产物**，不是额外抽象，也没有多写一行。**但它是本步唯一"存了没人读的值"**，
记在这里备查。

另：**`Turn.station` 现在在 `Turn` 上同样没有读取方**（`src/` 里只有 `model.load`
内部算 `base_cells` 时用它，那个用法在字段落进 `Turn` 之前就完成了）。保留它是因为
"由基地坐标推导 36 格防御盒子"是已确认的硬约束，下一步建墙就要读。**同样记在这里备查。**

### 验证

**1. 单测 12 条全过**

```bash
PYTHONUTF8=1 py -m unittest discover -s tests -v
# Ran 12 tests in 0.002s / OK
```

本步新增的 4 条：

| 用例 | 钉住什么 |
|---|---|
| `test_mines_are_parsed_by_kind` | 三种矿分开；**小贩/武器商店/任务点没混进来**（样例里恰好 6 座矿） |
| `test_mines_block_movement` | 矿格在 `blocked` 里——这条一旦挂了，"停在矿边"会变成"站到矿上" |
| `test_worker_walks_to_the_mine_and_then_stops` | **收敛**：把回合串起来跑 40 轮，最终 `dist == 1` 且此后不再产生动作 |
| `test_no_stone_mine_means_no_action` | 场上只有铁矿 → 原地不动，而不是随便挑座矿走过去 |

> 第 3 条是这一步最值钱的一条：**单帧"目标格算得对"证明不了走得过去、停得下来**——
> 走歪、绕圈、贴住后反复抖动都只有串起来跑才看得见。跑法是用 `Turn._replace` 把
> 上一回合的目标格喂给下一回合，**不开服务、不连判题器**。

**2. 起真服务打官方样例**

```bash
bash run.sh 18091
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18091/
```

响应：

```json
{"roleCommandMap":{"10012":{"action":"move","targetPos":[{"x":9,"y":17}]}},"prompt":"","executeCmd":""}
```

服务端日志：

```
2026-09-13 14:36:55,428 | listening on 0.0.0.0:18091
2026-09-13 14:36:57,434 | round 85 → 1 条指令
```

**3. 手工核对（响应与地图自洽）**

样例 `roundNo=85` 的快照里石矿在 `(4,24)` 与 `(14,3)`：

| 角色 | 位置 | 最近石矿 | 结果 |
|---|---|---|---|
| 工人 `10010` | `(5,23)` | `(4,24)`，切比雪夫距离 **1** | **已贴着矿，不动作** ✅ |
| 工人 `10012` | `(10,16)` | `(4,24)`，距离 14 | 走一格 → `(9,17)` ✅ |
| 开拓者 `10011` | — | — | 不发指令 ✅ |

实发响应与手算**完全一致**。特别注意 `10010` 那条：**它"什么都没做"正是正确行为**，
是矿格挡路 + `step_toward` 返回 `None` 的自然结果，不是漏了。

### 下一步

**下一步是 `collect`**（工人已经站到矿边了，却还不采——管线现在断在这里），
它正好触发第 3 步留的三件事，缺一不可：

1. 加 `Collect` 子类（`roles = WORKER`，构造即校验）——**这是全项目第一次真正拦住东西**；
2. 读 `errors` 与 `lastRoundRoleActionResults` 进日志（`handle` 仍是纯函数，**不做跨回合计数**）；
3. 补一条"开拓者构造 `Collect` 抛异常"的用例。

之后才是夜间 `attack`（角色↔武器配对 + 站到操控位 + 按武器等级给 `targetPos`，
**key 是武器 id** —— 见第 3 步末尾的警告）。
