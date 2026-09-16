# 真实请求中的机器人占格诊断

类别：本地日志工具；依据R01/R02的公开观测和响应隔离，不定义机器人碰撞规则。
基线18cdb9e。不改策略、模拟器、官方文档/协议；公司侧使用新包即可获得默认compact日志。

`diagnostics.build_summary`读取原请求 `robot.roles`，新增本地summary字段；
`ConsoleDigest`复用既有stream锁、迟到事件判断、轮号回退重置与最大8流上限。
正式HTTP JSON不增加字段，日志仍由已有响应后有序诊断队列处理。

输出以 `robot_occupancy ` 开头，后接一行JSON：

- `valid_live_count`：扫描范围内，health为正整数、坐标完整且位于当前有效mapInfo边界、ID无歧义的对象数；**不是全部真实机器人数**。
- `unique_cell_count`、`overlap_group_count`：上述对象的唯一坐标数、至少两个不同ID对象同坐标的组数。
- ID接受非负整数或ASCII十进制字符串并归一为整数；bool不是ID。重复ID涉及的全部存活条目排除，`duplicate_id_entries`计这些条目总数（不是只计多出的副本）。
- 缺失/非法地图尺寸或非列表robot.roles记录`available:false`和`data_insufficient`，不虚构零共格。
- `input_entries/scanned_entries/invalid_entries/dead_entries/duplicate_id_entries/input_truncated/metadata_truncated`保留质量口径；分类计数不保证互斥，不能相加当总数。
- 最多扫描4096条；有输入截断时所有空间计数仅代表前缀，后缀是否还有相同ID未知。负坐标和右/上越界坐标无效，(-1,-1)未上场占位不构成共格。
- `groups`最多3组，每组最多4个对象，保留坐标、完整组成员数及真实id、roleType、health、targetTeam；文字各最多16字符，截断单独计数。无共格时groups为空，不打印全量机器人。
- `groups_truncated/members_truncated`分别表示省略的组与已展示组内省略的成员。最后还有8192字节日志上限，超限明确打印output_omitted。
- 带round、side、stream、event及startup identity的code_commit/source_digest引用。库函数未经过startup时version为null，不伪造版本。

默认compact在每夜首个收到的帧、共格成员合并/拆分或消失、数据质量变化时输出。
相同共格成员整体移动、掉血及相同帧重复不会刷屏；下一局轮号回退重新记首个异常。
只有有界成员集合摘要，无长时序轨迹。full模式沿用原逐帧summary行为；off模式不打印。

手工回归 `tests/test_robot_occupancy_diagnostics.py` 覆盖共格、死亡同格、重复ID与归一、bool、缺坐标、越界占位、缺地图、重复帧、成员变化、新夜/新局、截断、大小界限与敏感字段隔离。
实际包HTTP对照证据由独立验证记录提供；合成请求只能证明诊断链和响应隔离，不能充作官方机器人共格实例。
输出scope恒为`observed_same_frame_not_collision_rule`。未记录共格不是禁止共格的证据；过滤/截断时尤其不能做这种推断。
