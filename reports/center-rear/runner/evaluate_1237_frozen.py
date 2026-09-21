"""Paired training sensitivity: finite task supply, never an official count claim."""
import argparse, collections, hashlib, json, os, subprocess, sys, time
from pathlib import Path

p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True)
p.add_argument('--expected-sha',required=True)
p.add_argument('--candidate',choices=['on','off'],required=True);p.add_argument('--tasks-per-point',type=int,default=3)
p.add_argument('--early-economy',choices=['on','off'],default='off')
p.add_argument('--repair-supply',choices=['on','off'],default='off')
p.add_argument('--task-gold',type=int,required=True)
p.add_argument('--failure-rate',type=float,default=.5)
p.add_argument('--construction-trip',choices=['on','off'],default='off')
p.add_argument('--suite',choices=['training','holdout'],default='training')
p.add_argument('--wall-sustain',choices=['on','off'],required=True)
p.add_argument('--seeds',default='1,19');p.add_argument('--sides',default='challenger,defender')
a=p.parse_args();root=Path(a.source).resolve();out=Path(a.output).resolve()
assert 0<=a.tasks_per_point<=30
assert a.tasks_per_point==3 and a.task_gold==80 and a.failure_rate==.5, 'Pre-registered task physics must not change'
assert all(v=='off' for v in (a.candidate,a.early_economy,a.repair_supply,a.construction_trip,a.wall_sustain)), 'Legacy experimental flags must be off'
assert list(map(int,a.seeds.split(','))) == ([1,19] if a.suite=='training' else [1237]), 'Use pre-registered seeds only'
assert a.sides == 'challenger,defender', 'Both sides are mandatory'
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
assert len(a.expected_sha)==40 and commit==a.expected_sha, 'Frozen source SHA mismatch'
assert not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root,text=True).strip(), 'Tracked source must be clean'
os.environ['COMPETITION_HW_MAINTENANCE_V2']='off'
for name in ('EARLY_NIGHT_TASKS','EARLY_ECONOMY','REPAIR_SUPPLY','CONSTRUCTION_TRIP','WALL_SUSTAIN'):
 os.environ['COMPETITION_HW_'+name]='off'
if out.exists():raise RuntimeError('Refuse to overwrite existing evidence')
out.mkdir(parents=True)
sys.path[:0]=[str(root),str(root/'Demo/CoreGeek/src')]
from bench_support import canonical,digest,save,public_state
from phase_metrics import metric_identity,command_errors,summarize
from frontline_metrics import removal_errors,summarize_frontline
from agent import brain,planner,scenarios,simulator,local_task_sandbox,local_scripted_model,taskworld
from benchmark import audit
from failure_profile import install
os.environ['COMPETITION_HW_EARLY_NIGHT_TASKS']=a.candidate
os.environ['COMPETITION_HW_EARLY_ECONOMY']=a.early_economy
os.environ['COMPETITION_HW_REPAIR_SUPPLY']=a.repair_supply
os.environ['COMPETITION_HW_CONSTRUCTION_TRIP']=a.construction_trip
os.environ['COMPETITION_HW_WALL_SUSTAIN']=a.wall_sustain
os.environ[brain.TASK_AGENT_ENV]='on';os.environ[brain.WORLD_AGENT_ENV]='on'
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
config_identity,require_single_operator=metric_identity()
source_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'Demo/CoreGeek/src/agent').glob('*.py')}
metadata={'maintenance_v2':os.environ.get('COMPETITION_HW_MAINTENANCE_V2','off'),'wall_sustain':a.wall_sustain,'source_commit':commit,'source_hashes':source_hashes,'python':sys.version,'candidate':a.candidate,'early_economy':a.early_economy,'repair_supply':a.repair_supply,
          'failure_rate':a.failure_rate,'construction_trip':a.construction_trip,'task_gold':a.task_gold,'tasks_per_point':a.tasks_per_point,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'reward_profile_sha256':hashlib.sha256(Path(__file__).with_name('reward_profile.py').read_bytes()).hexdigest(),'failure_profile_sha256':hashlib.sha256(Path(__file__).with_name('failure_profile.py').read_bytes()).hexdigest(),'scope':'Local injected unsolved tasks until timeout; not official judging or measured LLM accuracy. Paired failure assignment; successful reward unchanged',
          'evaluation_seeds':list(map(int,a.seeds.split(','))),'evaluation_sides':a.sides.split(','),'suite':a.suite,'heldouts_used':a.suite=='holdout','bench_support_sha256':hashlib.sha256(Path(__file__).with_name('bench_support.py').read_bytes()).hexdigest(),'profile':'observed-seven-days','limit':1300,'pid':os.getpid()}
metadata.update(strategy_config=config_identity,require_single_operator=require_single_operator,source_tracked_clean=True,
                phase_metrics_sha256=hashlib.sha256(Path(__file__).with_name('phase_metrics.py').read_bytes()).hexdigest(),
                frontline_metrics_sha256=hashlib.sha256(Path(__file__).with_name('frontline_metrics.py').read_bytes()).hexdigest())
save(out/'config.json',metadata);summaries=[]
for seed in map(int,a.seeds.split(',')):
 for side in a.sides.split(','):
  planner.reset();state=scenarios.scenario(seed,side,1,profile='observed-seven-days')
  world=state['_demo']['task_world']
  failure_spec=install(state,taskworld,tasks_per_point=a.tasks_per_point,gold_reward=a.task_gold,failure_rate=a.failure_rate)
  task_outcomes=[];active_task=None
  sustain_phases=collections.Counter();sustain_actual=collections.Counter()
  initial=digest(state);trace=[];errors=[];failures=0;times=[];actions=collections.Counter();cash=[];calls=0
  case=out/f'{a.suite}-{seed}-{side}'
  save(case/'failure-assignment.json',failure_spec)
  for _ in range(1300):
   obs=public_state(state); before_memory=json.loads(canonical(planner.state_for(obs).dump())); before_public=json.loads(canonical(obs))
   started=time.perf_counter();response=brain.respond(obs);elapsed=time.perf_counter()-started;times.append(elapsed)
   if len(times)==1 or elapsed>=max(times[:-1]):
    save(case/'slowest-request.json',before_public);save(case/'slowest-planner-before.json',before_memory)
    save(case/'slowest-context.json',{'round':obs['roundNo'],'policy_ms':elapsed*1000,'response':response,'decision_report':brain.decision_report(),'source_commit':commit,'runner_sha256':metadata['runner_sha256']})
   errors.extend(audit(obs,response['roleCommandMap']))
   sustain=(brain._DECISION_REPORT.get() or {}).get('defense_sustain') or {}
   phase_report=brain.decision_report() or {}
   control_failures=command_errors(response['roleCommandMap'],require_single_operator)
   if sustain:sustain_phases[str(sustain.get('phase'))+':'+str(sustain.get('reason'))]+=1
   result=simulator.step(state,external_response=response);state=result['state']
   if sustain.get('worker') is not None:
    uid=str(sustain['worker']);cmd=response['roleCommandMap'].get(uid) or {}
    if state['lastRoundRoleActionResults'].get(uid) is True:
     sustain_actual[str(sustain.get('phase'))+':'+str(cmd.get('action'))]+=1
   task_report=state['_demo'].get('task_report') or {}
   if task_report.get('accepted'):
    active=next(book['active'] for book in state['_demo']['task_world']['points'].values() if book['active'])
    active_task={'id':active.get('fixture_id'),'accepted_round':obs['roundNo'],'injected_failure':active.get('fixture_id') in failure_spec['failure_ids']}
   if task_report.get('ended'):
    task_outcomes.append({**(active_task or {}),'ended_round':obs['roundNo'],'reason':task_report['ended'],'rewards':{k:v for k,v in (task_report.get('rewards') or {}).items() if k not in ('answer','expected')}})
    active_task=None
   if response.get('prompt'):
    calls+=1;state['llmResp']=local_scripted_model.complete(response['prompt'])['answer']
   if response.get('executeCmd'):
    fixture=local_task_sandbox.active_task_fixture(state)
    state['lastCmdResult']=local_task_sandbox.execute(response['executeCmd'],fixture,active=fixture is not None)
   feedback=state['lastRoundRoleActionResults'];failures+=sum(v is False for v in feedback.values())
   for uid,command in response['roleCommandMap'].items():
    if feedback.get(uid) is True:
     actions[command['action']]+=1
     if command['action'] in ('sell','buy','use'):
      cash.append({'round':obs['roundNo'],'role':uid,**command})
   trace.append({'defense_sustain':json.loads(json.dumps(sustain)),'purchase_before':before_memory.get('teamTrips',{}).get('purchase'),'night_lease_before':before_memory.get('sustainMemory',{}).get('lease'),'policy_ms':elapsed*1000,'shop_quote':obs.get('weaponShopList') if any(c.get('action')=='buy' for c in response['roleCommandMap'].values()) else None,'vendor_quote':obs.get('vendorShopList') if any(c.get('action')=='sell' for c in response['roleCommandMap'].values()) else None,'units_before':obs['teamOur']['roles'],'units_after':state['teamOur']['roles'],'gold_before':obs['teamOur']['goldNum'],'upgrade_report':json.loads(json.dumps((brain.decision_report() or {}).get('upgrade_itinerary') or {})),'round':obs['roundNo'],'observation_hash':digest(obs),'response':response,'feedback':feedback,
                 'gold':state['teamOur']['goldNum'],'score':state['teamOur']['totalScore'],'after_hash':digest(public_state(state))})
   trace[-1].update(shared_rocket_control=json.loads(json.dumps(phase_report.get('shared_rocket_control') or {})),
                    maintenance=json.loads(json.dumps(phase_report.get('maintenance') or {})),
                    strategy_config=json.loads(json.dumps(phase_report.get('strategy_config') or config_identity)),
                    maintenance_before=before_memory.get('maintenanceState'),control_errors=control_failures,
                    traffic=json.loads(json.dumps(phase_report.get('traffic') or {})),
                    removal_errors=removal_errors(before_public,response['roleCommandMap']))
   if len(trace)%20==0:save(out/'status.json',{'status':'running','pid':os.getpid(),'heartbeat':time.time(),'seed':seed,'side':side,'round':len(trace)})
   if result['done']:break
  base=next(r for r in state['teamOur']['roles'] if r['roleType']=='station')
  summary={'wall_sustain':a.wall_sustain,'sustain_phases':dict(sustain_phases),'sustain_actual':dict(sustain_actual),'source_commit':commit,'seed':seed,'side':side,'candidate':a.candidate,'early_economy':a.early_economy,'repair_supply':a.repair_supply,'failure_rate':a.failure_rate,'construction_trip':a.construction_trip,'task_gold':a.task_gold,'tasks_per_point':a.tasks_per_point,
           'initial_hash':initial,'rounds':len(trace),'base_hp':base['health'],'score':state['teamOur']['totalScore'],
           'full_survival':len(trace)==1300 and base['health']>0,'execution_failures':failures,'audit_errors':errors,
           'task_outcomes':task_outcomes,'active_task_at_end':active_task,'observed_failed_tasks':sum(t['reason']!='completed' for t in task_outcomes),'observed_task_failure_rate':sum(t['reason']!='completed' for t in task_outcomes)/len(task_outcomes) if task_outcomes else None,
           'successful_actions':dict(actions),'economic_actions':cash,'fixture_llm_calls':calls,'max_policy_ms':1000*max(times),'max_policy_round':times.index(max(times))+1}
  metrics=summarize(trace)
  frontline_metrics=summarize_frontline(trace)
  summary['frontline_metrics']=frontline_metrics
  save(case/'frontline-metrics.json',frontline_metrics)
  summary.update(control_error_count=len(metrics['control_errors']),strategy_config=config_identity,
                 require_single_operator=require_single_operator,phase_metrics=metrics)
  save(case/'phase-metrics.json',metrics)
  save(case/'summary.json',summary);save(case/'trace.json',trace);summaries.append(summary)
  print(canonical({k:v for k,v in summary.items() if k!='economic_actions'}),flush=True)
save(out/'summary.json',summaries)
save(out/'receipt.json',{'status':'complete','source_commit':commit,'case_count':len(summaries),'source_tracked_clean':not subprocess.check_output(['git','status','--porcelain','--untracked-files=no'],cwd=root,text=True).strip(),'runner_sha256':metadata['runner_sha256'],'finished_at':time.time()})
save(out/'status.json',{'status':'completed','pid':os.getpid(),'heartbeat':time.time(),'cases':len(summaries)})
