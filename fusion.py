"""Pure ports of NOOP MetricArbitrationPolicy, FusionResolver and DayOwnerResolver.

Reference 7f396e98ed9d259df08e3a0a58cfac05fc70615c. Adapter aliases do not
equate official recovery/strain with NOOP Charge/Effort, or SDNN with RMSSD.
"""
import math

PRIORITY = {s:i for i,s in enumerate(('whoopImport','noopComputed','appleHealth','healthConnect','xiaomiBand','nutritionCsv','localCache'))}
TOLERANCES = {'restingHR':(3,8,False),'heartRate':(5,12,False),'hrv':(8,20,False),'spo2':(2,4,False),'skinTemp':(.5,1.5,False),'steps':(.10,.30,True),'sleep':(20,60,False),'calories':(.15,.40,True),'other':(.10,.30,True)}

def metric_kind(key):
    if key in ('rhr','resting_hr'): return 'restingHR'
    if key in ('avg_hr','max_hr'): return 'heartRate'
    if key=='hrv': return 'hrv'
    if key=='spo2': return 'spo2'
    # BOOP keeps absolute temperature and deviation as separate quantities; both
    # use the reference's absolute degree-C spread, never a percent-of-deviation.
    if key in ('skin_temp','skinTemp','skin_temp_deviation_c'): return 'skinTemp'
    if key=='steps': return 'steps'
    if key in ('sleep_total_min','asleep_min','sleep_deep_min','deep_min','sleep_rem_min','rem_min','sleep_light_min','core_min','in_bed_min'): return 'sleep'
    if key in ('active_kcal','energy_kcal'): return 'calories'
    return 'other'

def tier(kind,source):
    if kind=='steps': return 0 if source in ('xiaomiBand','appleHealth','healthConnect') else 3
    if kind=='sleep': return {'whoopImport':0,'noopComputed':1,'xiaomiBand':1,'appleHealth':2,'healthConnect':2}.get(source,3)
    if kind=='calories': return 2 if source in ('appleHealth','healthConnect') else 3
    if kind=='other' and source=='nutritionCsv': return 0
    return {'whoopImport':0,'xiaomiBand':0,'noopComputed':1,'appleHealth':2,'healthConnect':2}.get(source,3)

def reason(kind,source):
    if kind=='steps' and source in ('xiaomiBand','appleHealth','healthConnect'): return 'counts directly'
    if kind=='steps' and source in ('whoopImport','noopComputed'): return 'step estimate'
    if kind=='sleep' and source=='whoopImport': return 'best stager'
    if kind=='sleep' and source=='noopComputed': return 'computed stages'
    if kind=='sleep' and source in ('appleHealth','healthConnect'): return 'phone sleep buckets'
    if kind=='skinTemp': return 'worn sensor'
    return {0:'direct sensor',1:'computed on device',2:'phone aggregate'}.get(tier(kind,source),'estimate')

def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)

def resolve(metric,inputs):
    """Inputs use exact NOOP source categories; preserve supplied value and provenance."""
    kind=metric_kind(metric)
    rows=[dict(row,tier=tier(kind,row['source']),source_priority=PRIORITY.get(row['source'],6),reason=reason(kind,row['source'])) for row in inputs if finite(row.get('value'))]
    rows.sort(key=lambda r:(r['tier'],r['source_priority']))
    if not rows: return None
    winner=rows[0]; agreement='single'
    if len(rows)>1:
        agree,minor,percent=TOLERANCES[kind]
        if percent: agree*=abs(winner['value']); minor*=abs(winner['value'])
        states=[]
        for row in rows[1:]:
            delta=abs(row['value']-winner['value'])
            states.append(0 if delta<=agree else 1 if delta<=minor else 2)
        agreement=('agree','minorDelta','conflict')[max(states)]
    return dict(metric=metric,value=winner['value'],source=winner.get('source_id',winner['source']),winning_source=winner['source'],contributors=rows,agreement=agreement)

def day_owner(day,locked_owner,candidates):
    """NOOP DayOwnerResolver: lock wins even without data; otherwise lowest priority."""
    if locked_owner is not None: return locked_owner
    rows=sorted((r for r in candidates if r.get('has_data')),key=lambda r:r['priority'])
    return rows[0]['device_id'] if rows else None

def source_kind(source,computed=False):
    source=str(source or '')
    if source in PRIORITY: return source
    low=source.lower()
    if low in ('my-whoop','whoop') or low.startswith('whoop-'): return 'whoopImport'
    if low.startswith('apple'): return 'appleHealth'
    if low.startswith('health-connect'): return 'healthConnect'
    if low in ('mi-fitness','xiaomi-band'): return 'xiaomiBand'
    if low.startswith('nutrition'): return 'nutritionCsv'
    return 'noopComputed' if computed else 'localCache'

ALIASES = {
 'rhr':('resting_hr','rhr','Resting heart rate (bpm)'),
 'hrv':('hrv_ms','hrv','Heart rate variability (ms)'),
 'hrv_sdnn':('hrv_sdnn',),
 'avg_hr':('average_hr','avg_hr','Average HR (bpm)'), 'max_hr':('max_hr','Max HR (bpm)'),
 'spo2':('blood_oxygen','spo2','spo2_pct','Blood oxygen %'),
 'skin_temp':('skin_temperature','skin_temp_c','Skin temp (celsius)'),
 'skin_temp_deviation_c':('skin_temp_deviation_c',),
 'steps':('steps',), 'sleep_total_min':('total_sleep_min','sleep_total_min','Asleep duration (min)'),
 'sleep_deep_min':('deep_min','Deep (SWS) duration (min)'), 'sleep_rem_min':('rem_min','REM duration (min)'),
 'sleep_light_min':('light_min','core_min','Light sleep duration (min)'), 'in_bed_min':('in_bed_min','In bed duration (min)'),
 'active_kcal':('active_kcal',), 'energy_kcal':('energy_kcal','calories','Energy burned (cal)'),
 'recovery':('recovery','Recovery score %'), 'strain':('strain','Day Strain'),
 'sleep_performance':('sleep_performance','Sleep performance %'),
 'charge':('charge',), 'effort':('effort',), 'rest':('rest',),
 'reference_readiness_score':('reference_readiness_score',), 'reference_sleep_score':('reference_sleep_score',),
}

def hrv_identity(record):
    identity=str(record.get('hrv_method',record.get('hrv_metric',''))).lower()
    if 'sdnn' in identity: return 'SDNN'
    if 'rmssd' in identity: return 'RMSSD'
    if str(record.get('source','')).lower().startswith('apple'): return 'SDNN'
    return 'RMSSD'

def _values(record,computed=False):
    raw=dict(record.get('original') or {}); raw.update(record)
    energy=record.get('calories')
    if computed and isinstance(energy,dict) and finite(energy.get('active_kcal')):
        raw['active_kcal']={'value':energy['active_kcal'],**{key:energy[key] for key in ('source','coverage','unit','estimated') if key in energy}}
    sleep=record.get('sleep')
    if computed and isinstance(sleep,dict):
        main=sleep.get('main') or sleep.get('imported_summary') or {}
        raw.update({k:v for k,v in main.items() if k in ('total_sleep_min','in_bed_min','deep_min','rem_min','light_min')})
        stages=main.get('stage_seconds') or {}
        for stage in ('deep','rem','light'):
            if finite(stages.get(stage)): raw[stage+'_min']=stages[stage]/60
    for metric,aliases in ALIASES.items():
        for alias in aliases:
            value=raw.get(alias); provenance=value if isinstance(value,dict) else {}
            if isinstance(value,dict): value=value.get('value')
            if isinstance(value,str):
                try: value=float(value)
                except ValueError: continue
            if finite(value):
                # Imported HRV summaries declare RMSSD/SDNN identity explicitly.
                identity=hrv_identity({**record,**provenance})
                key='hrv_sdnn' if metric=='hrv' and identity=='SDNN' else metric
                yield key,value,provenance
                break

def fuse_day(computed,imported_records,settings=None):
    """Adapt one canonical BOOP day. Unknown source types retain cache tier, not invented trust.

    settings may supply day_owner_lock and day_owner_candidates from NOOP's ownership
    table/registry contract. Source preferences do not override this trust-policy engine.
    """
    computed=computed or {}; settings=settings or {}; day=computed.get('day')
    inputs={}; sources={}; seen=set(); skipped=0
    for is_computed,record in [(True,computed),*((False,r) for r in imported_records)]:
        record_day=record.get('date') or record.get('day')
        if day and record_day and record_day!=day: skipped+=1; continue
        for metric,value,provenance in _values(record,is_computed):
            source=provenance.get('source') or record.get('source') or ('computed NOOP' if is_computed else 'local-cache')
            category=source_kind(source,is_computed and provenance.get('source_category')!='imported_summary')
            identity=(metric,str(source),value)
            if identity in seen: continue
            seen.add(identity)
            row=dict(source=category,source_id=source,value=value,record_id=provenance.get('record_id',record.get('id')))
            inputs.setdefault(metric,[]).append(row); sources.setdefault(str(source),set()).add(metric)
    winners={key:resolve(key,rows) for key,rows in inputs.items()}
    agreements={key:row['agreement'] for key,row in winners.items()}
    candidates=settings.get('day_owner_candidates')
    if candidates is None:
        categories={r['source_id']:r['source'] for rows in inputs.values() for r in rows}
        candidates=[dict(device_id=source,priority=PRIORITY[categories[source]],has_data=True) for source in sources]
    owner=day_owner(day,settings.get('day_owner_lock'),candidates)
    return dict(day=day,winners=winners,agreements=agreements,conflicts={k:v for k,v in winners.items() if v['agreement']=='conflict'},sources={k:sorted(v) for k,v in sources.items()},day_owner=owner,policy='NOOP MetricArbitrationPolicy/FusionResolver; winner verbatim; no averaging; official score identities retained',coverage=dict(metrics=len(winners),contributing_sources=len(sources),skipped_other_days=skipped))
