"""Independent public-observation cases prompted by Issues 14–17."""
from copy import deepcopy
import json
import unittest
from legacy_strategy import LegacyStrategyCase

from test_tasks import observation
from agent import brain, planner, tasks, task_lifecycle, policy_supervisor
from agent.protocol import Turn


class AcceptanceTests(LegacyStrategyCase):
    def setUp(self):
        planner.reset()

    def test_cooling_point_cannot_borrow_another_points_readiness(self):
        state=observation()
        state['teamOur']['playerTasks'][0]['coldDownRounds']=20
        second=deepcopy(state['teamOur']['playerTasks'][0])
        second.update(taskPosition={'x':16,'y':5},coldDownRounds=0,taskType='自进化类2')
        state['teamOur']['playerTasks'].append(second)
        state['mapInfo']['zones'].append({'pos':{'x':16,'y':5},'neutralType':'challengerTaskPoint2'})
        response=brain.respond(state)
        self.assertNotEqual(response['roleCommandMap'].get('10011',{}).get('action'),'acceptTask')

    def test_all_cooling_does_not_fall_back_to_ready_map_zones(self):
        state=observation()
        state['teamOur']['playerTasks'][0]['coldDownRounds']=20
        response=brain.respond(state)
        self.assertFalse(any(c['action']=='acceptTask' for c in response['roleCommandMap'].values()))

    def test_missing_task_observation_waits_then_backs_off_without_solver_calls(self):
        accepted=[]
        for n in range(1,21):
            response=brain.respond(observation(round_no=n,timeout_rounds=0))
            if any(c['action']=='acceptTask' for c in response['roleCommandMap'].values()):accepted.append(n)
            self.assertNotIn('executeCmd',response)
            self.assertNotIn('prompt',response)
        self.assertEqual(accepted,[1,7,15])

    def test_explicit_rejection_is_not_a_successful_task(self):
        brain.respond(observation())
        state=observation(round_no=2)
        state['lastRoundRoleActionResults']={'10011':False}
        brain.respond(state)
        memory=planner.state_for(state)
        self.assertIsNone(memory.tasks.get('cycle'))
        self.assertEqual(memory.tasks['acceptance_status']['reason'],'accept_rejected')

    def test_delayed_description_preserves_acceptance_round(self):
        brain.respond(observation(timeout_rounds=0))
        brain.respond(observation(round_no=2,timeout_rounds=0))
        state=observation(round_no=3,phase_task='An unfamiliar task',timeout_rounds=20)
        brain.respond(state)
        cycle=planner.state_for(state).tasks['cycle']
        self.assertEqual(cycle.accepted_round,1)
        self.assertEqual(cycle.timeout_rounds,20)
        self.assertEqual(cycle.description,'An unfamiliar task')

    def test_unknown_deadline_is_unknown(self):
        cycle=tasks.TaskCycle(point={'x':6,'y':5},accepted_round=1)
        self.assertIsNone(cycle.deadline)
        self.assertIsNone(cycle.rounds_left(100))

    def test_backoff_and_submissions_survive_simulator_json_roundtrip(self):
        memory=planner.PlannerState()
        task_lifecycle.defer(memory.tasks,{'x':6,'y':5},5,'not_published')
        cycle=tasks.TaskCycle(point={'x':6,'y':5},accepted_round=1,description='task',timeout_rounds=20)
        cycle.record(3,'answer','fixture');memory.tasks['cycle']=cycle
        restored=planner.PlannerState.load(json.loads(json.dumps(memory.dump())))
        self.assertEqual(restored.tasks['accept_retries'],memory.tasks['accept_retries'])
        self.assertEqual(restored.tasks['cycle'].submissions[0].answer,'answer')


def defence_state(round_no=71,robots=True):
    state=observation(round_no=round_no,phase_task='An unfamiliar ongoing task')
    roles=state['teamOur']['roles']
    next(r for r in roles if r['roleType']=='worker')['pos']={'x':9,'y':9}
    worker=deepcopy(next(r for r in roles if r['roleType']=='worker'))
    worker.update(id=10012,pos={'x':12,'y':10});roles.append(worker)
    for index,(x,y) in enumerate(((9,8),(12,9),(12,11))):
        roles.append({'id':10040+index,'roleType':'rocket','pos':{'x':x,'y':y},
                      'health':1000,'level':1,'cooldown':0,'backpack':[]})
    if robots:
        state['robot']['roles']=[{'id':900,'roleType':'smallRobot','health':40,
                                  'pos':{'x':13,'y':10},'targetTeam':'challenger'}]
    memory=planner.PlannerState()
    memory.tasks['cycle']=tasks.TaskCycle(point={'x':6,'y':5},accepted_round=50,
                                         description=state['phaseTask'],timeout_rounds=100)
    return state,memory


class ArbitrationTests(LegacyStrategyCase):
    def test_threatened_night_reclaims_pioneer_from_task_hold(self):
        state,memory=defence_state()
        next(r for r in state['teamOur']['roles'] if r['roleType']=='station')['health']=1000
        cycle=memory.tasks['cycle']
        result=brain.plan_for_state(state,memory,judge_tasks=False).build()
        self.assertEqual(result['roleCommandMap']['10011']['action'],'move')
        self.assertNotIn('executeCmd',result)
        self.assertNotIn('prompt',result)
        self.assertIs(memory.tasks['cycle'],cycle)  # Paused, not fabricated completion.
        self.assertTrue(memory.tasks['supervisor']['reserve_pioneer'])

    def test_far_opponents_wave_does_not_claim_our_pioneer(self):
        state,memory=defence_state()
        state['robot']['roles'][0].update(targetTeam='defender',pos={'x':40,'y':31})
        result=brain.plan_for_state(state,memory,judge_tasks=False).build()
        self.assertFalse(memory.tasks['supervisor']['reserve_pioneer'])
        self.assertNotEqual(result['roleCommandMap'].get('10011',{}).get('action'),'move')

    def test_dusk_task_cannot_overwrite_return_to_damaged_base(self):
        state,memory=defence_state(round_no=70,robots=False)
        next(r for r in state['teamOur']['roles'] if r['roleType']=='station')['health']=1000
        result=brain.plan_for_state(state,memory,judge_tasks=False).build()
        self.assertEqual(result['roleCommandMap']['10011']['action'],'move')
        self.assertEqual(memory.tasks['supervisor']['mode'],'prepare')

    def test_preview_does_not_mutate_supervisor_or_task_memory(self):
        state,memory=defence_state()
        before=deepcopy(memory.dump())
        brain.plan_for_state(state,memory,commit=False,judge_tasks=False)
        self.assertEqual(memory.dump(),before)


class PressureTests(LegacyStrategyCase):
    def directive(self, state, committed=False):
        turn=Turn.load(state);pairs=brain._tower_pairs(turn)
        tower=next(t for r,t in pairs if r.kind=='pioneer')
        return policy_supervisor.evaluate(turn,state,tower,tower_pairs=pairs,committed_work=committed,
                                          tuning=policy_supervisor.SupervisorTuning(enforce_healthy_base=True))

    def test_intact_base_preserves_funded_day_itinerary(self):
        state,_=defence_state(round_no=70,robots=False)
        self.assertFalse(self.directive(state,True).reserve_pioneer)
        self.assertTrue(self.directive(state,False).reserve_pioneer)

    def test_two_workers_cover_low_observed_pressure_but_not_large_robot(self):
        state,_=defence_state()
        state['robot']['roles'][0]['pos']={'x':20,'y':10}
        self.assertFalse(self.directive(state).reserve_pioneer)
        state['robot']['roles'][0]['health']=500
        self.assertTrue(self.directive(state).reserve_pioneer)

    def test_unknown_target_is_counted_as_pressure(self):
        state,_=defence_state()
        state['robot']['roles'][0].update(pos={'x':30,'y':10},health=800)
        state['robot']['roles'][0].pop('targetTeam')
        self.assertTrue(self.directive(state).reserve_pioneer)

    def test_global_range_does_not_make_opposing_wave_near_our_base(self):
        state,_=defence_state()
        state['robot']['roles'][0].update(targetTeam='defender',pos={'x':40,'y':31})
        for role in state['teamOur']['roles']:
            if role['roleType']=='rocket': role['level']=3
        self.assertFalse(self.directive(state).reserve_pioneer)

    def test_private_treasure_cannot_exempt_pioneer_from_return(self):
        state,memory=defence_state(round_no=70,robots=False)
        memory.tasks.clear();state['phaseTask']=''
        state['_demo']={'treasure':{'site':{'x':5,'y':6},'items':['AcientTablet'],
                                   'opensAt':70,'closesAt':120}}
        brain.plan_for_state(state,memory,judge_tasks=False)
        self.assertTrue(memory.tasks['supervisor']['recommended_reserve'])


class IsolationTests(LegacyStrategyCase):
    def test_preview_backoff_does_not_mutate_live_nested_notes(self):
        state=observation(round_no=10,timeout_rounds=0)
        memory=planner.PlannerState()
        memory.tasks['cycle']=tasks.TaskCycle(point={'x':6,'y':5},accepted_round=1)
        task_lifecycle.defer(memory.tasks,{'x':6,'y':5},1,'earlier')
        before=deepcopy(memory.dump())
        brain.plan_for_state(state,memory,commit=False,judge_tasks=False)
        self.assertEqual(memory.dump(),before)

    def test_reconstruct_running_task_at_second_point_instead_of_first(self):
        state=observation(phase_task='Current task',timeout_rounds=50)
        point=deepcopy(state['teamOur']['playerTasks'][0])
        point.update(taskPosition={'x':16,'y':5},timeoutRounds=25)
        state['teamOur']['playerTasks'].append(point)
        state['mapInfo']['zones'].append({'pos':{'x':16,'y':5},'neutralType':'challengerTaskPoint2'})
        next(r for r in state['teamOur']['roles'] if r['roleType']=='pioneer')['pos']={'x':17,'y':6}
        cycle=tasks.TaskPipeline()._cycle_from_observation(state,Turn.load(state),'challenger')
        self.assertEqual(cycle.point,{'x':16,'y':5})
        self.assertEqual(cycle.timeout_rounds,25)


class ControllerBudgetTests(LegacyStrategyCase):
    def test_explicit_role_action_releases_attack_claim(self):
        from agent.coordination import reconcile
        state,_=defence_state()
        cmds={10040:{'action':'attack','controllerId':'10011','targetPos':[{'x':13,'y':10}]},
              10011:{'action':'submitAnswer','taskAnswer':'answer'}}
        self.assertEqual(reconcile(Turn.load(state),state,cmds),{10011:cmds[10011]})

    def test_two_weapons_cannot_claim_same_controller(self):
        from agent.coordination import reconcile
        state,_=defence_state()
        shot={'action':'attack','controllerId':'10011','targetPos':[{'x':13,'y':10}]}
        self.assertEqual(list(reconcile(Turn.load(state),state,{10040:shot,10041:dict(shot)})),[10040])


class PublishedOfferTests(LegacyStrategyCase):
    def test_explicit_empty_task_list_does_not_invent_map_offers(self):
        planner.reset()
        state=observation();state['teamOur']['playerTasks']=[]
        response=brain.respond(state)
        self.assertFalse(any(c['action']=='acceptTask' for c in response['roleCommandMap'].values()))


class RolloutTests(LegacyStrategyCase):
    def test_unvalidated_healthy_base_risk_is_advice_only(self):
        state,memory=defence_state()
        brain.plan_for_state(state,memory,judge_tasks=False)
        note=memory.tasks['supervisor']
        self.assertEqual(note['mode'],'watch')
        self.assertTrue(note['recommended_reserve'])
        self.assertFalse(note['reserve_pioneer'])
