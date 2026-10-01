"""Configurable, local journal with explicit missingness and auditable comparisons."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import math
import statistics
import uuid
import hashlib

TZ=timezone(timedelta(hours=10))
DEFAULTS={'coffee','late_eating','happiness','stress','workouts'}
CATALOG=[]
def group(category,items):
    for item in items:
        name,kind,*extra=item
        key=name.lower().replace('/',' ').replace('-',' ').replace(' ','_')
        CATALOG.append(dict(id=key,name=name,category=category,type=kind,unit=extra[0] if extra else '',
            options=extra[1] if len(extra)>1 else [],enabled=key in DEFAULTS,order=len(CATALOG)))
group('Diet',[('Gluten-free','boolean'),('Dairy-free','boolean'),('Fasting','duration','min'),('Meal times','event'),('Late eating','boolean'),('Protein','quantity','g'),('Fruit/vegetables','quantity','servings'),('Sugary foods','boolean'),('Takeaway','boolean'),('Digestive discomfort','rating')])
group('Drinks',[('Coffee','quantity','cups'),('Tea','quantity','cups'),('Energy drinks','quantity','cans'),('Alcohol','quantity','drinks'),('Water','quantity','mL')])
group('Supplements',[(name,'supplement') for name in ('Zinc','Magnesium','Vitamin D','Iron','Creatine','Omega-3','Multivitamin')])
group('Mood',[(name,'rating') for name in ('Happiness','Calmness','Stress','Anxiety','Sadness','Irritability','Motivation','Concentration','Social connection')])
group('Sexual wellbeing',[('Masturbation','boolean'),('Partnered sex','boolean'),('Orgasm','boolean'),('Libido','rating'),('Sexual discomfort','rating')])
group('Activity',[('Workouts','boolean'),('Walking','duration','min'),('Stretching','duration','min'),('Outdoor time','duration','min'),('Prolonged sitting','duration','min'),('Demanding physical work','boolean')])
group('Sleep habits',[('Naps','duration','min'),('Evening screens','duration','min'),('Bedtime routine','boolean'),('Unusual sleeping location','boolean'),('Overnight disturbances','event')])
group('Daily context',[('Illness','boolean'),('Pain','rating'),('Medication','event'),('Travel','boolean'),('Workload','rating'),('Custom notes','text')])
group('Cycle',[('Bleeding','choice','',['None','Spotting','Light','Medium','Heavy']),('Cycle symptoms','event'),('Period start','boolean'),('Cycle event','event'),('Temperature','quantity','°C'),('Fertility observations','text')])
TYPES={'boolean','rating','quantity','time','duration','event','supplement','text','choice'}
METRICS={'sleep_min':('Sleep','min'),'resting_hr':('RHR','bpm'),'hrv':('RHRV','ms'),'effort':('Effort','%'),'rest':('Sleep score','%')}

def finite(value):return type(value) in (int,float) and math.isfinite(value)
def day_key(value):
    try:return date.fromisoformat(value).isoformat()
    except (TypeError,ValueError):raise ValueError('Choose a valid journal date') from None
def shift(day,n):return (date.fromisoformat(day)+timedelta(days=n)).isoformat()
def clean_text(value,maximum=500):
    if not isinstance(value,str) or len(value)>maximum:raise ValueError('Journal text is too long or invalid')
    return value.strip()

def observed_value(rows,item):
    """Events sum quantities; unknowns and incompatible units remain missing."""
    if not rows:return None
    if item['type'] in ('boolean','supplement'):
        answers=[r['answer'] for r in rows if type(r.get('answer')) is bool]
        return any(answers) if answers else None
    values=[r for r in rows if finite(r.get('value'))]
    if not values:return None
    if item['type']=='rating':return statistics.mean(r['value'] for r in values)
    daily=[r for r in values if r.get('mode')=='daily']
    if daily:values=[max(daily,key=lambda r:r.get('updated_ms',0))]
    if len({r.get('unit',item.get('unit','')) for r in values})>1:return None
    return sum(r['value'] for r in values)

def compare_observations(records,item,days,metric='sleep_min',lag=1,quantity=False):
    if metric not in METRICS or lag not in (0,1):raise ValueError('Choose a supported outcome and day alignment')
    by_day=defaultdict(list)
    for row in records:by_day[row['day']].append(row)
    outcomes={d['day']:d.get(metric) for d in days};pairs=[]
    for day,rows in sorted(by_day.items()):
        exposure=observed_value(rows,item|{'type':'quantity'} if quantity and item['type']=='supplement' else item);outcome_day=shift(day,lag);outcome=outcomes.get(outcome_day)
        if exposure is None or not finite(outcome):continue
        if quantity and type(exposure) is bool:continue
        if not quantity and type(exposure) is not bool:continue
        pairs.append(dict(day=day,outcome_day=outcome_day,exposure=exposure,value=outcome,
            entry_ids=[r['id'] for r in rows],source=next((d.get('sources',{}).get(metric,'BOOP local estimate') for d in days if d['day']==outcome_day),'BOOP local estimate')))
    yes=[p['value'] for p in pairs if p['exposure'] is True];no=[p['value'] for p in pairs if p['exposure'] is False]
    ready=len(pairs)>=7 if quantity else min(len(yes),len(no))>=5
    result=dict(metric=metric,unit=METRICS[metric][1],lag=lag,quantity=quantity,pairs=pairs,logged_days=len(by_day),paired_days=len(pairs),
        missing_outcomes=len(by_day)-len(pairs),calendar_days=len(days),status='observed' if ready else 'learning',
        yes_n=len(yes),no_n=len(no),yes_mean=statistics.mean(yes) if yes else None,no_mean=statistics.mean(no) if no else None,
        delta=(statistics.mean(yes)-statistics.mean(no)) if ready and not quantity else None,
        note='Association only. Unlogged days are unknown; they are never controls. Five measured Yes and No days, or seven quantity pairs, unlock comparison.')
    if quantity:
        units={r.get('unit',item.get('unit','')) for r in records if finite(r.get('value'))}
        result['exposure_unit']=next(iter(units),item.get('unit',''))
        if len(units)>1:result.update(status='learning',pairs=[],paired_days=0,missing_outcomes=len(by_day),note='Mixed units: compare one consistent unit before calculating an association.');return result
        xs=[p['exposure'] for p in pairs];ys=[p['value'] for p in pairs]
        if ready and len(set(xs))>1 and len(set(ys))>1:result['correlation']=statistics.correlation(xs,ys)
        else:result['correlation']=None
    return result

class JournalService:
    def __init__(self,features,analytics):self.features,self.analytics=features,analytics
    def catalog(self):
        saved={r.get('catalog_id'):r for r in self.features.list_records('question',{'limit':10000}) if r.get('journal_catalog')}
        rows=[]
        for base in CATALOG:
            config=saved.pop(base['id'],{})
            rows.append(base|{k:config[k] for k in ('enabled','order') if k in config})
        for key,config in saved.items():
            if key and config.get('type') in TYPES:rows.append({k:config.get(k) for k in ('name','category','type','unit','options','enabled','order')}|{'id':key,'custom':True})
        names={r['name'].casefold() for r in rows}
        for entry in self.features.list_records('journal',{'limit':100000}):
            name=str(entry.get('question','')).strip()
            if not name or name.casefold() in names:continue
            names.add(name.casefold());key='legacy-'+hashlib.sha256(name.casefold().encode()).hexdigest()[:16]
            kind='boolean' if type(entry.get('answer')) is bool or entry.get('source_table')=='journal' and entry.get('answer') in (0,1) else 'quantity' if finite(entry.get('value')) else 'text'
            rows.append(dict(id=key,name=name,category='Imported',type=kind,unit=entry.get('unit',''),options=[],enabled=False,order=len(rows),legacy=True))
        return sorted(rows,key=lambda r:(r.get('order') or 0,r['name']))
    def configure(self,body):
        existing={r['id']:r for r in self.catalog()};items=body.get('items',[])
        if not isinstance(items,list) or len(items)>200:raise ValueError('Choose up to 200 tracking items')
        prepared=[]
        for patch in items:
            key=patch.get('id');base=existing.get(key)
            if base is None:raise ValueError('Unknown tracking item')
            enabled=patch.get('enabled',base['enabled']);order=patch.get('order',base['order'])
            if type(enabled) is not bool or type(order) is not int or not 0<=order<=1000:raise ValueError('Invalid tracking preference')
            prepared.append(base|dict(enabled=enabled,order=order))
        if body.get('custom'):
            custom=body['custom'];name=clean_text(custom.get('name',''),80);kind=custom.get('type')
            if not name or kind not in TYPES-{'choice'}:raise ValueError('Choose a name and supported input type')
            prepared.append(dict(id='custom-'+uuid.uuid4().hex,name=name,type=kind,unit=clean_text(custom.get('unit',''),40),category='Custom',options=[],enabled=True,order=len(existing)))
        with closing(self.features.store.connect()) as conn:
            with conn:
                for row in prepared:
                    self.features._save(conn,'question',row|dict(id='journal-item:'+row['id'],catalog_id=row['id'],journal_catalog=True,source='manual'))
        return self.catalog()
    def records(self):
        catalog=self.catalog();names={r['name'].casefold():r['id'] for r in catalog};result=[]
        for row in self.features.list_records('journal',{'limit':100000}):
            key=row.get('item_id') or names.get(str(row.get('question','')).casefold());day=row.get('day',row.get('date'))
            try:day=day_key(day)
            except ValueError:continue
            if row.get('source_table')=='journal' and type(row.get('answer')) is int and row['answer'] in (0,1):row=row|{'answer':bool(row['answer'])}
            if key:result.append(row|dict(item_id=key,day=day))
        return sorted(result,key=lambda r:(r['day'],r.get('event_time') or '',r.get('created_ms',0)))
    def save(self,body):
        day=day_key(body.get('day'));catalog={r['id']:r for r in self.catalog()};raw=body.get('entries',[])
        if not isinstance(raw,list) or len(raw)>200:raise ValueError('Save up to 200 entries at a time')
        current={r['id']:r for r in self.records()};prepared=[]
        clears=body.get('clear_ids',[])
        if not isinstance(clears,list) or len(clears)>200 or any(key not in current or current[key]['day']!=day for key in clears):raise ValueError('Only entries on the selected date can be cleared')
        for item in raw:
            spec=catalog.get(item.get('item_id'))
            if not spec:raise ValueError('Unknown journal item')
            mode=item.get('mode','daily')
            if mode not in ('daily','event'):raise ValueError('Choose daily answer or event')
            record_id=item.get('id') or ('journal-day:'+day+':'+spec['id'] if mode=='daily' else 'journal-event:'+uuid.uuid4().hex)
            prior=current.get(record_id)
            if not item.get('id') and prior and prior['day']!=day:
                record_id+=':'+uuid.uuid4().hex
                prior=None
            if item.get('id') and not prior:raise ValueError('Entry no longer exists; reload before editing')
            if prior and prior.get('item_id')!=spec['id']:raise ValueError('An entry cannot change its tracking item')
            row=dict(id=record_id,day=day,date=day,item_id=spec['id'],question=spec['name'],input_type=spec['type'],mode=mode,source='manual',journal_version=2)
            answer=item.get('answer');value=item.get('value');text=clean_text(item.get('text',''),2000)
            if spec['type'] in ('boolean','supplement'):
                if type(answer) is not bool:raise ValueError('Choose Yes or No, or leave the item unlogged')
                row['answer']=answer
            elif spec['type'] in ('rating','quantity','duration'):
                if not finite(value) or (spec['type']=='rating' and (not 1<=value<=5 or value!=int(value))) or (spec['type']!='rating' and not 0<=value<=1_000_000):raise ValueError('Enter a valid quantity or rating from 1 to 5')
                row['value']=value
            elif spec['type']=='choice':
                if text not in spec['options']:raise ValueError('Choose one of the listed values')
                row['text']=text
            elif spec['type']=='time':
                try:datetime.strptime(text,'%H:%M')
                except ValueError:raise ValueError('Enter a time as HH:MM') from None
                row['text']=text
            else:
                if not text:raise ValueError('Enter an event or note, or leave it unlogged')
                row['text']=text
            if spec['type']=='supplement' and value is not None and answer is True:
                if not finite(value) or not 0<=value<=1_000_000:raise ValueError('Enter a valid optional amount')
                row['value']=value
            row['unit']=clean_text(item.get('unit',spec.get('unit','')),40)
            if spec['type']=='supplement' and value is not None and answer is True and not row['unit']:raise ValueError('Give the supplement amount a unit')
            row['product']=clean_text(item.get('product',''),120);row['note']=clean_text(item.get('note',''),2000)
            event_time=item.get('event_time','')
            if event_time:
                try:occurred=datetime.strptime(day+' '+event_time,'%Y-%m-%d %H:%M').replace(tzinfo=TZ)
                except (ValueError,TypeError):raise ValueError('Enter an event time as HH:MM') from None
                row['event_time']=event_time;row['timestamp_ms']=int(occurred.timestamp()*1000)
            prepared.append(row)
        with closing(self.features.store.connect()) as conn:
            with conn:
                saved=[self.features._save(conn,'journal',row) for row in prepared]
                for key in clears:
                    now=int(datetime.now(TZ).timestamp()*1000)
                    conn.execute('INSERT INTO feature_revisions(record_id,kind,payload_json,deleted_ms,saved_ms) SELECT id,kind,payload_json,deleted_ms,? FROM feature_records WHERE id=?',(now,key))
                    conn.execute('UPDATE feature_records SET deleted_ms=?,updated_ms=? WHERE id=?',(now,now,key))
        return {'entries':saved}
    def checkin(self,day):
        day=day_key(day);items=self.catalog();history=self.records();rows=[r for r in history if r['day']==day]
        enabled=[r['id'] for r in items if r['enabled']];logged={r['item_id'] for r in rows}
        carryable={r['id'] for r in items if r['enabled'] and r['type'] not in ('event','text') and r['id']!='period_start'}
        carried={}
        for row in sorted(history,key=lambda r:(r['day'],r.get('updated_ms',0))):
            if row['day']<day and row.get('mode')=='daily' and row['item_id'] in carryable and row['item_id'] not in logged:
                carried[row['item_id']]=row
        return dict(day=day,items=items,entries=rows,carried=list(carried.values()),logged=sum(key in logged for key in enabled),enabled=len(enabled),note='Previous answers are unsaved drafts. Confirm & save explicitly submits the selected day. Skipped items stay unknown.')
    def summary(self,device,through,days=30,item_id=None,metric='sleep_min',lag=1,mode='auto',period='week'):
        through=day_key(through);days=min(90,max(7,int(days)));first=shift(through,1-days)
        window=30 if period=='month' else 7
        bundle=self.analytics.trends(device,max(days+7,window*2),self.features.settings(),through);observations=[]
        for day in bundle.get('days',[]):
            main=(day.get('sleep') or {}).get('main') or {};row={'day':day['day'],'sources':{}}
            row['sleep_min']=main.get('total_sleep_min');row['bedtime']=main.get('start');row['wake']=main.get('end')
            row['sources']['sleep_min']=main.get('source','BOOP estimated sleep')
            for key in METRICS:
                if key=='sleep_min':continue
                item=day.get(key) or {};row[key]=item.get('value') if isinstance(item,dict) else item
                row['sources'][key]=item.get('source','BOOP local estimate') if isinstance(item,dict) else 'BOOP local estimate'
            observations.append(row)
        catalog=self.catalog();records=[r for r in self.records() if first<=r['day']<=through]
        selected=next((r for r in catalog if r['id']==item_id),next((r for r in catalog if r['enabled']),catalog[0]))
        chosen=[r for r in records if r['item_id']==selected['id']]
        comparison=compare_observations(chosen,selected,[d for d in observations if d['day']>=first],metric,int(lag),mode=='quantity' or selected['type'] not in ('boolean','supplement'))
        changes=[]
        for key,(name,unit) in METRICS.items():
            current=[dict(day=r['day'],value=r.get(key),source=r['sources'].get(key)) for r in observations if shift(through,1-window)<=r['day']<=through and finite(r.get(key))]
            previous=[dict(day=r['day'],value=r.get(key),source=r['sources'].get(key)) for r in observations if shift(through,1-window*2)<=r['day']<shift(through,1-window) and finite(r.get(key))]
            changes.append(dict(metric=key,name=name,unit=unit,current=current,previous=previous,
                delta=statistics.mean(p['value'] for p in current)-statistics.mean(p['value'] for p in previous) if min(len(current),len(previous))>=3 else None))
        first_log=min((r['day'] for r in self.records() if r['item_id']==selected['id']),default=None)
        baseline=[r for r in observations if first_log and shift(first_log,-7)<=r['day']<first_log and finite(r.get(metric))]
        cycle_rows=[r for r in records if r['item_id'] in {i['id'] for i in catalog if i['category']=='Cycle'}]
        starts=[r['day'] for r in self.records() if r['item_id']=='period_start' and r.get('answer') is True and r['day']<=through]
        for record in self.features.list_records('cycle',{'limit':10000}):
            try:start=day_key(record.get('period_start'))
            except (ValueError,TypeError):continue
            if start<=through:starts.append(start)
        starts=sorted(set(starts));lengths=[(date.fromisoformat(b)-date.fromisoformat(a)).days for a,b in zip(starts,starts[1:])]
        return dict(through=through,first=first,days=[r for r in observations if r['day']>=first],items=catalog,entries=records,selected=selected,
            comparison=comparison,changes=changes,change_window=window,prior_baseline=baseline,
            cycle=dict(entries=cycle_rows,starts=starts,lengths=lengths,day=(date.fromisoformat(through)-date.fromisoformat(starts[-1])).days+1 if starts else None,
                note='Cycle day counts from a logged period start. Observations do not establish ovulation or fertile days.'))
