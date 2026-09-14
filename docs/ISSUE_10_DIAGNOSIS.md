# Issue #10：官方示例日志与我方上传失败的诊断

类别：**文档 / 运行诊断与打包工具**。R01/R02/R03/R04/R08 不变；官方原文与原始样例包
（`Demo/CoreGeek.tar.gz`，SHA256 `ba391bd7cb67751722ff48f4b4b566427590e6e22e08a28269eeae31a4533c7a`）
只读参考，未修改。基线：`aa6eb3e79c96e15e40ebc0c58817dd031ac6c933`。

## 0. 两份程序的证据归属

| # | 程序 | 状态 | 证据 |
|---|---|---|---|
| A | **官方示例**（demo） | 用户确认它产出了 Issue 中的 256 轮日志 | 用户 2026-09-14 澄清；§1 仅作特征比对 |
| B | **我们上传的包** | 平台报 `cant get package`，具体失败阶段与子系统未知 | 用户 2026-09-14 说明 |

因此：**不用官方示例日志诊断我们的策略**；示例日志中的 `{}` 空指令段**不是**我们代码的缺陷证据；
也不再请 owner 辨认该日志归属（已确认）。B 的失败原因需由包侧证据与公司平台回执共同定位。

## 1. 事实：示例日志与官方示例包一致（不是我们的实现）

对 [Issue #10](https://github.com/guihousun/Competition_HW/issues/10) 提供的日志统计：256 条 `round N -> {...}`，空指令段为 **[71–79]** 与 **[107–256]**
（共 159 条），非空段为 1–70、80–106（其中 71–79 为夜晚首段，80–106 只有 `10030` 由 `10011` 操控攻击）。

原始样例包成员（内存读取，未解压落盘）与已发布基线的差异：

| 特征 | 官方示例包 | 已发布基线 `aa6eb3e` |
|---|---|---|
| 响应日志 | `LOGGER.info("round %s -> %s", roundNo, response)`（整字典，server.py:18） | `"round %s -> %d commands"`（server.py:161） |
| 默认炮组 | `TOWER_LOADOUT = ("gatling","railgun","rocket")`（brain.py:20） | `("rocket","rocket","rocket")`（brain.py:40） |
| 入口 | `CoreGeek/main3.py`（29 行，`root = __file__.parent`，`chdir`，`sys.path+=src`） | 同名文件同结构 + 当前扩展 |

日志里同时出现 gatling/rocket/railgun 建造，且是整字典打印 → **与官方示例包特征一致**，
与已发布基线的“3 火箭 + 计数日志”不一致。特征比对支持用户的澄清，**不能**推断 B 的身份。

## 2. 事实与假设（B：`cant get package`）

**事实**：平台回执原文为 `cant get package`；owner 说明是**直接上传压缩包**（不是填写下载 URL）。
我们**没有**看到被拒绝的字节，也没有平台的解压/存储日志。

**仍属假设（不可当作结论）**：压缩格式、文件名/扩展名、包根与入口、传输截断、平台存储/解析，
都可能产生同一句回执。缓解措施是让**提交物本身**可自证：真实 gzip+tar 字节、单一 `CoreGeek`
根目录、`main3.py` 入口、清单+哈希、可复现构建与本地实跑。

**已修复的两个包侧问题**：① 缺少包身份（无法证明“跑的是哪个包”）；② 缺少可判读的运行诊断。
**未修复也不在范围内**：对未知败因的猜测性“修复”、以及任何策略改动。

## 3. 提交包完整性（本次工具保证的、可复核的性质）

`tools/build_submission.py`（stdlib + Git CLI，argv 列表，无 shell 拼接）：

- 只从**已解析的提交**打包；`--ref HEAD` 解析一次并打印 SHA；工作树未提交文件**绝不**进入包（摘要里
  显式说明），因此“最新”不会被当成 SHA。
- 真实 gzip+tar：`1f 8b` 魔数、显式 `CoreGeek` 顶层目录头、固定顺序/时间戳/权限 → 同一提交两次构建
  字节可复现。最终候选的 SHA、包哈希与验证记录以发布评论为准，旧基线测试不能代替候选验收。
- **整条 gzip 流校验**：包含 CRC32 与 8 字节 trailer。仅用 `tarfile` 会在 tar 结束标记处停止，
  截断包或被翻转 CRC 的包会被误判为有效——已单独修正并回归（`gzip_stream_integrity`）。
- 布局（兼容官方样例）：`CoreGeek/main3.py`（入口别名，字节等于已提交 `main.py`）、`CoreGeek/main.py`、
  `CoreGeek/run.sh`、`CoreGeek/pyproject.toml`，真实运行时代码保持嵌套在
  `CoreGeek/Demo/CoreGeek/src/agent/`（沿用 `Path(__file__).parents[4]` 等既有相对路径），
  `CoreGeek/web/`、`CoreGeek/docs/` 供本地 GET 使用。
- 清单 `CoreGeek/submission-manifest.json`：schema、精确提交、逐文件 SHA256、派生文件来源与哈希、
  源摘要；归档自身哈希放在**旁车** `.sha256`，避免自引用。
- 校验器：容器魔数（含“ZIP 改名 `.tar.gz`”）、根目录与入口、必需文件、逐文件哈希与源摘要、
  重复成员/清单项、特殊成员（fifo/设备/链接）、路径穿越、旁车哈希不符 → 一律失败并给出类别。
  缺清单明确标为“legacy/unversioned”，不称为“最新”。
- 已知基线指纹（原始样例包 SHA256）会被识别并**拒绝作为本次提交物**，且绝不改写原件。

## 4. 运行诊断（让下一次 FAIL 能自己带证据）

`Demo/CoreGeek/src/agent/diagnostics.py` + `main3.py`/`server.py` 的最小钩子（不改任何官方响应字段）：

- 启动一行 `identity`：提交（**清单优先**于 `git rev-parse`；包外父仓库的 SHA 不会被冒充为包版本）、
  源摘要与文件哈希、入口名、Python 版本、默认策略/炮组、清单状态（`verified`/`mismatch`/`absent`）。
  `verified` 表示文件与清单匹配，不是签名认证，也不代表官方平台通过。
- 每次判题响应一行 `response`：round、昼夜、指令数、动作计数与未知动作、`prompt`/`executeCmd` 存在性、
  基地血量/状态、存活工人/开拓者/武器/墙数量、可见机器人数、控制器与冷却摘要、失败动作数与协议错误数、
  以及**仅覆盖本次请求处理区间**的 `plan_ms`（启动耗时不会被当成请求超时）。
- 空输出类别是**可观测条件**，不是因果结论：`invalid_input` / `decision_exception` / `channel_only` /
  `base_observed_dead` / `no_live_controllers` / `no_weapons` / `unclassified`。
  **缺失血量 ≠ 死亡**（计为 `health_unknown`，计数返回 null）；**缺失冷却 ≠ 就绪**；机器人字段缺失
  不会写成“robot 已清空”；非有限数值一律视为未知。
- 摘要只读**已有的**观测与响应，不重跑规划、不改 `PlannerState`、不写入判题响应；
  序列化/日志/清单异常一律吞掉（fail open），响应字节不变。默认只打 stdout 少量行，
  不是磁盘录制器、不新增后台采集或全量 trace。

## 5. 未解决 / 仍需证据

1. B 的失败子系统（格式 / 上传 / 平台存储解析）**未定位**：需要被拒绝的字节或平台侧提示。
2. 公司平台的包获取规则（是否要求特定文件名、是否有大小/校验限制）**未验证**；
   `CoreGeek.tar.gz` + `CoreGeek/` 根 + `main3.py` 入口只是**兼容性选择**，不是已证实的平台要求。
3. 示例日志空指令段的成因（示例包在夜间的可执行条件）只作观察记录，不用于推断我们的策略。
4. 公司平台是否接受候选包、诊断输出是否可见、实际 Python 版本及完整对局结果尚待回测。

操作步骤见 [参赛包与内网测试使用说明](SUBMISSION_TESTING.md)。本轮不改变策略、协议字段或游戏规则。
