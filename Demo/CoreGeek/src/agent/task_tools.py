"""Internal structured tools; generated commands execute only in the judge sandbox."""
import base64
import json
import re
import shlex
import zlib
from urllib.parse import urlsplit, urlunsplit
from .task_context import COMMAND_LIMIT

HTTP_SOURCE = r'''
import json,sys,time,urllib.request as u,urllib.error as e,urllib.parse as p
sys.stdout.reconfigure(encoding="utf-8")
q=json.loads(sys.argv[1]); headers=q["headers"]; params=q["params"]; aliases={}; attempts=[]; auth="document"
if any(k.lower()=="authorization" and v.lower().startswith("bearer ") for k,v in headers.items()): auth="bearer"
url=q["url"]; part=p.urlsplit(url); endpoint=p.urlunsplit((part.scheme,part.netloc,part.path,"",""))
params=dict(p.parse_qsl(part.query,keep_blank_values=True),**params)
url=p.urlunsplit((part.scheme,part.netloc,p.quote(part.path,safe="/:@%"),"",""))
class NoRedirect(u.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs): return None
opener=u.build_opener(u.ProxyHandler({}),NoRedirect); deadline=time.monotonic()+6
out={"tool":"task-http/1","endpoint":endpoint}
for n in range(3):
 try:
  target=url+"?"+p.urlencode(params)
  request=u.Request(target,headers=headers,method="GET")
  try: response=opener.open(request,timeout=max(.1,min(2,deadline-time.monotonic())))
  except e.HTTPError as error: response=error
  with response:
   status=response.code; raw=response.read(10001)
  body=raw[:10000].decode("utf-8","replace")
  try: data=json.loads(body)
  except ValueError: data=body
  attempts.append({"status":status,"parameters":sorted(params),"auth":auth})
  out.update(status=status,data=data,truncated=len(raw)>10000)
  message=body.lower()
  if n<2 and status==401 and "authorization" in message and "bearer" in message:
   keys=[k for k in headers if k.lower() in ("x-api-key","api-key")]
   if len(keys)==1 and not any(k.lower()=="authorization" for k in headers):
    headers["Authorization"]="Bearer "+headers.pop(keys[0]); auth="bearer"; continue
  if n<2 and status==400:
   import re
   found=re.search(r"missing required parameter\s*:\s*([a-zA-Z_][a-zA-Z0-9_]*)",body,re.I)
   business=[k for k in params if k.lower() not in ("page","size","limit","offset","page_size","per_page")]
   if found and len(business)==1 and found[1] not in params:
    old=business[0]; params[found[1]]=params.pop(old); aliases[old]=found[1]; continue
  break
 except Exception as error:
  out.update(status=None,error=type(error).__name__); break
out["attempts"]=attempts
data=out.get("data")
out["shape"]={"type":type(data).__name__,"keys":list(data)[:30] if isinstance(data,dict) else [],"length":len(data) if isinstance(data,(dict,list)) else None}
out["profile"]={"endpoint":endpoint,"auth":auth,"aliases":aliases} if out.get("status")==200 and not out.get("truncated") else None
print(json.dumps(out,ensure_ascii=False,separators=(",",":")))
'''.strip()

CHECK_SOURCE = r'''
import sys,json,os,stat,subprocess,signal,tempfile
sys.stdout.reconfigure(encoding="utf-8")
p=json.loads(sys.argv[1])["path"]; a=os.path.abspath(p)
while a!=os.path.dirname(a):
 if os.path.islink(a): raise ValueError("linked check path")
 a=os.path.dirname(a)
fd=os.open(p,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_NONBLOCK",0))
with os.fdopen(fd,"rb") as f:
 if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError("not a regular script")
 raw=f.read(65537)
if len(raw)>65536: raise ValueError("check script too large")
text=raw.decode("utf-8"); first=text.splitlines()[0] if text else ""
allowed={"#!/bin/sh":["sh"],"#!/bin/bash":["bash"],"#!/usr/bin/env bash":["bash"],"#!/usr/bin/env python3":["python3"],"#!/usr/bin/python3":["python3"]}
cmd=allowed.get(first)
if cmd is None: raise ValueError("unsupported check interpreter")
of=tempfile.TemporaryFile(); ef=tempfile.TemporaryFile()
proc=subprocess.Popen(cmd,cwd=os.path.dirname(p),stdin=subprocess.PIPE,stdout=of,stderr=ef,start_new_session=True)
try:
 proc.communicate(text.replace("\r\n","\n").encode(),timeout=6); code=proc.returncode
except subprocess.TimeoutExpired:
 os.killpg(proc.pid,signal.SIGKILL); proc.communicate(timeout=1); code=124
of.seek(0); ef.seek(0); out=of.read(10001); err=ef.read(2001); of.close(); ef.close()
print(json.dumps({"tool":"task-check/1","path":p,"crlf_normalized":b"\r\n" in raw,"exit_code":code,"stdout":out[:10000].decode("utf-8","replace"),"stderr":err[:2000].decode("utf-8","replace"),"truncated":len(out)>10000 or len(err)>2000},ensure_ascii=False))
sys.exit(code if 0<=code<=255 else 1)
'''.strip()


def endpoint(url):
    part=urlsplit(url)
    if (part.scheme not in ('http','https') or not part.hostname or part.username or part.password
            or part.fragment or len(url)>700 or any(c in url for c in '\r\n\0')):
        raise ValueError('invalid task URL')
    return urlunsplit((part.scheme,part.netloc,part.path,'',''))


def command(kind, params):
    if not isinstance(params,dict):
        raise ValueError('tool arguments must be an object')
    if kind=='http':
        if set(params)-{'url','headers','params'} or not isinstance(params.get('url'),str):
            raise ValueError('http requires url, optional headers/params')
        endpoint(params['url'])
        config={'url':params['url'],'headers':params.get('headers',{}),'params':params.get('params',{})}
        for name in ('headers','params'):
            values=config[name]
            if not isinstance(values,dict) or len(values)>12:
                raise ValueError('too many http fields')
            for key,value in values.items():
                if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',key)
                        or not isinstance(value,str) or len(value)>500
                        or any(c in value for c in '\r\n\0')):
                    raise ValueError('http fields must be bounded strings without newlines')
        source=HTTP_SOURCE
    elif kind=='check':
        if set(params)!={'path'} or not isinstance(params['path'],str) or not params['path'].startswith('/'):
            raise ValueError('check requires an absolute script path')
        if len(params['path'])>500 or any(c in params['path'] for c in '\r\n\0'):
            raise ValueError('invalid check path')
        config=params;source=CHECK_SOURCE
    else:
        raise ValueError('unknown structured tool')
    packed=base64.b85encode(zlib.compress(source.encode(),9)).decode()
    code="import base64,zlib;exec(zlib.decompress(base64.b85decode("+repr(packed)+")))"
    text=('# task-'+kind+'/1\nif command -v python3 >/dev/null 2>&1; then P=python3; else P=python; fi\n'
          +'"$P" -c '+shlex.quote(code)+' '+shlex.quote(json.dumps(config,ensure_ascii=True,separators=(',',':'))))
    if len(text)>COMMAND_LIMIT:
        raise ValueError('generated tool command exceeds '+str(COMMAND_LIMIT)+' characters; reduce parameters')
    return text


def http_request(command_text):
    """Only accept our exact generated command as the source of a profile."""
    if not command_text.startswith('# task-http/1\n'):
        return None
    try:
        config=json.loads(shlex.split(command_text.splitlines()[-1])[-1])
        return config if command('http',config)==command_text else None
    except (ValueError,TypeError,KeyError,IndexError):
        return None


def standalone_check(command_text):
    """Resolve only a simple check invocation with a known absolute cwd/path.

    No mutation chains, shell expansions, redirects, arguments or relative cwd.
    Unknown/complex commands remain byte-for-byte unchanged.
    """
    import posixpath
    try:words=shlex.split(command_text)
    except ValueError:return None
    path=None
    if len(words)==1 and words[0].startswith('/'):
        path=words[0]
    elif len(words)==4 and words[0]=='cd' and words[1].startswith('/') and words[2]=='&&':
        path=posixpath.join(words[1],words[3])
    if not path or any(c in path for c in '$`\n\r\0;&|<>()*?[]{}~!') or '..' in path.split('/'):
        return None
    if posixpath.basename(path) not in ('check','check.sh','check.py'):return None
    return command('check',{'path':posixpath.normpath(path)})


def clean_profile(raw):
    if not isinstance(raw,dict) or set(raw)!={'endpoint','auth','aliases'}:
        raise ValueError('invalid service profile')
    url=raw['endpoint']
    if not isinstance(url,str) or endpoint(url)!=url or raw['auth'] not in ('document','bearer'):
        raise ValueError('invalid endpoint or auth method')
    aliases=raw['aliases']
    if not isinstance(aliases,dict) or len(aliases)>2 or any(
        not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v) for pair in aliases.items() for v in pair):
        raise ValueError('invalid parameter name mapping')
    return {'endpoint':url,'auth':raw['auth'],'aliases':dict(aliases)}
