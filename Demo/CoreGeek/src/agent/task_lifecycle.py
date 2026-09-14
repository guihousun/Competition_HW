"""Task acceptance observation and bounded per-point retry policy.

Retry delays are strategy backoff, never the official 30-round task refresh.
State lives inside the existing per-match planner notes and is JSON-compatible.
"""
from dataclasses import dataclass
from .protocol import Pos


@dataclass(frozen=True)
class AcceptanceTuning:
    observation_wait: int = 3
    max_retry_delay: int = 16
    max_points: int = 8


TUNING=AcceptanceTuning()


def key(point):
    return f"{point.get('x')},{point.get('y')}"


def published_ready(point):
    if point.get('isValid',True) is not True:return False
    raw=point.get('coldDownRounds',0)
    if isinstance(raw,bool):return False
    try:return int(raw or 0)<=0
    except (ValueError,TypeError,OverflowError):return False


def can_retry(notes,point,round_no):
    item=notes.get('accept_retries',{}).get(key(point),{})
    return round_no>=item.get('until',0)


def defer(notes,point,round_no,reason):
    retries=notes.setdefault('accept_retries',{})
    name=key(point);old=retries.get(name,{})
    attempts=min(int(old.get('attempts',0))+1,5)
    delay=min(2**attempts,TUNING.max_retry_delay)
    retries[name]={'attempts':attempts,'until':round_no+delay,'reason':reason}
    while len(retries)>TUNING.max_points:retries.pop(next(iter(retries)))
    notes['acceptance_status']={'phase':'retry_backoff','point':dict(point),
                                'reason':reason,'retry_after':round_no+delay}


def confirm(notes,point):
    notes.get('accept_retries',{}).pop(key(point),None)
    notes['acceptance_status']={'phase':'confirmed','point':dict(point)}


def region_cells(anchor,zones):
    pos=Pos.load(anchor)
    zone=next((z for z in zones if Pos.load(z['pos'])==pos),None)
    if zone is None:return (pos,)
    kind=zone.get('neutralType','')
    explicit=tuple(dict.fromkeys(Pos.load(z['pos']) for z in zones if z.get('neutralType')==kind))
    if len(explicit)>1:return explicit
    if str(kind).endswith('TaskPoint2'):return (pos,Pos(pos.x+1,pos.y))
    return (pos,)


def ready_regions(points,zones,notes,round_no):
    """Published readiness belongs to its own logical point, including 2-cell points."""
    return [(dict(p['taskPosition']),region_cells(p['taskPosition'],zones))
            for p in points if p.get('taskPosition') and published_ready(p)
            and can_retry(notes,p['taskPosition'],round_no)]
