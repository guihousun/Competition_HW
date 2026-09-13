# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

华为云核心网第十届编程大赛《未来战争》参赛 Agent。判题器按约定拉起进程，每回合通过 HTTP 推送全量战场状态，要求 **5 秒内**返回一条合法 JSON 指令。判题器是黑盒——**没有"打一局试试"这回事**，改动的正确性只能靠本地实测 + 赛后复盘。

## 编码规则

1. **禁止冗余设计。** 加任何东西（模块、抽象、常量、配置项、防御性分支）之前先问："**这一步**用得上吗？" 用不上就不加。判据是"现在有没有第二个使用者/第二种情况"，不是"以后可能需要"。历史教训：上一版重写前搞了四层架构 + 加密日志 + 分层 import-lint + 后台落盘线程，而当时决策逻辑只有一条经济线，绝大部分基建在交付时都没被真正用到。**宁可后续重构，不要预先抽象。**
2. **每一步都要在 `docs/design/code-task.md` 留痕。** 固定五段：目标 / 产出（含"这个文件为什么存在"）/ 不做什么 / 验证（**实际跑过的命令与输出**）/ 下一步。不删旧节，只追加。
3. **红线优先于一切优雅。** 见下节——5 次异常即出局，任何设计取舍在这一条面前让步。
4. 判题环境只有标准库：`pyproject.toml` 的 `dependencies` 保持为空，语法不得超出 3.11。
5. `logs/` 等运行期产物**绝不入库**。

## 硬约束

1. **`main3.py` 的文件名是平台契约，不要重命名。** 平台照官方 demo 的约定拉起它。改错的症状极具迷惑性：进程**从未启动** → 没有日志、没有报错、判题器也不报错，只是"所有单位一动不动"，而本地 `run.sh` 怎么跑都是对的，排查会被完全带偏。
2. **异常红线 = 5 次**，之后该队整场不再被调度。判题器只认三类：连接/响应超时、**响应格式错**、**指令非法**（字段缺失 / 动作码非法）。
   - 所有失败路径都必须退化成合法空指令 `{"roleCommandMap":{},"prompt":"","executeCmd":""}`——空指令**合法且不计异常**，宁可丢一个回合，不赌全队资格。
   - 注意区分：**指令非法**（计异常）vs **指令执行失败**（移动碰撞、攻击落点无目标 → 该条标记无效，**不计**异常）。
   - 响应三个顶层字段必须**永远都在**（官方 demo 只发了 `roleCommandMap`，别照抄这个）。
3. **动作有角色权限**（任务书 §4.4 表的**最右一列**）：`build` / `remove` / `collect` 仅**工人**；`acceptTask` / `submitAnswer` / `summonTreasure` 仅**开拓者**；其余动作全部角色可用。
   - 闸门在 **Action 的构造函数**里（`protocol/actions.py`）：`roleType` 不对就抛 `PermissionError`，**非法动作根本造不出来**。planner 侧接住它、丢那一条并告警，不连坐同回合其他角色（抛出去会变成"每回合空指令 → 全队冻结一整局"，现象与 `main3.py` 改名事故一样难排查）。
   - 这类 bug 的特征是**本地全绿**（格式完全合法，只有判题器会说"不"），而 `collect` 被误发给开拓者会每天吃一个异常——**红线只有 5 次**。所以每加一个受限动作，都要在 `tests/test_actions.py` 补一条对应用例。
4. 判题器侧超时：建连 > 10s 或响应 > 5s。

## 架构

骨架（逐步记录见 `docs/design/code-task.md`）：

```
main3.py              入口：读端口 → chdir → src/ 进 sys.path → 起服务。不放策略
run.sh                接口文档规定的 bash run.sh <port>；陪跑本地（python3 / py）
src/coregeek/
├── app.py            组装根：handle(bytes) + run(port)。**红线所在**，异常一律退化成空指令
├── web/server.py     HTTP：收字节 → handler → 回字节。handler 由 app 注入，不认识游戏概念
├── protocol/         线上格式：读与写，**只有这里知道字段名**
│   ├── model.py      payload → Turn（容错解析）
│   └── actions.py    BaseAction + 各动作。**创建即校验**，唯一写线上格式的地方
└── game/             领域与策略
    ├── grid.py       Pos / 8 方向 / 切比雪夫距离 / `step_toward`（**BFS 最短路**）
    ├── map.py        Map：格子矩阵（每格一个**类别**）+ `render()` 打印调试
    ├── roles.py      §4.5.2 的 Pioneer / Worker；`make()` 只认角色，建筑返回 None
    ├── world.py      Turn（round_no / map / roles）
    └── planner.py    决策。**策略只写在这里**（目前：两个工人各朝最近石矿走一格）
```

依赖方向：

```
app → web / protocol / game        protocol → game
game/planner → protocol/actions    ← 唯一一条"由内往外"，只走指令编码
```

- 三件事：**怎么收（web）/ 报文长什么样（protocol）/ 打什么（game）**，`app` 把它们接起来。**没有第四件事就不加第四个包**，也不提前建空目录。
- `app.py` 与 `planner.py` 的分工是**红线 vs 策略**：`app` 管"永远回得出合法报文"，`planner` 管"该做什么"。别让策略代码有机会破坏报文格式。
- `roleCommandMap` 的 key 用**字符串**（JSON 对象的 key 本来就是字符串）。
- 目前 `handle(raw: bytes) -> bytes` 是纯函数、无状态。**加任何跨回合状态之前先想清楚**是否需要，以及失败时的退化路径。
- 领域对象只带**当前步骤真正用到**的字段（`BaseRole` 目前只有 id/pos/type_name）。加字段之前先问这一步用不用得上——**尤其别把 §4.5.2 的 HP/背包上限写成类常量**，payload 里的 `health`/`backpack` 才是权威的当前值。
- **不加分层 import-lint**：现在一共 3 个子包，违规肉眼可见；上一版为此写的 ast 检查属于过度设计。

## 会影响策略正确性的领域事实

以下几条是读了任务书/接口文档/样例交叉验证得到的，**重新推导一遍的代价很高**：

- **日历**：130 回合 = 1 天（白 70 + 夜 60），10 天 1300 回合。`within = (roundNo-1) % 130 + 1`；`within <= 70` 为白天。样例 `roundNo=85`（夜晚，且场上确有机器人）已交叉验证。
- **开局是 0 武器 + 75 金 + 3 角色**（1 开拓者 + 2 工人），恰好买满 3 座武器（25×3=75）。⚠️ `docs/request.txt` 是 `roundNo=85` 的**中局快照**（已 3 武器 + 20 金），不是开局——曾把它当开局状态，推出"造武器是死支出线"，后果是第 1 天一座武器都不造。
- **固定 36 格防御盒子**：基地 2×2（`pos` 是**左上角**）+ 12 格武器环 + **20 格围墙环（单层，不可加厚）**。**一切坐标由己方 station 的 pos 推导，禁止绝对坐标**（上下半场换边后基地会挪）。可建造区在 `mapInfo.zones` 里**不提供**，只能这样推。
- **中立元素的字段名是 `neutralType`，不是 `zoneType`**（接口文档 §1.2.1，在 `mapInfo.zones` 里）：`stone`/`iron`/`copper`/`vendor`/`weaponShop`/`challengerTaskPoint1|2`/`defenderTaskPoint1|2`。**只有前三种是矿**，小贩/武器商店/任务点**不是矿，但一样挡路**——`Map.stones` 只装石矿，`Map.blocked` 全都装。
- **`Map` 的不变量：格子非空即挡路**（任务书 L85 那张清单），寻路只问 `blocked`、不问格子里是什么。类别原样取自 payload（`enemy:` / `robot:` 前缀区分来源），**未知类别也照旧挡路**——判错方向只会多挡、不会放行。`render()` 的字符表**是有损的**（机器人不分体型），`cells` 才是真相。
- **基地 2×2 对双方都成立**（接口文档原文："**双方**基地大小为 2*2，基地对应的 pos 传递的是左上角的坐标"）。`Map` 把**敌我双方**基地都展成 4 格——曾有一版只展了我方，敌方基地只挡 1 格。
- **`teamEnemy.roles` 是逐回合观测，不是固定名册**：敌方角色/武器离开视野就消失，**消失 ≠ 被摧毁**（任务书 L97）。敌方基地与围墙全图可见。**不要据此做跨回合战损推断**——`handle` 至今是纯函数。
- **走向矿 = 站到采集位，是同一件事。** 矿格挡路（任务书 L85），所以工人只能停在**矿周围一格**，而那正好就是 `collect` 的站位（§4.4）。不需要写两段逻辑（"走过去"+"停在旁边"），`step_toward` 撞上矿格自然停。
- **地图边界不在任务书 L85 的"阻挡移动"清单里**（那一列只写了建筑/角色/机器人/中立单位/任务点/矿区）。贪心挪一格时几乎撞不到，**但 BFS 会绕到图外去**，所以 `step_toward` 自己按 `Map.size = (width, height)`（取自 `mapInfo.width/height`）挡住 `(0,0)~(width-1,height-1)` 之外。越界算"指令非法"还是"执行失败"文档没写，不走一定安全。`size` 无效（≤0）⇒ `Map` 矩阵为空 ⇒ 无格可走 ⇒ **单位不动**，这是故意的降级。
- **`step_toward` 的终点是"贴着 goal 的一格"，不是 goal 本身**——因为 goal 通常是挡路的（矿/建筑/武器操控位）。⚠️ 但 `build` 的落点是**空地**，那一步要重新过一遍这条契约，别默认沿用。
- **没有转移物品的指令**（`drop` 只丢不捡，没有拾取）→ 每个角色的背包就是自己的料仓，"A 买 B 用"行不通。
- **`attack` 最易写反的两处**：`roleCommandMap` 的 **key 是武器 id**，`controllerId` 才是操控角色；`targetPos` 长度 = 武器当前等级（电磁狙击炮恒为 1）。攻击**仅黑夜**可用，且需要角色站在武器周围一格内（一人只能操一座武器）。
- **`attackRange` 以 payload 为准**：任务书等级表与样例数据矛盾（样例 gatling L1=4 / railgun=7 / rocket=INT_MAX，表格是 3/6/10）。
- 夜里**不能建墙**（`build` 仅工人、仅白天）→ 墙一旦被拆，整夜都是缺口，所以夜间第一优先级是**预防性修复**而非爆了再补。
- **任务点 2 占两格**，相邻判定要取两格的并集；开拓者一旦领任务，**离开任务点一格内即任务作废**（等于钉死原地）。

## 文档地图

| 文档 | 用途 |
|---|---|
| `docs/任务书.md`、`docs/接口文档.md` | **权威规则与协议**。冲突时以这两份为准 |
| `docs/design/code-task.md` | **迭代留痕**：每一步的目标/产出/不做/验证/下一步。动手前先看这里知道走到哪了 |
| `docs/design/task-analysis.md` | 规则整理笔记（标注了"文档明示 / 我的推断 / 矛盾点"三类，以及待实测清单） |
| `docs/design/code-design.md`、`docs/design/strategy.md` | 上一版重写的设计与策略稿。**正文已在工作区删除**，需要时 `git show HEAD:docs/design/code-design.md` 取。其中 §19「实盘接入踩坑」与策略稿的规则解构仍然有效，值得参考；但**代码架构部分已被本次重写取代** |
| `docs/request.txt` / `docs/response.txt` | **只能当字段形状参考**。`response.txt` 不是合法 JSON（缺逗号 + 同 id 重复），不能 `json.load`；`request.txt` 是合法 JSON 但几何是手工示意数据（挑战者那两座墙不在环上），**不可用于校准几何**，`lastRoundRoleActionResults` 的值也别当真实信号解读 |
| `example/CoreGeek/CoreGeek/` | 官方 demo（**双层嵌套目录**）。入口形态照抄；`brain.py` 的 `_wall_order` 等已验证实现可参考，但整体策略是玩具级，不继承 |

## 当前工作区状态

本次是**推倒重写**：`main3.py` / `src/` 等已按第 1 步重写（旧版本在 git 历史里，`git show 5b4dfcf^:<path>` 可取回）。
`tools/`（selfcheck / smoke / decrypt_log）、`README.md` 目前**不存在**——按需再加，别凭惯性建。
`tests/` 只有 `test_actions.py` 一个文件（权限用例），**不建自研测试框架**：标准库 `unittest` 够用。

**常用命令**：

```bash
bash run.sh 18085                                          # 起服务（自动挑 python3/py 并验版本）
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18085/
PYTHONUTF8=1 py -m unittest discover -s tests -v           # 跑用例；不加 PYTHONUTF8 中文会乱码
```

本地 **`python` 是 3.7.1**（Anaconda），必须用 `py`（3.13）；`run.sh` 已处理这个坑。
