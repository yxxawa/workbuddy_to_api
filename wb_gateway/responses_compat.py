# Protocol adapter helpers. Host-only tools are advertised as unavailable, never fabricated.
import copy,hashlib,json
from .storage import require,Fault

def tool_specs(body):
    result={};unavailable=[]
    def visit(tools,namespace=None):
        for t in tools:
            kind=t.get('type')
            if kind=='namespace':
                require(not namespace and isinstance(t.get('name'),str),'Invalid tool namespace');visit(t.get('tools',[]),t['name']);continue
            if kind not in ('function','custom'):
                unavailable.append(kind or 'unknown');continue
            name=t.get('name');require(isinstance(name,str) and name,'Tool name required')
            alias='ns_'+hashlib.sha256((namespace+':'+name).encode()).hexdigest()[:24] if namespace else name
            require(alias not in result,'Duplicate tool name')
            result[alias]={'name':name,'namespace':namespace,'kind':kind,'source':t}
    visit(body.get('tools') or [])
    return result,unavailable

def responses_request(b):
    require(isinstance(b,dict),'JSON object required')
    require(not any(b.get(k) for k in ('store','background','previous_response_id','conversation')),'Use stateless Responses: store=false and send conversation input',400,'unsupported_response_state')
    require(b.get('truncation','disabled')=='disabled','Automatic truncation unsupported')
    specs,unavailable=tool_specs(b)
    def alias(item):
        name=item.get('name');namespace=item.get('namespace')
        return next((k for k,s in specs.items() if s['name']==name and s['namespace']==namespace),name if not namespace else 'ns_'+hashlib.sha256((namespace+':'+name).encode()).hexdigest()[:24])
    def content(value):
        if isinstance(value,str):return value
        require(isinstance(value,list),'Invalid input content');out=[]
        for p in value:
            typ=p.get('type')
            if typ in ('input_text','output_text','text'):out.append({'type':'text','text':p.get('text','')})
            elif typ=='input_image':
                require(isinstance(p.get('image_url'),str),'image_url required');out.append({'type':'image_url','image_url':{'url':p['image_url'],**({'detail':p['detail']} if 'detail' in p else {})}})
            else:raise Fault(400,'Unsupported input content: '+str(typ))
        return out
    messages=[]
    if b.get('instructions'):messages.append({'role':'system','content':b['instructions']})
    inp=[{'role':'user','content':b['input']}] if isinstance(b.get('input'),str) else b.get('input');require(isinstance(inp,list),'input required')
    for item in inp:
        typ=item.get('type')
        if typ=='reasoning':continue
        if typ in ('function_call','custom_tool_call'):
            args=item.get('arguments') if typ=='function_call' else json.dumps({'input':item.get('input','')},ensure_ascii=False)
            require(isinstance(args,str) and isinstance(item.get('call_id'),str) and isinstance(item.get('name'),str),'Invalid tool call history')
            if not messages or not messages[-1].get('tool_calls'):messages.append({'role':'assistant','content':None,'tool_calls':[]})
            messages[-1]['tool_calls'].append({'id':item['call_id'],'type':'function','function':{'name':alias(item),'arguments':args}})
        elif typ in ('function_call_output','custom_tool_call_output'):
            require(isinstance(item.get('call_id'),str),'call_id required');messages.append({'role':'tool','tool_call_id':item['call_id'],'content':content(item.get('output'))})
        else:
            require(item.get('role') in ('system','developer','user','assistant'),'Invalid input role');messages.append({'role':item['role'],'content':content(item.get('content'))})
    out={k:b[k] for k in ('model','stream','parallel_tool_calls','temperature','top_p','user') if k in b};out['messages']=messages
    if 'tools' in b:
        out['tools']=[]
        for name,s in specs.items():
            t=s['source'];f={k:t[k] for k in ('description','parameters','strict') if k in t};f['name']=name
            if s['namespace']:f['description']='Namespace '+s['namespace']+'.'+s['name']+'. '+f.get('description','')
            if s['kind']=='custom':
                f['parameters']={'type':'object','properties':{'input':{'type':'string'}},'required':['input'],'additionalProperties':False}
                f['description']=f.get('description','')+' Return the exact free-form tool payload in the input string.'
                if t.get('format'):f['description']+=' Tool input format: '+json.dumps(t['format'],ensure_ascii=False)
            out['tools'].append({'type':'function','function':f})
    tc=b.get('tool_choice','auto')
    if isinstance(tc,dict):
        require(tc.get('type') in ('function','custom'),'Requested server-hosted tool is unavailable',400,'unsupported_tool');out['tool_choice']={'type':'function','function':{'name':alias(tc)}}
    else:out['tool_choice']=tc
    if unavailable and tc=='required':require(bool(out.get('tools')),'Required tools are unavailable',400,'unsupported_tool')
    if 'max_output_tokens' in b:out['max_tokens']=b['max_output_tokens']
    if (b.get('reasoning') or {}).get('effort'):out['reasoning_effort']=b['reasoning']['effort']
    fmt=(b.get('text') or {}).get('format')
    if fmt:out['response_format']={'type':'json_schema','json_schema':{k:v for k,v in fmt.items() if k!='type'}} if fmt.get('type')=='json_schema' else fmt
    return out

import time
from .storage import uid
class ResponsesWriter:
    def __init__(self,body,emit):
        self.emit=emit;self.seq=0;self.items=[];self.calls={};self.parts={};self.message=None;self.reasoning=None;self.finish_reason=None;self.custom_args={};self.specs,_=tool_specs(body)
        self.response={'id':uid('resp_'),'object':'response','created_at':int(time.time()),'status':'in_progress','error':None,'incomplete_details':None,'model':body['model'],'output':[],'usage':None,'parallel_tool_calls':body.get('parallel_tool_calls',True),'tool_choice':body.get('tool_choice','auto'),'tools':body.get('tools',[]),'metadata':body.get('metadata',{}),'store':False}
    async def event(self,t,**p):
        if self.emit:await self.emit(t,{'type':t,'sequence_number':self.seq,**copy.deepcopy(p)})
        self.seq+=1
    async def start(self):
        await self.event('response.created',response=self.response);await self.event('response.in_progress',response=self.response)
    async def add(self,c):
        if c.get('usage'):
            u=c['usage'];it=u.get('prompt_tokens',u.get('input_tokens',0));ot=u.get('completion_tokens',u.get('output_tokens',0))
            self.response['usage']={'input_tokens':it,'output_tokens':ot,'total_tokens':u.get('total_tokens',it+ot),'input_tokens_details':{'cached_tokens':(u.get('prompt_tokens_details') or {}).get('cached_tokens',0)},'output_tokens_details':{'reasoning_tokens':(u.get('completion_tokens_details') or {}).get('reasoning_tokens',0)}}
        for ch in c.get('choices',[]):
            require(ch.get('index',0)==0,'Responses supports one choice',502);d=ch.get('delta') or {}
            if d.get('reasoning_content'):
                if self.reasoning is None:
                    self.reasoning=len(self.items);item={'id':uid('rs_'),'type':'reasoning','summary':[]};self.items.append(item);await self.event('response.output_item.added',output_index=self.reasoning,item=item)
                    item['summary'].append({'type':'summary_text','text':''});await self.event('response.reasoning_summary_part.added',item_id=item['id'],output_index=self.reasoning,summary_index=0,part=item['summary'][0])
                item=self.items[self.reasoning];item['summary'][0]['text']+=d['reasoning_content'];await self.event('response.reasoning_summary_text.delta',item_id=item['id'],output_index=self.reasoning,summary_index=0,delta=d['reasoning_content'])
            for field,typ in [('content','output_text'),('refusal','refusal')]:
                if not d.get(field):continue
                if self.message is None:
                    self.message=len(self.items);self.items.append({'id':uid('msg_'),'type':'message','status':'in_progress','role':'assistant','content':[]});await self.event('response.output_item.added',output_index=self.message,item=self.items[-1])
                item=self.items[self.message];key='text' if typ=='output_text' else 'refusal'
                if typ not in self.parts:
                    self.parts[typ]=len(item['content']);part={'type':typ,key:''}
                    if typ=='output_text':part.update(annotations=[],logprobs=[])
                    item['content'].append(part);await self.event('response.content_part.added',item_id=item['id'],output_index=self.message,content_index=self.parts[typ],part=part)
                i=self.parts[typ];item['content'][i][key]+=d[field];await self.event('response.'+typ+'.delta',item_id=item['id'],output_index=self.message,content_index=i,delta=d[field],**({'logprobs':[]} if typ=='output_text' else {}))
            for t in d.get('tool_calls') or []:
                ti=t['index'];f=t.get('function') or {}
                if ti not in self.calls:
                    require(t.get('id') and f.get('name'),'Initial tool delta requires id and name',502,'invalid_tool_call');spec=self.specs.get(f['name'],{'name':f['name'],'namespace':None,'kind':'function'})
                    custom=spec['kind']=='custom';self.calls[ti]=len(self.items);item={'id':uid('ct_' if custom else 'fc_'),'type':'custom_tool_call' if custom else 'function_call','status':'in_progress','call_id':t['id'],'name':spec['name'],('input' if custom else 'arguments'):''}
                    if spec['namespace']:item['namespace']=spec['namespace']
                    self.items.append(item);await self.event('response.output_item.added',output_index=self.calls[ti],item=item)
                item=self.items[self.calls[ti]];args=f.get('arguments','')
                if item['type']=='custom_tool_call':self.custom_args[ti]=self.custom_args.get(ti,'')+args
                elif args:
                    item['arguments']+=args;await self.event('response.function_call_arguments.delta',item_id=item['id'],output_index=self.calls[ti],delta=args)
            require(not d.get('function_call'),'Use tools rather than legacy function_call',502)
            if ch.get('finish_reason'):self.finish_reason=ch['finish_reason']
    async def finish(self):
        for ti,args in self.custom_args.items():
            item=self.items[self.calls[ti]]
            try:payload=json.loads(args)
            except ValueError:raise Fault(502,'Malformed free-form tool wrapper','invalid_tool_call')
            require(isinstance(payload,dict) and isinstance(payload.get('input'),str),'Missing custom tool input',502,'invalid_tool_call');item['input']=payload['input']
            await self.event('response.custom_tool_call_input.delta',item_id=item['id'],output_index=self.calls[ti],delta=item['input'])
        for i,item in enumerate(self.items):
            if item['type']=='reasoning':
                for j,p in enumerate(item['summary']):
                    await self.event('response.reasoning_summary_text.done',item_id=item['id'],output_index=i,summary_index=j,text=p['text']);await self.event('response.reasoning_summary_part.done',item_id=item['id'],output_index=i,summary_index=j,part=p)
            elif item['type'] in ('function_call','custom_tool_call'):
                item['status']='completed';custom=item['type']=='custom_tool_call';field='input' if custom else 'arguments';await self.event('response.'+('custom_tool_call_input' if custom else 'function_call_arguments')+'.done',item_id=item['id'],output_index=i,name=item['name'],**{field:item[field]})
            else:
                item['status']='completed'
                for j,p in enumerate(item['content']):
                    field='text' if p['type']=='output_text' else 'refusal';await self.event('response.'+p['type']+'.done',item_id=item['id'],output_index=i,content_index=j,**{field:p[field]});await self.event('response.content_part.done',item_id=item['id'],output_index=i,content_index=j,part=p)
            await self.event('response.output_item.done',output_index=i,item=item)
        self.response.update(output=self.items,status='incomplete' if self.finish_reason in ('length','content_filter') else 'completed')
        if self.response['status']=='incomplete':self.response['incomplete_details']={'reason':'max_output_tokens' if self.finish_reason=='length' else 'content_filter'}
        await self.event('response.'+self.response['status'],response=self.response);return self.response
