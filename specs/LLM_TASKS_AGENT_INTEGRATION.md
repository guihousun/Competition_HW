# 三类任务的Agent集成接缝

状态：2026-09-15实施设计，组件已有独立证据，完整集成尚未完成。
类别：内部架构/策略优化；R01、R07及LLM_TASKS_ROADMAP.md保持不变。

## 队伍与任务

PlannerState持有一个TeamAgent。它拥有TaskAgent控制器、经过核验的SkillLibrary，以及独立的news/treasure主题记忆；预算和在途通道仍唯一归属LLMRouter/JudgeState。角色继续使用现有确定性调度、统一预算预留和落点协调，不创建每角色3次额度。

TaskPipeline先检查领取、范围、生死、超时和任务原文，再调用求解器。keyword-fill维持priority10；新Agent接入priority40，早于llm-ask的50。新Agent待模型、待工具、待判题或已停止时返回显式hold，不能落入旧求解器用未经关联的回复猜答案。

## 提案到回执

1. solve收到已确认的当前generation，构建有界上下文和证据索引，调用TaskAgent.decide。控制器只提出prompt、cmd、submit，不执行网络或宿主命令。
2. prompt/cmd进入共享router队列，保存request_id到控制器token/generation的映射。prompt的JSON request_id必须等于token。命令使用官方executeCmd，若没有实际nonce回显，只登记单在途及回合关联，不能称为强ID验证。
3. 公共仲裁实际发出后才调用acknowledge。排队、预览、被优先级替换、预算拒绝均不计操作次数。submit必须出现在协调后的开拓者动作中才确认提交。
4. 仅消费归属该request的received/expired/obsolete记录。模型回执传给receive；过期/拒绝传给failed。工具原文与退出状态更新当前generation证据；不能用4000字符预览冒充完整输出。不可恢复的截断须显式说明，并允许通过合法查询缩小结果。
5. 判题错误2/4进入纠错；phaseTask结束、死亡或离开范围终止当前控制器。控制器软限额耗尽仍在官方任务中时保持明确停止状态，不能清空cycle重领同一题以刷新软限额。

## 上下文与方法

组装prompt的总长度应共享同一预算，覆盖系统说明、证据ID、当前题目、工具结果和历史摘要。原文、答案格式、已验证结果和推断分开；任何裁剪记录丢失范围与真实可用来源，不发明磁盘trace引用。

方法库仅接受关联过的成功文档读取与查询。不存历史参数值和答案；同接口换参数仍执行新查询，文档指纹变化先使旧方法失效。方法提示可省去路径发现，但不能免除数据核验。

## 验证顺序

1. 实际brain/PlannerState JSON循环：模型计划→executeCmd→真实虚拟工具回执→答案→判题反馈；覆盖错误后修正、格式变化、软限额、迟到和两队交错。
2. 默认关键词题无需LLM；动态路径题在Agent开启时完整完成，关闭时维持基线，不硬编码fixture答案。
3. 新闻和传闻主题用独立上下文提出普通请求；跨日保存只来自公开新闻，经济动作读取最新vendorShopList，宝藏执行经过位置/时间/实际背包检查。
4. 同一组冻结地图、双方与留出种子比较基线/Agent；记录基地、分数、任务/宝藏结果、调用次数、超时及无进展行为。整局通过不能由组件测试或真实模型小样本替代。
5. 最终再做Python3.11.10与参赛包验证、UI实际观察，将已审核候选发布codex/sgh。公开结论只覆盖本地模拟，内网仍待真实证据。
