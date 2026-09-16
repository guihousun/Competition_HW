# Issue 29 世界任务上下文候选验证

源码：`7c41f182e0870cfa13ded850158d7dabdfe34662`。
包 SHA256：`f3815ba867c5ac751c3a6bce6392005a844fb92927c817f6f801b4dec7e7c7f6`。

修复类别：策略辅助实现/工具；规则 R01/R02/R06/R07。详见 `specs/ISSUE_29_WORLD_CONTEXT.md` 与 `docs/WORLD_NEWS_FEEDBACK.md`。

- Linux WSL Python 3.12.3：全量 1037 项，0 跳过，288.840 秒，通过。不是官方 Linux Python 3.11.10 环境。
- Windows Python 3.11.10：新增 7 项通过；开发环境另跑 36 项世界记忆测试通过。
- 从实际 tar.gz 解包启动原始 main3.py：Windows/Linux 各 12 次 POST，两阵营；跨日线索、真实日期校验、召唤动作和反馈停止重复执行通过。最大响应 <139ms。
- 原始示例入口保持一致；包结构/摘要校验通过，69 文件。全量报告见 full-linux.log，HTTP 记录见两个 JSON。

复现包检查（仓库根目录）：

```bash
python reports/issue29-world/verify_world_package.py --source . --archive CoreGeek.tar.gz --output world-package-check.json
```

本次模型回复和成功结果码是手写测试输入，验证动作链路而非真实 LLM 解题率；没有内网 PASS。
实际 Issue 中公里到格子的比例、开放窗口结束时间还不能作为已确认的官方条件。
模拟器结算未改，运行中的五路 DSH 冻结源码未改；这些包检查也不是完整对局胜率。
