"""Deterministic local associations and calendar insights over observed data.

Ports grounded in NOOP BehaviorInsights, EffectRanker, CorrelationEngine,
DoseResponseEngine/Priors, CircadianEngine, WeeklyDigest and StreakCalculator.
Calendar cycle labels and prospective experiment windows are explicitly labelled
BOOP additions; they do not claim NOOP temperature inference or causal effects.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import math
import statistics as stats
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def circular_clock_hour(hours):
    """SleepStageTotals.circularMeanSec unit-vector center, adapted to clock hours.

    Source rounds to observed second precision and withholds antipodal/uniform
    histories when resultant<1e-9 instead of guessing a habitual schedule.
    """
    hours=[h for h in hours if finite(h)]
    if not hours:return None
    sine=sum(math.sin(h*math.pi/12) for h in hours)
    cosine=sum(math.cos(h*math.pi/12) for h in hours)
    if math.hypot(sine,cosine)/len(hours)<1e-9:return None
    seconds=math.floor((math.atan2(sine,cosine)%(2*math.pi))*86400/(2*math.pi)+.5)%86400
    return seconds/3600


def shift(day,amount):
    try:
        return (date.fromisoformat(day)+timedelta(days=amount)).isoformat()
    except (TypeError,ValueError):
        return None


def normal_cdf(z):
    """A&S 7.1.26 approximation, exactly the NOOP constants."""
    x=z/math.sqrt(2); sign=-1 if x<0 else 1; x=abs(x)
    t=1/(1+.3275911*x)
    erf=sign*(1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-.284496736)*t+.254829592)*t*math.exp(-x*x))
    return .5*(1+erf)


def pearson(pairs):
    pairs=[(x,y) for x,y in pairs if finite(x) and finite(y)]
    n=len(pairs)
    if n<3:
        return None
    mx=sum(x for x,y in pairs)/n; my=sum(y for x,y in pairs)/n
    xx=sum((x-mx)**2 for x,y in pairs); yy=sum((y-my)**2 for x,y in pairs)
    xy=sum((x-mx)*(y-my) for x,y in pairs)
    if xx<=0 or yy<=0:
        return None
    r=max(-1,min(1,xy/math.sqrt(xx*yy)))
    p=0.0 if 1-r*r<=0 else 2*(1-normal_cdf(abs(r*math.sqrt((n-2)/(1-r*r)))))
    slope=xy/xx
    return dict(n=n,r=r,slope=slope,intercept=my-slope*mx,p_approx=max(0,min(1,p)))


def behavior_effect(with_values,without_values):
    yes=[v for v in with_values if finite(v)]; no=[v for v in without_values if finite(v)]
    n1,n2=len(yes),len(no)
    if not n1 or not n2 or n1+n2<3:
        return None
    m1,m2=stats.mean(yes),stats.mean(no); delta=m1-m2
    v1=stats.variance(yes) if n1>1 else 0; v2=stats.variance(no) if n2>1 else 0
    pooled=((n1-1)*v1+(n2-1)*v2)/(n1+n2-2)
    d=delta/math.sqrt(pooled) if pooled>0 else 0
    se2=v1/n1+v2/n2
    p=2*(1-normal_cdf(abs(delta/math.sqrt(se2)))) if se2>0 else 1.0 if delta==0 else 0.0
    return dict(mean_with=m1,mean_without=m2,delta=delta,pct_change=delta/abs(m2)*100 if m2 else None,n_with=n1,n_without=n2,cohens_d=d,p_approx=max(0,min(1,p)),significant=p<.05 and min(n1,n2)>=5,source="NOOP BehaviorInsights.swift",note="Association between explicitly logged groups; unlogged days excluded")


def best_lag(answers,outcomes,behavior,outcome):
    choices=[]
    for lag in (0,1,2):
        yes,no=[],[]
        for day,answer in sorted(answers.items()):
            value=outcomes.get(shift(day,lag))
            if finite(value) and type(answer) is bool:
                (yes if answer else no).append(value)
        effect=behavior_effect(yes,no)
        if effect and min(effect["n_with"],effect["n_without"])>=5:
            choices.append(effect|dict(behavior=behavior,outcome=outcome,lag_days=lag,confidence="solid" if min(len(yes),len(no))>=10 else "building"))
    return min(choices,key=lambda e:(-abs(e["cohens_d"]),e["lag_days"])) if choices else None


def dose_response(doses,outcomes,behavior,outcome,prior=None):
    pairs=[(dose,outcomes.get(shift(day,1))) for day,dose in sorted(doses.items()) if finite(dose) and dose>=0 and finite(outcomes.get(shift(day,1)))]
    fit=pearson(pairs); n=len(pairs); weight=n/(n+8)
    personal=fit["slope"] if fit else None
    value=personal
    if prior is not None:
        slope,low,high=prior
        value=max(low,min(high,weight*personal+(1-weight)*slope if personal is not None else slope))
    return dict(behavior=behavior,outcome=outcome,lag_days=1,n_user=n,user_slope=personal,prior_slope=prior[0] if prior else None,weight=weight if prior else 1 if personal is not None else 0,per_unit=value,
                prior_dominated=n<5 if prior else False,contradicts_prior=bool(prior and personal is not None and n>=5 and (personal==0 or (personal>0)!=(prior[0]>0))),confidence="calibrating" if n<5 else "solid" if n>=12 else "building",
                curve=[dict(dose=d,outcome_delta=d*value+0.0) for d in range(4)] if value is not None else [],source="NOOP DoseResponseEngine.swift: k=8, gates=5/12, next-day alignment" if prior else "BOOP personal next-day OLS; no documented population prior",
                note="Typical prior, not measured personal effect" if prior and personal is None else "Observed association, not a causal dose recommendation" if personal is not None else "Needs three paired days and spread in dose and outcome")


def compare_series(x,y,x_label="x",y_label="y",lag_days=0):
    keys=[k for k in sorted(x) if finite(x[k]) and finite(y.get(shift(k,lag_days)))]
    pairs=[(x[k],y[shift(k,lag_days)]) for k in keys]
    fit=pearson(pairs)
    observed_x=sum(finite(v) for v in x.values()); observed_y=sum(finite(v) for v in y.values())
    denominator=max(observed_x,observed_y)
    normalized=[]
    if fit:
        xs=[p[0] for p in pairs]; ys=[p[1] for p in pairs]
        mx,my=stats.mean(xs),stats.mean(ys); sx,sy=stats.stdev(xs),stats.stdev(ys)
        normalized=[dict(day=k,x=(a-mx)/sx,y=(b-my)/sy) for k,(a,b) in zip(keys,pairs)]
    return dict(x=x_label,y=y_label,lag_days=lag_days,n=len(pairs),r=fit["r"] if fit else None,slope=fit["slope"] if fit else None,intercept=fit["intercept"] if fit else None,p_approx=fit["p_approx"] if fit else None,
                overlap=len(pairs)/denominator if denominator else 0,observed_x=observed_x,observed_y=observed_y,normalized=normalized,source="NOOP CorrelationEngine.swift; sample-SD chart normalization",note="Approximate normal tail; association only" if fit else "Needs three overlapping days and nonzero variance")


def streaks(day_keys,today):
    days={date.fromisoformat(d).toordinal() for d in day_keys if shift(d,0)}
    longest=0
    for day in days:
        if day-1 not in days:
            count=1
            while day+count in days:
                count+=1
            longest=max(longest,count)
    current=0
    if shift(today,0):
        t=date.fromisoformat(today).toordinal()
        anchor=t if t in days else t-1 if t-1 in days else None
        while anchor is not None and anchor-current in days:
            current+=1
    return dict(current=current,longest=longest,observed_days=len(days),source="NOOP StreakCalculator.swift; yesterday grace")


def series_stat(values):
    values=[v for v in values if finite(v)]
    n=len(values)
    if not n:
        return dict(n=0,mean=None,median=None,minimum=None,maximum=None,stdev=None,slope_per_day=None)
    mean=stats.mean(values); mid=(n-1)/2
    denominator=sum((i-mid)**2 for i in range(n))
    slope=sum((i-mid)*(v-mean) for i,v in enumerate(values))/denominator if denominator else 0
    return dict(n=n,mean=mean,median=stats.median(values),minimum=min(values),maximum=max(values),stdev=stats.stdev(values) if n>1 else 0,slope_per_day=slope)


def compare_periods(current,previous):
    """NOOP ComparisonEngine comparison, with empty-display values kept null."""
    a,b=series_stat(current),series_stat(previous)
    delta=a["mean"]-b["mean"] if a["n"] and b["n"] else None
    return dict(current=a,previous=b,delta=delta,pct_change=delta/abs(b["mean"])*100 if delta is not None and b["mean"]!=0 else None,
                direction=1 if delta is not None and delta>0 else -1 if delta is not None and delta<0 else 0,source="NOOP ComparisonEngine.swift; empty periods display null rather than zero")


def month_over_month(by_day,reference_day):
    reference=date.fromisoformat(reference_day)
    current=reference.replace(day=1); previous=(current-timedelta(days=1)).replace(day=1)
    month_end=(current.replace(day=28)+timedelta(days=4)).replace(day=1)
    now=[v for d,v in sorted(by_day.items()) if current.isoformat()<=d<month_end.isoformat()]
    before=[v for d,v in sorted(by_day.items()) if previous.isoformat()<=d<current.isoformat()]
    return compare_periods(now,before)|dict(current_month=current.strftime("%Y-%m"),previous_month=previous.strftime("%Y-%m"))


def weekly_digest(series,through):
    day=date.fromisoformat(through); monday=day-timedelta(days=day.weekday())
    previous=monday-timedelta(days=7); baseline_start=monday-timedelta(days=28)
    metrics={}; movers=[]; observed=set()
    spread={"charge":12,"effort":12,"rest":12,"resting_hr":4,"hrv":8}
    for key,by_day in series.items():
        cur=[v for d,v in sorted(by_day.items()) if monday.isoformat()<=d<=through]
        prev=[v for d,v in sorted(by_day.items()) if previous.isoformat()<=d<monday.isoformat()]
        base=[v for d,v in sorted(by_day.items()) if baseline_start.isoformat()<=d<monday.isoformat()]
        a,b=series_stat(cur),series_stat(prev)
        delta=a["mean"]-b["mean"] if a["n"] and b["n"] else None
        baseline=stats.mean(base) if base else None
        normalized=delta/spread[key] if delta is not None and key in spread else None
        item=dict(current=a,previous=b,delta=delta,pct_change=delta/abs(b["mean"])*100 if delta is not None and b["mean"] else None,baseline_mean=baseline,vs_baseline=a["mean"]-baseline if a["n"] and baseline is not None else None,rough=bool(a["n"] and b["n"] and min(a["n"],b["n"])<3),normalized_move=normalized)
        metrics[key]=item
        observed.update(d for d in by_day if monday.isoformat()<=d<=through)
        if normalized is not None and min(a["n"],b["n"])>=3 and abs(normalized)>=.5:
            movers.append(dict(metric=key,delta=delta,normalized_move=normalized,goodness=(-1 if delta>0 else 1) if key=="resting_hr" else (1 if delta>0 else -1)))
    movers.sort(key=lambda r:(-abs(r["normalized_move"]),r["metric"]))
    effort=metrics.get("effort",{}).get("current",{}); charge=metrics.get("charge",{}).get("current",{})
    balance=None
    if effort.get("n",0)>=3 and charge.get("n",0)>=3:
        gap=effort["mean"]-charge["mean"]
        balance="overreaching" if gap>10 else "underloaded" if gap< -10 else "balanced"
    return dict(week_start=monday.isoformat(),week_end=(monday+timedelta(days=6)).isoformat(),through=through,days_with_data=len(observed),metrics=metrics,movers=movers[:3],balance=balance,
                sleep_consistency_sd=metrics.get("rest",{}).get("current",{}).get("stdev") if metrics.get("rest",{}).get("current",{}).get("n",0)>=2 else None,
                source="NOOP WeeklyDigest.swift: Monday anchor, 4-week baseline, 3-day focus gate, 0.5 normalized threshold",note="Observed week comparison; sparse periods retain counts and no confident mover")


def cosinor(bins):
    bins=[(h,v) for h,v in bins if finite(h) and finite(v)]
    if len(bins)<3:
        return None
    matrix=[[0.0]*3 for i in range(3)]; rhs=[0.0]*3
    for hour,value in bins:
        row=[1,math.cos(2*math.pi*hour/24),math.sin(2*math.pi*hour/24)]
        for i in range(3):
            rhs[i]+=row[i]*value
            for j in range(3):
                matrix[i][j]+=row[i]*row[j]
    # Solve the same OLS normal equations as NOOP (pivoting avoids tiny pivots).
    for i in range(3):
        pivot=max(range(i,3),key=lambda j:abs(matrix[j][i]))
        matrix[i],matrix[pivot]=matrix[pivot],matrix[i]; rhs[i],rhs[pivot]=rhs[pivot],rhs[i]
        if abs(matrix[i][i])<=1e-12:
            return None
        scale=matrix[i][i]; matrix[i]=[v/scale for v in matrix[i]]; rhs[i]/=scale
        for j in range(3):
            if j==i:
                continue
            factor=matrix[j][i]; matrix[j]=[a-factor*b for a,b in zip(matrix[j],matrix[i])]; rhs[j]-=factor*rhs[i]
    mesor,beta,gamma=rhs
    return dict(mesor=mesor,amplitude=math.hypot(beta,gamma),acrophase_hours=(math.atan2(gamma,beta)*24/(2*math.pi))%24)


def shift_plan(shift_hours,sleep_hour,wake_hour):
    if not all(finite(v) for v in (shift_hours,sleep_hour,wake_hour)):
        return dict(direction=None,days=[],note="Enter current sleep/wake hours and requested shift")
    magnitude=min(abs(shift_hours),24)
    if magnitude<.5:
        return dict(direction="none",estimated_days=0,total_shift_hours=0,days=[])
    advance=shift_hours>0; days=[]
    for i in range(1,math.ceil(magnitude)+1):
        signed=-min(i,magnitude) if advance else min(i,magnitude)
        sleep=(sleep_hour+signed)%24; wake=(wake_hour+signed)%24
        days.append(dict(day_index=i,bright_light_start_hour=wake if advance else (sleep-3)%24,bright_light_end_hour=(wake+2)%24 if advance else (sleep-1)%24,dim_from_hour=(sleep-2)%24 if advance else wake,target_sleep_hour=sleep,target_wake_hour=wake))
    return dict(direction="advance" if advance else "delay",estimated_days=len(days),total_shift_hours=magnitude,days=days,source="NOOP CircadianEngine.swift: at most 1 hour/day",note="Optional light and sleep timing plan")


def cycle_from_logs(records,through):
    starts=[]; lengths=[]
    for row in records:
        explicit_date = row.get("period_start") if isinstance(row.get("period_start"),str) else None
        if row.get("event",row.get("type")) in ("period_start","period","menstruation") or row.get("period_start") is True or explicit_date:
            day=explicit_date or row.get("day",row.get("date"))
            if shift(day,0) and day<=through:
                starts.append(day)
        length=row.get("cycle_length_days")
        if finite(length) and 21<=length<=40:
            lengths.append(length)
    starts=sorted(set(starts))
    observed_intervals=[(date.fromisoformat(b)-date.fromisoformat(a)).days for a,b in zip(starts,starts[1:])]
    regular=[n for n in observed_intervals if 21<=n<=40]
    length=round(stats.median(regular)) if len(regular)>=2 else round(lengths[-1]) if lengths else None
    result=dict(phase="unknown",cycle_day=None,length_days=length,logged_starts=starts,confidence="unavailable",source="BOOP voluntary calendar logs; not NOOP temperature-based CyclePhaseEngine",note="Log a period start to show calendar cycle day; no ovulation or fertility inference")
    if starts:
        cycle_day=(date.fromisoformat(through)-date.fromisoformat(starts[-1])).days+1
        result.update(cycle_day=cycle_day,confidence="calendar_only")
        if length is None or cycle_day>length+7:
            result["note"]="Calendar day since voluntary start; phase unknown without a usable cycle length"
        else:
            result["phase"]="menstrual_calendar" if cycle_day<=5 else "follicular_calendar" if cycle_day<=length-14 else "luteal_calendar"
            result["note"]="Approximate calendar phase from your logs; no detected hormonal phase or fertility prediction"
    return result


def cycle_fused_index(temp_z,rhr_z,hrv_z):
    terms=[(.6,temp_z),(.2,rhr_z),(.2,-hrv_z if finite(hrv_z) else None)]
    present=[(weight,value) for weight,value in terms if finite(value)]
    return sum(w*v for w,v in present)/sum(w for w,v in present) if present else None


def cycle_phase(nights,baseline_usable,logged_period_starts=()):
    """Source-identical NOOP CyclePhaseEngine constants, fusion and civil-day ranges."""
    result=dict(phase="learning",confidence="learning",cycle_day_low=None,cycle_day_high=None,length_days=None,next_period_window=None,shift_markers=[],observed_nights=len(nights),source="NOOP CyclePhaseEngine.swift",note="Learning your nightly temperature pattern; requires a usable baseline and 42 usable nights",awareness="For awareness only. Not a medical device, not contraception, not a substitute for professional care.")
    fused=[(n["day"],cycle_fused_index(n.get("temp_z"),n.get("rhr_z"),n.get("hrv_z"))) for n in nights]
    values=[value for day,value in fused if value is not None]
    result["usable_nights"]=len(values)
    if not baseline_usable or len(nights)<42 or len(values)<42:
        return result
    center=stats.median(values); spread=max(1e-9,stats.median(abs(value-center) for value in values))
    elevated=[value is not None and value-center>=.5*spread for day,value in fused]
    onsets=[i for i in range(len(fused)) if elevated[i] and (i==0 or not elevated[i-1])]
    result["shift_markers"]=[dict(day=fused[i][0]) for i in onsets]
    if not onsets:
        return result|dict(phase="unknown",confidence="building",note="No clear temperature pattern yet")
    def distance(a,b):
        return (date.fromisoformat(b)-date.fromisoformat(a)).days
    gaps=[distance(fused[a][0],fused[b][0]) for a,b in zip(onsets,onsets[1:])]
    median_gap=int(math.floor(stats.median(gaps)+.5)) if gaps else None
    length=median_gap if median_gap is not None and 21<=median_gap<=40 else None
    result.update(length_days=length,confidence="solid" if length is not None else "building")
    latest=fused[-1][0]; onset=fused[onsets[-1]][0]; anchor=onset
    eligible=[day for day in logged_period_starts if shift(day,0) and day<=latest]
    logged=max(eligible) if eligible else None
    note=None
    if logged:
        anchor=logged
        delta=distance(logged,onset)
        if delta<0 or delta>40 or distance(logged,latest)>40:
            note="Your temperature shift came at a different time than your logged date; the logged start may be off"
    since=distance(anchor,latest)
    if logged:
        day=max(1,since+1); low,high=max(1,day-1),day+1
    else:
        day=(length or 28)//2+since; low,high=max(1,day-2),day+2
    days_since_onset=distance(onset,latest)
    phase="periOvulatory" if days_since_onset<=2 else "luteal" if elevated[-1] else "follicular"
    window=None
    if length is not None:
        earliest=shift(anchor,length-2); latest_window=shift(anchor,length+2)
        if latest_window>=latest:
            window=dict(earliest_day=max(latest,earliest),latest_day=latest_window)
    notes={"follicular":"Follicular range; temperature near your baseline","periOvulatory":"Around the mid-cycle temperature shift","luteal":"Luteal range; temperature above your baseline"}
    return result|dict(phase=phase,cycle_day_low=low,cycle_day_high=high,next_period_window=window,note=note or notes[phase])


class InsightsService:
    def __init__(self,features,analytics):
        self.features,self.analytics=features,analytics

    def bundle(self,device,days=30,settings=None,through=None):
        settings=settings or self.features.settings()
        history=self.analytics.trends(device,min(max(int(days),1),366),settings,through)
        entries=history.get("days",[]); through=history.get("through",through)
        if through is None:
            through=datetime.now(timezone.utc).date().isoformat()
        first=entries[0]["day"] if entries else through
        metrics=("charge","effort","rest","hrv","resting_hr","steps")
        series={k:{} for k in metrics}
        for entry in entries:
            for metric in metrics:
                value=entry.get(metric)
                value=value.get("value") if isinstance(value,dict) else value
                if finite(value):
                    series[metric][entry["day"]]=value
        journals=self.features.list_records("journal",{"limit":100000})
        habits=self.features.list_records("habit",{"limit":100000})
        questions=defaultdict(dict); doses=defaultdict(dict); logged=set(); explicit_dose=set()
        for row in sorted(journals+habits,key=lambda r:r.get("updated_ms",0)):
            day=row.get("day",row.get("date")); original=row.get("original",{})
            if not shift(day,0) or not first<=day<=through or row.get("type")=="experiment":
                continue
            label=row.get("question",row.get("name",original.get("question","Unspecified")))
            answer=row.get("answer",row.get("answered_yes",original.get("answeredYes")))
            if row.get("source_table")=="journal" and type(answer) is int and answer in (0,1):
                answer=bool(answer)  # SQLite's declared NOOP journal Boolean
            value=row.get("dose",row.get("numeric_value",row.get("numericValue",row.get("value",original.get("numericValue")))))
            if type(answer) is bool:
                questions[label][day]=answer; logged.add(day)
            if finite(value) and value>=0:
                doses[label][day]=value; logged.add(day)
                questions[label][day]=value>=1
                if "dose" in row:
                    explicit_dose.add(label)
        effects=[]; same_day=[]; dose_rows=[]
        for label,answers in sorted(questions.items()):
            for metric in ("charge","hrv","resting_hr","effort","rest"):
                raw=behavior_effect([series[metric].get(d) for d,a in answers.items() if a is True],[series[metric].get(d) for d,a in answers.items() if a is False])
                if raw:
                    same_day.append(raw|dict(behavior=label,outcome=metric,lag_days=0))
                ranked=best_lag(answers,series[metric],label,metric)
                if ranked:
                    effects.append(ranked)
        effects.sort(key=lambda r:(not r["significant"],-abs(r["cohens_d"]),r["behavior"],r["outcome"]))
        for index,row in enumerate(effects,1):
            row["rank"]=index
        for label,values in sorted(doses.items()):
            lower=label.lower()
            if label in explicit_dose and ("alcohol" in lower or "drink" in lower):
                dose_rows.append(dose_response(values,series["charge"],label,"charge",(-5,-15,2)))
            elif label in explicit_dose and "caffeine" in lower and all(v in (0,1,2,3) for v in values.values()):
                dose_rows.append(dose_response(values,series["hrv"],label,"hrv",(-4,-20,4))|dict(unit_note="Timing bucket 0..3, not caffeine milligrams"))
            else:
                dose_rows.append(dose_response(values,series["charge"],label,"charge"))
        experiments=[]
        for row in habits:
            if row.get("type")!="experiment":
                continue
            metric=row.get("outcome","charge"); values=series.get(metric,{})
            bounds=[row.get(k) for k in ("baseline_start","baseline_end","intervention_start","intervention_end")]
            valid=all(shift(v,0) for v in bounds) and bounds[0]<=bounds[1]<bounds[2]<=bounds[3]
            baseline=[v for d,v in values.items() if valid and bounds[0]<=d<=bounds[1]]
            intervention=[v for d,v in values.items() if valid and bounds[2]<=d<=bounds[3]]
            effect=behavior_effect(intervention,baseline) if min(len(baseline),len(intervention))>=5 else None
            experiments.append(dict(id=row.get("id"),name=row.get("name","Experiment"),outcome=metric,status="invalid_windows" if not valid else "planned" if through<bounds[2] else "collecting" if effect is None else "observed",baseline_days=len(baseline),intervention_days=len(intervention),effect=effect,note="BOOP explicit before/after windows; requires 5 observed days per phase. No randomization, washout or causal inference"))
        hourly=defaultdict(list); observed_days=0; wake_hours=[]; sleep_hours=[]
        try:
            tz=ZoneInfo(settings.get("timezone","Australia/Brisbane"))
        except ZoneInfoNotFoundError:
            tz=timezone(timedelta(hours=10))
        for entry in entries:
            bins=entry.get("hourly_hr",{})
            if bins:
                observed_days+=1
            for hour,value in bins.items():
                if finite(value):
                    hourly[float(hour)].append(value)
            main=entry.get("sleep",{}).get("main")
            if main and finite(main.get("start")) and finite(main.get("end")):
                for key,target in (("end",wake_hours),("start",sleep_hours)):
                    clock=datetime.fromtimestamp(main[key],tz); target.append(clock.hour+clock.minute/60)
        fit=cosinor([(h,stats.mean(v)) for h,v in hourly.items()])
        wake=settings.get("habitual_wake_hour"); sleep=settings.get("habitual_sleep_hour")
        wake=wake%24 if finite(wake) and 0<=wake<=24 else circular_clock_hour(wake_hours)
        sleep=sleep%24 if finite(sleep) and 0<=sleep<=24 else circular_clock_hour(sleep_hours)
        circadian=dict(confidence="unreadable",observed_days=observed_days,acrophase_hours=None,temp_min_hour=None,forecast=entries[-1].get("sleep",{}).get("forecast") if entries else None,plan=shift_plan(settings.get("circadian_shift_hours"),sleep,wake),source="NOOP CircadianEngine.swift; HR rhythm phase proxy",note="Requires seven observed days and sufficient rhythm amplitude")
        if fit:
            relative=fit["amplitude"]/abs(fit["mesor"]) if fit["mesor"] else 0
            readable=observed_days>=7 and (relative>=.1 or fit["amplitude"]>=4.5)
            circadian.update(fit)
            if readable:
                minimum=(fit["acrophase_hours"]-12)%24
                circadian.update(confidence="solid" if observed_days>=14 and relative>=.1 else "wide",temp_min_hour=minimum,note="Approximate HR-derived phase proxy; temperature minimum is inferred, not measured")
                if wake is not None:
                    circadian["offset_vs_schedule_minutes"]=((minimum-(wake-2.5)+12)%24-12)*60
        cycle_records=self.features.list_records("cycle",{"limit":100000})
        calendar_cycle=cycle_from_logs(cycle_records,through)
        cycle=calendar_cycle
        if cycle_records:
            nights=[]
            for entry in entries:
                night=dict(day=entry["day"])
                for metric,z_key in (("skin_temp","temp_z"),("resting_hr","rhr_z"),("hrv","hrv_z")):
                    actual_key="skin_temperature" if metric=="skin_temp" else metric
                    value=entry.get(actual_key,{})
                    value=value.get("value") if isinstance(value,dict) else value
                    baseline=entry.get("baselines",{}).get(metric,{})
                    if baseline.get("usable") and finite(value) and finite(baseline.get("mean")) and finite(baseline.get("spread")) and baseline["spread"]>0:
                        night[z_key]=(value-baseline["mean"])/(1.253*baseline["spread"])
                nights.append(night)
            usable=bool(entries and entries[-1].get("baselines",{}).get("skin_temp",{}).get("usable"))
            cycle=cycle_phase(nights,usable,calendar_cycle["logged_starts"])|dict(calendar=calendar_cycle,cycle_day=calendar_cycle["cycle_day"])
        comparisons=[compare_series(series[x],series[y],x,y,lag) for x,y,lag in (("effort","charge",1),("rest","charge",0),("hrv","charge",0),("steps","effort",0),("resting_hr","hrv",0))]
        calendar=[dict(day=e["day"],observed=any(e["day"] in s for s in series.values()),journal=e["day"] in logged,workouts=len(e.get("workouts",{}).get("value") or [])) for e in entries]
        return dict(behaviour=dict(logged_days=len(logged),calendar_days=len(entries),questions=len(questions),coverage=len(logged)/len(entries) if entries else 0,same_day_effects=same_day,dose_response=dose_rows,note="No entries are never counted as No answers; associations may reflect confounding"),effects=effects,experiments=experiments,cycle=cycle,circadian=circadian,weekly_digest=weekly_digest(series,through),streaks=dict(charge=streaks(series["charge"],through),journal=streaks(logged,through),calendar=calendar),comparisons=comparisons,period_comparisons={metric:month_over_month(values,through) for metric,values in series.items()})
