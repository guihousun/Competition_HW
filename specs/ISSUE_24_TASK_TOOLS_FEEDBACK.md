# Issue #24 — API恢复、check执行与提交证据

- 输入digest：c910e60f0d255876997a75fab3718307d0ae8fa4b642467b33cfaafe7ba3eba3。
- [来源](https://github.com/guihousun/Competition_HW/issues/24)，全部评论0条；被测66a4d9a6c704aa0425d995cff29ab812ba6cc98e，开发基线df5eb5a4def09263df575d03c6fc62a2e052d5ae。
- 类别：Agent工程/策略、诊断、本地模拟；规则R01/R07/R08，接口§1.1/§1.7/§2，任务书§五–八。
- 执行Codex；DSH原生Key额度失败不重派。批准2026-09-15。

## 已知与不确定

当前GitHub main/codex/sgh未找到Issue所列teamA.log/teamB.log，指定路径读取404；已询问分支，不阻断基于摘录独立复现。工程题3次check成功并提交是用户记录；没有原始判题成功字段时不能宣称三次全部通过或失败。task_text_ended原先总写unknown，是本地诊断，不是官方回执。75→80也可能包含初始造塔支出，不能据此排除alpha奖励。API请求“风格”相同不代表实际URL字节/参数/鉴权完全相同，不能直接认定服务随机。memoryReads仅为inspect次数，不能据0判断全部上下文丢失。

## 实现

1. 新内部http计划，以结构化URL/headers/params构建标准库请求，自动URL编码。只GET；禁自动跳转，响应按实际JSON类型描述，不对dict使用[0]。限定请求次数、总时长、输出预算；不修改官方响应协议。
2. 在同一端点内仅根据实际401明确Bearer提示和400明确缺参数提示作有界恢复：从当前请求已提供的API key构造Bearer；只有唯一业务参数时才将其移到服务明确要求的新参数名。不能猜密钥、枚举端点、固定city/location、把200空结果当正确答案。保留每次尝试的状态和实际使用的参数名，值/鉴权秘密不进入跨任务方法。
3. 同队方法记忆绑定公开文档路径+全文hash+端点，只有关联真实成功HTTP回执才能候选存储；后题重新读取相同文档后才显示传输方法提示，服务错误优先于旧提示。不存城市、响应记录、一次性token或API key。
4. 新内部check计划按指定工作区路径读取小型脚本，将CRLF仅在内存归一化，依据明确shell/python解释器执行；保持cwd、超时和退出码，不修改check原件或验证服务。未知解释器/二进制/链接等明确拒绝，常规run仍可用。任务真实代码只在平台沙盒运行。
5. 结束日志保留“未确认”结论，同时附最近提交回合/答案hash、连续观测的金币/分数变化、原始errors与动作回执、观测缺口；不得将奖励变化或动作成功当判题PASS。agent_state补充方法数与原文来源数，避免把inspect=0误读为无记忆。

允许文件：agent/task_agent.py、team_agent.py、task_journal.py、新task_tools.py、相关tests/tools和Spec/reports。必要虚拟沙盒适配只扩展明确fixture指令，严禁本机执行任意比赛命令。官方文档/样例、其他DSH工作树禁止修改。

## 验收与发布

- Linux隔离fixture：Unicode查询编码；401→400→200恢复；列表/对象/按ID映射/空数据/非JSON；重定向不传凭据；超时/输出截断有界；无明确提示不猜测重试。
- 文档hash改变/换队/损坏存档不复用方法；同题不同城市只复用参数名/鉴权机制，不带旧值；没有关联成功回执不能晋升。
- CRLF shell check和Python check、非零退出、超时、原文件hash不变；真实check输出作为证据，不从源码找token。
- task消失但无反馈、动作成功、error2、观测间隔、同期买卖支出均不能产生伪PASS；提交关联和差值准确。
- 全量unittest、两侧/留出fixture、diff --check、实际tar.gz HTTP/startup验证；冻结源码后构建，再交付根目录三个包文件到codex/sgh。
- 持续回写Issue，保持open，真实模型解题率与内网同SHA结果另行记录。
