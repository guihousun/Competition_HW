# Issue #29、#42–44：check兼容与任务证据交付

源码 `043918d495313229b3d123aeca7b92a4a45d1281`；开发基线 `0d0ddb2`，实战反馈被测源码为旧 `c0743915`、包 `9d615b66…`。这不是新候选的实机结果。
本包SHA256：`bc72c72742e5a010f98d86ac3bc0ae8d9993bb8391ee585bac0c063402b34e2f`；`competition-flat-v1`保留官方原始入口。交付提交另包含本报告与产物。

## 已实施

- #43/#38：精确裸`./check`进入CRLF兼容工具。cwd仅在运行该命令的沙盒读取；不借用HTTP主机目录或上一题目录。只在内存规范换行，原check不改写，不伪造退出码/输出/TOKEN。
- 关联exit126且匹配公司env bash^M或既有bin/bash^M错误时，仅恢复check，复合命令的修改前缀不重放。保留8条命令与同命令2次上限；不扩展任意Shell。wrapper增加path_source说明当前路径来源。
- #42/#44：任务摘要增加`success_confirmation_capability`、`unconfirmed_is_failure=false`、`success_rate_eligible=false`。当前缺少可正向确认的官方结果字段，旧false不表示失败；保留真实错误回执及增分观察，不推出任务成功率。
- 对非法模型计划记录静态拒绝原因；agent_state.lastPlanRejection包含最近拒绝回合和原因，区分JSON、request_id、evidence_ids等问题，不输出原模型正文或凭据。不盲改8次工程上限和正常回执等待。
- #29：新增两图各自逐日线索测试（1/2/3/4→第5天，1/3/5/7→第8天）、未来线索隔离、跨场引用拒绝、商品参考。只更新验收和资料；没有把(3,3)、材料、日期硬编码到策略，也没有把答案作为尚未收到的news注入。

类别/规则：内部工具实现修复与观测诊断，R01/R07、接口§1.1/§1.7/§2.1及任务书§五；[Spec](../../specs/ISSUE_42_44_TASK_RECOVERY.md)。上一版三火炮/前六墙策略与模拟器结算未变。

## 验证

- 冻结源码Linux/Python3.12.3全量1289项，真实进程退出0，见[回执](tests-receipt.json)与tests.log。
- 真实POSIX专项涵盖CRLF原文件不变、两任务不同cwd、非零退出7、超时124、输出截断、缺文件/非脚本/链接拒绝、修改前缀一次、真实TaskAgent派发与回执关联。具体预期见[test_issue43_check_cwd.py](../../tests/test_issue43_check_cwd.py)，不依赖模型声称成功。
- Windows/Python3.11.10真实参赛包200轮HTTP通过，最大300.0ms，见[报告](http-report.json)。模型与工具任务为脚本测试输入，不是实际LLM解题率或内网PASS。
- #29相关19项测试通过；缺少关闭时间时保持unknown、不提前召唤。第5/8天的真实动作只在另行标注的合成完整窗口对照中验证；[资料说明](../../docs/ISSUE_29_REFERENCE_UPDATE.md)。

## 不能从当前日志推断的结论

1. “4题0个confirmed”不能写成0/4成功率。check通过和提交是已观察事实，正向官方判题仍未确认；任务期间金币/总分增量也可能包含其他行动。
2. stream是本地运行UUID与队伍/地图/基地身份哈希。变更可能来自进程、基地标识或队伍，不是官方换边证明。#42中的半场/402轮空转归因仍须原始字段核对。
3. 北京题8prompt只证明低产出。合法need_info会立即停止；不合法模型回执可能反复请求至上限。现有摘要不能确定是哪一种。新日志帮助下次区分，未声称已经修复该题逻辑。
4. #29两种公共新闻明确开启日、参考坐标和材料，未明确闭门时间。#43任务文档中的第五天结束关闭属于该沙盒任务契约，不能未经核验替代两种地图全局宝藏规则。

## 公司复测

固定拉取codex/sgh，上传根CoreGeek.tar.gz。记录包内源码SHA和包SHA。
对同一任务保留实际executeCmd、相邻lastCmdResult、generation/回合、agent_state、submit及判题回执。裸check应显示path_source=sandbox_cwd、CRLF按需规范，后续保持真实exit/token；失败输出不能记通过。
从已有北京题记录补充已关联的模型回执和计划拒绝原因；换边补原始teamOur.type、基地ID/位置和官方battle/half字段。无需等待新资料才能使用本次check修复。
来源Issue保持打开；未声明官方成功、未修复未知模板CRLF来源，也未把本地高分当实战成绩。
