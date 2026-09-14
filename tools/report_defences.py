"""Render saved defence experiments as a Markdown report; no simulation or API calls."""
import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def table(rows):
    names = [name for name in ('RRR','GGG','EEE','GER','RRE') if any(r['strategy']==name for r in rows)]
    lines = ['| 阵容 | 存活到1300轮 | 平均基地HP | 平均本地分 | 平均任务分 | 平均击杀 | 动作失败 | 审计问题 |',
             '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name in names:
        group = [r for r in rows if r['strategy'] == name]
        values = [statistics.mean(r[k] for r in group) for k in ('base_hp','score_local','task_score','kills')]
        lines.append(f"| {group[0]['label']} | {sum(r['survived_full'] for r in group)}/{len(group)} | "
                     + ' | '.join(f'{v:.1f}' for v in values)
                     + f" | {sum(r['execution_failures'] for r in group)} | {sum(sum(r['audit_errors'].values()) for r in group)} |")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    directory = args.directory
    rows = json.loads((directory/'results.json').read_text(encoding='utf-8'))
    meta = json.loads((directory/'metadata.json').read_text(encoding='utf-8'))
    expected = len(meta['loadouts']) * (len(meta['development_seeds']) + len(meta['holdout_seeds'])) * len(meta['sides'].split(',')) * len(meta['pressures'].split(','))
    assert len(rows) == expected, 'Incomplete experiment'
    assert all(r['source_sha256'] == meta['source_sha256'] for r in rows)
    for key in {(r['seed'],r['side'],r['pressure']) for r in rows}:
        group = [r for r in rows if (r['seed'],r['side'],r['pressure']) == key]
        assert len(group) == len(meta['loadouts'])
        assert len({r['initial_state_sha256'] for r in group}) == 1
        assert all(Counter(r['first_complete_roster']) == Counter(meta['loadouts'][r['strategy']]) for r in group)
    sections = ['# 防御阵容配对测试结果',
        f"类别：策略实验 / 验证工具；规则依据 R03/R04（任务书 §4.5）。共 {len(rows)} 场，每场最多 {meta['round_limit']} 轮。",
        '本轮只比较炮种组合。工人经济、围墙、任务、选址和回防算法保持相同；没有覆盖所有防御策略。'
        '默认三火箭保持不变；以下是当前本地模拟器结果，不是官方胜率或最优阵容认证。',
        f"开发地图：{meta['development_seeds']}；本轮留出地图：{meta['holdout_seeds']}；每图蓝红两侧、压力 {meta['pressures']}。"
        '每组初始状态哈希相同，实际造出的炮种数量全部核验。压力系数是本地设定，不是官方难度。',
        '## 全部场次（探索性汇总）', table(rows)]
    for split, label in [('development','开发集'),('holdout','本轮留出集')]:
        for pressure in sorted({r['pressure'] for r in rows}):
            sections += [f'## {label} / 压力 {pressure}',
                         table([r for r in rows if r['split'] == split and r['pressure'] == pressure])]
    sections += ['## 相同地图对比三火箭',
        '按完全相同的种子、阵营和压力配对。血量胜/平/负包含基地被毁（HP=0）的场次；平均分差是本地总分差。'
        '此处合并开发与留出场次用于描述结果，不作为独立的再次验证。',
        '| 阵容 | HP胜/平/负 | 平均HP差 | 平均本地分差 |', '|---|---:|---:|---:|']
    baseline = {(r['seed'],r['side'],r['pressure']):r for r in rows if r['strategy']=='RRR'}
    for name in meta['loadouts']:
        if name == 'RRR': continue
        group = [r for r in rows if r['strategy']==name]
        hp = [r['base_hp']-baseline[(r['seed'],r['side'],r['pressure'])]['base_hp'] for r in group]
        score = [r['score_local']-baseline[(r['seed'],r['side'],r['pressure'])]['score_local'] for r in group]
        sections.append(f"| {group[0]['label']} | {sum(v>0 for v in hp)}/{sum(v==0 for v in hp)}/{sum(v<0 for v in hp)} | {statistics.mean(hp):+.1f} | {statistics.mean(score):+.1f} |")
    sections += ['## 地图与阵营明细', '每格为“基地HP / 本地总分”；HP为0表示基地被毁。蓝方=challenger，红方=defender。']
    for pressure in sorted({r['pressure'] for r in rows}):
        sections += [f'### 压力 {pressure}',
                     '| 地图 / 阵营 | 三火箭 | 三加特林 | 三电磁炮 | 三种各一座 | 两火箭一电磁炮 |',
                     '|---|---:|---:|---:|---:|---:|']
        for seed in meta['development_seeds'] + meta['holdout_seeds']:
            for side in meta['sides'].split(','):
                group = {r['strategy']:r for r in rows if (r['seed'],r['side'],r['pressure']) == (seed,side,pressure)}
                label = '蓝方' if side == 'challenger' else '红方'
                sections.append(f'| {seed} / {label} | ' + ' | '.join(f"{group[n]['base_hp']} / {group[n]['score_local']}" for n in meta['loadouts'])+' |')
    errors = Counter()
    requests = Counter()
    for row in rows:
        errors.update(row['audit_errors']); requests.update(row['requested_channels'])
    sections += ['## 执行与验证',
        f"动作失败合计 {sum(r['execution_failures'] for r in rows)} 次；审计问题：`{dict(errors)}`。动作失败与协议/预算审计问题分别统计，不能视为全部通过。",
        f"请求通道计数：`{dict(requests)}`；付费 LLM 调用合计 {sum(r['paid_llm_calls'] for r in rows)} 次。请求被生成不等于沙盒或 LLM 已实际执行成功。",
        '回归测试：`python -m unittest discover -s tests -v`，227项通过。新增测试核验两种混合阵容在左右两侧的实际建造数量；运行前修复了动态炮位顺序导致混合阵容造错炮种的问题。',
        '## 范围与限制',
        '- 刷新点仍是基地周围随机环带，与用户补充 S03 的固定刷新点要求冲突，是已知非合规近似。固定点修复后须重跑，不能沿用这里的排序。\n'
        '- 建造区域、波次数量、机器人选敌、经济记忆路径和任务样例含本地假设或实现差异。\n'
        '- 同种子保证初始地图相同；后续刷新会读取当时的空闲格子，策略改变占用后，刷怪位置也可能分化。这不是固定敌军序列的纯炮台靶场。\n'
        '- 仅本地单队模拟，未检验真实双队对战、内网判题、HTTP超时、真实任务沙盒。\n'
        '- 阵容采用固定建造顺序，没有穷举炮位、升级优先级、围墙结构、人员分工或动态转型。\n'
        '- 总分含任务分与生存分，不能解释为纯炮台伤害；不同阵容改变生存和行动轨迹后也会影响任务完成。\n'
        '- 每阵容仅12场、留出仅1张图；压力档位同样来自本地生成器。样本不足以证明对所有地图泛化。',
        '## 复现与证据',
        f"源码与官方文档组合 SHA-256：`{meta['source_sha256']}`。各文件哈希见 [metadata.json](metadata.json)。",
        '[逐场结果](results.json) · [分组汇总](summary.json) · [实验方法](../../docs/DEFENCE_EXPERIMENT.md)',
        '```powershell\npython tools/compare_defences.py --output reports/defence-comparison-new-run\npython tools/report_defences.py reports/defence-comparison-new-run\n```',
        '输出目录须为空，避免覆盖旧版本证据。']
    diagnosis = directory/'diagnosis-RRR-s1-defender-p3.json'
    if diagnosis.exists():
        details = json.loads(diagnosis.read_text(encoding='utf-8'))
        assert details['source_sha256'] == meta['source_sha256']
        sections += ['## 已复现的共用策略问题',
            '种子1、红方、压力3、三火箭：[诊断记录](diagnosis-RRR-s1-defender-p3.json)。'
            '第952轮仅14金币，却同时安排购买10金币的药剂和10金币的修墙包，第二笔失败。'
            '第1191轮开始，工人与开拓者连续争抢同一格，合计70次移动失败。'
            '这是共享预算与移动冲突协调的缺口，不能简单归因于火箭武器。'
            '本轮保留固定版本完成比较，没有中途修复并混用结果。',
            '下一步先处理队伍统一预算预留和同轮落点协调，再在固定刷新点得到确认、实现后重跑矩阵。'
            '之后分别比较升级顺序、炮位与墙体布局，避免同时改变多项而无法判断收益来源。']
    rendered = ''
    for section in sections:
        separator = '\n' if rendered.endswith('|') and section.startswith('|') else '\n\n'
        rendered += (separator if rendered else '') + section
    (directory/'REPORT.md').write_text(rendered+'\n', encoding='utf-8')
    print(table(rows))
    print(f'Wrote {directory / "REPORT.md"}')


if __name__ == '__main__':
    main()
