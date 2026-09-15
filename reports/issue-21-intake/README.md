# Issue #21 分诊证据

来源为 [Issue #21](https://github.com/guihousun/Competition_HW/issues/21) 正文和摘录；全部评论读取成功，分诊时为0条。本轮没有收到完整双方原始日志附件，未声称逐轮重放或验证对手源码。

详见 [实施 Spec](../../specs/ISSUE_21_TASK_PIPELINE.md) 与 [逐项参数审核](audit.json)。摘录 B 节实际提取79项（含OVERTURNED项），保留这一数字与启动日志声称78项的差异，不补造缺项。

## 当前版本的独立核查

被测源码1831136d5a30f89caba3cb5ec1d2b91838684c2a；交付4572e620de0394aecd489c8a50a646c46c0d927d。

Python3.11.10执行：

```text
python -m unittest discover -s tests -p test_task_agent.py -q
Ran 8 tests in 0.006s — OK
python -m unittest discover -s tests -p test_team_agent.py -q
Ran 11 tests in 0.251s — OK
```

其中test_document_query_answer_loop_preserves_json_and_learns_only_method验证真实brain输出的顶层executeCmd和roleCommandMap内submitAnswer/taskAnswer，以及模型调用不占普通任务外额度。模型回复为脚本化测试输入；这19项通过不代表LLM实机解题成功。

另用TaskAgent的实际begin/decide/acknowledge/receive链输入一条2001字符的合成命令，再调用LLMRouter.offer：Agent接受并形成cmd，router返回None。未运行任何Shell命令。对应模块哈希在audit.json；这是新发现的工程契约不一致，尚未修复。建议在提议阶段使用共享命令上限并提供可恢复反馈，不能截断执行或擅自当作官方2000字符限制。

## 本轮结论范围

- 当前SHA的失败已登记；先修资料获取和求解往返，不把6道题答案写进策略。
- 已存在answer到submitAnswer链路；对手也出现prompt，零LLM结论不成立。
- 塔最多3座保留官方基线；对手配置不升格为规则。
- 70/90/116需按targetTeam分组核验，不能直接作为己方每夜数量。
- 后续实施按Spec的P0–P3逐阶段验收。本轮仅文档与诊断报告，无运行代码改动、无新修复包、无官方PASS。
