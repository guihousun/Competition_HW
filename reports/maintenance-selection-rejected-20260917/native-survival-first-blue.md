# Native v5 survival首蓝终局补核

2026-09-17。独立补核已结束`survival/train-1-challenger`，不覆盖前四局报告；没有读取红方、私有checkpoint或当前仍被复用的原生journal，也没有操作进程/会话或重跑比赛。

| 轮数/终局 | 得分（击杀分/任务等/生存） | 基地HP | 执行失败/普通audit | 角色死亡 | movement |
|---|---|---:|---:|---:|---|
| 1300，达到时限且存活 | 939（277/112/550） | 1500 | 10/8 | 0 | 1300已审、0未审、0错误 |

1300个真实round文件连续存在；失败、audit、死亡、得分分项聚合与summary及r1300结算记录完全一致。末夜60个公开观测hash与59处after→后继hash通过。review回执为completed，native_turn1478、原SID一致；不以review正文的解释代替动作证据。末轮没有额外完整公开后继观测，未用私有快照补证。

末帧为三座L1电磁炮，均HP1000；三名角色均有实际成功操炮，最高同轮3人，成功攻击分别449/452/451次。不是单守样本。现金0，修包和Medicine库存0；hero45HP，两工人220HP。已结束3个任务中仅r160有+80金/+112分，其他两次奖励0，公开结束帧核验；配置50%失败不能写成此局已尝试3题恰好50%失败。

## 药剂、修包与恢复链

- 全局成功WallFixer共13次，**全部白天**；Medicine成功1次，也在白天。每次均校验库存减少1及后继公开状态，不能用计划/库存持有宣称夜间修过墙。
- r1086：10010在(9,25)，Medicine反馈true；背包药剂减少1，HP **25→220**，r1086.after_hash等于r1087.observation_hash。
- r1087：同工人命名使用WallFixer，对存活L3墙(10,24)；反馈true，包数 **4→3**，墙HP **800→2000**；完整后继是r1088。
- r1088：下一份原生回答实际移动至(9,26)，反馈true。不能把此前计划中的返(6,24)或24步全段都说成已执行。
- 末日r1171/r1172/r1199另成功用包，将三面墙175→1000、285→1000、570→1000；这些也都是白天。尾夜无修包使用记录。

原SID为`competition-react-survival-6d65caef95244ae0b32e87e1ee97f4d7`。两份已保存恢复回执`recovery/ce356e514c/native-resume.json`及`recovery/7a3debfd01/native-resume.json`均为`created:false / resumed:true / deliveredBy:ctx.agents.resume`，没有新会话冒充恢复；身份仍指向原`training-1/survival/native`工作目录。

严格区分回答与执行：1087-2原生turn1460、第一次恢复turn1461的回答把观测hash漏了一字符；最终同SID恢复回答turn1462的hash才与真实r1087匹配。三份回答的plan动作数组完全相同，未把人工动作冒充模型输出。r1087 round/decision/provenance绑定最终原生message/batch `618bf56c-6b25-481b-aed5-24ef6be601fd`，与保存的恢复call一致。该batch明确`executed_steps:1`，随后`health_or_structure_change`中断；以上800→2000和4→3证明原模型计划的第一步实际执行，而非仅生成后闲置。

r1087观测hash：`e6c325f40751b9e90bd26d26f57fb244f6bd952074d45700a5bccf47384dbe35`。同名JSON保存恢复proof/receipt文件hash、原生回执字段、计划一致性核验及r1086–1088完整公开前后帧；未重读当前活跃journal验证历史压缩文件，证明范围限定为当时保存的receipt/proof链和已执行公开轨迹。

V027机器人互斥占格/导航及已知滞留偏差仍存在。记录支持药剂、修包和三人操炮动作确已生效，不支持“111足够”“没有夜修也最优”或官方PASS；不得用五个单图结果排名确定最优策略。
