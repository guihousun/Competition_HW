"""Opt-in daily news and treasure fixtures, owned exclusively by the simulator.

Official channels/day boundaries: task book 4.8/5.1/5.2. Text, event dates,
prices and treasure conditions here are LOCAL fixtures, not official test data.
Future publications and effects remain under _demo and never enter policy input.
"""
from copy import deepcopy


def install(state, *, start_day=1):
    """Install a three-day clue chain and the documented two-day mine outage.

    Start at the first round of the chosen day. Existing default scenarios are
    unchanged unless the caller explicitly installs this fixture.
    """
    if type(start_day) is not int or not 1 <= start_day <= 8:
        raise ValueError('fixture needs three game days within the ten-day match')
    if int(state.get('roundNo') or 0) != 1 + (start_day - 1) * 130:
        raise ValueError('install on the first round of the selected day')
    from .treasure import rite_of
    rite = rite_of(state)
    if rite is None or rite.opened:
        raise ValueError('fixture requires an unopened local treasure')
    rite.opens_at = 1 + (start_day + 1) * 130
    rite.closes_at = rite.opens_at + 29
    state['_demo']['treasure'] = rite.dump()
    x, y = rite.site['x'], rite.site['y']
    item_text = '、'.join(rite.items)
    publications = [
        {'day': start_day,
         'officialNews': '【本地新闻测试】矿井坍塌。今天铁矿仍可开采；明天开始全面停工两天，'
                         '停工期间小贩收购铁的价格上涨，修复结束后恢复开采和原收购价。',
         'folkLegends': f'【本地传闻测试·地点】祭坛位于横坐标{x}、纵坐标{y}的交点。'
                        '开启时间和献祭用品尚未传出。'},
        {'day': start_day + 1, 'officialNews': '【本地新闻测试】铁矿进入停工第一天。',
         'folkLegends': f'【本地传闻测试·用品】同一祭坛需要献祭{item_text}，每种恰好一份，'
                        '不能多也不能少；今天不公布开启时间。'},
        {'day': start_day + 2, 'officialNews': '【本地新闻测试】铁矿停工第二天，明天恢复。',
         'folkLegends': f'【本地传闻测试·时间】前两日所说的祭坛在第{start_day + 2}天白昼'
                        '开始后的前30个回合开启，含首尾回合。地点与用品要求不变。'},
        {'day': start_day + 3, 'officialNews': '【本地新闻测试】铁矿今日恢复开采和原收购价。',
         'folkLegends': '【本地传闻测试】此前祭坛的开启时段已经结束。'},
    ]
    base = {str(row['name']): row['price'] for row in state.get('vendorShopList') or []}
    # The example only promises an increase, not its magnitude. Six is a
    # deliberate fixture price, visible to both policy and trade execution.
    outage_price = max(6, base.get('iron', 3) + 1)
    state['_demo']['news_fixture'] = {
        'schema': 'local-world-news/1', 'publications': publications,
        'outages': [{'resource': 'iron', 'first_day': start_day + 1,
                     'last_day': start_day + 2, 'price': outage_price}],
        'base_prices': base, 'published_day': None,
        'note': 'Local test schedule; price amount and treasure conditions are not official constants',
    }
    publish(state, [])
    return state


def publish(state, events):
    """Publish this day's texts/prices; no future message is copied to the wire."""
    fixture = (state.get('_demo') or {}).get('news_fixture')
    if not isinstance(fixture, dict) or fixture.get('schema') != 'local-world-news/1':
        return
    round_no = int(state['roundNo'])
    day = (round_no - 1) // 130 + 1
    if fixture.get('published_day') == day:
        return
    publication = next((p for p in fixture['publications'] if p['day'] == day), {})
    state['worldNews'] = {key: publication.get(key, '') for key in ('officialNews', 'folkLegends')}
    prices = deepcopy(fixture['base_prices'])
    for outage in fixture['outages']:
        if outage['first_day'] <= day <= outage['last_day']:
            prices[outage['resource']] = outage['price']
    for row in state.get('vendorShopList') or []:
        if row.get('name') in prices:
            row['price'] = prices[row['name']]
    fixture['published_day'] = day
    events.append(f'第{day}天发布本地新闻/传闻测试线索（非官方题库）')


def resource_paused(state, resource, round_no):
    """Environment-side availability; not an extra observation permission."""
    fixture = (state.get('_demo') or {}).get('news_fixture')
    if not isinstance(fixture, dict) or fixture.get('schema') != 'local-world-news/1':
        return False
    day = (int(round_no) - 1) // 130 + 1
    return any(o['resource'] == resource and o['first_day'] <= day <= o['last_day']
               for o in fixture['outages'])
