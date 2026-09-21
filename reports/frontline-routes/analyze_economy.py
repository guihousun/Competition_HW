import json, argparse
from collections import Counter
from pathlib import Path
parser=argparse.ArgumentParser(description='Read-only economy summary of completed training cases.')
parser.add_argument('--batch',type=Path,default=Path(__file__).parent/'validation/training-b533655')
parser.add_argument('--output',type=Path,default=Path(__file__).parent/'economy-review-data.json')
args=parser.parse_args()
root=Path(__file__).parent
batch=args.batch
data=[]
for d in sorted(batch.iterdir()):
    if not (d/'summary.json').exists():continue
    trace=json.loads((d/'trace.json').read_text(encoding='utf8'))
    summary=json.loads((d/'summary.json').read_text(encoding='utf8'))
    window=[r for r in trace if r['round']<=460]
    days=[r for r in window if (r['round']-1)%130<70]
    deltas=[dict(round=r['round'],delta=r['gold']-r['gold_before']) for r in window if r['gold']!=r['gold_before']]
    economic=[dict(round=r['round'],role=uid,**c) for r in window for uid,c in r['response']['roleCommandMap'].items() if c['action'] in ('buy','sell','use') and r['feedback'].get(str(uid)) is True]
    reasons=Counter((r.get('upgrade_report') or {}).get('reason') for r in days)
    funded=[r for r in days if r['gold_before']>=100 and any(u['roleType']=='rocket' and u['level']<3 for u in r['units_before'])]
    funded_reasons=Counter((r.get('upgrade_report') or {}).get('reason') for r in funded)
    sample=[]
    for reason,_ in funded_reasons.most_common():
        rr=next(r for r in funded if (r.get('upgrade_report') or {}).get('reason')==reason)
        sample.append(dict(round=rr['round'],gold=rr['gold_before'],report=rr.get('upgrade_report'),purchase=rr.get('purchase_before')))
    blocked_ranges=[]
    for r in days:
        if (r.get('upgrade_report') or {}).get('reason')!='return_blocked':continue
        if blocked_ranges and blocked_ranges[-1][1]==r['round']-1:blocked_ranges[-1][1]=r['round']
        else:blocked_ranges.append([r['round'],r['round']])
    row=dict(case=d.name,source=summary['source_commit'],rounds=summary['rounds'],max_gold_first4days=max(r['gold_before'] for r in window),
             income_positive_deltas=sum(max(0,r['delta']) for r in deltas),negative_deltas=-sum(min(0,r['delta']) for r in deltas),
             gold_at_460=window[-1]['gold'],cash_deltas=deltas,economic=economic,
             levels=summary['frontline_metrics']['gun_levels_at_day_boundaries']['day4_daylight_end'],
             daylight_reasons=dict(reasons),funded_daylight_reasons=dict(funded_reasons),samples=sample,
             blocked_ranges=blocked_ranges,
             tasks=[t for t in summary['task_outcomes'] if t['ended_round']<=460])
    data.append(row)
args.output.parent.mkdir(parents=True,exist_ok=True)
args.output.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps([{k:r[k] for k in ('case','source','income_positive_deltas','negative_deltas','gold_at_460','funded_daylight_reasons','blocked_ranges')} for r in data],ensure_ascii=True,indent=2))
