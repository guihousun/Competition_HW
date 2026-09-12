"""Atomic local issue receipts. Ingest normalized connector snapshots, no tokens."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile

OWNER='guihousun'


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(str(path)+'.lock',os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try:
        os.write(fd,str(os.getpid()).encode()); os.close(fd)
        yield
    finally:
        Path(str(path)+'.lock').unlink()


def save(path,data):
    fd,name=tempfile.mkstemp(dir=path.parent,suffix='.tmp')
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(data,f,ensure_ascii=False,indent=2)
        os.replace(name,path)
    finally:
        if Path(name).exists(): Path(name).unlink()


def ingest(path, snapshot):
    """All pages/comments must be read successfully before ingesting a snapshot.

    Absent issues are NOT deleted (partial search results must not close work).
    Only owner body/comments change the actionable revision, avoiding bot loops.
    """
    if snapshot.get('repository')!='guihousun/Competition_HW':
        raise ValueError('Wrong repository')
    changed=[]
    with lock(path):
        data=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'issues':{}}
        for issue in snapshot['issues']:
            if issue['author']!=OWNER: continue
            number=int(issue['number'])
            if number<1: raise ValueError('Invalid issue number')
            comments=sorted([c for c in issue.get('comments',[]) if c['author']==OWNER and
                             not c.get('body','').startswith('<!-- competition-workflow:')],key=lambda c:int(c['id']))
            source={k:issue.get(k) for k in ('title','body','state')}
            source['comments']=comments
            digest=hashlib.sha256(json.dumps(source,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            old=data['issues'].get(str(number),{})
            if old.get('input_digest')==digest: continue
            row=dict(old,input_digest=digest,source=source,needs_triage=True)
            row.setdefault('stage','new')
            data['issues'][str(number)]=row
            changed.append(number)
        save(path,data)
    return changed


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,default=Path('.workflow/state.json'))
    parser.add_argument('--snapshot',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps({'changed_issues':ingest(args.state,json.loads(args.snapshot.read_text(encoding='utf-8')))}))


if __name__=='__main__': main()
