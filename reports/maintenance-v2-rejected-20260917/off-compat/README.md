# Maintenance V2 默认OFF独立兼容性回放

基线 `9c6ed09adc90f0b2b670456349cfb96c4efad495`，候选 `fa74d5ab760da601bd644e72962776989ce99303`。
两独立Python3.11.10进程明确设置 `COMPETITION_HW_MAINTENANCE_V2=off`。

**179个输入全部一致，0差异。** 比较了每次完整response、完整decision_report以及完整PlannerState.dump的UTF-8规范JSON字节；未删除任何诊断/状态字段再比较。

- 原公开热点prefix为165轮（r1–165），每个进程自行累积真实planner上下文；只使用其中公开observation，不把旧expected_response强作新基线答案。
- 另14个独立公开测试输入每例重置planner：日间商店补给、已持包、缺资金、完整返程时间不足、健康墙、已持武器券、夜间商店、安静墙内走廊、有怪受损墙、已accept待题开拓者、避险开拓者、镜像/改ID、日末时段及原自占缺墙r1042。
- 测试场景来自候选已冻结测试的纯数据fixture，先物化为同一maintenance-fixtures.json交给两进程；不是官方比赛录像，不作为维护开启收益证据。
- 两进程源码HEAD和clean状态在前后均已核对，agent源码文件SHA256与输入文件hash分别保存在baseline/candidate worker-receipt.json。
- 两个工作进程实际退出码均0；监督进程实际退出0。实际PID、完整命令和wall时间见process-receipts.json；不把内部写入status当作进程完成证据。
- 禁止socket网络连接；network_attempts均空。没有调用真实模型、执行本地模拟步进、读取private checkpoint或后续未来输入，也没有运行完整unittest或新整场。

双方完整results.json相同SHA256：`fd604718c0808354ae934efdfa41fc288e278380e833e6d32bdd08a942f139c7`。
比较详情comparison.json；有差异时该脚本会保存完整字段差异并非零退出，本次differences为空。

覆盖界限：这是明确输入集合下的默认OFF兼容证据，不证明维护开启有效、不替代完整回归/实际包HTTP或官方平台测试；没有覆盖从维护开启状态切换为关闭后的存量租约兼容性。
