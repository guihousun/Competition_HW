"""Pure choice between two feasible NEW purchase proposals, not success tracking."""


def sanitize_memory(value, current_round=None):
    if (not isinstance(value,dict) or value.get('operation') not in ('upgrade','repair')
            or type(value.get('round')) is not int or not 1<=value['round']<=1300
            or (current_round is not None and value['round']>current_round)):
        return {}
    return {'operation':value['operation'],'round':value['round']}


def choose(upgrade, repair, memory, round_no):
    """Neither reserves a role or writes memory; final dispatch is separate."""
    previous=sanitize_memory(memory,round_no)
    available={'upgrade':upgrade[0] is not None,'repair':repair[0] is not None if repair is not None else None}
    funded=available['upgrade'] and upgrade[1].get('phase') in ('use','return_with_voucher')
    if repair is None and not funded:
        raise ValueError('Repair may be unevaluated only for an already carried upgrade')
    if funded:
        selected,reason='upgrade','carried_upgrade_delivery'
    elif all(available.values()):
        selected='upgrade' if previous.get('operation')=='repair' else 'repair'
        reason='alternate_new_opportunity' if previous else 'initial_repair_opportunity'
    elif available['upgrade']:
        selected,reason='upgrade','only_upgrade_feasible'
    elif available['repair']:
        selected,reason='repair','only_repair_feasible'
    else:
        selected,reason=None,'no_feasible_new_purchase'
    report={'previous_dispatch':previous or None,'available':available,'selected':selected,
            'reason':reason,'scope':'proposal_choice_not_dispatch_or_success',
            'remaining_actions':{name:(proposal[1].get('route') or {}).get('remaining_actions') if proposal is not None else None
                                 for name,proposal in (('upgrade',upgrade),('repair',repair))}}
    # Keep old idle-upgrade diagnostics when neither option is feasible.
    return (repair if selected=='repair' else upgrade),report


def new_dispatch(events, trips, round_no):
    """Only TripFrame's final issued event and registered record count."""
    record=trips.get('purchase') if isinstance(trips,dict) else None
    if not record or record.get('issued_round')!=round_no:
        return None
    if not any(e.get('kind')=='purchase' and e.get('event')=='issued'
               and e.get('owner')==record.get('owner') for e in events):
        return None
    return sanitize_memory({'operation':record.get('operation','upgrade'),'round':round_no}) or None
