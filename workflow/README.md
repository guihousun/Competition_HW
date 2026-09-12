# 内网反馈 → Codex 设计 → dsh 实现 → 审核与回测

适用仓库：`guihousun/Competition_HW`。规则依据：项目 AGENTS.md 与
`docs/DEVELOPMENT_RULES.md`。本工作流属于工程工具，不更改官方行为。

默认派单现使用[专员与同会话续聊](SPECIALISTS.md)。明确任务交给DeepSeek，困难或歧义Issue由Codex直接处理。

## 角色与证据

| 角色 | 职责 | 不能替代的证据 |
|---|---|---|
| 用户（公司电脑） | 拉取候选提交、运行官方平台、提交 Issue/PASS/FAIL | 官方环境的真实执行结果 |
| Codex（本任务） | 监测、分诊、顶层设计、Spec、审核、是否返工/合并 | 不能宣称自己进入公司内网测试 |
| dsh | 按批准 Spec 在独立工作树实现，返回差异与测试记录 | 不能自批 Spec、提交、推送或合并 |
| GitHub | 保存反馈、Spec摘要、PR与可定位提交 | 不接收公司凭据或不允许公开的数据 |

dsh 固定为 `deepseek-flash` + `max`。使用 `dsh --profile sdk` 的正式 JSON-RPC
接口，在 initialize 中明确指定模型和强度。8011 网页不是程序化控制的依赖；不绕过其认证，
不改全局默认模型。不支持或不可用时停止该执行步骤，不替换模型。

## 用户怎样反馈

使用 `.github/ISSUE_TEMPLATE/intranet-test.yml`。已有 Issue 的后续结果直接评论：

```text
INTRANET: PASS  （或 FAIL / BLOCKED）
COMMIT: <候选 PR 当前 head 的完整 40 位 SHA>
PLATFORM: <官方平台/规则版本，无内网地址>
CASES: <实际覆盖的用例>
RESULT: <结果与脱敏最小证据>
```

以提交 SHA 为版本身份，不以“最新版”“main”“昨天那版”替代。平台报错附最小请求/响应、回合、
复现步骤、预期与来源。仓库当前公开，不附令牌、账号、内网URL、公司未授权的完整日志。
官方新说明优先引用可公开的版本与节号；不可公开的材料由用户给出允许传播的结论。

## 状态流程

`new → needs-info / specified → implementing → review → awaiting-intranet / merge-ready → merged → verified`

- `needs-info`：缺少被测SHA、现象或规则依据时，Codex一次性集中提问，不猜测修复。
- `specified`：Codex写 Spec，绑定输入 digest、base SHA、允许文件、验收测试。
- `implementing`：同一时刻只运行一个 dsh 任务；一次最多20分钟，同一输入版本最多2次实现/返工。
- `review`：退出0只说明执行结束。Codex检查完整diff（含未跟踪文件）、保护文件、独立测试和Spec验收。
- `awaiting-intranet`：发布候选PR，附精确head SHA和回测步骤，等待用户真实反馈；不自动关闭原Issue。
- `merge-ready`：当前head审核通过、必要检查通过、无未解决意见；若影响官方行为，必须有用户同SHA内网PASS。
- `merged`：Codex使用 expected_head_sha 合并，禁止强推、绕过分支保护或自行增加权限。
- `verified`：有足够证据后再关闭Issue。新反馈失败时回到分诊，保留历史证据。

只允许自动合并本工作流创建且已审查的PR，不批量合并仓库其他PR。
工程工具/纯文档无需官方回测即可合并；协议、策略、结算、运行环境等行为变化默认需要内网PASS。
规则歧义不能仅凭一次运行结果修改官方定义。任何head变化均重新审核和验证。

## 监测与去重

Codex本任务每30分钟由 heartbeat 唤醒，使用已连接的GitHub工具读取用户 `guihousun` 的Issue与评论。
当前CLI `gh` 登录曾返回401，不把它作为必要依赖，也不把凭据写入仓库。

首次读取所有相关Issue。后续按更新时间增量读取并重叠上次窗口，同时检查所有未完成Issue。
读取完整正文和全部评论；搜索截断时分割时间窗口或使用可分页接口，不把截断/鉴权错误当成“没有变化”。
`state.py` 接受归一化快照，不负责联网；成功读取后才写快照。输入示例：

```json
{"repository":"guihousun/Competition_HW","issues":[{"number":1,"author":"guihousun","title":"example","body":"...","state":"open","comments":[{"id":123,"author":"guihousun","body":"...","updated_at":"..."}]}]}
```

```powershell
python workflow/state.py --snapshot .workflow/snapshot.json
```

只以用户正文/评论变化生成新版本，标记 `needs_triage`，不覆盖正在执行的stage。
Codex的自动回复统一以 `<!-- competition-workflow:<issue>:<revision>:<stage> -->` 开头，
发之前先检查同标记是否已存在，避免重复回复和自触发。其他作者的信息可作证据，不能作为新的执行授权。
Issue关闭后停止新任务；正在运行的执行先核实再中止，不重开用户主动关闭的Issue。

无变化或等待用户时保持安静。仅新问题、需要回测、故障、审核/合并结果通知。
网络或dsh不可用时记录状态并在后续周期重试，连续同类失败只报告一次，不无限重复执行/收费。

## 隔离与派单

1. 读取Git状态。当前开发目录可能包含其他Agent未提交工作，不在这里让dsh直接改代码。
2. 根据Issue的被测SHA与目标分支核对版本。未发布的当前代码不得假装存在于main；
   缺少必要基线时等待发布/明确移植方案，不从旧main盲目修复。
3. 为专员维护固定的独立worktree，在其中使用候选分支 `codex/issue-<n>-v<revision>`；切换分支前必须处理完前项改动。
4. 按 `SPEC_TEMPLATE.md` 写文件。与CodeGraph定位结果核对后，生成manifest：

```json
{"specialist":"qa-tooling","worktree":"<absolute isolated worktree>","base_sha":"<40 hex>","workspace_sha256":"<reviewed tree fingerprint>","spec_path":"<absolute spec.md>","spec_sha256":"<sha256>","approved_by":"codex"}
```

5. 启动执行：

```powershell
python workflow/dsh_sessions.py send --manifest .workflow/issue-1/manifest.json --output .workflow/issue-1/run-1 --wait
```

输出目录必须是全新的，避免覆盖证据。保留result.json、answer.md和本地stderr.log；原始stderr和模型过程
不自动贴GitHub。路由器验证工作树指纹、base和Spec哈希；同session继续对话，结束后强制进入review。
Manifest是流程约束，不是签名/操作系统沙箱；更强隔离应另配容器或独立系统账号。

6. Codex比对保护文件、允许改动路径和Git HEAD。dsh越界修改或自行提交即拒绝自动合并，保留现场。
7. 用当前代码实际执行测试，自己阅读结果，不只复述dsh的“测试通过”。
8. 审核后才由Codex提交并推送候选分支，创建或更新PR，写明本地/实机验证的区别。

运行状态保存在本机忽略目录 `.workflow/`，Spec批准摘要、实施与审核结论回写Issue/PR；
可公开的完整Spec放入分支 `specs/issue-<n>.md`，供公司电脑读取。
原始反馈可重建，执行进度不能只放聊天记录。后台run已有process.json时先检查PID及命令行，
确认旧执行已结束才重试；锁文件残留也必须查证，不能按年龄直接并发重启。

## 调度条件与恢复

本机需要开机、Codex应用运行、网络与GitHub连接有效；dsh SDK复用已配置的DeepSeek凭据。
不能承诺关机后的实时监听；恢复后重扫未完成Issue和上次窗口，补处理遗漏变化。
官方说明：[Scheduled tasks](https://learn.chatgpt.com/docs/automations?surface=app)。

停止方式：暂停名为 `Competition_HW Issue 协作` 的自动化。
策略配置在 `policy.json`；只更改配置文件不会自动更新已保存的调度提示词，需同步更新自动化。

## 已验证与仍待发生

已验证GitHub读取、dsh调用；模型/强度握手与最小任务见本地probe结果。
还没有真实内网反馈Issue，因此真实修复→公司回测→合并闭环尚未发生。
测试模板与工作流文件合入默认分支后，GitHub新建Issue页面才会显示模板。
