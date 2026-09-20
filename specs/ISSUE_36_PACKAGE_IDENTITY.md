# Issue 36：修正包身份检测并重算平台部署证据

类别：UI工具／测试诊断。规则依据 R01 的运行入口与真实版本证据；不改变比赛协议、策略或官方规则。来源：[Issue #36](https://github.com/guihousun/Competition_HW/issues/36)，2026-09-20读取完整正文及全部评论（0条）。

## 已定位问题

公司端称2654场未匹配到 `9c6ed09|baac9f4`，把未命中这些字符串且未命中旧版的日志归为OLD。该分类混淆了交付提交和包内源码身份，而且把未知等同于旧版。

| 身份 | 当前值 | 用途 |
|---|---|---|
| 首次交付提交 | `9c6ed09adc90f0b2b670456349cfb96c4efad495` | Git中提交包与报告 |
| 后续报告提交 | `baac9f42b7fee5d15124d834df025944c374f47e` | 报告更新，包未变化 |
| 包内源码commit | `2296d9a16aa9a96df41225a785f0b313e12463f2` | 启动identity.code_commit |
| tar.gz SHA256 | `388582d9649c1253c0484414502562aae57b725f9b3c40b7e9822a2474fdac55` | 上传文件字节校验 |
| manifest.source_digest | `28fbbd4945c12e06f5bbbb45a63a95a5d78cdfd3fa7981de18be3fb6527371c1` | 包内清单的源码内容身份 |

本机从实际根目录参赛包解压，Python3.11.10启动原main3.py，取得 `code_commit=2296d9a…`、`commit_source=manifest`、`manifest.state=verified`。Issue原正则未命中，正确源码匹配命中。**证明检测会漏报，尚未证明公司平台已经部署新包。**

## 修复交付

新增 `tools/audit_battle_identity.py`，从指定manifest读取预期源码，不硬编码交付SHA。只解析控制台identity行；校验完整code_commit及已验证manifest/digest；未知、读取失败、混合身份、未验证源码分别输出。没有identity不能判OLD。工具只读取本地日志，不访问官方平台。

```powershell
git pull origin codex/sgh
python tools/audit_battle_identity.py --manifest CoreGeek.manifest.json --logs "D:\research_vault\work\projects\Code_competition\battle_batches" --output "issue36-identity-rescan.json"
```

若日志在其他目录，用实际路径替换 `--logs`；输出路径必须尚不存在。可指定单个日志文件。目录模式只递归读取teamA.log/teamB.log，支持UTF-8/BOM和带BOM的UTF-16。

## 公司端立即重算与回传

1. 在现有2654场日志上重扫，无须先重传包或重新比赛。也可快速搜索 `"code_commit":"2296d9a16aa9a96df41225a785f0b313e12463f2"` 找出样本，随后核对完整identity。
2. 工具统计单位是**日志文件**，同时包含双方，不能直接当我方场次。请按record.json中的teamAid/teamBid与我方4474关联对应teamA.log/teamB.log，再按实际对战/半场去重统计。不要按颜色或文件名推断我方。
3. 分列：EXPECTED_VERIFIED_SOURCE、EXPECTED_SOURCE_UNVERIFIED、OTHER_REPORTED_SOURCE、MIXED_IDENTITIES、UNKNOWN_NO_IDENTITY、UNKNOWN_INCOMPLETE。OTHER只说明记录了不同源码；UNKNOWN不支持版本结论。完整源码身份匹配也不单独证明压缩包字节相同或比赛PASS。
4. 回复#36：正确匹配数量、已识别其他源码及数量、未知数量、日志覆盖范围、最早/最新命中时间，并附少量我方完整identity原行及对应对战编号/半场/队伍映射。不要把整批原始对战日志自动公开。
5. 只有纠正身份后，仍能证明我方一直运行其他源码，才继续查上传记录、启用包选择、队伍/模式关联等平台部署问题。上传200与前端成功提示不单独证明比赛调度采用新包；暂不假设灰度池、53小时预热或缓存机制。

## 验收与边界

10项独立预期测试通过：交付SHA漏报复现、缺失身份、叙述内SHA、其他源码、混合、manifest未验证/摘要不符、损坏行、UTF16、无效manifest、读取失败。实际包启动日志复扫为EXPECTED_VERIFIED_SOURCE；原正则为0。仅诊断工具变更，不需要重跑整场策略基准，也不替换参赛包。

公司的2654场原始日志尚未在此端重算；#36保持开放，等待纠正口径后的真实统计。不宣称部署成功、失败或内网PASS。
