你这版 Prompt 已经不是“写得不好”的问题了。它其实已经把**任务理解、工具成本、SOP、验证、答案格式**写得比较完整。

你现在真正的问题是：

> **Prompt 同时在要求 LLM“自主规划整个流程”，又要求它“严格遵守流程”。这两个目标天然存在冲突。**

而且你的场景还有一个特殊点：**`executeCmd` 一次调用成本高，所以你反而希望 LLM 有较强的自主规划能力，把多个操作合并到一次命令里。**

所以我不建议你直接把 Agent 改成死板的 `PLAN → EXECUTE → VERIFY`。
对你的场景，更合适的是：

> **固定“控制层”，保留“执行层”的自主性。**

---

# 一、先说你这版 Prompt 最大的问题

你现在实际上让 LLM 同时负责 5 件事情：

```text
理解任务
   ↓
匹配 SOP
   ↓
规划
   ↓
选择工具
   ↓
决定下一状态
   ↓
执行
   ↓
判断是否成功
   ↓
决定是否继续
   ↓
总结 SOP
   ↓
提交
```

也就是说：

```text
LLM = Planner
    + Router
    + Executor
    + Verifier
    + Memory Manager
    + Termination Controller
```

这就是为什么：

> 相同 Prompt，不同 run 会走不同路径。

例如同一个任务：

### Run A

```text
executeCmd:
find xxx
cat xxx
python xxx
```

### Run B

```text
readSandboxFile
↓
executeCmd
↓
python_exec
↓
executeCmd
```

### Run C

```text
executeCmd:
ls
↓
executeCmd:
find
↓
executeCmd:
cat
↓
executeCmd:
python
```

这些都可能是合理路径。

**所以你的目标不应该是让这三次完全一致。**

你的真正目标应该是：

> **允许路径存在一定差异，但禁止出现无效探索、重复探索、无意义 Retry、错误结束和流程漂移。**

这点非常重要。

---

# 二、你的 Prompt 我建议做一个核心改造

把 Agent 分成：

```text
                    Runtime
                       │
        ┌──────────────┼──────────────┐
        ↓              ↓              ↓
    Task State      Tool Policy    Stop Policy
        │              │              │
        └──────────────┼──────────────┘
                       ↓
                      LLM
                       │
                 自主决定下一步
                       │
                       ↓
                  Tool / Answer
```

也就是：

### Runtime 控制

```text
什么时候结束
能不能继续
能不能重复
工具调用次数
当前是否已经得到答案
是否必须验证
SOP 是否需要更新
```

### LLM 控制

```text
具体怎么完成
用什么命令
一次命令里做哪些事情
如何利用当前环境
如何根据错误调整策略
```

这非常适合你的沙盒任务。

---

# 三、你的 Prompt 最应该删除/修改的是这一句

你现在：

> 按照下面的循环执行：
> (1)理解任务
> (2)检查已有 SOP
> (3)确定任务需要的输入、操作、验证方式和最终输出
> (4)检查当前环境中已有的资源
> ...

这看起来很合理，但实际上会导致一个问题：

**LLM 每次都会重新“规划整个 Agent”。**

建议改成：

```text
## Agent运行循环

Agent Runtime 会维护当前任务状态。

你每一回合只需要完成当前状态要求，并根据工具结果决定下一步具体操作。

固定控制流程：

INIT
→ UNDERSTAND
→ EXECUTE
→ VERIFY
→ COMPLETE

其中：

- INIT / UNDERSTAND：由 Runtime 确定
- EXECUTE：允许你自主选择最有效的工具和操作方式
- VERIFY：必须确认任务要求的完成条件
- COMPLETE：只有满足完成条件后才能进入

你不能自行修改上述状态顺序。

但在 EXECUTE 状态中，你可以根据环境和工具返回结果自主决定：
- 下一步执行什么
- 是否需要继续探索
- 如何组合多个 shell 操作
- 如何处理错误
- 是否使用已有 SOP
- 如何减少 executeCmd 调用次数
```

这样就很不一样了。

---

# 四、然后给 Agent 增加一个“当前状态”

你现在 Prompt 里没有真正的 State。

建议每一轮都给 LLM 一个类似：

```text
【当前任务状态】

phase: EXECUTE

goal:
获取任务要求的最终 token

deliverable:
token

known_facts:
- /tmp/task.json 已存在
- task.json 中包含 API 地址
- SOP 中已有 API 调用方式

completed:
- 已读取任务文件

current_objective:
获取 token

verification:
需要确认 token 可以通过判题要求

tool_budget:
executeCmd remaining: 3

last_action:
executeCmd("cat /tmp/task.json")

last_result:
...
```

注意：

> **这部分最好由你的程序动态生成，而不是让 LLM 自己总结。**

---

# 五、你现在最缺的其实是“Known Facts”

这是我特别建议你加的。

你的 Agent 经常会：

```text
已经知道 xxx
↓
下一轮忘了
↓
重新 ls
↓
重新 find
↓
重新 cat
```

所以维护：

```text
【已确认事实】

文件：
- /tmp/task.json
- /app/run.py

环境：
- Python 3.12
- API path = /api/v2/task

接口：
- POST
- parameter = target

任务：
- 最终答案是 token
```

而且每一条事实最好有来源：

```text
[VERIFIED]
/tmp/task.json exists
source: executeCmd#1

[VERIFIED]
API parameter = target
source: executeCmd#2 error response

[DOCUMENT]
API parameter = destination
source: task documentation
```

这样 LLM 就不会把：

```text
文档说的
```

和：

```text
实际上验证过的
```

混在一起。

---

# 六、你的“信息可信度”建议改成硬规则

你现在写的是：

> 实际执行结果 > 实际读到的环境 > 文档描述 > 推测

很好，但还不够。

改成：

```text
## 事实可信度

事实按照以下优先级处理：

VERIFIED_RUNTIME
>
VERIFIED_ENVIRONMENT
>
VERIFIED_SOP
>
DOCUMENT
>
INFERENCE

其中：

VERIFIED_RUNTIME：
当前任务中通过工具实际执行得到的结果。

VERIFIED_ENVIRONMENT：
当前沙盒中实际观察到的文件、命令、接口行为。

VERIFIED_SOP：
历史任务中已经实际验证过的知识。

DOCUMENT：
任务文档或接口文档。

INFERENCE：
模型根据已有信息做出的推断。

禁止把 DOCUMENT 或 INFERENCE 当成 VERIFIED_FACT。

如果高优先级信息与低优先级信息冲突，以高优先级为准。
```

这个会比你现在的自然语言描述稳定很多。

---

# 七、再加一个非常重要的规则：探索必须产生“新信息”

这是解决你 Agent 浪费回合的关键。

加入：

```text
## 探索约束

每一次探索操作都必须有明确的信息获取目的。

在执行工具前，判断：

1. 我需要知道什么？
2. 这个工具调用能否获得这个信息？
3. 这个信息是否已经知道？
4. 是否可以把多个探索操作合并到一次 executeCmd？

如果目标信息已经被 VERIFIED，不得为了重新确认而重复探索。

禁止：
- 重复 ls 已知目录
- 重复 find 已知文件
- 重复读取已经读取过的文件
- 重复调用已经确认结果的接口
- 重复执行不会产生新信息的命令
```

这比单纯说：

> 尽量少调用工具

有效得多。

---

# 八、`executeCmd` 这里我建议你增加一个“规划义务”

你现在已经写了：

> 尽可能完成多个连续操作。

可以进一步强化：

```text
## executeCmd调用策略

executeCmd 是高成本工具。

调用 executeCmd 前，优先判断当前连续操作是否可以一次完成。

例如：

错误方式：

executeCmd("find ...")
→ 等结果
→ executeCmd("cat ...")
→ 等结果
→ executeCmd("python ...")

更优方式：

executeCmd("
  find ...;
  cat ...;
  python ...;
")

但如果后续操作强依赖前一步输出，且无法安全地在同一命令中处理，
才拆成多个 executeCmd。

原则：

能确定的连续操作一次完成；
无法确定的依赖关系才等待下一回合。
```

这会明显改善你的 Agent。

---

# 九、再加一个“反重复机制”

你现在没有明确告诉 Agent：

> **不要连续做本质相同的事情。**

建议增加：

```text
## 重复操作检测

如果上一次工具调用已经回答了某个问题，
下一回合不得再次调用工具获取同一个信息。

如果连续两次操作没有产生新的有效信息：
必须改变策略，而不是重复原操作。

例如：

find 没找到
→ 不应该再次执行完全相同的 find

接口返回 unknown parameter destination
→ 不应该再次发送 destination

测试失败
→ 不应该无修改地再次运行同一个测试
```

这个规则对于 Agent 稳定性非常有帮助。

---

# 十、然后解决“什么时候结束”

你现在：

> 一旦最终答案已经明确获得，不要继续执行没有必要的工具调用。

方向对。

但建议把它变成非常明确的：

```text
## 完成判定

只有同时满足以下条件才能提交：

1. 已获得任务要求的最终交付物；
2. 交付物来自可信来源；
3. 已满足任务明确要求的验证条件；
4. 不存在尚未解决的错误；
5. 不需要额外工具调用才能确认答案正确。

满足后立即提交。

一旦满足完成条件：
禁止继续探索、优化、重新验证或寻找替代方案。
```

最后一句尤其重要：

> **完成以后禁止“手痒”。**

很多 Agent 明明已经拿到答案，却又继续：

```text
再验证一下
↓
再看看
↓
再执行一次
↓
结果被后面的操作搞坏
```

---

# 十一、你的 SOP 其实也需要做一个关键改动

你现在：

```text
检查 SOP
↓
执行
↓
发现新知识
↓
SOP2Prompt
```

很好。

但最好加：

```text
SOP 不是事实源，而是加速器。

使用 SOP 前：
- 如果 SOP 给出了已验证的操作方式，可以直接使用。
- 如果当前环境与 SOP 描述一致，无需重新确认。
- 如果实际结果与 SOP 冲突，立即停止继续依赖该条 SOP。
- 根据实际结果更新 SOP。
```

然后特别增加：

```text
禁止因为 SOP 存在而跳过任务要求的最终验证。
```

否则以后可能出现：

```text
历史 SOP：
这个接口返回 token

↓
Agent 直接相信

↓
当前环境接口已经变化

↓
提交错误
```

---

# 十二、SOP2Prompt 最好不要让主 Agent 自己决定“写什么都行”

你现在这个工具已经不错，但建议规定格式：

```text
SOP必须包含：

【适用场景】
什么情况下使用这条 SOP。

【前置条件】
需要什么已知条件。

【执行流程】
按照什么顺序操作。

【已验证环境知识】
实际确认过的路径、参数、返回结构等。

【验证方式】
如何确认执行成功。

【失效条件】
什么情况下不要继续使用这条 SOP。
```

例如不要：

```text
调用 xxx API 获取 token。
```

而是：

```text
【适用场景】
需要通过本地任务 API 获取任务 token 时。

【前置条件】
任务配置文件中已获得 API 基础路径。

【执行流程】
1. 从配置读取 API 路径。
2. 使用 POST 请求调用任务接口。
3. 参数使用 target。
4. 从响应中的 token 字段提取结果。

【已验证环境知识】
当前环境中该接口实际要求 target 参数。

【验证方式】
确认 HTTP 请求成功，并且响应包含 token。

【失效条件】
如果接口返回 unknown parameter 或路径不存在，
不要继续使用本 SOP，应重新探查接口。
```

这种 SOP 的复用价值高很多。

---

# 十三、我还建议你删掉一句话

你最后：

> 让我们一步步推理，仔细分析问题，确保每个步骤都正确无误。

**建议删掉。**

不是因为它不好，而是它和你的目标：

> 尽可能少工具调用

存在一点冲突。

而且它容易让模型倾向于：

```text
思考 → 再检查 → 再思考 → 再验证
```

你的 Agent 更需要的是：

```text
快速判断
→ 执行
→ 根据真实结果调整
```

所以改成：

```text
基于当前状态和已有证据做最小必要推理。
优先执行能够最大化信息增益或直接推进任务的操作。
```

---

# 十四、如果是我，我会把你的 Prompt 改成这个核心结构

你的原 Prompt 不需要全部推倒。

我会改成：

```text
# ROLE

你是一个高自主性、高效率的任务执行 Agent。

目标：

1. 正确完成当前任务。
2. 使用尽可能少的高成本工具调用。
3. 优先复用已经验证的 SOP。
4. 将新的、经过实际验证且具有复用价值的知识沉淀为 SOP。

你负责：
- 理解任务
- 制定执行策略
- 选择工具
- 组合操作
- 分析工具结果
- 修复错误

Runtime 负责：
- 维护任务状态
- 控制工具调用次数
- 控制是否允许继续
- 控制最终提交
- 防止无限循环

---

# 核心原则

## 1. 实际证据优先

VERIFIED_RUNTIME
>
VERIFIED_ENVIRONMENT
>
VERIFIED_SOP
>
DOCUMENT
>
INFERENCE

禁止将低优先级信息当作高优先级事实。

---

## 2. SOP优先

开始任务时：

1. 检查已有 SOP。
2. 如果 SOP 与当前环境一致，直接使用。
3. 不重复确认已经验证的知识。
4. 如果实际结果与 SOP 冲突，以实际结果为准。
5. 必要时更新 SOP。

---

## 3. 当前状态

每一回合根据 Runtime 提供的 CURRENT STATE 工作。

你不得自行修改 Runtime 状态。

固定流程：

INIT
→ UNDERSTAND
→ EXECUTE
→ VERIFY
→ COMPLETE

其中 EXECUTE 阶段允许自主规划。

---

## 4. EXECUTE阶段

你的目标不是执行固定步骤，而是：

> 用最少的工具调用完成任务。

你可以：

- 自主选择工具
- 合并多个连续 shell 操作
- 根据错误调整策略
- 使用已有 SOP
- 在一次 executeCmd 中完成多个确定操作

但必须避免：

- 重复探索
- 无意义验证
- 重复失败操作
- 已知信息重新获取

---

## 5. 探索原则

每次探索前明确：

- 我要知道什么？
- 这个操作能获得什么？
- 这个信息是否已经知道？
- 能否与后续操作合并？

只有探索能够产生新信息或推进任务时才执行。

---

## 6. 错误处理

工具失败后：

1. 分析错误产生的新信息。
2. 判断原策略是否仍然有效。
3. 如果有效，修正参数后继续。
4. 如果无效，改变策略。
5. 禁止无修改地重复相同失败操作。

---

## 7. 完成判定

只有同时满足：

- 已获得最终交付物；
- 结果来源可信；
- 满足任务验证条件；
- 没有未解决的错误；

才能提交。

一旦满足完成条件：

立即提交，不再进行额外探索。

---

## 8. SOP沉淀

只有新知识同时满足：

- 已实际验证；
- 对未来同类任务有复用价值；
- 当前 SOP 不包含该知识；

才调用 SOP2Prompt。

不要沉淀：
- 当前任务具体答案
- 临时结果
- 一次性参数
- 未验证推测
- 完整探索日志

---

# 当前任务状态

{RUNTIME_STATE}

# 当前任务

{TASK}

# 已有SOP

{SOPS}

# 已确认事实

{KNOWN_FACTS}

# 最近工具结果

{LAST_RESULT}

# 当前允许操作

根据当前状态和工具约束决定。
```

---

# 十五、但是还有一个更关键的地方：**最好别只改 Prompt**

如果你能改 Agent 代码，我建议你同时在代码层增加一个非常轻量的 `AgentState`：

```python
@dataclass
class AgentState:
    phase: str = "INIT"

    task_understood: bool = False
    final_answer: str | None = None

    known_facts: list = field(default_factory=list)

    tool_calls: int = 0
    repeated_actions: int = 0

    last_action: str | None = None
    last_result: str | None = None

    verification_required: bool = True
    verified: bool = False

    max_tool_calls: int = 10
```

然后每次请求 LLM：

```text
CURRENT STATE
phase = EXECUTE
tool_calls = 2
verified = false
known_facts = [...]
last_result = [...]
```

这样你的 LLM 就不需要自己“记住”这些东西。

---

# 十六、最终效果会从“随机 Agent”变成“受约束的自主 Agent”

现在你的模型更像：

```text
             LLM
              │
     ┌────────┼────────┐
     ↓        ↓        ↓
   Plan     Execute   Verify
     ↓        ↓        ↓
   Retry    Finish    SOP
     ↓        ↓        ↓
     └────────┴────────┘
```

所有事情都由 LLM 决定。

改完以后：

```text
                Runtime
                   │
          ┌────────┼────────┐
          ↓        ↓        ↓
       State    Budget    Stop
          │
          ▼
         LLM
          │
     自主执行策略
          │
          ▼
        Tool
          │
          ▼
        Result
          │
          ▼
       Runtime
```

这才是你这个场景最合适的形态。

**一句话概括你这次改造：**

> 不要把 LLM 变得“更听话”，而是让 Runtime 把 LLM 的自由度限制在“如何完成当前步骤”上，而不是让它决定“整个 Agent 应该怎么运行”。

如果你的目标是**尽量少改现有代码**，我下一步会建议直接把你这份 Prompt 改成一份**可直接替换的完整版本**，同时把 `executeCmd / readSandboxFile / python_exec / SOP2Prompt` 四个工具的调用策略嵌进去，而不是只给你框架。
