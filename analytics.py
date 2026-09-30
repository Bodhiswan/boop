"""Local, database-free NOOP analytics and a read-only SQLite adapter.

Formula provenance: reference/noop @ 7f396e98ed9d259df08e3a0a58cfac05fc70615c,
Packages/StrandAnalytics/Sources/StrandAnalytics and the independent Android
analytics twins. These are NOOP estimates, not WHOOP proprietary scores.
Streams use unix seconds, RR milliseconds and gravity g. No network or BLE.
"""
from __future__ import annotations

import bisect
import copy
import datetime as dt
import hashlib
import json
import math
import sqlite3
import statistics as stats
import threading
import time
from functools import lru_cache
from collections import defaultdict
from contextlib import closing
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REFERENCE = "7f396e98ed9d259df08e3a0a58cfac05fc70615c"


def finite(x):
    return not isinstance(x,bool) and isinstance(x, (int, float)) and math.isfinite(x)


def clamp(x, lo=0., hi=1.):
    return max(lo, min(hi, x))


def rounded(x, places=2):
    scale = 10 ** places
    return math.copysign(math.floor(abs(x) * scale + .5) / scale, x)


def result(value=None, reason=None, coverage=None, **extra):
    return dict(value=value, reason=reason, coverage=coverage or {}, **extra)


def percentile(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    pos = p * (len(xs)-1)
    lo = int(pos)
    return xs[lo] + (pos-lo) * (xs[min(lo+1, len(xs)-1)]-xs[lo])


def clean_rr(rr):
    """HRVAnalyzer.cleanRRGapAware: range then radius-2 Malik 20% filter."""
    ranged = [(i, float(v)) for i, v in enumerate(rr) if finite(v) and 300 <= v <= 2000]
    kept = []
    for k, row in enumerate(ranged):
        neighbors = [v for j, (_, v) in enumerate(ranged[max(0, k-2):k+3], max(0, k-2)) if j != k]
        if len(ranged) <= 2 or len(neighbors) < 2 or abs(row[1]-stats.median(neighbors)) / stats.median(neighbors) <= .20:
            kept.append(row)
    return [v for _, v in kept], [i > 0 and kept[i][0] == kept[i-1][0]+1 for i in range(len(kept))]


def hrv(rr, timestamps=None, max_rejected_fraction=None):
    """Task Force RMSSD, sample SDNN and pNN50, with NOOP integrity gates."""
    rr = list(rr)
    nn, contiguous = clean_rr(rr)
    coverage = dict(input_beats=len(rr), clean_beats=len(nn), rejected_beats=len(rr)-len(nn))
    empty = dict(rmssd_ms=None, sdnn_ms=None, mean_nn_ms=None, pnn50=None)
    if len(nn) < 20:
        return result(reason="Requires at least 20 clean RR intervals", coverage=coverage, **empty)
    if max_rejected_fraction is not None and 1-len(nn)/len(rr) > max_rejected_fraction:
        return result(reason="RR artifact rejection exceeds the spot capture limit", coverage=coverage, **empty)
    diffs = [nn[i]-nn[i-1] for i in range(1, len(nn)) if contiguous[i]]
    rmssd = math.sqrt(sum(v*v for v in diffs)/len(diffs)) if diffs else None
    sdnn = stats.stdev(nn)
    reason = None
    if timestamps is not None and len(timestamps) == len(rr) and len(rr) >= 2:
        span = max(timestamps)-min(timestamps)
        fraction = sum(abs(timestamps[i]-timestamps[i-1]-rr[i]/1000) <= .5 for i in range(1, len(rr)))/(len(rr)-1)
        beat_coverage = sum(rr)/1000/span if span > 0 else None
        coverage.update(beat_coverage=beat_coverage, beat_accuracy_fraction=fraction, span_seconds=span)
        if beat_coverage is not None and beat_coverage > 1.10:
            rmssd = sdnn = None
            reason = "RR beat time exceeds 110% of wall time; overlapping captures cannot be trusted"
        elif fraction < .5:
            sdnn = None
            reason = "SDNN refused: fewer than half the RR timestamps are beat accurate"
    return result(rmssd, reason, coverage, rmssd_ms=rmssd, sdnn_ms=sdnn,
                  mean_nn_ms=stats.mean(nn), pnn50=100*sum(abs(v)>50 for v in diffs)/len(diffs) if diffs else None,
                  source="HRVAnalyzer.swift: gap-aware Malik / Task Force 1996")


def stress_index(rr):
    """StressIndex.swift: Baevsky SI, 50ms modal histogram, lowest tie wins."""
    nn, _ = clean_rr(list(rr))
    if len(nn) < 20 or not rr or 1-len(nn)/len(rr) > .35:
        return result(reason="Requires 20 clean beats with no more than 35% artifacts")
    sec = [v/1000 for v in nn]
    lo, hi = min(sec), max(sec)
    if hi == lo:
        return result(reason="All RR intervals equal; stress histogram has no spread")
    counts = [0] * (math.floor((hi-lo)/.05)+1)
    for v in sec:
        counts[min(len(counts)-1, math.floor((v-lo)/.05))] += 1
    mode = counts.index(max(counts))
    mo, amo = lo+(mode+.5)*.05, max(counts)/len(sec)*100
    return result(amo/(2*mo*(hi-lo)), mo_seconds=mo, amplitude_percent=amo, range_seconds=hi-lo,
                  source="StressIndex.swift: Baevsky")


def durations(hr):
    """StrainScorer.sampleDurationsMinutes: next gap, 120s cap, reused tail."""
    if not hr:
        return []
    if len(hr) == 1:
        return [1.]
    values = [min(abs(hr[i+1][0]-hr[i][0]) or 1., 120.) for i in range(len(hr)-1)]
    return values + [values[-1]]


def zone_weight(bpm, resting_hr, max_hr):
    pct = (bpm-resting_hr)/(max_hr-resting_hr)*100
    return next((weight for cut, weight in [(90,5),(80,4),(70,3),(60,2),(50,1)] if pct >= cut), 0)


def effort(hr, max_hr=None, resting_hr=None, method="edwards", sex=None):
    hr = sorted(hr)
    coverage = dict(samples=len(hr), span_seconds=hr[-1][0]-hr[0][0] if hr else 0)
    if len(hr) < 600 and not (len(hr) >= 20 and coverage["span_seconds"] >= 600):
        return result(reason="Requires 600 HR samples, or 20 samples spanning 600 seconds", coverage=coverage)
    if not finite(max_hr) or not finite(resting_hr) or max_hr <= resting_hr:
        return result(reason="Requires known HRmax greater than resting HR", coverage=coverage)
    secs = durations(hr)
    zones = [0.] * 6
    for (_, bpm), seconds in zip(hr, secs):
        zones[zone_weight(bpm, resting_hr, max_hr)] += seconds/60
    if method == "edwards":
        trimp, denominator = sum(i*v for i,v in enumerate(zones)), 7201.
    elif method == "banister":
        b = 1.67 if str(sex or "").lower().startswith("f") else 1.92
        baseline = .64*.10*math.exp(b*.10)
        # Source floors EACH sample, so a sedentary deficit cannot erase real work.
        trimp = sum(seconds/60*max(0.,.64*clamp((h-resting_hr)/(max_hr-resting_hr))*math.exp(b*clamp((h-resting_hr)/(max_hr-resting_hr)))-baseline) for (_,h),seconds in zip(hr,secs))
        coverage["banister_coefficient_source"]="female" if str(sex or "").lower().startswith("f") else "male" if sex else "NOOP male-coefficient fallback"
        denominator = 1440*(.64*math.exp(b)-baseline)+1
    else:
        raise ValueError("effort_method must be edwards or banister")
    value = min(100., rounded(100*math.log1p(trimp)/math.log(denominator)))
    coverage["credited_seconds"] = sum(secs)
    return result(value, coverage=coverage, trimp=trimp, zone_minutes=zones, method=method,
                  max_hr=max_hr, resting_hr=resting_hr, source="StrainScorer.swift")


def hr_zones(hr, max_hr, custom_lower_bounds=None):
    if not finite(max_hr) or max_hr <= 0:
        return result(reason="Set age or HRmax to define HR zones")
    hr = sorted(hr)
    gaps = [hr[i][0]-hr[i-1][0] for i in range(1,len(hr)) if 0 < hr[i][0]-hr[i-1][0] < 300]
    tail = max(sorted(gaps)[len(gaps)//2],1) if gaps else 1
    custom=valid_zone_thresholds(custom_lower_bounds,profile_gate=False)
    bounds=custom or [p*max_hr for p in (.5,.6,.7,.8,.9)]
    totals = [0.] * 6
    for i, (stamp,bpm) in enumerate(hr):
        seconds = min(hr[i+1][0]-stamp,tail) if i+1 < len(hr) and hr[i+1][0]>stamp else tail
        zone = max([i+1 for i,bound in enumerate(bounds) if bpm>=bound], default=0)
        totals[zone] += seconds
    return result(totals[1:], below_zone1_seconds=totals[0],lower_bounds_bpm=bounds,zone_model="custom" if custom else "percent_hrmax",
                  boundaries=[max_hr*p for p in [.5,.6,.7,.8,.9,1]], source="HRZones.swift")


BASELINE_CFG = {"hrv": (5,250,5), "resting_hr": (30,120,2), "resp": (4,40,.5),
                "skin_temp": (20,42,.3), "strain": (0,100,5), "readiness_hrv_ln": (2.079,5.521,.08)}


def baseline(values, metric="hrv", reject_hard_outliers=True,day_keys=None,since_ms=0):
    """Baselines.swift production Winsor EWMA, including early anti-anchoring."""
    lo, hi, floor = BASELINE_CFG[metric]
    center, spread, n, missing = (lo+hi)/2, floor, 0, 0
    dropped=0
    for i,value in enumerate(values):
        if finite(since_ms) and since_ms>0 and day_keys is not None and i<len(day_keys):
            midnight=dt.datetime.combine(dt.date.fromisoformat(day_keys[i]),dt.time.min,tzinfo=dt.timezone.utc).timestamp()*1000
            if midnight<since_ms:dropped+=1;continue
        if not finite(value) or not lo <= value <= hi:
            missing += 1
            continue
        missing = 0
        if n == 0:
            center, n = value, 1
            continue
        young = n < 8
        if reject_hard_outliers and n >= 4 and not young and abs(value-center)>5*spread:
            continue
        eff_spread = spread*(2.5 if young else 1)
        lb, ls = 1-.5**(1/(3 if young else 14)), 1-.5**(1/21)
        center = lb*clamp(value,center-3*eff_spread,center+3*eff_spread)+(1-lb)*center
        spread = max(floor,ls*abs(value-center)+(1-ls)*spread)
        n += 1
    status = "stale" if missing>14 and n>=4 else "calibrating" if n<4 else "provisional" if n<14 else "trusted"
    return dict(mean=center, spread=spread, valid_nights=n, nights_since_update=missing,
                status=status, usable=status in ("provisional","trusted"), dropped_before_epoch=dropped,baseline_since_ms=since_ms or 0, source="Baselines.swift")


def charge(hrv_ms, resting_hr, hrv_baseline, rhr_baseline=None, resp=None, resp_baseline=None,
           sleep_perf=None, skin_temp_deviation=None, recovery_index_slope=None, prior_effort=None, effort_baseline=None):
    if not finite(hrv_ms) or not hrv_baseline or not hrv_baseline.get("usable"):
        return result(reason="Charge requires nightly HRV and at least 4 usable baseline nights",
                      coverage={"baseline_nights": (hrv_baseline or {}).get("valid_nights",0)})
    terms = []
    def add(key, value, base, weight, sign=1):
        if finite(value) and base and base.get("usable"):
            terms.append(dict(key=key,z=sign*(value-base["mean"])/max(1.253*base["spread"],1e-9),weight=weight))
    add("hrv",hrv_ms,hrv_baseline,.55)
    add("resting_hr",resting_hr,rhr_baseline,.20,-1)
    add("resp",resp,resp_baseline,.05,-1)
    add("activity_balance",prior_effort,effort_baseline,.05,-1)
    for key,v,w in [("rest_quality",(sleep_perf-.85)/.12 if finite(sleep_perf) else None,.15),
                    ("skin_temp",-abs(skin_temp_deviation) if finite(skin_temp_deviation) else None,.05),
                    ("recovery_index",-recovery_index_slope/2 if finite(recovery_index_slope) else None,.05)]:
        if v is not None:
            terms.append(dict(key=key,z=v,weight=w))
    z = sum(t["z"]*t["weight"] for t in terms)/sum(t["weight"] for t in terms)
    score = 100/(1+math.exp(clamp(-1.6*(z+.20),-700,700)))
    return result(score, band="red" if score<34 else "yellow" if score<67 else "green", drivers=terms,
                  source="RecoveryScorer.swift: approximate NOOP Charge")


def sleep_need(nightly_hours, age=None):
    floor = 9. if finite(age) and 0<age<18 else 8.
    xs = [v for v in nightly_hours if finite(v) and v>0]
    return min(max(percentile(xs,.75) if len(xs)>=7 else 8.,floor),9.5)


def sleep_consistency(nightly_hours):
    """VitalityEngine.sleepConsistency: 1 − duration CV (at least 3 nights)."""
    xs=[v for v in nightly_hours if finite(v) and v>0]
    return clamp(1-stats.pstdev(xs)/stats.mean(xs)) if len(xs)>=3 else None


def sleep_debt(series, need_hours=8, window=14):
    usable = [(day,v) for day,v in series if finite(v) and v>0][-max(1,window):]
    debt, nights = 0., []
    for day, slept in usable:
        debt = .55*max(0,need_hours*60+debt-slept)
        if debt < 10:
            debt = 0.
        nights.append(dict(day=day,slept_min=slept,delta_min=slept-need_hours*60))
    return dict(balance_min=rounded(-debt,1), nights=nights, need_min=need_hours*60, source="SleepDebt.swift")


def rest(tst_seconds, in_bed_seconds, efficiency, restorative_seconds, need_hours=8, consistency=None, deep_seconds=None):
    factor = .5+.5*clamp(deep_seconds/tst_seconds/.13) if deep_seconds is not None and tst_seconds>0 else 1
    restorative = clamp(restorative_seconds/tst_seconds/.50)*factor if tst_seconds>0 else 0
    return rounded(100*(.50*clamp(tst_seconds/(max(need_hours,.1)*3600))+.20*clamp(efficiency)+
                        .20*restorative+.10*clamp(.5 if consistency is None else consistency)))


def training_load(days, target=None):
    """TrainingLoadEngine.swift: no missing-day compression or zero filling."""
    unavailable = lambda reason,n=0: dict(value=None,state="unavailable",reason=reason,contiguous_days=n,points=[])
    if not days:
        return unavailable("noData")
    parsed = {}
    try:
        for day,load in days:
            d = dt.date.fromisoformat(day)
            if d.isoformat()!=day:
                return unavailable("invalidDay")
            if d in parsed:
                return unavailable("duplicateDay")
            if load is not None and (not finite(load) or load<0):
                return unavailable("invalidLoad")
            parsed[d]=load
        end = dt.date.fromisoformat(target) if target else max(parsed)
    except (ValueError,TypeError):
        return unavailable("invalidDay")
    if end not in parsed:
        return unavailable("missingTargetDay")
    suffix, cursor = [], end
    while cursor in parsed and parsed[cursor] is not None:
        suffix.append((cursor.isoformat(),parsed[cursor]))
        cursor-=dt.timedelta(days=1)
    suffix.reverse()
    if len(suffix)<14:
        return unavailable("notEnoughContiguousDays",len(suffix))
    ctl=atl=stats.mean(v for _,v in suffix[:7])
    points=[dict(day=suffix[6][0],load=suffix[6][1],ctl=ctl,atl=atl,tsb=0.)]
    for day,load in suffix[7:]:
        ctl+=(1-math.exp(-1/42))*(load-ctl)
        atl+=(1-math.exp(-1/7))*(load-atl)
        points.append(dict(day=day,load=load,ctl=ctl,atl=atl,tsb=ctl-atl))
    return dict(value=points[-1], state="established" if len(suffix)>=42 else "building",reason=None,
                contiguous_days=len(suffix),points=points,source="TrainingLoadEngine.swift")


def activity_cost(activity_days_by_sport, recovery_by_day):
    affected=set()
    for dates in activity_days_by_sport.values():
        for day in dates:
            for k in range(8):
                affected.add((dt.date.fromisoformat(day)+dt.timedelta(days=k)).isoformat())
    rest_values=[v for day,v in recovery_by_day.items() if day not in affected and finite(v)]
    if not rest_values:
        return result(reason="Requires untouched rest days outside every activity's 7-day recovery window")
    mean=stats.mean(rest_values)
    costs=[]
    for sport,dates in sorted(activity_days_by_sport.items()):
        trajectory=[]
        for k in range(1,8):
            vals=[recovery_by_day.get((dt.date.fromisoformat(day)+dt.timedelta(days=k)).isoformat()) for day in set(dates)]
            vals=[v for v in vals if finite(v)]
            trajectory.append((stats.mean(vals) if vals else None,len(vals)))
        next_mean,n=trajectory[0]
        if n<4:
            continue
        costs.append(dict(sport=sport,delta=mean-next_mean,mean_next_morning=next_mean,baseline_mean=mean,n=n,
                          confidence="solid" if n>=8 else "building",
                          days_to_baseline=next((i+1 for i,(v,count) in enumerate(trajectory) if count>0 and v is not None and v>=mean-3),None)))
    costs.sort(key=lambda x:(-abs(x["delta"]),x["confidence"]!="solid",x["sport"]))
    return result(costs, None if costs else "Requires 4 tagged activity / next-morning Charge pairs",source="ActivityCostEngine.swift")


def respiration(rr):
    """SleepStager.respRateFromRR RSA peak estimator including bank/splice gates."""
    rows=sorted(((t,v) for t,v in rr if finite(v) and 300<=v<=2000),key=lambda row:row[0])
    cov={"beats":len(rows)}
    if len(rows)<30:
        return result(reason="Respiration requires at least 30 plausible beat-timed RR intervals",coverage=cov)
    accurate=sum(abs(rows[i][0]-rows[i-1][0]-rows[i][1]/1000)<=.5 for i in range(1,len(rows)))/(len(rows)-1)
    cov["beat_accuracy_fraction"]=accurate
    if accurate<.5:
        return result(reason="Respiration refused: RR intervals are banked rather than beat timed",coverage=cov)
    times,values,splices=[],[],[]
    acc=0.
    for i,(t,v) in enumerate(rows):
        if i and t-rows[i-1][0]-v/1000>3:
            splices.append(acc)
        acc+=v/1000
        times.append(acc)
        values.append(v)
    if acc<150:
        return result(reason="Respiration requires at least 150 seconds of RR beat time",coverage=cov)
    grid=interpolate(times,values,[i*.25 for i in range(int(acc/.25)+1)])
    prefix=[0.]
    for v in grid:
        prefix.append(prefix[-1]+v)
    detrend=[v-(prefix[min(len(grid),i+17)]-prefix[max(0,i-16)])/(min(len(grid),i+17)-max(0,i-16)) for i,v in enumerate(grid)]
    rates=[]
    for start in range(0,len(grid),1200):
        end=min(len(grid),start+1200)
        if end-start<30 or any(start<=int(t/.25)<end for t in splices):
            continue
        peaks=find_peaks(detrend[start:end],10)
        intervals=[(peaks[i]-peaks[i-1])*.25 for i in range(1,len(peaks)) if 2.5<=(peaks[i]-peaks[i-1])*.25<=10]
        if len(peaks)>=3 and len(intervals)>=2:
            rates.append(60/stats.median(intervals))
    cov.update(usable_windows=len(rates),splices=len(splices))
    rate=stats.median(rates) if rates else None
    if rate is None or not 8<=rate<=25:
        return result(reason="No unspliced RSA windows produce a respiration estimate in 8–25 breaths/min",coverage=cov)
    return result(rate,coverage=cov,unit="breaths/min",source="SleepStager.swift: RR RSA, 4Hz / 8s detrend / 300s windows")


def interpolate(times,values,grid):
    out=[]
    seg=0
    for t in grid:
        while seg<len(times)-2 and times[seg+1]<t:
            seg+=1
        ta,tb=times[seg:seg+2]
        va,vb=values[seg:seg+2]
        out.append(va if tb<=ta else va+clamp((t-ta)/(tb-ta))*(vb-va))
    return out


def find_peaks(values,distance):
    candidates=[]
    i=1
    while i<len(values)-1:
        if values[i]>values[i-1] and values[i]>=0:
            j=i
            while j+1<len(values) and values[j+1]==values[i]:
                j+=1
            if j+1<len(values) and values[j+1]<values[i]:
                candidates.append((i+j)//2)
            i=j+1
        else:
            i+=1
    # NOOP findPeaks keeps tallest candidates first, earliest on equal height.
    kept=[]
    for i in sorted(candidates,key=lambda i:(-values[i],i)):
        if all(abs(i-j)>=distance for j in kept):
            kept.append(i)
    return sorted(kept)


def motion_series(gravity):
    gravity=sorted(gravity)
    return [(row[0],0. if i==0 else math.sqrt(sum((a-b)**2 for a,b in zip(row[1:],gravity[i-1][1:])))) for i,row in enumerate(gravity)]


def nearest(hr,t,tolerance=5):
    if not hr:
        return None
    j=bisect.bisect_left(hr,(t,-math.inf))
    candidates=[hr[i] for i in [j-1,j] if 0<=i<len(hr) and abs(hr[i][0]-t)<=tolerance]
    return min(candidates,key=lambda x:(abs(x[0]-t),-x[0]))[1] if candidates else None


def workouts(hr,gravity,resting_hr,max_hr,settings=None):
    """WorkoutDetector.swift HR+motion gates, smoothing, bridge and warmup backdate."""
    settings=analytics_settings(settings)
    hr,motion=sorted(hr),motion_series(gravity)
    if not hr or not motion:
        return result(reason="Workout detection requires both HR and calibrated gravity samples",coverage={"hr":len(hr),"motion":len(motion)})
    if not finite(resting_hr):
        resting_hr=sorted(h for _,h in hr)[max(0,math.ceil(.10*len(hr))-1)]
    floor=resting_hr+15
    smooth=[]
    lo,total=0,0.
    for i,(t,v) in enumerate(motion):
        total+=v
        while t-motion[lo][0]>10:
            total-=motion[lo][1]
            lo+=1
        smooth.append(total/(i-lo+1))
    active=[t for (t,_),v in zip(motion,smooth) if v>.20 and (nearest(hr,t) or 0)>floor]
    runs=[]
    for t in active:
        if not runs or t-runs[-1][1]>150:
            runs.append([t,t])
        else:
            runs[-1][1]=t
    bridged=[]
    for start,end in runs:
        gap_hr=[h for t,h in hr if bridged and bridged[-1][1]<t<start]
        if bridged and start-bridged[-1][1]<=300 and (not gap_hr or stats.mean(gap_hr)>floor):
            bridged[-1][1]=end
        else:
            bridged.append([start,end])
    sessions=[]
    for idx,(start,end) in enumerate(bridged):
        if end-start<290:
            continue
        core=[(t,h) for t,h in hr if start<=t<=end]
        if not core:
            continue
        counts=[0]*6
        if finite(max_hr) and max_hr>resting_hr:
            for _,h in core:
                counts[zone_weight(h,resting_hr,max_hr)]+=1
            if sum(counts[2:])/len(core)<.50:
                continue
        j=bisect.bisect_left([t for t,_ in motion],start)
        while j>0 and smooth[j-1]>.20 and motion[j][0]-motion[j-1][0]<=150:
            j-=1
        start=max(motion[j][0],bridged[idx-1][1]+1 if idx else -math.inf)
        window=[(t,h) for t,h in hr if start<=t<=end]
        e=effort(window,max_hr,resting_hr,settings.get("effort_method","edwards"),settings.get("sex"))
        bucket_count=math.floor((end-start)/60)+1
        observed_buckets=len(set(int((t-start)//60) for t,_ in window))
        sessions.append(dict(start=start,end=end,duration_seconds=end-start,avg_hr=stats.mean(h for _,h in window),
                             peak_hr=max(h for _,h in window),effort=e["value"],hr_coverage_percent=100*observed_buckets/bucket_count,
                             zone_percent=[rounded(100*v/len(core),1) for v in counts],
                             calories=bout_calories(window,settings,max_hr,resting_hr)))
    return result(sessions,coverage={"hr":len(hr),"motion":len(motion),"active_samples":len(active),"runs":len(runs),"bridged_runs":len(bridged)},source="WorkoutDetector.swift")


def bout_calories(hr,profile,max_hr,resting_hr):
    """Calories in WorkoutDetector.swift, Keytel fitness adjusted + Harris–Benedict."""
    if not all(finite(profile.get(k)) and profile[k]>0 for k in ("age","weight_kg","height_cm")) or not profile.get("sex"):
        return result(reason="Requires age, weight, height and sex for personalized calories")
    if not finite(max_hr) or not finite(resting_hr) or resting_hr<=0 or max_hr<=resting_hr:
        return result(reason="Requires HRmax and resting HR for personalized calories")
    male=[88.362,13.397,479.9,5.677,.634,.404,.394,.271,-95.7735]
    female=[447.593,9.247,309.8,4.33,.450,.380,.103,.274,-59.3954]
    sex=profile["sex"].lower()
    c=male if sex=="male" else female if sex=="female" else [(a+b)/2 for a,b in zip(male,female)]
    age,weight,height=profile["age"],profile["weight_kg"],profile["height_cm"]
    resting=max(0,c[0]+c[1]*weight+c[2]*height/100-c[3]*age)/86400
    vo2=15.3*max_hr/resting_hr
    total=0.
    hr=sorted(hr)
    for i,(t,h) in enumerate(hr):
        dur=min(hr[i+1][0]-t,150) if i<len(hr)-1 and hr[i+1][0]>t else 1.
        active=max(0,c[4]*min(h,max_hr)+c[5]*vo2+c[6]*weight+c[7]*age+c[8])/251.04
        total+=(resting if h<resting_hr+.30*(max_hr-resting_hr) else active)*dur
    return result(total,kj=total*4.184,unit="kcal",source="WorkoutDetector.swift Calories: Keytel 2005 + Uth 2004")


def correlation(pairs):
    """CorrelationEngine.swift Pearson / OLS / normal approximation of t tail."""
    pairs=[(a,b) for a,b in pairs if finite(a) and finite(b)]
    n=len(pairs)
    if n<3:
        return result(reason="Requires at least 3 paired observations",coverage={"pairs":n})
    xs,ys=zip(*pairs)
    mx,my=stats.mean(xs),stats.mean(ys)
    xx=sum((x-mx)**2 for x in xs)
    yy=sum((y-my)**2 for y in ys)
    xy=sum((x-mx)*(y-my) for x,y in pairs)
    if xx<=0 or yy<=0:
        return result(reason="Correlation is undefined when either series has no variation",coverage={"pairs":n})
    r=clamp(xy/math.sqrt(xx*yy),-1,1)
    t=abs(r)*math.sqrt((n-2)/(1-r*r)) if abs(r)<1 else math.inf
    if math.isinf(t):
        p=0.
    else:
        x=t/math.sqrt(2)
        q=1/(1+.3275911*x)
        erf=1-(((((1.061405429*q-1.453152027)*q)+1.421413741)*q-.284496736)*q+.254829592)*q*math.exp(-x*x)
        p=1-erf
    return result(r,coverage={"pairs":n},slope=xy/xx,intercept=my-xy/xx*mx,p_approx=p,source="CorrelationEngine.swift")


def skin_temp_anchor(raws):
    """WhoopProtocol/Streams.swift Whoop4SkinTemp.deviceAnchorRaw exact gates."""
    usable=[v for v in raws if finite(v) and 550<=v<=2040]
    learned=len(usable)>=100
    return result(stats.median(usable) if learned else 826.,coverage={"in_band_samples":len(usable),"minimum_samples":100},
                  source="device_worn_median" if learned else "NOOP global fallback826",provisional=True,
                  reason=None if learned else "Fewer than 100 worn-band raw values; provisional global offset used")


STAGES=["deep","rem","light","awake"]
TRANSITION=[[.86,.007,.126,.007],[.005,.88,.10,.015],[.06,.06,.85,.03],[0.,0.,.10,.90]]


def viterbi(emissions):
    """SleepStagerV2.swift sticky transitions, earliest-stage tie rule."""
    if not emissions:
        return []
    current=emissions[0][:]
    back=[]
    for emission in emissions[1:]:
        nxt,bp=[],[]
        for s in range(4):
            options=[current[p]+math.log(max(TRANSITION[p][s],1e-9)) for p in range(4)]
            p=max(range(4),key=lambda p:options[p])
            nxt.append(options[p]+emission[s])
            bp.append(p)
        current=nxt
        back.append(bp)
    last=max(range(4),key=lambda i:current[i])
    path=[last]
    for bp in reversed(back):
        last=bp[last]
        path.append(last)
    return [STAGES[i] for i in reversed(path)]


def stage_epochs(features):
    """Exact V2 emissions and two-pass 60-minute REM onset guard."""
    if not features:
        return []
    def z_values(key):
        values=[f.get(key) for f in features]
        present=[v for v in values if v is not None]
        mean=stats.mean(present) if present else 0.
        sd=(stats.pstdev(present) or 1.) if present else 1.
        return [(v-mean)/sd if v is not None else 0. for v in values]
    zhr,zhv,zmv,zrg=[z_values(k) for k in ["hr","hr_var","move_fraction","resp_regularity"]]
    flat=sorted(f["hr_flat11"] for f in features if f.get("hr_flat11") is not None)
    emissions=[]
    for i,f in enumerate(features):
        pct=bisect.bisect_right(flat,f["hr_flat11"])/len(flat) if flat and f.get("hr_flat11") is not None else .5
        gate=5*max(0,pct-.25)
        cardiac=.8*zhv[i]+.4*zhr[i]
        if f["move_fraction"]<=0 and f["jerk_max"]<=f["jerk_scale"]*55:
            cardiac=min(0,cardiac)
        clock=f["clock"]
        e=[-1.1*zhv[i]-.5*zmv[i]-gate+math.log(.15)+1.2*max(0,1-clock/.55),
           .6*zhv[i]-.6*zmv[i]+.4*zhr[i]+math.log(.22)+clock,
           math.log(.5),zmv[i]+cardiac+math.log(.10)]
        if f["jerk_max"]>f["jerk_scale"]*55:
            e[3]+=2
        if f.get("resp_regularity") is not None:
            e[0]+=.6*zrg[i]
            e[1]-=.6*zrg[i]
        emissions.append(e)
    first=viterbi(emissions)
    run,origin=0,0.
    for i,label in enumerate(first):
        run=0 if label=="awake" else run+1
        if run>=10:
            origin=features[i-9]["minutes"]
            break
    for e,f in zip(emissions,features):
        e[1]-=3*clamp(1-(f["minutes"]-origin)/60)
    return viterbi(emissions)


def resp_regularity(beats):
    """SleepStagerV2 RSA peakedness, the same DFT bins via Goertzel recurrence.

    Resampling, detrending, band limits and peak/sum are unchanged. Goertzel
    evaluates each bin's squared DFT magnitude with constant memory; differences
    from direct sine/cosine sums are floating-point roundoff only.
    """
    if len(beats)<12 or beats[-1][0]<=beats[0][0]:
        return None
    n=math.ceil((beats[-1][0]-beats[0][0])/.25-1e-9)
    if n<16:
        return None
    y=interpolate([t for t,v in beats],[v for t,v in beats],[beats[0][0]+i*.25 for i in range(n)])
    mean=stats.mean(y)
    y=[v-mean for v in y]
    powers=[]
    for coefficient in _dft_table(n):
        older=previous=0.
        for v in y:
            current=v+coefficient*previous-older
            older=previous;previous=current
        powers.append(previous*previous+older*older-coefficient*previous*older)
    return max(powers)/sum(powers) if powers and sum(powers)>0 else None


@lru_cache(maxsize=8)
def _dft_table(n):
    """Bounded exact NOOP band-bin recurrence coefficients; no samples retained."""
    return tuple(2*math.cos(2*math.pi*k/n) for k in range(math.ceil(.15*.25*n),math.floor(.40*.25*n)+1))


def stage_sleep(start,end,hr,gravity,rr):
    """V2 feature extraction / segments. Missing epochs are reported in coverage."""
    hs,gs,rs=defaultdict(list),defaultdict(list),defaultdict(list)
    for t,h in hr:
        if start-330<=t<end+390:
            hs[int(t)].append(h)
    for t,x,y,z in gravity:
        if start-330<=t<end+390:
            gs[int(t)].append((x,y,z))
    for t,v in rr:
        if start-330<=t<end+390:
            rs[int(t)].append(v)
    hs={t:stats.mean(v) for t,v in hs.items()}
    gs={t:tuple(stats.mean(v[i] for v in values) for i in range(3)) for t,values in gs.items()}
    features,jerk_pool=[],[]
    for epoch in range(math.ceil(start/30)*30,int(end),30):
        hrs=[hs[t] for t in range(epoch,epoch+30) if t in hs]
        gseq=[gs[t] for t in range(epoch,epoch+30) if t in gs]
        if not hrs and not gseq:
            continue
        jerks=[math.sqrt(sum((a-b)**2 for a,b in zip(gseq[i],gseq[i-1]))) for i in range(1,len(gseq))]
        jerk_pool.extend(jerks)
        def sd(lo,hi):
            values=[hs[t] for t in range(lo,hi) if t in hs]
            return stats.pstdev(values) if len(values)>=2 else None
        beats=sorted((float(t),clamp(v,300,2000)) for t in range(epoch-90,epoch+120) for v in rs.get(t,[]))
        features.append(dict(start=epoch,hr=stats.mean(hrs) if hrs else None,hr_var=sd(epoch-150,epoch+180),
                             hr_flat11=sd(epoch-330,epoch+390),jerks=jerks,gap_seconds=max(1,len(gseq)-1),
                             jerk_max=max(jerks,default=0),resp_regularity=resp_regularity(beats),
                             clock=(epoch+15-start)/max(1,end-start),minutes=(epoch+15-start)/60))
    scale=stats.median(jerk_pool) if jerk_pool else 1e-6
    for f in features:
        f["jerk_scale"]=scale
        f["move_fraction"]=sum(v>scale*38 for v in f["jerks"])/f["gap_seconds"]
    labels=stage_epochs(features)
    segments=[]
    for i,(f,label) in enumerate(zip(features,labels)):
        label="wake" if label=="awake" else label
        a=start if i==0 else f["start"]
        b=end if i==len(features)-1 else features[i+1]["start"]
        if segments and segments[-1]["stage"]==label:
            segments[-1]["end"]=b
        else:
            segments.append(dict(start=a,end=b,stage=label))
    return result(segments if features else None,None if features else "No HR or gravity-covered epochs in sleep window",
                  {"observed_epochs":len(features),"expected_epochs":math.ceil((end-start)/30)},source="SleepStagerV2.swift")


def session_resting_hr(start,end,hr):
    bins=defaultdict(list)
    for t,h in hr:
        if start<=t<=end:
            bins[min(int((t-start)//300),max(0,math.ceil((end-start)/300)-1))].append(h)
    means=[stats.mean(v) for v in bins.values()]
    gated=[stats.mean(v) for v in bins.values() if len(v)>=5 and stats.mean(v)>=25]
    return rounded(min(gated or means),0) if means else None


def adaptive_overnight_baseline(medians):
    """SleepStager.swift adaptiveOvernightHRBaseline: median, physiological floor40."""
    values=[v for v in medians if finite(v) and v>0]
    return max(40,stats.median(values)) if values else None


def merge_sleep_periods(periods):
    """SleepStager.mergePeriods literal neighbour absorption with 15-minute floor."""
    pending=[p[:] for p in periods];merged=[];i=0
    while i<len(pending):
        current=pending[i]
        if current[2]-current[1]>=900:merged.append(current);i+=1;continue
        prev=i>0 and bool(merged);nxt=i+1<len(pending)
        if prev and nxt and pending[i-1][0]==pending[i+1][0]:
            prior=merged.pop();merged.append([prior[0],prior[1],pending[i+1][2]]);i+=2
        elif nxt:pending[i+1][1]=current[1];i+=1
        elif prev:merged[-1][2]=current[2];i+=1
        else:i+=1
    return merged


def hr_sleep_band_across(a,b,hr,baseline):
    segment=[h for t,h in hr if a<t<=b]
    return bool(segment) and finite(baseline) and stats.median(segment)<=baseline*1.05


def bridge_sleep_periods(periods,hr,baseline,enabled=True):
    """SleepStager.bridgeSparseSleepTraced exact 90m gap and HR-vouched active60m."""
    if not enabled:return [p[:] for p in periods],[]
    out=[];attempts=[]
    for period in periods:
        p=period[:];left=None;active_seconds=0;drop=False
        if p[0] and out:
            if out[-1][0]:left=out[-1]
            elif len(out)>=2 and out[-2][0]:left=out[-2];active_seconds=out[-1][2]-out[-1][1];drop=True
        if left:
            gap=p[1]-left[2];in_band=hr_sleep_band_across(left[2],p[1],hr,baseline)
            cap=(3600 if in_band else 1800) if drop else 0
            reason="overlap" if gap<0 else "gapTooLong" if gap>5400 else "activeTooLong" if active_seconds>cap else "hrOutOfBand" if not in_band else "bridged"
            attempts.append(dict(gap_seconds=gap,active_seconds=active_seconds,active_cap_seconds=cap,hr_in_sleep_band=in_band,reason=reason))
            if reason=="bridged":
                if drop:out.pop()
                out[-1]=[True,left[1],p[2]];continue
        out.append(p)
    return out,attempts


def deeply_quiescent(start,end,gravity):
    """SleepStager.runIsDeeplyQuiescent: >=20 judged minutes, >=90% variance<.05g^2."""
    minutes=defaultdict(list)
    for row in gravity:
        if start<=row[0]<end:minutes[int(row[0]//60)].append(row[1:])
    judged=stable=0
    for rows in minutes.values():
        if len(rows)<2:continue
        means=[stats.mean(v[i] for v in rows) for i in range(3)]
        variance=sum(sum((v[i]-means[i])**2 for i in range(3)) for v in rows)/len(rows)
        judged+=1;stable+=variance<.05
    return judged>=20 and stable/judged>=.90


def off_wrist_fraction(start,end,hr,wrist_off=()):
    """SleepStager.offWristFraction union, with whole-stream sparse-HR density gate."""
    if end<=start:return 0.
    spans=[];all_hr=sorted(hr)
    if all_hr:
        span=all_hr[-1][0]-all_hr[0][0]
        dense=not (span>=600 and len(all_hr)<span/600)
        if dense:
            seen=[start]+[t for t,h in all_hr if start<=t<=end]+[end]
            spans.extend((a,b) for a,b in zip(seen,seen[1:]) if b-a>=1200)
    spans.extend((max(start,a),min(end,b)) for a,b in wrist_off if min(end,b)>max(start,a))
    if not spans:return 0.
    spans.sort();a,b=spans[0];covered=0
    for c,d in spans[1:]:
        if c<=b:b=max(b,d)
        else:covered+=b-a;a,b=c,d
    return (covered+b-a)/(end-start)


def passes_morning_guard(start,end,rhr,baseline,wake_end=None,band_states=()):
    """SleepStager daytime/H7 re-onset guards, optional actual band state rescue."""
    if end-start<5400 or not finite(rhr) or not finite(baseline) or rhr>baseline*.95:return False
    if wake_end is None or start<wake_end or start-wake_end>10800:return True
    states=[state for t,state in band_states if start<=t<=end]
    return bool(states) and sum(state==2 for state in states)/len(states)>=.6 or rhr<=baseline*.90


def detect_sleep(hr,gravity,timezone=dt.timezone.utc,recent_overnight_medians=(),wrist_off=(),band_states=()):
    """NOOP SleepStager.swift Stage0 detection, merge/bridge and physiological filters.

    Hypnogram staging occurs separately in stage_sleep. Empty gravity remains
    unscorable here; NOOP's separate lower-confidence HR-only fallback is not used.
    """
    gravity=sorted(gravity);hr=sorted(hr)
    if len(gravity)<2:return result(reason="Sleep detection requires at least two calibrated gravity samples",coverage={"motion_samples":len(gravity)})
    times=[g[0] for g in gravity];gaps=sorted(b-a for a,b in zip(times,times[1:]) if 0<b-a<300)
    cadence=max(gaps[len(gaps)//2],1.) if gaps else 60.;half=max(3,int(900/cadence))//2
    sparse=len(hr)>=2 and hr[-1][0]>hr[0][0] and (times[-1]-times[0]<.5*(hr[-1][0]-hr[0][0]) or max(b-a for a,b in zip(times,times[1:]))>1200)
    baseline=stats.median(h for _,h in hr) if hr else None;adaptive=adaptive_overnight_baseline(recent_overnight_medians)
    prefix=[0]
    for _,delta in motion_series(gravity):prefix.append(prefix[-1]+(delta<.01))
    flags=[(prefix[min(len(times),i+half+1)]-prefix[max(0,i-half)])/(min(len(times),i+half+1)-max(0,i-half))>=.70 for i in range(len(times))]
    periods=[];run=0
    for i in range(1,len(times)+1):
        at_end=i==len(times)
        class_changed=not at_end and flags[i]!=flags[run]
        gap=not at_end and times[i]-times[i-1]>1200
        if gap and sparse and not class_changed and flags[run] and hr_sleep_band_across(times[i-1],times[i],hr,baseline):gap=False
        if at_end or class_changed or gap:periods.append([flags[run],times[run],times[i-1]]);run=i
    merged=merge_sleep_periods(periods);durations=[p[2]-p[1] for p in merged if p[0]]
    fragmented=len(durations)>=2 and all(v<3600 for v in durations) and sum(durations)>=3600
    merged,attempts=bridge_sleep_periods(merged,hr,baseline,sparse or fragmented)
    sessions=[];rejected=defaultdict(int);candidates=[];chain_end=None;chain_overnight=False
    for asleep,start,end in merged:
        if not asleep:continue
        candidate=dict(start=start,end=end,duration_seconds=end-start);candidates.append(candidate)
        def reject(reason):candidate["gate"]=reason;rejected[reason]+=1
        if end-start<=3600:reject("minSleepMin");continue
        if end-start>57600:reject("maxMainSleepSpanS");continue
        in_hr=[(t,h) for t,h in hr if start<=t<=end];quiescent=deeply_quiescent(start,end,gravity)
        confirmation=adaptive if adaptive is not None else baseline;mult=1.30 if quiescent else 1.05
        if len(in_hr)>=30 and confirmation is not None and stats.median(h for _,h in in_hr)>confirmation*mult:reject("hrConfirm");continue
        off=off_wrist_fraction(start,end,hr,wrist_off)
        candidate.update(off_wrist_fraction=off,quiescent=quiescent)
        if off>=.5:reject("offWrist");continue
        rhr=session_resting_hr(start,end,hr)
        daytime=11<=dt.datetime.fromtimestamp((start+end)/2,timezone).hour<20
        continues=chain_end is not None and start-chain_end<=5400;night_tail=continues and chain_overnight
        if daytime and not night_tail and not passes_morning_guard(start,end,rhr,baseline,chain_end if chain_overnight else None,band_states):reject("morningStillness" if chain_overnight else "daytimeGuard");continue
        candidate["gate"]="accepted"
        sessions.append(dict(start=start,end=end,source="detected_motion",resting_hr=rhr,overnight_hr_median=stats.median(h for _,h in in_hr) if in_hr else None,sparse_motion=sparse))
        if not continues:chain_overnight=not 11<=dt.datetime.fromtimestamp(start,timezone).hour<20
        chain_end=end
    return result(sessions,None if sessions else "No motion-backed candidate passed the NOOP sleep gates",coverage=dict(motion_samples=len(gravity),hr_samples=len(hr),cadence_seconds=cadence,sparse_gravity=sparse,fragmented_to_nothing=fragmented,rejected=dict(rejected)),candidates=candidates,bridge_attempts=attempts,hr_baseline=baseline,adaptive_hr_baseline=adaptive,source="SleepStager.swift Stage0/merge/bridge/H4/H7/off-wrist gates",optional_paths=dict(hr_only="AnalyticsService opt-in when gravity is unavailable",band_state_wake_veto="NOOP default-off"))


def readiness(days):
    """ReadinessEngine thresholds, ln-HRV EWMA and optional ACWR/monotony."""
    if not days:
        return result(reason="No daily history",level="insufficient",signals=[])
    latest=days[-1]
    history=days[:-1][-30:]
    signals=[]
    for key,metric,sign,log in [("hrv","readiness_hrv_ln",1,True),("resting_hr","resting_hr",-1,False)]:
        values=[row.get(key) for row in history if finite(row.get(key))]
        value=latest.get(key)
        if len(values)<7 or not finite(value):
            continue
        if log:
            values=[math.log(max(v,1)) for v in values]
            value=math.log(max(value,1))
        b=baseline(values,metric,False)
        if b["usable"]:
            z=sign*(value-b["mean"])/(1.253*b["spread"])
            signals.append(dict(key=key,z=z,flag="good" if z>=.5 else "neutral" if z>=-.5 else "watch" if z>=-1 else "bad"))
    resp_values=[r.get("resp") for r in history if finite(r.get("resp"))]
    if len(resp_values)>=7 and finite(latest.get("resp")):
        sd=stats.stdev(resp_values)
        if sd>0:
            z=(latest["resp"]-stats.mean(resp_values))/sd
            if z>=1.5:
                signals.append(dict(key="respRate",z=z,flag="bad" if z>=2 else "watch"))
    loads=[r["effort"] for r in days if finite(r.get("effort"))]
    acwr=monotony=None
    if len(loads)>=14:
        chronic,acute=stats.mean(loads[-28:]),stats.mean(loads[-7:])
        if chronic>0:
            acwr=acute/chronic
            signals.append(dict(key="acwr",value=acwr,flag="watch" if acwr<.8 else "good" if acwr<1.3 else "watch" if acwr<1.5 else "bad"))
        week=loads[-7:]
        if len(week)>=4 and stats.stdev(week)>0:
            monotony=stats.mean(week)/stats.stdev(week)
            if monotony>=2:
                signals.append(dict(key="monotony",value=monotony,flag="watch"))
    bad=sum(s["flag"]=="bad" for s in signals)
    good=sum(s["flag"]=="good" for s in signals)
    watch=sum(s["flag"]=="watch" for s in signals)
    level="insufficient" if not signals else "rundown" if bad>=2 else "strained" if bad else "primed" if good>=2 and not watch else "balanced"
    return result(level,None if signals else "Requires 7 prior nightly observations or 14 load observations",level=level,
                  signals=signals,acwr=acwr,monotony=monotony,source="ReadinessEngine.swift")


def profile_for_day(settings,day):
    out=dict(settings);age=out.get("age")
    if (not finite(age) or age<=0) and out.get("birth_date",out.get("dob")):
        date=dt.date.fromisoformat(day);birth=dt.date.fromisoformat(out.get("birth_date",out.get("dob")))
        out["age"]=date.year-birth.year-((date.month,date.day)<(birth.month,birth.day))
    return out


def hr_only_sleep(hr):
    """Opt-in SleepStager.hrOnlySleepRuns spine; inferred bounds, measured HR/RR.

    The observed p10 is nearest-rank, minute medians use its 1.05 band,
    and HR gaps over20min break runs without a gravity-gap rescue.
    """
    if not hr:return result(reason="No HR samples for the opt-in HR-only sleep spine")
    bpms=sorted(h for _,h in hr);anchor=bpms[int((len(bpms)-1)*.10)]
    epochs=defaultdict(list);ends={}
    for t,h in hr:
        key=int(t//60);epochs[key].append(h);ends[key]=max(t,ends.get(key,t))
    keys=sorted(epochs);flags=[stats.median(epochs[k])<=anchor*1.05 for k in keys]
    periods=[];run=0
    for i in range(1,len(keys)+1):
        if i==len(keys) or flags[i]!=flags[run] or (keys[i]-keys[i-1])*60>1200:
            periods.append([flags[run],keys[run]*60,ends[keys[i-1]]]);run=i
    merged=merge_sleep_periods(periods)
    sessions=[dict(start=p[1],end=p[2],hr_only=True,source="hr_only",confidence="lower",bounds_inferred=True) for p in merged if p[0] and p[2]-p[1]>=3600]
    return result(sessions,None if sessions else "No HR-only run reached the source60-minute minimum",coverage=dict(hr_samples=len(hr),epochs=len(keys),raw_runs=len(periods),merged_runs=len(merged)),anchor_bpm=anchor,band_bpm=anchor*1.05,source="SleepStager.swift hrOnlySleepRuns/hrOnlySessions")


def day_energy(hr,profile,max_hr,resting_hr):
    """Calories.estimateDayEnergy exact observed-time integration, with explicit profile gate.

    NOOP's 70kg/170cm/age30 fallbacks are deliberately unavailable here: results
    require known profile inputs, and never extrapolate a partial recording to24h.
    """
    if not hr:return result(reason="No HR samples for observed energy")
    if not all(finite(profile.get(k)) and profile[k]>0 for k in ("age","weight_kg","height_cm")) or not profile.get("sex"):
        return result(reason="Requires age, weight, height and sex for personalized daily energy")
    if not finite(max_hr) or not finite(resting_hr) or resting_hr<=0 or max_hr<=resting_hr:return result(reason="Requires HRmax greater than known resting HR")
    male=[88.362,13.397,479.9,5.677,.634,.404,.394,.271,-95.7735];female=[447.593,9.247,309.8,4.33,.450,.380,.103,.274,-59.3954]
    sex=profile["sex"].lower();c=male if sex=="male" else female if sex=="female" else [(a+b)/2 for a,b in zip(male,female)]
    age,weight,height=profile["age"],profile["weight_kg"],profile["height_cm"]
    resting_rate=max(0,c[0]+c[1]*weight+c[2]*height/100-c[3]*age)/86400;vo2=15.3*max_hr/resting_hr
    ordered=sorted(hr,key=lambda r:(r[0],-r[1]));gaps=sorted(b[0]-a[0] for a,b in zip(ordered,ordered[1:]) if b[0]>a[0]);cadence=min(stats.median(gaps),60) if gaps else 1
    observed=min(86400,cadence+sum(min(gap,60) for gap in gaps));resting=resting_rate*observed;active=0
    for i,(t,h) in enumerate(ordered):
        duration=min(ordered[i+1][0]-t,cadence) if i+1<len(ordered) else cadence
        if duration<=0 or h<resting_hr+.50*(max_hr-resting_hr):continue
        gross=max(0,c[4]*min(h,max_hr)+c[5]*vo2+c[6]*weight+c[7]*age+c[8])/251.04
        active+=max(0,gross-resting_rate)*duration
    return result(resting+active,coverage=dict(observed_seconds=observed,nominal_sample_seconds=cadence,hr_samples=len(hr)),resting_kcal=resting,active_kcal=active,unit="kcal",estimated=True,source="WorkoutDetector.swift Calories.estimateDayEnergy")


def blood_oxygen(sensors,records=()):
    """Measured imported SpO2 only. WHOOP4 red/IR ADC never becomes a percentage."""
    observations=[];raw_count=0
    for sensor in sensors:
        v=sensor.get("values",{});kind=v.get("sensor_kind");original=v.get("sensor_values",{});value=None
        if kind=="HKQuantityTypeIdentifierOxygenSaturation":
            raw=original.get("value")
            try:raw=float(raw)
            except (TypeError,ValueError):raw=None
            # NOOP AppleHealthImporter/AppleHealthAggregator explicit HK type rule.
            if finite(raw):value=raw*100 if 0<raw<=1 else raw
        elif finite(v.get("spo2")):value=v["spo2"]
        elif kind=="spo2Sample":
            if original.get("unit") in ("percent","%","pct") and finite(original.get("red")):value=original["red"]
            else:raw_count+=1
        if any(k in v for k in ("spo2_raw","ppg_red_ir","ppg_red","ppg_ir")):raw_count+=1
        if finite(value) and 0<value<=100:observations.append(dict(timestamp_ms=sensor.get("timestamp_ms"),percent=value,source=sensor.get("source","imported")))
    for record in records:
        value=record.get("spo2_percent",record.get("spo2_pct"))
        if finite(value) and 0<value<=100:observations.append(dict(timestamp_ms=record.get("timestamp_ms"),percent=value,source=record.get("source","imported")))
    return result(stats.mean(r["percent"] for r in observations) if observations else None,None if observations else "WHOOP4 optical ADC is raw only; no measured imported SpO2 percentage",coverage=dict(measured_samples=len(observations),raw_only_samples=raw_count),unit="%",measured=True,observations=observations,source="Imported percent / NOOP Apple Health fraction-to-percent mapping")


def night_groups(blocks,offset_seconds=0):
    """SleepStageTotals.bridgedNightGroups two-tier <60m/<90m overnight bridge."""
    groups=[]
    for i in sorted(range(len(blocks)),key=lambda i:blocks[i]["start"]):
        b=blocks[i]
        if groups:
            last=groups[-1];gap=b["start"]-last["end"];hour=int((b["start"]+offset_seconds)%86400//3600)
            if gap>=0 and (gap<3600 or gap<5400 and (hour>=20 or hour<11)):
                last["end"]=max(last["end"],b["end"]);last["indices"].append(i);continue
        groups.append(dict(start=b["start"],end=b["end"],indices=[i]))
    return groups


def main_night_indices(blocks,offset_seconds=0,habitual_midsleep_seconds=None,cycle_gate=False):
    """SleepStageTotals scored groups and PhysiologicalSteps cycle eligibility."""
    explicit=[i for i,b in enumerate(blocks) if b.get("sleep_kind")=="main_sleep"]
    if cycle_gate and explicit:return explicit
    selectable=[i for i,b in enumerate(blocks) if b.get("sleep_kind")!="nap"]
    subset=[blocks[i] for i in selectable];groups=night_groups(subset,offset_seconds)
    if cycle_gate:
        groups=[g for g in groups if sum(max(0,subset[i]["end"]-subset[i]["start"]) for i in g["indices"])>=10800 and (g["start"]+offset_seconds)%86400//3600 not in range(11,20)]
    if not groups:return []
    target=habitual_midsleep_seconds if finite(habitual_midsleep_seconds) else 12600
    def score(g):
        mid=(g["start"]+(g["end"]-g["start"])/2+offset_seconds)%86400;delta=abs(mid-target)%86400;distance=min(delta,86400-delta)
        bonus=90 if distance<=7200 else 0 if distance>=18000 else 90*(18000-distance)/10800
        return ((g["end"]-g["start"])/60+bonus,-g["start"])
    picked=max(groups,key=score)
    return sorted(selectable[i] for i in picked["indices"])


def fallback_midnight_after(start,offset_seconds=0):
    """DayCycleResolver first local midnight at least18h after onset."""
    minimum=start+64800;midnight=math.floor((minimum+offset_seconds)/86400)*86400-offset_seconds
    return int(midnight if midnight>=minimum else midnight+86400)


def active_day_cycle(mode,latest_sleep,now,offset_seconds=0):
    """DayCycleResolver.activeWindow exact40h safety cap and unchanged recorded onset."""
    if mode!="sleep_onset" or not latest_sleep:
        start=math.floor((now+offset_seconds)/86400)*86400-offset_seconds
        return dict(start=start,end=now,day=dt.datetime.fromtimestamp(start+offset_seconds,dt.timezone.utc).date().isoformat(),source="calendar")
    if now-latest_sleep["start"]>=144000:
        start=fallback_midnight_after(latest_sleep["start"],offset_seconds)
        return dict(start=start,end=now,day=dt.datetime.fromtimestamp(start+offset_seconds,dt.timezone.utc).date().isoformat(),source="synthetic_midnight")
    return dict(latest_sleep,end=now)


def physiological_cycle_windows(boundaries,now):
    """PhysiologicalSteps.cycleWindows unique ids, onset-inclusive next-onset-exclusive."""
    seen=set();ordered=[]
    for b in boundaries:
        if b["start"]<=now and b["id"] not in seen:seen.add(b["id"]);ordered.append(b)
    ordered.sort(key=lambda b:b["start"]);output=[]
    for i,b in enumerate(ordered):
        end=ordered[i+1]["start"] if i+1<len(ordered) else now
        if end>b["start"]:output.append(dict(b,end=end))
    return output


def valid_zone_thresholds(raw,profile_gate=True):
    """HRZones.validCustomLowerBounds plus UserProfile persistence30..250 gate."""
    if isinstance(raw,str):
        try:raw=[float(v.strip()) for v in raw.split(",") if v.strip()]
        except ValueError:return None
    if not isinstance(raw,(list,tuple)) or len(raw)!=5:return None
    if not all(finite(v) and v>0 and (not profile_gate or 30<=v<=250) for v in raw):return None
    return list(raw) if all(raw[i]>raw[i-1] for i in range(1,5)) else None


def analytics_settings(settings):
    """Resolve NOOP persisted choices; invalid restored strings get source defaults."""
    out=dict(settings or {})
    for key,allowed,default in [("hrv_window",("whole","deep"),"whole"),("effort_method",("edwards","banister"),"edwards"),("effort_scale",("hundred","whoop"),"hundred"),("day_cycle_mode",("sleep_onset","midnight"),"sleep_onset")]:
        if out.get(key) not in allowed:out[key]=default
    out["hr_zone_thresholds"]=valid_zone_thresholds(out.get("hr_zone_thresholds"))
    return out


def hrv_windows(start,end,rr,stages=()):
    """SleepStager.sessionHrvWindows closed final bucket and center-stage tagging."""
    seg=[(t,v) for t,v in sorted(rr,key=lambda x:x[0]) if start<=t<=end]
    if not seg:return []
    output=[];t=start;i=0
    while True:
        final=t+300>=end;j=i
        while j<len(seg) and (final or seg[j][0]<t+300):j+=1
        values=[v for _,v in seg[i:j]];clean,_=clean_rr(values);reading=hrv(values)
        stage=next((s["stage"] for s in stages if s["start"]<=t+150<s["end"]),"?")
        output.append(dict(start=t,stage=stage,clean_beats=len(clean),rmssd_ms=reading["value"],sdnn_ms=reading["sdnn_ms"]))
        i=j;t+=300
        if t>=end:break
    return output


def sdnn_index(rr,segment_seconds=300):
    """HRVAnalyzer.sdnnIndex: first-beat anchored segments, with analyze timing gates."""
    if not rr or segment_seconds<=0:return None
    ordered=sorted(rr,key=lambda r:r[0]);start=ordered[0][0];last=ordered[-1][0];values=[];cursor=0
    while start<=last:
        stop=cursor
        while stop<len(ordered) and ordered[stop][0]<=start+segment_seconds-1:stop+=1
        segment=ordered[cursor:stop]
        if segment:
            value=hrv([v for _,v in segment],[t for t,_ in segment])["sdnn_ms"]
            if finite(value):values.append(value)
        cursor=stop;start+=segment_seconds
    return stats.mean(values) if values else None


def daily_hrv(sessions,mode="whole",main_indices=None,rr=None):
    """AnalyticsEngine avgHRVDaily: in-bed weighted whole or pooled deep windows.

    Source deep mode derives from windows directly (not session-average integrity
    gates); the known over-count is therefore surfaced alongside its source value.
    """
    motion=[r for r in sessions if not r.get("hr_only")];pool=motion or sessions
    windows=[w for r in pool for w in r.get("hrv_windows",[])];deep=[w["rmssd_ms"] for w in windows if w["stage"]=="deep" and finite(w["rmssd_ms"])]
    whole_pairs=[(r["hrv"]["value"],r["end"]-r["start"]) for r in pool if r.get("hrv",{}).get("value") is not None]
    refused=[r for i,r in enumerate(sessions) if i in (main_indices or []) and r.get("hrv",{}).get("coverage",{}).get("beat_coverage",0)>1.10]
    if refused:whole_pairs=[(r["hrv"]["value"],r["end"]-r["start"]) for i,r in enumerate(sessions) if i in (main_indices or []) and r in pool and r.get("hrv",{}).get("value") is not None]
    whole=sum(v*w for v,w in whole_pairs)/sum(w for _,w in whole_pairs) if whole_pairs else None
    value=stats.mean(deep) if mode=="deep" and deep else None if mode=="deep" else whole
    index=sdnn_index([(t,v) for t,v in (rr or []) if any(s["start"]<=t<s["end"] for s in pool)])
    return result(value,None if value is not None else "No deep-stage 5-minute windows with 20 clean beats" if mode=="deep" else "Main-night RR over-count refused; naps cannot replace it" if refused else "No scorable sleep RR windows",coverage=dict(sessions=len(pool),windows=len(windows),deep_windows=len(deep)),rmssd_ms=value,sdnn_index_ms=index,whole_night_ms=whole,deep_only_ms=stats.mean(deep) if deep else None,window=mode,integrity_warning="Source deep-window mode bypasses refused session averages" if mode=="deep" and refused else None,source="AnalyticsEngine.swift avgHRVDaily / HRVAnalyzer.sdnnIndex / SleepStager.sessionHrvWindows")


def frequency_hrv(rr):
    """HRVFreqDomain.swift exact normalized Lomb-Scargle grid; approximate NOOP units."""
    nn,_=clean_rr(rr)
    times=[];acc=0
    for value in nn:
        times.append(acc/1000);acc+=value
    span=times[-1] if times else 0
    if len(nn)<20 or span<60:
        return result(reason="Requires 20 clean beats and 60 seconds of RR span",coverage=dict(clean_beats=len(nn),span_seconds=span))
    y=[v-stats.mean(nn) for v in nn];variance=sum(v*v for v in y)/len(y)
    def band(lo,hi):
        if variance==0:return 0.
        previous=None;total=0.;f=lo
        while f<=hi+1e-12:
            w=2*math.pi*f
            tau=math.atan2(sum(math.sin(2*w*t) for t in times),sum(math.cos(2*w*t) for t in times))/(2*w)
            cs=[math.cos(w*(t-tau)) for t in times];sn=[math.sin(w*(t-tau)) for t in times]
            cd=sum(v*v for v in cs);sd=sum(v*v for v in sn)
            power=((sum(a*b for a,b in zip(y,cs))**2/cd if cd>0 else 0)+(sum(a*b for a,b in zip(y,sn))**2/sd if sd>0 else 0))/(2*variance)
            if previous:total+=(power+previous[1])*.5*(f-previous[0])
            previous=(f,power);f+=.005
        return total
    hf=band(.15,.40);lf=band(.04,.15) if span>=250 else None
    return result(dict(hf=hf,lf=lf,lf_hf=lf/hf if lf is not None and hf>0 else None,total_power=hf if lf is None else hf+lf+band(.0033,.04)),coverage=dict(clean_beats=len(nn),span_seconds=span),source="HRVFreqDomain.swift")


def weighted_median(pairs):
    pairs=sorted(pairs);half=sum(w for _,w in pairs)/2;cum=0
    for i,(v,w) in enumerate(pairs):
        cum+=max(0,w)
        if cum>half:return v
        if cum==half:return (v+pairs[min(i+1,len(pairs)-1)][0])/2
    return pairs[-1][0]


def steps_estimate(motion,points=(),manual=None):
    """StepsEstimateEngine.swift: personal motion-weighted fit; never uncalibrated steps."""
    usable=[(m,n) for m,n in points if finite(m) and finite(n) and m>=1 and n>0]
    if finite(manual) and manual>0:k=manual;confidence=1.;source="manual"
    elif len(usable)>=3:
        ratios=[(n/m,m) for m,n in usable];k=weighted_median(ratios)
        confidence=.5*min(1,len(usable)/14)+.5*max(0,1-weighted_median([(abs(r-k),w) for r,w in ratios])/k)
        source="motion_weighted_calibration"
    else:return result(reason="Requires 3 days of overlapping reference steps and motion, or a manual coefficient",coverage=dict(calibration_days=len(usable)),estimated=True)
    value=int(clamp(rounded(motion*k,0),0,60000)) if finite(motion) and motion>=1 else None
    return result(value,None if value is not None else "Motion volume below the calibration floor",coverage=dict(calibration_days=len(usable)),coefficient=k,confidence=confidence,estimated=True,source=source)


def manual_steps_coefficient(settings):
    """Source manualOverride is positive steps per motion unit, not a multiplier.

    A persisted default is not a user calibration. Null/zero requests auto-fit.
    """
    value=settings.get("step_calibration",settings.get("steps_per_motion"))
    if settings.get("profile_provenance",{}).get("step_calibration")=="default":return None
    return value if finite(value) and value>0 else None


def recovery_forecast(charges,efforts,today_effort,planned_sleep,need=8,need_nights=0):
    """RecoveryForecast.swift; observed nights, not missing-night zeroes."""
    recent=[x for x in charges if finite(x)][-14:]
    if len(recent)<5:return result(reason="Requires 5 observed Charge nights",coverage=dict(nights=len(recent)))
    if not finite(planned_sleep):return result(reason="Requires planned sleep hours")
    center=stats.mean(recent);mx=(len(recent)-1)/2
    slope=sum((i-mx)*(v-center) for i,v in enumerate(recent))/sum((i-mx)**2 for i in range(len(recent)))
    prior=[x for x in efforts if finite(x)][-14:]
    strain=clamp(-9*(today_effort-stats.mean(prior))/12,-12,12) if finite(today_effort) and prior else 0
    sleep=14*clamp(max(0,planned_sleep)/max(.1,need)-1,-1,.25)
    score=rounded(clamp(center+strain+sleep+clamp(-slope,-8,8),0,100),0)
    band=rounded(max(stats.stdev(recent),8)+(6 if len(recent)<10 else 0),0)
    return result(score,coverage=dict(nights=len(recent)),band=band,baseline=center,planned_sleep_hours=max(0,planned_sleep),need_hours=need,confidence="solid" if len(recent)>=10 and need_nights>=7 else "building",source="RecoveryForecast.swift")


def hydration_goal(sex=None,effort_value=None):
    """HydrationGoal.swift fluid adequate-intake baseline plus capped Effort adjustment."""
    sex=str(sex or "").strip().lower();base=3700 if sex in ("male","m") else 2700 if sex in ("female","f") else 3200
    bump=clamp(rounded(effort_value/100*700,0),0,700) if finite(effort_value) else 0
    return int((base+bump+25)//50*50)


def lift_metrics(sets):
    """LiftMetrics.swift typed working sets; no cardiovascular strain imputation."""
    performed=[r.get("details",r) for r in sets if r.get("details",r).get("reps")!=0]
    grouped=defaultdict(list)
    for original in sets:
        r=original.get("details",original)
        if r.get("reps")!=0:grouped[original.get("exercise",r.get("exercise","Unspecified"))].append(r)
    output=[]
    for name,rows in grouped.items():
        total=0.;estimates=[]
        for r in rows:
            w=r.get("weight_kg",r.get("weightKg"));n=r.get("reps")
            if finite(w) and finite(n) and w>0 and n>0 and not r.get("isWarmup",r.get("is_warmup",False)):total+=w*n
            if finite(w) and finite(n) and w>0 and 1<=n<=12 and not r.get("isWarmup",r.get("is_warmup",False)):estimates.append(w if n==1 else w*(1+n/30))
        working=[r for r in rows if not r.get("isWarmup",r.get("is_warmup",False))]
        def rank(r):
            w=r.get("weight_kg",r.get("weightKg"));n=r.get("reps")
            estimate=w if finite(w) and n==1 else w*(1+n/30) if finite(w) and finite(n) and 1<n<=12 else None
            return (estimate is not None,estimate if estimate is not None else w if finite(w) else 0)
        best=max(working,key=rank) if working else {}
        output.append(dict(exercise=name,performed_sets=len(rows),working_sets=len(working),warmup_sets=len(rows)-len(working),volume_kg=total or None,estimated_1rm_kg=max(estimates) if estimates else None,best_weight_kg=best.get("weight_kg",best.get("weightKg")),best_reps=best.get("reps")))
    return dict(exercises=output,performed_sets=len(performed),rpe_profile=lift_rpe_profile(sets),muscle_counts=lift_muscle_counts(sets),source="LiftMetrics.swift",cardiovascular_strain=None)


LIFT_MUSCLES=frozenset("chest frontDelts sideDelts rearDelts triceps lats upperBack traps biceps forearms quads hamstrings glutes adductors abductors calves abs obliques lowerBack neck".split())


def lift_rpe_profile(sets,threshold=8):
    """LiftMetrics.rpeProfile; rated/unrated counted independently of muscle credit."""
    working=[r.get("details",r) for r in sets if r.get("details",r).get("reps")!=0 and not r.get("details",r).get("isWarmup",r.get("details",r).get("is_warmup",False))]
    rated=[r["rpe"] for r in working if finite(r.get("rpe"))]
    return dict(mean=stats.mean(rated) if rated else None,rated_sets=len(rated),unrated_sets=len(working)-len(rated),sets_at_or_above_threshold=sum(v>=threshold for v in rated),threshold=threshold)


def lift_muscle_counts(sets):
    """LiftMetrics.muscleCounts exact explicit canonical primary/secondary assignments."""
    direct=defaultdict(int);indirect=defaultdict(int);fractional=defaultdict(float);unclassified=0
    for original in sets:
        r=original.get("details",original)
        if r.get("reps")==0 or r.get("isWarmup",r.get("is_warmup",False)):continue
        primary=r.get("primaryMuscle",r.get("primary_muscle"));secondary=r.get("secondaryMuscles",r.get("secondary_muscles",[]))
        if isinstance(secondary,str):
            try:secondary=json.loads(secondary)
            except ValueError:secondary=[]
        if not isinstance(secondary,list):secondary=[]
        if primary in LIFT_MUSCLES:direct[primary]+=1;fractional[primary]+=1
        else:unclassified+=1
        for muscle in secondary:
            if muscle in LIFT_MUSCLES and muscle!=primary:indirect[muscle]+=1;fractional[muscle]+=.5
    return dict(direct=dict(direct),indirect=dict(indirect),fractional=dict(fractional),unclassified_working_sets=unclassified,reference_weekly_band=dict(hypertrophy_minimum=4,strength_minimum=1,strength_plateau=4),source="LiftMetrics.swift / WhoopStore LiftMuscle.swift")


def lift_session_progression(records):
    """LiftSessionDetailSheet previousVolume comparison using explicit session identity.

    Missing session ids stay unassociated rather than merging separate sessions
    into a day. Only observed performed sets contribute to a session's volume.
    """
    grouped=defaultdict(list);unassociated=[];sessions={}
    for r in records:
        if r.get("kind")=="workout":sessions[r.get("id")]=r
    for record in records:
        if record.get("kind")!="lifting_set":continue
        details=record.get("details",{});key=next((record.get(k,details.get(k)) for k in ("session_id","sessionId","workout_id","workoutId") if record.get(k,details.get(k)) is not None),None)
        if key is None:unassociated.append(record.get("id"));continue
        grouped[str(key)].append(record)
    ordered=[]
    for key,rows in grouped.items():
        session=sessions.get(key,{})
        stamps=[r.get("start_ms",r.get("timestamp_ms")) for r in rows];stamps=[v for v in stamps if finite(v)]
        stamp=session.get("start_ms",min(stamps) if stamps else None)
        if not finite(stamp):
            dates=[r.get("day",r.get("date")) for r in rows if r.get("day",r.get("date"))]
            if dates:stamp=dt.datetime.combine(dt.date.fromisoformat(min(dates)),dt.time.min,tzinfo=dt.timezone.utc).timestamp()*1000
        if not finite(stamp):unassociated.extend(r.get("id") for r in rows);continue
        rows=sorted(rows,key=lambda r:r.get("ord",r.get("set_index",r.get("details",{}).get("ord",0))) or 0)
        ordered.append(dict(session_id=key,start_ms=stamp,day=session.get("day",next((r.get("day",r.get("date")) for r in rows if r.get("day",r.get("date"))),None)),record_ids=[r.get("id") for r in rows],exercises=lift_metrics(rows)["exercises"]))
    ordered.sort(key=lambda r:(r["start_ms"],r["session_id"]));previous={}
    for session in ordered:
        for exercise in session["exercises"]:
            before=previous.get(exercise["exercise"]);volume=exercise["volume_kg"]
            # Date-only records on the same day do not establish session order.
            if before and before[2]>=session["start_ms"]:before=None
            exercise["previous_session_id"]=before[0] if before else None
            exercise["previous_volume_kg"]=before[1] if before else None
            exercise["volume_delta_kg"]=volume-before[1] if finite(volume) and before and before[1]>0 else None
            if finite(volume):previous[exercise["exercise"]]=(session["session_id"],volume,session["start_ms"])
    return result(ordered,None if ordered else "Requires explicitly associated lifting sets with observed session dates",coverage=dict(sessions=len(ordered),unassociated_sets=len(unassociated)),unassociated_record_ids=unassociated,source="LiftMetrics.swift / LiftSessionDetailSheet previousVolume")


def lift_session_load(rpe,duration_seconds):
    """LiftMetrics.sessionLoad Foster sRPE-TL, independent from HR-measured Effort."""
    valid=finite(rpe) and rpe>0 and finite(duration_seconds) and duration_seconds>0
    return result(rpe*duration_seconds/60 if valid else None,None if valid else "Requires a rated session and a positive observed duration",source="LiftMetrics.swift Foster sRPE-TL")


def fitness_age(age,sex,rhr,efforts,waist=None):
    """FitnessAgeEngine.swift HUNT peer-equivalent age; actual weekly coverage required."""
    values=[x for x in efforts if finite(x)]
    if not finite(age) or age<=0 or not finite(rhr) or rhr<=0 or len(values)<4:
        return result(reason="Requires age, resting HR and 4 observed activity days in the last 7",coverage=dict(activity_days=len(values)))
    active=[x for x in values if x>0];n=len(active);frequency=0 if n<1 else .5 if n==1 else 1 if n==2 else 2.5 if n<=4 else 5
    pai=frequency*min(3,max(0,stats.mean(active)/30)) if active else 0
    female=str(sex).lower()=="female";intercept,ac,wc,rc,pc=(74.74,.247,.259,.114,.198) if female else (100.27,.296,.369,.155,.226)
    fa=clamp(age+(rc*(rhr-65)-pc*(pai-5))/ac,20,80)
    vo2=intercept-ac*age+pc*pai-wc*waist-rc*rhr if finite(waist) and waist>0 else None
    return result(fa,coverage=dict(activity_days=len(values)),estimated=True,vo2max_estimate=vo2,pa_index=pai,band_years=5,lower_confidence=sex not in ("male","female") or len(values)<6,source="FitnessAgeEngine.swift")


def vitality(age,rhr=None,sleep_hours=None,consistency=None,rmssd=None,steps=None,vo2=None,expected_vo2=None):
    """VitalityEngine.swift population-equivalent estimate, requires 3 independent factors."""
    contributors={}
    if finite(rhr):contributors["rhr"]=(rhr-65)/10*.100
    if finite(vo2) and finite(expected_vo2) and expected_vo2>0:contributors["vo2max"]=clamp((expected_vo2-vo2)/3.5,-4,4)*.130
    if finite(sleep_hours):contributors["sleep"]=clamp(max(0,abs(sleep_hours-7.5)-.5),0,3)*.110
    if finite(consistency):contributors["consistency"]=(.75-clamp(consistency))*.450
    if finite(age) and finite(rmssd):
        anchors=[(20,47),(30,40),(40,33),(50,29),(60,25),(70,22),(80,20)]
        norm=interpolate([a for a,_ in anchors],[v for _,v in anchors],[clamp(age,20,80)])[0];contributors["hrv"]=clamp((norm-rmssd)/norm,-1,1)*.160
    if finite(steps):contributors["steps"]=clamp((7000-clamp(steps,0,11000))/1000,-4,4)*.064
    if not finite(age) or age<=0 or len(contributors)<3:return result(reason="Requires age and at least 3 available physiological/activity factors",coverage=dict(factors=len(contributors)))
    body=clamp(age+sum(contributors.values())*.75/(math.log(2)/8),20,90)
    return result(clamp(50+(age-body)*2.5,0,100),body_age=body,band_years=5,contributions=contributors,estimated=True,source="VitalityEngine.swift")


def illness_signal(zscores,trusted=False,confounders=(),already_unwell=False):
    """IllnessSignalEngine.swift illnessward z inputs, never a diagnosis."""
    fired=[k for k,v in zscores.items() if finite(v) and v>2];score=min(100,sum(min(40,22*(zscores[k]-2)) for k in fired))
    level="quiet"
    if trusted:
        if already_unwell:level="alreadyUnwell"
        elif len(fired)>=2 and score>=25:
            if confounders:score*=.45;level="suppressed"
            else:level="raised" if score>=50 else "mild"
    return result(score,level=level,fired_signals=fired,suppressed_by=list(confounders),baseline_trusted=trusted,source="IllnessSignalEngine.swift")


def circadian_phase(hourly,days_observed,habitual_wake=None):
    """CircadianEngine.swift 24h cosinor OLS. HR-derived phase, not measured CBT."""
    rows=[(h,v) for h,v in hourly if finite(h) and finite(v)]
    if len(rows)<3:return result(reason="Requires at least 3 distinct hourly HR bins")
    xs=[[1,math.cos(2*math.pi*h/24),math.sin(2*math.pi*h/24)] for h,_ in rows]
    a=[[sum(x[i]*x[j] for x in xs) for j in range(3)] for i in range(3)]
    rhs=[sum(x[i]*v for x,(_,v) in zip(xs,rows)) for i in range(3)]
    def det(m):return m[0][0]*(m[1][1]*m[2][2]-m[1][2]*m[2][1])-m[0][1]*(m[1][0]*m[2][2]-m[1][2]*m[2][0])+m[0][2]*(m[1][0]*m[2][1]-m[1][1]*m[2][0])
    determinant=det(a)
    if abs(determinant)<1e-12:return result(reason="Hourly phase fit is singular")
    coefficients=[]
    for k in range(3):coefficients.append(det([[rhs[i] if j==k else a[i][j] for j in range(3)] for i in range(3)])/determinant)
    mesor,beta,gamma=coefficients;amp=math.hypot(beta,gamma);phase=(math.atan2(gamma,beta)/(2*math.pi/24))%24;tmin=(phase-12)%24
    relative=amp/abs(mesor) if mesor else 0;readable=days_observed>=7 and (relative>=.10 or amp>=4.5)
    confidence="solid" if readable and days_observed>=14 and relative>=.10 else "wide" if readable else "unreadable"
    offset=(((tmin-(habitual_wake-2.5)%24)+12)%24-12)*60 if finite(habitual_wake) and readable else None
    return result(dict(mesor=mesor,amplitude=amp,acrophase_hour=phase,estimated_temperature_min_hour=tmin,offset_vs_schedule_minutes=offset),None if readable else "Requires 7 observed days and rhythmic amplitude >=10% or 4.5 bpm",coverage=dict(observed_days=days_observed,hour_bins=len(rows)),confidence=confidence,estimated=True,source="CircadianEngine.swift",parity_gap="Jet-lag/shift light planner not yet ported")


def behavior_effect(with_values,without_values):
    """BehaviorInsights.swift explicit logged Yes/No cohorts, approximate Welch tail."""
    a=[v for v in with_values if finite(v)];b=[v for v in without_values if finite(v)]
    if not a or not b or len(a)+len(b)<3:return result(reason="Requires explicitly logged Yes and No groups and 3 outcomes",coverage=dict(n_with=len(a),n_without=len(b)))
    ma,mb=stats.mean(a),stats.mean(b);va=stats.variance(a) if len(a)>1 else 0;vb=stats.variance(b) if len(b)>1 else 0
    pooled=((len(a)-1)*va+(len(b)-1)*vb)/(len(a)+len(b)-2);se=va/len(a)+vb/len(b)
    if se<=0:p=1. if ma==mb else 0.
    else:
        x=abs(ma-mb)/math.sqrt(se*2);q=1/(1+.3275911*x)
        p=(((((1.061405429*q-1.453152027)*q)+1.421413741)*q-.284496736)*q+.254829592)*q*math.exp(-x*x)
    return result(ma-mb,mean_with=ma,mean_without=mb,pct_change=(ma-mb)/abs(mb)*100 if mb else None,cohens_d=(ma-mb)/math.sqrt(pooled) if pooled>0 else 0,p_approx=p,significant=p<.05 and min(len(a),len(b))>=5,coverage=dict(n_with=len(a),n_without=len(b)),source="BehaviorInsights.swift",causal=False)


class AnalyticsService:
    """Read-only calculation over Store.path; derived results never write raw DB."""
    def __init__(self,store):
        self.store=store
        self._cache={}
        self._trends_cache={}
        self._trends_lock=threading.RLock()

    def invalidate_cache(self):
        """Recalculate projections after an import or full archive merge."""
        with self._trends_lock:
            self._cache.clear()
            self._trends_cache.clear()

    @staticmethod
    def _timezone(settings):
        name=settings.get("timezone") or "Australia/Brisbane"
        try:
            return name,ZoneInfo(name)
        except ZoneInfoNotFoundError:
            # Windows Python may lack tzdata. Brisbane is fixed UTC+10 all year.
            if name=="Australia/Brisbane":
                return name,dt.timezone(dt.timedelta(hours=10))
            if name in ("UTC","Etc/UTC"):
                return name,dt.timezone.utc
            raise ValueError("Timezone data unavailable; install tzdata or select Australia/Brisbane / UTC")

    def _read(self,device,start,end):
        conn=sqlite3.connect(self.store.path.as_uri()+"?mode=ro",uri=True)
        conn.row_factory=sqlite3.Row
        try:
            tables={row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows=[dict(r) for r in conn.execute("SELECT * FROM readings WHERE device=? AND timestamp_ms>=? AND timestamp_ms<? ORDER BY timestamp_ms,frame_id",(device,int(start*1000),int(end*1000)))]
            sensors=[]
            if "sensors" in tables:
                for r in conn.execute("SELECT * FROM sensors WHERE device=? AND timestamp_ms>=? AND timestamp_ms<? ORDER BY timestamp_ms,frame_id",(device,int(start*1000),int(end*1000))):
                    sensors.append(dict(r)|{"values":json.loads(r["values_json"])})
            records=[]
            if "feature_records" in tables:
                from features import record_stream_sample
                for r in conn.execute("SELECT id,kind,payload_json FROM feature_records WHERE deleted_ms IS NULL"):
                    payload=json.loads(r["payload_json"])
                    if r["kind"]=="health_sample":
                        sample=record_stream_sample(payload)
                        if sample and payload.get("device",payload.get("original",{}).get("deviceId")) in (None,"",device) and start*1000<=sample["timestamp_ms"]<end*1000:
                            sample["frame_id"]="feature:"+r["id"]
                            rows.append(sample)
                            raw_values=sample.get("sensor_values",{})
                            if sample.get("sensor_kind")=="skinTempSample" and finite(raw_values.get("raw")):sample["skin_temp_raw"]=raw_values["raw"]
                            if sample.get("sensor_kind")=="HKQuantityTypeIdentifierStepCount" and finite(raw_values.get("value")):sample["steps"]=float(raw_values["value"])
                            if sample.get("sensor_kind") or any(k in sample for k in ("spo2","respiratory_rate","skin_temp_c","steps")):
                                sensors.append(dict(frame_id=sample["frame_id"],device=sample["device"],timestamp_ms=sample["timestamp_ms"],
                                                    source=sample["source"],values={k:v for k,v in sample.items() if k in ("sensor_kind","sensor_values","spo2","respiratory_rate","skin_temp_c","skin_temp_raw","steps")}))
                    elif payload.get("device") in (None,"",device) or payload.get("source_category")=="imported":
                        records.append(payload|{"id":r["id"],"kind":r["kind"]})
            if "workout_dismissals" in tables:
                records.extend(dict(kind="workout_dismissal",device=device,start=r[0]/1000,end=r[1]/1000) for r in conn.execute("SELECT start_ms,end_ms FROM workout_dismissals WHERE device=? AND start_ms<? AND end_ms>?",(device,end*1000,start*1000)))
            return rows,sensors,records
        finally:
            conn.close()

    @staticmethod
    def _streams(rows,sensors):
        # Prefer banked HR over live HR on the same second to avoid dual-source load.
        by_second={}
        rr=[]
        gravity={}
        # Owned strap stream wins where a second overlaps an imported source.
        # Imports fill gaps; RR never combine competing trains on one second.
        rows=sorted(rows,key=lambda r:(int(r["timestamp_ms"]//1000),r.get("kind")=="imported",r.get("kind")=="live",r["timestamp_ms"]))
        rr_source={}
        rr_trains=set()
        for row in rows:
            t=int(row["timestamp_ms"]//1000)
            if row.get("contact")==0:
                continue
            if finite(row.get("hr")) and row["hr"]>0:
                if t not in by_second or row["kind"] not in ("live","imported"):
                    by_second[t]=row["hr"]
            try:
                intervals=json.loads(row["rr_json"])
            except (ValueError,TypeError):
                intervals=[]
            # Keep raw emission timestamps: never fabricate beat timing inside banked rows.
            source=(row.get("kind"),row.get("source",row.get("device","strap")))
            train=(t,source,tuple(intervals))
            if intervals and train not in rr_trains and (t not in rr_source or rr_source[t]==source):
                rr_source[t]=source
                rr_trains.add(train)
                rr.extend((t,v) for v in intervals if finite(v))
            if all(finite(row.get(k)) for k in ("gx","gy","gz")):
                gravity.setdefault(t,(t,row["gx"],row["gy"],row["gz"]))
        for s in sensors:
            v=s["values"]
            t=int(s["timestamp_ms"]//1000)
            if all(finite(v.get(k)) for k in ("gx","gy","gz")):
                gravity.setdefault(t,(t,v["gx"],v["gy"],v["gz"]))
        return sorted(by_second.items()),sorted(rr,key=lambda x:x[0]),sorted(gravity.values())

    @staticmethod
    def _saved_workout(record,hr,profile,maximum,rest_hr):
        """Preserve saved/imported identity and quantities; attach local scores separately."""
        a=record.get("start",record.get("start_ms",0)/1000);b=record.get("end",record.get("end_ms",0)/1000)
        sample=[(t,h) for t,h in hr if a<=t<=b]
        computed=effort(sample,maximum,rest_hr,profile.get("effort_method","edwards"),profile.get("sex"))
        calories=bout_calories(sample,profile,maximum,rest_hr)
        out=copy.deepcopy(record)|dict(start=a,end=b,record_id=record.get("id"),sport=record.get("sport",record.get("activity",record.get("name"))),source=record.get("source") or ("imported" if record.get("source_category")=="imported" else "manual"),duration_seconds=record.get("duration_seconds",b-a),computed_effort=computed,computed_calories=calories)
        out["effort"]=record.get("effort",computed)
        out["calories"]=record.get("calories",record.get("energy_kcal",calories))
        if not finite(out.get("avg_hr")) and sample:out["avg_hr"]=stats.mean(h for _,h in sample)
        if not finite(out.get("peak_hr")):out["peak_hr"]=record.get("max_hr",max((h for _,h in sample),default=None))
        return out

    @staticmethod
    def _visible_detected(candidates,records):
        """Source WorkoutSource.isDismissed half-open overlap, device-scoped read tokens."""
        spans=[r for r in records if r.get("kind")=="workout_dismissal"]
        return [dict(w,id="detected:"+str(int(w["start"]*1000))+":"+str(int(w["end"]*1000)),source="detected",detected=True) for w in candidates if not any(w["start"]<r["end"] and r["start"]<w["end"] for r in spans)]

    def _calculate(self,device,day,settings):
        settings=analytics_settings(settings)
        name,tz=self._timezone(settings)
        d=dt.date.fromisoformat(day)
        midnight=dt.datetime.combine(d,dt.time.min,tzinfo=tz).timestamp()
        end=dt.datetime.combine(d+dt.timedelta(days=1),dt.time.min,tzinfo=tz).timestamp()
        # Night belongs to wake date: read previous noon through today's midnight.
        read_start=dt.datetime.combine(d-dt.timedelta(days=1),dt.time(12),tzinfo=tz).timestamp()
        rows,sensors,records=self._read(device,read_start,end)
        # Do not retain megabytes of biometric JSON as every memoization key.
        fingerprint=hashlib.blake2b(digest_size=20)
        for value in (settings,rows,sensors,records):
            fingerprint.update(json.dumps(value,sort_keys=True,separators=(',',':')).encode())
        signature=(device,day,fingerprint.digest())
        if signature in self._cache:
            return copy.deepcopy(self._cache[signature])
        hr,rr,gravity=self._streams(rows,sensors)
        age=settings.get("age")
        profile_provenance=settings.get("profile_provenance",{})
        if (not finite(age) or age<=0) and settings.get("birth_date",settings.get("dob")):
            birth=dt.date.fromisoformat(settings.get("birth_date",settings.get("dob")))
            age=d.year-birth.year-((d.month,d.day)<(birth.month,birth.day))
        settings=settings|{"age":age}
        maximum=settings.get("hr_max")
        max_source=("noop_default" if profile_provenance.get("hr_max")=="default" else "manual") if finite(maximum) else "unknown"
        if not finite(maximum) and finite(age) and age>0:
            maximum,max_source=208-.7*age,"tanaka"
        if len(hr)>=600:
            observed=percentile([h for _,h in hr],.995)
            if not finite(maximum) or observed>maximum and max_source!="manual":
                maximum,max_source=observed,"observed"
        overrides=settings.get("sleep_sessions",[])+[r for r in records if r["kind"]=="sleep"]
        windows=[]
        for s in overrides:
            a=s.get("start",s.get("start_ms",0)/1000)
            b=s.get("end",s.get("end_ms",0)/1000)
            if finite(a) and finite(b) and b>a and midnight<=b<end:
                windows.append(dict(start=a,end=b,source="user_override",id=s.get("id"),stages=s.get("stages"),sleep_kind=s.get("sleep_kind","nap" if s.get("source_sleep_type")=="nap" else "main_sleep" if s.get("source_sleep_type")=="long_sleep" else "unclassified")))
        detection=detect_sleep(hr,gravity,tz,settings.get("_overnight_medians",()),settings.get("wrist_off",()),settings.get("band_sleep_states",())) if not windows else result(windows,source="user_override")
        if not windows and len(gravity)<2 and settings.get("hr_only_sleep_enabled") is True:
            detection=hr_only_sleep(hr)
        if not windows:
            windows=[s for s in (detection["value"] or []) if midnight<=s["end"]<end]
        sessions=[]
        for s in windows:
            a,b=s["start"],s["end"]
            if s.get('stages'):
                stages=result(s['stages'],source='user_override')
            elif settings.get('experimental_sleep_v2_enabled',True):
                stages=stage_sleep(a,b,hr,gravity,rr)
            else:
                from sleep_stager_v1 import stage_sleep as classic_stages
                respiration_samples=[]
                for sensor in sensors:
                    values=sensor.get('values',{});original=values.get('sensor_values',{})
                    # V1 consumes a sampled raw respiratory waveform, never a rate summary.
                    # WHOOP4's provisional resp_rate_raw field is not a validated waveform.
                    if values.get('sensor_kind')=='respSample' and finite(original.get('raw')):
                        respiration_samples.append((sensor['timestamp_ms']/1000,original['raw']))
                stages=classic_stages(a,b,hr,gravity,rr,respiration_samples)
            if stages["value"] is None:
                sessions.append(s|{"staging":stages,"total_sleep_min":None})
                continue
            from sleep_refinement import apply as refine_wake
            device_steps=[]
            for sensor in sensors:
                values=sensor.get('values',{});original=values.get('sensor_values',{})
                if values.get('sensor_kind')=='stepSample':
                    device_steps.append(dict(ts=sensor['timestamp_ms']/1000,counter=original.get('counter'),activity_class=original.get('activityClass')))
            refinement=refine_wake(stages['value'],gravity,device_steps,settings.get('motion_aware_wake_enabled',False))
            stages['motion_refinement']={k:v for k,v in refinement.items() if k!='value'}
            stages['value']=refinement['value']
            totals=defaultdict(float)
            for seg in stages["value"]:
                totals[seg["stage"]]+=max(0,min(b,seg["end"])-max(a,seg["start"]))
            tst=sum(v for k,v in totals.items() if k not in ("wake","awake"))
            window_rr=[(t,v) for t,v in rr if a<=t<=b]
            raw_hrv=hrv([v for t,v in window_rr],[t for t,v in window_rr])
            hrv_bins=hrv_windows(a,b,window_rr,stages["value"])
            window_values=[w["rmssd_ms"] for w in hrv_bins if finite(w["rmssd_ms"])]
            if raw_hrv["coverage"].get("beat_coverage",0)>1.10:window_values=[]
            raw_hrv["whole_window_rmssd_ms"]=raw_hrv["rmssd_ms"]
            raw_hrv["value"]=raw_hrv["rmssd_ms"]=stats.mean(window_values) if window_values else None
            sessions.append(s|dict(staging=stages,stage_seconds=dict(totals),total_sleep_min=tst/60,
                                   efficiency=tst/(b-a),resting_hr=session_resting_hr(a,b,hr),hrv=raw_hrv,hrv_windows=hrv_bins,respiration=respiration(window_rr)))
        main_indices=main_night_indices(sessions,int(dt.datetime.fromtimestamp(midnight,tz).utcoffset().total_seconds()),settings.get("habitual_midsleep_seconds"))
        main_pool=[sessions[i] for i in main_indices]
        main=max(main_pool,key=lambda s:s.get("total_sleep_min") or 0) if main_pool else None
        naps=[s for i,s in enumerate(sessions) if i not in main_indices]
        for i,session in enumerate(sessions):
            session["is_nap"]=session.get("sleep_kind")=="nap" or i not in main_indices
        rhr=main.get("resting_hr") if main else None
        day_hr=[(t,h) for t,h in hr if midnight<=t<end]
        rest_hr=settings.get("hr_rest") if finite(settings.get("hr_rest")) else rhr
        if not finite(rest_hr) and day_hr:
            rest_hr=sorted(h for _,h in day_hr)[max(0,math.ceil(.10*len(day_hr))-1)]
            rest_source="day_10th_percentile"
        else:
            rest_source=("noop_default" if profile_provenance.get("hr_rest")=="default" else "manual") if finite(settings.get("hr_rest")) else "sleep_5min_floor"
        e=effort(day_hr,maximum,rest_hr,settings.get("effort_method","edwards"),settings.get("sex"))
        day_grav=[g for g in gravity if midnight<=g[0]<end]
        detected_workouts=workouts(day_hr,day_grav,rest_hr,maximum,settings)
        if detected_workouts["value"] is not None:detected_workouts["value"]=self._visible_detected(detected_workouts["value"],records)
        manual_workouts=[]
        tagged=[]
        for record in settings.get("workouts",[])+[r for r in records if r["kind"]=="workout"]:
            if record.get("device") not in (None,"",device) and record.get("source_category")!="imported":continue
            a=record.get("start",record.get("start_ms",0)/1000)
            b=record.get("end",record.get("end_ms",0)/1000)
            if not finite(a) or not finite(b) or b<a or not midnight<=a<end:
                continue
            sport=record.get("sport",record.get("activity",record.get("name")))
            if sport:
                tagged.append(str(sport))
            window=[(t,h) for t,h in hr if a<=t<=b]
            computed=effort(window,maximum,rest_hr,settings.get("effort_method","edwards"),settings.get("sex"))
            manual_workouts.append(self._saved_workout(record,hr,settings,maximum,rest_hr))
        if manual_workouts:
            detected=[s for s in (detected_workouts["value"] or []) if not any(s["start"]<=m["end"] and m["start"]<=s["end"] for m in manual_workouts)]
            detected_workouts["value"]=sorted(detected+manual_workouts,key=lambda s:s["start"])
            detected_workouts["reason"]=None
        # Learn WHOOP4 raw offset per device, not from another strap's 826 ADC.
        raw_temperatures=[s["values"].get("skin_temp_raw") for s in sensors if s.get("device")==device]
        temp_anchor=settings.get("_skin_temp_anchor") or skin_temp_anchor(raw_temperatures)
        temperatures=[]
        for sensor in sensors:
            if not main or not main["start"]<=sensor["timestamp_ms"]/1000<=main["end"]:
                continue
            raw=sensor["values"].get("skin_temp_raw")
            value=33+(raw-temp_anchor["value"])*.05 if finite(raw) and 550<=raw<=2040 else sensor["values"].get("skin_temp_c") if not finite(raw) else None
            if finite(value) and 28<=value<=42:
                temperatures.append(value)
        h=daily_hrv(sessions,settings.get("hrv_window","whole"),main_night_indices(sessions,int(dt.datetime.fromtimestamp(midnight,tz).utcoffset().total_seconds()),settings.get("habitual_midsleep_seconds")),rr)
        resp=main.get("respiration",result(reason="No scorable sleep window")) if main else result(reason="No scorable sleep window")
        need=settings.get("sleep_goal_hours") or sleep_need([],age)
        rv=None
        if main and finite(main.get("total_sleep_min")):
            totals=main["stage_seconds"]
            rv=rest(main["total_sleep_min"]*60,main["end"]-main["start"],main["efficiency"],totals.get("deep",0)+totals.get("rem",0),need,deep_seconds=totals.get("deep"))
        output=dict(day=day,device=device,timezone=name,provenance={"reference_commit":REFERENCE,"hr_max_source":max_source,"hr_rest_source":rest_source,"hr_max_bpm":maximum,"effort_resting_hr_bpm":rest_hr,"age":age},
                    coverage={"hr_samples":len(day_hr),"rr_intervals":len(rr),"gravity_samples":len(day_grav),"sensor_samples":len(sensors),"read_start":read_start,"day_start":midnight,"day_end":end,
                              "sources":sorted(set(r.get("source","owned_strap") for r in rows)),"arbitration":"Owned strap samples win overlapping seconds; imports fill gaps; RR sources never mix within a second"},
                    hrv=h,respiration=resp,resting_hr=result(rhr,None if rhr is not None else "No scorable sleep window"),
                    effort=e,rest=result(rv,None if rv is not None else "Rest requires a staged sleep window",source="AnalyticsEngine.Rest"),
                    sleep=result(sessions,None if sessions else detection.get("reason") or "No sleep sessions cleared the detector gates",detection=detection,main=main,main_indices=main_indices,main_sessions=main_pool,naps=naps,need_hours=need),
                    workouts=detected_workouts,activity_tags=sorted(set(tagged)),zones=hr_zones(day_hr,maximum,settings.get("hr_zone_thresholds")),
                    stress=stress_index([v for t,v in rr if midnight<=t<end]),
                    skin_temperature=result(stats.mean(temperatures) if temperatures else None,None if temperatures else "No contacted skin-temperature samples in the main sleep window",unit="°C",calibration="NOOP provisional per-device ADC calibration",anchor=temp_anchor),
                    charge=result(reason="Charge requires preceding nightly baseline history"))
        output["effort"]["display_value"]=output["effort"]["value"]*.21 if settings["effort_scale"]=="whoop" and finite(output["effort"]["value"]) else output["effort"]["value"]
        output["effort"].update(display_max=21 if settings["effort_scale"]=="whoop" else 100,display_scale=settings["effort_scale"])
        output["settings"]= {k:settings.get(k) for k in ("hrv_window","effort_method","effort_scale","hr_zone_thresholds","day_cycle_mode","charge_baseline_since_ms","hr_only_sleep_enabled","experimental_sleep_v2_enabled","motion_aware_wake_enabled")}
        imported=[r for r in records if r["kind"] in ("daily_metric","sleep") and (r.get("day")==day or r.get("date")==day)]
        # Imported summary metrics retain their measurement identity. Never turn
        # an SDNN summary into RMSSD or Oura readiness into NOOP Charge.
        from fusion import hrv_identity
        for metric,key in [("resting_hr","resting_hr"),("hrv","hrv_ms"),("respiration","respiratory_rate")]:
            if output[metric]["value"] is None:
                observations=[r for r in imported if finite(r.get(key)) and (metric!="hrv" or hrv_identity(r)!="SDNN")]
                if observations:
                    record=observations[-1]
                    output[metric]=result(record[key],source=record.get("source","imported"),source_category="imported_summary",record_id=record["id"],unit={"resting_hr":"bpm","hrv":"ms","respiration":"breaths/min"}[metric])
                    if metric=="hrv":output[metric]["hrv_method"]="RMSSD"
        sdnn=[r for r in imported if finite(r.get("hrv_ms")) and hrv_identity(r)=="SDNN"]
        if sdnn:
            record=sdnn[-1]
            output["hrv_sdnn"]=result(record["hrv_ms"],source=record.get("source","imported"),source_category="imported_summary",record_id=record["id"],hrv_method="SDNN",unit="ms")
        imported_sleep=[r for r in imported if finite(r.get("total_sleep_min")) and r["total_sleep_min"]>0]
        if imported_sleep and not main:
            record=max(imported_sleep,key=lambda r:(r.get("source_sleep_type")=="long_sleep",r["total_sleep_min"]))
            output["sleep"].update(total_sleep_min=record["total_sleep_min"],source=record.get("source","imported"),imported_summary=record)
            if finite(record.get("efficiency")) and finite(record.get("deep_min")) and finite(record.get("rem_min")):
                output["rest"]=result(rest(record["total_sleep_min"]*60,record["total_sleep_min"]*60/max(record["efficiency"],.01),record["efficiency"],(record["deep_min"]+record["rem_min"])*60,need,deep_seconds=record["deep_min"]*60),source="NOOP Rest from imported stage totals")
        output["source_records"]=[r for r in records if r.get("day")==day or r.get("date")==day or finite(r.get("timestamp_ms",r.get("start_ms"))) and midnight*1000<=r.get("timestamp_ms",r.get("start_ms"))<end*1000]
        spot_rr=[(t,v) for t,v in rr if end-300<=t<end]
        # Spot uses only the last captured five-minute window; timestamp integrity
        # gates prevent duplicate banked RR from acquiring a frequency spectrum.
        if rr:
            last=max(t for t,_ in rr);spot_rr=[(t,v) for t,v in rr if last-300<=t<=last]
        spot=hrv([v for _,v in spot_rr],[t for t,_ in spot_rr],max_rejected_fraction=.35)
        output["hrv_spot"]=spot
        output["hrv_frequency"]=frequency_hrv([v for _,v in spot_rr]) if spot["value"] is not None else result(reason=spot["reason"],coverage=spot["coverage"])
        motion=sum(math.dist(a[1:],b[1:]) for a,b in zip(day_grav,day_grav[1:]))
        actual_steps=[v["values"].get("steps") for v in sensors if midnight*1000<=v["timestamp_ms"]<end*1000 and finite(v["values"].get("steps"))]
        summary_steps=[r["steps"] for r in imported if finite(r.get("steps"))]
        hourly=defaultdict(list)
        for t,h in day_hr:hourly[dt.datetime.fromtimestamp(t,tz).hour].append(h)
        output["hourly_hr"]={str(hour):stats.mean(v) for hour,v in hourly.items()}
        output["motion_intensity"]= result(motion if len(day_grav)>1 else None,source="StepsEstimateEngine.swift gravity delta sum",coverage=dict(gravity_samples=len(day_grav)))
        output["steps"]=result(max(summary_steps) if summary_steps else sum(actual_steps),source="imported_reference",estimated=False) if summary_steps or actual_steps else steps_estimate(motion,manual=manual_steps_coefficient(settings))
        daily_records=output["source_records"]
        fluids=[r.get("amount_ml",r.get("volume_ml",r.get("ml"))) for r in daily_records if r["kind"]=="hydration"]
        fluids=[v for v in fluids if finite(v) and v>=0];goal=hydration_goal(settings.get("sex"),e["value"])
        output["hydration"]=result(sum(fluids) if fluids else None,None if fluids else "No quantified hydration entries",goal_ml=goal,progress=clamp(sum(fluids)/goal) if fluids else None,source="HydrationGoal.swift",goal_estimated=True)
        foods=[r for r in daily_records if r["kind"]=="nutrition"]
        nutrients={k:sum(r[k] for r in foods if finite(r.get(k))) if any(finite(r.get(k)) for r in foods) else None for k in ("calories","protein_g","carbs_g","fat_g")}
        output["nutrition"]=result(nutrients if foods else None,None if foods else "No nutrition entries",coverage=dict(entries=len(foods)),source="logged nutrition totals")
        output["lifting"]=lift_metrics([r for r in daily_records if r["kind"]=="lifting_set"])
        output["calories"]=day_energy(day_hr,settings,maximum,rest_hr)
        output["blood_oxygen"]=blood_oxygen([s for s in sensors if midnight*1000<=s["timestamp_ms"]<end*1000],daily_records)
        progression=lift_session_progression(records)
        day_ids={r["id"] for r in daily_records if r["kind"]=="lifting_set"}
        output["lifting"]["sessions"]=result([session for session in progression["value"] if set(session["record_ids"]) & day_ids],progression["reason"],progression["coverage"],source=progression["source"])
        output["lifting"]["programs"]=[r for r in records if r["kind"]=="lifting_program"]
        output["lifting"]["session_loads"]=[dict(record_id=r["id"],**lift_session_load(r.get("rpe",r.get("session_rpe")),r.get("duration_seconds",(r["end_ms"]-r["start_ms"])/1000 if finite(r.get("end_ms")) and finite(r.get("start_ms")) else None))) for r in daily_records if r["kind"]=="workout" and (r.get("rpe") is not None or r.get("session_rpe") is not None)]
        output["cycle"]= dict(logs=[r for r in daily_records if r["kind"]=="cycle"],phase=result(reason="Cycle phase is provided by insights.InsightsService"),integration="insights.InsightsService")
        if len(self._cache)>=366:
            self._cache.pop(next(iter(self._cache)))
        self._cache[signature]=copy.deepcopy(output)
        return output

    def _apply_day_cycles(self,device,entries,settings,tz,target):
        """DayCycleIntelligenceIntegration load attribution; calendar physiology stays on wake date."""
        now_dt=dt.datetime.now(tz);now=now_dt.timestamp() if target==now_dt.date() else dt.datetime.combine(target+dt.timedelta(days=1),dt.time.min,tzinfo=tz).timestamp()
        offset=int(dt.datetime.fromtimestamp(now,tz).utcoffset().total_seconds());boundaries=[]
        for entry in entries:
            for name in ("effort","zones","steps","motion_intensity","workouts","calories"):
                entry.setdefault("calendar_metrics",{})[name]=copy.deepcopy(entry[name])
            midnight=entry["coverage"]["day_start"];end=min(entry["coverage"]["day_end"],now)
            entry["day_cycle"]=dict(start=midnight,end=max(midnight,end),day=entry["day"],source="calendar",mode=settings["day_cycle_mode"])
            if settings["day_cycle_mode"]!="sleep_onset":continue
            sessions=entry["sleep"]["value"] or [];indices=main_night_indices(sessions,offset,settings.get("habitual_midsleep_seconds"),cycle_gate=True)
            if indices:
                onset=min(sessions[i]["start"] for i in indices)
                if onset<=now:boundaries.append(dict(id="sleep:"+entry["day"]+":"+str(onset),start=onset,day=entry["day"],source="edited_sleep" if any(sessions[i].get("source")=="user_override" for i in indices) else "detected_sleep"))
        latest=max(boundaries,key=lambda b:b["start"]) if boundaries else None
        active=active_day_cycle(settings["day_cycle_mode"],latest,now,offset)
        if latest and active["source"]=="synthetic_midnight":boundaries.append(dict(active,id="synthetic:"+active["day"]))
        windows=physiological_cycle_windows(boundaries,now) if settings["day_cycle_mode"]=="sleep_onset" else []
        if not windows:return active
        by_day={entry["day"]:entry for entry in entries};first=min(b["day"] for b in boundaries)
        for entry in entries:
            if entry["day"]>=first:
                entry["day_cycle"]=dict(start=None,end=None,day=entry["day"],source="no_boundary",mode="sleep_onset")
                for name in ("effort","zones","steps","motion_intensity","calories"):entry[name]=result(reason="No main-sleep onset boundary attributed to this wake date")
                entry["workouts"]=result(reason="No main-sleep onset boundary attributed to this wake date")
                entry["activity_tags"]=[]
        for window in windows:
            entry=by_day.get(window["day"])
            if not entry:continue
            start,end=window["start"],window["end"];r,sensors,records=self._read(device,start,end);hr,rr,gravity=self._streams(r,sensors)
            profile=profile_for_day(settings,entry["day"]);max_hr=entry["provenance"].get("hr_max_bpm");rhr=entry["resting_hr"]["value"] or entry["provenance"].get("effort_resting_hr_bpm")
            entry["day_cycle"]=dict(window,mode="sleep_onset",end_exclusive=True)
            entry["effort"]=effort(hr,max_hr,rhr,settings["effort_method"],settings.get("sex"))
            entry["effort"].update(display_scale=settings["effort_scale"],display_max=21 if settings["effort_scale"]=="whoop" else 100,display_value=entry["effort"]["value"]*.21 if settings["effort_scale"]=="whoop" and finite(entry["effort"]["value"]) else entry["effort"]["value"])
            entry["zones"]=hr_zones(hr,max_hr,settings["hr_zone_thresholds"])
            entry["calories"]=day_energy(hr,profile,max_hr,rhr)
            motion=sum(math.dist(a[1:],b[1:]) for a,b in zip(gravity,gravity[1:]));entry["motion_intensity"]=result(motion if len(gravity)>1 else None,coverage=dict(gravity_samples=len(gravity)),source="StepsEstimateEngine.swift cycle gravity delta sum")
            measured=[v["values"].get("steps") for v in sensors if finite(v["values"].get("steps"))]
            if measured:entry["steps"]=result(sum(measured),source="Timestamped imported step counts within cycle",estimated=False)
            else:
                points=[(e["calendar_metrics"]["motion_intensity"]["value"],e["calendar_metrics"]["steps"]["value"]) for e in entries if not e["calendar_metrics"]["steps"].get("estimated")]
                entry["steps"]=steps_estimate(motion,points,manual_steps_coefficient(settings))
            detected=workouts(hr,gravity,rhr,max_hr,profile)
            if detected["value"] is not None:detected["value"]=self._visible_detected(detected["value"],records)
            actual=[r for r in records if r["kind"]=="workout" and finite(r.get("start_ms")) and start*1000<=r["start_ms"]<end*1000 and (r.get("device") in (None,"",device) or r.get("source_category")=="imported")]
            manual=[]
            for record in actual:
                a=record["start_ms"]/1000;b=record.get("end_ms")
                if not finite(b) or b<=record["start_ms"]:continue
                b/=1000;sample=[(t,h) for t,h in hr if a<=t<=b]
                manual.append(self._saved_workout(record,hr,profile,max_hr,rhr))
            detected["value"]=[w for w in (detected["value"] or []) if not any(w["start"]<m["end"] and m["start"]<w["end"] for m in manual)]+manual
            entry["workouts"]=detected;entry["activity_tags"]=sorted({str(m["sport"]) for m in manual if m["sport"]})
        return active

    def day(self,device,date=None,settings=None):
        return self.day_and_history(device,date,settings)[0]

    def day_and_history(self,device,date=None,settings=None):
        settings=analytics_settings(settings)
        _,tz=self._timezone(settings)
        key=date or dt.datetime.now(tz).date().isoformat()
        bundle=self.trends(device,30,settings,through=key)
        selected=bundle["days"][-1]
        if date is None and settings["day_cycle_mode"]=="sleep_onset":selected=next((e for e in bundle["days"] if e["day"]==bundle["active_day_cycle"]["day"]),selected)
        selected["active_day_cycle"]=bundle["active_day_cycle"]
        return selected,bundle["days"]

    def trends(self,device,days=30,settings=None,through=None):
        """Daily aggregates refresh at most every30s; edits and completed sync invalidate.

        Live HR and the live chart have their independent 2s/4s refresh. Returning
        copies keeps source fusion/health enrichment out of shared cached values.
        A lock coalesces simultaneous cold requests from multiple dashboard views.
        """
        settings=analytics_settings(settings)
        _,tz=self._timezone(settings)
        target=through or dt.datetime.now(tz).date().isoformat()
        count=min(max(int(days),1),366)
        generations=[]
        with closing(sqlite3.connect(self.store.path.as_uri()+'?mode=ro',uri=True)) as conn:
            tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table,key in [('feature_records','updated_ms'),('feature_revisions','revision'),('chunks','id'),('workout_dismissals','rowid')]:
                if table not in tables:
                    generations.append(None)
                elif table=='workout_dismissals':
                    # Undo deletes these small control rows; SQLite may reuse a
                    # rowid on the next dismissal, so count/max is insufficient.
                    tokens=conn.execute('SELECT device,start_ms,end_ms,operation_id FROM workout_dismissals ORDER BY device,start_ms,end_ms,operation_id').fetchall()
                    generations.append(hashlib.blake2b(repr(tokens).encode(),digest_size=16).digest())
                elif key=='rowid' or key in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
                    generations.append(conn.execute(f'SELECT COUNT(*),MAX({key}) FROM {table}').fetchone())
                else:
                    # Read-only adapters can also receive an older minimal import schema.
                    generations.append(hashlib.blake2b(repr(conn.execute(f'SELECT * FROM {table}').fetchall()).encode(),digest_size=16).digest())
        stamp=hashlib.blake2b(json.dumps(settings,sort_keys=True).encode(),digest_size=16).digest()
        key=(device,target,count,stamp,int(time.time()//30),tuple(generations))
        with self._trends_lock:
            if key not in self._trends_cache:
                value=self._trends_uncached(device,count,settings,target)
                if len(self._trends_cache)>=12:self._trends_cache.pop(next(iter(self._trends_cache)))
                self._trends_cache[key]=value
            return copy.deepcopy(self._trends_cache[key])

    def _trends_uncached(self,device,days=30,settings=None,through=None):
        settings=analytics_settings(settings)
        _,tz=self._timezone(settings)
        target=dt.date.fromisoformat(through) if through else dt.datetime.now(tz).date()
        count=min(max(int(days),1),366)
        # One scan-window anchor for ALL nights: a common offset cancels in the
        # personal deviation. Independently re-anchoring each night would erase it.
        end_time=dt.datetime.combine(target+dt.timedelta(days=1),dt.time.min,tzinfo=tz).timestamp()
        conn=sqlite3.connect(self.store.path.as_uri()+"?mode=ro",uri=True)
        try:
            exists=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sensors'").fetchone()
            raws=[]
            if exists:
                for (encoded,) in conn.execute("SELECT values_json FROM sensors WHERE device=? AND timestamp_ms>=? AND timestamp_ms<?",(device,int((end_time-count*86400)*1000),int(end_time*1000))):
                    value=json.loads(encoded).get("skin_temp_raw")
                    if finite(value):
                        raws.append(value)
            records_table=conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='feature_records'").fetchone()
            if records_table:
                for (encoded,) in conn.execute("SELECT payload_json FROM feature_records WHERE kind='health_sample' AND deleted_ms IS NULL"):
                    payload=json.loads(encoded);original=payload.get("original",{})
                    if payload.get("source_table")=="skinTempSample" and payload.get("device",original.get("deviceId")) in (None,"",device):
                        timestamp=payload.get("timestamp_ms",original.get("ts",0)*1000)
                        raw=original.get("raw")
                        if finite(timestamp) and (end_time-count*86400)*1000<=timestamp<end_time*1000 and finite(raw):raws.append(raw)
            settings=settings|{"_skin_temp_anchor":skin_temp_anchor(raws)}
        finally:
            conn.close()
        # Only captured days need heavy staging; missing calendar days remain explicit.
        captured=[]
        for i in range(count-1,-1,-1):
            key=(target-dt.timedelta(days=i)).isoformat()
            historical_medians=[e["sleep"]["main"].get("overnight_hr_median") for e in captured if e["sleep"]["main"] and finite(e["sleep"]["main"].get("overnight_hr_median"))]
            captured.append(self._calculate(device,key,settings|{"_overnight_medians":historical_medians[-14:]}))
        active_cycle=self._apply_day_cycles(device,captured,settings,tz,target)
        entries=[];rows=[]
        for entry in captured:
            key=entry["day"]
            main=entry["sleep"]["main"]
            nightly_hours=[r["sleep_min"]/60 for r in rows[-28:] if finite(r.get("sleep_min"))]
            sleep_minutes=main.get("total_sleep_min") if main else entry["sleep"].get("total_sleep_min")
            if finite(sleep_minutes):
                nightly_hours.append(sleep_minutes/60)
                profile_age=settings.get("age")
                if (not finite(profile_age) or profile_age<=0) and settings.get("birth_date",settings.get("dob")):
                    birth=dt.date.fromisoformat(settings.get("birth_date",settings.get("dob")));date_value=dt.date.fromisoformat(key)
                    profile_age=date_value.year-birth.year-((date_value.month,date_value.day)<(birth.month,birth.day))
                need=settings.get("sleep_goal_hours") or sleep_need(nightly_hours,profile_age)
                consistency=sleep_consistency(nightly_hours)
                entry["sleep"]["need_hours"]=need
                entry["sleep"]["consistency"]=result(consistency,None if consistency is not None else "Requires 3 observed sleep nights",source="VitalityEngine.swift duration coefficient of variation")
                if main:
                    totals=main.get("stage_seconds",{})
                    entry["rest"]["value"]=rest(main["total_sleep_min"]*60,main["end"]-main["start"],main["efficiency"],totals.get("deep",0)+totals.get("rem",0),need,consistency,totals.get("deep"))
                elif entry["rest"]["value"] is not None:
                    imported=entry["sleep"]["imported_summary"]
                    entry["rest"]["value"]=rest(sleep_minutes*60,sleep_minutes*60/max(imported["efficiency"],.01),imported["efficiency"],(imported["deep_min"]+imported["rem_min"])*60,need,consistency,imported["deep_min"]*60)
            row=dict(day=key,hrv=entry["hrv"]["value"],resting_hr=entry["resting_hr"]["value"],
                     resp=entry["respiration"]["value"],effort=entry["effort"]["value"],sleep_min=main.get("total_sleep_min") if main else entry["sleep"].get("total_sleep_min"),
                     skin_temp=entry["skin_temperature"]["value"])
            bs={metric:baseline([r.get(column) for r in rows],metric,day_keys=[r["day"] for r in rows],since_ms=settings.get("charge_baseline_since_ms",0) if metric!="strain" else 0) for metric,column in [("hrv","hrv"),("resting_hr","resting_hr"),("resp","resp"),("strain","effort"),("skin_temp","skin_temp")]}
            dev=row["skin_temp"]-bs["skin_temp"]["mean"] if row["skin_temp"] is not None and bs["skin_temp"]["usable"] else None
            entry["charge"]=charge(row["hrv"],row["resting_hr"],bs["hrv"],bs["resting_hr"],row["resp"],bs["resp"],
                                   entry["rest"]["value"]/100 if entry["rest"]["value"] is not None else None,dev,
                                   prior_effort=rows[-1]["effort"] if rows else None,effort_baseline=bs["strain"])
            row["charge"]=entry["charge"]["value"]
            rows.append(row)
            entry["baselines"]=bs
            slept=row["sleep_min"]
            nap=sum(s.get("total_sleep_min") or 0 for s in entry["sleep"]["naps"])
            fragments=sum(s.get("total_sleep_min") or 0 for s in entry["sleep"].get("main_sessions",[]) if s is not main)
            credited_total=slept+fragments+nap if slept is not None else None
            credited=[(r["day"],r.get("credited_sleep_min")) for r in rows[:-1]]+[(key,credited_total)]
            row["credited_sleep_min"]=credited_total
            entry["sleep"]["debt"]=sleep_debt(credited,entry["sleep"]["need_hours"])
            entry["readiness"]=readiness(rows)
            if entry["steps"].get("estimated"):
                points=[(past["motion_intensity"]["value"],past["steps"]["value"]) for past in entries if not past["steps"].get("estimated")]
                entry["steps"]=steps_estimate(entry["motion_intensity"]["value"],points,manual_steps_coefficient(settings))
            week=rows[-7:]
            age=settings.get("age")
            if (not finite(age) or age<=0) and settings.get("birth_date"):
                birth=dt.date.fromisoformat(settings["birth_date"]);date_value=dt.date.fromisoformat(key)
                age=date_value.year-birth.year-((date_value.month,date_value.day)<(birth.month,birth.day))
            nightly_rhr=[r["resting_hr"] for r in week if finite(r["resting_hr"])]
            entry["fitness_age"]=fitness_age(age,settings.get("sex"),stats.mean(nightly_rhr) if len(nightly_rhr)>=4 else None,[r["effort"] for r in week],settings.get("waist_cm"))
            entry["vitality"]=vitality(age,rhr=row["resting_hr"],sleep_hours=slept/60 if finite(slept) else None,consistency=entry["sleep"].get("consistency",{}).get("value"),rmssd=row["hrv"],steps=entry["steps"]["value"],vo2=settings.get("vo2max"),expected_vo2=settings.get("expected_vo2max"))
            zs={}
            for metric,column,sign in [("resting_hr","resting_hr",1),("hrv","hrv",-1),("resp","resp",1),("skin_temp","skin_temp",1)]:
                base=bs[metric]
                if base["usable"] and finite(row[column]):zs[metric]=sign*(row[column]-base["mean"])/(1.253*base["spread"])
            entry["illness_signal"]=illness_signal(zs,all(bs[m]["status"]=="trusted" for m in zs) and len(zs)>=2,settings.get("illness_confounders",()),settings.get("already_unwell",False))
            entry["sleep"]["forecast"]=recovery_forecast([r["charge"] for r in rows],[r["effort"] for r in rows[:-1]],row["effort"],settings.get("planned_sleep_hours"),entry["sleep"]["need_hours"],len([r for r in rows if finite(r["sleep_min"])]))
            hour_history=defaultdict(list)
            for past in entries+[entry]:
                for hour,value in past["hourly_hr"].items():hour_history[int(hour)].append(value)
            entry["circadian"]=circadian_phase([(h,stats.mean(v)) for h,v in hour_history.items()],sum(bool(past["hourly_hr"]) for past in entries+[entry]),settings.get("habitual_wake_hour"))
            entries.append(entry)
        weekly_lift_sets=[r for entry in entries[-7:] for r in entry["source_records"] if r["kind"]=="lifting_set"]
        weekly_lifting=dict(muscle_counts=lift_muscle_counts(weekly_lift_sets),rpe_profile=lift_rpe_profile(weekly_lift_sets),source="LiftMetrics.swift observed trailing7calendar days")
        hour_bins=defaultdict(list)
        for entry in entries:
            for hour,value in entry["hourly_hr"].items():hour_bins[int(hour)].append(value)
        circadian=circadian_phase([(h,stats.mean(v)) for h,v in hour_bins.items()],sum(bool(e["hourly_hr"]) for e in entries),settings.get("habitual_wake_hour"))
        entries[-1]["circadian"]=circadian
        correlations=[]
        for x,y,lag in [("effort","charge",1),("sleep_min","charge",0),("sleep_min","hrv",0),("effort","hrv",1)]:
            by={r["day"]:r for r in rows}
            pairs=[]
            for r in rows:
                after=by.get((dt.date.fromisoformat(r["day"])+dt.timedelta(days=lag)).isoformat(),{})
                if finite(r.get(x)) and finite(after.get(y)):
                    pairs.append((r[x],after[y]))
            correlations.append(dict(x=x,y=y,lag_days=lag,n=len(pairs))|correlation(pairs))
        tagged=defaultdict(set)
        for entry in entries:
            for sport in entry["activity_tags"]:
                tagged[sport].add(entry["day"])
        recoveries={r["day"]:r["charge"] for r in rows if finite(r["charge"])}
        return dict(device=device,through=target.isoformat(),days=entries,training_load=training_load([(r["day"],r["effort"]) for r in rows]),
                    baselines=entries[-1]["baselines"],readiness=readiness(rows),correlations=correlations,
                    activity_cost=activity_cost(tagged,recoveries),circadian=circadian,lifting_week=weekly_lifting,active_day_cycle=active_cycle,
                    provenance={"reference_commit":REFERENCE,"local_only":True})

    def insights(self,device,days=30,settings=None,through=None):
        """Public observed-data analysis. Cohort associations do not establish causation.

        Additional source algorithms are integrated by the separate InsightsService.
        This helper is the basic explicit-control association view.
        """
        bundle=self.trends(device,days,settings,through);entries=bundle["days"]
        behaviors=defaultdict(dict)
        for e in entries:
            for r in e["source_records"]:
                if r["kind"] not in ("journal","habit"):continue
                answer=r.get("answeredYes",r.get("answered_yes",r.get("answer")))
                if isinstance(answer,bool):behaviors[r.get("question",r.get("name","Unspecified"))][e["day"]]=answer
        effects=[]
        for behavior,answers in behaviors.items():
            for metric in ("charge","hrv","resting_hr","effort"):
                yes=[e[metric]["value"] for e in entries if answers.get(e["day"]) is True]
                no=[e[metric]["value"] for e in entries if answers.get(e["day"]) is False]
                effect=behavior_effect(yes,no)
                if effect["value"] is not None:effects.append(dict(behavior=behavior,outcome=metric,**effect))
        effects.sort(key=lambda r:(not r["significant"],-abs(r["cohens_d"]),r["behavior"]))
        return dict(behavior_effects=effects,correlations=bundle["correlations"],activity_cost=bundle["activity_cost"],integration="Advanced NOOP dose-response, comparisons, cycle, planner and digest are provided by insights.InsightsService")

    def report(self,device,days=7,settings=None,through=None):
        """JSON weekly digest of actual observations, no missing-day zero imputation."""
        bundle=self.trends(device,days,settings,through);entries=bundle["days"]
        summaries={}
        for key in ("charge","hrv","resting_hr","effort","rest","steps"):
            values=[e[key]["value"] for e in entries if finite(e[key]["value"])]
            summaries[key]=result(stats.mean(values) if values else None,None if values else "No observed values in this window",coverage=dict(observed_days=len(values),calendar_days=len(entries)),minimum=min(values) if values else None,maximum=max(values) if values else None)
        return dict(device=device,through=bundle["through"],summaries=summaries,workouts=sum(len(e["workouts"]["value"] or []) for e in entries),training_load=bundle["training_load"],insights=self.insights(device,days,settings,through),source="BOOP observed weekly digest",integration="Advanced NOOP digest is provided by insights.InsightsService")
