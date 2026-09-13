"""全局可调阈值。

设计见 docs/design/code-design.md §3 / §12。
这里只放**不可变常量**；锚定校验结果、观测到的 attackRange、波次拟合系数属于
**习得参数**，落在 infra/state.py 的持久化 KV 里，启动时覆盖默认值。
"""

from __future__ import annotations

# ── 日历（✅ 策略稿 §8，已被样例 roundNo=85 为夜交叉验证） ──────────────
ROUNDS_PER_DAY = 130
DAY_ROUNDS = 70
NIGHT_ROUNDS = ROUNDS_PER_DAY - DAY_ROUNDS  # 60
HALF_ROUNDS = ROUNDS_PER_DAY * 10  # 1300

# ── 红线 A ───────────────────────────────────────────────────────────
MAX_TEAM_ERRORS = 5  # 单队累计 5 次异常 → 后续不再调度

# ── 几何（✅ demo 实测：基地 2×2 / 武器环 4×4 / 围墙环 6×6 = 20 格） ──
BASE_SPAN = 2
WEAPON_RING_SPAN = 4
WALL_RING_SPAN = 6
WALL_CELL_COUNT = WALL_RING_SPAN * WALL_RING_SPAN - WEAPON_RING_SPAN * WEAPON_RING_SPAN  # 20
WEAPON_SLOT_COUNT = WEAPON_RING_SPAN * WEAPON_RING_SPAN - BASE_SPAN * BASE_SPAN  # 12

# ── 建造与经济（策略稿 §4.3 / §12.1） ────────────────────────────────
WEAPON_BUILD_COST = 25
#: ✅任务书 L205：「武器工事全局同时最多建造 3 座」，而样例开局**已放好 3 座**
#: → `WEAPON_BUILD_COST` 这条支出线在武器被打掉前是死的。采购阶梯必须
#: 让位给升级券（见 domain/economy.purchase_wish 的注释）。
MAX_WEAPONS = 3
WALL_MATERIAL = "stone"
ORE_KINDS = ("stone", "iron", "copper")
STONE_BUFFER = 3  # stone_target = 缺口面数 + STONE_BUFFER

# ── 采购阶梯的参数（domain/economy.py 用） ───────────────────────────
WALLFIXER_PRICE = 10  # 任务书 §4.6.3 消耗品表
WALLFIXER_STOCK = 1  # 背包里常态保留几件修复包
WALLFIXER_RESERVE_GOLD = 30  # 买了修复包之后至少要剩这么多金，否则推迟升级券太亏
STATION_HURT_RATIO = 0.8  # 基地血量低于满血的这个比例 → 视为"被打过"，优先加固

# ── 采集与贩卖 ───────────────────────────────────────────────────────
SELL_BACKPACK_RATIO = 0.85  # 背包占用超过这个比例就去小贩处清仓
WORKER_STONE_TARGET = 4  # 工人随身带多少石头就够建墙（不够时优先去挖石头）
#: 选矿时的"预期开采回合数"：`价值 × 它 − 距离` 才是正确的比较尺度。
#: 取值不必精确 —— 它只需要保证**价值差压得过距离差**，让工人去挖铜而不是石头。
MINE_HORIZON = 20

# ── 日程（策略稿 §8） ────────────────────────────────────────────────
#: ⚠️ 策略稿 §8 的 `within ≥ 52 一律回防` **已被实测否决**（见 planner._should_retreat）：
#: 它每天砍掉 18 个白天回合、10 天共 180 回合（26% 的白天），而真正要防的
#: "从最远矿区回不来"只有算路才知道。改为按**实际 BFS 步数**判断。
RETREAT_TAIL = 5  # 白天最后 5 回合无条件往门口收
RETREAT_MARGIN = 2  # 路长余量：中途被挡/绕行时的缓冲
DOOR_CLOSE_WITHIN = (64, 70)  # 白天末尾补门窗口
TASK_MIN_ROUNDS_BACK_AND_FORTH = 3  # 余下白天回合少于这个数就不接新任务

# ── 沙盒（接口文档 §2.1） ────────────────────────────────────────────
SANDBOX_TIMEOUT_S = 15  # 判题器侧上限，我们留余量
SANDBOX_CMD_TIMEOUT_S = 12
SANDBOX_OUTPUT_LIMIT = 64 * 1024
SANDBOX_SKIP_IF_REMAINING_LE = 2  # 剩余回合 <= 2 时不再发 executeCmd（竞态 #3）

# ── LLM 配额（接口文档 §1.7） ────────────────────────────────────────
LLM_DAILY_QUOTA = 3  # 每个游戏日 3 次；任务执行期间不受限且不计次

# ── 武器（接口文档 / 任务书 §4.5.4） ─────────────────────────────────
# ⚠️ 仅作 fallback：payload 的 attackRange > 0 时**一律以 payload 为准**（✅实测矛盾）
TOWER_RANGE_BY_LEVEL: dict[str, tuple[int, ...]] = {
    "gatling": (3, 5, 7),
    "railgun": (6, 8, 10),
    "rocket": (10, 15, 10**9),  # 射程无限
}
# 落点个数：电磁狙击炮恒为 1；加特林/火箭 = 当前等级
MULTI_TARGET_WEAPONS = ("gatling", "rocket")
MAX_TARGETS = 3

ROBOT_KINDS = ("smallRobot", "middleRobot", "largeRobot", "bossRobot")

# ── 运输层 ───────────────────────────────────────────────────────────
RESPONSE_DEADLINE_S = 5.0  # 接口文档：5 秒内必须响应
DECIDE_BUDGET_S = 3.0  # decide() 自身的软预算，留 2 秒给序列化与网络
LOCK_TIMEOUT_S = 3.0

# 回合号"倒退"多少才认定是**新一场**（下半场 roundNo 是否重置未确认，见 app.MatchState）。
# 取 65：明显大于任何"迟到的重复请求"的倒退幅度，又远小于半场长度 1300。
MATCH_RESET_GAP = 65

HTTP_MAX_BODY = 4 * 1024 * 1024
SOCKET_TIMEOUT_S = 10.0  # 接口文档：建立连接过程超过 10 秒判超时

# ── 日志 ─────────────────────────────────────────────────────────────
# 密钥**内置在代码里**（需求阶段选定）。这是"日志混淆"，不是安全边界：
# 只防顺手翻看，不防拿到代码的人。详见 code-design.md §10.4。
LOG_ENCRYPT = True
LOG_PASSPHRASE = "coregeek.futurewar.v1.local-log-only"
LOG_ITERS = 20_000
LOG_DIR = "logs"
LOG_MAX_BYTES = 8 * 1024 * 1024
LOG_QUEUE_SIZE = 2048
LOG_STDOUT_SUMMARY = True  # 每回合一行明文摘要
