"""NOOP legacy V1 staging, pure and independent of V2.

Port of SleepStager.swift at 7f396e98ed9d259df08e3a0a58cfac05fc70615c.
PolyForm Noncommercial 1.0.0; Copyright 2026 NoopApp.
Inputs: HR/RR/raw respiration pairs (unix seconds, value), gravity (seconds,x,y,z).
Missing physiology remains unmeasured; stage estimates are not clinical labels.
"""
import math
import statistics as stats

NAN = float("nan")
SOURCE = "SleepStager.swift V1"
CK_WEIGHTS = (106.,54.,58.,76.,230.,74.,67.)


def percentile(values, pct):
    values = sorted(v for v in values if math.isfinite(v))
    if not values:
        return None
    p = (pct/100.)*(len(values)-1)
    lo, hi = int(p), min(int(p)+1,len(values)-1)
    return values[lo] + (values[hi]-values[lo])*(p-lo)


def standard_deviation(values):
    if not values:
        return 0.
    mean = sum(values)/len(values)
    return math.sqrt(sum((v-mean)**2 for v in values)/len(values))


def rescale_counts(counts):
    return [min(v/100.,300.) for v in counts]


def cole_kripke(counts):
    return [sum(w*counts[j] for k,w in enumerate(CK_WEIGHTS)
                if 0 <= (j := i-4+k) < len(counts))*.001 < 1. for i in range(len(counts))]


def onset_and_final_wake(flags):
    run, onset = 0, 0
    for i, asleep in enumerate(flags):
        run = run+1 if asleep else 0
        if run >= 3:
            onset = i-2
            break
    final = next((i for i in range(len(flags)-1,-1,-1) if flags[i]),len(flags)-1)
    return onset, final if final >= onset else len(flags)-1


def gaussian_kernel(sigma_seconds):
    sigma = max(sigma_seconds/30.,1e-6)
    radius = max(1,math.ceil(3*sigma))
    kernel = [math.exp(-.5*(x/sigma)**2) for x in range(-radius,radius+1)]
    total = sum(kernel)
    return [v/total for v in kernel]


def convolve_reflect(values,kernel):
    radius = len(kernel)//2
    if radius == 0 or len(values) <= radius:
        return list(values)
    padded = list(reversed(values[1:radius+1])) + list(values) + list(reversed(values[-radius-1:-1]))
    return [sum(padded[i+j]*kernel[-1-j] for j in range(len(kernel))) for i in range(len(values))]


def dog_hr_variability(values):
    known = [i for i,v in enumerate(values) if not math.isnan(v)]
    if not known:
        return [0.]*len(values)
    filled, right = [], 0
    for i,v in enumerate(values):
        while right < len(known)-1 and known[right] < i:
            right += 1
        if not math.isnan(v):
            filled.append(v)
        elif i <= known[0]:
            filled.append(values[known[0]])
        elif i >= known[-1]:
            filled.append(values[known[-1]])
        else:
            lo,hi = known[right-1],known[right]
            filled.append(values[lo]+(values[hi]-values[lo])*(i-lo)/(hi-lo))
    fast,slow = [convolve_reflect(filled,gaussian_kernel(s)) for s in (120.,600.)]
    return [a-b for a,b in zip(fast,slow)]


def find_peaks(values,distance=2,height=0.):
    candidates, i = [], 1
    while i < len(values)-1:
        if values[i] > values[i-1] and values[i] >= height:
            j = i
            while j+1 < len(values) and values[j+1] == values[i]:
                j += 1
            if j+1 < len(values) and values[j+1] < values[i]:
                candidates.append((i+j)//2)
            i = j+1
        else:
            i += 1
    if distance <= 1:
        return candidates
    keep = set(candidates)
    for p in sorted(candidates,key=lambda p:(-values[p],p)):
        if p in keep:
            keep.difference_update(q for q in candidates if q != p and abs(q-p)<distance)
    return sorted(keep)


def resp_rate_and_rrv(raw,dt=1.):
    if len(raw)<8:
        return NAN,NAN
    mean = sum(raw)/len(raw)
    centered = [v-mean for v in raw]
    if all(abs(v)<1e-12 for v in centered) or standard_deviation(centered)<=0:
        return NAN,NAN
    peaks = find_peaks(centered,max(2,math.floor(2/dt+.5)),0.)
    if len(peaks)<3:
        return NAN,NAN
    intervals = [(b-a)*dt for a,b in zip(peaks,peaks[1:]) if 1.5 <= (b-a)*dt <= 12.]
    if len(intervals)<2:
        return NAN,NAN
    return 60/stats.median(intervals),standard_deviation(intervals)


def build_epoch_grid(start,end,hr,gravity,rr,resp):
    n = max(1,math.ceil((end-start)/30)) if end>start else 0
    edges = [start+i*30 for i in range(n+1)]
    counts,move_n,grav_n = [0.]*n,[0]*n,[0]*n
    hrs,rrs,resps = [[[] for _ in range(n)] for _ in range(3)]
    def index(ts):
        if ts == end and n:
            return n-1
        return min(int((ts-start)/30),n-1) if start<=ts<end else None
    previous = None
    for row in gravity:
        delta = math.sqrt(sum((a-b)**2 for a,b in zip(previous[1:],row[1:]))) if previous is not None else 0.
        previous = row
        i = index(row[0])
        if i is not None:
            counts[i] += delta
            grav_n[i] += 1
            move_n[i] += delta >= .01
    for rows,buckets in ((hr,hrs),(rr,rrs),(resp,resps)):
        for ts,value in rows:
            i = index(ts)
            if i is not None:
                buckets[i].append(float(value))
    return dict(edges=edges,counts=counts,move_fraction=[move_n[i]/grav_n[i] if grav_n[i] else 1. for i in range(n)],
                hr=[sum(v)/len(v) if v else NAN for v in hrs],rr=rrs,resp=resps,
                gravity_covered=sum(v>0 for v in grav_n),hr_covered=sum(bool(v) for v in hrs))


def extract_features(grid,flags,dog,onset,final):
    features = []
    n = len(grid["counts"])
    rescaled = rescale_counts(grid["counts"])
    for i in range(n):
        lo,hi = max(0,i-5),min(n,i+6)
        hrs = [v for v in grid["hr"][lo:hi] if not math.isnan(v)]
        rr = [v for bucket in grid["rr"][lo:hi] for v in bucket if 300<=v<=2000]
        rmssd = math.sqrt(sum((b-a)**2 for a,b in zip(rr,rr[1:]))/(len(rr)-1)) if len(rr)>=5 else NAN
        rr_mean = sum(rr)/len(rr) if rr else NAN
        sdnn = math.sqrt(sum((v-rr_mean)**2 for v in rr)/(len(rr)-1)) if len(rr)>=5 else NAN
        rate,rrv = resp_rate_and_rrv([v for bucket in grid["resp"][lo:hi] for v in bucket])
        features.append(dict(index=i,mid_ts=grid["edges"][i]+15,count=rescaled[i],move_fraction=grid["move_fraction"][i],
                             ck_sleep=flags[i],hr=sum(hrs)/len(hrs) if hrs else NAN,
                             hr_var=standard_deviation(dog[lo:hi]) if hi-lo>=2 else NAN,
                             rmssd=rmssd,sdnn=sdnn,resp_rate=rate,rrv=rrv,
                             clock=min(1.,max(0.,(i-onset)/max(1,final-onset)))))
    return features


def resp_evidence(rrv,low,high):
    if not math.isfinite(rrv):
        return "unmeasured"
    above,below = high is not None and rrv>=high,low is not None and rrv<=low
    return "bars_degenerate" if above and below else "irregular" if above else "regular" if below else "measured_mid_band"


def reference_bars(features):
    sleep = [f for f in features if f["ck_sleep"]] or features
    bars = {name:percentile([f[key] for f in sleep],p) for name,key,p in
            (("hr_low","hr",25),("hr_high","hr",70),("rmssd_high","rmssd",70),
             ("hr_var_high","hr_var",65),("rrv_high","rrv",65),("rrv_low","rrv",50))}
    bars["cardiac_sparse"] = bool(sleep) and sum(not math.isfinite(f["rmssd"]) for f in sleep)>=.5*len(sleep)
    return bars


def _predicates(f,bars):
    def over(key,bar):
        return math.isfinite(f[key]) and bars[bar] is not None and f[key]>=bars[bar]
    has_hr = math.isfinite(f["hr"])
    low = has_hr and bars["hr_low"] is not None and f["hr"]<=bars["hr_low"]
    high,var_high = over("hr","hr_high"),over("hr_var","hr_var_high")
    parasymp = not math.isfinite(f["rmssd"]) or over("rmssd","rmssd_high")
    resp = resp_evidence(f["rrv"],bars["rrv_low"],bars["rrv_high"])
    still,moving = f["move_fraction"]<=.10,f["move_fraction"]>=.15
    wake = moving and ((high if bars.get("cardiac_sparse",False) else high or var_high) or not has_hr)
    deep = still and parasymp and low and resp in ("regular","unmeasured","bars_degenerate")
    rem = still and ((high or var_high) and resp in ("irregular","bars_degenerate") or high and var_high and resp=="unmeasured")
    return wake,deep,rem,still,high or var_high,resp


def classify_one(f,bars):
    wake,deep,rem,*_ = _predicates(f,bars)
    return "wake" if wake else "deep" if deep else "rem" if rem else "light"


def rem_reject_reason(f,bars):
    wake,deep,rem,still,cardiac,resp = _predicates(f,bars)
    return ("won_other_stage" if wake or deep else "rem_eligible" if rem else "not_still" if not still
            else "no_cardiac_activation" if not cardiac else "resp_regular" if resp!="unmeasured" else "no_resp_fallback_bar")


def smooth_labels(labels,window=5):
    if not labels or window<=1:
        return list(labels)
    half = (window+(window%2==0))//2
    out = []
    for i,label in enumerate(labels):
        counts = {}
        for v in labels[max(0,i-half):min(len(labels),i+half+1)]:
            counts[v] = counts.get(v,0)+1
        best = max(counts.values())
        winners = [v for v,count in counts.items() if count==best]
        out.append(label if label in winners else winners[0])
    return out


def reimpose_physiology(labels,features,onset,final):
    out = list(labels)
    early_deep = any(label=="deep" and f["clock"]<=1/3 for label,f in zip(labels,features))
    for i,f in enumerate(features):
        if onset<=i<=final:
            if out[i]=="rem" and i-onset<30:
                out[i]="light"
            if out[i]=="deep" and f["clock"]>1/3 and early_deep:
                out[i]="light"
    return out


def merge_fragments(labels,threshold=6):
    if not labels or threshold<=1:
        return list(labels)
    runs = []
    for stage in labels:
        if runs and runs[-1][0]==stage:
            runs[-1][1]+=1
        else:
            runs.append([stage,1])
    merged,i = [],0
    rank = {"light":1,"rem":2,"deep":3}
    while i<len(runs):
        stage,length = runs[i]
        if length>=threshold:
            merged.append([stage,length]); i+=1; continue
        prev = merged[-1] if merged else None
        nxt = runs[i+1] if i+1<len(runs) else None
        if prev and nxt and prev[0]==nxt[0]:
            prev[1]+=length+nxt[1]; i+=2
        elif prev and nxt:
            winner = prev if prev[1]>nxt[1] else nxt if nxt[1]>prev[1] else prev if rank.get(prev[0],0)<=rank.get(nxt[0],0) else nxt
            winner[1]+=length; i+=1
        elif nxt:
            nxt[1]+=length; i+=1
        elif prev:
            prev[1]+=length; i+=1
        else:
            merged.append([stage,length]); i+=1
    return [stage for stage,length in merged for _ in range(length)]


def stage_sleep(start,end,hr,gravity,rr,resp=()):
    """Forced-window source V1 staging, including its explicit light fallback.

    Source includes rows at both boundaries and preserves input chronological order.
    Coverage reports missing epochs even though the source emits a tiled hypnogram.
    """
    coverage = {"expected_epochs":max(0,math.ceil((end-start)/30))}
    def result(value,reason=None,**extra):
        return dict(value=value,reason=reason,coverage=coverage,source=SOURCE,**extra)
    if end<=start:
        return result(None,"Empty or inverted sleep window")
    rows = [[tuple(row) for row in values if start<=row[0]<=end] for values in (hr,gravity,rr,resp)]
    hr,gravity,rr,resp = rows
    if len(gravity)<2:
        coverage.update(observed_epochs=0,gravity_covered_epochs=0,hr_covered_epochs=0,gravity_samples=len(gravity),source_fallback=True)
        return result([dict(start=start,end=end,stage="light")],"Source V1 light fallback: fewer than two gravity samples")
    grid = build_epoch_grid(start,end,hr,gravity,rr,resp)
    flags = cole_kripke(rescale_counts(grid["counts"]))
    onset,final = onset_and_final_wake(flags)
    features = extract_features(grid,flags,dog_hr_variability(grid["hr"]),onset,final)
    bars = reference_bars(features)
    raw = [classify_one(f,bars) for f in features]
    smoothed = smooth_labels(raw)
    reimposed = reimpose_physiology(smoothed,features,onset,final)
    labels = merge_fragments(reimposed)
    labels = [label if onset<=i<=final else "wake" for i,label in enumerate(labels)]
    segments = []
    for i,label in enumerate(labels):
        if segments and segments[-1]["stage"]==label:
            segments[-1]["end"]=grid["edges"][i+1]
        else:
            segments.append(dict(start=grid["edges"][i],end=grid["edges"][i+1],stage=label))
    if segments:
        segments[-1]["end"]=end
    coverage.update(observed_epochs=grid["gravity_covered"],gravity_covered_epochs=grid["gravity_covered"],
                    hr_covered_epochs=grid["hr_covered"],gravity_samples=len(gravity),
                    rr_covered_epochs=sum(bool(v) for v in grid["rr"]),resp_covered_epochs=sum(bool(v) for v in grid["resp"]),
                    source_fallback=False,cardiac_sparse=bars["cardiac_sparse"])
    reasons = {}
    for f in features[onset:final+1]:
        reason = rem_reject_reason(f,bars)
        reasons[reason]=reasons.get(reason,0)+1
    return result(segments,rem_funnel=dict(sleep_epochs=max(0,final-onset+1),rejections=reasons,
                  rem_at_classify=raw[onset:final+1].count("rem"),rem_after_reimpose=reimposed[onset:final+1].count("rem"),
                  rem_stripped_by_onset_guard=sum(smoothed[i]=="rem" and i-onset<30 for i in range(onset,final+1)),
                  resp_channel_present=any(math.isfinite(f["rrv"]) for f in features[onset:final+1])))
