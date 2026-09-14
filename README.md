# Competition_HW

**比赛上传：`git pull` 后直接上传项目根目录的 [CoreGeek.tar.gz](CoreGeek.tar.gz)。**
不用去 Release 找附件，也不要上传 `Demo/CoreGeek.tar.gz`（官方原始示例）。
根目录的 `CoreGeek.tar.gz.sha256` 用于校验，`CoreGeek.manifest.json` 记录包内源码版本。

《未来战争》参赛策略与本地端到端调试平台，Python 3.11+，无第三方依赖。
基于 `Demo/CoreGeek.tar.gz` 示例扩展，当前策略覆盖防御生存子集。

公司电脑固定使用 `codex/sgh`：`git switch codex/sgh` 后运行 `git pull --ff-only origin codex/sgh`。
每次记录 `git rev-parse HEAD`；内部工作分支由 Codex 管理。Codex 会把审核过的参赛包提交到根目录，详见下方参赛说明。

## 启动

```powershell
python main.py 8080
```

浏览器打开 http://localhost:8080/ ，选择种子、阵营、压力，点击「新建对局」，再用
`播放 / 暂停 / 单步 / 退一步 / 重置` 走完整条链路。画面是完整的俯视战场（基地、角色、
炮台、围墙、矿区、商店、任务点、机器人、昼夜与战斗特效），左栏是控制台，右栏为主要游戏画面。

- 「⏺ 录制 600 回合」或「⏺ 录制到终局」让服务端逐回合录制，之后可拖时间轴任意回看。
- 「请求 / 响应」页签可编辑请求 JSON 并只跑策略，用于单回合决策对照。
- 修改 Python 或 `web/` 下的文件后停止旧进程再启动；网页资源以 `no-store` 返回。

Linux 判题入口：`bash run.sh <port>`。官方请求使用根路径 POST，返回 `roleCommandMap`。

## 文档

- [详细日志、策略回放与模拟器对齐](docs/TRACE_LOGGING.md)（自动采集、窗口导出、观测样本、差异核对）
- [参赛包下载、启动与内网测试使用说明](docs/SUBMISSION_TESTING.md)（直接上传、版本校验、反馈闭环）
- [Issue #10 上传失败诊断与证据边界](docs/ISSUE_10_DIAGNOSIS.md)
- [全局规则与官方核验台账](project_contract.yaml)（25 项核验条目、P0/P1/P2 优先级；当前不作为运行时配置加载）
- [官方平台核对指南与 Issue 反馈模板](docs/OFFICIAL_VERIFICATION_GUIDE.md)

- [本地演示与调试平台使用说明](docs/VIEWER.md)（操作、画面含义、本地接口、验收证据）
- [变更记录](docs/CHANGES.md)（每次改动：类别、规则依据、行为例子、验证方式）
- [后续开发规范与官方不可改规则](docs/DEVELOPMENT_RULES.md)
- [Agent 开发合同](AGENTS.md)
- [Demo 完整链路、运行与规则边界](docs/DEMO.md)
- [官方任务书](docs/任务书.md)
- [官方接口文档](docs/接口文档.md)
- [本次验证结果](reports/VALIDATION.md)（代码版本、源码指纹、基准数字与未覆盖项）

## 验证

本次完整提交的检查范围与结果见 [发布验证记录](reports/SUBMISSION_20260914.md)。

```powershell
python -m unittest discover -s tests -v
python benchmark.py --seeds 1,7,19
python benchmark.py --seeds 101 --pressure 3
```

测试覆盖协议与结算（Python）、以及前端纯逻辑（坐标翻转、动画起点必须是已提交坐标、
昼夜切分、血量/射程表、未知类型降级、特效上限）。基准通过真实 HTTP 调用策略，每轮审计后
驱动模拟；保存初始状态、每轮动作/事件、终局统计。报告位于 `reports/`，可复查相同种子。
`_demo` 私有信息不会进入策略。

## 当前范围

支持多布局、左右换边、昼夜、采集建造、火箭防守、矿点刷新、压力波次、角色复活、本地计分，
**交易与升级**（按观测价格卖矿、商店购买升级券/修墙包/药剂/眩晕法宝/范围炸弹/召唤令），
**任务链路**（开拓者在己方任务点接取 → 保持范围 → 求解 → 提交 → 结算 → 30 回合刷新，
配合 `prompt` / `executeCmd` / `lastCmdResult` 接口层），以及**官方弹道**：加特林 90° 锥形
约束与逐发"命中路径上最近机器人"，电磁炮 10/20/30 能量沿路径穿透并按伤害扣减。

架构按"可插拔 + 原子"组织，各层互不越界、可单独替换与测试：

- `actions.py`：12 个官方动作的构造器、字段契约与原子注册表。
- `ballistics.py`：纯几何弹道（锥形、路径命中、能量穿透），无状态、可单测。
- `vision.py`：官方视野（共享视距 4、基地/围墙/机器人全局可见）与任一阵营视角。
- `treasure.py`：召唤宝藏的原子结算 + 本地祭坛夹具（原文未给出祭坛条件）。
- `match.py`：本地双队对局骨架（同一回合快照收双方指令、各自记忆隔离、本地胜负）。
- `twomatch.py`：本地双队对局的后台任务（整场约 100 秒，接口轮询进度与结果）。
- `market.py`：观测驱动的价格、背包、升级链与召唤额度。
- `turnactions.py`：交易/升级/消耗品的本地结算（成功即完整生效，失败不改状态）。
- `sandbox.py`：判题器通道（LLM 提示、沙盒命令、结果解析）。
- `tasks.py`：任务状态机 + 可插拔求解器注册表。
- `taskworld.py`：本地任务夹具（离线演示用，非官方判题）。
- `planner.py`：跨回合的判题器额度与待回请求记账。
- `brain.py`：策略选择（防御、经济、任务三者的取舍）。

建造环带、机器人选敌/移动、波次数量、任务文本与"弹道半格"判据仍有本地假设。未实现官方
任务文本与答案、召唤宝藏、双队同时对抗和官方胜负结算。不能把本地通过率或分数当作官方成绩。

## 主要文件

- `main.py` / `run.sh`：参赛服务入口。
- `Demo/CoreGeek/src/agent/actions.py`：官方动作构造器、字段契约与原子注册表。
- `Demo/CoreGeek/src/agent/ballistics.py`：官方弹道几何（锥形、路径命中、能量穿透）。
- `Demo/CoreGeek/src/agent/vision.py`：官方视野过滤与阵营视角。
- `Demo/CoreGeek/src/agent/treasure.py`：召唤宝藏结算与本地祭坛夹具。
- `Demo/CoreGeek/src/agent/match.py`：本地双队对局骨架（非官方判题）。
- `Demo/CoreGeek/src/agent/twomatch.py`：本地双队对局的后台任务。
- `Demo/CoreGeek/src/agent/market.py`：观测驱动的价格、背包、升级链与召唤额度。
- `Demo/CoreGeek/src/agent/turnactions.py`：交易/升级/消耗品的本地结算（原子）。
- `Demo/CoreGeek/src/agent/sandbox.py`：判题器通道与结果解析。
- `Demo/CoreGeek/src/agent/tasks.py`：任务状态机与可插拔求解器。
- `Demo/CoreGeek/src/agent/taskworld.py`：本地任务夹具（非官方）。
- `Demo/CoreGeek/src/agent/planner.py`：跨回合记账。
- `Demo/CoreGeek/src/agent/brain.py`：基于当前观测的策略。
- `Demo/CoreGeek/src/agent/scenarios.py`：种子场景与本地生命周期。
- `Demo/CoreGeek/src/agent/simulator.py`：本地结算，并产出逐回合结构化 `frame`。
- `Demo/CoreGeek/src/agent/server.py`：官方 POST 入口 + `/debug/*` 与静态页面。
- `Demo/CoreGeek/src/agent/debug.py`：本地调试 API（常量、差异清单、录制、截图）。
- `web/index.html` + `web/js/*`：可视化调试台（渲染、特效、视图模型、面板、控制）。
- `web/css/app.css`：界面样式。
- `tests/`：协议/结算/场景/经济/任务测试与前端逻辑测试。
- `benchmark.py`：HTTP 基准与独立动作审计。
