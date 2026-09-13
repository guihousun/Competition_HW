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
