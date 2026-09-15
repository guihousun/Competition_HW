# Issue #19 实施报告（本地证据）

日期：2026-09-15。基线 `1e5d53ed732863a77f330cbbd8077e09b62a5796`。
变更类别：规则实现修复 / 有标注的本地复现实验 / UI工具。未 commit、未 push、未改工作树外文件、未调用子agent。

## 做了什么

1. `agent/wave_data.py`：单一可审查波次数据源。附件 Sheet1 B2:H5 数值保留原始 `null`，
   登记格子/SHA/范围。默认 `observed-seven-days`（第1–7天附件实测，空白按0解释并标注；
   第8–10天未观测：本地续演假设每夜多5只小型 = 96/101/106）；可选 `local-pressure`
   （旧 `day×pressure+1`）。未知 profile 在 scenario/debug/recording/twomatch 入口拒绝。
2. `agent/scenarios.py`：固定列阵刷新池（`spawn_column_pool`/`spawn_row_band`），
   蓝方首列 x≈27 向右、红方 x≈13 向左，每列≤9 且高度≥9 时平移纵向带保持9格；高级怪
   近到远排序含召唤怪；占格跳过并报 `spawn_shortfall`；`spawn_points` 保序；layout 记
   `ownerTeam`。旧快照缺 profile 时迁移为 `local-pressure` 并打印提示。
3. `agent/match.py`：双队各自持有自己的固定池（按 `ownerTeam` 判定，自定义池保留，
   共享蓝池不再污染红方）。
4. `agent/simulator.py`：保留并复核墙体保护 `_intervening_wall`。
5. UI/协议：`profile` 贯通 scenario/series/recording/llm-demo/twomatch；默认实测时压力档
   禁用并说明仅旧实验可用；界面用“波次来源”中文文案显示，旧录像显示“来源未知”。
6. 文档：RULE_SUPPLEMENTS S02/S03/S04、DEVELOPMENT_RULES R05/D09、project_contract
   V-001/V-004、debug RULE_ROWS、`specs/ISSUE_19_MONSTERS.md`。

## 独立预期（写死表格）

`tests/test_fixed_spawns.py` 写死：前 7 天 35/45/58/69/75/86/91 与逐日类型；第 8–10 天
96/101/106、`wave.observed=false`/`extrapolation=true`/`raw_counts` 全 null/`seed_counts_day7`
可见，并出现“未观测…多 5 只小型”；空白类型按 0 有提示；默认 profile 不受 pressure 影响；
`local-pressure` 十夜 4/7/…/31 递增。

## 命令与结果

```
python -m unittest -v test_fixed_spawns test_robot_wall_screen test_rule_supplements   # OK
python -m unittest -v test_match.SpawnIsolationTests                                    # OK（双队池隔离/自定义池）
python -m unittest -v test_twomatch test_match                                          # OK
python -m unittest test_frontend_stability test_frontend_layout test_frontend_labels \
       test_frontend_daynight test_viewer test_recordings test_replay                   # 33 OK
python -m unittest discover -s tests -v                                                 # 785 tests, 1 failure（见下）
python reports/issue-19-monsters/regression_three_nights.py                             # 见下
```

`python -m unittest discover -s tests`（Python 3.11.10，解释器
`C:/Users/27334/AppData/Roaming/uv/python/cpython-3.11.10-windows-x86_64-none/python.exe`）
结果记录在 `reports/issue-19-monsters/full-suite.log`：**Ran 785 tests，FAILED (failures=1)**。

## 已知失败（保留，未修）

`tests/test_long_world_simulator.py::LongWorldSimulatorTests::test_long_clues_lead_to_actual_purchase_travel_and_summon_on_both_sides`
（seed 90317，side=defender）：新闻 Agent 能正确解释线索，但 `opened=false`（未召唤宝藏）。

该用例已显式选择 `profile='local-pressure'`（小波次），仍失败——说明不能单归因于新的 35 只默认
数量，固定布局/墙体保护改变机器人节奏后出现了真实的寻宝调度问题。未修改 seed/开启窗口/原料/
任务要求，未跳过测试或删除断言，也未退回四面随机刷怪。诊断见
`reports/issue-19-monsters/treasure-defender-diagnostic.{py,log,json}`：第 255–290 回合窗口内
开拓者动作为 move/acceptTask/submitAnswer，`focus.treasure` 始终为 false，宝藏行程从未接管。

这属于需要 Codex 连同新闻 Agent 进行策略定位的问题，超出本次怪物修复的确定性范围。
**本次交付为带 1 个已知失败的审核候选，不宣称总体通过。**

### 前三夜两侧本地回归（默认 profile，无人工指令）

`reports/issue-19-monsters/three-nights-local.json` / `.log`：

| seed | side | 每夜出怪 | 首个墙受损回合 | 首个基地受损回合 | 最终基地HP | 错误数 |
|---:|---|---|---|---|---:|---:|
| 1 | challenger | 35/45/58 | 87 | 无 | 1500 | 0 |
| 1 | defender | 35/45/58 | 86 | 无 | 1500 | 0 |
| 7 | challenger | 35/45/58 | 88 | 无 | 1500 | 0 |
| 7 | defender | 35/45/58 | 89 | 无 | 1500 | 0 |

这是真实本地结果：首个墙体受伤为总回合86–89，即第一夜第16–19回合，390回合内基地未被击中。
附件“约第10回合到墙”不是同一指标，也仍存在位置/行进节奏的校准差距。
允许真实失败，未伪造存活、未改官方常数。

## 改过的原测试及原因（显式选择 local-pressure）

- `tests/test_rule_supplements.py::test_basic_wave_size_increases_over_ten_nights_on_clear_boards`
  —— 该用例断言“旧压力实验”的十夜严格递增（4/7/…/31）；它验证的是旧公式本身，故显式
  `profile='local-pressure'`。新默认也有自己的逐日写死表测试，二者不互相替代。
- `tests/test_long_world_simulator.py::test_long_clues_lead_to_actual_purchase_travel_and_summon_on_both_sides`
  —— 测的是宝藏/长途采购链路，依赖小波次让开拓者存活；显式 `profile='local-pressure'`，
  避免用减怪掩盖默认失守。
- `tests/test_fixed_spawns.py` 中原有两个用例继承旧默认（2 只基础波次），改标
  `profile='local-pressure'` 后再断言落位/短缺。
- `tests/test_recordings.py` 的假 producer 增加 `**_kwargs` 以匹配新增 `profile` 关键字。

## 局限（不宣称官方认证）

- 精确刷怪坐标、纵向中心、红方镜像、占格处理、第 8–10 天数量均为本地假设；墙体直线穿格/
  擦角为本地几何约定。
- 本地单队/双队结算不是官方判题器；本轮未覆盖官方内网、真实任务判题与完整官方胜负。
- 旧报告是旧规则版本证据，未重算旧分数。
