"""Read-only item effects for diagnostics, never price/availability authority.

Official baseline: docs/任务书.md v1.0 (2026-09-09), §4.5.1 / §4.6.
Unknown names stay unknown. Descriptions do not alter policy or settlement.
"""

SOURCE='任务书v1.0(2026-09-09) §4.5.1/§4.6.1/§4.6.3'
EFFECTS={
    'stone':('石头','可按本轮小贩报价卖出换金币；建造一座围墙消耗1个石头，拆墙不返还。'),
    'iron':('铁矿','可按本轮小贩报价卖出换金币；回收价格可随世界新闻变化。'),
    'copper':('铜矿','可按本轮小贩报价卖出换金币；回收价格可随世界新闻变化。'),
    'WeaponUpgradeVoucher1':('武器升级券1','武器Lv1→Lv2并回满血1500；火箭射程10→15、导弹1→2枚；电磁炮射程6→8、穿透能量10→20；加特林射程3→5、子弹1→2发。需在目标周围1格内使用并指定坐标。'),
    'WeaponUpgradeVoucher2':('武器升级券2','武器Lv2→Lv3并回满血2000；火箭射程15→全图、导弹2→3枚；电磁炮射程8→10、穿透能量20→30；加特林射程5→7、子弹2→3发。需在目标周围1格内使用并指定坐标。'),
    'WallUpgradeVoucher1':('围墙升级券1','指定单座墙Lv1→Lv2，血量上限1000→1500并回满血；需在目标周围1格内使用并指定坐标。'),
    'WallUpgradeVoucher2':('围墙升级券2','指定单座墙Lv2→Lv3，血量上限1500→2000并回满血；需在目标周围1格内使用并指定坐标。'),
    'StationUpgradeVoucher1':('基地升级券1','基地Lv1→Lv2，血量上限1500→3000并回满血；需在目标周围1格内使用并指定坐标。'),
    'StationUpgradeVoucher2':('基地升级券2','基地Lv2→Lv3，血量上限3000→4500并回满血；需在目标周围1格内使用并指定坐标。'),
    'WallFixer':('围墙修复包','目标围墙一次回满血；需在墙周围1格内并指定坐标。不是炮台维修包。'),
    'Medicine':('生命药剂','使用者回满血（工人220、开拓者200）；不是复活道具。'),
    'DizzyWeapon':('眩晕法宝','目标坐标为中心3×3内双方机器人眩晕5回合；无使用距离限制；对角色和建筑无效，先于机器人移动结算。'),
    'Bomb':('范围炸弹','目标坐标为中心3×3内双方机器人各受100伤害；无使用距离限制；对角色和建筑无效，先于机器人移动结算。'),
    'SmallRobotSummonOrder':('小型机器人召唤令','对方下个夜晚小型机器人+1；所有机器人召唤令合计每天最多使用10张。'),
    'MiddleRobotSummonOrder':('中型机器人召唤令','对方下个夜晚中型机器人+1；所有机器人召唤令合计每天最多使用10张。'),
    'LargeRobotSummonOrder':('大型机器人召唤令','对方下个夜晚大型机器人+1；所有机器人召唤令合计每天最多使用10张。'),
    'BossRobotSummonOrder':('BOSS召唤令','对方下个夜晚BOSS机器人+1；所有机器人召唤令合计每天最多使用10张。'),
}
TASK_ITEMS={'AcientTablet':'古符石板','StarSand':'星辰之沙','FlameBreath':'烈焰之息',
            'FrostPotion':'寒霜药剂','ThornAmulet':'荆棘护符','IronWhistle':'回音铁哨'}


def describe(name):
    if name in EFFECTS:
        label,effect=EFFECTS[name]
    elif name in TASK_ITEMS:
        label=TASK_ITEMS[name]
        effect='任务用品，只可用于任务，召唤宝藏后消耗；具体组合与条件以当局任务/传闻为准，不推断额外战斗效果。'
    else:
        return {'effect_status':'unknown','effect':'效果待确认；官方基线未登记此名称。'}
    return {'label':label,'effect_status':'official_baseline','effect':effect}
