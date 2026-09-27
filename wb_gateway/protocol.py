import asyncio, codecs, copy, json, time, os
from .storage import require, Fault, uid

def validate_chat(b):
    require(isinstance(b,dict),'JSON object required')
    require(isinstance(b.get('model'),str) and b['model'].strip(),'model required')
    require(isinstance(b.get('messages'),list) and len(b['messages'])>0,'messages required')
    require('workbuddy' not in b,'Parameters must be set by the client, not saved server presets')
    require('stream' not in b or type(b['stream']) is bool,'stream must be boolean')
    require(type(b.get('n',1)) is int and 1<=b.get('n',1)<=16,'Invalid n')
    for m in b['messages']:
        require(isinstance(m,dict) and m.get('role') in ('system','developer','user','assistant','tool','function'),'Invalid message role')
        require(isinstance(m.get('content'),(str,list)) or (m['role']=='assistant' and m.get('content') is None),'Invalid message content')
        if m['role']=='tool': require(isinstance(m.get('tool_call_id'),str),'tool_call_id required')
        for t in m.get('tool_calls',[]):
            f=t.get('function') or {}; require(t.get('type')=='function' and isinstance(t.get('id'),str) and isinstance(f.get('name'),str) and isinstance(f.get('arguments'),str),'Invalid tool call')
    for t in b.get('tools',[]): require(t.get('type')=='function' and isinstance(t.get('function',{}).get('name'),str),'Only function tools supported')
    if 'max_completion_tokens' in b:
        require('max_tokens' not in b,'Specify only one output token limit'); b['max_tokens']=b.pop('max_completion_tokens')
    if 'max_tokens' in b: require(type(b['max_tokens']) is int and b['max_tokens']>0,'Invalid output token limit')
    if b.get('stream_options'): require(b.get('stream') is True,'stream_options requires stream')
    tc=b.get('tool_choice','auto')
    require(tc in ('auto','none','required') if isinstance(tc,str) else isinstance(tc,dict) and tc.get('type')=='function','Invalid tool_choice')
    if tc=='required' or isinstance(tc,dict): require(bool(b.get('tools')),'Tool choice requires tools')
    f=b.get('response_format',{})
    require(f.get('type','text') in ('text','json_object','json_schema'),'Invalid response_format')
    if f.get('type')=='json_schema': require(isinstance(f.get('json_schema',{}).get('schema'),dict),'JSON schema required')
    return b
def tool_arguments(arguments, name, schema=None, *, status=502, code='invalid_tool_call'):
    # Never execute or silently repair partial model-generated tool payloads.
    try:
        def invalid_constant(value): raise ValueError('Non-finite JSON number')
        payload=json.loads(arguments,parse_constant=invalid_constant)
    except (ValueError,TypeError):
        raise Fault(status,'Tool '+str(name)+' returned incomplete or invalid JSON arguments',code)
    require(isinstance(payload,dict),'Tool '+str(name)+' arguments must be a JSON object',status,code)
    if isinstance(schema,dict):
        missing=[key for key in schema.get('required',[]) if key not in payload]
        require(not missing,'Tool '+str(name)+' is missing required arguments: '+', '.join(missing),status,code)
        for key,rule in schema.get('properties',{}).items():
            if key not in payload or not isinstance(rule,dict): continue
            types=rule.get('type'); types=[types] if isinstance(types,str) else types
            if not isinstance(types,list): continue
            value=payload[key]
            checks={'string':isinstance(value,str),'object':isinstance(value,dict),'array':isinstance(value,list),'integer':type(value) is int,'number':type(value) in (int,float),'boolean':type(value) is bool,'null':value is None}
            require(any(checks.get(t,True) for t in types),'Tool '+str(name)+' argument '+key+' has an invalid type',status,code)
    return payload

def prepare(chat,region):
    b=copy.deepcopy(chat)
    for message in b['messages']:
        for call in message.get('tool_calls') or []:
            f=call.get('function') or {}
            tool_arguments(f.get('arguments'),f.get('name'),status=400,code='invalid_tool_history')
    # Native upstream accepts system/user/assistant/tool, not developer.
    # Keep all caller instructions; normalize protocol roles rather than identities.
    system=[];developer=[];conversation=[]
    for message in b['messages']:
        role=message['role']
        if role in ('system','developer'):
            text=message.get('content','')
            if isinstance(text,list):
                require(all(p.get('type')=='text' and isinstance(p.get('text'),str) for p in text),'Instruction messages must contain text')
                text=chr(10).join(p['text'] for p in text)
            (system if role=='system' else developer).append(text)
        else:conversation.append(message)
    if system or developer:
        instructions=chr(10).join(system)
        if developer:
            if instructions:instructions+=chr(10)*2+'Additional developer instructions (subject to the system instructions above):'+chr(10)
            instructions+=chr(10).join(developer)
        b['messages']=[{'role':'system','content':instructions}]+conversation
    else:b['messages']=conversation
    if isinstance(b.get('tool_choice'),dict):
        name=b['tool_choice'].get('function',{}).get('name'); tools=[t for t in b.get('tools',[]) if t['function']['name']==name]
        require(len(tools)==1,'Named tool is not present'); b['tools']=tools; b['tool_choice']='required'
    if region=='intl' and b['messages'][0]['role']!='system': b['messages'].insert(0,{'role':'system','content':os.environ.get('WB_DEFAULT_SYSTEM_PROMPT','You are a helpful assistant. Follow the user instructions and use only the tools explicitly provided.')})
    fmt=b.get('response_format',{})
    if fmt.get('type') in ('json_object','json_schema'):
        text='Return only valid JSON, without markdown fences or commentary.'
        if fmt['type']=='json_schema': text+=' Follow this JSON schema: '+json.dumps(fmt['json_schema']['schema'],ensure_ascii=False)
        b['messages'].insert(0,{'role':'system','content':text})
    b['stream']=True; b['stream_options']={'include_usage':True}; return b
def contract(value,request):
    declared={t['function']['name']:t['function'] for t in request.get('tools',[]) if t.get('type')=='function'}
    for c in value['choices']:
        m=c['message']; calls=m.get('tool_calls',[]); tc=request.get('tool_choice')
        if c['finish_reason'] in ('length','content_filter'):
            require(not calls,'Tool generation ended before completion; no tool call was completed',502,'incomplete_tool_call')
            continue
        for call in calls:
            f=call['function'];name=f['name']
            require(name in declared,'Upstream returned an undeclared tool: '+str(name),502,'invalid_tool_call')
            tool_arguments(f.get('arguments'),name,declared[name].get('parameters'))
        if tc=='required' or isinstance(tc,dict): require(bool(calls),'Upstream did not honor required tool choice',502,'tool_choice_not_honored')
        if tc=='none': require(not calls,'Upstream returned prohibited tool calls',502,'tool_choice_not_honored')
        if isinstance(tc,dict): require(all(t['function']['name']==tc['function']['name'] for t in calls),'Upstream returned wrong tool',502,'tool_choice_not_honored')
        fmt=request.get('response_format',{}).get('type')
        if fmt in ('json_object','json_schema') and not calls:
            try: parsed=json.loads(m.get('content') or '')
            except ValueError: raise Fault(502,'Upstream did not return valid JSON','structured_output_not_honored')
            if fmt=='json_object': require(isinstance(parsed,dict),'Expected JSON object',502,'structured_output_not_honored')

def responses_to_chat(b):
    from .responses_compat import responses_request
    return validate_chat(responses_request(b))

async def read_bounded(content,limit):
    data=bytearray()
    async for part in content.iter_chunked(65536):
        data.extend(part); require(len(data)<=limit,'Upstream response too large',502,'response_too_large')
    return bytes(data)

async def sse_frames(content):
    decoder=codecs.getincrementaldecoder('utf-8')(); buffer=''; data=[]; event='message'; size=0
    async for raw in content.iter_any():
        buffer+=decoder.decode(raw)
        while True:
            indices=[i for i in (buffer.find(chr(10)),buffer.find(chr(13))) if i>=0]
            if not indices: break
            i=min(indices)
            if buffer[i]==chr(13) and i==len(buffer)-1: break
            line=buffer[:i]; step=2 if buffer[i:i+2]==chr(13)+chr(10) else 1; buffer=buffer[i+step:]
            if not line:
                if data: yield event,chr(10).join(data)
                data=[]; event='message'; size=0; continue
            if line.startswith(':'): continue
            size+=len(line.encode()); require(size<=4*1024*1024,'SSE frame too large',502,'frame_too_large')
            field,_,value=line.partition(':'); value=value[1:] if value.startswith(' ') else value
            if field=='data': data.append(value)
            elif field=='event': event=value
        require(len(buffer.encode())+size<=4*1024*1024,'SSE frame too large',502,'frame_too_large')
    buffer+=decoder.decode(b'',final=True)
    for line in (buffer+chr(10)).replace(chr(13),chr(10)).split(chr(10)):
        if not line:
            if data: yield event,chr(10).join(data); data=[]
        elif line.startswith('data:'): data.append(line[5:].lstrip(' '))

def normalize(c,seen):
    require(isinstance(c,dict) and not c.get('error') and c.get('code') in (None,0,200,'0','200'),'Upstream stream error',502,'upstream_error')
    require(isinstance(c.get('choices'),list),'Missing choices',502,'invalid_stream_chunk')
    for ch in c['choices']:
        delta=ch.get('delta'); snapshot=ch.pop('message',None)
        is_snapshot=not delta and isinstance(snapshot,dict)
        d=copy.deepcopy(snapshot if is_snapshot else delta or {}); ch['delta']=d
        for field in ('content','reasoning_content','refusal'):
            if d.get(field) is None:continue
            require(isinstance(d[field],str),'Invalid text delta',502)
            key=('text',ch.get('index',0),field); previous=seen.get(key,'')
            if is_snapshot:
                require(d[field].startswith(previous),'Completion snapshot conflicts with streamed text',502,'invalid_stream_snapshot')
                full=d[field]; d[field]=full[len(previous):]; seen[key]=full
            else:seen[key]=previous+d[field]
        if 'function_call' in d and not any((d['function_call'] or {}).get(k) for k in ('name','arguments')): del d['function_call']
        for t in (d.get('tool_calls') or []):
            require(type(t.get('index')) is int and 0<=t['index']<1024,'Invalid tool index',502)
            require(t.get('type','function')=='function','Unsupported tool type',502)
            for k in ('name','arguments'):
                if k in t.get('function',{}): require(isinstance(t['function'][k],str),'Invalid tool delta',502)
            key=(ch.get('index',0),t['index'])
            if t.get('id'):
                require(isinstance(t['id'],str) and (key not in seen or seen[key]==t['id']),'Tool ID changed',502,'invalid_tool_call')
                if key in seen: del t['id']
                else: seen[key]=t['id']
    return c
async def chunks(response):
    seen={}; typ=response.headers.get('Content-Type','')
    if 'text/event-stream' in typ:
        done=False
        async for event,data in sse_frames(response.content):
            if data=='[DONE]': done=True; break
            try: c=json.loads(data)
            except ValueError: raise Fault(502,'Malformed SSE JSON','invalid_sse_json')
            if event=='error' or c.get('error') or c.get('code') not in (None,0,200,'0','200'):
                e=Fault(502,'Upstream stream error','upstream_error'); e.upstream_body=c; raise e
            yield normalize(c,seen)
        require(done,'Upstream closed before [DONE]','502' if False else 502,'incomplete_stream')
    else:
        require('json' in typ,'Expected JSON or SSE',502,'invalid_content_type')
        raw=await read_bounded(response.content,32*1024*1024); require(len(raw)<=32*1024*1024,'Response too large',502)
        try: c=json.loads(raw)
        except ValueError: raise Fault(502,'Invalid upstream JSON','invalid_upstream')
        for ch in c.get('choices',[]):
            ch['delta']=ch.pop('message',{})
            for i,t in enumerate(ch['delta'].get('tool_calls',[])): t['index']=i
        c['object']='chat.completion.chunk'; yield normalize(c,seen)
class Accumulator:
    def __init__(self,model): self.value={'id':uid('chatcmpl_'),'object':'chat.completion','created':int(time.time()),'model':model}; self.choices={}; self.bytes=0
    def add(self,c):
        self.bytes+=len(json.dumps(c).encode()); require(self.bytes<=32*1024*1024,'Response too large',502,'response_too_large')
        for k in ('id','created','model','system_fingerprint','service_tier','usage'):
            if k in c: self.value[k]=c[k]
        for ch in c.get('choices',[]):
            i=ch.get('index',0); target=self.choices.setdefault(i,{'index':i,'message':{'role':'assistant','content':None},'finish_reason':None}); m=target['message']; d=ch.get('delta',{})
            for k in ('content','reasoning_content','refusal'):
                if d.get(k) is not None: require(isinstance(d[k],str),'Invalid text delta',502); m[k]=(m.get(k) or '')+d[k]
            for t in (d.get('tool_calls') or []):
                calls=m.setdefault('tool_calls',[]); index=t['index']
                while len(calls)<=index: calls.append(None)
                if calls[index] is None: calls[index]={'id':'','type':'function','function':{'name':'','arguments':''}}
                call=calls[index]
                if t.get('id'): call['id']=t['id']
                for k in ('name','arguments'): call['function'][k]+=t.get('function',{}).get(k,'')
            if d.get('function_call'):
                call=m.setdefault('function_call',{'name':'','arguments':''})
                for k in call: call[k]+=d['function_call'].get(k,'')
            if ch.get('finish_reason') is not None: target['finish_reason']=ch['finish_reason']
            if ch.get('logprobs'): target['logprobs']=ch['logprobs']
    def finish(self):
        self.value['choices']=[self.choices[i] for i in sorted(self.choices)]; require(bool(self.choices),'Empty completion',502,'empty_completion')
        for c in self.value['choices']:
            require(c['finish_reason'] is not None,'Missing finish_reason',502,'incomplete_stream')
            require(all(t and t['id'] and t['function']['name'] for t in c['message'].get('tool_calls',[])),'Incomplete tool call',502,'invalid_tool_call')
        return self.value
from .responses_compat import ResponsesWriter

async def heartbeat_chunks(response,beat,interval=15):
    iterator=chunks(response).__aiter__();task=None
    try:
        while True:
            task=asyncio.create_task(anext(iterator))
            while not task.done():
                done,_=await asyncio.wait([task],timeout=interval)
                if not done:await beat()
            try:value=task.result()
            except StopAsyncIteration:break
            yield value
    finally:
        if task and not task.done():
            task.cancel()
            try:await task
            except (asyncio.CancelledError,StopAsyncIteration):pass
        await iterator.aclose()
