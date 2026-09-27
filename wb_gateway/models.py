import re, math, time
from datetime import datetime
from zoneinfo import ZoneInfo
from .storage import require, Fault, now, stamp, atomic, uid

def number(x): return type(x) in (int,float) and math.isfinite(x) and x>=0
def visible(product):
    agents=product.get('agents',[])
    agent=next((a for a in agents if a.get('name')=='craft'),None) or next((a for a in agents if 'default' in a.get('tags',[])),None) or next((a for a in agents if a.get('name')=='cli'),None)
    available=set(product.get('availableModels') or [])
    ids=set(agent['models']) if agent and isinstance(agent.get('models'),list) else available
    require(bool(ids),'Official chat model visibility unavailable',503,'catalog_visibility_unavailable')
    out={}; excluded={'text-to-image','text-to-video','image-to-video'}
    for m in product.get('models',[]):
        i=m.get('id')
        if isinstance(i,str) and i in ids and (not available or i in available) and not m.get('local') and not m.get('url') and not m.get('disabled') and m.get('enabled') is not False and not m.get('hidden') and not excluded.intersection(m.get('tags',[])):
            out.setdefault(i,m)
    return list(out.values())
def controls(m):
    r=m.get('reasoning') or {}; allowed={'minimal','low','medium','high','xhigh','max'}
    efforts=[e for e in r.get('supportedEfforts',[]) if e in allowed]
    lengths=sorted({x for x in m.get('contextWindow',{}).get('supportedLengths',[]) if number(x) and x>0 and x<=m.get('maxInputTokens',float('inf'))})
    return {'id':m['id'],'name':m.get('name',m['id']),'supports_reasoning':bool(m.get('supportsReasoning')),'can_disable':not m.get('onlyReasoning',False) and r.get('canDisableThinking') is not False,'efforts':efforts,'default_effort':r.get('defaultEffort',r.get('effort')),'context_lengths':lengths if len(lengths)>1 else [],'default_context':(m.get('contextWindow',{}).get('defaultLength') if m.get('contextWindow',{}).get('defaultLength') in lengths else lengths[0]) if len(lengths)>1 else m.get('maxInputTokens'),'max_input_tokens':m.get('maxInputTokens'),'max_output_tokens':m.get('maxOutputTokens'),'supports_tools':bool(m.get('supportsToolCall')),'supports_images':bool(m.get('supportsImages')),'temperature':m.get('temperature'),'related_models':m.get('relatedModels',{})}
def multiplier(raw):
    if not isinstance(raw,str): return None
    m=re.fullmatch(r'(?:[x×]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*[x×])(?:\s*credits?)?',raw.strip(),re.I)
    return float(m[1] or m[2]) if m else None
def active(p):
    if p.get('enabled') is False: return False
    s=p.get('schedule') or {}
    try:
        for k,start in [('validFrom',True),('validUntil',False)]:
            if s.get(k):
                t=datetime.fromisoformat(s[k].replace('Z','+00:00')).timestamp()
                if (start and time.time()<t) or (not start and time.time()>=t): return False
        if not s.get('daily'): return True
        d=datetime.now(ZoneInfo(s['timezone'])) if s.get('timezone') else datetime.now(); minute=d.hour*60+d.minute
        for w in s['daily']:
            a,b=[sum(int(z)*f for z,f in zip(w[k].split(':'),(60,1))) for k in ('start','end')]
            if a==b or (a<b and a<=minute<b) or (a>b and (minute>=a or minute<b)): return True
    except (ValueError,KeyError,TypeError): return False
    return False
def quote(m,promotions):
    raw=m.get('credits'); base=multiplier(raw); value=base; source='not_reported' if base is None else 'models.credits'
    ps=[p for p in promotions if m['id'] in p.get('modelIds',[])]+m.get('promotions',[])
    ps=sorted([p for p in ps if ('modelIds' not in p or m['id'] in p['modelIds']) and active(p)],key=lambda p:p.get('priority',0),reverse=True)
    p=ps[0] if ps else None; discount=(p or {}).get('discount') or {}
    explicit=multiplier(discount.get('discountedCredits'))
    if explicit is not None: value=explicit; source='modelPromotions.discountedCredits'
    elif base is not None and number(discount.get('factor')): value=float(format(base*discount['factor'],'.12g')); source='modelPromotions.factor'
    badge=(p or {}).get('badge') or {}
    return {'credits_raw':raw,'base_multiplier':base,'effective_multiplier':value,'status':'provided' if value is not None else 'unparsed' if raw else 'not_provided','source':source,'promotion':{'id':p.get('id'),'label':badge.get('label',badge.get('labelZh',badge.get('labelEn'))),'valid_until':p.get('schedule',{}).get('validUntil'),'factor':discount.get('factor'),'discounted_credits':discount.get('discountedCredits')} if p else None}
class Risk:
    def __init__(self,store):
        self.store=store
        changed=False
        for e in store.risks:
            if e.get('kind')=='channel_not_allowed' and e.get('scope')=='region' and str(e.get('upstream_code'))=='11128':
                e.update(scope='request',manual=False,retry_at=0,diagnostic_only=True,scope_corrected_at=now());changed=True
        if changed:self.save()
    def save(self): atomic(self.store.root/'risk-state.json',{'version':1,'entries':self.store.risks})
    def active(self,e): return not e.get('resolved_at') and (e.get('manual') or e.get('retry_at',0)>stamp())
    def matches(self,e,region,account,path):
        if e.get('scope')=='request': return False
        if e.get('kind')=='quota_exhausted' and '/chat/' not in path and not path.endswith('/daily-checkin'): return False
        if e.get('kind')=='login_required' and not account and '/auth/' in path and '/refresh' not in path: return False
        if e['scope']=='global': return True
        if e.get('region')!=region: return False
        if e['scope']=='account': return not e.get('account_id') or e['account_id']==account
        if e['scope']=='operation': return e.get('path')==path
        return True
    def check(self,region,account,path):
        for e in self.store.risks:
            if self.active(e) and self.matches(e,region,account,path): raise Fault(e.get('status',429),e.get('action','Upstream restriction active'),'risk_'+e['kind'],e)
    def observe(self,status,body,region,account,path,headers=None):
        err=body.get('error') if isinstance(body.get('error'),dict) else body
        code=str(err.get('code','')); msg=str(err.get('message',err.get('msg','')))
        kind=None; scope='account'; manual=True; delay=0; st=status
        if code=='11128' and 'unapproved channel' in msg.lower(): kind='channel_not_allowed'; scope='request'; manual=False; delay=0; st=status if 400<=status<500 else 400
        elif code=='10081': kind='ip_restricted'; scope='global'; st=403
        elif code in ('E0010458','E0010459'): kind='enterprise_quota'; scope='operation'; st=403
        elif re.search('captcha|turing|device.?token|device.?verification|risk.?control|验证码|人机验证|风控',code+' '+msg,re.I): kind='device_verification'; scope='global'; st=403
        elif status==401 or code=='401': kind='login_required'; st=401
        elif status==429 or code=='429' or re.search('rate_limit|too_many_requests',code,re.I): kind='rate_limited'; scope='region'; manual=False; delay=60000; st=429
        elif status==402 or re.search('insufficient_quota|quota_exceeded|insufficient_credits',code,re.I): kind='quota_exhausted'; st=402
        elif status==403 or code=='403': kind='permission_denied'; st=403
        elif status>=500: kind='upstream_unavailable'; scope='region'; manual=False; delay=5000; st=503
        if not kind: return None
        old=next((e for e in self.store.risks if self.active(e) and e['kind']==kind and e.get('region')==region and e.get('account_id')==account),None)
        count=(old or {}).get('count',0)+1; delay=min(delay*2**min(count-1,10),60000 if kind=='upstream_unavailable' else 900000)
        try: delay=max(delay,float((headers or {}).get('Retry-After',0))*1000)
        except ValueError: pass
        e={'id':(old or {}).get('id',uid()),'kind':kind,'scope':scope,'manual':manual,'region':region,'account_id':account,'path':path,'status':st,'retry_at':stamp()+delay,'count':count,'action':'请在官方渠道处理账号限制后手动恢复。' if manual else '上游暂时限制请求，冷却期间不换号或重放请求。','upstream_code':code,'request_id':(headers or {}).get('X-Request-ID'),'updated_at':now()}
        if kind=='channel_not_allowed':e['action']='上游拒绝本次请求（11128: unapproved channel）。原因尚未定位；不自动重放、不换号，也不据此封锁整个区域。'
        if old: old.update(e)
        else: self.store.risks.append(e)
        self.save(); return Fault(st,e['action'],'risk_'+kind,e)
    def list(self): return [e for e in self.store.risks if self.active(e)]
