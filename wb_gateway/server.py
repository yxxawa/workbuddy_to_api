from . import __version__
import asyncio, copy, ipaddress, json, os, secrets, time, urllib.parse, uuid
from pathlib import Path
from aiohttp import web, ClientSession, ClientTimeout, ClientError
from .responses_compat import tool_specs
from .anthropic import anthropic_to_chat, AnthropicWriter, anthropic_error
from .storage import Store, Fault, require, unpack, now, stamp, atomic, read, uid, safe_error
from .models import Risk, visible, controls, quote, number
from .protocol import validate_chat, prepare, responses_to_chat, chunks, heartbeat_chunks, Accumulator, ResponsesWriter, contract, read_bounded

def network_proxy():
    mode=os.environ.get('WB_PROXY_MODE','auto')
    if mode=='direct': return None,'direct'
    for key in ('HTTPS_PROXY','https_proxy','HTTP_PROXY','http_proxy'):
        if os.environ.get(key): return os.environ[key],'environment'
    if mode=='auto' and os.name=='nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as k:
                if winreg.QueryValueEx(k,'ProxyEnable')[0]:
                    value=winreg.QueryValueEx(k,'ProxyServer')[0]
                    if '=' in value:
                        pairs=dict(p.split('=',1) for p in value.split(';') if '=' in p); value=pairs.get('https',pairs.get('http'))
                    if value: return value if '://' in value else 'http://'+value,'windows_system'
        except OSError: pass
    return None,'direct'
def auth_values(data,previous=None):
    a={**(previous or {}),**data}; require(isinstance(a.get('accessToken'),str) and a['accessToken'],'Missing access token',502)
    a['lastRefreshTime']=stamp()
    for field,target in [('expiresIn','expiresAt'),('refreshExpiresIn','refreshExpiresAt')]:
        if number(data.get(field)): a[target]=stamp()+data[field]*1000
    return a
def profile(a): return {k:a[k] for k in ('uid','nickname','type','enterpriseId','enterpriseName','departmentFullName','lastLogin','avatar') if k in a}
def internal_region(region): return 'intl' if region=='gl' else region
def public_region(region): return 'gl' if region=='intl' else region
def public_model(region,model): return public_region(region)+'/'+model
def jresponse(value,status=200): return web.json_response(value,status=status,headers={'Cache-Control':'no-store'},dumps=lambda v:json.dumps(v,ensure_ascii=False))
class Gateway:
    def __init__(self,root,data_dir=None,config=None,admin_key=None,test_origins=False):
        self.root=Path(root); self.store=Store(data_dir or self.root/'data'); self.config=config or {r:read(self.root/'config'/f'{r}.json',{}) for r in ('cn','intl')}
        self.test_origins=test_origins; keys=read(self.store.root/'keys.json',{})
        self.admin_key=admin_key or os.environ.get('WB_ADMIN_KEY') or keys.get('admin_key') or secrets.token_urlsafe(32)
        if not admin_key and not os.environ.get('WB_ADMIN_KEY') and not keys.get('admin_key'): atomic(self.store.root/'keys.json',{**keys,'admin_key':self.admin_key})
        self.risk=Risk(self.store); self.sessions={}; self.pending={}; self.jobs={}; self.active_job=None; self.busy={}; self.switching=set(); self.locks={}; self.refresh_tasks={}; self.catalogs={}; self.catalog_tasks={}; self.last_picked={}; self.tasks=set()
        self.proxy,self.proxy_source=network_proxy()
        if self.proxy: require(urllib.parse.urlsplit(self.proxy).scheme in ('http','https'),'Only HTTP(S) network proxies supported',500)
        self.allowed_origins=set(filter(None,os.environ.get('WB_ALLOWED_ORIGINS','').split(',')))
    def task(self,coro):
        t=asyncio.create_task(coro); self.tasks.add(t); t.add_done_callback(self.tasks.discard); return t
    async def start(self,app): self.http=ClientSession(timeout=ClientTimeout(total=1200,connect=30,sock_read=120),trust_env=False)
    async def close(self,app):
        for t in list(self.tasks): t.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True); await self.http.close()
    def headers(self,region,record=None,anonymous=False):
        c=self.config[region]; app=c.get('applicationName',c['platform']); version=c['version']; h={'Accept':'application/json','Content-Type':'application/json','X-Product':'SaaS','X-Requested-With':'XMLHttpRequest','X-Domain':urllib.parse.urlsplit(c['endpoint']).netloc,'X-IDE-Type':app,'X-IDE-Name':app,'X-IDE-Version':version,'X-Product-Version':version,'X-Request-ID':uid('req_'),'User-Agent':f'CLI/unknown {app}/{version} WorkBuddyStandaloneProxy/1.0'}
        if anonymous:
            h.update({k:'true' for k in ('X-No-Authorization','X-No-User-Id','X-No-Enterprise-Id','X-No-Department-Info')})
        elif record:
            a=record.get('auth',{}); p=record.get('account',{}); h['Authorization']='Bearer '+a.get('accessToken','')
            if a.get('domain'): h['X-Domain']=a['domain']
            for field,key in [('uid','X-User-Id'),('enterpriseId','X-Enterprise-Id'),('departmentFullName','X-Department-Info')]:
                if p.get(field) is not None: h[key]=urllib.parse.quote(str(p[field]),safe='')
        return h
    async def call(self,region,path,record=None,method='GET',body=None,headers=None,anonymous=False,stream=False):
        require(region in self.config,'Invalid region'); self.risk.check(region,(record or {}).get('id'),path)
        endpoint=self.config[region]['endpoint']; url=urllib.parse.urlsplit(endpoint)
        require((url.scheme=='https' and url.hostname in ('www.workbuddy.cn','www.workbuddy.ai','www.codebuddy.cn','www.codebuddy.ai','copilot.tencent.com')) or (self.test_origins and url.hostname in ('127.0.0.1','localhost')),'Untrusted upstream origin',500)
        h={**self.headers(region,record,anonymous),**(headers or {})}
        require(all(isinstance(v,str) and len(v)<=4096 and not any(ord(ch)<32 or ord(ch)==127 for ch in v) for v in h.values()),'Unsafe upstream header',400)
        try:
            response=await self.http.request(method,endpoint.rstrip('/')+path,json=body,headers=h,proxy=None if url.hostname in ('127.0.0.1','localhost') else self.proxy,allow_redirects=False,timeout=ClientTimeout(total=1200 if stream else 30,connect=30,sock_read=120 if stream else 30))
            if stream and 200<=response.status<300: return response
            try:
                raw=await read_bounded(response.content,4*1024*1024); require(len(raw)<=4*1024*1024,'Upstream JSON too large',502)
                try: data=json.loads(raw)
                except ValueError: data={'error':{'code':'invalid_upstream','message':'Upstream returned non-JSON'}}
                require(isinstance(data,dict),'Invalid upstream body',502)
                bad=not 200<=response.status<300 or data.get('error') or data.get('code') not in (None,0,200,'0','200')
                if bad:
                    if str(data.get('code')) in ('11217','12151') and ('/auth/token?' in path or '/login/account?' in path): return data
                    guarded=self.risk.observe(response.status,data,region,(record or {}).get('id'),path,response.headers)
                    if guarded: raise guarded
                    details=data.get('error') if isinstance(data.get('error'),dict) else data
                    message=str(details.get('message',details.get('msg','Official API rejected request')))
                    for record_value in self.store.records.values():
                        for secret in record_value.get('auth',{}).values():
                            if isinstance(secret,str) and len(secret)>16:message=message.replace(secret,'[REDACTED]')
                    raise Fault(response.status if 400<=response.status<500 else 502,message[:500],'upstream_'+str(details.get('code','error')))
                return data
            finally: response.release()
        except asyncio.TimeoutError: raise Fault(504,'Upstream timeout','upstream_timeout')
        except ClientError: raise Fault(502,'Upstream connection failed','upstream_connection_error')
    async def refresh(self,r,force=False):
        self.risk.check(r['region'],r['id'],'/v2/plugin/auth/token/refresh')
        if r['id'] in self.refresh_tasks: return await asyncio.shield(self.refresh_tasks[r['id']])
        if not force and (not r['auth'].get('expiresAt') or r['auth']['expiresAt']>stamp()+60000): return r
        async def work():
            a=r['auth']; require(a.get('refreshToken') and (not a.get('refreshExpiresAt') or a['refreshExpiresAt']>stamp()),'Session expired; log in again',401,'login_required')
            data=unpack(await self.call(r['region'],'/v2/plugin/auth/token/refresh',r,'POST',{}, {'X-Refresh-Token':a['refreshToken'],'X-Auth-Refresh-Source':'plugin'}))
            nxt={**r,'auth':auth_values(data,a)}; self.store.put(nxt)
            rows=unpack(await self.call(r['region'],'/v2/plugin/accounts',nxt)); rows=rows if isinstance(rows,list) else rows.get('accounts',[])
            account=next((x for x in rows if (x.get('uid')==r['account'].get('uid') and x.get('enterpriseId')==r['account'].get('enterpriseId'))),None)
            if nxt.get('needsAccountSync'): account=next((x for x in rows if x.get('lastLogin')),None) or (rows[0] if rows else None)
            if account: nxt['account']=profile(account); nxt.pop('needsAccountSync',None); self.store.put(nxt)
            return nxt
        task=self.task(work()); self.refresh_tasks[r['id']]=task; task.add_done_callback(lambda _: self.refresh_tasks.pop(r['id'],None))
        try: return await asyncio.shield(task)
        finally:
            if task.done(): self.refresh_tasks.pop(r['id'],None)
    async def catalog(self,r,fresh=False):
        r=await self.refresh(r); old=self.catalogs.get(r['id'])
        if not fresh and old and old[0]>time.time() and old[1]==r['account']: return old[2]
        if r['id'] in self.catalog_tasks: return await asyncio.shield(self.catalog_tasks[r['id']])
        async def load():
            require(not r.get('needsAccountSync') and r['id'] not in self.switching,'Account requires synchronization',409)
            p=unpack(await self.call(r['region'],'/v3/config',r,headers={'Cache-Control':'no-cache','Pragma':'no-cache'})); visible(p); self.catalogs[r['id']]=(time.time()+60,copy.deepcopy(r['account']),p); return p
        task=self.task(load()); self.catalog_tasks[r['id']]=task; task.add_done_callback(lambda _: self.catalog_tasks.pop(r['id'],None))
        try: return await asyncio.shield(task)
        finally:
            if task.done(): self.catalog_tasks.pop(r['id'],None)
    def accounts(self,scope='all',region=None,account=None):
        scope=internal_region(scope); region=internal_region(region)
        require(region in (None,'','cn','intl'),'Invalid region')
        require(scope=='all' or not region or scope==region,'API key does not permit region',403,'scope_denied')
        return [r for r in self.store.records.values() if (scope=='all' or r['region']==scope) and (not region or r['region']==region) and (not account or r['id']==account)]
    async def catalogs_for(self,scope,region=None,account=None):
        rows=self.accounts(scope,region,account); require(rows,'No accounts in key scope',503,'no_accounts')
        for r in rows:
            for e in self.risk.list():
                if e['scope'] in ('global','region') and self.risk.matches(e,r['region'],r['id'],'/v2/chat/completions'): self.risk.check(r['region'],r['id'],'/v2/chat/completions')
        out=[]; errors=[]
        for r in rows:
            try: out.append((r,visible(await self.catalog(r))))
            except Fault as e:
                if e.risk: raise
                errors.append(safe_error(e))
        require(out,'No live model catalogs available',503,'catalog_unavailable'); return out,errors
    async def model_list(self,scope,region=None):
        rows,errors=await self.catalogs_for(scope,internal_region(region)); by={}
        for r,models in rows:
            for m in models:
                mid=public_model(r['region'],m['id'])
                if mid in by: continue
                region_id=public_region(r['region']); c=controls(m)
                by[mid]={'id':mid,'object':'model','created':0,'owned_by':'workbuddy','metadata':{'region':region_id,'regions':[region_id],'account_region':r['region'],'upstream_model':m['id'],'capabilities_by_region':[{'region':region_id,'source':'live','regional_id':mid,'capabilities':c}],'max_input_tokens':c['max_input_tokens'],'max_output_tokens':c['max_output_tokens'],'reasoning_efforts':c['efforts'],'supports_tools':c['supports_tools'],'supports_images':c['supports_images']}}
        return {'object':'list','data':[by[k] for k in sorted(by)],'partial':bool(errors),'note':'Region-qualified IDs: cn/ for China, gl/ for Global. Use the returned ID unchanged in requests. Model parameters remain client controlled.'}
    async def pricing(self,region,account=None):
        require(region in ('cn','intl'),'Invalid region'); allrows=self.accounts(region); rows=self.accounts(region,account=account)
        if account: require(rows,'Account not found',404)
        by={}; errors=[]; fetched=[]
        for r in rows:
            try:
                p=await self.catalog(r,True); at=now(); fetched.append(at)
                for m in visible(p):
                    row=by.setdefault(m['id'],{'id':m['id'],'name':m.get('name',m['id']),'variants':[]}); row['variants'].append({'account_id':r['id'],'account_name':r['account'].get('nickname',r['account'].get('uid',r['id'])),'fetched_at':at,**quote(m,p.get('modelPromotions',[]))})
            except Exception as e: errors.append({'account_id':r['id'],'error':safe_error(e)})
        models=sorted(by.values(),key=lambda m:m['name'])
        return {'region':region,'source':'live_account_config','source_path':'/v3/config','fetched_at':max(fetched) if fetched else None,'server_time':now(),'accounts':[{'id':r['id'],'name':r['account'].get('nickname',r['id'])} for r in allrows],'models':models,'errors':errors,'partial':bool(errors and fetched),'status':'no_accounts' if not rows else 'unavailable' if not fetched else 'partial' if errors else 'ok','provided_models':sum(any(v['status']=='provided' for v in m['variants']) for m in models)}
    async def balance(self,r):
        headers={'Accept-Language':'zh' if r['region']=='cn' else 'en'}
        if r['account'].get('enterpriseId'):
            headers['X-Tenant-Id']=str(r['account']['enterpriseId']); d=unpack(await self.call(r['region'],'/v2/billing/meter/get-enterprise-user-usage',r,'POST',{},headers)); d=d.get('data',d)
            require(type(d.get('limitNum')) in (int,float) and number(d.get('credit')),'Invalid enterprise balance',502)
            unlimited=d['limitNum']==-1; return {'remaining':None if unlimited else d['limitNum']-d['credit'],'total':None if unlimited else d['limitNum'],'used':d['credit'],'unlimited':unlimited,'unit':'credits','source':'enterprise-user-usage'}
        d=unpack(await self.call(r['region'],'/billing/meter/get-user-resource-summary',r,'POST',{},headers)); require(isinstance(d.get('Packages'),list),'Invalid resource summary',502)
        out={'remaining':0,'total':0,'used':0,'unit':'credits','unlimited':False,'source':'user-resource-summary'}
        for p in d['Packages']:
            for k,f in [('remaining','CycleRemainCapacity'),('total','CycleTotalCapacity'),('used','CycleUsedCapacity')]:
                try: n=float(p[f]) if p[f] not in (None,'') and type(p[f]) is not bool else None
                except (ValueError,KeyError,TypeError): n=None
                require(n is not None and number(abs(n)),'Incomplete credit summary',502); out[k]+=max(0,n)
        return out
    async def checkin_status(self,r):
        if r['account'].get('enterpriseId'): return {'active':False,'reason':'personal_accounts_only'}
        d=unpack(await self.call(r['region'],'/v2/billing/meter/checkin-activity-status',r,'POST',{})); require(type(d.get('active')) is bool,'Invalid check-in status',502)
        return {k:d[k] for k in ('active','today_checked_in','streak_days','is_streak_day','today_credit','next_streak_day','streak_bonus_credit','streak_bonus_days') if k in d}
    async def inspect(self,r,action):
        lock=self.locks.setdefault(r['id'],asyncio.Lock()); require(not lock.locked() and r['id'] not in self.switching,'Account operation in progress',409)
        async with lock:
            self.busy[r['id']]=self.busy.get(r['id'],0)+1
            try:
                r=await self.refresh(self.store.records[r['id']]); result=copy.deepcopy(r.get('management',{})); result['updated_at']=now()
                async def status():
                    try: result['checkin']={**await self.checkin_status(r),'updated_at':now(),'stale':False}
                    except Exception as e: result['checkin']={**result.get('checkin',{}),'error':safe_error(e),'stale':True,'last_attempt_at':now()}
                if action!='balance': await status()
                if action=='checkin' and not result.get('checkin',{}).get('error'):
                    s=result['checkin']; previous=result.get('claim',{})
                    if s.get('today_checked_in'): result['claim']={'status':'already_claimed','at':now()}
                    elif not s.get('active'): result['claim']={'status':'not_eligible','at':now()}
                    elif previous.get('status')=='outcome_unknown' and previous.get('at','')[:10]==now()[:10]: pass
                    else:
                        result['claim']={'status':'outcome_unknown','at':now()}; r['management']=copy.deepcopy(result); self.store.put(r)
                        try:
                            d=unpack(await self.call(r['region'],'/v2/billing/meter/daily-checkin',r,'POST',{})); require(number(d.get('credit')),'Invalid check-in reward',502)
                            result['claim']={'status':'claimed','credit':d['credit'],'at':now()}; result['checkin']['today_checked_in']=True
                        except Exception as e: result['claim']={**result['claim'],'error':safe_error(e),'status':'rejected' if isinstance(e,Fault) and (e.risk or e.code.startswith('upstream_') and e.code not in ('upstream_timeout','upstream_connection_error')) else 'outcome_unknown'}
                try: result['balance']={**await self.balance(r),'updated_at':now(),'stale':False}
                except Exception as e: result['balance']={**result.get('balance',{}),'error':safe_error(e),'stale':True,'last_attempt_at':now()}
                r['management']=result; self.store.put(r); return result
            finally: self.busy[r['id']]=max(0,self.busy.get(r['id'],1)-1)
    def dashboard(self):
        rows=[{**self.store.public(r),'management':r.get('management',{}),'restrictions':[e for e in self.risk.list() if self.risk.matches(e,r['region'],r['id'],'/v2/chat/completions')]} for r in self.store.records.values()]; regions={}
        for region in ('cn','intl'):
            unique={}
            for r in rows:
                if r['region']==region: unique[(r['account'].get('uid'),r['account'].get('enterpriseId'))]=r
            balances=[r.get('management',{}).get('balance',{}) for r in unique.values()]; known=[b for b in balances if number(b.get('remaining')) and not b.get('error') and not b.get('stale')]
            regions[region]={'accounts':len(unique),'known_subtotal':sum(b['remaining'] for b in known),'known':len(known),'unknown':sum(not b or bool(b.get('error')) for b in balances),'unlimited':sum(bool(b.get('unlimited')) for b in balances),'unit':'credits','stale':sum(bool(b.get('stale')) and number(b.get('remaining')) for b in balances),'cached_subtotal':sum(b['remaining'] for b in balances if number(b.get('remaining'))),'cached':True}
        return {'accounts':rows,'regions':regions,'restrictions':self.risk.list(),'network':{'source':self.proxy_source,'mode':os.environ.get('WB_PROXY_MODE','auto')},'updated_at':now()}
    async def new_login(self,region):
        require(region in ('cn','intl'),'Invalid region'); self.pending={i:p for i,p in self.pending.items() if p['expires_at']>stamp()}; require(len(self.pending)<32,'Too many pending logins',429)
        d=unpack(await self.call(region,'/v2/plugin/auth/state?platform='+urllib.parse.quote(self.config[region]['platform']),method='POST',body={},anonymous=True)); require(isinstance(d.get('state'),str) and isinstance(d.get('authUrl'),str),'Invalid login state',502)
        u=urllib.parse.urlsplit(d['authUrl']); require(u.scheme=='https' and u.hostname in ('www.workbuddy.cn','www.workbuddy.ai','www.codebuddy.cn','www.codebuddy.ai','copilot.tencent.com') and not u.username and not u.password or self.test_origins and u.hostname=='127.0.0.1','Untrusted authorization URL',502)
        query=dict(urllib.parse.parse_qsl(u.query)); query['version']=self.config[region]['version']; url=urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(query)))
        i=uid('login_'); p={'region':region,'state':d['state'],'expires_at':stamp()+300000,'next_poll':0,'lock':asyncio.Lock()}; self.pending[i]=p
        return {'login_id':i,'auth_url':url,'expires_at':p['expires_at'],'interval_ms':1000}
    async def poll_login(self,i):
        p=self.pending.get(i); require(p and p['expires_at']>stamp(),'Login request expired',410,'login_expired')
        async with p['lock']:
            if p.get('result'): return p['result']
            if stamp()<p['next_poll']: return {'status':'pending','interval_ms':1000}
            p['next_poll']=stamp()+1000; region=p['region']; path='?state='+urllib.parse.quote(p['state'])
            if not p.get('auth'):
                e=await self.call(region,'/v2/plugin/auth/token'+path,anonymous=True)
                if str(e.get('code'))=='11217': return {'status':'pending','interval_ms':1000}
                d=unpack(e)
                if not d.get('accessToken'): return {'status':'pending','interval_ms':1000}
                p['auth']=auth_values(d)
            e=await self.call(region,'/v2/plugin/login/account'+path,{'auth':p['auth']})
            if str(e.get('code'))=='12151': return {'status':'pending','interval_ms':1000}
            account=profile(unpack(e)); require(bool(account.get('uid')),'Missing account profile',502)
            old=next((r for r in self.accounts(region) if r['account'].get('uid')==account['uid'] and r['account'].get('enterpriseId')==account.get('enterpriseId')),None)
            if old: require(not self.busy.get(old['id']) and old['id'] not in self.refresh_tasks and old['id'] not in self.switching,'Existing account busy',409)
            require(i in self.pending and not p.get('cancelled'),'Login cancelled',410)
            r={**(old or {}),'id':old['id'] if old else uid(region+'_'),'region':region,'account':account,'auth':p['auth'],'needsAccountSync':False}; self.store.put(r)
            for e in self.risk.list():
                if e['kind']=='login_required' and e.get('account_id')==r['id']: e['resolved_at']=now()
            self.risk.save(); p['result']={'status':'complete','account_id':r['id'],'region':region,'account':account}; p.pop('state',None); p.pop('auth',None); return p['result']
    async def bulk(self,b):
        require(b.get('action') in ('refresh','checkin'),'Unknown bulk action'); require(b.get('region') in (None,'cn','intl'),'Invalid region')
        if self.active_job:
            require(all(self.active_job.get(k)==b.get(k) for k in ('action','region','account_ids')),'Another bulk job running',409); return self.active_job
        ids=b.get('account_ids')
        if ids is not None: require(isinstance(ids,list) and 0<len(ids)<=500 and all(i in self.store.records for i in ids),'Invalid selected accounts')
        rows=[r for r in self.accounts(region=b.get('region')) if ids is None or r['id'] in ids]
        job={'id':str(uuid.uuid4()),'action':b['action'],'region':b.get('region'),'account_ids':ids,'status':'running','total':len(rows),'completed':0,'results':[],'created_at':now()}; self.jobs[job['id']]=job; self.active_job=job
        while len(self.jobs)>30: self.jobs.pop(next(iter(self.jobs)))
        async def work():
            try:
                for index,r in enumerate(rows):
                    if index: await asyncio.sleep(1)
                    try: job['results'].append({'account_id':r['id'],'region':r['region'],'result':await self.inspect(r,'balance' if b['action']=='refresh' else 'checkin')})
                    except Exception as e: job['results'].append({'account_id':r['id'],'error':safe_error(e)})
                    job['completed']+=1
                job['status']='completed'
            finally: self.active_job=None
        self.task(work()); return job
    def log_query(self,q):
        try: page=max(1,min(100000,int(q.get('page',1)))); limit=max(1,min(100,int(q.get('limit',25))))
        except ValueError: raise Fault(400,'Invalid pagination')
        rows=list(reversed(self.store.logs))
        for k in ('region','model','status'):
            v=q.get(k)
            if v: rows=[r for r in rows if (r.get('outcome')==v if k=='status' else v.lower() in str(r.get('model','')).lower() if k=='model' else r.get(k)==v)]
        for k,before in [('from',False),('to',True)]:
            if q.get(k): rows=[r for r in rows if (r['started_at']<=q[k] if before else r['started_at']>=q[k])]
        credits=[r['credit'] for r in rows if number(r.get('credit'))]
        return {'data':rows[(page-1)*limit:page*limit],'total':len(rows),'page':page,'limit':limit,'summary':{'requests':len(rows),'known_credit':sum(credits),'credit_known':len(credits),'credit_unknown':len(rows)-len(credits),'request_bytes':sum(r.get('request_bytes',0) for r in rows)},'retention_days':30,'max_entries':10000,'storage_error':None}
    async def generate(self,req,principal):
        start=time.monotonic(); raw=await req.read()
        row={'id':uid('req_'),'started_at':now(),'endpoint':req.path,'request_ip':request_ip(req),'api_key_id':principal['id'],'request_bytes':len(raw),'status_code':200,'outcome':'pending','credit':None,'credit_source':None,'first_token_ms':None,'first_text_ms':None,'reasoning_ms':None,'reasoning_timing_source':None,'latency_source':None,'input_tokens':None,'output_tokens':None,'reasoning_tokens':None}
        stream_response=None; upstream=None; selected=None; busy_acquired=False; writer=None; reasoning_start=None; reasoning_end=None; last_reason=None
        try:
            try: body=json.loads(raw)
            except ValueError: raise Fault(400,'Invalid JSON')
            require(isinstance(body,dict),'JSON object required'); response_api=req.path=='/v1/responses'; messages_api=req.path=='/v1/messages'; chat=responses_to_chat(body) if response_api else anthropic_to_chat(body) if messages_api else validate_chat(copy.deepcopy(body))
            row.update(requested_model=chat['model'],model=chat['model'],stream=bool(chat.get('stream')))
            region=internal_region(req.headers.get('X-WB-Region') or req.query.get('region')); prefix,sep,mid=chat['model'].partition('/')
            if sep and prefix in ('cn','gl','intl'):
                resolved=internal_region(prefix); require(not region or region==resolved,'Conflicting region'); region=resolved; chat['model']=mid
            elif sep: raise Fault(400,'Use a cn/ or gl/ model ID','invalid_model_region')
            catalogs,_=await self.catalogs_for(principal['scope'],region,req.headers.get('X-WB-Account')); candidates=[]; exists=False
            matching_regions={r['region'] for r,models in catalogs if any(m['id']==chat['model'] for m in models)}
            require(region or len(matching_regions)<=1,'Model exists in both regions; select cn/'+chat['model']+' or gl/'+chat['model'],400,'ambiguous_model_region')
            for r,models in catalogs:
                m=next((m for m in models if m['id']==chat['model']),None)
                if not m: continue
                exists=True; c=controls(m)
                # A requested maximum is a ceiling; cap it to the selected model's real limit.
                if chat.get('reasoning_effort') and c['efforts'] and chat['reasoning_effort'] not in c['efforts']: continue
                if chat.get('tools') and not c['supports_tools']: continue
                if self.busy.get(r['id'],0)>=8 or r['id'] in self.switching or r.get('needsAccountSync'): continue
                candidates.append((r,m))
            require(candidates,'Unsupported model parameters or all matching accounts busy' if exists else 'Model not available',400 if exists else 404,'unsupported_model_parameter' if exists else 'model_not_found')
            candidates.sort(key=lambda rm:(self.busy.get(rm[0]['id'],0),self.last_picked.get(rm[0]['id'],0),rm[0]['id'])); selected,m=candidates[0]; self.risk.check(selected['region'],selected['id'],'/v2/chat/completions'); self.busy[selected['id']]=self.busy.get(selected['id'],0)+1; self.last_picked[selected['id']]=time.monotonic(); busy_acquired=True
            selected=await self.refresh(selected); response_model=public_model(selected['region'],chat['model']); row.update(region=selected['region'],account_id=selected['id'],model=response_model)
            cap=controls(m)['max_output_tokens']
            if chat.get('max_tokens') and number(cap) and cap>0:
                row['requested_max_tokens']=chat['max_tokens'];chat['max_tokens']=min(chat['max_tokens'],int(cap));row['effective_max_tokens']=chat['max_tokens']
            unavailable=tool_specs(body)[1] if response_api else []
            if unavailable:row['unavailable_server_tools']=unavailable
            rid=row['id']; headers={'X-Model-ID':chat['model'],'X-Agent-Intent':'craft','X-Agent-Purpose':'conversation','x-codebuddy-request':'1','X-Private-Data':'true','X-Conversation-ID':req.headers.get('X-Conversation-ID',uid('conv_')),'X-Conversation-Request-ID':rid,'X-Conversation-Message-ID':rid}
            upstream=await self.call(selected['region'],'/v2/chat/completions',selected,'POST',prepare(chat,selected['region']),headers,stream=True)
            async def emit(event,data):
                payload=('event: '+event+chr(10) if event else '')+'data: '+(data if isinstance(data,str) else json.dumps(data,ensure_ascii=False,separators=(',',':')))+chr(10)*2
                await stream_response.write(payload.encode())
            if chat.get('stream'):
                stream_response=web.StreamResponse(headers={'Content-Type':'text/event-stream; charset=utf-8','Cache-Control':'no-cache, no-transform','X-Accel-Buffering':'no','X-Request-ID':rid,**({'X-WB-Unavailable-Tools':','.join(unavailable)} if unavailable else {})}); await stream_response.prepare(req)
            writer=ResponsesWriter({**body,'model':response_model},emit if stream_response is not None else None) if response_api else AnthropicWriter({**body,'model':response_model},emit if stream_response is not None else None) if messages_api else None
            if writer: await writer.start()
            acc=Accumulator(response_model)
            async def heartbeat():
                if stream_response is not None:await stream_response.write((': ping'+chr(10)*2).encode())
            async for c in heartbeat_chunks(upstream,heartbeat):
                ms=round((time.monotonic()-start)*1000,2); row['upstream_model']=c.get('model',row.get('upstream_model')); row['latency_source']='gateway_observed'
                u=c.get('usage') or {}
                if number(u.get('credit')): row['credit']=u['credit']; row['credit_source']='upstream_usage.credit'
                for src,target in [('prompt_tokens','input_tokens'),('completion_tokens','output_tokens'),('input_tokens','input_tokens'),('output_tokens','output_tokens')]:
                    if number(u.get(src)): row[target]=u[src]
                reason=u.get('completion_tokens_details',{}).get('reasoning_tokens',u.get('completion_thinking_tokens'))
                if number(reason): row['reasoning_tokens']=reason
                for ch in c['choices']:
                    d=ch.get('delta',{}); any_output=any(d.get(k) for k in ('content','reasoning_content','tool_calls','function_call','refusal'))
                    if any_output and row['first_token_ms'] is None: row['first_token_ms']=ms
                    if d.get('content') and row['first_text_ms'] is None: row['first_text_ms']=ms
                    if d.get('reasoning_content'): reasoning_start=reasoning_start if reasoning_start is not None else ms; last_reason=ms
                    if reasoning_start is not None and reasoning_end is None and (d.get('content') or d.get('tool_calls')): reasoning_end=ms
                c['model']=response_model
                acc.add(c)
                if writer: await writer.add(c)
                elif stream_response is not None:
                    if c.get('choices') or body.get('stream_options',{}).get('include_usage'): await emit(None,c)
            value=acc.finish(); contract(value,chat)
            if writer: value=await writer.finish()
            row['outcome']='completed'
            if stream_response is not None:
                if not writer: await emit(None,'[DONE]')
                await stream_response.write_eof(); return stream_response
            result=jresponse(value)
            if unavailable:result.headers['X-WB-Unavailable-Tools']=','.join(unavailable)
            return result
        except (asyncio.CancelledError,ConnectionResetError): row.update(outcome='cancelled',status_code=499,error_code='client_disconnected'); raise
        except Exception as e:
            if getattr(e,'upstream_body',None) and selected:
                guarded=self.risk.observe(502,e.upstream_body,selected['region'],selected['id'],'/v2/chat/completions'); e=guarded or e
            if isinstance(e,asyncio.TimeoutError): e=Fault(504,'Upstream stream timeout','stream_timeout')
            row.update(outcome='failed',status_code=getattr(e,'status',502),error_code=getattr(e,'code','upstream_error'))
            row['error_message']=safe_error(e)['message'][:500]
            risk=getattr(e,'risk',None) or {}
            if risk.get('upstream_code') is not None: row['upstream_code']=risk['upstream_code']

            if stream_response is not None:
                err=error_body(e,row['id'])
                if isinstance(writer,AnthropicWriter): await writer.fail(e,row['id'])
                elif writer:
                    writer.response.update(status='failed',error=err['error'],output=writer.items); await writer.event('response.failed',response=writer.response)
                else: await emit('error',err)
                await stream_response.write_eof(); return stream_response
            raise e
        finally:
            if upstream is not None: upstream.close()
            if selected and busy_acquired: self.busy[selected['id']]=max(0,self.busy.get(selected['id'],1)-1)
            if reasoning_start is not None and upstream is not None and 'text/event-stream' in upstream.headers.get('Content-Type',''):
                row['reasoning_ms']=max(0,(reasoning_end if reasoning_end is not None else last_reason)-reasoning_start); row['reasoning_timing_source']='stream_observed'
            row['duration_ms']=round((time.monotonic()-start)*1000,2); self.store.log(row)
    def authorized_admin(self,req):
        auth=req.headers.get('Authorization','')
        if auth.startswith('Bearer ') and secrets.compare_digest(auth[7:],self.admin_key): return
        if not auth and req.headers.get('X-WB-UI')=='1' and self.sessions.get(req.cookies.get('wb_ui_session'),0)>stamp(): return
        raise Fault(401,'Invalid management key or expired session','invalid_api_key')
    async def handle(self,req):
        path=req.path; method=req.method
        if path=='/health': return jresponse({'status':'ok','version':__version__,'implementation':'python','capabilities':{'independent':True,'regions':['cn','intl'],'login':True,'streaming':True,'tools':True,'responses':True,'messages':True}})
        static={'/':'index.html','/ui':'index.html','/ui/':'index.html','/ui/theme.js':'theme.js','/theme.js':'theme.js','/ui/app.js':'app.js','/ui/style.css':'style.css','/ui/styles.css':'styles.css','/app.js':'app.js','/style.css':'style.css','/styles.css':'styles.css'}
        if path in static and method=='GET':
            p=self.root/'web'/static[path]; require(p.exists(),'Asset not found',404,'not_found'); return web.FileResponse(p,headers={'Cache-Control':'no-cache','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'"})
        if path.startswith('/admin/'):
            self.authorized_admin(req); parts=path.strip('/').split('/')
            if path=='/admin/session':
                if method=='POST':
                    require(secrets.compare_digest(req.headers.get('Authorization',''),'Bearer '+self.admin_key),'Management key required',401); sid=secrets.token_urlsafe(32); expiry=stamp()+28800000
                    self.sessions={k:v for k,v in self.sessions.items() if v>stamp()}
                    while len(self.sessions)>=32: self.sessions.pop(next(iter(self.sessions)))
                    self.sessions[sid]=expiry; response=jresponse({'authenticated':True,'expires_at':expiry}); response.set_cookie('wb_ui_session',sid,max_age=28800,httponly=True,samesite='Strict',secure=req.secure,path='/'); return response
                if method=='DELETE': self.sessions.pop(req.cookies.get('wb_ui_session'),None); response=jresponse({'authenticated':False}); response.del_cookie('wb_ui_session',path='/'); return response
                if method=='GET': return jresponse({'authenticated':True,'expires_at':self.sessions.get(req.cookies.get('wb_ui_session'))})
            if path=='/admin/dashboard' and method=='GET': return jresponse(self.dashboard())
            if path=='/admin/accounts' and method=='GET': return jresponse({'accounts':[self.store.public(r) for r in self.store.records.values()]})
            if path=='/admin/model-pricing' and method=='GET': return jresponse(await self.pricing(req.query.get('region','cn'),req.query.get('account_id')))
            if path=='/admin/call-logs' and method=='GET': return jresponse(self.log_query(req.query))
            if path=='/admin/risk' and method=='GET': return jresponse({'restrictions':self.risk.list()})
            if len(parts)==4 and parts[1]=='risk' and parts[3]=='resume' and method=='POST':
                b=await json_body(req); require(b.get('official_issue_resolved') is True,'Official issue resolution must be confirmed'); e=next((x for x in self.store.risks if x['id']==parts[2]),None); require(e,'Restriction not found',404); e['resolved_at']=now(); self.risk.save(); return jresponse({'resumed':True,'replayed':False})
            if path=='/admin/keys':
                if method=='GET': return jresponse({'keys':self.store.public_keys(),'legacy_api_key_enabled':False,'base_path':'/v1'})
                if method=='POST': return jresponse(self.store.new_key(await json_body(req)),201)
            if len(parts)==3 and parts[1]=='keys':
                k=next((x for x in self.store.keys if x['id']==parts[2]),None); require(k,'Key not found',404)
                if method=='DELETE': self.store.keys.remove(k); self.store.save_keys(); return jresponse({'revoked':True})
                if method=='POST':
                    b=await json_body(req); require(type(b.get('enabled')) is bool,'enabled must be boolean'); k['enabled']=b['enabled']; self.store.save_keys(); return jresponse({a:v for a,v in k.items() if a!='digest'})
            if path=='/admin/login' and method=='POST': return jresponse(await self.new_login((await json_body(req)).get('region','cn')),201)
            if len(parts)==3 and parts[1]=='login':
                if method=='GET': return jresponse(await self.poll_login(parts[2]))
                if method=='DELETE':
                    p=self.pending.pop(parts[2],None)
                    if p: p['cancelled']=True
                    return jresponse({'cancelled':True})
            if path=='/admin/jobs' and method=='POST': return jresponse(await self.bulk(await json_body(req)),202)
            if len(parts)==3 and parts[1]=='jobs' and method=='GET': require(parts[2] in self.jobs,'Job not found',404); return jresponse(self.jobs[parts[2]])
            if len(parts)>=3 and parts[1]=='accounts':
                r=self.store.records.get(parts[2]); require(r,'Account not found',404); action=parts[3] if len(parts)==4 else None
                if action in ('model-settings','request-preview'): raise Fault(410,'Model settings belong to the client','client_parameters_required')
                if action is None and method=='DELETE':
                    require(not self.busy.get(r['id']) and r['id'] not in self.refresh_tasks and r['id'] not in self.switching,'Account busy',409); self.store.records.pop(r['id']); self.store.save(); self.catalogs.pop(r['id'],None); return jresponse({'removed':True,'upstream_revoked':False})
                if action in ('inspect','balance','checkin') and method=='POST': return jresponse(await self.inspect(r,action))
                if action=='refresh' and method=='POST': require(r['id'] not in self.switching,'Account switching',409); return jresponse(self.store.public(await self.refresh(r,True)))
                if action=='config' and method=='GET': return jresponse(await self.call(r['region'],'/v3/config',await self.refresh(r)))
                if action=='management' and method=='GET': return jresponse(r.get('management',{}))
                if action=='switch' and method=='POST':
                    require(not self.busy.get(r['id']) and r['id'] not in self.refresh_tasks and r['id'] not in self.switching,'Account busy',409); require(r['auth'].get('refreshToken'),'Refresh token required',401); self.switching.add(r['id'])
                    try:
                        b=await json_body(req); d=unpack(await self.call(r['region'],'/v2/plugin/account/switch',r,'POST',{'target_enterprise_id':str(b['enterprise_id'])} if b.get('enterprise_id') else {},{'X-Refresh-Token':r['auth']['refreshToken']})); r={**r,'auth':auth_values(d,r['auth']),'needsAccountSync':True}; r.pop('management',None); r.pop('modelSettings',None); self.store.put(r)
                        account=profile(unpack(await self.call(r['region'],'/v2/plugin/account',r))); require(account.get('uid'),'Missing switched profile',502); r.update(account=account,needsAccountSync=False); self.store.put(r); self.catalogs.pop(r['id'],None); return jresponse(self.store.public(r))
                    finally: self.switching.remove(r['id'])
            raise Fault(404,'Unknown admin route','not_found')
        auth=req.headers.get('Authorization',''); token=auth[7:] if auth.startswith('Bearer ') else req.headers.get('x-api-key',''); principal=self.store.authenticate(token)
        if (path=='/v1/models' or path.startswith('/v1/models/')) and method=='GET':
            result=await self.model_list(principal['scope'],req.headers.get('X-WB-Region') or req.query.get('region'))
            if path!='/v1/models':
                requested_id=path[len('/v1/models/'):]; requested_id='gl/'+requested_id[5:] if requested_id.startswith('intl/') else requested_id
                model=next((m for m in result['data'] if m['id']==requested_id),None); require(model,'Model not found',404,'model_not_found'); result=model
            return jresponse(result)
        if path=='/v1/messages/count_tokens' and method=='POST':
            body=await json_body(req);model=body.get('model','');prefix,sep,mid=model.partition('/')
            if sep and prefix in ('cn','gl','intl'):
                region=internal_region(prefix);require(principal['scope']=='all' or principal['scope']==region,'Key cannot access this region',403)
            payload={k:body[k] for k in ('system','messages','tools') if k in body}
            require(isinstance(body.get('messages'),list),'messages required')
            # Advisory estimate only: the upstream does not expose an exact tokenizer.
            count=max(1,(len(json.dumps(payload,ensure_ascii=False).encode('utf-8'))+2)//3)
            response=jresponse({'input_tokens':count});response.headers['X-WB-Token-Count']='estimate';return response
        if path in ('/v1/chat/completions','/v1/responses','/v1/messages','/v2/chat/completions') and method=='POST': return await self.generate(req,principal)
        raise Fault(404,'Unknown route','not_found')

def request_ip(req):
    peer=req.remote or ''; trusted=set(filter(None,os.environ.get('WB_TRUSTED_PROXY_IPS','').split(',')))
    def clean(ip):
        try: return str(ipaddress.ip_address(ip.strip()).ipv4_mapped or ipaddress.ip_address(ip.strip())) if ':' in ip else str(ipaddress.ip_address(ip.strip()))
        except (ValueError,AttributeError):
            try: return str(ipaddress.ip_address(ip.strip()))
            except ValueError: return None
    peer=clean(peer) or peer; chain=[clean(x) for x in req.headers.get('X-Forwarded-For','').split(',')]
    if peer in trusted and 0<len(chain)<=20 and all(chain):
        for ip in reversed(chain):
            if peer not in trusted: break
            peer=ip
    return peer
async def json_body(req):
    try: b=await req.json()
    except (ValueError,UnicodeError): raise Fault(400,'Invalid JSON')
    require(isinstance(b,dict),'JSON object required'); return b
def error_body(e,rid=None):
    return {'error':{'message':str(e) if isinstance(e,Fault) else 'Request failed','type':getattr(e,'code','proxy_error'),'code':getattr(e,'code','proxy_error'),'param':None,'request_id':rid or uid('req_'),**({'risk':e.risk} if getattr(e,'risk',None) else {})}}
def create_app(gateway):
    @web.middleware
    async def guard(req,handler):
        origin=req.headers.get('Origin'); allowed=False
        try:
            host=req.headers.get('Host',''); h=urllib.parse.urlsplit('//'+host).hostname
            allowed=not origin or origin in gateway.allowed_origins or h in ('127.0.0.1','localhost','::1') and origin in ('http://'+host,'https://'+host)
            require(allowed,'Origin not allowed',403,'origin_not_allowed')
            if req.method=='OPTIONS': result=web.Response(status=204)
            else: result=await handler(req)
        except web.HTTPRequestEntityTooLarge: result=jresponse(error_body(Fault(413,'Request body too large','body_too_large')),413)
        except Fault as e:
            result=jresponse(anthropic_error(e) if req.path.startswith('/v1/messages') else error_body(e),e.status)
            if e.risk: result.headers['Retry-After']=str(max(0,int((e.risk.get('retry_at',stamp())-stamp()+999)//1000)))
        except asyncio.CancelledError: raise
        except Exception: result=jresponse(error_body(Fault(500,'Internal request failure','internal_error')),500)
        if allowed and origin and not result.prepared:
            result.headers.update({'Access-Control-Allow-Origin':origin,'Access-Control-Allow-Credentials':'true','Access-Control-Allow-Headers':'Authorization, Content-Type, X-Api-Key, Anthropic-Version, Anthropic-Beta, X-WB-UI, X-WB-Region, X-WB-Account','Access-Control-Allow-Methods':'GET, POST, DELETE, OPTIONS','Vary':'Origin'})
        return result
    app=web.Application(client_max_size=16*1024*1024,middlewares=[guard]); app.router.add_route('*','/{tail:.*}',gateway.handle); app.on_startup.append(gateway.start); app.on_cleanup.append(gateway.close); return app
