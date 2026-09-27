import copy,json
from .storage import require,Fault,uid
from .protocol import validate_chat

def anthropic_error(exc,rid=None):
    status=getattr(exc,'status',500)
    typ={400:'invalid_request_error',401:'authentication_error',403:'permission_error',404:'not_found_error',413:'request_too_large',429:'rate_limit_error',529:'overloaded_error'}.get(status,'api_error')
    return {'type':'error','error':{'type':typ,'message':str(exc) if isinstance(exc,Fault) else 'Internal request failure'},'request_id':rid or uid('req_')}

def anthropic_to_chat(b):
    require(isinstance(b,dict),'JSON object required')
    require(isinstance(b.get('messages'),list) and b['messages'],'messages required')
    out={k:copy.deepcopy(b[k]) for k in ('model','stream','max_tokens','temperature','top_p','top_k') if k in b}
    require(type(b.get('max_tokens')) is int and b['max_tokens']>0,'max_tokens required')
    messages=[]
    def parts(value):
        if isinstance(value,str):return [{'type':'text','text':value}]
        require(isinstance(value,list),'Invalid content blocks'); result=[]
        for p in value:
            typ=p.get('type')
            if typ=='text':result.append({'type':'text','text':p.get('text','')})
            elif typ=='image':
                source=p.get('source',{})
                if source.get('type')=='base64':url='data:'+source.get('media_type','image/png')+';base64,'+source.get('data','')
                elif source.get('type')=='url':url=source.get('url','')
                else:raise Fault(400,'Unsupported image source')
                require(bool(url),'Image source required');result.append({'type':'image_url','image_url':{'url':url}})
            else:raise Fault(400,'Unsupported content block: '+str(typ),'unsupported_content')
        return result
    if b.get('system'):
        system=parts(b['system']);require(all(p['type']=='text' for p in system),'system must be text');messages.append({'role':'system','content':chr(10).join(p['text'] for p in system)})
    for m in b['messages']:
        role=m.get('role');require(role in ('user','assistant','system'),'Invalid message role')
        content=m.get('content');blocks=[{'type':'text','text':content}] if isinstance(content,str) else content
        require(isinstance(blocks,list),'Invalid message content')
        text=[];calls=[];results=[]
        for p in blocks:
            typ=p.get('type')
            if typ=='tool_use':
                require(role=='assistant' and isinstance(p.get('id'),str) and isinstance(p.get('name'),str) and isinstance(p.get('input'),dict),'Invalid tool_use')
                calls.append({'id':p['id'],'type':'function','function':{'name':p['name'],'arguments':json.dumps(p['input'],ensure_ascii=False)}})
            elif typ=='tool_result':
                require(role=='user' and isinstance(p.get('tool_use_id'),str),'Invalid tool_result')
                value=parts(p.get('content',''))
                if all(x['type']=='text' for x in value):value=chr(10).join(x['text'] for x in value)
                if p.get('is_error') and isinstance(value,str):value='Tool error: '+value
                results.append({'role':'tool','tool_call_id':p['tool_use_id'],'content':value})
            elif typ in ('thinking','redacted_thinking'):require(role=='assistant','Thinking only allowed in assistant history')
            else:text.extend(parts([p]))
        messages.extend(results)
        if text or calls:
            item={'role':role,'content':text or None}
            if calls:item['tool_calls']=calls
            messages.append(item)
    out['messages']=messages
    if b.get('stop_sequences'):out['stop']=b['stop_sequences']
    if 'tools' in b:
        tools=[]
        for t in b['tools']:
            require(t.get('type','custom')=='custom','Server-hosted tools are not available; use client tools',400,'unsupported_tool')
            require(isinstance(t.get('name'),str),'Tool name required')
            tools.append({'type':'function','function':{'name':t['name'],'description':t.get('description',''),'parameters':t.get('input_schema',{'type':'object','properties':{}})}})
        out['tools']=tools
    tc=b.get('tool_choice',{'type':'auto'});require(isinstance(tc,dict),'Invalid tool_choice')
    if tc.get('type')=='tool':out['tool_choice']={'type':'function','function':{'name':tc.get('name')}}
    else:
        require(tc.get('type') in ('auto','any','none'),'Invalid tool choice');out['tool_choice']={'any':'required','auto':'auto','none':'none'}[tc['type']]
    if 'disable_parallel_tool_use' in tc:out['parallel_tool_calls']=not tc['disable_parallel_tool_use']
    if b.get('thinking'):
        require(b['thinking'].get('type') in ('enabled','disabled','adaptive'),'Invalid thinking type');out['thinking']=copy.deepcopy(b['thinking'])
    config=b.get('output_config') or {}
    if config.get('effort'):out['reasoning_effort']=config['effort']
    fmt=config.get('format') or b.get('output_format')
    if fmt:
        require(fmt.get('type')=='json_schema','Unsupported output format');out['response_format']={'type':'json_schema','json_schema':{'name':'response','schema':fmt.get('schema',{})}}
    return validate_chat(out)

class AnthropicWriter:
    def __init__(self,body,emit):
        self.emit=emit;self.items=[];self.indices={};self.arguments={};self.finish_reason=None;self.body=body
        self.response={'id':uid('msg_'),'type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':0,'output_tokens':0}}
    async def event(self,kind,**fields):
        if self.emit:await self.emit(kind,{'type':kind,**copy.deepcopy(fields)})
    async def start(self):await self.event('message_start',message=self.response)
    async def block(self,key,value):
        if key not in self.indices:
            self.indices[key]=len(self.items);self.items.append(value);await self.event('content_block_start',index=self.indices[key],content_block=value)
        return self.indices[key]
    async def add(self,c):
        u=c.get('usage') or {};usage=self.response['usage']
        for source,target in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('input_tokens','input_tokens'),('output_tokens','output_tokens')]:
            if source in u:usage[target]=u[source]
        for ch in c.get('choices',[]):
            require(ch.get('index',0)==0,'Messages supports one choice',502);d=ch.get('delta') or {}
            for field,key,typ,delta in [('reasoning_content','reasoning','thinking','thinking_delta'),('content','text','text','text_delta'),('refusal','text','text','text_delta')]:
                if not d.get(field):continue
                val={'type':typ,typ:''}
                if typ=='thinking':val['signature']=''
                i=await self.block(key,val);self.items[i][typ]+=d[field];await self.event('content_block_delta',index=i,delta={'type':delta,typ:d[field]})
            for t in d.get('tool_calls') or []:
                key=('tool',t['index']);f=t.get('function') or {}
                if key not in self.indices:require(t.get('id') and f.get('name'),'Incomplete initial tool delta',502,'invalid_tool_call')
                i=await self.block(key,{'type':'tool_use','id':t.get('id',''),'name':f.get('name',''),'input':{}})
                self.arguments[i]=self.arguments.get(i,'')+f.get('arguments','')
                if f.get('arguments'):await self.event('content_block_delta',index=i,delta={'type':'input_json_delta','partial_json':f['arguments']})
            if ch.get('finish_reason'):self.finish_reason=ch['finish_reason']
    async def finish(self):
        for i,item in enumerate(self.items):
            if item['type']=='tool_use':
                try:item['input']=json.loads(self.arguments.get(i) or '{}')
                except ValueError:raise Fault(502,'Invalid tool arguments','invalid_tool_call')
                require(isinstance(item['input'],dict),'Tool arguments must be an object',502,'invalid_tool_call')
            await self.event('content_block_stop',index=i)
        self.response.update(content=self.items,stop_reason={'tool_calls':'tool_use','length':'max_tokens','content_filter':'refusal'}.get(self.finish_reason,'end_turn'))
        await self.event('message_delta',delta={'stop_reason':self.response['stop_reason'],'stop_sequence':None},usage=self.response['usage']);await self.event('message_stop')
        return self.response
    async def fail(self,e,rid):
        error=anthropic_error(e,rid);await self.event('error',error=error['error'])
