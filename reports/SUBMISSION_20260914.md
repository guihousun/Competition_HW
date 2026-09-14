# 2026-09-14 完整项目发布验证

类别：文档 / 流程工具，以及此前本地模拟器、策略和前端成果的首次完整提交。此次台账编制不修改游戏行为或官方原文。

## 本次内容

- 根目录 `project_contract.yaml`：全局规则与官方核验台账；`runtime_loading: false`。
- 25 个核验条目：P0 9 项、P1 13 项、P2 3 项；7 项已知实现缺陷单独跟踪。
- `docs/OFFICIAL_VERIFICATION_GUIDE.md`：优先核对顺序、最小证据与 Issue 回填模板。
- 完整参赛入口、本地模拟器、前端、测试及 DSH 工作流；排除本地 `.workflow/`、索引、缓存和凭据。

## 验证证据

执行者：DSH，deepseek-flash / max；Codex 审核日志与台账。复用原生 QA 会话，恢复由 `ctx.agents.resume` 确认 `created:false`。

| 检查 | 结果 | 日志 |
|---|---|---|
| `python -m unittest discover -s tests -v` | 311 项通过，154.027 秒 | [游戏/前端测试](submission-20260914/01-tests-unittest.log) |
| `python -m unittest discover -s workflow -p "test_*.py" -v` | 59 项通过，5.868 秒 | [工作流测试](submission-20260914/02-workflow-unittest.log) |
| `node --test workflow/test_resume.mjs` | 1 个测试文件、9 项内部断言通过 | [原生恢复测试](submission-20260914/03-node-test-resume.log) |
| `node --check web/js/*.js`（逐文件执行） | 10 个文件通过 | [前端语法](submission-20260914/04-node-check-web-js.log) |
| YAML 结构与来源 | 拒绝重复键；25 个条目 ID 唯一；来源引用和 15 个编制快照哈希匹配 | [台账](../project_contract.yaml) |
| 发布源码核对 | 11 个 Git 内容 SHA256 与暂存源码匹配；原始官方文档、样例和压缩包未改动 | 台账 `implementation_snapshot.git_content_sha256` |
| 提交格式 | `git diff --cached --check` 通过 | 提交前执行 |

Node 原生恢复测试运行时设置 `COMPETITION_DSH_PACKAGE_JSON` 为本机全局安装的 `@deepseek-ai/dsh/package.json` 路径；其他机器需按自己的安装位置设置。

## 版本与覆盖边界

台账保留编制前的基线提交 `7e629424c87f3f194d071696d3ee59a8dc2c0a75`，当时实现尚未跟踪；首次发布后的实现身份以包含本报告的提交与台账 Git 内容哈希共同定位。Windows 磁盘字节哈希与 Git 换行规范化后的哈希分别保留。

这些测试覆盖本地协议、模拟、策略、前端纯逻辑与工作流，未进行公司内网官方平台实机验证，也未在本轮重跑整场策略基准或浏览器视觉验收。现有测试通过不代表全部符合官方规则；移动互换/静止占用、终局、新闻经济、任务点占地、召唤令目标队与联合移动等已知缺陷仍按台账追踪。历史报告仍只代表其当时版本，不作为本次新验证。
