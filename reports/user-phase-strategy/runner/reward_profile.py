"""Experiment-only reward sensitivity, preserving generated task text/order."""
import random


def install(state, taskworld, *, tasks_per_point, gold_reward):
    if type(tasks_per_point) is not int or not 0 <= tasks_per_point <= 30:
        raise ValueError('tasks_per_point must be 0..30')
    if type(gold_reward) is not int or not 1 <= gold_reward <= 10000:
        raise ValueError('gold_reward must be 1..10000')
    world = state['_demo']['task_world']
    if world.get('generated') or any(p['active'] for p in world['points'].values()):
        raise ValueError('Only initialize fresh games; never rewrite live tasks')
    if world.get('agent_cases'):
        raise ValueError('Do not replace another task suite')
    cases = []
    # At most two task points worth of tasks can be accepted by our team.
    for ordinal in range(max(1, tasks_per_point * 2)):
        rng = random.Random(world['seed'] * 104729 + ordinal * 7717 + 13)
        description, answer = taskworld._payload_for(rng)
        cases.append(dict(id=f'reward-sensitivity-{ordinal}', description=description,
                          answer=answer, grading='fields', timeout_rounds=25,
                          score_reward=50, gold_reward=gold_reward))
    world['agent_cases'] = cases
    for book in world['points'].values():
        book['tasks_left'] = tasks_per_point
    state['teamOur']['playerTasks'] = taskworld.player_tasks(state, world, state['teamOur']['type'])
