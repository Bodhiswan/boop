"""Pure NOOP RhythmScreener, VitalBands, LabBookProjection and marker catalog ports.

Pinned reference 7f396e98ed9d259df08e3a0a58cfac05fc70615c. Descriptive wellness
outputs only: no rhythm diagnosis, alarm, lab reference-range invention or I/O.
"""
import datetime as dt
import math
import statistics as stats
from bisect import bisect_left
from analytics import clean_rr, correlation, finite

_MARKERS=[
 ('total_cholesterol','Total cholesterol','mmol/L',2),('ldl','LDL cholesterol','mmol/L',2),('hdl','HDL cholesterol','mmol/L',2),('triglycerides','Triglycerides','mmol/L',2),
 ('fasting_glucose','Fasting glucose','mmol/L',1),('hba1c','HbA1c','mmol/mol',0),('ferritin','Ferritin','µg/L',0),('iron','Serum iron','µmol/L',1),('transferrin_saturation','Transferrin saturation','%',0),('haemoglobin','Haemoglobin','g/L',0),
 ('vitamin_d','Vitamin D','nmol/L',0),('vitamin_b12','Vitamin B12','ng/L',0),('folate','Folate','µg/L',1),('tsh','TSH','mIU/L',2),('free_t4','Free T4','pmol/L',1),('crp','C-reactive protein (CRP)','mg/L',1),('egfr','eGFR','mL/min/1.73m²',0),('creatinine','Creatinine','µmol/L',0),('alt','ALT','U/L',0),('ast','AST','U/L',0),('ggt','GGT','U/L',0),('sodium','Sodium','mmol/L',0),('potassium','Potassium','mmol/L',1),
 ('bp_systolic','Blood pressure (systolic)','mmHg',0),('bp_diastolic','Blood pressure (diastolic)','mmHg',0),('resting_pulse','Resting pulse','bpm',0),('weight','Weight','kg',1),('body_fat','Body fat','%',1),('waist','Waist circumference','cm',1),('height','Height','cm',1)]

def marker_catalog():
    return [dict(key=k,display_name=name,category='bodyMeasurement' if i>=26 else 'bloodPressure' if i>=23 else 'bloodPanel',canonical_unit=unit,decimals=precision,reference_text_hint=None if i>=26 else 'From your own report (optional)',higher_is_better=None) for i,(k,name,unit,precision) in enumerate(_MARKERS)]

def _day(value):
    try:
        parsed=dt.date.fromisoformat(value)
        return parsed if parsed.isoformat()==value else None
    except (ValueError,TypeError): return None

def calendar_series(rows,end_day=None):
    by={day:value for day,value in rows if _day(day)}
    if not by:return []
    start=min(by); end=max(by) if not _day(end_day) else max(max(by),end_day)
    count=(_day(end)-_day(start)).days
    return [by.get((_day(start)+dt.timedelta(days=i)).isoformat()) for i in range(count+1)]

def _baseline(history,cfg):
    lo,hi,floor=cfg; center=(lo+hi)/2; spread=floor; n=missing=0
    for value in history:
        if not finite(value) or not lo<=value<=hi: missing+=1; continue
        missing=0
        if n==0:center=value;n=1;continue
        young=n<8
        if n>=4 and not young and abs(value-center)>5*spread:continue
        effective=spread*(2.5 if young else 1)
        lb=1-.5**(1/(3 if young else 14)); ls=1-.5**(1/21)
        bounded=max(center-3*effective,min(center+3*effective,value))
        center=lb*bounded+(1-lb)*center
        spread=max(floor,ls*abs(value-center)+(1-ls)*spread); n+=1
    return dict(mean=center,spread=spread,valid_nights=n,trusted=n>=14 and missing<=14,nights_since_update=missing)

def vital_band(value,history,population_range,cfg=None):
    if not finite(value):return dict(band='noData',basis='population',nights=0)
    state=_baseline(history,cfg) if cfg else None
    nights=state['valid_nights'] if state else 0
    if cfg and not cfg[0]<=value<=cfg[1]:return dict(band='outOfRange',basis='population',nights=nights)
    if state and state['trusted']:
        z=(value-state['mean'])/(1.253*state['spread'])
        return dict(band='inRange' if abs(z)<=2 else 'outOfRange',basis='personal',nights=nights,z=z)
    return dict(band='inRange' if population_range[0]<=value<=population_range[1] else 'outOfRange',basis='population',nights=nights)

def _value(day,key):
    value=day.get(key)
    return value.get('value') if isinstance(value,dict) else value if finite(value) else None

def vital_bands(day,history=()):
    specs={'respiration':((12,20),(4,40,.5)),'spo2':((95,100),None),'resting_hr':((40,60),(30,120,2)),'hrv':((40,120),(5,250,5))}
    skin=_value(day,'skin_temperature'); absolute=not finite(skin) or skin>=20
    specs['skin_temperature']=((33,36),(20,42,.3)) if absolute else ((-.6,.6),(-8,8,.3))
    result={}
    day_key=day.get('day'); before=(_day(day_key)-dt.timedelta(days=1)).isoformat() if _day(day_key) else None
    for key,(population,cfg) in specs.items():
        value=_value(day,key)
        rows=[(r['day'],_value(r,key)) for r in history if _day(r.get('day')) and (not day_key or r['day']<day_key)]
        series=calendar_series(rows,before)
        if key=='skin_temperature':series=[v if finite(v) and (v>=20)==absolute else None for v in series]
        result[key]=dict(value=value,**vital_band(value,series,population,cfg),population_range=list(population),coverage=dict(calendar_nights=len(series)),source='VitalBands.swift',note='Approximate personal comparison; population fallback before 14 usable nights or after a wear gap')
    return result

def rr_integrity(timestamps,rr):
    if len(timestamps)!=len(rr) or len(rr)<2:return dict(coverage=None,beat_accuracy_fraction=None,trustworthy=True)
    span=max(timestamps)-min(timestamps)
    coverage=sum(rr)/1000/span if span>0 else 0
    fraction=sum(abs(timestamps[i]-timestamps[i-1]-rr[i]/1000)<=.5 for i in range(1,len(rr)))/(len(rr)-1)
    return dict(coverage=coverage,beat_accuracy_fraction=fraction,trustworthy=not(coverage>1.10) and fraction>=.5)

def _rhythm_stats(clean):
    diffs=[b-a for a,b in zip(clean,clean[1:])]; rmssd=math.sqrt(stats.mean(v*v for v in diffs)); sdnn=stats.stdev(clean)
    sd1=rmssd/math.sqrt(2); sd2=math.sqrt(max(0,2*sdnn*sdnn-sd1*sd1)); ratio=sd1/sd2 if sd2>0 else None
    turns=sum((clean[i]-clean[i-1])*(clean[i+1]-clean[i])<0 for i in range(1,len(clean)-1))/(len(clean)-2)/(2/3)
    kept,_=clean_rr(clean)
    ect=(len(clean)-len(kept))/len(clean); norm=rmssd/stats.mean(clean)
    label='unreadable' if ratio is None else 'varied' if ratio>=.55 and norm>=.12 and turns>=.90 else 'occasionalEctopy' if ect>=.04 and turns<.90 else 'steady'
    return dict(label=label,sd1=sd1,sd2=sd2,sd1sd2=ratio,norm_rmssd=norm,turning_point_rate=turns,ectopic_fraction=ect)

def rhythm_window(rr_ms,motion_still,mean_hr=None,timestamps=(),ppg_ibi=None):
    clean=[float(v) for v in rr_ms if finite(v) and 300<=v<=2000]
    n=len(clean); confidence='calibrating' if n<60 else 'solid' if n>=200 else 'building'
    empty=dict(label='unreadable',sd1=None,sd2=None,sd1sd2=None,norm_rmssd=None,turning_point_rate=None,ectopic_fraction=None,n_beats=0 if not motion_still else n,confidence='calibrating' if not motion_still or n<60 else confidence,agreed_across_sources=False,poincare=[])
    integrity=rr_integrity(timestamps,rr_ms); empty['coverage']=integrity
    mean_hr=60000/stats.mean(clean) if mean_hr is None and clean else mean_hr
    if not motion_still or n<60 or not finite(mean_hr) or not 40<=mean_hr<=110 or not integrity['trustworthy']:return empty
    result=_rhythm_stats(clean)
    ppg=[float(v) for v in (ppg_ibi or []) if finite(v) and 300<=v<=2000]
    return dict(**result,n_beats=n,confidence=confidence,agreed_across_sources=len(ppg)>=60 and _rhythm_stats(ppg)['label']==result['label'],poincare=[dict(x=a,y=b) for a,b in zip(clean,clean[1:])],coverage=integrity)

def rhythm_summary(windows):
    readable=[r for r in windows if r['label']!='unreadable']; counts={label:sum(r['label']==label for r in readable) for label in ('steady','occasionalEctopy','varied')}
    varied=counts['varied']; overall='unreadable' if not readable else 'varied' if varied>=3 else 'occasionalEctopy' if varied or counts['occasionalEctopy'] else 'steady'
    # Exact shipped function counts three varied windows; declared span constant is not used.
    return dict(readable_windows=len(readable),steady_windows=counts['steady'],occasional_windows=counts['occasionalEctopy'],varied_windows=varied,variation_recurred=varied>=3,overall=overall)

def rhythm_night(day,rr=(),gravity=()):
    sleep=day.get('sleep') or {}; main=sleep.get('main') or {}
    start=main.get('start'); end=main.get('end'); windows=[]
    rr=sorted((float(t),float(v)) for t,v in rr if finite(t) and finite(v))
    gravity=sorted(row for row in gravity if len(row)==4 and all(finite(v) for v in row))
    rr_times=[r[0] for r in rr]; gravity_times=[r[0] for r in gravity]
    if finite(start) and finite(end) and end>start:
        t=start
        while t<end:
            stop=min(t+300,end); pairs=rr[bisect_left(rr_times,t):bisect_left(rr_times,stop)]
            if len(pairs)>=60:
                grav=[math.sqrt(x*x+y*y+z*z) for ts,x,y,z in gravity[bisect_left(gravity_times,t):bisect_left(gravity_times,stop)]]
                mean=stats.mean(grav) if grav else 0
                still=len(grav)>=4 and mean>0 and math.sqrt(stats.pvariance(grav))/mean<.03
                window=rhythm_window([v for ts,v in pairs],still,timestamps=[ts for ts,v in pairs]); window.update(start=t,end=stop); windows.append(window)
            t=stop
    night=rhythm_summary(windows); had_motion=any(start<=r[0]<end for r in gravity) if finite(start) and finite(end) else False
    pairs=[(ts,v) for ts,v in rr if finite(start) and finite(end) and start<=ts<end]
    accurate=rr_integrity([t for t,v in pairs],[v for t,v in pairs])['beat_accuracy_fraction']
    empty='none' if night['readable_windows'] else 'gatheringData' if had_motion or not windows else 'deviceBanksBeats' if accurate is not None and accurate<.5 else 'deviceNoMotion'
    return dict(windows=windows,night=night,empty_state=empty,coverage=dict(rr_intervals=len(pairs),gravity_samples=sum(start<=r[0]<end for r in gravity) if finite(start) and finite(end) else 0,eligible_windows=len(windows)),source='RhythmScreener.swift / RhythmHost 5-minute windows',note='Descriptive regularity visualization only; no condition verdict, notification or recommendation')

def project_labs(readings,fold='latest'):
    if fold not in ('latest','mean'):raise ValueError('Choose latest or mean daily fold')
    cells={}
    for row in readings:
        key=row.get('marker_key',row.get('markerKey',row.get('marker'))); day=row.get('day') or row.get('date'); value=row.get('value')
        if not key or not _day(day) or not finite(value):continue
        cells.setdefault((key,day),[]).append(row)
    result=[]
    for (key,day),group in sorted(cells.items()):
        def taken(row):
            explicit=row.get('taken_at_epoch',row.get('takenAtEpoch'))
            if finite(explicit):return explicit
            stamp=row.get('timestamp_ms') or row.get('updated_ms') or 0
            return stamp/1000 if finite(stamp) else 0
        best=max(enumerate(group),key=lambda pair:(taken(pair[1]),pair[0]))[1]
        result.append(dict(marker_key=key,day=day,value=stats.mean(r['value'] for r in group) if fold=='mean' else best['value']))
    return result

def pair_marker_wearable(marker,wearable,window_days=14):
    width=max(1,int(window_days)); m={d:v for d,v in marker if _day(d) and finite(v)}; w={d:v for d,v in wearable if _day(d) and finite(v)}; result=[]
    for day,value in sorted(m.items()):
        values=[w[k] for back in range(width) if (k:=(_day(day)-dt.timedelta(days=back)).isoformat()) in w]
        if values:result.append(dict(day=day,marker_value=value,wearable_mean=stats.mean(values),wearable_n=len(values)))
    return result

def lab_book(records,history=(),window_days=14):
    catalog={r['key']:r for r in marker_catalog()}; names={r['display_name'].lower():k for k,r in catalog.items()}; groups={}
    for original in records:
        row=dict(original); name=str(row.get('marker_key',row.get('markerKey',row.get('marker',''))))
        key=names.get(name.lower(),name); unit=row.get('unit') or catalog.get(key,{}).get('canonical_unit','')
        row['marker_key']=key; groups.setdefault((key,unit),[]).append(row)
    markers=[]; projected=[]
    for (key,unit),rows in sorted(groups.items()):
        series=project_labs(rows); projected.extend(dict(r,unit=unit) for r in series)
        associations={}
        for metric in ('hrv','resting_hr','charge','effort','rest'):
            wearable=[(r['day'],_value(r,metric)) for r in history if _day(r.get('day'))]
            pairs=pair_marker_wearable([(r['day'],r['value']) for r in series],wearable,window_days)
            associations[metric]=dict(pairs=pairs,correlation=correlation([(p['marker_value'],p['wearable_mean']) for p in pairs]) if len(pairs)>=4 else dict(value=None,reason='Requires four paired lab reading days',coverage=dict(pairs=len(pairs))),window_days=window_days)
        markers.append(dict(marker_key=key,label=catalog.get(key,{}).get('display_name',key),unit=unit,latest=series[-1] if series else None,history=series,associations=associations,coverage=dict(numeric_days=len(series),records=len(rows)),report_references=[{k:r.get(k) for k in ('date','day','low','high','reference_text','notes') if r.get(k) is not None} for r in rows if any(r.get(k) is not None for k in ('low','high','reference_text'))]))
    return dict(markers=markers,projected=projected,source='LabBookProjection.swift',note='Latest numeric reading per marker/day; trailing wearable mean includes reading day. Units stay separate; report ranges are user-supplied; no marker normality judgement.')

def health_projections(day,rr=(),gravity=(),lab_records=(),history=(),settings=None):
    settings=settings or {}
    return dict(rhythm=rhythm_night(day,rr,gravity),vital_bands=vital_bands(day,history),lab_book=lab_book(lab_records,[*history,day],settings.get('lab_window_days',14)),marker_catalog=marker_catalog())
