# 双炮操控与战斗显示交付验证

源码：`3632d37793c1ced04c77992e7a161d5830bb122a`。分类：策略优化 / 本地假设 / UI 工具。
Spec：`specs/SHARED_GUNS_AND_VISUALS.md`；说明：`docs/COMBAT_VISUALS.md`。
规则依据 R01/R02/R04/R05/R06 与用户补充 S06；原始任务书、接口、样例及官方 Demo 保持原样。

## 变更

- 新建双火箭优先共享墙内操控位，保留内圈连通；一工人轮换火箭，另一工人操作电磁炮。
  每角色每轮一个动作，火箭仍有 3 轮冷却，白天准备炮位不发射。已有不兼容布局不强制拆建。
- 模拟机器人主要向基地推进，只与附近角色交战；3 格交战半径为本地选择，伤害/射程未降。
- 开拓者避开可见、无遮挡的近敌射程；不读取未来波次，不保证绝对存活。
- 武器与墙三级外观、实际等级角标、升级反馈；火箭短尾焰/爆炸与电磁光束区分。
  攻击指令不再叠加假激光，射击/伤害只读取帧记录，缓存版本统一为 `20260916-combat`。

## 验证证据

- `full-suite.log` / `validation.json`：Linux Python **3.11.10** 完整 unittest 回归，**993 项全部通过，372.975 秒**。
- `cases.json` / `cases.log`：当前源码 seed 1、90601 左右换边，各 260 轮；全部基地 HP 1500，
  非法动作与执行失败为零。本地分数分别 573 / 1209 / 1017 / 873。
- `package-check.json`：真实解包启动后的 10 次根路径 POST，覆盖左右换边、回防、维修、
  清场及第四夜；Windows Python **3.11.10**，最慢响应 46.815 ms。
- `visual-check.txt`：`node tools/check_combat_visuals.cjs`，覆盖真实 level、倒放不伪造升级、
  不同武器效果、火箭到达后爆炸、伤害记录、无假激光线和粒子数量上限。
- 浏览器：无前端错误；使用独立单帧展示数据目视核对火箭/电磁炮/墙 1–3 级；此数据仅用于
  外观检查，不是合法对局或官方样例。实际 seed 1 模拟到第 319 回合，0 执行失败，暂停、
  跳转、基地近景可用。浏览器验证基于 f4919cc，后续只加白天开火禁令，UI 文件未变。
- `pioneer-comparison.json`：原版 96e4b4b 对比 f4919cc，seed 1/19/90601 双方各首 130 轮。
  原版 2 组开拓者死亡，新版 0 组；这是 **AI 模型和策略共同变化**，不能归因于单个策略。
  文件明确标注其源码，不能冒充 3632d37 的新测数据。

复现主验证：

```sh
python -m unittest discover -s tests -v
python tools/check_front_defense.py --rounds 260 --output reports/retest-cases.json
node tools/check_combat_visuals.cjs
python tools/verify_front_package.py --archive CoreGeek.tar.gz --output reports/retest-package.json
```

包 SHA256：`9946290933f761d8ae0c3a761d0c000c4644658f607e4fb03fbd0c433c09109b`。
包从确定的源码提交构建；报告与根目录包在后续交付提交中保存，两个 SHA 不要求自引用相等。

## 尚未覆盖

无公司内网或官方 PASS。精确寻敌半径、目标排序、机器人同步移动与角色争格、固定刷怪点
精确坐标、8–10 夜数量仍有待确认项。这里只新增局部风险避让，未完成全图风险路径规划。
旧全图追角色 AI 的报告仍属于旧模型；中间 8b93a89 的非连通布局与旧测试失败记录保留在本地
工作目录，不作为当前验收证据。第四夜全员防守与前三夜清场工作约定未取消。
