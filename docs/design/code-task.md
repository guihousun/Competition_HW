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
