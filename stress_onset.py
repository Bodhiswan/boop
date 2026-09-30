"""Pure source port of NOOP StressOnsetDetector at 7f396e98.

Describes a sustained HRV dip at rest; never diagnoses or performs any actuator.
The caller persists next_state, de-duplicates live packets and explicitly opts in.
"""
import math
from analytics import clean_rr, finite

DEFAULT_CONFIG=dict(enabled=False,auto_nudge=False,quiet_hours_enabled=False,
                    quiet_start_minutes=1320,quiet_end_minutes=420,buzz_loops=1)
INITIAL_STATE=dict(baseline_rmssd=0.0,was_below=False,last_fire_at=0,pending_edge_at=0)

def evaluate(rr_buffer,current_hr,recent_motion_g,session_active,state=None,config=None,
             now_sec=0,tz_offset_sec=0,*,live=True,replay=False):
    """Return an inert decision plus a JSON-persistable state; all times are seconds.

    live/replay are BOOP transport gates before the reference evaluator. They leave
    state untouched, just as source AppModel de-duplicates packet sequence before
    calling the engine. Lack of motion alone does not invent an exercise gate.
    """
    prior={**INITIAL_STATE,**(state or {})}; cfg={**DEFAULT_CONFIG,**(config or {})}
    next_state=dict(prior); fast=None; baseline=prior['baseline_rmssd'] or None
    def decision(reason,nudge=False):
        return dict(should_nudge=nudge,reason=reason,buzz_loops=cfg['buzz_loops'],
                    fast_rmssd=fast,baseline_rmssd=baseline,next_state=dict(next_state),
                    source='StressOnsetDetector.swift',note='HRV dipped while at rest; descriptive proxy only')
    if live is not True or replay is True:return decision('suppressed')
    if cfg['enabled'] is not True or cfg['auto_nudge'] is not True:return decision('disabled')
    clean,_=clean_rr(rr_buffer); tail=clean[-60:]
    if len(tail)<20:return decision('insufficientData')
    fast=math.sqrt(sum((b-a)**2 for a,b in zip(tail,tail[1:]))/(len(tail)-1))
    if not finite(fast) or fast<=0:
        fast=None
        return decision('insufficientData')
    baseline=fast if prior['baseline_rmssd']==0 else prior['baseline_rmssd']*.98+fast*(1-.98)
    next_state['baseline_rmssd']=baseline
    below=fast<baseline*.6; edge=below and not prior['was_below']; next_state['was_below']=below
    if edge:next_state['pending_edge_at']=now_sec
    if not below:next_state['pending_edge_at']=0
    if not below:return decision('noDip')
    if next_state['pending_edge_at']==0:return decision('notAnEdge')
    if now_sec-next_state['pending_edge_at']<60:return decision('awaitingSustain')
    next_state['pending_edge_at']=0 # Consume the arm even when a later gate suppresses it.
    if not finite(current_hr) or not 55<=current_hr<=100 or (finite(recent_motion_g) and recent_motion_g>=.15):
        return decision('exerciseGated')
    if session_active or (prior['last_fire_at']!=0 and now_sec-prior['last_fire_at']<900):return decision('suppressed')
    if cfg['quiet_hours_enabled']:
        minute=((int(now_sec)+int(tz_offset_sec))%86400)//60
        start=cfg['quiet_start_minutes']; end=cfg['quiet_end_minutes']
        quiet=start<=minute<end if start<end else minute>=start or minute<end if start>end else False
        if quiet:return decision('suppressed')
    next_state['last_fire_at']=now_sec
    return decision('onset',True)
