# Issue 26 历史批次验收

日期：2026-09-15。变更类别：测试与文档。开发基线83285bcc32822117a7909863ec2ae4ddad64185c，运行源码仍为2a95c26c715c22aedf3263dc95ccd64f7ea0dc60。

## 本次实际验证

Python 3.11.10，Windows，真实planner/router路径，脚本化模型输入：

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -s tests -p test_task_answer_contract.py -v` | 10项通过，0.269秒，无跳过 |
| `python -m unittest discover -s tests -p test_task_pipeline.py -v` | 7项通过，0.242秒，无跳过 |
| `git diff --check` | 通过 |

新回归 `test_issue26_instruction_echo_is_rejected_and_corrected_on_both_sides` 覆盖4个组合：challenger/defender × 裸提示回显/JSON字符串包装。题目明确要求对象时，`requirement: 遵守题目原文指定的格式、字段与单位` 不会进入submitAnswer，也不消耗提交计数；带拒绝原因重新规划后，合法结构按原文提交，位于15轮窗口内。每轮经过状态序列化。

这些用例使用独立合成题与脚本化模型回复，未访问官方沙盒或外部LLM。格式合法不是答案正确。未知或未成功读取的原题契约仍不强制JSON，不能声称所有提示回显都被拦截。

## 证据口径

Issue 26报告的6场/36个episode/1次提交来自1831136d。当前fetch到的origin/codex/sgh为83285bc，尚无所列596xxx日志文件；本次只读取正文及全部评论，不宣称已逐行复盘这6场。用户说明公司代理延迟推送，后续日志到达再补核验。

Issue 25原始审计中，598595+598290两场为7次提交、3个token对象；包含598291三场则10次提交、6个token对象。Issue 26的跨构建对比沿用了更正前的#25计数。合并统计前需明确是否包含598291并核对本批原始记录。

旧批次超时不证明当前候选仍有相同故障；未观测正向判题不证明零任务收益，更不能单凭unknown字段归因全部胜负。

## 交付与后续

仅增加Spec、回归测试和本报告，比赛运行代码及参赛包没有改变。无需以新测试提交冒充新的实机策略版本。

- 源码：`2a95c26c715c22aedf3263dc95ccd64f7ea0dc60`
- 包SHA256：`e5f24932abfb8db539313835921ce4cc10bd42974da9ec2f73821d5996e5bfb4`
- [固定参赛包下载](https://github.com/guihousun/Competition_HW/raw/83285bcc32822117a7909863ec2ae4ddad64185c/CoreGeek.tar.gz)
- [当前回测说明](../../docs/INTRANET_ISSUE25_CANDIDATE.md)
- [分诊与验收Spec](../../specs/ISSUE_26_HISTORICAL_BATCH.md)

沿用当前候选等待新SHA实机反馈；保持Issue 26开放，原始日志暂缺与候选测试并行。未新增全量比赛基准，也未声明当前候选的内网PASS。
