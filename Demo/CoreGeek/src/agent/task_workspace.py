"""Bounded read-only task-file discovery, transported through official executeCmd.

R01/R07; roots are discovery hints, not official paths. No local execution by
the policy. The embedded Python is our own auditable probe, never task code.
"""
import base64
import json
import posixpath
import re
import zlib
from .task_context import COMMAND_LIMIT

ROOTS = ('/tmp/selfEvolutionTask', '/workspace', '/app', '/tmp', '/home')
FILE_CAP = 4500
# The sandbox has Python; interpreter selection does not install anything.
# Nofollow + regular-file checks avoid FIFOs/devices and linked workspaces.
PROBE_SOURCE = r'''
import os,sys,json,time,stat
sys.stdout.reconfigure(encoding="utf-8")
q=json.loads(sys.argv[1]); ref=q["ref"]; roots=q["roots"]
end=time.monotonic()+2; hits=[]; seen=set(); errors=[]; limited=False; count=0
def plain(p):
 p=os.path.abspath(p)
 while p!=os.path.dirname(p):
  if os.path.islink(p): return False
  p=os.path.dirname(p)
 return True
def add(p):
 if p not in hits and plain(p) and os.path.isfile(p): hits.append(p)
if os.path.isabs(ref): add(ref)
else:
 for root in roots:
  if not plain(root): continue
  def failed(e):
   if len(errors)<6: errors.append(str(e)[:200])
  for top,dirs,files in os.walk(root,followlinks=False,onerror=failed):
   if top in seen: dirs[:]=[]; continue
   seen.add(top); count+=1
   if count>800 or time.monotonic()>end:
    limited=True; break
   dirs[:]=sorted(d for d in dirs if not os.path.islink(os.path.join(top,d)))[:32]
   if len(dirs)>=32: limited=True
   if os.path.relpath(top,root).count(os.sep)>=6: dirs[:]=[]; limited=True
   if os.path.basename(ref) in files: add(os.path.join(top,os.path.basename(ref)))
   if len(hits)>4: limited=True; break
  if limited and (count>800 or time.monotonic()>end or len(hits)>4): break
out={"probe":"task-workspace/1","ref":ref,"candidates":hits[:4],"limited":limited,"errors":errors,"files":[]}
if len(hits)==1:
 p=hits[0]; parent=os.path.dirname(p)
 for name in [p]+[os.path.join(parent,n) for n in ("API_DOCS.md","spec.md","check","check.sh","README.md")]:
  if len(out["files"])>=4: break
  if not plain(name) or not os.path.isfile(name): continue
  try:
   fd=os.open(name,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
   with os.fdopen(fd,"rb") as f:
    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): continue
    b=f.read(4501)
   if any(x["path"]==name for x in out["files"]): continue
   out["files"].append({"path":name,"text":b[:4500].decode("utf-8","replace"),"truncated":len(b)>4500})
  except OSError as e:
   if len(errors)<6: errors.append(str(e)[:200])
out["status"]="ambiguous" if len(hits)>1 else "found" if hits else "not_found"
print(json.dumps(out,ensure_ascii=False))
'''.strip()


def file_reference(text):
    """Only an explicit file reference; ambiguous text goes to the general agent."""
    if not isinstance(text, str) or len(text) > 16000:
        return None
    quoted = re.findall(r'[\"\x27`“「]([^\"\x27`”」\n\r]+\.(?:md|txt))[\"\x27`”」]', text, re.I)
    matches = quoted or re.findall(r'(?:/[^\s\x27\"\x60<>，。；、（）()]+|[A-Za-z0-9_][A-Za-z0-9_.-]*)\.(?:md|txt)\b', text, re.I)
    unique = list(dict.fromkeys(matches))
    if len(unique) != 1:
        return None
    ref = unique[0]
    if len(ref) > 240 or any(c in ref for c in '\n\r\0') or '..' in ref.split('/'):
        return None
    return ref


def command_for(ref, roots=ROOTS):
    """Literal JSON data is passed as argv, never interpolated into Python code."""
    import shlex
    data = json.dumps({'ref': ref, 'roots': list(roots)}, ensure_ascii=True, separators=(',', ':'))
    packed = base64.b64encode(zlib.compress(PROBE_SOURCE.encode(), 9)).decode()
    code = "import base64,zlib;exec(zlib.decompress(base64.b64decode(" + repr(packed) + ")))"
    command = ("# task-workspace/1\n"
               "if command -v python3 >/dev/null 2>&1; then P=python3; else P=python; fi\n"
               '"$P" -c ' + shlex.quote(code) + ' ' + shlex.quote(data))
    return command if len(command) <= COMMAND_LIMIT else None


def bootstrap(text):
    ref = file_reference(text)
    return command_for(ref) if ref else None


def virtual_probe(command, files):
    """Recognize only our exact generated command; interpret no arbitrary code."""
    if not command.startswith('# task-workspace/1\n'):
        return None
    import shlex
    try:
        args = shlex.split(command.splitlines()[-1])
        config = json.loads(args[-1])
        if set(config) != {'ref', 'roots'} or config['roots'] != list(ROOTS):
            return None
        ref = config['ref']
        if not isinstance(ref, str) or command_for(ref) != command:
            return None
    except (ValueError, TypeError, KeyError, IndexError):
        return None
    hits = [p for p in sorted(files) if (p == ref if ref.startswith('/') else
            posixpath.basename(p) == ref and any(p.startswith(r.rstrip('/')+'/') for r in ROOTS))]
    result = dict(probe='task-workspace/1', ref=ref, candidates=hits[:4],
                  limited=len(hits)>4, errors=[], files=[],
                  status='ambiguous' if len(hits)>1 else 'found' if hits else 'not_found')
    if len(hits) == 1:
        p = hits[0]
        for name in dict.fromkeys([p]+[posixpath.join(posixpath.dirname(p), n) for n in
                                       ('API_DOCS.md','spec.md','check','check.sh','README.md')]):
            if name in files and len(result['files']) < 4:
                raw = files[name].encode('utf-8')
                result['files'].append(dict(path=name,text=raw[:FILE_CAP].decode('utf-8','replace'),truncated=len(raw)>FILE_CAP))
    return json.dumps(result, ensure_ascii=False)
