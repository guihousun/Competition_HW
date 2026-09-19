# CoreGeek · 《未来战争》参赛 Agent

华为云核心网第十届编程大赛参赛程序。判题器按约定拉起进程，**每回合通过 HTTP 推送全量战场状态，要求 5 秒内返回一条合法 JSON 指令**。

判题器是黑盒——**没有"打一局试试"这回事**，改动的正确性只能靠本地实测 + 赛后复盘。因此本仓库的迭代方式是**一次一小步、每步留痕、纯重构也要逐字节证明行为不变**（见 [`CLAUDE.md`](CLAUDE.md) 与 [`docs/design/code-task.md`](docs/design/code-task.md)）。

## 运行

接口文档规定的启动方式：

```bash
bash run.sh <port>        # 例：bash run.sh 18085
```

`run.sh` 依次试 `python3` / `python` / `py` 并**验版本 ≥ 3.11**（本地 `python` 是 3.7，挑中会当场崩），然后 `exec` 入口 `main3.py`。

本地自测（官方样例报文）：

```bash
curl -s -X POST --data-binary @docs/request.txt http://127.0.0.1:18085/
```

## 开发

```bash
PYTHONUTF8=1 py -m unittest discover -s tests -v   # 跑全部用例（不加 PYTHONUTF8 中文会乱码）
py tests/test_game_path.py                        # 单文件可直跑，只查寻路
```

用例按 `src/coregeek/` 一一对应拆分（`test_<模块>.py`），标准库 `unittest`，不自研框架。

**日志是加密的，没有明文落点**（含完整对局状态，绝不入库）。三种落点是同一套密文：判题器把进程 stdout 落成的 **`team.log`**、本地重定向出来的 `run.enc`、文件 sink 的 `log/match-<年月日-时分秒>-<pid>.enc`。

```bash
py log/decode_log.py                                       # 解脚本里 raw_path 指的那份（改那一行即可）
py log/decode_log.py log/match-20260919-020301-19400.enc    # 或直接给文件 ⇒ 当前目录 decode_log.log
bash run.sh 18085 > run.enc                                # stdout 那份也是密文，先落成文件
py log/decode_log.py run.enc
```

## 结构

```
main3.py              入口（文件名是平台契约，不要改）：读端口 → chdir → src/ 进 sys.path → 起服务
run.sh                bash run.sh <port>
src/coregeek/
├── app.py            组装根：永远回得出合法报文（红线所在）
├── web/server.py     HTTP：收字节 → handler → 回字节
├── protocol/         线上格式：读（model）与写（actions），只有这里知道字段名
├── agent/            跟 LLM 说什么、怎么解析它的回复（叶子包，只依赖标准库）
└── game/             领域与策略：grid（几何）/ path（BFS 寻路）/ map / roles / world /
                      states（白天工人链）/ utils（通用判定）/ planner（决策入口）
tests/                按 src 拆分，文件名一一对应
docs/                 任务书、接口文档、设计与迭代留痕
log/decode_log.py     解密脚本（加密格式唯一的消费者）
```

依赖方向是单向的：`app → web / protocol / game`，`planner → states → utils`，`path → grid`。

## 硬约束

1. **`main3.py` 的文件名是平台契约，不要重命名。** 改错的症状极具迷惑性：进程**从未启动** → 没有日志、没有报错、判题器也不报错，只是"所有单位一动不动"。
2. **异常红线 = 5 次**，之后该队整场不再被调度。所有失败路径都必须退化成合法空指令（空指令**合法且不计异常**——宁可丢一个回合，不赌全队资格）。注意区分**指令非法**（计异常）与**指令执行失败**（移动碰撞、攻击落空 → 不计异常）。
3. 响应三个顶层字段 `roleCommandMap` / `prompt` / `executeCmd` **永远都在**（官方 demo 只发第一个，别照抄）。
4. 判题环境**只有标准库**：`pyproject.toml` 的 `dependencies` 保持为空，语法不得超出 3.11。
5. 运行期产物（`log/`、`logs/`、`tmp/`）**绝不入库**。

## 文档

| 文档 | 用途 |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | 给 Claude Code 的项目须知：架构、硬约束、**重新推导代价很高的领域事实** |
| [`docs/任务书.md`](docs/任务书.md) · [`docs/接口文档.md`](docs/接口文档.md) | **权威规则与协议**，冲突时以这两份为准 |
| [`docs/design/strategy.md`](docs/design/strategy.md) | 当前策略导读（`§1` 是角色决策状态机，读 `planner` 之前先看它） |
| [`docs/design/worker.md`](docs/design/worker.md) | 角色运行逻辑全景：每回合怎么决策 |
| [`docs/design/code-task.md`](docs/design/code-task.md) | **迭代留痕**：每一步的目标 / 产出 / 仍生效的已知不确定性 |
| [`docs/design/task-analysis.md`](docs/design/task-analysis.md) | 规则整理笔记（"文档明示 / 我的推断 / 矛盾点"三类） |
