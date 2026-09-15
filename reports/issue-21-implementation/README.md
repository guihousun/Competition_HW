# #21/#22 运行代码候选验证

源码提交：`66a4d9a6c704aa0425d995cff29ab812ba6cc98e`。类别为Agent策略/工程优化及本地模拟工具；官方规则依据R01/R07，任务书§五、接口§1.1/§2。官方文档、原始Demo、数值与参赛协议未改。

## 改动与验证结果

| 范围 | 结果 | 证据 |
|---|---|---|
| 冻结源码全量测试，Python3.11.10/Windows | 913项，OK；跳过2项平台相关检查，耗时353.715秒 | full-tests.log |
| 只读定位工具，Linux3.11.10 | 8项通过，包括符号链接拒绝、重名、字面文件名和截断 | linux-workspace.log |
| Linux真实Shell及本地API/check | 12个子场景，API/工程各3个变化×双方阵营，5个响应回合提交；1项总测试通过 | linux-pipeline.log；tests/test_task_pipeline_linux.py |
| 15轮任务完整模拟器 | 3种种子1/6173/90601×双方阵营，全部完成；各2次模型请求、2次命令 | tests/test_task_pipeline_simulator.py；full-tests.log |
| 命令边界/超限恢复/最后提交窗口/可选摘要 | 7项通过（含双方子场景） | tests/test_task_pipeline.py；full-tests.log |
| 实际tar.gz的main3/main/日志不可写/缺manifest启动 | Windows20次POST通过，Linux25次POST通过（含bash run.sh） | smoke-windows-final.json / smoke-linux-final.json |
| 实际tar.gz任务HTTP链 | 双方阵营共10次POST，第5回合输出正确submitAnswer；最大HTTP159.199ms | package-pipeline-final.json |
| 包结构与manifest/hash | flat-v1，原版main3.py，56文件，源码与sidecar核验通过 | package-build.json |

Windows跳过的是无法创建符号链接的环境检查和Linux Shell测试；对应测试已在Linux执行。Linux工具测试在c1637ee完成；后续66a4d9a只同步本地脚本模型的证据标签并增加包验证工具，Linux所测运行模块与测试文件字节不变。正式包启动/HTTP验证使用最终66a4d9a包。

新模拟器场景可通过local_task_cases.install(state, ['nested-api-15-rounds'])显式启用。任务时限/奖励在公开playerTasks同步展示，不覆盖默认场景的数值，不进入策略私有观测。已有网页脚本模型和长资料演示已通过回归。

## 证据边界与保留失败

- 这些测试使用脚本化模型，验证执行链及判定，不证明真实模型对未知题的通过率。没有本轮真实LLM重复试验或同SHA内网PASS；DSH既有任务因OpenRouter Key额度限制原生失败，没有重复派发。
- Linux服务是测试自建的loopback API/check，验证文件/权限变更和真实请求响应；未宣称复制官方v7.0 chroot/nobody隔离。
- 开发中第一轮全量发现3个旧模拟器脚本适配失败，保留于full-tests-before-adapter-fix.log；这轮期间还补充了最后提交窗口守卫，因此只作开发诊断，不作为冻结版本认证。修复适配后重跑上述完整913项。
- package-pipeline.json保留的是先前c1637ee包的中间验证，交付证据以带final后缀的报告为准。
- 本次没有修改防御布局、武器伤害或机器人波次；不声称修复所有失分原因或提高基地存活率。原有失败实机和模拟器证据继续保留。
- #21 P0及#22主要上下文/工具契约改进已有运行实现；专用泛化求解器、扩展SOP晋升、真实模型效果验证、官方沙盒隔离对齐与既有防御升级仍有后续阶段，不自动关闭两个来源Issue。

## 交付包

`CoreGeek.tar.gz` SHA256：`676e42ab26837599479944f1dcadb6187de07b01f7b1376e34addbc3038165c7`。

包内源码SHA在manifest中，二进制交付提交在其后。[公司上传与回测说明](../../docs/INTRANET_ISSUE21_CANDIDATE.md)。
