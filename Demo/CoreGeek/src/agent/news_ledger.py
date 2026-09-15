"""Deterministic helpers for the bounded public-news event ledger.

R01/R06/R07; task book 4.8/5.1.  These are pure functions used by WorldAgent:

* identity: a stable per-fact id derived from the typed event and its sources
  (never from the quote punctuation of a re-publication);
* price binding: a stated amount is accepted only when a verbatim quote binds
  that exact number to a price predicate and the unit is explicit.  A number
  that is only a date, a negated statement, a percentage-point figure where a
  percentage was claimed, or a rise with no magnitude never becomes an amount,
  and no unit is converted or guessed;
* date consistency: every known start/end/resume endpoint stays inside 1..10,
  start<=end and resume strictly after the endpoints it is compared with.  Any
  endpoint may be unknown on its own.

Nothing here reads simulator state, hidden answers, the filesystem or the
network, and no output modifies official prices, ore or observations.
"""
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation

ID_PREFIX = 'n'
ID_PATTERN = re.compile(r'n[0-9a-f]{16}\Z')
PRICE_BASES = ('absolute', 'delta', 'percent', 'unknown')

# Explicit correction/retraction wording.  A resumption notice ("已恢复") is
# not by itself a retraction of the earlier publication.
_CORRECTION = re.compile(
    r'更正|纠正|勘误|订正|撤回|撤销|取消|作废|改口|澄清|辟谣|'
    r'correct(?:ion|ed)?|retract(?:ed|ion)?|withdraw(?:n)?|cancel(?:led|ed)?|'
    r'revoke[dn]?|clarif(?:y|ied|ication)',
    re.I)

# Negation around a price claim.  "并未上涨到6金币" / "did not rise to 6 gold"
# state that the rise did not happen, so they cannot support a positive amount.
# The window is cut at sentence punctuation so a negation in an earlier sentence
# does not leak into a later, positive claim.
_NEGATION = re.compile(
    r'并未|并没有|没有|未能|尚未|不再|从未|未曾|无法|并不|不能|不会|'
    r'不上涨|不涨|未上涨|未涨|n\'t|\bnot\b|\bno\b|\bnever\b|\bwithout\b',
    re.I)
_SENTENCE = re.compile(r'[。；;！？!?\n]')

_CN_NUM = r'\d+(?:\.\d+)?'
_CN_UNIT = r'(?:金币|金|块钱|元|coin(?:s)?|gold)'
_EN_UNIT = r'(?:gold|coins?|g)\b'
_CN_RISE = (r'上涨|上升|增加|提高|涨价|上调|攀升|涨|升')
_CN_FALL = (r'下跌|下降|降低|降价|下调|回落|下滑|跌|降')
_CN_TO = (r'上涨到|涨到|上升到|升至|升到|提高到|提高至|达到|调至|调整到|'
          r'下降到|跌到|降至|降到|降低到|降低至|下调到|下调至')
_EN_RISE = r'rises?|rose|increases?|increased|goes?\s+up|went\s+up|climbs?|climbed|jumps?|jumped'
_EN_FALL = r'falls?|fell|drops?|dropped|declines?|declined|goes?\s+down|went\s+down'
_EN_UP = r'rises?|rose|up|increases?|increased|gains?|gained|climbs?|climbed|jumps?|jumped'
_EN_DOWN = r'falls?|fell|down|drops?|dropped|declines?|declined'
# "20 percent" is a percentage; "20 percentage points" is a different measure
# and must not satisfy a percent claim.
_PCT_UNIT = r'(?:%|％|percent(?!age|\s*points?)\b|pct\b)'

_ABSOLUTE = (
    # "收购价上涨到6金币", "铁价下降到6金币", "价格为6金币"
    re.compile(r'(?:收购价|售价|卖价|单价|价格|价钱|报价|物价|价)[^，。；;！？!?\n]{0,8}?'
               r'(?:' + _CN_TO + r'|变为|变成|达到|达|为|是|成|在)(?:了)?\s*'
               r'(?:约|大约|近)?\s*(' + _CN_NUM + r')\s*(' + _CN_UNIT + r')'),
    # "上涨到6金币" / "下降到6金币" without repeating the price noun
    re.compile(r'(?:' + _CN_TO + r')(?:了)?\s*(?:约|大约)?\s*(' + _CN_NUM + r')\s*(' + _CN_UNIT + r')'),
    # "每份6金币"
    re.compile(r'每\s*(?:个|单位|份|件|枚|块)?\s*(' + _CN_NUM + r')\s*(' + _CN_UNIT + r')'),
    # "price rises to 6 gold", "price falls to 6 gold"
    re.compile(r'(?:' + _EN_RISE + r'|' + _EN_FALL + r')\s+(?:to|at)\s+('
               + _CN_NUM + r')\s*(' + _EN_UNIT + r')', re.I),
    # "priced at 6 gold", "reaches 6 gold"
    re.compile(r'(?:priced\s+at|price\s+is|price\s+of|reaches?|reached|becomes?|became|costs?|at)\s+('
               + _CN_NUM + r')\s*(' + _EN_UNIT + r')', re.I),
)

_DELTA = (
    # "上涨2金币", "下降2金币", "涨了2金币", "降幅为2金币"
    re.compile(r'(?:' + _CN_RISE + r'|' + _CN_FALL + r')(?:了|幅(?:度)?(?:为|是))?\s*'
               r'(?:约|大约)?\s*(' + _CN_NUM + r')\s*(' + _CN_UNIT + r')'),
    re.compile(r'(?:' + _EN_UP + r'|' + _EN_DOWN + r')\s+by\s+('
               + _CN_NUM + r')\s*(' + _EN_UNIT + r')', re.I),
    re.compile(r'(?:' + _EN_RISE + r'|' + _EN_FALL + r')\s+(' + _CN_NUM + r')\s*(' + _EN_UNIT + r')', re.I),
)

_PERCENT = (
    re.compile(r'(?:' + _CN_RISE + r'|' + _CN_FALL + r'|涨幅)'
               r'(?:了|幅(?:度)?(?:为|是))?\s*(?:约|大约)?\s*(' + _CN_NUM + r')\s*' + _PCT_UNIT),
    re.compile(r'(?:' + _EN_UP + r'|' + _EN_DOWN + r')\s+(?:by\s+)?(' + _CN_NUM + r')\s*' + _PCT_UNIT, re.I),
    re.compile(r'(' + _CN_NUM + r')\s*' + _PCT_UNIT + r'\s*'
               r'(?:rise|increase|up|gain|fall|drop|decline|down)', re.I),
)

_MENTION_PATTERNS = (tuple((pattern, 'percent') for pattern in _PERCENT)
                     + tuple((pattern, 'absolute') for pattern in _ABSOLUTE)
                     + tuple((pattern, 'delta') for pattern in _DELTA))


def normalize_amount(value):
    """Return (amount, ok) without ever bouncing through float for integers.

    None is a valid "unknown"; bool, non-numbers, NaN, +/-Infinity and negative
    values fail.  Arbitrary-size Python ints stay exact instead of overflowing.
    """
    if value is None:
        return None, True
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False
    if isinstance(value, int):
        return (value, True) if value >= 0 else (None, False)
    if not math.isfinite(value) or value < 0:
        return None, False
    return (int(value) if value.is_integer() else value), True


def dates_consistent(start_day, end_day, resume_day):
    """Validate every known endpoint; each of the three may be unknown alone."""
    for value in (start_day, end_day, resume_day):
        if value is None:
            continue
        if isinstance(value, bool) or type(value) is not int or not 1 <= value <= 10:
            return False
    if start_day is not None and end_day is not None and start_day > end_day:
        return False
    if end_day is not None and resume_day is not None and resume_day <= end_day:
        return False
    if start_day is not None and resume_day is not None and resume_day <= start_day:
        return False
    return True


def has_correction_marker(text):
    return bool(isinstance(text, str) and _CORRECTION.search(text))


def _negated(quote, start, number_start):
    window = quote[max(0, start - 12):number_start]
    return bool(_NEGATION.search(_SENTENCE.split(window)[-1]))


def parse_price_mentions(quote):
    """Every (amount, basis) a verbatim non-negated quote explicitly states."""
    if not isinstance(quote, str) or not quote:
        return []
    mentions = []
    for pattern, basis in _MENTION_PATTERNS:
        for match in pattern.finditer(quote):
            if _negated(quote, match.start(), match.start(1)):
                continue
            try:
                amount = Decimal(match.group(1))
            except (TypeError, ValueError, InvalidOperation):
                continue
            if amount.is_finite() and amount >= 0:
                mentions.append((amount, basis))
    return mentions


def verify_price(quotes, amount, basis):
    """True only when a quote states exactly this amount with this basis.

    amount=None requires basis='unknown'.  Comparison is exact: 6 and 6.0 are
    the same value, while 1000000 never satisfies a claim of 1000001.
    """
    if amount is None:
        return basis == 'unknown'
    if basis not in ('absolute', 'delta', 'percent'):
        return False
    if not isinstance(quotes, (list, tuple)):
        return False
    normalized, valid = normalize_amount(amount)
    if not valid:
        return False
    expected = Decimal(str(normalized))
    for quote in quotes:
        for found, found_basis in parse_price_mentions(quote):
            if found_basis == basis and found == expected:
                return True
    return False


def witness(value):
    """Stable digest binding a JSON snapshot so a later rewrite is detectable."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def news_id(*, resource, availability, start_day, end_day, resume_day,
            price_direction, price_amount, price_basis, source_ids):
    """Stable identity: typed semantics plus the originating publications.

    Quote punctuation is deliberately excluded, so a re-publication of the same
    fact from the same source keeps one identity instead of becoming a second
    fact.  Distinct publications remain distinct provenance.
    """
    key = {
        'resource': resource, 'availability': availability,
        'startDay': start_day, 'endDay': end_day, 'resumeDay': resume_day,
        'priceDirection': price_direction, 'priceAmount': price_amount,
        'priceBasis': price_basis,
        'sources': sorted({source for source in source_ids if isinstance(source, str)}),
    }
    return ID_PREFIX + witness(key)[:16]


def record_id(record):
    return news_id(
        resource=record.get('resource'), availability=record.get('availability'),
        start_day=record.get('startDay'), end_day=record.get('endDay'),
        resume_day=record.get('resumeDay'), price_direction=record.get('priceDirection'),
        price_amount=record.get('priceAmount'), price_basis=record.get('priceBasis'),
        source_ids=[item.get('sourceId') for item in record.get('evidence', [])
                    if isinstance(item, dict)])
