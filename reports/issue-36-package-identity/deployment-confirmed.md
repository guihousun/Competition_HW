# Issue 36：参赛包身份重扫结果

2026-09-20。来源是公司电脑在 [Issue #36](https://github.com/guihousun/Competition_HW/issues/36) 回写的三条完整评论；本机未取得公司原始 5319 个日志文件，因此下面的数量是公司端证据登记，不是本机重新计算。

## 结论

原来的“`pkg 388582d9` 在 2654 场中零出现”结论作废。检测使用了交付提交 `9c6ed09` / 报告提交 `baac9f4`，而包启动 identity 的源码提交是 `2296d9a16aa9a96df41225a785f0b313e12463f2`，导致新包被归入未知或旧包。

公司端随后拉取 `fe1ee73`，按包内 `CoreGeek.manifest.json` 的以下身份重扫：

| 项目 | 值 |
|---|---|
| 包 SHA256 | `388582d9649c1253c0484414502562aae57b725f9b3c40b7e9822a2474fdac55` |
| 源码 commit | `2296d9a16aa9a96df41225a785f0b313e12463f2` |
| `source_digest` | `28fbbd4945c12e06f5bbbb45a63a95a5d78cdfd3fa7981de18be3fb6527371c1` |
| 日志文件 | 5319（teamA 2659、teamB 2660） |
| 对战 | 2665 |
| `EXPECTED_VERIFIED_SOURCE` | 2036 |
| `OTHER_REPORTED_SOURCE` | 617 |
| `UNKNOWN_NO_IDENTITY` | 2613 |
| `UNKNOWN_INCOMPLETE` | 53 |

按 `record.json` 的 `teamAid/teamBid` 映射到我方 `teamId=4474` 后，公司端报告 2036 个独立我方对战半场命中；最早为 `pk-615658`（2026-09-17 03:11:18，teamB），最新为 `pk-666405`（2026-09-20 09:59:35，teamB），并报告期间无间断。

## 证据边界

这证明公司端日志中记录了已验证的 `2296d9a` 包身份，足以确认该包已进入实际对战日志。它不等于比赛胜率、策略最优或官方 PASS；未知日志仍不能归为旧包，也不能用双方日志文件数直接当我方场次。后续策略分析应以这 2036 个已映射半场为样本，并保留 617 个其他身份与 2666 个未知/损坏文件的缺口。

本机实际解压启动同一 `CoreGeek.tar.gz` 的独立证据仍在 [identity-proof.json](identity-proof.json) 和 [startup.txt](startup.txt)。完整公司原始日志不上传仓库，避免公开内网路径和冗余对战数据。

## 下一步

Issue 保持开放。下一轮应按源码身份分组比较生存、战果、任务、金币、超时和基地终局，先确认半场统计口径；不要再用交付提交 SHA 搜索运行身份，也不因为包已部署就宣称性能改善或平台通过。
