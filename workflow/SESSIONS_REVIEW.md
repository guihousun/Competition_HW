# 专员session复用验收

日期：2026-09-12。工程工具变更；不改变官方比赛行为。

- 固定 deepseek-official / deepseek-flash / max。
- 实际通过文件派单器向同一 qa-tooling 原生session连续发送4次消息，原生turn依次为1、2、3、4，message ID不同、session ID不变。
- 第二轮没有重发第一轮的测试标签，也禁止读取文件，仍准确回忆标签，验证原生上下文续接。
- 第三轮在同一session完成有限测试Spec，第四轮接收Codex审核意见；没有重建、压缩或重放历史。
- Codex独立复核改动与结论，修复输出目录提前拒绝和PID登记中断恢复；已反驳错误的“文件锁不能跨进程”判断并用实际子进程测试验证。
- 24项工具测试通过：多轮通知/回执顺序、不同session隔离、工作树/Spec版本校验、固定工作树归属、重复输出保护、跨进程互斥等。
- git diff --check通过，官方原文与示例未修改。
- 只实测qa-tooling活跃专员，其余三类为按需启动配置，未虚构已经执行的任务。
- DSH进程重启后的原生恢复不在当前SDK接口内；本工具拒绝静默替换，不自行实现上下文恢复。
- 没有公司内网实机测试；此项为协作工具验证。

测试命令：`python -m unittest discover -s workflow -p "test_*.py" -v`

审核源文件SHA256（工作树字节）：

- `dsh_sessions.py`: `9f61f1b2e9ee2fb81bd45b5eabfd55b48f95131fc6df2f805e1a237fe434bf35`
- `test_sessions.py`: `74722e50b750b20d086b817fced0390dc9426f9fd01b82b88f06a8477bf6947d`
