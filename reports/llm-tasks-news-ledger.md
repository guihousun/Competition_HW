# P3 新闻事件账本：更正关系、范围缺口恢复与一致消费视图（实现说明）

- 类别：Agent 内部架构 / 官方机制实现修复 / UI 工具。规则依据 R01/R02/R06/R07，
  任务书 §4.8/§5.1，接口 §1.6/§1.7/§2，Spec `specs/LLM_TASKS_P3_P4.md`、
  `specs/NEWS_LEDGER_INTERVAL_REVIEW.md`，以及父级 graph-followup 追加意见。
- 基线：`ac0b8cbeca815e2298543852d254d161d1723b94`。官方原文、价格、额度定义未改；
  成交价仍只取当前 `vendorShopList`，未新增观测/权限/动作。
- 未提交、未推送、未重启服务，仅修改本工作树。原失败日志
  `.workflow/llm-tasks/world-memory/news-ledger/interval-counterexamples.log` 未改动。

## 变更文件

| 文件 | 变更 |
| --- | --- |
| `Demo/CoreGeek/src/agent/news_ledger.py` | 纯函数：稳定 ID、Decimal 精确量价绑定、日期一致性、更正措辞、witness。（本轮沿用，未改语义。） |
| `Demo/CoreGeek/src/agent/world_agent.py` | 多次更正的 DAG、统一按日有效性、维度冲突、范围缺口 lostIds/overflow 与恢复、`news_view`、prompt、policy、dump/load 与 v2/v4 迁移。SCHEMA → `competition-world-agent/5`。 |
| `Demo/CoreGeek/src/agent/local_scripted_model.py` | 新闻分支新字段（未变）。 |
| `web/js/experience.js` | 新闻面板改读后端 `news_view`（不再自行判“未解决”），展示有效日/可能冲突/缺口/溢出/来源；观测价仍单列。 |
| `tests/test_news_ledger_graph.py` | 新增 18 项独立边界测试（多更正、链、环、可能冲突、缺口恢复、溢出、prompt 预算、视图一致）。 |
| `tests/test_news_ledger.py` | 更新被父级规则取代的期望（见下）。 |
| `tests/test_frontend_stability.py` | 账本显示与 `news_view` 断言。 |

## API / 字段映射

**持久化记录** `news_events[]`（键集固定，程序赋值，模型自带即判非法）：
`id, resource, availability, startDay, endDay, resumeDay, priceDirection, priceAmount,
priceBasis, evidence, sourceRound, kind, status, resolution, witness`。
- `resolution = {kind: corrected|cancelled, targetId, evidence}`：每条记录至多一条出边；多条入边表示一个事实被多个有界更正分别覆盖。
- `status` 由入边派生：无入边 `active`；有 `corrected` 入边 `corrected`；任一 `cancelled` 入边 `cancelled`。resolver 自身可被后续更正。
- `witness`：`{evidence, resolution}` 快照摘要；保留来源逐字复验，来源被淘汰后只信摘要。

**缺口** `news_gaps[]`：`{resource, startDay, endDay, count, lostIds, overflow}`；
`lostIds ≤ 64`，`overflow=True` 表示恢复元数据不完整、永不自动清除。
顶层 `news_gap_overflow`：不同资源缺口超过 `MAX_NEWS_GAPS=8` 时为真，可见保守溢出。

**派生视图** `world.dump()["news_view"]`（schema `competition-news-view/1`，非模型输入，
load 重算校验）：
```
{schema, facts:[{id, resource, availability, status, kind, inferred, startDay, endDay,
                 resumeDay, priceDirection, priceAmount, priceBasis, partial,
                 effectiveDays:[int]|null, possibleDays:[int], sources:[srcId],
                 resolution:{kind,targetId}|null}],
 conflicts:[{resource, ids, dimensions:[availability|price|direction], days, possibleDays,
             definite, availability, directions, prices, sources}],
 gaps:[...], gapOverflow, unverifiable}
```
- `effectiveDays`：声明日集合减去**所有直接更正/撤回事件的声明区间**（不递归）；未覆盖日期保留。
  不完整端点 → `null`（不产生禁采）。
- `possibleDays`：用已知边界（缺省 1..10）限定“可能重叠”，同样减去直接更正事件的声明区间。

## 更正/冲突/缺口的确定性规则

1. **有界多次更正**：一个旧事实可被多个不同时间范围的更正覆盖；每条新事实至多指向一个目标；
   禁止自指、环、给已绑定的同一来源事实换 `targetId`；重复同一更正幂等合并证据。
   显式关系需真实逐字引文 + 更正/撤回措辞 + 同资源 + 可能日期交集；自动关联同样走 `_bind_resolution`（防环/幂等）。
2. **统一按日有效性**：`effective(事实) = 声明日 − ⋃ 直接更正事件的声明区间`。
   后续更正只编辑该更正自身，**不递归复活祖先事实**（父级补充反例）。
3. **维度冲突**：`availability` 取 `{available,unavailable}`；`price` 仅同 basis 不同金额；`direction` 仅已知方向不同。
   只有 `availability` 的**确定或可能**冲突抑制该日禁采；纯价格/方向冲突保留停矿建议；
   两个都 `unavailable` 的价格 6/7 仍禁采且价格建议未知。
4. **缺口恢复**：淘汰整个更正/冲突组件，组件内所有记录计入 `lostIds`；
   先淘汰再恢复核对；仅当全部 `lostIds` 在保留账本中**逐字复验**后清除缺口；`overflow` 缺口永不清除。
5. **视图一致**：`policy_view`、prompt（`_ledger_view`/`_ledger_context`）、页面（`news_view`）用同一计算；
   只在 `news_view` 内判定，页面不再自行比较历史事件。

## 父级补充反例（本轮修复）

- **不复活祖先**：A 报 D2-D5 停矿；B 更正 A 为 D3-D5 可采；C 又更正 B、仅 D4 未知（或仅改 D4 价格仍可采）。
  修复前递归扣除 B 的 *effective* 集合会让 A 在 D4“复活”。现改为扣除 B 的 *声明* 区间：
  A 有效日仅 `[2]`、可能日 `[2]`；D2 停，D3/D5 可采，D4 以 C 为准（unknown/available），无 A-C 伪冲突；JSON 一致。
- **可能 availability 冲突也抑制禁采**：A 明确 D2-D3 停矿，B 报从 D2 起照常但结束日未知。
  B 无确定日，但与 A 在 D2/D3 有 possible availability 冲突；消费端不再单方禁采，D1 等不可能重叠日期不受影响；
  两个都 unavailable、只有价格矛盾时仍保留停矿建议。

## 验证命令与结果

在 `tests/` 目录运行相关组：

```
python -m unittest test_world_agent test_world_agent_memory test_world_memory test_local_world_news \
  test_news_ledger test_news_ledger_review test_news_ledger_intervals test_news_ledger_graph \
  test_frontend_stability test_long_world_simulator test_agent_demo test_external_agent_simulator \
  test_team_agent test_treasure_contract test_treasure_preparation test_llm_router_integration \
  test_policy_architecture test_evidence_memory
```

结果：`Ran 193 tests ... OK`。其中父级 63 项（49+10+4）全部通过、本轮新增 18 项全部通过、
`test_frontend_stability`（node harness）通过。

独立反例对照（临时把 `_news_effective`/`_news_possible_effective` 换成递归实现）：
两条“不复活祖先”测试均失败，证明它们确实约束递归复活而非迎合实现。

完整 unittest（仓库根，输出写 `%TEMP%\ds-news-ledger-graph-full.log`）：

```
python -m unittest discover -s tests -v
```

结果：`Ran 826 tests in 469.276s` / `OK`，失败与错误 0。

另：`git diff --check`、`compileall` 通过。未做真实 API 试验，未重跑整局 1300 轮。

## 仍未满足 / 已知限制

- 父级补充的“或明确可证的关系解决”未实现：缺口只按 `lostIds` 全部逐字恢复清除；
  其它可证关系（例如丢失的更正边被更新的显式更正等价替代）不会自动清除缺口，仍保守保留。
- `news_gap_overflow` 一旦置位不清除：无法得知被丢弃资源的丢失集合，全部新闻禁采保守撤回，
  直到进程重启/状态重建。官方资源通常很少，触发条件为 >8 个不同资源同时有缺口。
- 更正区间用更正事件自身的声明日；更正事件日期未知时不覆盖任何日期（保守，不猜生效日）。
- 价格仅识别中英文显式量价谓词；含否定词的合法金额（如“不断上涨到6”）保守判未知。
- 来源被提示词窗口淘汰后仅靠 witness，无法对正文再复验逐字引用。
- 上一轮中途格式 `competition-world-agent/3` 不迁移、显式降级；`/2` 与 `/4` 迁移，v2 不补造 witness。
- `priceDirection` 仍只影响展示/建议，**价格的经济策略消费者仍未实现**，是整体 P3 的待完成项，
  不能从 Spec 删除或改称可选。

## Codex 整合后的增量（2026-09-15，尚未发布）

父级工作树基线为1e5d53e；最终增量完整回归 **852项通过，451.028秒，Python3.11.10**。准备冻结为内部候选供场景评估，不代表已发布、经济收益已验证或官方PASS。

以上“未实现消费者”是 DSH 原始交付范围的结论。父级现已接入 `news_economy.sale_signals`、brain 的卖矿出发与出售顺序、前端交易建议。经济投影直接使用统一 news_view，不另建更正关系图。固定场景能实际改变一件铜矿的出发决定，并通过原 sell 指令按当前价格结算；未声称整局收益已提高。

父级补充反例：明确更正为“从第2天开始情况未知，结束日期未知”时，旧降价记录此前仍然触发出售；类似更正会保留旧停矿禁采。两项独立测试先失败。现将旧事实的确定有效日扣除更正的可能范围；旧事实仍保留可能日，不冒充已确定撤回。范围外日期保持有效，页面标出待确认日期。99项新闻相关测试和前端交互通过。该增量后的完整测试正在另行运行。

整合前850项完整测试通过（428.658秒，Python3.11.10），日志保存在本地 `parent-merged-full-tests.log`。这份旧结果不覆盖上述两项新反例修复。价格场景、模型实测、整局消融与真实包验证仍待完成；怪物 Issue19 的新规则尚未合入此树。
