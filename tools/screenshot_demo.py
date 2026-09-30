"""Deterministic synthetic screenshot examples for a disposable BOOP store.

Used only by qa_server --demo. No production database, Bluetooth manager,
credentials, cloud service or platform integration is opened. Scores and sleep
stages are computed by the normal analytics from simulated observations.
"""
from contextlib import closing
from datetime import datetime, timedelta
import json
import math
import struct
from zoneinfo import ZoneInfo

from whoop_protocol import STANDARD_HR


def seed_demo(store, features, device, now=None):
    """Populate the caller's empty disposable store, returning count-only metadata."""
    now = now or datetime.now(ZoneInfo('Australia/Brisbane'))
    if now.tzinfo is None:
        raise ValueError('Demo clock must include a timezone')
    with closing(store.connect()) as conn:
        if conn.execute('SELECT COUNT(*) FROM frames').fetchone()[0]:
            raise ValueError('Screenshot demo requires an empty disposable store')
    features.update_settings(dict(name='Synthetic demo', age=32, sex='female',
        weight_kg=68, height_cm=172, hr_max=188, hr_rest=56,
        timezone=str(now.tzinfo), sleep_goal_hours=8, step_calibration=1,
        auto_sync_minutes=0, keep_laptop_awake=False,
        notifications_enabled=False, start_with_windows=False,
        external_push_enabled=False, os_automation_enabled=False,
        scripts_enabled=False, hr_zone_haptics=False))
    observations = {}
    sleeps = []

    def observe(stamp, bpm, night, day_index):
        # Overlapping daytime/workout examples share one observation grid.
        # A workout replaces that instant instead of alternating two HR values.
        stamp = int(stamp) // 10 * 10
        # Breath-like RR modulation and small beat variation are synthetic inputs,
        # not a manually supplied HRV result or physiological score.
        count = max(1, round(10*bpm/60))
        rr = [60000/bpm + (48+day_index*2)*math.sin((stamp+j*60/bpm)*math.pi/2)
              + 9*math.sin((stamp+j)*1.7) for j in range(count)]
        raw = bytes((22, bpm))+b''.join(struct.pack('<H',round(v*1024/1000)) for v in rr)
        angle = .08*math.sin(stamp/1700) if night else .25*math.sin(stamp/7)
        gravity = (math.sin(angle), .03*math.sin(stamp/900), math.cos(angle))
        observations[int(stamp*1000)] = (raw, gravity)

    for offset in range(5, -1, -1):
        wake_day = now.date()-timedelta(days=offset)
        wake = datetime.combine(wake_day, datetime.min.time(), now.tzinfo)+timedelta(hours=6,minutes=30)
        if wake > now:
            wake = now-timedelta(minutes=15)
        start = wake-timedelta(hours=7,minutes=35+(offset%3)*10)
        sleeps.append((start, wake))
        for stamp in range(int(start.timestamp()), int(wake.timestamp()), 10):
            bpm = round(56+offset%4+3*math.sin(stamp/1200)+2*math.sin(stamp/190))
            observe(stamp,bpm,True,offset)
        # Morning/evening observations leave honest gaps outside sample periods.
        morning_end = min(wake+timedelta(hours=2),now)
        for stamp in range(int(wake.timestamp()), int(morning_end.timestamp()), 10):
            observe(stamp,round(74+8*math.sin(stamp/330)),False,offset)
        if offset:
            run = wake.replace(hour=17,minute=15)
            for stamp in range(int(run.timestamp()), int((run+timedelta(minutes=32)).timestamp()), 10):
                observe(stamp,round(138+12*math.sin(stamp/140)),False,offset)

    run_end=now-timedelta(minutes=10)
    run_start=max(sleeps[-1][1]+timedelta(minutes=10),run_end-timedelta(minutes=32))
    if (run_end-run_start).total_seconds()>=600:
        for stamp in range(int(run_start.timestamp()),int(run_end.timestamp()),10):
            observe(stamp,round(138+12*math.sin(stamp/140)),False,0)
    else:
        run_start=sleeps[-2][1].replace(hour=17,minute=15)
        run_end=run_start+timedelta(minutes=32)

    # A short recent interval makes the real chart useful even before dawn.
    for stamp in range(int(now.timestamp())-600,int(now.timestamp()),10):
        observe(stamp,round(70+5*math.sin(stamp/100)),False,0)
    records = [(device,stamp,STANDARD_HR,raw) for stamp,(raw,_) in sorted(observations.items())]
    for index in range(0,len(records),1000):
        store.save(records[index:index+1000])
    with closing(store.connect()) as conn,conn:
        for row in conn.execute('SELECT frame_id,received_ms FROM readings').fetchall():
            stamp=row['received_ms'];gravity=observations[stamp][1]
            conn.execute('UPDATE readings SET gx=?,gy=?,gz=? WHERE frame_id=?',(*gravity,row['frame_id']))
            values=dict(gx=gravity[0],gy=gravity[1],gz=gravity[2],motion_rms=.004,
                        skin_temp_c=33.4+.15*math.sin(stamp/3600),
                        temperature_method='synthetic demo example',synthetic=True)
            conn.execute('INSERT INTO sensors VALUES(?,?,?,?,?,?)',(row['frame_id'],device,stamp,stamp,'synthetic_demo',json.dumps(values)))
    for index,(start,wake) in enumerate(sleeps):
        features.save_record('sleep',dict(id='demo-sleep-'+str(index),device=device,
            date=wake.date().isoformat(),start_ms=int(start.timestamp()*1000),
            end_ms=int(wake.timestamp()*1000),sleep_kind='main_sleep',
            notes='Synthetic demo sleep window; stages computed from simulated observations.'))
        features.save_record('journal',dict(id='demo-journal-'+str(index),date=wake.date().isoformat(),
            question='Outdoor time',answer=index%3!=0,notes='Synthetic demo entry.'))
    today=now.date().isoformat()
    features.save_record('workout',dict(id='demo-run',device=device,date=run_start.date().isoformat(),
        start_ms=int(run_start.timestamp()*1000),end_ms=int(run_end.timestamp()*1000),
        sport='running',label='Morning run' if run_start.date()==now.date() else 'Evening run',notes='Synthetic demo workout.'))
    features.save_record('nutrition',dict(id='demo-breakfast',date=today,calories=520,
        protein_g=28,carbs_g=62,fat_g=18,notes='Synthetic breakfast example.'))
    features.save_record('hydration',dict(id='demo-water',date=today,amount_ml=650,notes='Synthetic demo water log.'))
    features.save_record('mood',dict(id='demo-mood',date=today,score=4,notes='Synthetic demo check-in.'))
    features.save_record('lifting_program',dict(id='demo-program',name='Strength A',
        lines=[dict(exercise='Goblet squat',sets=3,reps=10,weight_kg=16,rest_seconds=90),
               dict(exercise='Dumbbell row',sets=3,reps=10,weight_kg=12,rest_seconds=90)],
        notes='Synthetic example program; no live workout is started.'))
    return dict(synthetic=True,observations=len(records),sleep_windows=len(sleeps),
                date=today,hardware_actions_enabled=False)
