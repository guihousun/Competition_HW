# Issue27：混合防守与升级采购回测

包内源码：`32b1c609e94b70ce87f6c30020c89fa587f9294e`。公司仍使用固定 `codex/sgh`，git pull后上传根目录CoreGeek.tar.gz。

包SHA256：`37708c1630761ac8c9b369c4cd24e176776122e3f72b2c752c103971de0aa2ce`。

## 这轮行为

- 默认2座火箭发射台（rocket）+1座电磁狙击炮（railgun），不是改武器数值。
- 白天根据当前武器商店位置与价格，派一个工人购买建筑升级券、返回目标并使用；另一工人留在炮位附近。背包实际出现券才按已购入处理，服从团队预算、容量、路径与回防时间。
- 受损基地优先；通常先提升一级武器、再基地，墙后置。没有合法目标、买不起、商店未观测到或往返时间不足时不会硬买。
- 夜间空闲的相邻操控者可以补上就绪但无人开火的武器，不绑定在冷却炮台上空等。不会覆盖用道具、任务等已占用动作。炮台必须有射程内目标、冷却就绪且操控者相邻。
- 识别题目中的“提交形式”等Markdown章节，拒绝用裸数字代替明确要求的对象。独立且能确定绝对路径的check调用使用现有CRLF兼容工具；包含其他修改命令的复杂shell不自动改写。

## 小贩与商店不能凭图标混用

官方 `vendor` 是卖石头/铁/铜换金币的位置，`weaponShop` 才是购买升级券和消耗品的位置。实际地图是否两者都有，以mapInfo.zones为准；只有vendor时本候选不会猜测其兼有商店权限。

用户已在2026-09-16确认当前实机地图的武器商店位于小贩右上侧。因此当前问题应继续核验采购流程，而非归因商店不存在；代码仍按观测坐标寻路，不硬编码这张图的相对位置。

新控制台 `task_event` 的 `upgrade_itinerary` 行记录商店/小贩数量、可用金币与购物状态。重点看：

| reason/phase | 含义 |
|---|---|
| weapon_shop_not_observed | 本轮地图没有weaponShop；不能据此断言全地图永久没有商店 |
| no_shop_prices | 没收到可用商品价格 |
| no_affordable_upgrade | 没有买得起且适合当前等级的升级券 |
| to_shop / buy / return_with_voucher / use | 前往购买、购买、带券返回、使用 |
| trip_unreachable_full_or_too_late | 路径、背包或剩余白天时间不足 |

`weapon_readiness` 记录每座武器的 `attack_issued/cooldown/no_target_in_range/no_controller_in_range/controller_claimed_or_unassigned`。它解释本轮是否发出攻击，不等于平台已执行伤害。两类记录只在状态变化时打印。

## 判题和金币记录

官方金币字段是 `teamOur.goldNum`，输出摘要继续用 `stats_delta.gold`。上一版误读gold导致null，本版修正；不得拿旧null当零金币收益。`official_success_confirmed=false` 一直表示未确认，不是19次全部判错；只有明确的错误回执单独记录为错误。

HTTP查询的真实分页/数据聚合正确性仍需实机检验。没有足够原始回执时不能把服务空结果当正确答案，也不保证本版已解决所有API题。

## 需要观察

1. identity源码SHA是否对应本候选，两侧建成的阵容是否2R+1E。
2. 金币是否按goldNum正确显示；`upgrade_itinerary` 是否发现商店，工人有无完成buy→背包出现券→use→建筑升级的链路。
3. 工人不攻击时，武器是否冷却/无目标/不相邻；若reason仍不合理，保留同轮角色位置、武器位置、cooldown、机器人和已发动作。
4. 原题“提交形式”章节是否形成完整契约，独立check是否不再报CRLF启动错误。

当前夜间维修仍以清场后维护为主，尚未新增交战中按低/高血量阈值来回调度的机动维修工人。局部配对测试不是官方胜率，本地模拟的地图/机器人/任务假设仍存在。
