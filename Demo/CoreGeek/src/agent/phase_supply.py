"""Daytime spare-parts shopping using the existing single purchase contract.

Stock is carried back unused for night repair. No future income or robot wave
is assumed; only current quotes, stock, route and day deadline permit dispatch.
"""
from . import team_trip, strategy_config, upgrade_itinerary
from .protocol import WALL_FIXER
from .coordination import available_gold
from .market import shop_prices


def plan(turn, payload, commands, *, deadline, reserved_workers=()):
    config=strategy_config.get()
    report={'phase':'idle','reason':'stock_disabled','operation':'stock','voucher':WALL_FIXER}
    if not config['enabled']:
        return None,report
    tuning=config['maintenance']
    day=(turn.round_no-1)//130+1
    if not turn.is_day or day<tuning['survival_reserve_from_day'] or tuning['stock_target']==0:
        return None,report
    if len(turn.weapons())<3 or len(turn.workers())<2 or turn.station() is None or team_trip.threat(turn):
        report['reason']='initial_defence_or_visible_threat'
        return None,report
    operator=min(w.unit_id for w in turn.workers())
    repair_workers={r.unit_id for r in turn.workers() if r.unit_id!=operator}
    held=sum(r.backpack.count(WALL_FIXER) for r in turn.workers() if r.unit_id in repair_workers)
    held+=sum(c.get('num',1) for uid,c in commands.items() if int(uid) in repair_workers and c.get('action')=='buy' and c.get('name')==WALL_FIXER)
    needed=max(0,tuning['stock_target']-held)
    if not needed:
        report['reason']='night_stock_already_carried'
        return None,report
    price=shop_prices(payload).get(WALL_FIXER)
    if price is None or price<0 or price*needed>available_gold(turn,payload,commands):
        report['reason']='stock_not_affordable'
        return None,report
    # From the configured survival-reserve day onward, the repair stock is a
    # prerequisite for weapon spending. Emergency walls still use the same
    # path; a quoted weapon reserve no longer blocks buying the two repair
    # packs that protect the next night.
    for worker in sorted(turn.workers(),key=lambda w:(w.unit_id==operator,w.unit_id)):
        if worker.unit_id==operator or worker.unit_id in commands or worker.unit_id in reserved_workers:
            continue
        quantity=worker.backpack.count(WALL_FIXER)+needed
        if quantity>10:
            continue
        record=dict(owner=worker.unit_id,target=turn.station().unit_id,item=WALL_FIXER,
                    operation='stock',quantity=quantity,deadline=team_trip.deadline(turn,deadline))
        cost=team_trip.evaluate_trip(turn,payload,record,commands=commands)
        if cost.feasible and cost.command:
            report.update(phase='buy' if cost.command['action']=='buy' else 'to_shop',
                          reason='prepare_night_repair_stock',worker=worker.unit_id,
                          building=turn.station().unit_id,quantity=quantity,route=cost.report(),price=price)
            return (worker.unit_id,cost.command),report
    report['reason']='no_complete_spare_worker_stock_trip'
    return None,report
