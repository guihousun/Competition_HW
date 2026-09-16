"""Literal field descriptions from a current task; never a stored answer bank."""
import json
import re


def template_json(text):
    """Remove comments/placeholders only OUTSIDE JSON strings, without eval."""
    out=[];i=0;quoted=False;escape=False
    while i<len(text):
        char=text[i]
        if quoted:
            out.append(char)
            if escape:escape=False
            elif char=='\\':escape=True
            elif char=='"':quoted=False
            i+=1;continue
        if char=='"':quoted=True;out.append(char);i+=1;continue
        if text[i:i+2]=='//':
            end=text.find('\n',i);i=len(text) if end<0 else end;continue
        if char=='<':
            end=text.find('>',i+1)
            if end>i and end-i<=202 and '\n' not in text[i:end]:
                out.append('null');i=end+1;continue
        out.append(char);i+=1
    return ''.join(out)


def descriptions(text, fields):
    found={};conflicts=[]
    def add(key, value):
        value=value.strip().strip('<>').strip()
        if key not in fields or not value:return
        value=value[:160]
        if key in found and found[key]!=value:conflicts.append(key)
        else:found[key]=value
    for match in re.finditer(r'"([^"\n]{1,80})"\s*:\s*(?:\[\s*)?<([^<>\n]{1,200})>',text):
        add(match[1],match[2])
    for line in text.splitlines():
        # JSON comment after a field: ignore // that belongs to a quoted URL.
        field=re.match(r'\s*"([^"\n]{1,80})"\s*:',line)
        if field:
            quoted=False;escape=False
            for i,c in enumerate(line):
                if quoted:
                    if escape:escape=False
                    elif c=='\\':escape=True
                    elif c=='"':quoted=False
                elif c=='"':quoted=True
                elif line[i:i+2]=='//':add(field[1],line[i+2:]);break
        # Explicit field-definition bullet or Markdown table, not example values.
        m=re.match(r'\s*(?:[-*]\s+)?`?([A-Za-z_][\w]{0,79})`?\s*[:：]\s*(.+)$',line)
        if m:add(m[1],m[2])
        m=re.match(r'\s*\|\s*`?([A-Za-z_][\w]{0,79})`?\s*\|\s*(.+?)\s*\|\s*$',line)
        if m:add(m[1],m[2])
    return found,sorted(set(conflicts))


def record_name_error(answer, contract, documents):
    """Reject only a witnessed compare-value/name confusion, not an unknown answer."""
    targets=[key for key,description in contract.get('semantics',{}).items()
             if re.search(r'(?:最早|最晚|最大|最小|最高|最低|最古老).{0,30}(?:名称|名字)',description)
             and re.search(r'(?:遗产|记录|文物|景点|项目|实体|对象)(?:的)?(?:名称|名字)',description)]
    if not targets:return None
    from .task_tools import http_request
    for doc in reversed(documents):
        command=doc.get('command')
        if not command:continue
        # A newer failed/partial/different tool supersedes older query evidence.
        if (doc.get('verified') is not True or doc.get('exit_code')!=0 or doc.get('truncated')
                or http_request(command) is None):return None
        try:
            result=json.loads(doc['text'])
            if (not isinstance(result,dict) or result.get('tool')!='task-http/1'
                    or result.get('status')!=200 or result.get('truncated')):return None
            data=result.get('data')
            if not isinstance(data,dict):return None
            if data.get('status')=='error' or (type(data.get('code')) is int and data['code']>=400):return None
            data=data.get('data',data)
            if not isinstance(data,dict):return None
            records=data.get('records');page=data.get('pagination')
            if (not isinstance(records,list) or not records or not isinstance(page,dict)
                    or type(page.get('total_count')) is not int or page['total_count']!=len(records)
                    or page.get('offset')!=0 or not all(isinstance(r,dict) and isinstance(r.get('name'),str) for r in records)):
                return None
            names={r['name'] for r in records}
            compare={str(r[k]) for r in records for k in ('era','date','year','period','age') if k in r}
            for key in targets:
                value=answer.get(key)
                if isinstance(value,str) and value not in names and value in compare:
                    return '字段'+key+'按当前题文要求返回记录名称；此值来自年代/日期等比较字段。请按题意排序并返回对应name，不猜测判题答案'
            return None
        except (ValueError,TypeError,KeyError,RecursionError):return None
    return None
