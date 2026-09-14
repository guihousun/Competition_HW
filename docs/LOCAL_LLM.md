# 本地 DeepSeek 与官方 LLM 通道

本轮类别：官方接口实现修复 / 本地适配 / UI工具，依据 R01/R07（接口 §1.1、§1.7、§2）。
固定模型 deepseek-flash，推理强度 max。适配官方
[Chat Completions](https://api-docs.deepseek.com/) 与
[thinking / reasoning_effort](https://api-docs.deepseek.com/guides/thinking_mode/)。

## 已补通

- 根路径 POST 使用完整 respond()，保留 prompt、executeCmd，不再只返回角色指令。
  正式入口**不会直接请求外部 API**：官方判题器执行这两个通道，下轮提供 llmResp / lastCmdResult。
- Planner 的 JSON 存档保存待返回请求和已解析命令结果；回执仅在后续回合消费。
  每日普通 LLM 调用额度和任务期豁免仍来自规则；沙盒请求限制在任务期间。
- 显式启用的本地场景使用后台 DeepSeek 请求；等待期间轮数保持不变。
  同对局/同轮/同提示词重传复用请求，失败不会伪造答案或自动重试。
- 前一回合的 llmResp / lastCmdResult 消费后清空，避免污染下一道任务。
- 本地没有官方沙盒，executeCmd 返回明确的 JUDGER_ERROR，不在宿主机任意执行。

## 使用

重启服务后，打开网页的「对局设置 → 体验 LLM 任务（真实 API）」。
演示把开拓者放到任务点附近，用一道人造运算题触发真实模型，然后提交答案并由本地夹具结算。
普通「开始模拟」、录制整场与批量基准默认不调用付费 API。已加载录像只播放，不请求模型。
演示题和奖励不是官方任务或官方成绩。

密钥读取顺序：进程环境变量 DEEPSEEK_API_KEY；Windows 当前用户加密凭据。
Windows 可运行 tools/configure_deepseek.ps1 以隐藏输入方式配置。
凭据保存在 LOCALAPPDATA/CompetitionHW/deepseek-key.dpapi，使用 Windows 用户加密，位于仓库之外。
更新密钥后重启服务。网页、状态、录像、异常信息均不包含密钥；不记录模型 reasoning_content。
不要把密钥粘贴进请求 JSON、Issue 或提交文件。

真实 API 操作只允许本机请求；目标固定 https://api.deepseek.com/chat/completions，拒绝重定向。
每个服务进程最多 20 次真实调用，单次最多 4096 输出 token、60 秒网络等待；这些是本地费用和
可用性保护，不是新增比赛规则。达到上限会报告 local_call_budget_reached；不自动重启绕过预算。
API timeout 为网络读取超时，不保证整个 TCP 响应的绝对墙钟上限。
服务重启后不重放旧任务，旧 pending ID 会明确报失效。后台结果保留于内存，不支持跨进程恢复。

## 证据与未覆盖

测试使用独立手算的 result=42，检查真实策略请求、回执、提交与结算，不给求解器注入答案。
一次真实接口连通检查成功；另一次完整任务链调用 1 次、160 token，等待时停在第 12 回合，
第 14 回合完成，证据和当时的源码哈希在 reports/LLM_VALIDATION.json。
该证据来自本地合成任务，不是内网 PASS。新的源文件哈希单独登记，不冒用旧报告。

仍有缺口：官方隔离沙盒、真实任务判题、固定机器人刷新点（坐标未提供）、完整双队胜负与
其他 docs/DEVELOPMENT_RULES.md 差异。模型可用不等于这些规则已经补完。
