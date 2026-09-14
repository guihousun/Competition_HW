# 平台入口兼容回测包

类别：R01 运行/打包兼容改进及工程诊断。来源：官方原始 Demo、Issue #6 FAQ、Issue #7 接口正文，以及用户提供的编译环境截图（Python 3.11.10、SDK_Python/CoreGeek/main3.py）。

## 失败信息与边界

用户报告：新版上传后平台只显示 `failure`，原始 Demo 在平台正常。尚不清楚失败发生在包接收、启动还是对局阶段，没有错误堆栈；**根因未确认，不能宣布已修复平台故障**。
此前 Windows 检查不足以作为 Linux/平台环境验证，本轮补测精确 Python 版本与 Linux 入口。

## 本包

- 包内源码提交：`00a08bd7eb0e571f01c0420b2459d42e370519cd`。
- SHA256：`0be79ceb4f03a1f675b6ee0bc680c5a1e8ff42fd945468b40d9e5b3e78bf6ce4`。
- 大小：129004 字节。
- `main3.py` 和 `pyproject.toml` 与官方原始压缩包中的对应文件字节完全相同。
- 直接使用 `CoreGeek/src/agent/server.py`；没有 `Demo/` 包装入口、没有网页资源，启动不导入 debug/simulator/recordings/twomatch/local_llm。
- 正式服务恢复原始示例的 HTTP/1.0 响应方式；仍接受指定端口、监听 0.0.0.0，返回完整官方动作及任务通道。
- 策略源码未改，详细日志保留；不可写或关闭日志时仍能返回有效动作。未加入 DSH 防御改动。

## 验证

- 完整回归：383 项通过，268.034 秒，Windows Python 3.11.14。不能把这份全套结论写成 3.11.10 全套通过。
- Windows **Python 3.11.10**：6 项打包/HTTP 专项通过，14.351 秒。
- 实际上传字节解压：Windows **3.11.10** 与 Ubuntu **3.11.10** 各 4 项通过，包括直接 main3、主入口、日志路径不可写和日志关闭。
- Ubuntu 实测 `bash run.sh <port>` 与 `python3 main3.py <port>`，GET/POST 200；官方样例每次返回 3 条指令。响应字段集合、动作枚举、targetPos 数组、controllerId 字符串检查通过。
- Windows 主入口项实际运行 main.py，不冒充已运行 bash。详见两份 `flat-probe-*.json`。
- 专项还验证原版入口字节一致、同提交可重复打包、未导入模拟器/网页、日志工具支持平铺布局、日志故障隔离。
- Issue #7 的接口正文与本地 docs/接口文档.md 去除空白后一致（7255 字符），见 `interface-check.json`；Issue 中样例尾逗号的既有问题不改写官方原件。

这些是本机对自包含参赛包的验证，**不是公司平台 PASS**。下一步只需拉取 codex/sgh 并上传根目录 CoreGeek.tar.gz；以平台实际结果继续定位。
