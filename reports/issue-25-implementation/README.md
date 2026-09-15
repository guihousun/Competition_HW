# Issue #25 答案格式与围栏兼容验证

新源码：`2a95c26c715c22aedf3263dc95ccd64f7ea0dc60`；开发基线35bfe0e，保留用户上传日志。被测历史源码仍为66a4d9a。类别为Agent工程/策略，依据R01/R07/R08，未改官方协议或规则数值。

## 原始证据核对

完整读取三场我方instrumented日志并逐条解析task_event，文件哈希、行号、回合和摘要见[log-audit.json](log-audit.json)。没有执行日志中的Shell，没有将这些事件当成完整逐回合请求回放。

- 598595记录3次issued_submitAnswer，598290记录4次，598291记录3次：共10次，其中6次token对象。Issue的9/5及598290仅2次汇总不符。
- 598290 r37/r39/r41分别337/251/333字符，完整哈希各不相同；是相同heredoc错误模式反复出现，不是完全同一条指令重复三次。
- 598595 r17/r61与598291 r144的3条完整JSON围栏，在原严格nonce解析下拒绝，在新任务边界下均可正确关联。此检查不意味着其内层Shell或答案一定正确。
- 3条heredoc坏命令在新预检下全部被识别。check成功/格式正确不等于官方确认PASS；来源中的unknown保持其本地诊断语义。

#24曾缺失的日志已由用户提交35bfe0e补齐。本轮原始审计覆盖该场我方文件，历史报告中的“当时404”不再是当前缺口。

## 运行改动

1. 任务回复允许整个文本恰为一个JSON/无语言围栏；混合文本、多块、半截仍拒绝，解包后照常校验nonce/重复键/字段/证据。原始回执哈希不变，非任务通道保持既有处理。
2. 从当前原题或明确命名的已读取题目文件提取提交契约，优先放入prompt，并保留题目文件原文。API_DOCS响应样例不会成为答案契约；未知、截断或冲突格式不臆测字段。
3. 明确token对象契约且裸值等于当前成功check回执时，进行结构包装并在同轮提交。旧题token、源码例子、失败/截断check不可自动包装。格式校验不判断答案是否真实正确。
4. 明确JSON对象任务中的标量/裸串/缺字段等先本地拒绝，给出来源和修正要求；已有合法答案字符串原样保留。最后窗口仍不启动无法返回的新请求。
5. 有限识别heredoc关闭后换行&&的错误模式，并阻止最近已确认语法失败命令的原样重发。不会自动改写任意Shell；正常引号和heredoc数据仍保留。

## 验证

| 检查 | 结果 | 证据 |
|---|---|---|
| Windows3.11.10冻结全量 | 939项，OK，291.977秒，5项平台相关跳过 | full-tests.log |
| Linux提交契约与围栏 | 9项通过（含两侧流程） | linux-test_task_answer_contract.log |
| LinuxHTTP/check | 9项通过，含CRLF/超时/实际Shell | linux-test_task_tools.log |
| Linux文件定位 | 8项通过，含符号链接 | linux-test_task_workspace.log |
| LinuxAPI/工程流程 | 1项测试含12个API/工程与阵营组合，通过 | linux-test_task_pipeline_linux.log |
| 实际tar.gz启动 | Windows20/Linux25次POST通过，含bash run.sh | smoke-windows.json / smoke-linux.json |
| 实际包+围栏模型回复+结构化HTTP | 双方阵营10次POST通过，第5回合提交；最大HTTP100.371ms | package-fenced-http.json |
| 包校验 | 原始main3入口，59文件，源码/manifest/sidecar一致 | package-build.json |

5项Windows跳过由上述Linux测试覆盖。一度Linux批量发现命令未匹配测试，零测试记录已排除并在本地保留；表中均为逐项重跑的非零执行结果。发布的Linux文本日志仅去掉WSL启动警告前缀，原始字节本地另存，测试结果未修改。

测试使用脚本化模型和本地fixture，不是当前新SHA的真实LLM通过率、官方判题PASS或基地存活提升。复杂自然语言答案规范与未知数据仍由Agent结合实际题目求解，不填造统计答案或猜判题值。

## 包与复测

SHA256：`e5f24932abfb8db539313835921ce4cc10bd42974da9ec2f73821d5996e5bfb4`。

从codex/sgh根目录上传CoreGeek.tar.gz；[回测说明](../../docs/INTRANET_ISSUE25_CANDIDATE.md)。来源Issue保持open，下一次明确包内源码SHA和各题提交/判题结果。
