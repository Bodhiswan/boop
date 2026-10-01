"""Small, source-aware observations for BOOP's terminal view."""
import math


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def quantile(values,p):
    values=sorted(v for v in values if finite(v))
    if not values:return None
    index=(len(values)-1)*p;low=int(index);high=min(len(values)-1,low+1)
    return values[low]+(values[high]-values[low])*(index-low)


def personal_baselines(bundle,selected):
    """Exclude the inspected day so it cannot move its own comparison range."""
    keys={'sleep':('rest','%'),'effort':('effort','%'),'rhr':('resting_hr','bpm'),'rhrv':('hrv','ms')}
    previous=[day for day in bundle.get('days',[]) if day.get('day','')<selected]
    output={}
    for label,(key,unit) in keys.items():
        observations=[]
        for day in previous:
            item=day.get(key);item={} if item is None else item;value=item.get('value') if isinstance(item,dict) else item
            if finite(value):observations.append({'day':day.get('day'),'value':value,'source':item.get('source','BOOP local estimate') if isinstance(item,dict) else 'BOOP local estimate'})
        values=[o['value'] for o in observations]
        output[label]={'n':len(values),'median':quantile(values,.5),'low':quantile(values,.25),'high':quantile(values,.75),
            'ready':len(values)>=7,'unit':unit,'observations':observations,
            'status':'established' if len(values)>=14 else 'building' if len(values)>=7 else 'learning'}
    slept=[]
    for day in previous:
        sleep=day.get('sleep') or {};main=sleep.get('main') or {}
        value=main.get('total_sleep_min',sleep.get('total_sleep_min'))
        if finite(value):slept.append({'day':day.get('day'),'value':value})
    values=[o['value'] for o in slept]
    output['sleep_minutes']={'n':len(values),'median':quantile(values,.5),'low':quantile(values,.25),'high':quantile(values,.75),
        'ready':len(values)>=7,'unit':'min','observations':slept,'status':'established' if len(values)>=14 else 'building' if len(values)>=7 else 'learning'}
    return {'through':selected,'window_days':30,'metrics':output,'source':'Real local observations; demo excluded',
        'note':'Typical range is the middle 50% of observed prior days. Seven observations unlock comparison; this is a descriptive product threshold.'}


def clock_profile(points,min_days=3):
    """One vote per observed local day per five-minute bin, never fill gaps."""
    days={}
    for point in points:
        if not finite(point.get('t')) or not finite(point.get('hr')):continue
        local=int(point['t'])+10*3600000
        key=(local//86400000,(local%86400000)//300000)
        days.setdefault(key,[]).append(point['hr'])
    bins={}
    for (_,bucket),values in days.items():bins.setdefault(bucket,[]).append(sum(values)/len(values))
    return [{'t':(bucket+.5)*300000,'low':quantile(values,.25),'median':quantile(values,.5),'high':quantile(values,.75),'n':len(values),'mean':sum(values)/len(values)}
            for bucket,values in sorted(bins.items()) if len(values)>=min_days]


def day_rhythm(hr,gravity,start,end):
    """15-minute bins, with equal weight per observed minute and explicit gaps.

    Motion is a relative wrist signal, from adjacent one-second gravity vectors.
    Never compare vectors across recording gaps or interpret missing data as rest.
    """
    minutes={}
    for t,value in hr:
        if finite(t) and finite(value) and start<=t<end and 30<=value<=240:
            minutes.setdefault(int((t-start)//60),{'hr':[],'motion':[]})['hr'].append(value)
    previous=None
    for sample in sorted(gravity):
        if len(sample)!=4 or not all(finite(v) for v in sample):continue
        t=sample[0]
        if previous is not None and 0<t-previous[0]<=1 and start<=previous[0] and t<end:
            delta=math.dist(sample[1:],previous[1:])
            minutes.setdefault(int((t-start)//60),{'hr':[],'motion':[]})['motion'].append(delta)
        previous=sample
    bins={}
    for minute,signals in minutes.items():
        slot=bins.setdefault(minute//15,{'hr':[],'motion':[],'paired_hr':[],'paired_motion':[]})
        for name,values in signals.items():
            if values:slot[name].append(sum(values)/len(values))
        if signals['hr'] and signals['motion']:
            slot['paired_hr'].append(sum(signals['hr'])/len(signals['hr']))
            slot['paired_motion'].append(sum(signals['motion'])/len(signals['motion']))
    points=[]
    for index,signals in sorted(bins.items()):
        values=signals['hr'];movement=signals['motion']
        points.append({'t':int((start+(index+.5)*900)*1000),
            'hr':sum(values)/len(values) if values else None,
            'low':min(values) if values else None,'high':max(values) if values else None,
            'motion':sum(movement)/len(movement) if movement else None,
            'paired_hr':sum(signals['paired_hr'])/len(signals['paired_hr']) if signals['paired_hr'] else None,
            'paired_motion':sum(signals['paired_motion'])/len(signals['paired_motion']) if signals['paired_motion'] else None,
            'paired_minutes':len(signals['paired_hr']),
            'hr_minutes':len(values),'motion_minutes':len(movement)})
    return {'start':int(start*1000),'end':int((start+86400)*1000),'observed_until':int(end*1000),
        'points':points,'hr_minutes':sum(len(s['hr']) for s in bins.values()),
        'motion_minutes':sum(len(s['motion']) for s in bins.values()),
        'source':'Recorded HR and relative wrist motion; 15-minute bins, equal weight per observed minute',
        'motion_note':'Mean change between adjacent one-second gravity vectors; relative wrist movement, not steps or exercise intensity. Gaps stay empty.'}


def motion_profile(gravity,start,end,bucket_seconds):
    bins={};previous=None
    for sample in sorted(gravity):
        if len(sample)!=4 or not all(finite(v) for v in sample):continue
        t=sample[0]
        if previous is not None and 0<t-previous[0]<=1 and start<=previous[0] and t<end:
            bins.setdefault(int((t-start)//bucket_seconds),[]).append(math.dist(sample[1:],previous[1:]))
        previous=sample
    return {'points':[{'t':int((start+(index+.5)*bucket_seconds)*1000),'motion':sum(values)/len(values),
        'coverage':min(1,len(values)/bucket_seconds)} for index,values in sorted(bins.items())],
        'bucket_ms':bucket_seconds*1000,'start':int(start*1000),'end':int(end*1000)}
