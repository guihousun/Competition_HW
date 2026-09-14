# Agent参赛候选交付验证

日期2026-09-15；类别：策略/Agent架构、模拟器与UI工具；依据R01/R02/R06/R07/R08。
包内源码SHA：`ddca27eb3b3ec2d40d7d4b30d9f06fc9bad93d71`。
包SHA256：`922e5aad87ef20dfecb4be8619a1370b2758fbd10a2c8a3a7f1147ded0b95052`，205340字节。
源码提交先确定，再构建压缩包，再提交交付文件；交付提交与包内源码SHA不相同。
交付文件所在提交可用 `git log -1 --format=%H -- CoreGeek.tar.gz` 查阅。

## 已确认

- 原样保留官方main3.py入口；单一CoreGeek顶层目录，实际gzip容器，文件清单和哈希校验通过。
- 默认开启TaskAgent和WorldAgent，显式off可回退；原正式HTTP字段/监听/端口方式不变。
- 711项集成回归通过；真实固定查询任务3种各3次，9/9完整流程通过，48次模型请求。
- 一场真实长文档任务及一组四日真实新闻/传闻通过，追加重复实验另行记录。
- Windows与Ubuntu/Python3.11.10共10项入口与日志检查通过，每项25次POST。
  Windows使用main.py/main3.py，Ubuntu实际执行bash run.sh；不可用日志目录没有破坏响应。
- 实际解压包的独立HTTP进程，各运行完整1300轮。没有环境变量强行开启Agent，验证的是包的默认配置。
  另一进程只结算外部响应；模型为脚本、工具为虚拟沙盒，没有宿主机任意Shell或真实API费用。

| 环境/场景 | 完成任务 | 新闻/宝藏/任务prompt | 基地HP | HTTP p95 | p99 | 最大 |
|---|---:|---|---:|---:|---:|---:|
| Windows，seed1 challenger | 18 | 4/3/57 | 1500 | 276.64ms | 304.86ms | 392.59ms |
| Ubuntu，seed90317 defender | 23 | 4/4/72 | 1500 | 188.58ms | 214.52ms | 274.59ms |

没有同轮prompt/executeCmd冲突，所有已测HTTP响应低于5秒，也低于本地p99≤1秒的工程目标。
原始逐请求耗时与包指纹见相邻JSON。不同系统使用不同种子和阵营，不能将其用作操作系统性能对比。

## 公司电脑使用

```bash
git switch codex/sgh
git pull --ff-only origin codex/sgh
git rev-parse HEAD
```

上传**仓库根目录的CoreGeek.tar.gz**。不要上传Demo目录里的原始示例，也不需要重新压缩。
记录当前交付SHA和CoreGeek.manifest.json中的包内源码commit，回传启动、动作与任务结算证据。

本地复现实包HTTP验证（Python3.11.10，输出目录必须全新，无API费用）：

```bash
python tools/verify_agent_http.py --source . --archive CoreGeek.tar.gz --output .workflow/http-check-1 --seed 1 --side challenger
```

## 仍需区分

全部是本地验证，不是官方或内网PASS。固定刷新坐标、官方真实沙盒/题目、双队胜负等差异仍按DEVELOPMENT_RULES.md登记。
已测全功能策略的得分具有地图相关取舍，不宣称全面优于仅任务模式；备料消费者消融和未采纳策略均保留报告。
三类任务与Agent集成已可运行；追加重复验证和整个goal的最终审计另行完成，不用交付候选冒充全部研究结束。
