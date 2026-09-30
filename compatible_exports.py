"""Pure WHOOP-shaped CSV adapter for canonical computed BOOP analytics.

Layout and conversions: NOOP7f396e98, StrandImport/WhoopCsvExporter.swift.
These are portable approximate metrics, not proprietary WHOOP scores. Source's
cycle key is display-day midnight; only sleep/workout onset fields encode an
instant. Timestamps are emitted UTC unchanged, even for a Brisbane display day.
Unlike source's legacy false-only nap column, an actually known nap stays true.
No timestamps, HR peaks, sleep stages, or oxygen percentages are synthesized.
"""
import csv
import datetime as dt
import io
import math


CYCLE_COLUMNS = "Cycle start time,Cycle end time,Cycle timezone,Recovery score %,Resting heart rate (bpm),Heart rate variability (ms),Skin temp (celsius),Blood oxygen %,Day Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),Sleep onset,Wake onset,Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),Awake duration (min),Sleep efficiency %,Sleep consistency %,Sleep need (min),Sleep debt (min),Source".split(",")
SLEEP_COLUMNS = "Cycle start time,Sleep onset,Wake onset,Cycle timezone,Nap,Sleep performance %,Respiratory rate (rpm),Asleep duration (min),In bed duration (min),Light sleep duration (min),Deep (SWS) duration (min),REM duration (min),Awake duration (min),Sleep efficiency %,Sleep consistency %,Sleep need (min),Sleep debt (min),Source".split(",")
WORKOUT_COLUMNS = "Cycle start time,Workout start time,Workout end time,Cycle timezone,Activity name,Activity Strain,Energy burned (cal),Max HR (bpm),Average HR (bpm),HR Zone 1 %,HR Zone 2 %,HR Zone 3 %,HR Zone 4 %,HR Zone 5 %,Distance (meters),Source".split(",")
SOURCE = "boop (APPROXIMATE)"


def _number(value):
    if isinstance(value,dict):value=value.get("value")
    return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) else None


def _scale(value,factor):
    number=_number(value)
    return number*factor if number is not None else None


def _field(value):
    if value is None:return ""
    if isinstance(value,(int,float)) and not isinstance(value,bool):
        if not math.isfinite(value):return ""
        return str(int(value)) if value==round(value) and abs(value)<1e12 else str(value)
    value=str(value)
    return "'"+value if value and value[0] in "=+-@\t\r" else value


def _utc(value):
    number=_number(value)
    if number is None:return None
    try:return dt.datetime.fromtimestamp(number,dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError,OSError,ValueError):return None


def _csv(columns,rows):
    output=io.StringIO(newline="");writer=csv.writer(output,lineterminator="\r\n")
    writer.writerow(columns)
    writer.writerows([_field(row.get(key)) for key in columns] for row in rows)
    return output.getvalue()


def stage_minutes(session):
    """Source tolerant stage decoder; no usable data returns all missing values."""
    source=session.get("stage_seconds")
    if isinstance(source,dict) and source:
        source={key:_scale(value,1/60) for key,value in source.items()}
    else:
        source=session.get("stages",session.get("staging",{}).get("value"))
    keys=("light","deep","rem","awake");result={key:None for key in keys}
    if isinstance(source,dict):
        for key in keys:result[key]=_number(source.get(key,source.get("wake") if key=="awake" else source.get("sws") if key=="deep" else None))
    elif isinstance(source,list):
        usable=[]
        for segment in source:
            if not isinstance(segment,dict):continue
            minutes=_number(segment.get("min"))
            if minutes is None and _number(segment.get("start")) is not None and _number(segment.get("end")) is not None:
                minutes=(segment["end"]-segment["start"])/60
            if minutes is not None:usable.append((segment.get("stage",""),minutes))
        if usable:
            result=dict.fromkeys(keys,0.)
            for stage,minutes in usable:
                stage={"wake":"awake","sws":"deep"}.get(str(stage).lower(),str(stage).lower())
                if stage in result:result[stage]+=minutes
    result["asleep"]=sum(result[k] or 0 for k in ("light","deep","rem")) if any(result[k] is not None for k in ("light","deep","rem")) else None
    return result


def _sleep_fields(session):
    stages=stage_minutes(session)
    return {"Asleep duration (min)":stages["asleep"],"In bed duration (min)":(session["end"]-session["start"])/60 if _valid_span(session) else None,
            "Light sleep duration (min)":stages["light"],"Deep (SWS) duration (min)":stages["deep"],"REM duration (min)":stages["rem"],"Awake duration (min)":stages["awake"],"Sleep efficiency %":_scale(session.get("efficiency"),100)}


def _valid_span(record):
    start,end=_number(record.get("start")),_number(record.get("end"))
    return start is not None and end is not None and end>start and _utc(start) is not None and _utc(end) is not None


def export_computed(days,settings=None):
    """Return three upstream-shaped CSV strings, without database or filesystem IO.

    settings is accepted for callers' shared interface; canonical days already
    contain resolved timezone/profile settings. Source cycles carry a day-key
    rather than an instant, and their end/onset/wake fields intentionally blank.
    Source sleeps leave performance, respiration, consistency, need/debt blank.
    """
    if isinstance(days,dict):days=days.get("days",[])
    cycles=[];sleeps=[];workouts=[];seen_sleep=set();seen_workout=set()
    for day in sorted(days,key=lambda row:row.get("day","")):
        key=day.get("day")
        try:dt.date.fromisoformat(key)
        except (TypeError,ValueError):continue
        cycle_start=key+" 00:00:00";sleep=day.get("sleep") or {};main=sleep.get("main") or {}
        row={"Cycle start time":cycle_start,"Cycle timezone":"UTC+00:00","Recovery score %":_number(day.get("charge")),"Resting heart rate (bpm)":_number(day.get("resting_hr")),"Heart rate variability (ms)":_number(day.get("hrv")),"Skin temp (celsius)":_number(day.get("skin_temperature")),"Blood oxygen %":_number(day.get("blood_oxygen")),"Day Strain":_scale(day.get("effort"),.21),"Energy burned (cal)":_number(day.get("calories")),"Max HR (bpm)":_number(day.get("max_hr")),"Average HR (bpm)":_number(day.get("avg_hr")),"Sleep performance %":_number(day.get("rest")),"Respiratory rate (rpm)":_number(day.get("respiration")),"Source":SOURCE}
        if main:row.update(_sleep_fields(main))
        else:
            summary=sleep.get("imported_summary") or {}
            row.update({"Asleep duration (min)":_number(sleep.get("total_sleep_min")),"Light sleep duration (min)":_number(summary.get("light_min")),"Deep (SWS) duration (min)":_number(summary.get("deep_min")),"REM duration (min)":_number(summary.get("rem_min")),"Awake duration (min)":_number(summary.get("awake_min")),"Sleep efficiency %":_scale(summary.get("efficiency"),100)})
        if _number(row.get("Asleep duration (min)")) is not None:
            row["Sleep consistency %"]=_scale(sleep.get("consistency"),100)
            row["Sleep need (min)"]=_scale(sleep.get("need_hours"),60)
            balance=_number((sleep.get("debt") or {}).get("balance_min"))
            row["Sleep debt (min)"]=max(0,-balance) if balance is not None else None
        observed=any(_number(row.get(column)) is not None for column in CYCLE_COLUMNS[3:])
        coverage=day.get("coverage") or {}
        if observed or any((_number(coverage.get(k)) or 0)>0 for k in ("hr_samples","rr_intervals","gravity_samples","sensor_samples")):cycles.append(row)
        for session in sleep.get("value") or []:
            if not _valid_span(session):continue
            identity=(day.get("device"),session["start"],session["end"])
            if identity in seen_sleep:continue
            seen_sleep.add(identity)
            nap=session.get("sleep_kind")=="nap" or session.get("is_nap") is True
            sleeps.append(dict(_sleep_fields(session),**{"Cycle start time":cycle_start,"Sleep onset":_utc(session["start"]),"Wake onset":_utc(session["end"]),"Cycle timezone":"UTC+00:00","Nap":"true" if nap else "false","Source":SOURCE}))
        for workout in (day.get("workouts") or {}).get("value") or []:
            if not _valid_span(workout):continue
            sport=workout.get("sport",workout.get("name",workout.get("activity")))
            identity=(day.get("device"),workout["start"],sport)
            if identity in seen_workout:continue
            seen_workout.add(identity)
            zones=workout.get("zone_percent")
            if isinstance(zones,list) and len(zones)==6:zones=zones[1:]
            if not isinstance(zones,list) or len(zones)!=5 or not any((_number(v) or 0)>0 for v in zones):zones=[None]*5
            row={"Cycle start time":_utc(workout["start"]),"Workout start time":_utc(workout["start"]),"Workout end time":_utc(workout["end"]),"Cycle timezone":"UTC+00:00","Activity name":sport,"Activity Strain":_scale(workout.get("effort"),.21),"Energy burned (cal)":_number(workout.get("calories")),"Max HR (bpm)":_number(workout.get("peak_hr",workout.get("max_hr"))),"Average HR (bpm)":_number(workout.get("avg_hr")),"Distance (meters)":_number(workout.get("distance_m")),"Source":SOURCE}
            row.update({f"HR Zone {i+1} %":_number(value) for i,value in enumerate(zones)});workouts.append(row)
    sleeps.sort(key=lambda row:row["Sleep onset"]);workouts.sort(key=lambda row:row["Workout start time"])
    return {"physiological_cycles.csv":_csv(CYCLE_COLUMNS,cycles),"sleeps.csv":_csv(SLEEP_COLUMNS,sleeps),"workouts.csv":_csv(WORKOUT_COLUMNS,workouts)}
