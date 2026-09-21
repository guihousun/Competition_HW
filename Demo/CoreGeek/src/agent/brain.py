from typing import Any
from collections import Counter
from contextvars import ContextVar
from copy import deepcopy
from itertools import combinations, permutations
import os

from . import ballistics, defense_layout, planner, sandbox, tasks, treasure, nightwork, policy_supervisor, task_context, news_economy, upgrade_itinerary
from .grid import _cost_to_goal, next_step
from . import home_defense, strategy_config, phase_maintenance, traffic, frontline, night_gunner, dusk_return
_STRATEGY = strategy_config.get()
from .coordination import available_gold, reconcile
from .tasks import TaskPipeline
from .market import (
    buy_plan,
    can_upgrade,
    count_item,
    sellable_inventory,
    shop_prices,
    vendor_prices,
)
from .protocol import (
    BOMB,
    DAY_ROUNDS,
    ROUNDS_PER_DAY,
    MEDICINE,
    PIONEER,
    Pos,
    Turn,
    Unit,
    WALL,
    WALL_FIXER,
    WALL_MATERIAL,
    WEAPON_BUILD_COST,
    attack_command,
    attack_command_multi,
    build_command,
    collect_command,
    distance,
    move_command,
    sell_command,
    buy_command,
    use_command,
    station_footprint,
)

TOWER_LOADOUT = tuple(_STRATEGY["defense"]["tower_loadout"]) if _STRATEGY["enabled"] else ("rocket", "railgun", "rocket")
# Stone carried per wall run. Above the surplus threshold, so that a worker that
# has filled up while the wall ring is unfinished still visits the vendor with
# the excess instead of hoarding it.
STONE_BATCH = _STRATEGY["economy"]["stone_batch"] if _STRATEGY["enabled"] else 10
# How close we want the shop/vendor work to happen before dusk (R02: 70+60).
RETURN_BEFORE_NIGHT = _STRATEGY["economy"]["return_day_index"] if _STRATEGY["enabled"] else 55
UPGRADE_RETURN_MARGIN = _STRATEGY["economy"]["upgrade_return_margin"] if _STRATEGY["enabled"] else 3  # strategy buffer; the observed trip must fit before night
# Economy window: after the first towers are up, a worker may walk to the
# vendor/shop. The trip can span days, because the neutral points are randomly
# placed and may sit far from the base; an errand is abandoned in the evening so
# the worker is back for night defence.
ECONOMY_WINDOW_START = 12
# Surplus worth a trip. Kept close to the stone reserve so a worker that fills up
# while the wall ring is unfinished still earns something for the excess.
ECONOMY_MIN_SURPLUS = 3
# Stone kept back for wall building and repairs, never sold (R03: walls cost a
# stone and razing them refunds nothing, so the reserve is not wasteful).
WALL_RESERVE = 2
# Ore gathering is a top-up, not a goal in itself: walking further than this for
# a few gold costs more wall-building rounds than it earns. It is measured from
# the role, not from the base, so it bounds only the *first* step of a run; the
# return budget below is what keeps a trip affordable in rounds.
ORE_MAX_DISTANCE = _STRATEGY["economy"]["ore_max_distance"] if _STRATEGY["enabled"] else 8
# Metal carried per mining run. The same order of magnitude as STONE_BATCH: big
# enough that one trip to the vendor repays the walk, small enough that a worker
# never parks on a mine with a full bag (R03: worker backpack is 100 cells).
METAL_BATCH = _STRATEGY["economy"]["metal_batch"] if _STRATEGY["enabled"] else 10
# Evening return: enough rounds left to walk home before robots arrive.
RETURN_SAFE_INDEX = 50
# Stand by a cooling task point instead of walking home when the refresh is
# shorter than the round trip (local scheduling choice, official 30-round refresh).
CAMP_MAX_COOLDOWN = 34
TREASURE_PREP_RESERVE = 25  # strategy reserve, not an official price/limit
# Task pipeline: the frame is official, the solvers are pluggable.
TASK_PIPELINE = TaskPipeline()
_DECISION_REPORT = ContextVar('competition_decision_report',default=None)
_SHARED_CONTROL = ContextVar('competition_shared_control', default=None)
_UPGRADE_REPORT = ContextVar('competition_upgrade_report',default=None)
_TRIP_FRAME = ContextVar('competition_team_trip_frame',default=None)
_PHASE_MAINTENANCE = ContextVar("phase_maintenance",default=None)
_TRAFFIC = ContextVar('team_traffic',default=None)
_WORLD_VIEW = ContextVar('competition_world_view', default=None)
_DUSK_RETURN = ContextVar('competition_dusk_return', default=None)


def _work_deadline(turn):
    return dusk_return.deadline(turn)


def _purchase_deadline(turn):
    return _work_deadline(turn) if dusk_return.enabled() else DAY_ROUNDS - UPGRADE_RETURN_MARGIN


_CLEARED_NIGHT = ContextVar('competition_cleared_night', default=None)


def _productive_night(turn):
    context = _CLEARED_NIGHT.get()
    return bool(context and context[0] is turn and context[1]['phase'] == 'productive')
# Explicit opt-in for the P0b shared cognitive-channel scheduler. Default off:
# with the variable unset the judge path runs the reviewed deterministic strategy.
ROUTER_ENV = "COMPETITION_HW_LLM_ROUTER"
TASK_AGENT_ENV = "COMPETITION_HW_TASK_AGENT"
WORLD_AGENT_ENV = "COMPETITION_HW_WORLD_AGENT"
ROUTER_ANSWER_INSTRUCTION = ("请按题目要求作答，只输出答案本身；多个字段用 '字段=值' 并以 '; ' 分隔。")


def llm_router_enabled() -> bool:
    """True only when the operator explicitly enables the shared router."""
    return task_agent_enabled() or world_agent_enabled() or os.environ.get(ROUTER_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def task_agent_enabled() -> bool:
    return os.environ.get(TASK_AGENT_ENV, "").strip().lower() in ("1", "on", "true", "yes")


def world_agent_enabled() -> bool:
    value = os.environ.get(WORLD_AGENT_ENV)
    return task_agent_enabled() if value is None else value.strip().lower() in ("1", "on", "true", "yes")


def _treasure_notes(payload, turn):
    view = _WORLD_VIEW.get()
    if view is not None and view[0] is turn:
        return view[1]["treasure"]
    return treasure.treasure_notes(payload, (payload.get("teamOur") or {}).get("type", ""), turn.round_no)


def _mine_available(turn, material):
    view = _WORLD_VIEW.get()
    return view is None or view[0] is not turn or material not in view[1]["unavailable"]


def _sale_signals(turn):
    view = _WORLD_VIEW.get()
    return view[1].get('sale_signals', {}) if view is not None and view[0] is turn else {}


def decision_report():
    """Current request's local explanation; never part of official response JSON."""
    return deepcopy(_DECISION_REPORT.get())

# Defence geometry is recomputed for every wall/tower question asked about one
# snapshot (the layout alone is needed by the tower choice, the wall order, the
# stone count and the exit check). The layout and the interior graph depend only
# on the map size, the base position, the terrain near the base and the walls
# that already stand — never on towers or roles — so the answer is memoised. The
# key carries every one of those inputs, so an entry can never be stale; the
# cache is deliberately small and cleared wholesale on overflow rather than
# trying to evict cleverly. Sizes are per-snapshot work, not a global store.
_DEFENCE_CACHE: dict[tuple, tuple[defense_layout.Layout, defense_layout.InteriorGraph]] = {}
_DEFENCE_CACHE_LIMIT = 24
_TOWER_SITES_CACHE: dict[tuple, tuple[Pos, ...]] = {}
_TOWER_SITES_CACHE_LIMIT = 24
_NEIGHBOUR_STEPS = (
    (-1, -1), (-1, 0), (-1, 1),
    (0, -1), (0, 1),
    (1, -1), (1, 0), (1, 1),
)


def decide(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Competition entry: the roleCommandMap for this observation.

    Kept as the simple entry point; :func:`respond` is the full official
    response (it can additionally carry ``prompt`` / ``executeCmd``).
    """
    return respond(payload)["roleCommandMap"]


def respond(payload: dict[str, Any]) -> dict[str, Any]:
    with planner.planning_lock(payload):
        return _respond_locked(payload)


def _respond_locked(payload: dict[str, Any]) -> dict[str, Any]:
    """Full official response for one observation, including task bookkeeping.

    This is the judge-facing entry: it folds in the replies from the previous
    round, plans, and emits ``roleCommandMap`` plus at most one ``prompt`` or
    ``executeCmd``.
    """
    _DECISION_REPORT.set(None)
    state = _planner_state(payload)
    round_no = Turn.load(payload).round_no
    if state.last_round and round_no < state.last_round and round_no != 1:
        state.cleared_night_state = {}
        return {"roleCommandMap": {}}
    state.note_round(round_no)
    state.note_results(payload, round_no, routed=llm_router_enabled())
    response = plan_for_state(payload, state, judge_tasks=False)
    state.note_submission(response.prompt, response.execute, round_no,
                          bool(state.tasks.get("cycle")))
    state.last_round = round_no
    if isinstance(payload.get("_demo"), dict):
        _locally_judge_tasks(payload, response.commands, state)
    return response.build()


def plan_for_state(payload: dict[str, Any], planner_state: Any, *,
                   commit: bool = True, judge_tasks: bool = True) -> sandbox.ResponseBuilder:
    """Plan one round for an already-advanced planner state.

    ``commit=False`` makes the call a pure preview: the task pipeline still
    decides what it *would* do, but nothing about the cycle is remembered, so a
    preview can never accept or end a task.

    ``judge_tasks=False`` skips the local judge fixture entirely. The simulator
    needs that for its end-of-round preview: the round has already been judged
    with the commands that really executed, and judging it again would corrupt
    the task result bookkeeping.
    """
    turn = Turn.load(payload)
    _TRIP_FRAME.set(None)
    return_frame = dusk_return.Frame(turn)
    _DUSK_RETURN.set(return_frame)
    _CLEARED_NIGHT.set(None)
    from . import grid
    grid.set_policy_obstacles(turn)
    _PHASE_MAINTENANCE.set(None)
    traffic_frame=traffic.Frame(turn,deepcopy(getattr(planner_state,'traffic_state',{})))
    _TRAFFIC.set(traffic_frame)
    last_round = getattr(planner_state, "last_round", 0)
    if last_round and turn.round_no < last_round and turn.round_no != 1:
        if commit:
            planner_state.cleared_night_state = {}
        return sandbox.ResponseBuilder()
    if commit:
        _DECISION_REPORT.set(None)
    from . import cleared_night
    clearance_memory = getattr(planner_state, 'cleared_night_state', {})
    clearance_config = strategy_config.get()['nightwork']
    was_productive = clearance_memory.get('safe_rounds', 0) >= clearance_config['quiet_rounds']
    next_clearance, clearance = cleared_night.observe(
        turn, payload, clearance_memory, quiet_rounds=clearance_config['quiet_rounds'],
        enabled=strategy_config.get()['enabled'] and clearance_config['allow_after_clear'])
    _CLEARED_NIGHT.set((turn, clearance))
    if _productive_night(turn):
        grid.set_policy_obstacles(turn, cleared_night.hazard_cells(turn))
    if commit:
        planner_state.cleared_night_state = next_clearance
    if llm_router_enabled():
        if not commit:
            planner_state = planner.PlannerState.load(planner_state.dump())
        planner_state.note_results(payload, turn.round_no, routed=True)
        payload = planner_state.routed_observation(payload)
    _WORLD_VIEW.set(None)
    if world_agent_enabled():
        world = planner_state.ensure_team_agent(payload).world
        world.observe(payload, planner_state.ensure_llm_router())
        world_view = world.policy_view(turn.round_no)
        day = (turn.round_no - 1) // 130 + 1
        forecast = world.economic_view(day * 130 + 1) if day < 10 else {}
        world_view['sale_signals'] = news_economy.sale_signals(
            vendor_prices(payload), day, forecast.get('events', []),
            forecast.get('conflicts', []), forecast.get('gaps', []))
        _WORLD_VIEW.set((turn, world_view))
    commands: dict[int, dict[str, Any]] = {}
    pioneer = turn.pioneer()
    tower_pairs = _tower_pairs(turn)
    pioneer_tower = next((tower for role,tower in tower_pairs
                          if pioneer is not None and role.unit_id == pioneer.unit_id),None)
    cycle = planner_state.tasks.get('cycle')
    committed_work = bool(cycle and cycle.description and cycle.phase != 'ended' and not cycle.ended_round)
    if pioneer is not None:
        # Only public rumours and actually held items establish a commitment.
        notes = _treasure_notes(payload, turn)
        site = notes.get('site')
        required = notes.get('items') or []
        committed_work = committed_work or bool(notes.get('known') and not notes.get('taken')
            and site and required and all(pioneer.backpack.count(item) >= required.count(item) for item in required)
            and turn.round_no + max(0, distance(pioneer.pos, Pos.load(site))-1) <= int(notes.get('closesAt') or 0))
    directive = policy_supervisor.evaluate(turn,payload,pioneer_tower,tower_pairs=tower_pairs,
                                          committed_work=committed_work,dusk_index=_work_deadline(turn),
                                          cleared=_productive_night(turn))
    if return_frame.active and pioneer is not None:
        reserve = return_frame.required(pioneer)
        directive = policy_supervisor.Directive('prepare' if reserve else 'work', reserve,
            'dynamic_return_due' if reserve else 'dynamic_day_work', 0,
            return_frame.routes(pioneer)[0].get(pioneer.pos, 0))
    if was_productive and not turn.is_day and not _productive_night(turn):
        directive = policy_supervisor.Directive('defend', True, 'clearance_revoked_return_now',
                                                len(turn.robots), 0)
    if commit:
        planner_state.tasks['supervisor'] = directive.summary()
    night_staging = None
    if turn.is_day:
        _day(turn, commands, payload, planner_state)
    else:
        night_staging = _night(turn, commands, payload, planner_state)

    if directive.reserve_pioneer and turn.is_day and pioneer is not None:
        # Workers keep their day plan. Only the needed pioneer returns early.
        commands.pop(pioneer.unit_id,None)
        if pioneer_tower is not None and distance(pioneer.pos,pioneer_tower.pos)>1:
            target=_step_toward(turn,pioneer,pioneer_tower.pos,set())
            if target is not None:commands[pioneer.unit_id]=move_command(target)
    if directive.reserve_pioneer or night_staging is not None:
        # Pending judge results were already ingested by respond(). Keep task
        # memory, but do not let a hold, task walk, or treasure claim steal a post.
        job = None
    elif commit:
        job = _task_step(turn, commands, payload, planner_state)
    else:
        # A preview must not be able to change anything, and the pipeline writes
        # into `tasks` (solver notes, cycle phase, cooldowns). Running it on a
        # detached copy keeps the read-only promise while still showing the user
        # the command the pipeline would issue.
        scratch = _detached_planner(planner_state)
        job = _task_step(turn, commands, payload, scratch)
        plan = job.get("plan") if job else None
        if plan is not None and plan.kind in ("prompt", "cmd"):
            job = None  # a preview never claims a judge channel
    # Capture what the pipeline decided before anything else can overwrite it.
    pipeline_pioneer = None
    if job and job.get("claimed"):
        pioneer_role = turn.pioneer()
        if pioneer_role is not None:
            pipeline_pioneer = commands.get(pioneer_role.unit_id)
    # A pipeline claim must survive the defence planner: `_day` / `_night` already
    # ran above and may have queued a tower-post move or an attack for the same
    # pioneer. The task frame is turn-sensitive (hold range, timeout) once admitted
    # by the supervisor, so an admitted task command wins — including when
    # the pipeline deliberately issued nothing (a hold), in which case the defence
    # claim is dropped rather than allowed to walk the pioneer out of the ring.
    if job and job.get("claimed"):
        pioneer = turn.pioneer()
        pioneer_command = pipeline_pioneer
        for role in turn.controllable():
            if role.kind != PIONEER:
                continue
            if pioneer_command is not None:
                commands[role.unit_id] = pioneer_command
            else:
                commands.pop(role.unit_id, None)
    # Treasure work outranks the ordinary task walk: the window is narrow and the
    # altar is somewhere else entirely, so the pioneer must commit while it can.
    # It must not *re-run* over a purchase or summon the day plan already issued —
    # this pass rebuilt the command and replaced the `buy` with a walk to the altar
    # or the task point, which is what stopped the errand from ever completing.
    pioneer_role = turn.pioneer()
    issued = (commands.get(pioneer_role.unit_id) if pioneer_role is not None else None) or {}
    already_acting = issued.get("action") in ("buy", "summonTreasure")
    treasure_night = bool(pioneer_role is not None and _treasure_night_allowed(
        turn, payload, pioneer_role))
    if ((not directive.reserve_pioneer or treasure_night)
            and (night_staging is None or treasure_night)
            and not (job and job.get("claimed")) and not already_acting):
        pioneer = turn.pioneer()
        if pioneer is not None:
            altar = _treasure_step(turn, payload, pioneer)
            if altar is not None:
                commands[pioneer.unit_id] = altar
    # No pipeline claim this round. During the day the pioneer's job is the task
    # loop (任务书 §五): walk to a ready point, or stand on the one it is waiting
    # for. Defence positioning must not drag it back — that produced an
    # oscillation between two cells with every task point left unvisited.
    # The treasure itinerary only commits when the trip is essentially free (see
    # `_treasure_errand`), so when it has issued a command the day planner must not
    # overwrite it: the task walk would otherwise walk the pioneer straight back
    # towards the task point, which is what kept the errand from ever arriving.
    treasure_claimed = bool(
        pioneer_role is not None
        and commands.get(pioneer_role.unit_id)
        and (commands[pioneer_role.unit_id].get("action") in ("buy", "summonTreasure")
             or payload.get("_treasureRound") == turn.round_no))
    if ((not directive.reserve_pioneer or treasure_night)
            and (night_staging is None or treasure_night)
            and not (job and job.get("claimed")) and not already_acting
            and not treasure_claimed):
        pioneer = turn.pioneer()
        if pioneer is not None:
            has_work, task_move = _task_walk(turn, pioneer, payload)
            if has_work:
                if task_move is not None:
                    commands[pioneer.unit_id] = task_move
                else:
                    commands.pop(pioneer.unit_id, None)
    if job and job.get('claimed') and pioneer_role is not None:
        # A task hold is also a role claim even though it has no wire command.
        for uid, command in list(commands.items()):
            if command.get('action') == 'attack' and str(command.get('controllerId')) == str(pioneer_role.unit_id):
                commands.pop(uid)
    if not turn.is_day and not _productive_night(turn):
        excluded = {pioneer_role.unit_id} if job and job.get('claimed') and pioneer_role is not None else set()
        if night_staging and night_staging['hold']:
            excluded.add(night_staging['owner'])
        _fill_ready_weapons(turn, commands, excluded)
    from . import pioneer_safety
    safety = pioneer_safety.override(turn, commands)
    # A legal treasure summon is the terminal action of an already purchased,
    # time-bounded route.  Do not replace it with the generic night safety move;
    # the action itself is resolved at the altar and cannot be postponed to the
    # next daylight window.
    treasure_summon = bool(pioneer_role is not None
                           and (commands.get(pioneer_role.unit_id) or {}).get('action')
                           == 'summonTreasure')
    if safety is not None and pioneer_role is not None and not treasure_summon:
        for uid, command in list(commands.items()):
            if uid == pioneer_role.unit_id or (command.get('action') == 'attack'
                    and str(command.get('controllerId')) == str(pioneer_role.unit_id)):
                commands.pop(uid)
        if safety['command'] is not None:
            commands[pioneer_role.unit_id] = safety['command']
            job = None  # Leaving task range must not also submit a new task action.
    from . import rocket_post
    protected_posts = {pioneer_role.unit_id} if job and job.get('claimed') and pioneer_role else set()
    staged_maintenance = _PHASE_MAINTENANCE.get()
    if staged_maintenance is not None:
        protected_posts.update(staged_maintenance[1]['owned_roles'])
    post_reservations = ([] if _productive_night(turn) else
                         rocket_post.reserve(turn, commands, protected=protected_posts,
                                            business_goals=traffic_frame.goals))
    traffic_frame.recover(commands,protected_posts)
    commands = return_frame.apply(commands)
    if pioneer_role is not None and pioneer_role.unit_id in return_frame.returning:
        job = None
    commands = reconcile(turn, payload, commands, day_yield_deadline=_work_deadline(turn))
    trip_frame = _TRIP_FRAME.get()
    if trip_frame is not None:
        shared_context = _SHARED_CONTROL.get()
        shared = shared_context[1] if shared_context and shared_context[0] is turn else {}
        staging_owner = ({shared['owner']} if turn.is_day and shared.get('single_operator')
                         and shared.get('phase') == 'moving_to_common_stand'
                         and (turn.round_no-1) % ROUNDS_PER_DAY >= RETURN_BEFORE_NIGHT else set())
        commands = trip_frame.protect_final(commands, priority_return_moves=staging_owner | return_frame.returning)
        next_trips = trip_frame.finalize(commands)
        if commit:
            planner_state.team_trips = next_trips
        report = _UPGRADE_REPORT.get()
        if report is not None:
            report['team_trip_events'] = deepcopy(trip_frame.events)
            deferred=next((e for e in trip_frame.events if e.get('kind')=='purchase' and e.get('event')=='defer'),None)
            if deferred:
                report['planned_phase']=report.get('phase')
                report.update(phase='deferred',reason=deferred['reason'],final_route=deepcopy(deferred['route']))
    elif commit and hasattr(planner_state,'team_trips'):
        planner_state.team_trips.clear()
    maintenance = _PHASE_MAINTENANCE.get()
    if maintenance is not None:
        memory,result=maintenance
        phase_maintenance.finalize(memory,result,commands)
        if commit:
            planner_state.maintenance_state=memory
    elif commit and (turn.is_day or _productive_night(turn)):
        planner_state.maintenance_state={}
    traffic_memory=traffic_frame.finish(commands)
    if commit:planner_state.traffic_state=traffic_memory
    response = sandbox.ResponseBuilder()
    response.commands = {str(key): value for key, value in commands.items()}
    plan = job.get("plan") if job else None
    if commit and llm_router_enabled():
        if plan is not None and plan.kind in ("prompt", "cmd"):
            _route_task_channel(payload, planner_state, plan, response, turn=turn)
        _emit_router_channel(payload, planner_state, response, turn=turn)
        if (world_agent_enabled() or task_agent_enabled()) and planner_state.team_agent is not None:
            planner_state.team_agent.acknowledge(payload, planner_state, response)
    elif plan is not None:
        if plan.kind == "prompt":
            if commit and llm_router_enabled():
                _route_task_channel(payload, planner_state, plan, response, turn=turn)
            elif planner_state.judge.llm_available(bool(planner_state.tasks.get("cycle"))):
                response.prompt = plan.prompt or None
        elif plan.kind == "cmd":
            if planner_state.tasks.get("cycle"):
                if commit and llm_router_enabled():
                    _route_task_channel(payload, planner_state, plan, response, turn=turn)
                else:
                    response.execute = plan.command or None
    if commit and job and job.get("note"):
        planner_state.tasks["last_note"] = job["note"]
    if judge_tasks and isinstance(payload.get("_demo"), dict) and commit:
        judge_local_tasks(payload, response.commands, planner_state)
    if commit:
        cycle=planner_state.tasks.get('cycle')
        judge_state=getattr(planner_state,'judge',None)
        _DECISION_REPORT.set({'supervisor':directive.summary(),
                             'task':{'phase':'paused_for_defence' if directive.reserve_pioneer else (cycle.phase if cycle else 'idle'),
                                     'cycle_active':bool(cycle),
                                     'plan_kind':getattr(plan,'kind',None),
                                     'plan_purpose':getattr(plan,'purpose',None),
                                     'acceptance_status':deepcopy(planner_state.tasks.get('acceptance_status') or {}),
                                     'pending_command':getattr(judge_state,'pending_cmd',None) is not None if judge_state is not None else None,
                                     'pending_prompt':getattr(judge_state,'pending_prompt',None) is not None if judge_state is not None else None}})
        if getattr(planner_state, "team_agent", None) is not None:
            _DECISION_REPORT.get()["agent"] = planner_state.team_agent.summary()
        if turn.is_day or _productive_night(turn):
            _DECISION_REPORT.get()['upgrade_itinerary'] = deepcopy(_UPGRADE_REPORT.get())
        context = _SHARED_CONTROL.get()
        if context and context[0] is turn:
            _DECISION_REPORT.get()['shared_rocket_control'] = deepcopy(context[1])
        _DECISION_REPORT.get()['pioneer_safety'] = {k:v for k,v in safety.items() if k != 'command'} if safety else None
        _DECISION_REPORT.get()['strategy_config'] = strategy_config.identity()
        _DECISION_REPORT.get()['dusk_return'] = deepcopy(return_frame.rows)
        _DECISION_REPORT.get()['cleared_night'] = deepcopy(clearance)
        _DECISION_REPORT.get()['cleared_night']['observed_round'] = turn.round_no
        activities = {}
        operators = {str(c.get('controllerId')): uid for uid, c in commands.items()
                     if c.get('action') == 'attack'}
        for role in turn.controllable():
            action = commands.get(role.unit_id, {}).get('action')
            if str(role.unit_id) in operators:
                activities[str(role.unit_id)] = dict(action='attack', reason='weapon_operator',
                                                     target_weapon=operators[str(role.unit_id)])
                continue
            task_hold = role.kind == PIONEER and job and job.get('claimed')
            hold_reason = ('awaiting_command_result' if judge_state and judge_state.pending_cmd else
                           'awaiting_model_result' if judge_state and judge_state.pending_prompt else
                           'task_pipeline_hold')
            activities[str(role.unit_id)] = {'action': action,
                'reason': ('issued_action' if action else hold_reason if task_hold else
                           'no_reachable_affordable_work' if _productive_night(turn) else
                           'defence_or_phase_wait')}
        _DECISION_REPORT.get()['cleared_night']['roles'] = activities
        planner_state.tasks['supervisor']['cleared_night'] = deepcopy(_DECISION_REPORT.get()['cleared_night'])
        _DECISION_REPORT.get()['rocket_post_reservations'] = post_reservations
        _DECISION_REPORT.get()['traffic'] = {'events':traffic_frame.events,
                                            'openings':[p.dump() for p in traffic_frame.openings()]}
        if maintenance is not None:
            _DECISION_REPORT.get()['maintenance'] = deepcopy(maintenance[1]['report'])
        _DECISION_REPORT.get()['weapon_readiness'] = _weapon_readiness(turn, commands)
        _DECISION_REPORT.get()['worker_shelter'] = home_defense.status(
            turn, commands, quiet=_productive_night(turn))
        if _sale_signals(turn):
            _DECISION_REPORT.get()['news_economy'] = deepcopy(_sale_signals(turn))
        supervisor_notes = planner_state.tasks.get('supervisor')
        if isinstance(supervisor_notes, dict):
            if _sale_signals(turn):
                supervisor_notes['news_economy'] = deepcopy(_sale_signals(turn))
            else:
                supervisor_notes.pop('news_economy', None)
        if night_staging is not None:
            preparation = {
                'phase': 'waiting_guards' if not night_staging['hold'] else
                         'moving' if night_staging['command'] else 'holding',
                'reason': 'public_items_ready_waiting_for_time', 'radius': nightwork.WORK_RADIUS,
                'guard_count': night_staging['guard_count'],
                'confirmed_reduced_crew': night_staging['confirmed_reduced_crew']}
            _DECISION_REPORT.get()['treasure_preparation'] = preparation
            planner_state.tasks['supervisor']['treasure_preparation'] = deepcopy(preparation)
    return response


def _router_confirmation(payload: dict[str, Any], planner_state: Any) -> Any:
    """Public-evidence task confirmation for the shared router."""
    return task_context.public_task_confirmed(payload, planner_state.tasks.get("cycle"))


def _route_task_channel(payload: dict[str, Any], planner_state: Any, plan: Any,
                        response: Any, *, turn: Turn) -> None:
    """Offer the pipeline's cognitive request to the shared router (opt-in).

    When the router declines (quota spent, slot busy, not confirmed, oversized)
    the deterministic command map already built is returned unchanged; no model
    work is launched for a task that public evidence does not confirm.
    """
    try:
        router = planner_state.ensure_llm_router()
        confirmation = _router_confirmation(payload, planner_state)
        generation = confirmation.generation or "none"
        source_digest = confirmation.source_digest or task_context.context_digest("task", generation)
        if plan.kind == "prompt" and confirmation.confirmed:
            envelope = _remember_task_context(planner_state, confirmation, generation,
                                              source_digest, turn)
            # The context body, instruction header and nonce room share one
            # coherent prompt budget (task_context.render_prompt).
            text = (task_context.render_prompt(envelope, instruction=ROUTER_ANSWER_INSTRUCTION)
                    if envelope is not None else (plan.prompt or ""))
        else:
            text = plan.prompt if plan.kind == "prompt" else plan.command
        if not text:
            return
        request = router.offer(
            "task", generation, source_digest, kind=plan.kind, payload=text,
            purpose=plan.purpose or "", in_task=confirmation.confirmed,
            expected_result_shape=("answer_text" if plan.kind == "prompt"
                                   else "command_output"))
        if request is None:
            return
    except Exception:  # noqa: BLE001 - the deterministic response must survive any fault
        return


def _emit_router_channel(payload: dict[str, Any], planner_state: Any,
                         response: Any, *, turn: Turn) -> None:
    """Dispatch the selected owner even on rounds without a task proposal."""
    router = planner_state.ensure_llm_router()
    chosen = router.select(payload, confirmed_task=_router_confirmation(payload, planner_state))
    if chosen is not None and router.mark_emitted(
            chosen, round_no=turn.round_no, judge=planner_state.judge):
        if chosen.kind == "prompt":
            response.prompt = chosen.payload
        else:
            response.execute = chosen.payload


def _remember_task_context(planner_state: Any, confirmation: Any, generation: str,
                           source_digest: str, turn: Turn) -> Any:
    """Persist a bounded context for the confirmed generation (public fields only)."""
    if not getattr(confirmation, "confirmed", False) or generation == "none":
        return None
    store = planner_state.ensure_task_context()
    existing = store.get("task", generation)
    if existing is not None:
        return existing
    cycle = planner_state.tasks.get("cycle")
    envelope = task_context.build_context(
        "task", generation, source_digest,
        task_text=str(getattr(cycle, "description", "") or ""),
        answer_contract={"note": "答案格式以题目原文要求为准（本地约定，非官方新增规则）"},
        facts=[{"text": "phaseTask 已发布当前任务原文", "kind": "observed",
                "round": turn.round_no, "source": "phaseTask"}],
        open_questions=["题目要求的答案字段与单位"])
    store.put(envelope)
    return envelope


def _detached_planner(planner_state: Any) -> Any:
    """Copy of the planner state whose task memory is safe to mutate."""
    return planner.PlannerState.load(planner_state.dump())


def _detached_tasks(tasks_state: dict[str, Any]) -> dict[str, Any]:
    """Detached preview state, including nested retry and submission records."""
    return deepcopy(tasks_state)


def _planner_state(payload: dict[str, Any]) -> Any:
    """Planner memory: private to a simulated match, digest-keyed for the judge.

    Only a *generated* local match (``_demo.seed``) keeps planner memory inside
    the payload. A plain observation — the judge path, or an imported sample —
    must never have non-serializable state written into it, because that payload
    is exactly what the strategy is handed and what gets exported.
    """
    meta = payload.get("_demo")
    if isinstance(meta, dict) and "seed" in meta:
        state = meta.get("planner")
        if not isinstance(state, planner.PlannerState):
            state = planner.PlannerState.load(state)
            meta["planner"] = state
        return state
    return planner.state_for(payload)


def judge_local_tasks(payload: dict[str, Any], commands: dict[str, Any],
                      planner_state: Any) -> None:
    """Advance the local judge fixture with this round's executed commands.

    Public because the simulator owns the round loop: it must judge with the
    commands that really ran, and only once per round. The judge-facing path
    reaches the same code through :func:`respond`.
    """
    meta = payload.get("_demo")
    world = meta.get("task_world") if isinstance(meta, dict) else None
    if not world:
        return
    from . import taskworld
    events: list[str] = []
    report = taskworld.advance(payload, world, events)
    payload["teamOur"]["playerTasks"] = taskworld.player_tasks(
        payload, world, payload["teamOur"].get("type", "challenger"))
    meta["task_report"] = report
    meta["task_events"] = events
    if not report.get("phaseTask"):
        payload["phaseTask"] = ""
    if report.get("rewards"):
        planner_state.tasks["last_reward"] = report["rewards"]
    if report.get("ended"):
        planner_state.tasks["cycle"] = None
        planner_state.tasks["cooldown_until"] = int(payload.get("roundNo") or 1) + tasks.REFRESH_ROUNDS
        planner_state.tasks["solver_notes"] = {}


def _locally_judge_tasks(payload: dict[str, Any], commands: dict[str, Any],
                         planner_state: Any) -> None:
    """Backwards-compatible alias used by :func:`respond`."""
    judge_local_tasks(payload, commands, planner_state)


def _task_step(turn: Turn, commands: dict[int, dict[str, Any]],
               payload: dict[str, Any], planner_state: Any, *,
               commit: bool = True) -> dict[str, Any] | None:
    """Give the task pipeline its turn, and let it claim the pioneer."""
    pioneer = turn.pioneer()
    if pioneer is None:
        return None
    # Treasure work already issued this round wins. It is a one-cell, one-round
    # action (a purchase at the shop, a summon at the altar) with a hard deadline,
    # while the task walk is repeatable — and letting the pipeline claim the pioneer
    # here replaced the purchase with a walk, so the errand never completed.
    existing = commands.get(pioneer.unit_id) or {}
    if (turn.is_day or _productive_night(turn)) and payload.get("_treasureRound") == turn.round_no:
        return None  # includes a publicly justified preparation walk to the shop
    if existing.get("action") in ("buy", "summonTreasure"):
        return None
    # The task loop must also stand aside when the treasure trip is affordable and
    # can still finish: the window is one-shot while a task point comes back after
    # its refresh (任务书 §五). This is a narrow condition — the items must be
    # payable, or the walk must fit inside the remaining window — so it does not
    # starve the task loop, which an unconditional priority did (measured 150–260
    # points worse).
    if _treasure_claims_pioneer(turn, payload, pioneer):
        return None
    team = (payload.get("teamOur") or {}).get("type", "")
    # An imported request snapshot (官方示例) may carry no playerTasks and no task
    # points at all; then there is nothing to do and the pipeline stays out of
    # the way. A running cycle always keeps the pipeline alive, and so does a
    # generator that publishes the points only through map zones.
    has_points = bool(TASK_PIPELINE.own_points(payload, team))
    if not has_points and not planner_state.tasks.get("cycle"):
        return None
    # A preview must not be able to start or end a task.
    if not commit and not planner_state.tasks.get("cycle"):
        return None
    in_task = bool(planner_state.tasks.get("cycle"))
    # Night belongs to the defence (任务书 §4.7): the pioneer must be at a weapon,
    # not walking to a task point. A task already in flight is still held, and the
    # hold rule keeps the pioneer on the point, which the towers do not need.
    if not turn.is_day and not in_task and not _productive_night(turn):
        return None
    plan = None
    claimed = False
    pipeline = TASK_PIPELINE
    pending = planner_state.judge.pending_cmd
    try:
        command, plan = pipeline.step(
            state=payload, turn=turn, pioneer=pioneer,
            judge=planner_state.judge, notes=planner_state.tasks,
            is_day=turn.is_day, pending=pending,
            cognitive_solver=(lambda context: planner_state.ensure_team_agent(payload).solve(
                payload, planner_state, context)) if task_agent_enabled() else None,
        )
    except Exception as error:  # a solver bug must not cost us the round
        if commit:
            planner_state.tasks["last_error"] = f"{type(error).__name__}: {error}"
        return None
    if commit and plan is not None and plan.kind == "accept":
        # Remember the accepted point immediately: the hold range has to be
        # enforced from this round on, before the judge publishes phaseTask.
        zone = pipeline.on_point(payload, team, pioneer.pos)
        accepted_point=planner_state.tasks.pop('accept_target',None)
        planner_state.tasks["cycle"] = tasks.TaskCycle(
            point=accepted_point or (dict(zone["pos"]) if zone else dict(pioneer.pos.dump())),
            accepted_round=turn.round_no,
            task_type="自进化类",
            description="",
            timeout_rounds=0,
        )
        planner_state.tasks["solver_notes"] = {}
    elif commit and (in_task or planner_state.tasks.get("cycle")):
        _sync_cycle(payload, turn, planner_state)
    if planner_state.tasks.get("cycle") or plan is not None:
        # The task pipeline owns the pioneer for this round: a defence move would
        # walk it out of the task ring (ending the task) or cancel the walk to the
        # point. The claim is recorded so the caller can honour it too.
        commands.pop(pioneer.unit_id, None)
        claimed = True
    if command:
        commands[pioneer.unit_id] = command
    return {"plan": plan, "in_task": bool(planner_state.tasks.get("cycle")),
            "claimed": claimed,
            "note": getattr(plan, "purpose", "") if plan else ""}


def _sync_cycle(payload: dict[str, Any], turn: Turn, planner_state: Any) -> None:
    """Keep the recorded cycle's description/timeout in line with the observation.

    ``phaseTask`` and ``timeoutRounds`` are published by the judge; when they
    arrive we adopt them instead of guessing.
    """
    cycle = planner_state.tasks.get("cycle")
    if cycle is None:
        return
    description = str(payload.get("phaseTask") or "")
    if description and description != cycle.description:
        cycle.description = description
    for point in payload["teamOur"].get("playerTasks") or []:
        pos = point.get("taskPosition") or {}
        if {"x": pos.get("x"), "y": pos.get("y")} == cycle.point:
            timeout = int(point.get("timeoutRounds") or 0)
            if timeout and timeout != cycle.timeout_rounds:
                cycle.timeout_rounds = timeout
            cycle.task_type = str(point.get("taskType") or cycle.task_type)
            break
    # Unknown timeout/description does not reset the original acceptance attempt.


def _day(turn: Turn, commands: dict[int, dict[str, Any]], state: dict[str, Any],
         planner_state: Any = None) -> None:
    from . import team_trip
    frame = team_trip.TripFrame(turn,state,getattr(planner_state,'team_trips',{}),
        purchase_deadline=_purchase_deadline(turn),construction_deadline=_work_deadline(turn))
    _TRIP_FRAME.set(frame)

    def upgrade_plan():
        construction = frame.construction
        proposal,report = upgrade_itinerary.plan(turn,state,commands,start=0,
            deadline=_purchase_deadline(turn),commitment=frame.purchase,
            reserved_workers=({construction['owner']} if construction else ()))
        if frame.purchase and proposal is None and frame.purchase.get('phase')!='return':
            frame.cancel('purchase',report['reason'])
            proposal,report = upgrade_itinerary.plan(turn,state,commands,start=0,
                deadline=_purchase_deadline(turn),
                reserved_workers=({construction['owner']} if construction else ()))
        if frame.purchase is None and _STRATEGY['enabled']:
            from . import phase_supply
            # Existing/carried vouchers finish first. New investment competes
            # with stock only after the phase firepower target or wall emergency.
            held_voucher = any(can_upgrade(item,b.kind,b.level) for r in turn.workers()
                               for item in r.backpack for b in turn.ours if b.health>0
                               and not (b.kind==WALL and b.pos in _retired_rear_walls(turn)))
            if not held_voucher:
                stock,stock_report=phase_supply.plan(turn,state,commands,
                    deadline=min(DAY_ROUNDS-UPGRADE_RETURN_MARGIN,_work_deadline(turn)),
                    reserved_workers=({construction['owner']} if construction else ()))
                if stock is not None:
                    proposal,report=stock,stock_report
        frame.stage_purchase(report,proposal)
        return proposal,report

    _UPGRADE_REPORT.set({'phase':'idle','reason':'return_before_night'})
    # Return before night instead of waiting until robots arrive.
    index = (turn.round_no - 1) % ROUNDS_PER_DAY
    carried_upgrade = any(
        can_upgrade(item, building.kind, building.level)
        for worker in turn.workers() for item in worker.backpack
        for building in turn.ours if building.health > 0)
    dynamic_stage = (dusk_return.enabled() and turn.weapons()
                     and any(_DUSK_RETURN.get() is not None
                             and _DUSK_RETURN.get().required(worker)
                             for worker in turn.workers()))
    if ((not dusk_return.enabled() and index >= RETURN_BEFORE_NIGHT or dynamic_stage)
            and turn.weapons() and not carried_upgrade):
        # A priced upgrade trip uses its actual round-trip budget, not the
        # generic early-return cutoff that used to strand it before buying.
        upgrade, report = upgrade_plan()
        _UPGRADE_REPORT.set(report)
        if upgrade:
            commands[upgrade[0]] = upgrade[1]
        _night(turn, commands, state)
        return
    sites = _tower_sites(turn)
    order = _wall_order(turn)
    standing_towers = {unit.pos for unit in turn.weapons()}
    standing_walls = {unit.pos for unit in turn.walls()}
    occupied = turn.occupied_cells()
    towers_missing = [pos for pos in sites if pos not in standing_towers]
    walls_missing = [pos for pos in order if pos not in standing_walls]
    free_towers = [pos for pos in towers_missing if pos not in occupied]
    free_walls = [pos for pos in walls_missing if pos not in occupied]

    claimed: set[Pos] = set()
    # Metal targets are coordinated separately from movement/build landing
    # cells.  A worker reserves the observed mine it is actually pursuing for
    # this planning pass, allowing the next worker to choose a different mine
    # without treating the mine itself as a blocked movement cell.
    metal_claimed: set[Pos] = set()
    busy: set[int] = set()
    # Roles the treasure itinerary has claimed for this round. The tower-post loop at
    # the end of `_day` must not overwrite them: it would replace the `buy` or
    # `summonTreasure` command with a move, which is exactly how the pioneer ended
    # up strolling past the shop without ever purchasing anything.
    reserved: set[int] = set()
    # The old viewer-only _demo.errands ledger is not official input and is
    # absent after observation() serialization. Reading it made identical public
    # observations + PlannerState issue different commands (seed7, round50).
    # Use the existing official path: actual purchase/construction commitments
    # belong to TripFrame, and treasure plans derive from public news/items.
    errands = None
    # The treasure itinerary is deliberately independent of that ledger: it is
    # derived each round from the published rumour, the pioneer's backpack and its
    # gold. Tying it to `_demo` (as the first version did) meant it never ran on
    # the official stateless POST, where `_demo` is stripped from the request.
    # A treasure window is a hard deadline, so it gets the pioneer's turn first.
    pioneer = turn.pioneer()
    if pioneer is not None and pioneer.unit_id not in commands:
        if _treasure_errand(turn, pioneer, commands, state, errands):
            busy.add(pioneer.unit_id)
            reserved.add(pioneer.unit_id)
    if isinstance(state, dict):
        # Tell the later stages of this round that the treasure itinerary owns the
        # pioneer, so the task walk does not walk it back to the task point.
        state["_treasureRound"] = turn.round_no if reserved else None
    upgrade, upgrade_report = upgrade_plan()
    _UPGRADE_REPORT.set(upgrade_report)
    if frame.purchase and frame.purchase.get('phase')=='return':
        busy.add(frame.purchase['owner']);reserved.add(frame.purchase['owner'])
    if upgrade:
        owner, command = upgrade
        commands[owner] = command
        busy.add(owner);reserved.add(owner)
        if errands is not None:
            for key,mission in list(errands.items()):
                if key==str(owner) or mission.get('goal')=='shop':errands.pop(key,None)
    if errands is not None:
        # Starting an errand must come before the construction plan: once a
        # worker leaves for the vendor the defence plan must not re-assign it,
        # otherwise a multi-day trip could never finish.
        for role in turn.workers():
            if role.unit_id in commands or role.unit_id in busy:
                continue
            if frame.construction and role.unit_id==frame.construction['owner']:
                continue
            if not upgrade and _start_errand(turn, role, commands, state, errands, busy):
                busy.add(role.unit_id)
                break
        for role in turn.workers():
            if role.unit_id in busy:
                continue
            if frame.construction and role.unit_id==frame.construction['owner']:
                continue
            if _errand_mission(turn, role, commands, state, errands):
                busy.add(role.unit_id)
    # A trip already under way owns the team's spare worker: the metal run must
    # not start a second one and bypass the one-errand-at-a-time ledger.
    errand_owners = {int(key) for key, mission in (errands or {}).items() if mission}
    if upgrade:errand_owners.add(upgrade[0])
    # One route memo per planning pass: the metal decision probes many candidate
    # mines and stand cells, and every miss is a bounded A* search (R01: the
    # response must stay well inside 5s).
    routes = _RouteCost(turn)
    def construct(role, *, only_sites=None):
        from . import construction_trip
        landings = set(claimed)
        for issued in commands.values():
            if issued.get('action') in ('move','build'):
                landings.update(Pos.load(p) for p in issued.get('targetPos',()))
        contract = frame.construction
        targets = ([Pos.load(p) for p in contract['walls']] if contract else
                   walls_missing if only_sites is None else only_sites)
        if contract and contract['phase']=='return':
            targets = []
        targets = [p for p in targets if p not in standing_walls and p not in _retired_rear_walls(turn)
                   and (p not in occupied or p==role.pos)]
        guard = team_trip.RouteGuard(turn,state,frame.purchase,commands)
        trip = construction_trip.plan(turn,role,targets,claimed=landings,
            deadline=_work_deadline(turn),batch_limit=STONE_BATCH,
            mine_available=_mine_available(turn,'stone'),route_guard=guard)
        report = {'worker':role.unit_id,'phase':'hold','reason':'no_safe_complete_trip'}
        if trip:
            command,report = trip
            commands[role.unit_id] = command
            frame.stage_construction(role.unit_id,command,report)
            destinations = [Pos.load(p) for p in command.get('targetPos',())]
            claimed.update(destinations)
            if command['action']=='build':
                free_walls[:] = [p for p in free_walls if p not in destinations]
        elif contract:
            if home_defense.inside(turn,role.pos) and any(distance(role.pos,g.pos)==1 for g in turn.weapons()):
                frame.cancel('construction','returned_observed')
            else:
                contract['phase']='return'
        report['team_route'] = guard.report()
        upgrade_report.setdefault('construction',[]).append(report)
        busy.add(role.unit_id)

    for role in turn.workers():
        if role.unit_id in busy:
            continue
        if frame.construction and role.unit_id==frame.construction['owner']:
            construct(role)
            continue
        if upgrade and not any(distance(role.pos,g.pos)<=4 for g in turn.weapons()):
            step = home_defense.step_inside(turn,role,claimed=claimed)
            if step is not None:
                commands[role.unit_id]=move_command(step);claimed.add(step)
            continue
        previous_claimed,previous_walls = set(claimed),list(free_walls)
        _worker_day(
            turn, role, sites, free_towers, free_walls, claimed, commands, state, busy,
            other_errand=bool(errand_owners - {role.unit_id}), routes=routes,
            metal_claimed=metal_claimed,
        )
        proposed = commands.get(role.unit_id,{})
        if (not proposed and role.unit_id not in busy and role.unit_id not in errand_owners
                and not towers_missing
                and frame.construction is None
                and 'construction' not in frame.pending
                and (frame.purchase is None or frame.purchase['owner'] != role.unit_id)
                and role.pos in walls_missing):
            # Ordinary free_walls excludes our occupied cell. Adopt only this
            # one missing wall, via a proven move/build/return trip; never build
            # on ourselves or reserve every remaining wall as an endless job.
            construct(role,only_sites=[role.pos])
            continue
        if frame.purchase and proposed.get('action') in ('move','build'):
            guard = team_trip.RouteGuard(turn,state,frame.purchase,
                {uid:c for uid,c in commands.items() if uid!=role.unit_id})
            if not guard.allows_command(role,proposed):
                commands.pop(role.unit_id,None)
                claimed.clear();claimed.update(previous_claimed)
                free_walls[:] = previous_walls
                if not towers_missing and walls_missing:
                    construct(role)
                continue
        if upgrade and commands.get(role.unit_id,{}).get('action') == 'move':
            destination=Pos.load(commands[role.unit_id]['targetPos'][0])
            if not any(distance(destination,g.pos)<=4 for g in turn.weapons()):
                commands.pop(role.unit_id,None)
                claimed.discard(destination)
                if not towers_missing and walls_missing:
                    claimed.clear();claimed.update(previous_claimed)
                    free_walls[:] = previous_walls
                    construct(role)
    # Tasks come last so a task command always wins the pioneer: the task frame
    # is turn-sensitive (timeout, hold range) while defence positioning is not.
    for role, tower in _tower_pairs(turn):
        if role.kind != PIONEER:
            continue
        # A role already claimed this round keeps its command: the reward for
        # walking away from the shop is losing the whole errand.
        if role.unit_id in reserved:
            continue
        # A pioneer running a task must not be re-assigned to a tower post: the
        # walk back would break the hold range and end the task (任务书 §五). The
        # pipeline already issued its command, so an existing entry is a claim.
        # At night an open task releases the pioneer, because defence comes first.
        if turn.is_day and (role.unit_id in commands
                            or pioneer_is_busy(role, state, planner_state)):
            continue
        if distance(role.pos, tower.pos) <= 1 and role.pos not in walls_missing:
            continue
        step = _step_toward(turn, role, tower.pos, claimed, inside_only=True)
        if step is not None:
            commands[role.unit_id] = move_command(step)


def pioneer_is_busy(role: Any, payload: dict[str, Any], planner_state: Any) -> bool:
    """True while the pioneer owes its turn to the task pipeline.

    Either memory says a cycle is open, or the judge is publishing a task text —
    the stateless judge path has only the latter, and both must keep the pioneer
    away from tower posts.
    """
    if planner_state is not None and planner_state.tasks.get("cycle"):
        return True
    return bool(str((payload or {}).get("phaseTask") or "").strip())


def _task_walk(turn: Turn, pioneer: Unit, state: dict[str, Any]
               ) -> tuple[bool, dict[str, Any] | None]:
    """Decide the pioneer's *movement* for a task point, from published data only.

    Returns ``(has_work, command)``:

    * ``(False, None)`` — nothing to do, the caller may position the pioneer for
      defence;
    * ``(True, command)`` — walk toward a ready point;
    * ``(True, None)``  — stay put, because the pioneer is already standing on the
      point it is waiting for. This is the important case: without it the defence
      planner walks the pioneer home, and it spends the whole day shuttling while
      the task point waits.

    Everything comes from ``playerTasks`` (isValid / coldDownRounds), so the
    decision is identical whether or not the caller keeps planner memory — the
    official POST is stateless.
    """
    if not turn.is_day and not _productive_night(turn):
        return False, None
    team = (state.get("teamOur") or {}).get("type", "")
    own_cells = {
        Pos.load(zone["pos"]) for zone in (state.get("mapInfo") or {}).get("zones") or ()
        if str(zone.get("neutralType", "")).startswith(team)
        and "TaskPoint" in str(zone.get("neutralType", ""))
    }
    ready: list[Pos] = []
    cooling: list[tuple[int, Pos]] = []
    for entry in (state.get("teamOur") or {}).get("playerTasks") or []:
        position = entry.get("taskPosition") or {}
        if not position:
            continue
        goal = Pos(int(position.get("x", -1)), int(position.get("y", -1)))
        if own_cells and goal not in own_cells:
            continue  # only our own points exist for us (任务书 §4.6.2)
        cooldown = int(entry.get("coldDownRounds") or 0)
        if entry.get("isValid", True) and cooldown <= 0:
            ready.append(goal)
        elif 0 < cooldown <= CAMP_MAX_COOLDOWN:
            cooling.append((cooldown, goal))

    candidates = ready or [goal for _cooldown, goal in cooling]
    if not candidates:
        return False, None
    # Pick between several points by arrival, not by raw distance: a point that is
    # still cooling can be the better target when its refresh finishes about when
    # we would get there. Task points are far apart (a full map crossing is ~40
    # rounds), so committing to the wrong one costs a whole game day. With a single
    # candidate this is exactly the old nearest-point behaviour.
    def arrival_key(goal: Pos) -> tuple[float, int, int]:
        wait = 0
        for cooldown, candidate in cooling:
            if candidate == goal:
                wait = cooldown
                break
        walk = distance(pioneer.pos, goal)
        return (max(walk, wait), walk, goal.x, goal.y)

    goal = min(candidates, key=arrival_key)
    if distance(pioneer.pos, goal) <= 1:
        return True, None  # standing on it: hold, and let the pipeline act
    step = tasks.walk_to_ring(turn, pioneer, goal)
    if step is None:
        return False, None
    return True, move_command(step)


def _camp_command(turn: Turn, pioneer: Unit, state: dict[str, Any]) -> dict[str, Any] | None:
    """Stand by a cooling task point instead of walking home and back.

    A task point 20+ cells from the base costs roughly a 40-round round trip per
    task, while the official refresh is only 30 rounds (任务书 §五). Waiting next
    to the point turns the next task into a single step. The cost is real: the
    pioneer is not at a weapon at night, so this only happens during the day.

    Note the clock: day is rounds 1..70 and the defence return starts at 55, so
    the task window is only ~55 rounds per game day. A point 20 cells away needs
    roughly 20 rounds to reach, which fits — but only if the walk is allowed to
    start inside that window rather than being treated as already too late.
    """
    if not turn.is_day:
        return None
    team = (state.get("teamOur") or {}).get("type", "")
    waiting: list[tuple[int, Pos]] = []
    for entry in (state.get("teamOur") or {}).get("playerTasks") or []:
        cooldown = int(entry.get("coldDownRounds") or 0)
        if 0 >= cooldown or cooldown > CAMP_MAX_COOLDOWN:
            continue
        position = entry.get("taskPosition") or {}
        waiting.append((cooldown, Pos(int(position.get("x", -1)), int(position.get("y", -1)))))
    if not waiting:
        return None
    cooldown, goal = min(waiting, key=lambda item: (item[0], item[1].x, item[1].y))
    if not any(Pos.load(zone["pos"]) == goal for zone in
               [z for z in (state.get("mapInfo") or {}).get("zones") or ()
                if str(z.get("neutralType", "")).startswith(team)
                and "TaskPoint" in str(z.get("neutralType", ""))]):
        return None
    if distance(pioneer.pos, goal) <= 1:
        return None  # already standing by; no need to spend the turn
    # The point is an obstacle, so approach its ring rather than the cell itself.
    step = tasks.walk_to_ring(turn, pioneer, goal)
    if step is None:
        return None
    return move_command(step)


def _treasure_route(turn, state, pioneer, notes, commands=None):
    """Public, bounded feasibility estimate for a complete known treasure trip.

    Each buy consumes a round; arrival next to the altar is not itself a summon.
    Costs use reachable standing cells instead of straight-line travel estimates.
    """
    if (state.get("phaseTask") or not notes.get("known")
            or notes.get("taken") or not notes.get("site")):
        return None
    required = Counter(notes.get("items") or [])
    if not required:
        return None
    missing = list((required - Counter(pioneer.backpack)).elements())
    if missing and (pioneer.capacity is None or len(pioneer.backpack) + len(missing) > pioneer.capacity):
        return None
    prices = shop_prices(state)
    available = available_gold(turn, state, commands or {}, replacing=pioneer.unit_id)
    if any(item not in prices for item in missing) or sum(prices[item] for item in missing) > available:
        return None
    site = Pos.load(notes["site"])
    closes = int(notes.get("closesAt") or 0)
    # Cheap lower bound avoids path searches once even an unobstructed trip fails.
    if turn.round_no + max(0, distance(pioneer.pos, site) - 1) + len(missing) > closes:
        return None
    routes = _RouteCost(turn, pioneer)
    start, elapsed = pioneer.pos, 0
    if missing:
        shop = _shop_cell(turn)
        if shop.x < 0:
            return None
        if not _adjacent_zone(state, pioneer.pos, "weaponShop"):
            approach = _mine_approach(turn, pioneer, shop, routes)
            if approach is None:
                return None
            start, elapsed = approach
        elapsed += len(missing)
    if distance(start, site) <= 1:
        altar_cost = 0
    else:
        altar_cost = min((routes(start, cell) for cell in _stand_cells(turn, pioneer, site, set())), default=10 ** 9)
    earliest = max(turn.round_no + elapsed + altar_cost, int(notes.get("opensAt") or 0))
    if altar_cost >= 10 ** 6 or earliest > closes:
        return None
    return {"missing": missing, "site": site, "summon_round": earliest}


def _treasure_claims_pioneer(turn: Turn, state: dict[str, Any], pioneer: Unit) -> bool:
    """Only an open, feasible trip may displace an unstarted task walk."""
    if pioneer is None or (not turn.is_day and not _productive_night(turn)
                           and not _treasure_night_allowed(turn, state, pioneer)):
        return False
    notes = _treasure_notes(state, turn)
    return bool(notes.get("open") and _treasure_route(turn, state, pioneer, notes))


def _treasure_night_allowed(turn: Turn, state: dict[str, Any], pioneer: Unit) -> bool:
    """Keep an already-open day-1..3 treasure itinerary moving at night.

    This is an existing committed errand, not a new night expedition.  From the
    fourth day onward full defence wins; before then the original simulator lets
    a purchased, time-bounded route finish so that the pioneer is not stranded
    at the altar when dusk begins.
    """
    if turn.is_day or home_defense.full_night(turn):
        return False
    notes = _treasure_notes(state, turn)
    return bool(notes.get("open") and not notes.get("taken")
                and _treasure_route(turn, state, pioneer, notes))


def _treasure_step(turn: Turn, payload: dict[str, Any], pioneer: Unit) -> dict[str, Any] | None:
    """Send the pioneer to the altar once the rite can actually be paid for.

    The official text leaves site, conditions and timing to be inferred from the
    rumours (任务书 §5.2), so the local fixture publishes them through
    `treasure_notes`. Nothing here guesses: the pioneer only goes when the window
    is open, the treasure is untaken, and the required 任务用品 are already in its
    backpack — otherwise the trip is a wasted day.
    """
    notes = _treasure_notes(payload, turn)
    if not notes.get("open") or _treasure_route(turn, payload, pioneer, notes) is None:
        return None
    bag = [str(item) for item in pioneer.backpack]
    required = [str(item) for item in notes.get("items") or ()]
    if any(bag.count(item) < required.count(item) for item in set(required)):
        return None      # cannot pay yet; the shop plan owns that errand
    site = Pos(int(notes["site"].get("x", -1)), int(notes["site"].get("y", -1)))
    if distance(pioneer.pos, site) <= 1:
        return {"action": "summonTreasure", "targetPos": [site.dump()],
                "item": required}
    step = tasks.walk_to_ring(turn, pioneer, site)
    if step is None:
        return None
    return move_command(step)


def _treasure_errand(turn: Turn, pioneer: Unit, commands: dict[int, dict[str, Any]],
                     state: dict[str, Any], errands: dict[str, Any] | None) -> bool:
    """Run the treasure itinerary: buy the 任务用品, then walk to the altar.

    Deliberately **memory-free**: everything is derived each round from the
    published notes plus the pioneer's own backpack and gold. That matters because
    the official POST is stateless — a plan that only lives in the errand ledger of
    one planner instance silently does nothing on the stateless path, which is
    exactly how the first version of this failed (it bought nothing in a real
    match while looking correct in a trace).

    Only when the window is close enough to matter and the pioneer can afford the
    items. Returns True when this consumed the pioneer's turn.
    """
    if not turn.is_day and not _productive_night(turn):
        return False
    key = str(pioneer.unit_id)
    if errands is not None:
        mission = errands.get(key)
        if mission is not None and mission.get("goal") not in ("shop", "altar"):
            return False
    notes = _treasure_notes(state, turn)
    if notes.get("preparable") and not notes.get("taken"):
        return _prepare_treasure(turn, pioneer, commands, state, notes)
    if not notes.get("known") or notes.get("taken") or not notes.get("site"):
        if errands is not None:
            errands.pop(key, None)
        return False
    route = _treasure_route(turn, state, pioneer, notes, commands)
    if route is None:
        return False
    site = route["site"]
    required = [str(item) for item in notes.get("items") or ()]
    missing = route["missing"]
    if missing:
        if not _adjacent_zone(state, pioneer.pos, "weaponShop"):
            return _walk_to_zone(turn, pioneer, "weaponShop", commands)
        commands[pioneer.unit_id] = {"action": "buy", "name": missing[0]}
        return True

    if not notes.get("open"):
        return False
    if distance(pioneer.pos, site) <= 1:
        commands[pioneer.unit_id] = {"action": "summonTreasure",
                                     "targetPos": [site.dump()], "item": required}
        if errands is not None:
            errands.pop(key, None)
        return True
    step = tasks.walk_to_ring(turn, pioneer, site)
    if step is None:
        return False
    commands[pioneer.unit_id] = move_command(step)
    if errands is not None:
        errands[key] = {"goal": "altar", "site": dict(notes["site"])}
    return True


def _prepare_treasure(turn, pioneer, commands, state, notes):
    """Buy supported requirements before the opening day is known; never summon.

    Keep three guns, a healthy base, a cash buffer, the current task, and enough
    real path budget to return before dusk. These are strategy choices.
    """
    base = turn.station()
    if ((not turn.is_day and not _productive_night(turn)) or base is None or base.health < 1000 or len(turn.weapons()) < 3
            or state.get("phaseTask")):
        return False
    required = Counter(notes.get("items") or [])
    held = Counter(pioneer.backpack)
    missing = list((required - held).elements())
    if not missing or pioneer.capacity is None or len(pioneer.backpack) + len(missing) > pioneer.capacity:
        return False
    prices = shop_prices(state)
    if any(item not in prices for item in missing):
        return False
    available = available_gold(turn, state, commands, replacing=pioneer.unit_id)
    if sum(prices[item] for item in missing) + TREASURE_PREP_RESERVE > available:
        return False
    shop = _shop_cell(turn)
    if shop.x < 0:
        return False
    cost = _RouteCost(turn, pioneer)
    approach = _mine_approach(turn, pioneer, shop, cost)
    if approach is None:
        return False
    stand, outbound = approach
    homes = _stand_cells(turn, pioneer, base.pos, set())
    homeward = min((cost(stand, cell) for cell in homes), default=10 ** 9)
    remaining = (130 if _productive_night(turn) else 0) + _work_deadline(turn) - (turn.round_no - 1) % 130
    if outbound + len(missing) + homeward + 2 > remaining:
        return False
    if _adjacent_zone(state, pioneer.pos, "weaponShop"):
        commands[pioneer.unit_id] = {"action": "buy", "name": missing[0]}
        return True
    return _walk_to_zone(turn, pioneer, "weaponShop", commands)


def _shop_cell(turn: Turn) -> Pos:
    """The weapon shop's own cell (the map has one; a fallback keeps it total)."""
    for pos, kind in sorted(turn.zones.items(), key=lambda item: (item[0].x, item[0].y)):
        if kind == "weaponShop":
            return pos
    return Pos(-1, -1)


def _distance_to_shop(turn: Turn, state: dict[str, Any]) -> int:
    """Distance from the pioneer to the weapon shop, or 0 when it is not visible."""
    pioneer = turn.pioneer()
    shops = [pos for pos, kind in turn.zones.items() if kind == "weaponShop"]
    if pioneer is None or not shops:
        return 0
    return min(distance(pioneer.pos, pos) for pos in shops)


def _economy_open(turn: Turn) -> bool:
    """True between mid-morning and the dusk return window of the current day.

    A round-index test rather than a round-number test keeps this correct across
    all ten days (R02) without hard-coding any day boundary.
    """
    index = (turn.round_no - 1) % 130
    return _productive_night(turn) or ECONOMY_WINDOW_START <= index < _work_deadline(turn)


def _is_return_phase(turn: Turn) -> bool:
    """Evening: errands must come home so night defence is not short-handed."""
    return not _productive_night(turn) and (turn.round_no - 1) % 130 >= RETURN_SAFE_INDEX


def _towers_done(turn: Turn) -> bool:
    """The three towers are the top priority; trade waits until they stand."""
    return len(turn.weapons()) >= 3


def _errand_mission(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
                    state: dict[str, Any], errands: dict[str, Any]) -> bool:
    """Continue or finish an errand already assigned to this worker.

    Returns True when the worker's turn is consumed by the mission. The mission
    ends as soon as the work is done, the evening return starts, or the reason
    for going disappears (prices gone, surplus sold, gold spent).
    """
    key = str(role.unit_id)
    if role.unit_id in commands:
        return False
    mission = errands.get(key)
    if not mission:
        return False
    goal = mission.get("goal")
    if goal == "vendor" and not _should_sell(turn, role, state):
        errands.pop(key, None)
        return False
    if goal == "shop" and (not _should_buy(turn, state) or _metal_needed(turn, role, state)):
        # A shop trip yields to a metal run the crew can still make: standing at
        # the shop with nothing worth buying used to consume every daylight
        # round, so the workers never reached an ore they could sell.
        errands.pop(key, None)
        return False
    if _adjacent_zone(state, role.pos, goal):
        if _try_trade(turn, role, commands, state):
            if goal == "vendor" and not _should_sell(turn, role, state):
                errands.pop(key, None)
            return True
        return False
    if _is_return_phase(turn):
        errands.pop(key, None)
        return False
    return _walk_to_zone(turn, role, goal, commands)


def _start_errand(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
                  state: dict[str, Any], errands: dict[str, Any] | None,
                  busy: set[int] | None = None) -> bool:
    """Assign a new errand when the defence is stable enough to spare a worker."""
    if errands is None or not _economy_open(turn) or not _towers_done(turn):
        return False
    if role.unit_id in commands or role.unit_id in (busy or set()):
        return False
    if any(mission for mission in errands.values()) or any(
            mission.get("home") for mission in errands.values()):
        return False
    if _should_sell(turn, role, state):
        errands[str(role.unit_id)] = {"goal": "vendor"}
        if _walk_to_zone(turn, role, "vendor", commands):
            return True
        errands.pop(str(role.unit_id), None)
        return False
    if _should_buy(turn, state) and not _metal_needed(turn, role, state):
        errands[str(role.unit_id)] = {"goal": "shop"}
        if _walk_to_zone(turn, role, "weaponShop", commands):
            return True
        errands.pop(str(role.unit_id), None)
    return False


def _metal_needed(turn: Turn, role: Unit, state: dict[str, Any],
                  routes: Any = None) -> bool:
    """True when some worker still owes the day a metal run.

    A dispatched shop walk owns the team's one errand for the rest of the day, so
    it must not be started while any worker could instead be mining: gold in the
    treasury is no use if nobody ever mines the ore that pays for the next
    upgrade. Checking the whole crew matters because the worker that can reach a
    mine is often not the one the errand loop visits first.
    """
    routes = routes if routes is not None else _RouteCost(turn)
    return any(_worker_metal_ready(turn, worker, state, routes.for_role(worker))
               for worker in turn.workers())


def _worker_metal_ready(turn: Turn, role: Unit, state: dict[str, Any],
                        cost_of: Any = None) -> bool:
    """True when this one worker has metal it can mine and later sell."""
    if role.backpack_full:
        # A full bag is not mineable; whether it is sellable is decided by the
        # ordinary errand order in front of the metal run.
        return False
    cost_of = cost_of if cost_of is not None else _RouteCost(turn, role)
    if _vendor_route(turn, role, cost_of) is None:
        # Ore that could never reach a vendor is not worth the trip, so the buy
        # errand keeps its ordinary priority.
        return False
    return _metal_target(turn, role, state, cost_of) is not None


def _worker_day(
    turn: Turn,
    role: Unit,
    sites: tuple[Pos, ...],
    towers_missing: list[Pos],
    walls_missing: list[Pos],
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
    state: dict[str, Any],
    busy: set[int],
    *,
    other_errand: bool = False,
    routes: Any = None,
    metal_claimed: set[Pos] | None = None,
) -> None:
    # Strategy-retired rear targets must not consume a worker's construction turn.
    rear = _retired_rear_walls(turn)
    walls_missing[:] = [p for p in walls_missing if p not in rear]
    planned = sum(
        cmd.get("action") == "build" and cmd.get("name") in TOWER_LOADOUT
        for cmd in commands.values()
    )
    if (towers_missing and available_gold(turn, state, commands) >= WEAPON_BUILD_COST
            and len(turn.weapons()) + planned < 3):
        # Assign types along the defence line, with a quota fallback for old guns.
        built = Counter(unit.kind for unit in turn.weapons())
        built.update(cmd['name'] for cmd in commands.values()
                     if cmd.get('action') == 'build' and cmd.get('name') in TOWER_LOADOUT)
        desired = Counter(TOWER_LOADOUT)
        missing_kind = next((kind for kind in TOWER_LOADOUT if built[kind] < desired[kind]), None)
        if missing_kind is None:
            return
        for index, site in enumerate(sites):
            if site in towers_missing and site not in claimed:
                preferred = TOWER_LOADOUT[index]
                kind = preferred if built[preferred] < desired[preferred] else missing_kind
                _build_or_walk(
                    turn, role, site, kind, claimed, commands,
                )
                towers_missing.remove(site)
                busy.add(role.unit_id)
                return
    # Economy first when standing next to the shop: gold buys towers, upgrades
    # and defensive items, and the vendor price only exists while we are there.
    if _try_trade(turn, role, commands, state):
        busy.add(role.unit_id)
        return
    # The wall ring is the standing priority. While it is unfinished the worker
    # gathers and places stone; only afterwards may it spend the day on ore.
    if walls_missing:
        stones = role.backpack.count(WALL_MATERIAL)
        mine = _adjacent_mine(turn, role)
        if mine is not None and stones < STONE_BATCH and not role.backpack_full:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            busy.add(role.unit_id)
            return
        if stones:
            frame=_TRAFFIC.get()
            chosen=frame.wall_target(role,walls_missing) if frame and frame.turn is turn else None
            candidates=([chosen]+[p for p in walls_missing if p!=chosen]) if chosen else walls_missing
            if frame and frame.turn is turn and frame.enabled and chosen is None:candidates=[]
            for site in candidates:
                if site not in claimed:
                    _build_or_walk(turn, role, site, WALL, claimed, commands)
                    walls_missing.remove(site)
                    busy.add(role.unit_id)
                    return
            return
        _mine(turn, role, claimed, commands)
        busy.add(role.unit_id)
        return
    # Defences stand. Until now `if not walls_missing: return` ended the day
    # here, so a completed defence never earned its upkeep back: the metal
    # branches below were unreachable. A worker carrying metal now walks it to
    # the vendor (the errand ledger owns that leg when it is available);
    # otherwise it gathers the dearest ore the vendor actually buys.
    _mine_metal(turn, role, claimed, commands, state, other_errand=other_errand,
                routes=routes, mine_claimed=metal_claimed)


def _sellable_metals(state: dict[str, Any]) -> dict[str, int]:
    """Current vendor price per ore, ignoring anything it does not buy.

    R06: the acquisition list moves with the news. An ore with no published
    price (or price 0) is never mined — it could not be turned into gold.
    """
    prices = vendor_prices(state)
    return {kind: int(prices[kind]) for kind in ("copper", "iron")
            if int(prices.get(kind, 0)) > 0}


def _metal_carried(role: Unit, state: dict[str, Any]) -> int:
    """Total backpack cells of ores the vendor currently buys."""
    sellable = _sellable_metals(state)
    return sum(1 for item in role.backpack if item in sellable)


def _vendor_cells(turn: Turn) -> list[Pos]:
    """Vendor cells in the published map, sorted for a stable walk target."""
    return sorted((pos for pos, kind in turn.zones.items() if kind == "vendor"),
                  key=lambda pos: (pos.x, pos.y))


def _vendor_route(turn: Turn, role: Unit, cost_of: Any = None) -> tuple[Pos, int] | None:
    """A standable cell next to a vendor and its verified route length, if any.

    ``next_step`` only answers for a goal it can actually reach, so a vendor
    behind terrain, walls or occupied cells reports no route — which is what
    stops a loaded worker from walking at it forever.
    """
    cost_of = cost_of if cost_of is not None else _RouteCost(turn, role)
    best = None
    for vendor in _vendor_cells(turn):
        if distance(role.pos, vendor) <= 1:
            return role.pos, 0
        for stand in _neighbours(vendor):
            if not _free_cell(turn, role, stand):
                continue
            if next_step(turn, role, stand) is None:
                continue
            cost = cost_of(role.pos, stand)
            if best is None or cost < best[1]:
                best = (stand, cost)
    return best


def _free_cell(turn: Turn, role: Unit, pos: Pos) -> bool:
    """True when `pos` is land and nothing stands on it (the role itself aside).

    `_stand_cells` filters by `claimed`, which is empty when a candidate is
    evaluated ahead of time. A goal that is already occupied (an existing tower,
    a wall, a robot) can never be reached — `next_step` refuses it — so ranking
    candidates by route length must skip those cells first.
    """
    return turn.land(pos) and pos not in turn.blocked(role)


def _route_cost(turn: Turn, start: Pos, goal: Pos, moving: Unit | None = None) -> int:
    """Verified shortest step count between two cells; `big` when unreachable.

    Uses the same bounded search as ``next_step``, so it never claims a route
    through obstacles that the walk itself could not take. A cell is always
    reachable from itself — without that, a goal the role happens to be standing
    on (its own cell, which `blocked` counts as occupied by other movers) would
    be reported as unreachable and a zero-length walk would look impossible.
    """
    if start == goal:
        return 0
    cost = _cost_to_goal(turn, start, goal, turn.width * turn.height * 2, moving)
    return cost if cost < 10 ** 9 else 10 ** 6


class _RouteCost:
    """Per-planning-pass memo of `_route_cost`, so candidate scans stay cheap.

    A planning pass evaluates the same start/goal pairs from several helpers
    (vendor route, mine approach, home leg). Each miss is a bounded A* search, so
    without the memo a worker-rich round could spend seconds on repeated probing
    and risk the 5s response limit (R01).

    Scope rules: one instance is created inside a single `_day` call and is never
    stored in the observation, the planner state or any module global, so it
    cannot outlive the Turn it was built from or leak between requests, maps,
    teams or rounds. Entries are keyed by the moving role as well as the cells,
    because `turn.blocked(role)` depends on which role is moving: a cell occupied
    for one worker may be free for another.
    """

    def __init__(self, turn: Turn, role: Unit | None = None):
        self._turn = turn
        self._role = role
        self._cache: dict[tuple[int, Pos, Pos], int] = {}

    def for_role(self, role: Unit) -> "_RoleRoutes":
        return _RoleRoutes(self, role)

    def __call__(self, start: Pos, goal: Pos) -> int:
        if self._role is None:
            raise TypeError("a role-free memo must be used through for_role()")
        return self.cost(self._role, start, goal)

    def cost(self, role: Unit, start: Pos, goal: Pos) -> int:
        key = (role.unit_id, start, goal)
        cached = self._cache.get(key)
        if cached is None:
            cached = _route_cost(self._turn, start, goal, role)
            self._cache[key] = cached
        return cached


class _RoleRoutes:
    """A role-bound view of one pass's route memo: ``cost(start, goal)``."""

    __slots__ = ("_routes", "_role")

    def __init__(self, routes: _RouteCost, role: Unit):
        self._routes = routes
        self._role = role

    def __call__(self, start: Pos, goal: Pos) -> int:
        return self._routes.cost(self._role, start, goal)


def _mine_approach(turn: Turn, role: Unit, mine: Pos,
                   cost_of: Any = None) -> tuple[Pos, int] | None:
    """A standable, free, *reachable* cell next to `mine` and its route length.

    A cell behind a sealed ring, or one an existing tower already occupies, is
    not an approach: the worker could not walk there, so the trip must not start.
    """
    cost_of = cost_of if cost_of is not None else _RouteCost(turn, role)
    best = None
    for stand in sorted((cell for cell in _stand_cells(turn, role, mine, set())
                         if _free_cell(turn, role, cell)),
                        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y)):
        cost = cost_of(role.pos, stand)
        if cost >= 10 ** 6:
            continue
        if best is None or cost < best[1]:
            best = (stand, cost)
    return best


def _metal_target(turn: Turn, role: Unit, state: dict[str, Any],
                  cost_of: Any = None,
                  excluded: set[Pos] | None = None) -> Pos | None:
    """Closest reachable in-range mine of the dearest ore still worth mining.

    Returns None when no mine is usable, which is what keeps a worker from
    chasing a missing, depleted, unreachable or vendor-less mine.
    """
    cost_of = cost_of if cost_of is not None else _RouteCost(turn, role)
    prices = _sellable_metals(state)
    excluded = excluded or set()
    for material in sorted(prices, key=lambda kind: (-prices[kind], kind)):
        if not _mine_available(turn, material):
            continue
        if role.backpack.count(material) >= METAL_BATCH:
            continue  # This ore already fills a run: sell it before mining more.
        mines = sorted(
            (pos for pos, kind in turn.zones.items()
             if kind == material and pos not in excluded
             and distance(role.pos, pos) <= ORE_MAX_DISTANCE),
            key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
        )
        for mine in mines:
            if _metal_trip_fits(turn, role, mine, state, cost_of) is None:
                continue
            return mine
    return None


def _metal_batch_needed(role: Unit, state: dict[str, Any]) -> int:
    """Ore the worker must still gather before the vendor trip pays off.

    The sale itself is the existing policy's decision (`_should_sell` over the
    published surplus threshold), so the budget must not demand a whole
    `METAL_BATCH` when that policy will sell earlier — a full-batch requirement
    rejected trips that really were profitable and left the worker oscillating
    near the base. `METAL_BATCH` stays the cap: ore already in the bag counts.
    """
    return max(0, min(METAL_BATCH, ECONOMY_MIN_SURPLUS) - _metal_carried(role, state))


def _metal_trip_fits(turn: Turn, role: Unit, mine: Pos, state: dict[str, Any],
                     cost_of: Any = None) -> tuple[Pos, int] | None:
    """The route to a mine, or None when the whole trip does not fit the day.

    Route lengths come from the same bounded A* the walk itself uses, so the
    budget accounts for walls, terrain and occupied cells rather than for a
    straight line. `ORE_MAX_DISTANCE` alone is measured from the role and says
    nothing about how far the base has been left behind; this budget does: walk
    to the mine, gather the rest of a payable batch, walk home, and still be back
    before the dusk hand-off. A trip that does not fit is not started at all,
    which is more conservative than starting it and turning back half-way.
    """
    cost_of = cost_of if cost_of is not None else _RouteCost(turn, role)
    station = turn.station()
    if station is None or _vendor_route(turn, role, cost_of) is None:
        return None
    approach = _mine_approach(turn, role, mine, cost_of)
    if approach is None:
        return None
    stand, to_mine = approach
    # Home is a free cell *next to* the base, never a base cell: the base
    # footprint is a blocked zone, so a route to its own coordinates does not
    # exist and would make every trip look impossible.
    homes = [cell for cell in _stand_cells(turn, role, station.pos, set())
             if _free_cell(turn, role, cell)]
    if not homes:
        return None
    home = min(homes, key=lambda cell: cost_of(stand, cell))
    # One move per round (R02), one ore per collect (R03), plus one round of
    # slack. The budget is measured against the dusk hand-off: the trip must end
    # before the day's economy window does, so `_economy_open` and the evening
    # return keep working exactly as before.
    budget = (130 if _productive_night(turn) else 0) + _work_deadline(turn) - ((turn.round_no - 1) % 130)
    harvest = _metal_batch_needed(role, state)
    if dusk_return.enabled():
        harvest = min(harvest, max(0, budget-to_mine-cost_of(stand,home)-1))
    trip = to_mine + harvest + cost_of(stand, home) + 1
    return approach if trip <= budget else None


def _return_to_station(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
                       routes: Any = None) -> bool:
    """Issue a bounded, legal step back to the station when work disappears.

    This is a fallback for a worker whose public mine vanished or became too
    expensive to finish.  It does not invent a task or walk through the base:
    the destination is a free cell adjacent to the observed station and the
    existing route planner still enforces occupancy, walls and dusk limits.
    """
    station = turn.station()
    if station is None or home_defense.inside(turn, role.pos):
        return False
    routes = routes if routes is not None else _RouteCost(turn, role)
    homes = [cell for cell in _stand_cells(turn, role, station.pos, set())
             if _free_cell(turn, role, cell)]
    homes = [cell for cell in homes if routes(role.pos, cell) < 10 ** 6]
    if not homes:
        return False
    target = min(homes, key=lambda cell: (routes(role.pos, cell), cell.x, cell.y))
    step = _step_toward(turn, role, target, set())
    if step is None:
        return False
    commands[role.unit_id] = move_command(step)
    return True


def _metal_fallback(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
                    state: dict[str, Any], routes: Any = None) -> bool:
    """Sell a stranded load or return home after a mine target disappears.

    A small load is still worth selling when no next mine is available.  If
    the vendor leg no longer fits before the normal hand-off, the safer action
    is a station-adjacent return step rather than standing on a depleted mine.
    """
    if not _economy_open(turn):
        return False
    routes = routes if routes is not None else _RouteCost(turn, role)
    carried = _metal_carried(role, state)
    if carried:
        vendor = _vendor_route(turn, role, routes)
        if vendor is not None:
            stand, outward = vendor
            index = (turn.round_no - 1) % 130
            remaining = (130 if _productive_night(turn) else 0) + _work_deadline(turn) - index
            # Leave one round to execute the eventual sale and one for a safe
            # hand-off.  If this no longer fits, prefer returning home below.
            return_frame = dusk_return.Frame(turn)
            homeward = return_frame.routes(role)[0].get(stand) if return_frame.active else 0
            if homeward is not None and outward + 1 + homeward <= remaining:
                return _walk_to_zone(turn, role, "vendor", commands)
    return _return_to_station(turn, role, commands, routes)


def _mine_metal(turn: Turn, role: Unit, claimed: set[Pos],
                commands: dict[int, dict[str, Any]], state: dict[str, Any],
                *, other_errand: bool = False, routes: Any = None,
                mine_claimed: set[Pos] | None = None) -> bool:
    """Gather a metal batch, or carry a finished one off to the vendor.

    Sell-ready comes first so a full backpack never blocks the sale. The trip is
    bounded by the day's economy window and by a route budget, so the worker is
    never still out at dusk: night defence keeps its crew (R02). While *another*
    worker holds the team's one errand, this worker may still take an
    independent, bounded mine/sale action.  The purchase owner's route and
    budget remain protected by the surrounding TripFrame; this function only
    avoids the old all-workers idle gate.
    """
    if not _economy_open(turn):
        return False
    cost_of = routes.for_role(role) if routes is not None else _RouteCost(turn, role)
    if _vendor_route(turn, role, cost_of) is None:
        # A vendor we cannot walk to makes *new* ore worthless (R06).  A worker
        # already outside still gets the ordinary safe return fallback below.
        return _metal_fallback(turn, role, commands, state, cost_of)
    carrying = _metal_carried(role, state)
    if carrying >= METAL_BATCH:
        # A full batch in hand and no errand ledger to carry it: walk it to the
        # vendor. With the ledger present `_start_errand`/`_errand_mission`
        # already own this leg; this keeps the stateless judge path working.
        # Sell-ready is checked before `backpack_full`, so a full bag still walks.
        return _walk_to_zone(turn, role, "vendor", commands)
    if role.backpack_full:
        return False
    # Prefer a mine not already selected by another worker in this round.  If
    # there is only one viable public mine, deliberately fall back to sharing
    # it; refusing to work would be worse than the official shared collection
    # rule and would recreate the old idle behaviour.
    target = _metal_target(turn, role, state, cost_of,
                           excluded=mine_claimed)
    if target is None and mine_claimed:
        target = _metal_target(turn, role, state, cost_of)
    if target is None:
        return _metal_fallback(turn, role, commands, state, cost_of)
    if mine_claimed is not None:
        mine_claimed.add(target)
    if role.pos != target and distance(role.pos, target) <= 1:
        commands[role.unit_id] = collect_command(target)
        claimed.add(target)
        return True
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)
        return True
    return False


def _run_errand(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
                state: dict[str, Any], busy: set[int]) -> bool:
    """Backwards-compatible single-shot errand: sell or buy if already in range.

    Used by callers that do not own mission state; the day loop uses the
    mission-aware ``_start_errand``/``_errand_mission`` pair instead.
    """
    if role.unit_id in commands or role.unit_id in busy:
        return False
    if _try_trade(turn, role, commands, state):
        busy.add(role.unit_id)
        return True
    return False


def _should_sell(turn: Turn, role: Unit, state: dict[str, Any]) -> bool:
    """True when carrying enough surplus that a trip to the vendor pays off.

    A reserve of stone is always kept for wall building and repairs, so the
    economy can never strip the defence of its building material.
    """
    role_state = _role_state(state, role.unit_id)
    if role_state is None:
        return False
    sellable = sellable_inventory(role_state, state)
    if not sellable:
        return False
    stones_needed = len(_missing_wall_sites(turn))
    surplus = 0
    for material, amount in sellable.items():
        if material == WALL_MATERIAL:
            keep = STONE_BATCH if stones_needed else WALL_RESERVE
            surplus += max(0, amount - keep)
        else:
            surplus += amount
    if surplus >= ECONOMY_MIN_SURPLUS:
        return True
    # A verified next-day drop can justify selling a small metal holding now.
    # The existing threshold remains unchanged for all ordinary sale decisions.
    return (any(amount > 0 and material in _sale_signals(turn)
                for material, amount in sellable.items())
            and _forecast_sale_trip_fits(turn, role))


def _forecast_sale_trip_fits(turn: Turn, role: Unit) -> bool:
    """Do not accelerate a speculative errand at the expense of construction or return."""
    if (not _economy_open(turn) or not _towers_done(turn)
            or _missing_wall_sites(turn)
            or any(robot.health > 0 for robot in turn.robots)
            or any(enemy.health > 0 for enemy in turn.enemies)):
        return False
    station = turn.station()
    if station is None:
        return False
    cost = _RouteCost(turn, role)
    vendor = _vendor_route(turn, role, cost)
    if vendor is None:
        return False
    stand, outward = vendor
    homes = [cell for cell in _stand_cells(turn, role, station.pos, set())
             if _free_cell(turn, role, cell)]
    if not homes:
        return False
    home_cost = min(cost(stand, home) for home in homes)
    budget = (130 if _productive_night(turn) else 0) + _work_deadline(turn) - ((turn.round_no - 1) % 130)
    return outward + 1 + home_cost + 1 <= budget


def _should_buy(turn: Turn, state: dict[str, Any]) -> bool:
    """True when gold could buy something the defence still needs.

    A reserve is kept for the next tower/wall so trading never starves defence.
    """
    prices = shop_prices(state)
    if not prices:
        return False
    if len(turn.weapons()) < 3:
        return False
    weapon_budget = upgrade_itinerary.weapon_reserve(turn, state)
    base = turn.station()
    emergency = base is not None and upgrade_itinerary.priority(base)[0] == 0
    if weapon_budget and turn.gold < weapon_budget['gold'] and not emergency:
        return False  # Keep earning; do not walk to the shop just for cheap wall vouchers.
    reserve = WEAPON_BUILD_COST
    if turn.gold < reserve + min(prices.values()):
        return False
    return (turn.round_no - 1) % 130 < _work_deadline(turn)


def _walk_to_zone(turn: Turn, role: Unit, kind: str,
                  commands: dict[int, dict[str, Any]]) -> bool:
    """Step toward the closest free cell adjacent to a neutral zone."""
    cells = [unit.pos for unit in turn.ours if unit.health > 0] + \
            [unit.pos for unit in turn.enemies if unit.health > 0] + \
            [robot.pos for robot in turn.robots if robot.health > 0]
    blocked = set(cells) | {pos for pos, zone_kind in turn.zones.items() if zone_kind != "land"}
    goals = sorted(
        (pos for pos in turn.zones if turn.zones[pos] == kind),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    for goal in goals:
        if distance(role.pos, goal) <= 1:
            # Already in range: the trade branch above owns what to do here.
            return False
        stands = [cell for cell in _neighbours(goal)
                  if turn.land(cell) and cell not in blocked]
        stands.sort(key=lambda cell: (distance(role.pos, cell), cell.x, cell.y))
        for stand in stands:
            if stand == role.pos:
                return False
            step = next_step(turn, role, stand)
            if step is not None:
                commands[role.unit_id] = move_command(step)
                return True
    return False


def _try_trade(turn: Turn, role: Unit, commands: dict[int, dict[str, Any]],
               state: dict[str, Any]) -> bool:
    """Sell surplus material and buy what the defence needs, but only in place.

    Every branch requires the real adjacency the official rules demand (小贩 /
    武器商店 周围一格内), read from the observation. A command that could not
    execute is never sent, so a failure here always means a genuine conflict.
    """
    if role.unit_id in commands:
        return False
    role_state = _role_state(state, role.unit_id)
    if role_state is None:
        return False
    # Selling: at a vendor, keep the stone the walls still need.
    if _adjacent_zone(state, role.pos, "vendor"):
        prices = vendor_prices(state)
        stones_needed = len(_missing_wall_sites(turn))
        signals = _sale_signals(turn)
        for material, amount in sorted(sellable_inventory(role_state, state).items(),
                                       key=lambda entry: (entry[0] not in signals, entry[0])):
            if material == WALL_MATERIAL:
                keep = STONE_BATCH if stones_needed else WALL_RESERVE
                surplus = max(0, amount - keep)
            else:
                surplus = amount
            if surplus > 0 and int(prices.get(material, 0)) > 0:
                commands[role.unit_id] = sell_command(material, surplus)
                return True
    # Buying: only while standing at the weapon shop.
    if _adjacent_zone(state, role.pos, "weaponShop"):
        item = _shop_choice(turn, role, state, commands)
        if item is not None:
            plan = buy_plan(role_state, state, item, available_gold(turn, state, commands))
            if plan is not None:
                _amount, _cost = plan
                commands[role.unit_id] = buy_command(item, 1)
                return True
    return False


def _adjacent_zone(state: dict[str, Any], pos: Pos, kind: str) -> bool:
    """Standing within one cell of a neutral zone of `kind` (R06)."""
    for zone in (state.get("mapInfo") or {}).get("zones") or ():
        if zone.get("neutralType") != kind:
            continue
        cell = zone.get("pos") or {}
        if distance(pos, Pos(int(cell.get("x", -99)), int(cell.get("y", -99)))) <= 1:
            return True
    return False


def _shop_choice(turn: Turn, role: Unit, state: dict[str, Any],
                 commands: dict[int, dict[str, Any]]) -> str | None:
    """Pick the most useful affordable item while standing at the weapon shop.

    Nearby targets use the same emergency-base/weapon/base/wall priority as
    the delivery planner. All defensive purchases respect the next quoted
    weapon voucher reserve; affordability alone cannot promote a cheap wall.
    """
    prices = shop_prices(state)
    gold = available_gold(turn, state, commands)
    planned_items = [cmd.get("name") for cmd in commands.values() if cmd.get("action") == "buy"]

    def affordable(name: str) -> bool:
        price = prices.get(name)
        return price is not None and price <= gold and upgrade_itinerary.purchase_allowed(turn, state, name, commands)

    def investment_priority(building):
        if strategy_config.get()['enabled'] and building.kind == WALL:
            return (5, frontline.wall_tier(turn, building.pos)) + upgrade_itinerary.priority(building)[1:]
        return upgrade_itinerary.priority(building)

    for building in sorted(turn.ours, key=investment_priority):
        if building.kind == WALL and building.pos in _retired_rear_walls(turn):
            continue
        if distance(role.pos, building.pos) > 1:
            continue
        for item in sorted(prices):
            if item in planned_items:
                continue
            if not can_upgrade(item, building.kind, building.level):
                continue
            if affordable(item):
                return item
    # Reagents we can actually use later: they sit in the backpack until needed.
    fallback = []
    if gold >= prices.get(BOMB, 10 ** 9) + 25:
        fallback.append(BOMB)
    if any(unit.health <= 0 for unit in turn.ours):
        fallback.append(MEDICINE)
    if any(unit.kind == WALL and unit.health < 1000 and unit.pos not in _retired_rear_walls(turn) for unit in turn.ours):
        fallback.append(WALL_FIXER)
    for item in fallback:
        if item in planned_items:
            continue
        if affordable(item):
            return item
    return None


def _role_state(state: dict[str, Any], unit_id: int) -> dict[str, Any] | None:
    for role in (state.get("teamOur") or {}).get("roles") or ():
        if role.get("id") == unit_id:
            return role
    return None


def _adjacent_mine(turn: Turn, role: Unit) -> Pos | None:
    if not _mine_available(turn, "stone"):
        return None
    mines = sorted(
        (
            mine for mine in turn.stone_mines()
            if role.pos != mine and distance(role.pos, mine) <= 1
        ),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    return mines[0] if mines else None


def _night_prepare(turn, commands, state, planner_state):
    """Explicit legal night economy; no construction allocation or fake clock.

    Reuse the real procurement contract, budget and final route guard. No gunner
    or repair-return lease owns a role after clearance; actual inventory and HP
    decide whether maintenance still has work. Sunrise can continue a purchase.
    """
    from . import team_trip
    deadline = 130 + _work_deadline(turn)
    frame = team_trip.TripFrame(turn, state, getattr(planner_state, 'team_trips', {}),
                               purchase_deadline=deadline, night_prepare=True)
    _TRIP_FRAME.set(frame)
    claimed = set()
    metal_claimed: set[Pos] = set()
    report = {'phase': 'idle', 'reason': 'no_affordable_upgrade'}
    if frame.purchase:
        proposal, report = upgrade_itinerary.plan(turn, state, commands,
            commitment=frame.purchase, night_prepare=True, deadline=deadline)
        if proposal:
            commands[proposal[0]] = proposal[1]
            frame.stage_purchase(report, proposal)
    # Consumables are already owned; no lease forces a return to a quiet gun.
    serviced = set()
    for role in turn.workers():
        if role.unit_id in commands:
            continue
        for item, target in nightwork._maintenance(turn, role):
            if item in (MEDICINE, WALL_FIXER) and item in role.backpack and target not in serviced:
                commands[role.unit_id] = use_command(item, target)
                serviced.add(target)
                break
        if role.unit_id not in commands and WALL_FIXER in role.backpack:
            candidates = sorted((wall for wall in turn.walls()
                if wall.pos not in _retired_rear_walls(turn) and wall.pos not in serviced
                and wall.health <= .7 * (1000, 1500, 2000)[min(3, max(1, wall.level))-1]),
                key=lambda wall: (nightwork._repair_rank(turn, wall), distance(role.pos, wall.pos), wall.unit_id))
            for wall in candidates:
                step = _step_toward(turn, role, wall.pos, claimed)
                if step is not None:
                    commands[role.unit_id] = move_command(step)
                    claimed.add(step)
                    serviced.add(wall.pos)
                    break
    if frame.purchase is None:
        proposal, report = upgrade_itinerary.plan(turn, state, commands,
            start=0, deadline=deadline, night_prepare=True)
        if proposal:
            commands[proposal[0]] = proposal[1]
            frame.stage_purchase(report, proposal)
    _UPGRADE_REPORT.set(report)
    routes = _RouteCost(turn)
    for role in turn.workers():
        if role.unit_id in commands:
            continue
        if _try_trade(turn, role, commands, state):
            continue
        if _mine_metal(turn, role, claimed, commands, state, routes=routes,
                       mine_claimed=metal_claimed):
            continue
        # Daytime walls still need material, but building is never proposed here.
        if (_missing_wall_sites(turn) and role.backpack.count('stone') < STONE_BATCH
                and not role.backpack_full):
            _mine(turn, role, claimed, commands)
    pioneer = turn.pioneer()
    cycle = getattr(planner_state, 'tasks', {}).get('cycle')
    if pioneer and not (cycle and cycle.phase != 'ended'):
        if _treasure_errand(turn, pioneer, commands, state, None):
            state['_treasureRound'] = turn.round_no


def _night(turn: Turn, commands: dict[int, dict[str, Any]],
           state: dict[str, Any] | None = None, planner_state: Any = None) -> dict | None:
    _SHARED_CONTROL.set(None)
    if _productive_night(turn):
        _night_prepare(turn, commands, state, planner_state)
        return None
    claimed: set[Pos] = set()
    pairs = _tower_pairs(turn)
    # Full planning requires the persistent confirmation window. Isolated legacy
    # helper calls keep the old instantaneous predicate for diagnostic fixtures.
    confine = (not _productive_night(turn) if planner_state is not None else
               not nightwork.field_clear(turn, state))
    if (_STRATEGY['enabled']
            and _STRATEGY['defense']['single_operator_three_rockets']):
        guards=turn.workers()
        operator=min((r.unit_id for r in guards),default=None)
        fixed=next((r for r in guards if r.unit_id==operator),None)
        handover = None
        # A completed pioneer task may take the common post, but only during
        # ordinary combat defence. Day-four full defence deliberately reserves
        # workers for the established gunner/repair policy. The helper preserves
        # the worker's shot until a legal, covered handover exists.
        if not home_defense.full_night(turn):
            eligible, _ = night_gunner.pioneer_status(turn, state, planner_state)
            if eligible:
                handover = night_gunner.plan(turn, state, planner_state, commands,
                                              claimed, _aim_points)
        if handover and handover.get('owner') is not None:
            operator = handover['owner']
            fixed = next((r for r in turn.workers() + (turn.pioneer(),)
                          if r is not None and r.unit_id == operator), None)
            shared = dict(handover)
            shared.setdefault('single_operator', True)
        else:
            shared=_coordinate_rockets(turn,commands,claimed)
        if shared is not None:
            _SHARED_CONTROL.set((turn,shared))
        # The common post may require the public rear opening during daylight.
        # A generic nearest-interior move here would undo that route each turn.
        if (fixed and operator not in commands
                and not any(c.get('controllerId')==str(operator) for c in commands.values())
                and not home_defense.inside(turn,fixed.pos)):
            step=home_defense.step_inside(turn,fixed,claimed=claimed)
            if step is not None:
                commands[operator]=move_command(step);claimed.add(step)
        # A separate worker carries day-purchased spare parts. Even when all
        # rockets cool down, the gunner retains ownership and cannot be drafted.
        memory=deepcopy(getattr(planner_state,'maintenance_state',{}))
        repair=phase_maintenance.night_plan(turn,memory,commands,
            excluded_roles=(() if operator is None else (operator,)),
            config={**_STRATEGY['maintenance'],'enabled':True},payload=state)
        commands.update(repair['commands']);claimed.update(repair['claimed'])
        _PHASE_MAINTENANCE.set((memory,repair))
        owned=set(repair['owned_roles']) | ({operator} if operator is not None else set())
        if not confine:
            extra=nightwork.plan(turn,state,pairs) if state is not None else {}
            for uid,command in extra.items():
                if uid not in owned and uid not in commands:
                    commands[uid]=command
                    if command.get('action')=='move':claimed.add(Pos.load(command['targetPos'][0]))
        for role in turn.controllable():
            if role.unit_id in owned or role.unit_id in commands:
                continue
            reserve_pioneer = getattr(planner_state,'tasks',{}).get('supervisor',{}).get('reserve_pioneer',False)
            if role.kind==PIONEER and not home_defense.full_night(turn) and not reserve_pioneer:
                continue  # existing tasks keep their original first-three-night admission rules
            if not home_defense.inside(turn,role.pos):
                step=home_defense.step_inside(turn,role,claimed=claimed)
                if step is not None:
                    commands[role.unit_id]=move_command(step);claimed.add(step)
        return None
    cycle = getattr(planner_state, 'tasks', {}).get('cycle') if planner_state is not None else None
    committed_task = bool(cycle and cycle.description and cycle.phase != 'ended' and not cycle.ended_round)
    committed_task = committed_task and not home_defense.full_night(turn)
    staging = _treasure_night_staging(turn, state, pairs) if state is not None and not committed_task else None
    extra_work = (nightwork.plan(turn, state, pairs) if not confine else nightwork._front_repair(turn, pairs)) if state is not None and staging is None else {}
    for command in extra_work.values():
        if command.get('action') == 'move':
            claimed.add(Pos.load(command['targetPos'][0]))
    # Confirmed quiet-night jobs may leave; otherwise return before firing outside.
    outside_workers = set()
    for worker in turn.controllable():
        if worker.kind == PIONEER and committed_task:
            continue
        if (confine and turn.station() is not None and not home_defense.inside(turn, worker.pos)
                and worker.unit_id not in extra_work and worker.unit_id not in commands):
            outside_workers.add(worker.unit_id)
            step = home_defense.step_inside(turn, worker, claimed=claimed)
            if step is not None:
                claimed.add(step)
                commands[worker.unit_id] = move_command(step)
    if state is not None:
        if staging is None:
            for uid, command in extra_work.items():
                commands.setdefault(uid, command)
        elif staging['command'] is not None:
            commands.setdefault(staging['owner'], staging['command'])
        if staging is not None and not staging['hold']:
            repair = _guard_access_repair(turn, pairs)
            if repair is not None:
                uid, command = repair
                commands.setdefault(uid, command)
    # Battlefield reagents first: a bomb or a dizzy on a clustered wave is worth
    # more than one extra shot, and items resolve before robot movement (R06).
    if state is not None and _try_battle_items(turn, commands, state):
        pass
    shared = _coordinate_rockets(turn, commands, claimed) if confine else None
    if shared is not None:
        _SHARED_CONTROL.set((turn, shared))
        # A second worker runs the laser while the first rotates the rockets.
        laser = next((g for g in turn.weapons() if g.kind == 'railgun'), None)
        guards = [r for r in turn.workers() if r.unit_id != shared['owner']]
        if shared['active'] and laser is not None and guards:
            guard = min(guards, key=lambda r: (distance(r.pos, laser.pos), r.unit_id))
            pairs = ((guard, laser),)
    for role, tower in pairs:
        if shared and (role.unit_id == shared['owner'] or (shared['active'] and tower.unit_id in shared['weapons'])):
            continue
        if role.unit_id in outside_workers:
            continue
        if role.unit_id in commands:
            continue
        if staging is not None and staging['hold'] and role.unit_id == staging['owner']:
            continue
        if distance(role.pos, tower.pos) <= 1:
            if turn.is_day or tower.cooldown > 0:
                continue
            targets = _aim_points(turn, tower)
            if targets:
                commands[tower.unit_id] = attack_command_multi(role.unit_id, targets)
            continue
        if confine and turn.station() is not None and (role.kind == 'worker' or not committed_task):
            step = home_defense.tower_step(turn, role, tower, claimed)
            if step is not None:
                claimed.add(step)
        else:
            step = _step_toward(turn, role, tower.pos, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
    return staging


def _confirmed_reduced_crew(payload, guards):
    """One worker is explicitly dead; an absent observation is not a death."""
    workers = [role for role in (payload.get('teamOur') or {}).get('roles', [])
               if isinstance(role, dict) and role.get('roleType') == 'worker']
    losses = [role for role in workers
              if type(role.get('health')) is int and role['health'] <= 0]
    return (len(guards) == 1 and guards[0][0].kind == 'worker'
            and len(workers) == 2 and len(losses) == 1)


def _treasure_night_staging(turn, payload, pairs):
    """Prepare a bounded trip while the surviving guards keep their guns.

    No predicted window or wave is consulted. Return None to restore ordinary
    defence immediately; a hold flag is internal arbitration, never an action.
    """
    if home_defense.full_night(turn):
        return None
    robots = payload.get('robot')
    pioneer, base = turn.pioneer(), turn.station()
    if (turn.is_day or (turn.round_no - 1) % 130 == 70 or payload.get('phaseTask')
            or pioneer is None or base is None or base.health < 1000
            or not isinstance(robots, dict) or not isinstance(robots.get('roles'), list)
            or any(robot.health > 0 for robot in turn.robots)
            or any(enemy.health > 0 and enemy.kind in ('worker', 'pioneer')
                   and any(distance(enemy.pos, role.pos) <= 8 for role in turn.controllable())
                   for enemy in turn.enemies)):
        return None
    notes = _treasure_notes(payload, turn)
    if (not notes.get('preparable') or notes.get('taken') or not notes.get('site')
            or not notes.get('items') or Counter(notes['items']) - Counter(pioneer.backpack)):
        return None
    post = next((tower for role, tower in pairs if role.unit_id == pioneer.unit_id), None)
    guards = [(role, tower) for role, tower in pairs if role.unit_id != pioneer.unit_id]
    radius = nightwork.WORK_RADIUS
    # After a confirmed loss, one ready worker can cover a completely cleared
    # field. Missing own-role/health observations are not proof of a death.
    reduced_crew = _confirmed_reduced_crew(payload, guards)
    if (post is None or (len(guards) < 2 and not reduced_crew)
            or distance(pioneer.pos, post.pos) > radius):
        return None
    routes = _RouteCost(turn, pioneer)
    stands = tuple(_stand_cells(turn, pioneer, post.pos, set()))
    return_cost = lambda start: min((routes(start, stand) for stand in stands), default=10 ** 6)
    if return_cost(pioneer.pos) > radius:
        return None
    site = Pos.load(notes['site'])
    goal = min(((routes(pioneer.pos, cell), cell.x, cell.y, cell)
                for cell in _stand_cells(turn, pioneer, site, set())), default=None)
    if goal is None or goal[0] >= 10 ** 6:
        return None
    # Suppressing ordinary nightwork returns all surviving guards before the
    # pioneer departs, and prevents a worker scout leaving during the hold.
    ready = all(distance(role.pos, tower.pos) <= 1 for role, tower in guards)
    result = {'owner': pioneer.unit_id, 'hold': ready, 'command': None,
              'guard_count': len(guards), 'confirmed_reduced_crew': reduced_crew}
    if not ready or goal[0] == 0:
        return result
    step = next_step(turn, pioneer, goal[3])
    if (step is not None and distance(step, post.pos) <= radius
            and return_cost(step) <= radius and routes(step, goal[3]) < goal[0]):
        result['command'] = move_command(step)
    return result


def _guard_access_repair(turn, pairs):
    """Open one own-wall access cell for a guard who cannot reach its gun.

    Called only during verified quiet preparation while the other two roles
    already guard their posts. Existing reachable routes never trigger removal.
    """
    ready = [(role, post) for role, post in pairs if distance(role.pos, post.pos) <= 1]
    if len(ready) < 2:
        return None
    for role, post in pairs:
        if role.kind != 'worker' or distance(role.pos, post.pos) <= 1:
            continue
        routes = _RouteCost(turn, role)
        if any(routes(role.pos, stand) < 10 ** 6 for stand in _stand_cells(turn, role, post.pos, set())):
            continue
        choices = []
        for wall in turn.ours:
            if wall.kind != WALL or wall.health <= 0 or distance(wall.pos, post.pos) != 1:
                continue
            if wall.pos in frontline.protected_walls(turn):
                continue
            for stand in _stand_cells(turn, role, wall.pos, set()):
                cost = routes(role.pos, stand)
                if cost < 10 ** 6:
                    choices.append((cost, wall.pos.x, wall.pos.y, stand.x, stand.y, wall.pos, stand))
        if not choices:
            continue
        chosen = min(choices)
        wall, stand = chosen[-2:]
        if distance(role.pos, wall) == 1:
            return role.unit_id, {'action': 'remove', 'targetPos': [wall.dump()]}
        step = next_step(turn, role, stand)
        if step is not None:
            return role.unit_id, move_command(step)
    return None


def _try_battle_items(turn: Turn, commands: dict[int, dict[str, Any]],
                      state: dict[str, Any]) -> bool:
    """Use a bomb when a 3×3 blast covers several robots and we hold one.

    Only committed when the blast is clearly worth it: the item costs 100 gold
    and the strategy must not spend it on a single small robot.
    """
    from .protocol import BOMB_RADIUS
    best: tuple[int, Pos, Unit] | None = None
    for role in turn.controllable():
        if role.unit_id in commands:
            continue
        role_state = _role_state(state, role.unit_id)
        if not role_state or count_item(role_state, BOMB) <= 0:
            continue
        for robot in turn.robots:
            if robot.health <= 0:
                continue
            hits = sum(1 for other in turn.robots
                       if other.health > 0 and distance(other.pos, robot.pos) <= BOMB_RADIUS)
            if hits < 3:
                continue
            if best is None or hits > best[0]:
                best = (hits, robot.pos, role)
    if best is None:
        return False
    _hits, target, role = best
    commands[role.unit_id] = use_command(BOMB, target)
    return True


def _coordinate_rockets(turn, commands, claimed):
    """Single mode stages a three-gun post; legacy mode shares two rockets."""
    from . import strategy_config
    config = strategy_config.get()
    if config['enabled'] and config['defense']['single_operator_three_rockets']:
        # R01/R04: one worker controls at most one ready rocket per round. A
        # failed approach never licenses a second worker/pioneer to take over.
        workers = turn.workers()
        guns = sorted((g for g in turn.weapons() if g.kind == 'rocket'),
                      key=lambda g: g.unit_id)
        worker = workers[0] if workers else None
        result = {'owner': worker.unit_id if worker else None, 'stand': None,
                  'weapons': [g.unit_id for g in guns], 'active': False,
                  'phase': 'unavailable', 'reason': None, 'single_operator': True}
        if worker is None:
            result['reason'] = 'no_living_worker'
            return result
        if turn.station() is None or not guns:
            result['reason'] = 'base_missing' if turn.station() is None else 'no_rocket'
            return result
        if worker.unit_id in commands:
            result.update(phase='owner_busy', reason='owner_action_preserved')
            return result
        permanent = {p for u in turn.ours + turn.enemies
                     if u.health > 0 and u.kind not in ('worker', 'pioneer')
                     for p in turn.footprint(u)}
        inner = _controller_cells(turn, turn.station().pos, permanent)
        shared = _shared_rocket_cells({g.pos: g.kind for g in guns}, inner)
        from collections import deque
        blocked = turn.blocked(worker) | set(claimed)
        # Daytime staging may use the real rear opening between inner pockets.
        # At night an already-inside operator must not leave the wall ring.
        confined = not turn.is_day and home_defense.inside(turn, worker.pos)
        queue = deque([worker.pos])
        paths = {worker.pos: (None, 0)}
        while queue:
            at = queue.popleft()
            for dx, dy in _NEIGHBOUR_STEPS:
                nxt = Pos(at.x + dx, at.y + dy)
                if (nxt in paths or nxt in blocked or not turn.land(nxt)
                        or (confined and not home_defense.inside(turn, nxt))):
                    continue
                paths[nxt] = (nxt if at == worker.pos else paths[at][0], paths[at][1] + 1)
                queue.append(nxt)
        reachable = [(stand, *paths[stand]) for stand in inner
                     if stand in paths and stand not in claimed]
        common = [row for row in reachable if row[0] in shared]
        result['blocked_control_cells'] = [r.pos.dump() for r in turn.controllable()
            if r.unit_id != worker.unit_id and r.pos in inner
            and any(distance(r.pos, gun.pos) == 1 for gun in guns)]
        result['common_stand_feasible'] = bool(common)
        if not common:
            result['reason'] = ('common_stand_unreachable' if shared else
                                'no_common_inner_stand')
        if shared and worker.pos not in shared:
            if not common:
                result.update(phase='common_stand_blocked',
                              desired_stands=[p.dump() for p in shared])
            else:
                stand, step, length = min(common, key=lambda row: (
                    row[2], row[0].x, row[0].y))
                result.update(stand=stand.dump(), approach_steps=length,
                              phase='moving_to_common_stand')
                if step is not None:
                    commands[worker.unit_id] = move_command(step)
                    claimed.add(step)
                return result
        ready = []
        if not turn.is_day:
            for gun in guns:
                aim = _aim_points(turn, gun) if gun.cooldown == 0 else []
                if not aim:
                    continue
                # Expected immediate useful splash damage from public robots.
                # Cooldown naturally rotates otherwise equal guns; ID breaks ties.
                damage = sum(min(robot.health, sum(
                    20 if target == robot.pos else
                    10 if distance(target, robot.pos) == 1 else 0
                    for target in aim)) for robot in turn.robots if robot.health > 0)
                ready.append((-damage, -gun.level, gun.unit_id, gun, aim))
        adjacent = [row for row in ready if home_defense.inside(turn, worker.pos)
                    and distance(worker.pos, row[3].pos) == 1]
        if adjacent:
            _, _, _, gun, aim = min(adjacent, key=lambda row: row[:3])
            commands[gun.unit_id] = attack_command_multi(worker.unit_id, aim)
            claimed.add(worker.pos)
            result.update(active=True, stand=worker.pos.dump(), phase='firing',
                          firing=gun.unit_id)
            if shared and not common:
                result['fallback'] = 'same_operator_adjacent_fire_common_blocked'
            return result
        if shared and not common:
            return result  # no safe shot: retain the explicit common-post failure
        choices = common
        if not choices:
            ready_positions = [row[3].pos for row in ready]
            useful = ready_positions or [g.pos for g in guns]
            choices = [(p, s, length) for p, s, length in reachable
                       if any(distance(p, gun) == 1 for gun in useful)]
        # Hold a legal inner post throughout a full cooldown window; do not
        # manufacture movement or give the free roles a second firing action.
        if not ready and home_defense.inside(turn, worker.pos):
            claimed.add(worker.pos)
            result.update(active=True, stand=worker.pos.dump(),
                          phase='holding_cooldown_or_no_target')
            return result
        if choices:
            stand, step, length = min(choices, key=lambda row: (
                row[2], row[0].x, row[0].y))
            result.update(stand=stand.dump(), active=step is None,
                          approach_steps=length,
                          phase='moving' if step else 'holding_cooldown_or_no_target')
            if step is not None:
                commands[worker.unit_id] = move_command(step)
                claimed.add(step)
            else:
                claimed.add(stand)
        else:
            result.update(phase='return_blocked', reason='no_reachable_inner_control_cell')
        return result
    base = turn.station()
    if base is None or len(turn.weapons()) != 3:
        return None
    permanent = {p for u in turn.ours + turn.enemies if u.health > 0 and u.kind not in ('worker','pioneer')
                 for p in turn.footprint(u)}
    stands = _shared_rocket_cells({u.pos:u.kind for u in turn.weapons()},
                                  _controller_cells(turn,base.pos,permanent))
    choices = []
    for worker in turn.workers():
        if worker.unit_id in commands or not home_defense.inside(turn, worker.pos):
            continue
        for stand in stands:
            if stand in claimed or (stand != worker.pos and stand in turn.blocked(worker)):
                continue
            step = None if worker.pos == stand else home_defense.step_inside(turn,worker,[stand],claimed)
            if worker.pos == stand or step is not None:
                choices.append((distance(worker.pos,stand),worker.unit_id,stand.x,stand.y,worker,stand,step))
    if not choices:
        return None
    _,_,_,_,worker,stand,step = min(choices,key=lambda row:row[:4])
    guns = sorted((g for g in turn.weapons() if g.kind == 'rocket'),key=lambda g:g.unit_id)
    result = {'owner':worker.unit_id,'stand':stand.dump(),'weapons':[g.unit_id for g in guns],
              'active':worker.pos == stand, 'phase':'moving' if step else 'holding_cooldown_or_no_target'}
    if step:
        commands[worker.unit_id] = move_command(step); claimed.add(step)
    else:
        claimed.add(stand)
        for gun in guns:
            targets = _aim_points(turn,gun) if not turn.is_day and gun.cooldown == 0 else []
            if targets:
                commands[gun.unit_id] = attack_command_multi(worker.unit_id,targets)
                result.update(phase='firing',firing=gun.unit_id)
                break
    return result


def _fill_ready_weapons(turn, commands, excluded):
    """Fill an unserved ready weapon with adjacent free crew after task arbitration.

    A cooling weapon's nominal owner may fire another weapon. One official
    action per role still applies; never replace a use/buy/task action.
    """
    from . import strategy_config
    config = strategy_config.get()
    if turn.is_day or (config['enabled'] and config['defense']['single_operator_three_rockets']):
        return
    context = _SHARED_CONTROL.get()
    shared = context[1] if context and context[0] is turn else None
    excluded = set(excluded)
    reserved = set()
    if shared:
        excluded.add(shared['owner'])
        if shared['active']: reserved.update(shared['weapons'])
    used={str(c.get('controllerId')) for c in commands.values() if c.get('action')=='attack'}
    roles=[r for r in turn.controllable() if r.unit_id not in excluded and str(r.unit_id) not in used
           and (turn.station() is None or home_defense.inside(turn, r.pos))
           and (r.unit_id not in commands or commands[r.unit_id].get('action')=='move')]
    towers=[(t,_aim_points(turn,t)) for t in turn.weapons() if t.cooldown==0 and t.unit_id not in commands and t.unit_id not in reserved]
    towers=[(t,aim) for t,aim in towers if aim]
    best=[]
    def search(index,chosen,claimed):
        nonlocal best
        if index==len(towers):
            if len(chosen)>len(best):best=list(chosen)
            return
        tower,aim=towers[index]
        for role in roles:
            if role.unit_id not in claimed and distance(role.pos,tower.pos)<=1:
                search(index+1,chosen+[(role,tower,aim)],claimed|{role.unit_id})
        search(index+1,chosen,claimed)
    search(0,[],set())
    for role,tower,aim in best:
        commands.pop(role.unit_id,None)
        commands[tower.unit_id]=attack_command_multi(role.unit_id,aim)


def _weapon_readiness(turn, commands):
    rows=[]
    for tower in turn.weapons():
        command=commands.get(tower.unit_id,{})
        nearby=[r.unit_id for r in turn.controllable() if distance(r.pos,tower.pos)<=1]
        if command.get('action')=='attack':reason='attack_issued'
        elif turn.is_day:reason='daytime'
        elif tower.cooldown>0:reason='cooldown'
        elif not _attack_target(turn,tower):reason='no_target_in_range'
        elif not nearby:reason='no_controller_in_range'
        else:reason='controller_claimed_or_unassigned'
        rows.append({'weapon':tower.unit_id,'type':tower.kind,'level':tower.level,
                     'cooldown':tower.cooldown,'nearby_roles':nearby,'reason':reason,
                     'controller':command.get('controllerId')})
    return rows


def _tower_pairs(turn: Turn) -> tuple[tuple[Unit, Unit], ...]:
    roles, towers = turn.controllable(), turn.weapons()
    n = min(len(roles), len(towers))
    if not n:
        return ()
    return min((tuple(zip(rs, ts)) for rs in permutations(roles, n)
                for ts in permutations(towers, n)),
               key=lambda pairs: sum(max(0, distance(r.pos, t.pos)-1) for r, t in pairs))


def _attack_target(turn: Turn, tower: Unit) -> Pos | None:
    """First aim point for a tower: the nearest robot inside its range."""
    reach = tower.range_of_attack()
    targets = [
        robot for robot in turn.robots
        if robot.health > 0 and distance(tower.pos, robot.pos) <= reach
    ]
    if not targets:
        return None
    nearest = min(
        targets,
        key=lambda robot: (distance(tower.pos, robot.pos), robot.robot_id),
    )
    return nearest.pos


def _aim_points(turn: Turn, tower: Unit) -> list[Pos]:
    """Legal aim points for one tower, chosen by the official weapon geometry.

    * **加特林**: the nearest robot first, then the extra bullets go to targets
      whose direction from the tower stays inside 90° of the first one — an
      illegal cone makes the *whole* attack illegal, so this must be checked here
      rather than discovered by the judge.
    * **电磁狙击炮**: one aim point, chosen for the longest penetration line: the
      total damage along the path is what the weapon actually deals.
    * **火箭发射台**: up to level-many aim points, cluster-first (its damage is
      20 centre / 10 splash), unchanged from the local strategy.
    """
    reach = tower.range_of_attack()
    live = [robot for robot in turn.robots
            if robot.health > 0 and distance(tower.pos, robot.pos) <= reach]
    if not live:
        return []
    first = _attack_target(turn, tower)
    if first is None:
        return []
    if tower.kind == "railgun":
        best = None
        for robot in live:
            volley = ballistics.railgun_volley(tower.pos, tower.level, robot.pos,
                                               turn.robots)
            total = sum(hit["damage"] for hit in volley["hits"])
            key = (-total, distance(tower.pos, robot.pos), robot.robot_id)
            if best is None or key < best[0]:
                best = (key, robot.pos)
        return [best[1]] if best else [first]
    if tower.kind == "gatling":
        bullets = max(1, min(tower.level, 3))
        chosen = [first]
        remaining = [robot for robot in live if robot.pos != first]
        # Greedy: each extra bullet goes to the target that adds the most damage
        # while keeping every pair of directions within the cone.
        while len(chosen) < bullets and remaining:
            best = None
            for robot in remaining:
                candidate = chosen + [robot.pos]
                if not ballistics.cone_legal(candidate, tower.pos):
                    continue
                volley = ballistics.gatling_volley(tower.pos, len(candidate), candidate,
                                                   turn.robots)
                gain = sum(shot["damage"] for shot in volley["shots"][len(chosen):])
                key = (-gain, distance(tower.pos, robot.pos), robot.robot_id)
                if best is None or key < best[0]:
                    best = (key, robot)
            if best is None:
                break
            chosen.append(best[1].pos)
            remaining.remove(best[1])
        # An illegal set would make the whole attack illegal, so fall back to the
        # single nearest target rather than sending a command the judge refuses.
        return chosen if ballistics.cone_legal(chosen, tower.pos) else [first]
    # Rocket: cluster-first, up to level-many missiles. Repeat aim points are
    # legal and their damage stacks (任务书 §4.5.4), so a lone target is simply
    # aimed at with every missile.
    bullets = max(1, min(tower.level, 3))
    ranked = sorted(
        live,
        key=lambda robot: (
            -sum(1 for other in live if distance(other.pos, robot.pos) <= 1),
            distance(tower.pos, robot.pos), robot.robot_id,
        ),
    )
    chosen: list[Pos] = []
    for robot in ranked:
        if len(chosen) >= bullets:
            break
        chosen.append(robot.pos)
    while len(chosen) < bullets:
        chosen.append(ranked[0].pos)
    return chosen


def _build_or_walk(
    turn: Turn,
    role: Unit,
    target: Pos,
    name: str,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> None:
    if name == WALL:
        from .coordination import wall_build_seals_role
        if target in _retired_rear_walls(turn):
            return  # Team build policy, not an official restriction on these cells.
        additions={Pos.load(p) for c in commands.values() if c.get('action')=='build'
                   for p in c.get('targetPos',())}
        if strategy_config.get()['enabled'] and wall_build_seals_role(turn,{target},existing_additions=additions):
            return
    if role.pos != target and distance(role.pos, target) <= 1:
        commands[role.unit_id] = build_command(target, name)
        claimed.add(target)
        return
    step = _step_toward(turn, role, target, claimed)
    if step is not None:
        commands[role.unit_id] = move_command(step)


def _mine(
    turn: Turn,
    role: Unit,
    claimed: set[Pos],
    commands: dict[int, dict[str, Any]],
) -> bool:
    if role.backpack_full or not _mine_available(turn, "stone"):
        return False
    mines = sorted(
        (pos for pos in turn.stone_mines() if pos not in claimed),
        key=lambda pos: (distance(role.pos, pos), pos.x, pos.y),
    )
    for mine in mines:
        if role.pos != mine and distance(role.pos, mine) <= 1:
            commands[role.unit_id] = collect_command(mine)
            claimed.add(mine)
            return True
        step = _step_toward(turn, role, mine, claimed)
        if step is not None:
            commands[role.unit_id] = move_command(step)
            return True
    return False


def _step_toward(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    *,
    inside_only: bool = False,
) -> Pos | None:
    frame=_TRAFFIC.get()
    if frame is not None and frame.turn is turn:frame.goal(role,target,inside_only)
    cfg=strategy_config.get()
    if cfg['enabled'] and cfg.get('navigation',{}).get('enabled',False):
        # Select the shortest complete approach, not the nearest-looking stand.
        # Reserve the gunner's post before searching an economic trip so the
        # final post guard does not reverse the worker's business direction.
        from . import rocket_post
        blocked=turn.blocked(role)|set(claimed)
        from .grid import policy_obstacles
        blocked |= policy_obstacles(turn)
        workers=turn.workers()
        if (not _productive_night(turn) and cfg['defense']['single_operator_three_rockets']
                and workers and role.unit_id!=workers[0].unit_id):
            blocked |= rocket_post.common_cells(turn)[0]-{role.pos}
        if inside_only and not turn.is_day and home_defense.inside(turn,role.pos):
            blocked |= {Pos(x,y) for x in range(turn.width) for y in range(turn.height)
                        if not home_defense.inside(turn,Pos(x,y))}
        goals=set(_stand_cells(turn,role,target,claimed,inside_only))-blocked
        route=traffic.path(turn,role.pos,goals,blocked)
        if route and len(route)>1:
            claimed.add(route[1]);return route[1]
        return None
    for stand in _stand_cells(turn, role, target, claimed, inside_only):
        if stand == role.pos:
            return None
        step = next_step(turn, role, stand)
        if step is None or step in claimed:
            continue
        claimed.add(step)
        return step
    return None


def _stand_cells(
    turn: Turn,
    role: Unit,
    target: Pos,
    claimed: set[Pos],
    inside_only: bool = False,
) -> list[Pos]:
    station = turn.station()
    footprint = station_footprint(station.pos) if station else ()
    blocked = turn.blocked(role)
    cells = [
        pos for pos in _neighbours(target)
        if turn.land(pos)
        and pos not in blocked
        and (pos == role.pos or pos not in claimed)
        and (
            not inside_only
            or _footprint_distance(pos, footprint) <= 1
        )
    ]
    cells.sort(key=lambda pos: (distance(role.pos, pos), _footprint_distance(pos, footprint), pos.x, pos.y))
    return cells


def _terrain_key(turn: Turn, base_pos: Pos) -> tuple:
    """A hashable fingerprint of the terrain that can affect this base.

    Only the base's neighbourhood matters to the layout (the ring at radius two,
    the interior at radius one, and the exit flood out to ``WALL_RADIUS + 2``), so
    special zones further away are not part of the key. ``turn.zones`` lists only
    non-default cells in this payload, and the default is land, so the key stays
    tiny. Bounds are part of the key because an opening must stay on the map.
    """
    xmin, xmax, ymin, ymax = defense_layout.footprint_bounds(base_pos)
    horizon = defense_layout.WALL_RADIUS + 2
    special = tuple(sorted(
        ((pos.x, pos.y, kind) for pos, kind in turn.zones.items()
         if kind != "land"
         and max(max(xmin - pos.x, 0, pos.x - xmax),
                 max(ymin - pos.y, 0, pos.y - ymax)) <= horizon),
        key=lambda item: (item[0], item[1], item[2])))
    return (turn.width, turn.height, base_pos.x, base_pos.y, special)


def _defence_geometry(
        turn: Turn) -> tuple[tuple, defense_layout.Layout, defense_layout.InteriorGraph] | None:
    """(cache key, layout, interior graph) for this snapshot, memoised.

    The interior graph is built here rather than inside the tower search so the
    per-combination connectivity check is a walk over at most twelve cells
    instead of a fresh flood of the neighbourhood for every candidate.
    """
    station = turn.station()
    if station is None:
        return None
    standing = frozenset(u.pos for u in turn.walls())
    key = (_terrain_key(turn, station.pos), standing)
    hit = _DEFENCE_CACHE.get(key)
    if hit is not None:
        return key, hit[0], hit[1]
    plan = defense_layout.layout(station.pos, turn.width, turn.height, land=turn.land,
                                 standing_walls=standing)
    graph = defense_layout.interior_graph(station.pos, plan.exit_cells, turn.land)
    if len(_DEFENCE_CACHE) >= _DEFENCE_CACHE_LIMIT:
        _DEFENCE_CACHE.clear()
    _DEFENCE_CACHE[key] = (plan, graph)
    return key, plan, graph


def _defence_layout(turn: Turn) -> defense_layout.Layout | None:
    """The ring/approach/exit plan for this snapshot, or None without a base.

    Standing walls are passed in so the opening is not chosen where an earlier
    layout already built: the exit must stay reachable, not merely be intended.
    """
    geometry = _defence_geometry(turn)
    return geometry[1] if geometry is not None else None


def _tower_sites(turn: Turn) -> tuple[Pos, ...]:
    """Weapon slots, preferring the side the enemy is expected from.

    Existing towers are retained in stable order and never rebuilt for placement:
    this only decides where *new* towers go. Sites come from the assumed weapon
    ring (radius 1, D01); the choice is a **combination search** over the free
    cells rather than a greedy pick, because two things can go wrong at once:

    * a weapon no role can stand next to is close to useless, so every tower
      needs its own reachable controller cell — a contiguous row shares one;
    * our own towers can cut the interior into pockets, trapping the crew inside
      a ring that is otherwise open. So a combination is only accepted when every
      inner cell still reaches the outside through the exit.

    Single-operator mode prioritizes a shared three-rocket post: separate inner
    components are allowed only when every cell still reaches a rear opening.
    Where no such common post exists, prefer the shortest inner control circuit.
    Legacy mode prefers mutually connected inner stands and a shared two-rocket
    post. The approach side is secondary to feasible control; no future wave or
    hidden map data enters the ranking. The pool is at most twelve cells.
    """
    from . import strategy_config
    config = strategy_config.get()
    single = config['enabled'] and config['defense']['single_operator_three_rockets']
    station = turn.station()
    if station is None:
        return ()
    geometry = _defence_geometry(turn)
    if geometry is None:
        return ()
    geo_key, plan, graph = geometry
    permanent = {p for u in turn.ours + turn.enemies
                 if u.health > 0 and u.kind not in ('worker', 'pioneer')
                 for p in turn.footprint(u)}
    fixed = tuple(u.pos for u in turn.weapons())
    sites_key = (geo_key, frozenset(permanent),
                 tuple((u.pos, u.kind) for u in turn.weapons()), TOWER_LOADOUT, single)
    hit = _TOWER_SITES_CACHE.get(sites_key)
    if hit is not None:
        return hit
    guard = defense_layout.exit_guard_cells(station.pos, plan.exit_cells)
    # geo_key is (terrain fingerprint, standing walls); the walls are already in
    # the layout, so the same set is reused as the obstacle set here.
    standing = set(geo_key[1])
    ring = [pos for pos in defense_layout.weapon_cells(station.pos, land=turn.land)
            if pos not in permanent and pos not in fixed]
    need = max(0, 3 - len(fixed))
    stands = _controller_cells(turn, station.pos, permanent)
    best_key: tuple | None = None
    best_sites: list[Pos] = list(fixed)
    def control_cycle_length(sites):
        # Shortest closed inner walk whose visited cells cover all three guns.
        # A common stand costs 0; two adjacent stands cost 2. At most 12 cells
        # and eight coverage masks; no map-specific coordinate enters this rank.
        from collections import deque
        free = set(graph.cells) - set(sites) - standing - permanent
        masks = {p: sum(1 << i for i, gun in enumerate(sites) if distance(p, gun) == 1)
                 for p in free}
        goal = (1 << len(sites)) - 1
        best = 999
        for start in free:
            queue = deque([(start, masks[start], 0)])
            seen = {(start, masks[start])}
            while queue:
                pos, mask, length = queue.popleft()
                if pos == start and mask == goal:
                    best = min(best, length)
                    break
                if length >= best:
                    continue
                for nxt in graph.neighbours.get(pos, ()):
                    if nxt not in free:
                        continue
                    state = (nxt, mask | masks[nxt])
                    if state not in seen:
                        seen.add(state)
                        queue.append((nxt, state[1], length + 1))
        return best
    for size in range(need, -1, -1):
        for combo in combinations(ring, size):
            sites = list(fixed) + list(combo)
            if len(set(sites)) != len(sites):
                continue
            matched = (sum(any(distance(p, tower) == 1 for p in stands - set(sites))
                           for tower in sites) if single else
                       _matched_controller_cells(sites, stands))
            connected, trapped = defense_layout.interior_reachability(
                graph, obstacles=set(sites) | standing)
            ranks: list[int] = []
            corners: list[int] = []
            for pos in combo:
                best_side, corner = defense_layout.side_rank(pos, station.pos,
                                                             plan.side_order)
                ranks.append(best_side)
                corners.append(corner)
            side_key = (tuple(sorted(ranks)), tuple(sorted(corners)))
            spread = min((distance(a, b) for a, b in combinations(sites, 2)), default=0)
            order = tuple((pos.x, pos.y) for pos in combo)
            feasible = matched == len(sites) and connected
            ordered = _ordered_sites(sites, plan.approach)
            intended = dict(zip(ordered, TOWER_LOADOUT))
            mismatch = sum(intended.get(u.pos) != u.kind for u in turn.weapons())
            key = (0 if feasible else 1,
                   -size if feasible else 0,
                   trapped,
                   -matched,
                   mismatch,
                   0 if single or _inner_connected(graph, set(sites) | standing) else 1,
                   0 if _shared_rocket_cells(intended, stands - set(sites)) else 1,
                   control_cycle_length(sites) if single and feasible else 0,
                   sum(defense_layout.side_rank(p, station.pos, plan.side_order)[0]
                       for p, kind in intended.items() if kind == 'rocket'),
                   sum(distance(a, b) < 2 for a, b in combinations(sites, 2)),
                   len(sites) - len({p.y if plan.approach in ('E', 'W') else p.x for p in sites}),
                   sum(defense_layout.side_rank(p, station.pos, plan.side_order)[0]
                       for p, kind in intended.items() if kind == 'railgun'),
                   sum(1 for pos in combo if pos in guard),
                   side_key,
                   -spread,
                   order)
            if best_key is None or key < best_key:
                best_key, best_sites = key, sites
    result = _ordered_sites(best_sites[:3], plan.approach)
    if len(_TOWER_SITES_CACHE) >= _TOWER_SITES_CACHE_LIMIT:
        _TOWER_SITES_CACHE.clear()
    _TOWER_SITES_CACHE[sites_key] = result
    return result


def _ordered_sites(sites, approach):
    """Across the front: bottom-to-top for E/W, left-to-right for N/S."""
    return tuple(sorted(sites, key=lambda p: (p.y, p.x) if approach in ('E', 'W') else (p.x, p.y)))


def _inner_connected(graph, blocked):
    # Reaching separate rear gateways is not enough: guards may not walk
    # outside the walls just to get from one interior post to another.
    free = set(graph.cells) - set(blocked)
    if not free:
        return False
    pending = [next(iter(free))]
    seen = set(pending)
    while pending:
        for cell in graph.neighbours.get(pending.pop(), ()):
            if cell in free and cell not in seen:
                seen.add(cell); pending.append(cell)
    return seen == free


def _shared_rocket_cells(intended, stands):
    from . import strategy_config
    rockets = [p for p, kind in intended.items() if kind == 'rocket']
    config = strategy_config.get()
    count = 3 if config['enabled'] and config['defense']['single_operator_three_rockets'] else 2
    if len(rockets) != count:
        return ()
    return tuple(sorted((p for p in stands if all(distance(p, gun) == 1 for gun in rockets)),
                        key=lambda p: (p.x, p.y)))


def _controller_cells(turn: Turn, station_pos: Pos, terrain: set[Pos]) -> set[Pos]:
    """Cells inside the wall ring where a role could stand to control a weapon.

    Only the interior counts: the ring itself is going to be walled, so a
    controller standing there would be outside the finished defence. Buildings
    (including the base) are excluded; towers are removed by the matcher.
    """
    interior = {pos for pos in _cells_at_distance(station_pos, 1) if turn.land(pos)}
    return interior - terrain


def _matched_controller_cells(towers: list[Pos], stands: set[Pos]) -> int:
    """How many towers can get their own distinct standing cell.

    Kuhn's augmenting-path matching over at most three towers and a dozen cells:
    two towers that share a single reachable stand cannot both be operated, and
    counting the maximum matching is what keeps the third tower from silently
    becoming unusable.
    """
    available = stands - set(towers)
    if not available:
        return 0
    adjacency = {tower: [cell for cell in _neighbours(tower) if cell in available]
                 for tower in towers}
    matched: dict[Pos, Pos] = {}

    def augment(tower: Pos, seen: set[Pos]) -> bool:
        for cell in adjacency[tower]:
            if cell in seen:
                continue
            seen.add(cell)
            holder = matched.get(cell)
            if holder is None or augment(holder, seen):
                matched[cell] = tower
                return True
        return False

    return sum(1 for tower in towers if augment(tower, set()))


def _retired_rear_walls(turn: Turn) -> set[Pos]:
    """Entire rear row is retired only for the enabled team strategy."""
    return set(frontline.rear_walls(turn)) if strategy_config.get()['enabled'] else set()


def _missing_wall_sites(turn: Turn) -> set[Pos]:
    """Count required locations, never credit unrelated or old rear walls."""
    return set(_wall_order(turn)) - {wall.pos for wall in turn.walls()}


def _wall_order(turn: Turn) -> tuple[Pos, ...]:
    """Build center two, other front two, front corners, then retained flanks.

    The radius-two footprint remains the existing D01 layout assumption.
    Enabled strategy leaves the entire rear row open, including its corners;
    existing rear walls remain obstacles and are not automatically demolished.
    Proven traffic openings remain excluded. Disabled strategy uses the legacy
    perimeter/order and is not subject to the new rear-row policy.
    """
    plan = _defence_layout(turn)
    order = plan.wall_order if plan is not None else ()
    if plan is not None and strategy_config.get()['enabled']:
        # Old rear walls may make the layout pick a missing FRONT wall as its
        # temporary physical exit. It is still an unmet investment target.
        base = turn.station()
        order = tuple(sorted(plan.ring, key=lambda p: defense_layout.wall_priority(
            p,base.pos,turn.width,turn.height)))
    frame = _TRAFFIC.get()
    openings = frame.openings() if frame is not None and frame.turn is turn else set()
    rear = _retired_rear_walls(turn)
    retained = [p for p in order if p not in openings and p not in rear]
    if strategy_config.get()['enabled']:
        retained.sort(key=lambda p: frontline.wall_tier(turn,p))  # stable within tier
    return tuple(retained)


def _exit_cells(turn: Turn) -> tuple[Pos, ...]:
    """The two-cell opening deliberately left in the wall ring."""
    plan = _defence_layout(turn)
    return plan.exit_cells if plan is not None else ()


def _ring_is_sealed(turn: Turn) -> bool:
    """True when the ring around the base has no reachable way out.

    Only used by tests and diagnostics: the strategy always leaves an exit, so
    this should never fire for a completed ring.
    """
    from collections import deque

    station = turn.station()
    pioneer = turn.pioneer()
    if station is None or pioneer is None:
        return False
    gate = set(_exit_cells(turn))
    if not gate or not gate <= {pos for pos in turn.zones if turn.zones[pos] == "land"}:
        return False
    blocked = turn.blocked(pioneer)
    seen = {pioneer.pos}
    queue = deque([pioneer.pos])
    while queue:
        current = queue.popleft()
        if current in gate:
            return False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                nxt = Pos(current.x + dx, current.y + dy)
                if nxt in seen or not turn.land(nxt) or nxt in blocked:
                    continue
                seen.add(nxt)
                queue.append(nxt)
    return True


def _cells_at_distance(station_pos: Pos, radius: int) -> tuple[Pos, ...]:
    footprint = station_footprint(station_pos)
    xs = [pos.x for pos in footprint]
    ys = [pos.y for pos in footprint]
    cells = []
    for x in range(min(xs) - radius, max(xs) + radius + 1):
        for y in range(min(ys) - radius, max(ys) + radius + 1):
            pos = Pos(x, y)
            if pos in footprint:
                continue
            if _footprint_distance(pos, footprint) == radius:
                cells.append(pos)
    return tuple(cells)


def _footprint_distance(pos: Pos, footprint: tuple[Pos, ...]) -> int:
    if not footprint:
        return 0
    return min(distance(pos, cell) for cell in footprint)


def _neighbours(pos: Pos) -> tuple[Pos, ...]:
    return tuple(
        Pos(pos.x + dx, pos.y + dy) for dx, dy in _NEIGHBOUR_STEPS
    )
