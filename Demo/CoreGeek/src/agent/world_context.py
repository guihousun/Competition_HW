"""Public background for interpretation, never a recipe or inventory authority.

R06/R07: appearances are from taskbook v1.0 section 4.6.3. Current game
observations alone establish stock/prices. No issue answer or private fixture.
"""
import json

from .item_reference import EFFECTS, TASK_ITEMS

SOURCE = '任务书v1.0(2026-09-09) §4.6.3 任务用品'
APPEARANCES = {
    'AcientTablet': '灰白色石板，表面刻有无法辨认的远古文字，触摸时发出微弱的嗡鸣声',
    'StarSand': '一小袋银白色细沙，在暗处会自行闪烁冷光，触感冰凉',
    'FlameBreath': '透明晶石瓶，内封一团橙红色雾气，晃动时雾气自行发光发热',
    'FrostPotion': '深蓝色粘稠液体，瓶口始终结着一层薄霜，靠近时感到刺骨寒意',
    'ThornAmulet': '以活藤蔓编织的圆形护符，表面布满细密尖刺，散发淡淡的草木气味',
    'IronWhistle': '生铁铸造的哨子，表面布满锈迹，摇晃时内部有金属片碰撞的脆响',
}


def item_context(shop):
    """Bounded current public catalog; unknown names remain uninterpreted."""
    rows, omitted, used = [], 0, 0
    valid = isinstance(shop, list)
    for entry in shop if valid else []:
        if not isinstance(entry, dict):
            continue
        name = entry.get('name')
        if not isinstance(name, str) or not 0 < len(name) <= 80 or name in EFFECTS:
            continue
        price = entry.get('price')
        row = {'name': name, 'price': price if type(price) is int and price >= 0 else None}
        cost = len(json.dumps(row, ensure_ascii=False))
        if len(rows) >= 16 or used + cost > 1300:
            omitted += 1
            continue
        rows.append(row)
        used += cost
    return {'reference_source': SOURCE,
            'reference': [{'name': name, 'label': TASK_ITEMS[name], 'appearance': text}
                          for name, text in APPEARANCES.items()],
            'observed_task_candidates': rows, 'catalog_received': valid,
            'omitted': omitted}


def prefix_limits(texts, budget=2000):
    """Max-min fair prefixes: short sources donate unused quota to longer ones."""
    sizes = [len(text) for text in texts]
    limits = [0] * len(sizes)
    remaining = budget
    active = list(range(len(sizes)))
    while active and remaining:
        share = max(1, remaining // len(active))
        for i in active:
            count = min(share, sizes[i] - limits[i], remaining)
            limits[i] += count
            remaining -= count
        active = [i for i in active if limits[i] < sizes[i]]
    return limits
