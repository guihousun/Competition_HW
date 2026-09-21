"""Private local stress fixture: selected tasks remain unsolved until timeout.

This deliberately injected grading failure is NOT an official judge model or
an estimate of actual LLM accuracy. Policy observations contain no failure flag.
The successful-task rewards, public texts, deadlines and quotas are unchanged.
"""
import random
from reward_profile import install as install_rewards


def install(state, taskworld, *, tasks_per_point, gold_reward, failure_rate=.5):
    if not isinstance(failure_rate,(int,float)) or not 0 <= failure_rate <= 1:
        raise ValueError('failure_rate must be 0..1')
    install_rewards(state,taskworld,tasks_per_point=tasks_per_point,gold_reward=gold_reward)
    world=state['_demo']['task_world']
    cases=world['agent_cases']
    count=2*tasks_per_point
    # Balanced assignment over available tasks; actual attempts may be a subset.
    failure_count=int(count*failure_rate+.5)
    order=list(range(count))
    random.Random(world['seed']*16381+41).shuffle(order)
    failed=set(order[:failure_count])
    failure_ids={cases[i]['id'] for i in failed}
    original=getattr(taskworld._grade,'_before_failure_fixture',taskworld._grade)

    def grade(answer,active):
        if active.get('fixture_id') in failure_ids:
            return 0.0
        return original(answer,active)

    grade._before_failure_fixture=original
    taskworld._grade=grade
    return {'mode':'injected_unsolved_until_timeout','requested_failure_rate':failure_rate,
            'assigned_failure_count':failure_count,'available_tasks':count,
            'assigned_failure_rate':failure_count/count if count else None,
            'failure_ordinals':sorted(failed),'failure_ids':sorted(failure_ids)}
