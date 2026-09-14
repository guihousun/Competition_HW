# 提交包本地验证与内网回测指南

适用：`guihousun/Competition_HW`。本文只讲**怎么拿到正确的候选包并回测**，
不改规则、不改策略、不声称已通过公司平台。规则依据：`docs/DEVELOPMENT_RULES.md` R01/R08。

**当前状态**：Issue #10 报告 `cant get package`，owner 说明是**直接上传压缩包**（不是下载 URL）。
被拒绝的字节我们没有看到，所以格式/上传/平台存储都仍是**未定位项**；下面把**包侧变量**降到最少：
真实 gzip+tar、单一 `CoreGeek/` 根目录、`CoreGeek/main3.py` 入口、清单+旁车哈希。

## 1. 下载候选包，或检出候选源码

公司电脑可以直接从 [Issue #10](https://github.com/guihousun/Competition_HW/issues/10)
中的候选 Release 链接下载 **Assets → CoreGeek.tar.gz** 和 `CoreGeek.tar.gz.sha256`。
对照该评论中的 SHA256 后，将压缩包原样上传平台。不要上传 GitHub 自动生成的
`Source code (zip)` / `Source code (tar.gz)`；那是整个仓库快照，不是参赛打包工具的产物。

Windows 校验下载文件：

```powershell
Get-FileHash .\CoreGeek.tar.gz -Algorithm SHA256
Get-Content .\CoreGeek.tar.gz.sha256
```

若要检查或运行源码，首次下载：

```powershell
git clone --branch codex/sgh https://github.com/guihousun/Competition_HW.git
cd Competition_HW
git rev-parse HEAD
git status --short
```

`aa6eb3e79c96e15e40ebc0c58817dd031ac6c933` 是**历史已发布基线**（示例日志那一版不是它）；
后续每次回测都要从 Issue/PR 里给出的**候选提交 SHA**检出，**不要**用“最新/main/昨天那版”：

```powershell
git fetch origin
$candidate = "替换为 Issue 中给出的完整40位候选SHA"
git checkout --detach $candidate
git rev-parse HEAD
git status --short
```

候选 SHA 由 Codex 在 PR/Issue 中公布；`git status --short` 应无输出。
本候选分支为 `codex/issue-10-diagnostics`，以 `codex/sgh` 为合并目标；
它修复打包和诊断能力，不包含尚未发布的策略学习实验。

## 2. 本地先自检（Windows / PowerShell）

```powershell
python main.py 8080
```

另开一个终端（`docs/request.txt` 是官方示例请求，只读、不要修改）：

```powershell
curl.exe -H "Content-Type: application/json" --data-binary "@docs/request.txt" http://127.0.0.1:8080/
curl.exe -s -o NUL -w "%{http_code}`n" http://127.0.0.1:8080/
```

Linux/macOS 等价：

```bash
python3 main.py 8080
curl -H "Content-Type: application/json" --data-binary "@docs/request.txt" http://127.0.0.1:8080/
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/
```

POST 应返回含 `roleCommandMap` 的 JSON；浏览器打开 http://localhost:8080/ 可用本地调试页面。
这只证明本地入口能工作，不代表官方对局通过。
启动时会打印一行 `identity {...}`，每次判题请求再打印一行 `response {...}`；
这两行就是回测要贴的证据（见 §4）。

## 3. 构建并验证可上传的包（Codex 主导，本地可复核）

```powershell
python tools/build_submission.py build --repo . --ref HEAD --output CoreGeek.tar.gz
python tools/build_submission.py verify --archive CoreGeek.tar.gz --sidecar CoreGeek.tar.gz.sha256
```

- `verify` 会检查：真实 gzip（含 **CRC/尾部完整性**，截断包会被拒绝）、“ZIP 改名 `.tar.gz`”、
  单一 `CoreGeek/` 根目录与 `CoreGeek/main3.py` 入口、必需文件、逐文件哈希与源摘要、
  重复成员/清单项、路径穿越与特殊成员、旁车哈希。
- 文件名 `CoreGeek.tar.gz`、根目录 `CoreGeek/`、入口 `main3.py` 是与**官方示例包一致**的兼容性选择，
  **不是**已证实的平台要求；包的精确身份以清单和哈希为准。
- 产物写到 `--output` 指定位置，旁边生成 `.sha256`；已有文件会被拒绝覆盖。
  工具仅包含指定提交的内容，不包含未提交改动。发布评论另行提供实际测试记录。

解包后本地再跑一次（验证的是**将要上传的字节**，不是工作树）：

```powershell
python tools/build_submission.py extract --archive CoreGeek.tar.gz --dest .\unpacked
cd .\unpacked\CoreGeek
python main3.py 8080
```

## 4. 内网回测与反馈模板

按平台要求**直接上传** `CoreGeek.tar.gz`（若平台需要其它文件名/形态，以平台提示为准并回报）。
官方文档指定的启动形式为 `bash run.sh <平台指定端口>`，监听 `0.0.0.0`。
本包另保留 `python3 main3.py <平台指定端口>` 兼容示例入口。
包内根目录同时有 `main.py`、`main3.py`、`run.sh` 和 `pyproject.toml`，实际运行代码保持在
`Demo/CoreGeek/` 下。无需在内网联网安装 Python 依赖；实际 Python/沙盒版本仍需平台核对。
回测后把下面内容贴回原 Issue：

```text
INTRANET: PASS / FAIL / BLOCKED
COMMIT: <候选完整 40 位 SHA；若平台只认包，再附包 SHA256 与旁车文件>
PLATFORM: <平台/规则版本>
CASES: <本轮覆盖：接口自检 / 基本动作 / 完整对局 / 换边>
RESULT: <结果；FAIL 请附失败回合、平台原文提示（如 cant get package）、观察到的现象>
LOGS: <identity 行 + 失败回合附近的 response 行；不要贴令牌/内网地址/公司原始日志>
```

建议依次验证，PASS 要注明实际覆盖范围：

1. **官方接口联调**：平台能解包、启动并调用根路径 POST，返回有效 `roleCommandMap`，无超时。
   GET 页面与 `/sample` 仅供本地调试，判题器无需支持。
2. **基本动作**：移动、采矿、卖矿、建造、防守、任务领取和提交；结合下一轮观测与动作回执判断。
3. **完整对局**：整场到 1300 回合或终局条件，记录首次异常回合与前后诊断行。
4. **换边**：左右/阵营互换复测，避免单侧偶然。

**若平台仍报 `cant get package`**，请同时回报：上传的文件名与字节数、是否直接上传、
平台提示原文。这些信息才能区分“包格式 / 上传 / 平台存储解析”三种原因（目前都还只是假设）。

## 5. 流程闭环（2 分钟轮询 → 设计 → 实现 → 审核 → 回测）

`issue_poller`（每 2 分钟）→ 原 Codex 线程 → `workflow/state.py` 摘要与 Spec →
DSH 专员或 Codex 实现 → Codex 审核 → 候选 PR、上传包与精确 SHA → 用户同 SHA 内网回测。
**证据不完整不阻塞**：可并行做有界只读分析、独立可复现修复、身份/可观测性与候选包工具；
需要 owner 的只有平台侧动作与真实回测。行为合并仍以**同 SHA 内网 PASS** 为准。

## 6. 边界（不要过度声明）

- 本地打包、本地启动、本地 POST、`verify` 全绿都**不是**公司平台通过；平台是否接受该包**尚未验证**。
- 诊断行是工程元数据，不是官方判题字段，也不构成官方认证。
- 原始示例包 `Demo/CoreGeek.tar.gz` 只作布局参考：不改写、不覆盖、不作为本次提交物。
