# Maintenance-trip-v2 独立接线预审

2026-09-17。只读共享工作树brain/planner及对应模块接口；不改源码、不跑全量、比赛或native。不把主线程已通过的11项集成测试冒充本审查独立运行结果。

**此前已持券抢占问题已在相同场景复核修复；当前接线预审没有遗留阻断项。** 这是限定范围的代码/单帧规划结论，不是整体策略收益或官方PASS。

## 原发现与同条件复核

原发现：新维护位于upgrade_itinerary之前，只保护已有purchase槽；无memory但工人已经持有可交付券时，会被重新派去买WallFixer，延迟已付物资交付。

复用此前完全相同的greedy r415合成变体：保留损墙40000，移除其余墙和墙zone以明确打开路线；10012持WeaponUpgradeVoucher1；当前现金仍10；phaseTask/playerTasks清空；PlannerState无purchase承诺。完整原始来源、修改清单及精确输入在review.json；这是合成边界，不伪称DSH原来就在此帧持券。

- 未改预期：10012应move至(23,20)，交付目标10030，完整剩余动作 **16**。
- 直接upgrade_itinerary仍返回上述16动作配送。
- 当前真实`brain.plan_for_state`最终也返回同一move、`return_with_voucher`及16动作；purchase登记WeaponUpgradeVoucher1/目标10030，未登记repair或购买WallFixer。
- 直接探针deadline=68预算44，集成既有截止预算43；距离/动作预期没有改动，集成余量27仍可行。
- 新接线只优先可行`use/return_with_voucher`，并缓存原升级规划结果；尚未购券的新采购不因此固定压过维护。

## 其它接线审阅范围

- 夜间owned_roles贯穿原炮位循环、共享火箭协调和最终补炮excluded；最终reconcile后再finalize，掉落的新提案不登记租约，旧租约掉落转返程并移除推测use标记。
- 任务cycle（含已接待题）、公开phaseTask、宝藏staging和pioneer_safety先进入可用性排除；临时保护占位只在内部protected，不进入正式commands。
- 日间复用唯一purchase槽与RouteGuard/final dispatch，不把影子消费当库存回执；夜间模块无buy入口。ON预览使用副本，白天/新局清理维修租约；默认OFF空状态不新增租约dump字段。
- 尚应由冻结后的相关/全量验证覆盖并发边界、协议输出、真实返岗全过程；本次没有重复主线程测试。

策略限制需单独标注：一名工人维修、≤70%入口、三炮后补给、一包上限、必须返炮位均不是新增官方规则。此前mixed r767为专职hero墙边维修、主炮手继续攻击；不能将这类角色“不返炮”一律视为错误。本候选针对承担炮位的工人采用完整返岗闭环。

review.json保存本次五个相关源码文件SHA256、精确请求、直接/最终计划及持久承诺，用于对应未提交工作区的审核断面。
