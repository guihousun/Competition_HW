# 真实模型驱动的任务控制器组件评测

**范围：TaskAgent + OpenRouter模型 + 本地虚拟沙盒。尚未接入实际角色/共享router/整局调度，不是P1-P5整体完成，也不是内网或官方PASS。**

类别：策略内部架构验证 / 本地适配 / 模拟工具修复；依据R01/R07、任务书§5.3、接口§1.1/§1.7/§2.1。

## 固定试验

模型：OpenRouter deepseek/deepseek-v4.1-flash；配置max映射API xhigh。每组3次，9/9通过。
每次通过必须同时满足真实模型发出文档读取、成功参数查询、正确答案三项；不能仅猜中答案。

| 题组 | 结果 | 模型调用 | 验证重点 |
|---|---|---|---|
| weather-doc-v1-beijing | 3/3 | 5、6、7 | 发现目录、读取接口说明、查询北京、按JSON类型提交 |
| weather-doc-v2-shanghai | 3/3 | 5、6、6 | 不同文档路径、不同程序路径与参数名、上海数据和字段顺序变化 |
| decimal-aggregation | 3/3 | 5、6、6 | 读取账目接口、过滤A区、十进制汇总、精确输出0.3 |

合计52次调用、82001 token，未超过本次明确的54次评测预算。使用Python3.11.14作为本地评测驱动；参赛代码另在Python3.11.10上通过553项冻结源码单测。组件文件在真实试验前后哈希相同；完整版本与逐次结果见report.json，逐次输入/模型输出/工具结果见同目录题名JSON。

这些是真实API调用，不是脚本化答案。题目、工具文件和数据是本地合成fixture；正确答案独立列举在评测端，不进入模型输入。模型看到的只有题目、已读取文档/查询结果与操作反馈。评测程序没有执行任意宿主Shell或Python代码。

## 保留失败与修复过程

第一次试验的首题失败：虚拟cat把一个实际存在的目录报告为不存在，导致模型反复寻找该路径。保留failed-directory-v1.json。随后停止了该次评测进程以免继续消耗调用；第二个未完成trial的调用/计费未知，见interrupted-v1.json，不虚构总用量。

修复为准确区分文件、目录和不存在路径；空工作目录可列举；虚拟程序源码未提供时明确说明。没有改变原题、数据、正确答案或TaskAgent来让该题变简单。修复后先做一次单题真实试验：5次调用、7706 token通过，见pilot-report.json，再做上述完整9次试验。另有连通性试验1次、193 token，见smoke.json。历史失败不纳入新版本9/9的分母，但原样保留，不能隐去。

## 回归与限制

Python3.11.10全套553 tests PASS，用时264.791秒，执行前后源码和测试指纹一致；见validation.json和full-suite-summary.txt。页面改为读取实际模型标识，不再写死旧deepseek-flash；两个JS文件node --check通过。

尚需共享router与实际TaskPipeline接入、SOP方法复用、新闻/宝藏模型推理及跨日记忆、实际角色互斥、队伍额度、HTTP时延与整局消融。此处的rounds是组件评测的操作序列，不是完整官方游戏的完成回合数。参赛入口仍只提交prompt/executeCmd给平台，外部API只属于显式本地测试。

本地默认API已迁移OpenRouter；密钥从OPENROUTER_API_KEY或当前Windows用户的openrouter-key.dpapi读取，不回退到DeepSeek官方渠道密钥。配置说明见docs/LOCAL_LLM.md。数据文件不包含API密钥或模型隐藏推理字段。
