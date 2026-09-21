# 当前策略与微调入口

唯一可编辑配置是仓库根目录 `strategy.json`。采用 JSON 是为了让公司 Python 3.11 环境无需安装 PyYAML；JSON 也是 YAML 1.2 的子集，但本项目加载器只接受 JSON 语法。不要加注释、尾逗号或创建另一份 YAML 配置。

## 当前采用的方案

- 三座火炮（`rocket / rocket / rocket`），优先寻找一个工人不用移动就能覆盖三座炮的共同操控位；每人每轮仍只能操作一座炮，利用冷却轮换。白天通过基地后侧开口安排入位；实际地图找不到可达的共同操控位时采用安全回退，不把非法站位当作已完成一人三炮。
- 前三天允许其他角色在安全且能及时回防时发展经济或做任务。
- 第四天起**夜间**全员参加防守；白天继续采矿、买卖、任务与建设，不是第四天起全天停工。
- 前三天武器目标分别为 `111 → 122 → 222`，后续默认维持 `222`。这是建设目标，不保证金币、商店或路径条件一定允许当天完成。
- 后期为城墙准备维修物品，低血量时修复，达到恢复阈值后释放维修角色；夜间清场后允许为白天准备。

布局必须由当前己方基地和合法建造区求出，不能把蓝方坐标当成红方坐标。上述内容是团队策略，不改变官方价格、伤害、射程、动作额度、视野、昼夜长度或计分。

## 参数表

| 字段 | 默认 | 含义与调整影响 |
|---|---:|---|
| `name` | `user_phase_v1` | 本次参数方案名；微调时改成能辨识的名称。 |
| `enabled` | `true` | 本方案总开关；关闭用于对照旧策略，不表示机器人停止行动。 |
| `defense.full_defense_from_day` | `4` | 全员夜防起始日，范围 1–10；不限制白天职业活动。 |
| `defense.single_operator_three_rockets` | `true` | 尽量让一个操控者轮换三火炮。关闭可对照原炮手分配机制。 |
| `defense.tower_loadout` | 三个 `rocket` | 三座武器类型；单人三火炮开启时必须全部为 `rocket`。 |
| `upgrades.day_targets` | `[[1,1,1],[1,2,2],[2,2,2]]` | 第 1、2、3 天的三座炮目标等级。按当前观测到的存活武器 `(y, x, id)` 升序对应，不按传入列表顺序；可把第二天改为 `[1,2,1]` 测试更省钱方案。 |
| `upgrades.late_weapon_target` | `[2,2,2]` | 第四天后的目标。提高到三级会与维修、经济争用时间和金币。 |
| `maintenance.from_day` | `4` | 启用后期维修准备的起始日；白天可备货，不独占所有工人。 |
| `maintenance.stock_target` | `2` | 维修物品储备目标，0–10；目标不是强制购买或无限预算。 |
| `maintenance.entry_fraction` | `0.55` | 城墙血量比例低到该值时考虑维修。提高会更早离开普通岗位。 |
| `maintenance.exit_fraction` | `0.85` | 修到该比例后退出维修，避免每轮往返切换。 |
| `maintenance.emergency_fraction` | `0.25` | 城墙紧急血量阈值。 |
| `maintenance.base_emergency_fraction` | `0.40` | 基地紧急血量阈值。 |
| `economy.return_day_index` | `55` | 白天回防时机，以白天内 0–69 轮为刻度；调小更早回防。 |
| `economy.upgrade_return_margin` | `3` | 升级采购往返所留的安全余量轮数。 |
| `economy.stone_batch` | `10` | 单次石头采集批量目标，1–100；不是修改背包容量。 |
| `economy.metal_batch` | `10` | 单次金银铜采集批量目标，1–100；需遵守实际矿量与容量。 |
| `economy.ore_max_distance` | `8` | 采矿候选距离上限，1–40；扩大可能增加赶路损耗。 |
| `nightwork.allow_after_clear` | `true` | 夜间已确认清场后允许工作；不能把当前无目标误当作未来绝无刷怪。 |

升级目标必须在 1–3 级内，按槽位随日期不下降；维修阈值必须满足 `emergency_fraction <= entry_fraction < exit_fraction`。未知字段、漏字段、非法类型和重复 JSON 键均会报错，不会悄悄忽略。

炮塔被毁或重建后，当前武器排序可能变化，三个等级数字不是永久绑定到某个武器 ID。等级上限限制新增升级采购；已携带的合法升级券仍允许使用，不会为了守住目标数字丢弃已有资源。

## 怎么修改和确认生效

1. 修改根目录 `strategy.json`，每次只调整一组相关参数，保留原文件。
2. 运行 `python tools/validate_strategy.py`。必须显示 `valid: true`；输出包含实际解析后的全部参数、文件路径和 SHA256。
3. 本地重启服务。服务启动时读取并缓存配置，运行中改文件不会热更新。可用 `COMPETITION_HW_STRATEGY_FILE` 指向另一份本机 JSON 做对照；显式指定的路径不存在会失败。
4. 公司平台需要重新构建并上传 `CoreGeek.tar.gz`，仅修改电脑上仓库的 JSON 不会改变已经上传的包。包内根目录包含同一份 `strategy.json`。
5. 记录包内源码 SHA、策略名称与策略文件 SHA256。配置加载身份中 `loaded: true, source: file` 表示读取了文件；`source: fallback/no_file` 表示文件缺失而使用内置默认，不应误当作已读取个人微调配置。

在仓库根目录完成修改与验证后，可用以下 PowerShell 命令制作自己的参数包。先提交配置，再从确定的提交构建；不能直接拿旧压缩包上传：

```powershell
python tools/validate_strategy.py
git add -- strategy.json
git commit -m "Tune user phase strategy parameters"
$strategySource = git rev-parse HEAD
git status --short
python tools/build_competition.py --repo . --ref $strategySource --output "dist/strategy-$strategySource/CoreGeek.tar.gz"
```

构建前工作树应干净；存在其他未提交文件时先核实和妥善保存，不要为打包盲目删除或全量暂存。上传此次输出目录中的 `CoreGeek.tar.gz`，同目录的 `.sha256` 和 `submission-manifest.json` 用于核对。输出路径包含源码 SHA，防止把不同参数版本的包混在一起。上述命令构建本地文件，不会自动向 GitHub 推送或替换平台已部署包。

对照测试同时看存活轮数、基地被攻破次数、真实游戏积分、实际升级完成回合、维修消耗、角色死亡和非法动作。赛事胜负、对战双方身份与游戏积分分开核实，不能只挑高分一场来决定参数。地图与怪物模拟假设没有因此成为官方认证。
