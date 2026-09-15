# Issue #24 验证与交付记录

- [来源Issue](https://github.com/guihousun/Competition_HW/issues/24)，被测旧源码66a4d9a。
- 新候选源码：`9f1a2c82ed09126db35abb4799cd74e3ce9e2e3c`。
- 类别：Agent策略/工程、诊断、本地工具；规则R01/R07/R08。官方文档/样例、伤害、奖励规则与正式传输字段未修改。
- Spec：[ISSUE_24_TASK_TOOLS_FEEDBACK.md](../../specs/ISSUE_24_TASK_TOOLS_FEEDBACK.md)。

## 落实内容

1. 内部http计划转换为官方executeCmd：标准库GET、中文参数编码、有界响应、JSON类型与键名描述。只在真实401明确Bearer提示或400明确缺参数提示时有限恢复，至多3次请求/约6秒预算；不跟随跳转。遇到200空数据仍保留原样，不能当作任务完成。
2. 在同队保存公开文档hash与端点绑定的传输方法，只记录鉴权机制与参数名映射，不存城市/密钥/token/响应记录。后题重新读同文档才显示；最新文档覆盖旧读数，400/401/403或传输错误撤销相应旧提示。支持schema3到4存档迁移。
3. 内部check计划读取小型脚本，CRLF在内存归一化后交明确解释器执行；不改原check文件。保持工作目录、非零退出与6秒超时，超时终止进程组。它在平台沙盒执行，不在策略HTTP进程里执行。
4. task_text_ended增加提交回合/答案hash、相邻观测金币与积分差值、动作回执、errors和观测缺口，明确unknown是本地未确认，不是官方失败。差值不自动归因于任务奖励，不推断PASS。
5. agent_state增加方法计数/HTTP方法计数/原文来源数；memoryReads仍只表示inspect次数。虚拟沙盒增加显式HTTP服务fixture，不连接宿主网络或执行任意fixture代码。

## 验证

| 范围 | 结果 | 文件 |
|---|---|---|
| 冻结源码全量，Windows Python3.11.10 | 930项，OK，316.844秒；5项Linux/权限相关跳过 | full-tests.log |
| Linux HTTP/check工具 | 9项通过：401→400→200、Unicode、对象/列表/空数据、拒绝跳转、截断、CRLF/非零退出/进程组超时及实际Shell传输 | linux-tools.log |
| Linux只读题目定位 | 8项通过，含Windows跳过的符号链接检查 | linux-workspace.log |
| Linux API/工程完整工具链 | 1项测试含12个变体，双方阵营通过 | linux-pipeline.log |
| 实际tar.gz启动 | Windows20次POST与Linux25次POST通过，含main3/main、日志写入故障、缺manifest、bash run.sh | smoke-windows.json / smoke-linux.json |
| 实际tar.gz结构化HTTP计划→恢复→提交 | 双方阵营10次POST通过，第5轮提交；最大HTTP106.0175ms | package-http-tools.json |
| 包核验 | 原版main3入口，57文件，manifest/source/sidecar一致 | package-build.json |

全量测试包含新增方法记忆的跨episode复用/文档更新失效/新鉴权错误撤销/换队拒绝，以及提交证据不能伪造PASS。测试是脚本化模型与本地fixture，不是实际LLM解题率或官方v7.0隔离认证。5项Windows跳过在列出的Linux测试中覆盖。

## 原始反馈核对边界

Issue列出的teamA.log/teamB.log在本轮拉取的main和codex/sgh未找到，指定GitHub路径读取404；已询问所在分支。分析依据当前完整Issue正文和摘录，不宣称已逐行重放原始文件。

工程3次check通过并提交是用户记录；日志unknown不是失败证据。alpha初始75到80须考虑同期建塔等支出，不能据此排除任务收入，也不能直接判它PASS。API请求“风格相同”不证明字节级请求相同或服务随机。task_event只记录任务相关动作，其未显示工人指令不代表全队停工。

本次不修改物理防御策略，不宣称解决所有失分、基地生存或全部P1/P2后续任务。下一次以新源码SHA验证真实模型是否使用结构化工具、API题提交数/通过数、工程提交后实际判题与收入，并继续保留未知结论。

## 包

SHA256：`4d35c693893100a328b0f641380d9a7898c8f73e519f8f6ea84e0fbfd810066e`。

固定分支codex/sgh的根目录CoreGeek.tar.gz；manifest.commit代表包内源码，交付提交在其后。
