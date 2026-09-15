# 本轮内网测试候选（2026-09-15）

按用户要求先交付真实场景测试，不等待本地模拟器调优完成。公司电脑继续使用固定分支 `codex/sgh`。

```powershell
git switch codex/sgh
git pull --ff-only origin codex/sgh
git rev-parse HEAD
Get-FileHash .\CoreGeek.tar.gz -Algorithm SHA256
```

直接上传仓库根目录 **CoreGeek.tar.gz**。不要上传GitHub自动生成的源码zip，也不要使用Demo目录下的原始示范包。

- 包内源码：`1831136d5a30f89caba3cb5ec1d2b91838684c2a`
- 包SHA256：`f185f5f2f69642f53d5d715ff840524d8d2e592f08910b1e9bfd43195af46586`
- 格式：gzip压缩的tar，顶层CoreGeek，保留原示范包main3.py入口；Python3.11.10。
- 交付提交包含构建产物，故它与包内源码提交不同；以CoreGeek.manifest.json和本次git rev-parse HEAD分别记录。

## 本版包含

新闻证据账本、停产/价格独立消费、长传闻原文检索与未知项提示、受约束的清场寻宝准备；模拟器前七夜实测波次/固定列阵/墙体遮挡；侧方基地防御朝向修订；对应前端和日志说明。

## 测试重点

1. 启动及接口是否成功，平台有无failure/超时，确认实际红蓝阵营。
2. 自进化：领取确认→模型返回→沙盒执行/回执→提交→判题结果；保留失败前后几轮原始请求与响应。
3. 新闻：原文中的日期、停产/恢复、价格与实际商店信息能否对上。
4. 宝藏：多日线索、未知项说明、购买/移动/召唤及实际回执。新的提示词尚未完成三次新的真实模型复验，不预先声明成功率。
5. 防守：怪物出生坐标/数量、墙是否挡住攻击、控制者何时阵亡、基地失守回合。出现失败直接上传证据，不需要先自行定位原因。

## 已知限制

- DSH的完整升级行程和召唤令归属/日限/眩晕时序修复**尚未审核，未包含在此包/仓库版本**。策略仍可能金币充足却未及时升级。
- 后三夜数量、固定点精确几何、机器人选敌等本地未确认假设继续标注；比赛结算以官方系统为准。
- 完整895项本地回归通过；实际包Windows/Linux共45次POST、run.sh与日志故障检查通过。实际包本地整局HTTP p99约246ms，但第901状态基地被攻破（900次请求），不算生存通过。
- 本地成功不是官方认证，本候选不是内网PASS；目标仍是用真实数据迭代，而非追求模拟器满分。

## Issue反馈模板

```yaml
INTRANET: PASS / FAIL / BLOCKED
SOURCE_COMMIT: 1831136d5a30f89caba3cb5ec1d2b91838684c2a
DELIVERY_COMMIT: git rev-parse HEAD 的输出
PACKAGE_SHA256: f185f5f2f69642f53d5d715ff840524d8d2e592f08910b1e9bfd43195af46586
SIDE: challenger / defender
CASES: 启动 / 单项任务 / 完整对局
ROUND: 出错或关注回合
EXPECTED: 预期行为
ACTUAL: 实际行为
EVIDENCE: 脱敏的请求→响应→下一轮回执及相关日志
```
