"""Submission structure from the current task, never answers from examples."""
import hashlib
import json
import posixpath
import re
from .task_workspace import file_reference


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate key')
        result[key]=value
    return result


def _json(text):
    return json.loads(text,object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def derive(text, source):
    """Only explicit format anchors in the task itself, not API response docs."""
    if not isinstance(text,str) or not text or len(text)>131072:
        return None
    shapes=[]
    # Markdown answer sections are also explicit anchors; never harvest an
    # arbitrary JSON object from an API example or the task background.
    anchors=list(re.finditer(r'(?:答案(?:的)?格式|提交(?:答案)?(?:的)?格式|输出格式|answer\s+format|形式)\s*[:：]',text,re.I))
    anchors+=list(re.finditer(r'^#{1,6}\s*(?:提交(?:要求|规则|格式|形式)|答案(?:要求|格式)|输出(?:要求|格式|示例)|answer\s+format)\s*[:：]?\s*$',text,re.I|re.M))
    for anchor in anchors:
        tail=text[anchor.end():anchor.end()+4000].lstrip()
        tail=re.split(r'\n#{1,6}\s',tail,maxsplit=1)[0]
        if not tail.startswith(('{','```')):
            block=re.search(r'```(?:json)?\s*\n\s*(\{)',tail,re.I)
            if block:tail=tail[block.start():]
        tail=re.sub(r'^\x60\x60\x60(?:json)?\s*','',tail,flags=re.I)
        if not tail.startswith('{'):continue
        try:
            sample,_=json.JSONDecoder(object_pairs_hook=_unique).raw_decode(tail)
        except (ValueError, RecursionError):continue
        if not isinstance(sample,dict) or not sample or len(sample)>16:continue
        if any(not isinstance(k,str) or len(k)>80 for k in sample):continue
        types={k:type(v).__name__ for k,v in sample.items()}
        if types not in shapes:shapes.append(types)
    contract={'source_id':source,'source_sha256':hashlib.sha256(text.encode()).hexdigest()}
    if len(shapes)==1:
        return {**contract,'kind':'object','fields':shapes[0]}
    if len(shapes)>1:
        return {**contract,'kind':'ambiguous','fields':{}}
    if re.search(r'JSON\s*(?:对象|object)|(?:对象|object)\s*JSON',text,re.I):
        return {**contract,'kind':'object','fields':{}}
    if re.search(r'(?:返回|提交|输出|return|submit)[^\n。]{0,60}\bJSON\b',text,re.I):
        return {**contract,'kind':'json','fields':{}}
    return None


def for_task(phase_text, documents):
    ref=file_reference(phase_text)
    if not ref:
        return derive(phase_text,'current_task')
    # Most recent observed version of the named task file wins, even if unusable.
    for doc in reversed(documents):
        path=doc.get('path','')
        if (path==ref if ref.startswith('/') else posixpath.basename(path)==ref):
            if doc.get('exit_code')!=0 or doc.get('truncated') or doc.get('verified') is not True:
                return None
            return derive(doc.get('text',''),doc.get('id','current_task_file'))
    return None


def checked_token(documents):
    """Current-generation executed check output only; never cat/source examples."""
    for doc in reversed(documents):
        command=doc.get('command','')
        check=(command.startswith('# task-check/1\n') or
            bool(re.search(r'(?:^|&&|[;\n])\s*(?:\./check|/[^\s;]+/check)(?:\s|$)',command)))
        if not check:continue
        if doc.get('verified') is not True or doc.get('exit_code')!=0 or doc.get('truncated'):
            return None
        text=doc.get('text','')
        if command.startswith('# task-check/1\n'):
            try:
                wrapper=_json(text)
                if wrapper.get('tool')!='task-check/1' or wrapper.get('exit_code')!=0 or wrapper.get('truncated'):
                    return None
                text=wrapper['stdout']
            except (ValueError,TypeError,KeyError,AttributeError,RecursionError):return None
        if re.search(r'\bFAIL(?:ED)?\b|\[FAIL\]',text,re.I):return None
        tokens=re.findall(r'^\s*TOKEN:\s*([A-Za-z0-9_-]{6,128})\s*$',text,re.M)
        if not tokens:
            try:
                parsed=_json(text)
                if isinstance(parsed,dict) and set(parsed)=={'token'} and isinstance(parsed['token'],str):
                    tokens=[parsed['token']]
            except (ValueError,TypeError,RecursionError):pass
        unique=set(tokens)
        if len(unique)==1:
            token=next(iter(unique))
            if re.fullmatch(r'[A-Za-z0-9_-]{6,128}',token) and not re.search(r'example|sample|placeholder|your.?token|token.?here|xxxx',token,re.I):
                return token
        return None
    return None


def validate(answer, contract, documents):
    """Return (accepted, exact_or_normalized_answer, explanation)."""
    if not contract or contract['kind']=='ambiguous':
        return True,answer,'no unambiguous machine-readable contract'
    try:
        value=_json(answer)
    except (ValueError,TypeError,RecursionError):
        value=None
    if contract['kind']=='object' and contract['fields']=={'token':'str'}:
        token=checked_token(documents)
        # Format repair only when the proposed value is the actual check token.
        if token and (answer.strip()==token or value==token):
            return True,json.dumps({'token':token},ensure_ascii=False),'已按本题契约包装当前check实际token'
    try:
        value=_json(answer)
    except (ValueError,TypeError,RecursionError):
        return False,answer,'本题要求合法JSON，不能提交裸token或裸地名；按原题结构修正，不猜结果'
    if contract['kind']=='object':
        if not isinstance(value,dict):
            return False,answer,'本题要求JSON对象，单个数字/字符串不是所需结构；示例数值不是答案'
        fields=contract['fields']
        if fields and set(value)!=set(fields):
            return False,answer,'本题对象字段应为'+','.join(fields)+'；请根据真实数据填写，不得补造'
        for key,kind in fields.items():
            if kind=='NoneType':continue  # null example does not establish a type
            if type(value[key]).__name__!=kind:
                return False,answer,'字段'+key+'应为'+kind+'，请按当前题目和真实数据修正'
    return True,answer,'structure valid; official grading not inferred'


def bad_heredoc_chain(command):
    """Recognize the observed EOF-newline-&& error outside quotes/content."""
    delimiter=None; quote=None; strip_tabs=False
    lines=command.splitlines()
    for index,line in enumerate(lines):
        if delimiter:
            if (line.lstrip('\t') if strip_tabs else line)==delimiter:
                delimiter=None
                following=next((s.lstrip() for s in lines[index+1:] if s.strip()),'')
                if following.startswith('&&'):
                    return True
            continue
        i=0
        while i<len(line):
            ch=line[i]
            if ch=='\\' and quote!="'":
                i+=2;continue
            if quote:
                if ch==quote:quote=None
                i+=1;continue
            if ch in ('"',"'"):quote=ch;i+=1;continue
            if ch=='#' and (i==0 or line[i-1].isspace()):break
            if line[i:i+2]=='<<' and line[i:i+3]!='<<<':
                match=re.match(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1",line[i:])
                if match:
                    # Multiple simultaneous here-docs need a full shell parser;
                    # leave them alone rather than misread the second body.
                    if '<<' not in line[i+len(match[0]):]:
                        delimiter=match[2]
                        strip_tabs=line[i:i+3]=='<<-'
                    break
            i+=1
    return False
