# 提交包本地验证与内网回测指南

适用：`guihousun/Competition_HW`。本文只讲**怎么拿到正确的候选包并回测**，
不改规则、不改策略、不声称已通过公司平台。规则依据：`docs/DEVELOPMENT_RULES.md` R01/R08。

**当前状态**：Issue #10 报告 `cant get package`，owner 说明是**直接上传压缩包**（不是下载 URL）。
被拒绝的字节我们没有看到，所以格式/上传/平台存储都仍是**未定位项**；下面把**包侧变量**降到最少：
真实 gzip+tar、单一 `CoreGeek/` 根目录、`CoreGeek/main3.py` 入口、清单+旁车哈希。

## 1. 固定使用 codex/sgh 获取测试版本

**最简单的上传方法：更新仓库，上传项目根目录 `CoreGeek.tar.gz`。**

```powershell
git switch codex/sgh
git pull --ff-only origin codex/sgh
Get-FileHash .\CoreGeek.tar.gz -Algorithm SHA256
Get-Content .\CoreGeek.tar.gz.sha256
```

根目录压缩包是 Codex 已构建并验证的交付物，不需要用户再去找 Release 或自行打包。
包内源码 SHA 见 `CoreGeek.manifest.json` 的 `commit`。平台报告旧包 `failure` 后，参赛包已改用官方原版 `main3.py` 和 `pyproject.toml` 的原始字节，直接加载同级 `src/agent/server.py`；不再转入 `Demo/CoreGeek/` 子目录。
参赛 HTTP 服务不加载网页、录制器或模拟器；根路径 GET 仅返回就绪 JSON。源码仓库的 `python main.py` 仍提供本地可视化。
这项兼容性收敛尚需平台复测，不能据此声称已经定位或修复 `failure`。当前根目录参赛包已整合通过 Codex 审核的 DSH 防御朝向/出口策略，与本地模拟器共用实现，见 [防御审核记录](../reports/issue-12-defence/CODEX_REVIEW.md)。
运行环境按用户提供的官方截图核对为 **Python 3.11.10**，本次已在 Windows/Ubuntu 的该精确版本检查实际包入口。接口正文已重新与 Issue #7 对照一致；验证覆盖与局限见 [本次打包验证](../reports/platform-package-20260914/VALIDATION.md)。
上传的是 `.tar.gz` 文件，不是同目录的校验/清单文件，也不是 `Demo/` 中的原始示例。

包先由确定的源码 SHA 构建，再由后续交付提交加入仓库；因此包内 SHA 可以与包含二进制的仓库 HEAD 不同。
反馈优先记录 `CoreGeek.manifest.json` 中的 `commit` 和包 SHA256，同时可附仓库 HEAD。这避免把打包产物包含自身造成版本循环。
后续 Codex 每次交付更新根目录压缩包、旁车和清单，不让用户切换下载入口。

`codex/sgh` 是公司电脑固定拉取的测试集成分支。Codex 审核并完成相关本地检查后，将可测试变更合入此分支；内部工作分支和 PR 由 Codex 管理，不要求参赛者反复切换。
分支上的新版本仍可能等待内网验证，合入不等于官方 PASS。每次测试只需记录实际 SHA。

也可以直接从 Issue 中最新交付评论的 Release 链接下载 **Assets → CoreGeek.tar.gz** 和 `CoreGeek.tar.gz.sha256`。
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

以后更新始终使用同一组命令：

```powershell
git switch codex/sgh
git pull --ff-only origin codex/sgh
git rev-parse HEAD
git status --short
```

`git status --short` 应无输出；若公司电脑有自己的修改，先保留它们，不要用强制重置覆盖。
把 `git rev-parse HEAD` 的实际结果附到 Issue。只有复现特定旧版本时才另行检出指定 SHA。
源码 SHA 与包清单 SHA 需要分别核对：根目录受 Git 管理的包会随 `git pull` 更新，另行下载到其他文件夹的旧包不会。

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
$sha = git rev-parse HEAD
python tools/build_competition.py --repo . --ref $sha --output "dist/$sha/CoreGeek.tar.gz"
python tools/build_submission.py verify --archive "dist/$sha/CoreGeek.tar.gz" --sidecar "dist/$sha/CoreGeek.tar.gz.sha256"
```

- `verify` 会检查：真实 gzip（含 **CRC/尾部完整性**，截断包会被拒绝）、“ZIP 改名 `.tar.gz`”、
  单一 `CoreGeek/` 根目录与 `CoreGeek/main3.py` 入口、必需文件、逐文件哈希与源摘要、
  重复成员/清单项、路径穿越与特殊成员、旁车哈希。
- 文件名 `CoreGeek.tar.gz`、根目录 `CoreGeek/`、直接入口 `main3.py` 是与**官方示例包一致**的兼容性选择，
  **不是**已证实的平台要求；包的精确身份以清单和哈希为准。
- 可上传文件在 `dist/<本次SHA>/CoreGeek.tar.gz`，旁边是 `.sha256`；重复构建同一目录会拒绝覆盖，可复用已验证产物或另选新输出目录。
  工具仅包含指定提交的内容，不包含未提交改动。发布评论另行提供实际测试记录。

解包后本地再跑一次（验证的是**将要上传的字节**，不是工作树）：

```powershell
python tools/build_submission.py extract --archive "dist/$sha/CoreGeek.tar.gz" --dest .\unpacked
cd .\unpacked\CoreGeek
python main3.py 8080
```

## 4. 内网回测与反馈模板

新候选默认把详细比赛请求、响应及连续回合变化记录在包根目录 `logs/`。
可按失败回合导出窗口，并生成策略回放/模拟器对齐材料，操作见 [详细日志使用说明](TRACE_LOGGING.md)。
文件只保存在运行机器；若平台不支持导出容器文件，先提供可获取的控制台 `identity` 和 `response` 行。

按平台要求**直接上传** `CoreGeek.tar.gz`（若平台需要其它文件名/形态，以平台提示为准并回报）。
官方文档指定的启动形式为 `bash run.sh <平台指定端口>`，监听 `0.0.0.0`。
本包另保留 `python3 main3.py <平台指定端口>` 兼容示例入口。
包内根目录有 `main.py`、`main3.py`、`run.sh` 和 `pyproject.toml`，实际运行代码在同级 `src/agent/`。
没有外部服务依赖，无需在内网联网安装 Python 依赖；实际 Python/沙盒版本仍需平台核对。
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
DSH 专员或 Codex 实现 → Codex 审核及本地验证 → 合入固定 `codex/sgh` 测试分支、生成上传包与精确 SHA → 用户同 SHA 内网回测。
**证据不完整不阻塞**：可并行做有界只读分析、独立可复现修复、身份/可观测性与候选包工具；
需要 owner 的只有平台侧动作与真实回测。`codex/sgh` 允许等待内网验证的已审核候选；宣称官方通过或晋升正式稳定版本，仍须取得对应被测 SHA 的真实内网 PASS。

## 6. 边界（不要过度声明）

- 本地打包、本地启动、本地 POST、`verify` 全绿都**不是**公司平台通过；平台是否接受该包**尚未验证**。
- 诊断行是工程元数据，不是官方判题字段，也不构成官方认证。
- 原始示例包 `Demo/CoreGeek.tar.gz` 只作布局参考：不改写、不覆盖、不作为本次提交物。
