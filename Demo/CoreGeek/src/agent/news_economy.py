"""Cited sale timing signals from a validated public-news projection.

Pure strategy advice. No observation rewriting, pricing settlement, I/O or LLM.
The caller supplies events after WorldAgent's correction/evidence validation.
"""
import math


def _number(value):
    return type(value) in (int, float) and value >= 0 and (type(value) is int or math.isfinite(value))


def sale_signals(prices, day, events, conflicts=(), gaps=()):
    """Prioritize live, sellable metal facing an evidenced next-day decrease.

    Forecasts never replace vendorShopList. Unknown dates, provenance or active
    uncertainty produce no signal. Values and inputs remain unmodified.
    """
    if type(day) is not int or not 1 <= day < 10 or not isinstance(prices, dict):
        return {}
    target_day = day + 1
    blocked = {row.get('resource') for row in conflicts if isinstance(row, dict)}
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        start, end = gap.get('startDay'), gap.get('endDay')
        if ((type(start) is not int or start <= target_day)
                and (type(end) is not int or target_day <= end)):
            blocked.add(gap.get('resource'))
    if '*' in blocked:
        return {}
    candidates = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        resource = event.get('resource')
        # Construction stone is governed only by the existing material reserve.
        if resource not in ('iron', 'copper', 'silver', 'gold') or resource in blocked:
            continue
        live = prices.get(resource)
        if not _number(live) or live <= 0:
            continue
        start, end = event.get('startDay'), event.get('endDay')
        evidence = event.get('evidence')
        if (event.get('kind') != 'inferred' or event.get('priceDirection') != 'down'
                or type(start) is not int or type(end) is not int
                or not start <= target_day <= end or start != target_day
                or not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, dict) or not item.get('sourceId') or not item.get('quote')
                       for item in evidence)):
            continue
        amount, basis = event.get('priceAmount'), event.get('priceBasis', 'unknown')
        if amount is not None and (not _number(amount) or basis not in ('absolute', 'delta', 'percent')):
            continue
        if amount is None and basis != 'unknown':
            continue
        if basis == 'absolute' and not amount < live:
            continue  # a direction label cannot override the currently observed quote
        if basis in ('delta', 'percent') and amount == 0:
            continue
        signal = candidates.setdefault(resource, {
            'reason': 'public_next_day_price_drop', 'due_day': target_day,
            'price_direction': 'down', 'source_ids': [], 'event_ids': [],
            'observed_price': live, 'inferred_price': None})
        signal['source_ids'] = sorted(set(signal['source_ids']) | {x['sourceId'] for x in evidence})
        if event.get('id') and event['id'] not in signal['event_ids']:
            signal['event_ids'].append(event['id'])
        if basis == 'absolute':
            signal['inferred_price'] = amount
    return candidates
