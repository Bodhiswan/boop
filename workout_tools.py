"""Pure reachable NOOP workout/lifting ports at7f396e98.

HeartRateRecovery/ManualWorkoutRescore are called by WorkoutDetailView and
IntelligenceEngine. LiftSessionEngine and LiftSessionController finish/program
helpers are called by the shipped LiftSessionView. No IO, timer or haptic calls.
Mutations affect only this in-memory session. Workout edit plans never persist.
All times are unix seconds; declared timestamp_ms inputs are converted explicitly.
"""
import copy
import math
import statistics as stats

from analytics import effort,bout_calories,rounded,result


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


def hr_samples(samples):
    """Adapt declared canonical HR inputs without manufacturing beat timestamps."""
    output=[]
    for sample in samples:
        if isinstance(sample,dict):
            t=sample.get('ts',sample['timestamp_ms']/1000 if finite(sample.get('timestamp_ms')) else None)
            h=sample.get('bpm',sample.get('hr'))
            if sample.get('contact')==0:continue
        else:t,h=sample
        if finite(t) and finite(h):output.append((t,h))
    return output


def heart_rate_recovery(samples,workout_start,workout_end,hr_max):
    """Exact HeartRateRecovery.calculate; recovery fields are signed BPM DROPS."""
    if not all(finite(v) for v in (workout_start,workout_end,hr_max)) or workout_start<=0 or workout_end<=workout_start or hr_max<=0:
        return result(reason='Requires positive workout bounds and HRmax')
    lower=max(workout_start,workout_end-300)
    ordered=sorted((t,h) for t,h in hr_samples(samples) if lower<=t<=workout_end+315 and 30<=h<=250)
    before=[(t,h) for t,h in ordered if t<=workout_end];current=longest=0
    for (t,h),(next_t,_) in zip(before,before[1:]):
        gap=next_t-t
        if gap<=0:continue
        if gap>10 or h<hr_max*.70:current=0;continue
        current+=gap;longest=max(longest,current)
    coverage=dict(samples=len(ordered),continuous_high_seconds=longest)
    if longest<120:return result(reason='Requires continuous120 seconds at70%HRmax in the last5 minutes',coverage=coverage)
    cessation=[h for t,h in before if t>=workout_end-30]
    if len(cessation)<3:return result(reason='Requires3 cessation HR samples in the final30 seconds',coverage=coverage)
    peak=max(cessation);measured={}
    for minutes in (1,2,5):
        values=[h for t,h in ordered if abs(t-(workout_end+minutes*60))<=15]
        measured[f'after_{minutes}_minute_bpm']=peak-int(rounded(stats.median(values),0)) if len(values)>=3 else None
        coverage[f'after_{minutes}_minute_samples']=len(values)
    value=dict(end_hr_bpm=peak,**measured) if any(v is not None for v in measured.values()) else None
    return result(value,None if value is not None else 'No post-workout target window has3 recorded HR samples',coverage,unit='bpm drop',source='HeartRateRecovery.swift')


def manual_workout_rescore(samples,profile,hr_max,resting_hr=None,effort_method='edwards'):
    """ManualWorkoutRescore.scored; profile gaps withhold calories, not measured HR.

    Source cold-start RHR60 is explicit provenance. Personalized calorie gates
    match BOOP analytics; source's unknown-weight/age fallback is not asserted.
    """
    samples=hr_samples(samples)
    if len(samples)<2:return result(reason='Requires at least2 observed HR samples',coverage=dict(samples=len(samples)))
    rhr=resting_hr if finite(resting_hr) else 60
    load=effort(samples,hr_max,rhr,effort_method,profile.get('sex'))
    calories=bout_calories(samples,profile,hr_max,rhr)
    return result(dict(avg_hr=int(rounded(stats.mean(h for _,h in samples),0)),peak_hr=max(h for _,h in samples),effort=load['value'],calories=calories['value'] if finite(calories['value']) and calories['value']>0 else None),coverage=dict(samples=len(samples)),effort_detail=load,calorie_detail=calories,resting_hr_source='measured_or_explicit' if finite(resting_hr) else 'NOOP cold-start60',source='ManualWorkoutRescore.swift')


def rescore_improves(scored,current_kcal=None,current_effort=None,allow_effort_only_fill=False):
    """Source strict>1kcal improvement or explicit merged-row missing-strain fill."""
    kcal=scored.get('calories')
    return finite(kcal) and kcal>(current_kcal or 0)+1 or allow_effort_only_fill and current_effort is None and scored.get('effort') is not None


def _span(record):
    return tuple(record.get(key,record[key+'_ms']/1000 if finite(record.get(key+'_ms')) else None) for key in ('start','end'))


def _value(value):
    return value.get('value') if isinstance(value,dict) else value


def rescore_workout(record,samples,profile,hr_max,resting_hr=None,effort_method='edwards',allow_effort_only_fill=False):
    """Return proposed full row plus source improvement decision, without writing.

    A merged-row Effort fill preserves its summed calories. Callers must still
    bind persistence to the exact id/device/natural key and their origin guard.
    """
    start,end=_span(record);old_kcal=_value(record.get('calories'));old_effort=_value(record.get('effort'))
    if workout_source(record)!='manual':return result(reason='Source rescore applies to manual rows, not imported history or detector rows')
    if not finite(start) or not finite(end) or end<=start:return result(reason='Invalid recorded workout bounds')
    if (old_kcal or 0)>5 and old_effort is not None:return result(reason='Already scored; source rescore gate leaves row unchanged')
    scored=manual_workout_rescore([(t,h) for t,h in hr_samples(samples) if start<=t<=end],profile,hr_max,resting_hr,effort_method)
    if scored['value'] is None:return scored
    gain=rescore_improves(scored['value'],old_kcal,old_effort,allow_effort_only_fill)
    if not gain:return result(reason='Recomputed metrics do not exceed source improvement gate',scored=scored)
    proposed=copy.deepcopy(record);proposed.update(scored['value'])
    if not finite(scored['value']['calories']) or scored['value']['calories']<=(old_kcal or 0)+1:proposed['calories']=record.get('calories')
    return result(proposed,binding={k:record.get(k) for k in ('id','device','source')},source='ManualWorkoutRescore.swift / IntelligenceEngine post-sync',scored=scored)


def workout_source(record):
    """WorkoutSource.classify, with explicit BOOP adapter names for computed rows."""
    source=str(record.get('source','')).lower()
    if source.endswith('-noop') or source in ('detected','computed'):return 'detected'
    if source in ('manual','user_record','record') or record.get('source_category')=='manual':return 'manual'
    return 'imported'


def merge_workouts(rows,sport=None):
    """WorkoutMerge math: sum active duration, honest min/max span, no summed Effort.

    Source canMerge permits manual/detected only; disjoint selections are allowed
    by source and retained as a span with the separate sum of active durations.
    """
    if len(rows)<2 or any(workout_source(r) not in ('manual','detected') for r in rows):return result(reason='Merge requires2+ manual/detected workouts; imported history stays read-only')
    spans=[_span(r) for r in rows]
    if any(not finite(a) or not finite(b) or b<=a for a,b in spans):return result(reason='Every selected workout requires actual positive bounds')
    def total(key):
        values=[_value(r.get(key)) for r in rows if finite(_value(r.get(key)))]
        return sum(values) if values else None
    weights=[r.get('duration_seconds') if finite(r.get('duration_seconds')) else max(0,b-a) for r,(a,b) in zip(rows,spans)]
    hr_pairs=[(r['avg_hr'],weight) for r,weight in zip(rows,weights) if finite(r.get('avg_hr'))]
    labels=[r.get('sport',r.get('name')) for r in rows if r.get('sport',r.get('name')) not in (None,'detected')]
    label=sport if sport is not None else max(dict.fromkeys(labels),key=lambda s:labels.count(s)) if labels else 'Activity'
    notes=[str(r['notes']).strip() for r in rows if r.get('notes') and str(r['notes']).strip()]
    peaks=[r.get('peak_hr',r.get('max_hr')) for r in rows if finite(r.get('peak_hr',r.get('max_hr')))]
    return result(dict(start=min(a for a,_ in spans),end=max(b for _,b in spans),source='manual',sport=label,duration_seconds=sum(weights),calories=total('calories'),avg_hr=int(rounded(sum(h*w for h,w in hr_pairs)/sum(w for _,w in hr_pairs),0)) if hr_pairs and sum(w for _,w in hr_pairs)>0 else None,peak_hr=max(peaks) if peaks else None,distance_m=total('distance_m'),steps=total('steps'),effort=None,zone_percent=None,notes=' · '.join(notes) if notes else None),source='WorkoutSource.swift WorkoutMerge',input_ids=[r.get('id') for r in rows])


def workout_edit_plan(action,rows,sport=None):
    """Pure source-shaped mutation proposal; caller persists atomically, never this helper."""
    if action not in ('merge','dismiss','relabel'):return result(reason='Choose merge, dismiss or relabel')
    if action=='merge':
        merged=merge_workouts(rows,sport)
        if merged['value'] is None:return merged
        write=merged['value']
    else:
        if len(rows)!=1 or workout_source(rows[0])!='detected':return result(reason='Dismiss/relabel applies only to one detected workout')
        write=copy.deepcopy(rows[0]) if action=='relabel' else None
        if write is not None:
            if not isinstance(sport,str) or not sport.strip():return result(reason='Relabel requires a supplied activity name')
            start,end=_span(write)
            if not finite(start) or not finite(end) or end<=start:return result(reason='Invalid recorded workout bounds')
            write.update(start=start,end=end,source='manual',sport=sport.strip())
            # A relabel is a new manual row followed by retiring the detector
            # row; reusing its record id would delete the replacement afterward.
            write.pop('id',None)
    retired=[]
    for row in rows:
        start,end=_span(row)
        if not finite(start) or not finite(end) or end<=start:return result(reason='Invalid recorded workout bounds')
        # Do not delete the natural key now owned by the replacement.
        if action=='merge' and write is not None and start==write['start'] and row.get('sport',row.get('name'))==write['sport']:continue
        retired.append(dict(id=row.get('id'),device=row.get('device'),source=row.get('source'),start=start,end=end,sport=row.get('sport',row.get('name')),dismiss_token=f'{int(start)}:{int(end)}' if workout_source(row)=='detected' else None))
    return result(dict(write=write,retire=retired),persisted=False,source='Repository relabelDetected/dismissDetected/mergeWorkouts')


class LiftSession:
    """LiftSessionEngine pure sheet, arbitrary slot order, absolute rest and undo.

    plan uses snake_case LiftPlanItem fields; slots are(exercise_index0,set_index1).
    Grey carry never becomes typed input or a measured RPE. Finishing returns
    proposed records only. The caller chooses whether to complete unstarted slots.
    """
    def __init__(self,plan,start_ts):
        self.plan=[self._line(p) for p in plan];self.start_ts=start_ts;self.stage='warmup';self.slot=None;self.ends_at=None;self.stage_started_at=start_ts;self.sets=[];self.history=[]

    @classmethod
    def from_snapshot(cls,snapshot):
        """Source restoring initializer: supplied actual state, fresh undo history."""
        session=cls(snapshot['plan'],snapshot['start_ts']);session.stage=snapshot['stage'];session.slot=tuple(snapshot['slot']) if snapshot.get('slot') is not None else None;session.ends_at=snapshot.get('ends_at');session.stage_started_at=snapshot['stage_started_at'];session.sets=copy.deepcopy(snapshot['sets']);return session

    @staticmethod
    def _line(item):
        out=copy.deepcopy(item);out['target_sets']=max(1,out.get('target_sets') or 1);out['rest_seconds']=max(0,120 if out.get('rest_seconds') is None else out['rest_seconds']);return out

    def all_slots(self):return [(e,s) for e,p in enumerate(self.plan) for s in range(1,p['target_sets']+1)]
    def recorded(self,slot):return next((s for s in self.sets if (s['exercise_index'],s['set_index'])==tuple(slot)),None)
    def next_pending(self):return next((s for s in self.all_slots() if not self.recorded(s)),None)
    def slot_after(self,slot):return next((s for s in self.all_slots() if s[0]==slot[0] and not self.recorded(s)),None) or self.next_pending()
    def upcoming(self):
        if self.stage=='finished':return None
        if self.stage=='warmup':return self.next_pending()
        pending=[s for s in self.all_slots() if not self.recorded(s) and s!=self.slot]
        return next((s for s in pending if s[0]==self.slot[0]),None) or (pending[0] if pending else None)
    def _push(self):self.history.append(copy.deepcopy((self.plan,self.stage,self.slot,self.ends_at,self.sets,self.stage_started_at)))
    def snapshot(self):return copy.deepcopy(dict(plan=self.plan,start_ts=self.start_ts,stage=self.stage,slot=list(self.slot) if self.slot is not None else None,ends_at=self.ends_at,sets=self.sets,stage_started_at=self.stage_started_at,next_slot=self.upcoming(),can_undo=bool(self.history)))
    def start(self,slot,now):
        slot=tuple(slot)
        if slot not in self.all_slots():return False
        self._push();self.sets=[s for s in self.sets if (s['exercise_index'],s['set_index'])!=slot];self.stage='working';self.slot=slot;self.ends_at=None;self.stage_started_at=now;return True
    def advance(self,now):
        if self.stage=='warmup':
            slot=self.next_pending();return self.start(slot,now) if slot is not None else False
        if self.stage=='finished':return False
        self._push()
        if self.stage=='working':
            self.sets.append(dict(exercise_index=self.slot[0],set_index=self.slot[1],weight_kg=None,reps=None,rpe=None,is_warmup=False,start_ts=self.stage_started_at,end_ts=now,rest_seconds=None))
            self.stage='resting';self.ends_at=now+self.plan[self.slot[0]]['rest_seconds'];self.stage_started_at=now
        else:
            recorded=self.recorded(self.slot)
            if recorded is not None:recorded['rest_seconds']=max(0,now-self.stage_started_at)
            next_slot=self.slot_after(self.slot)
            if next_slot is not None:self.stage='working';self.slot=next_slot;self.ends_at=None
            else:self.ends_at=now
            self.stage_started_at=now
        return True
    def rest_remaining(self,now):return max(0,self.ends_at-now) if self.stage=='resting' else None
    def update_set(self,slot,weight_kg=None,reps=None,rpe=None,is_warmup=False):
        record=self.recorded(slot)
        if record is None:return False
        record.update(weight_kg=weight_kg,reps=reps,rpe=rpe,is_warmup=is_warmup);return True
    def finish(self,now):
        if self.stage=='finished':return False
        self._push();record=self.recorded(self.slot) if self.stage=='resting' else None
        if record is not None and record['rest_seconds'] is None:record['rest_seconds']=max(0,now-self.stage_started_at)
        self.stage='finished';self.slot=None;self.ends_at=None;self.stage_started_at=now;return True
    def undo(self):
        if not self.history:return False
        self.plan,self.stage,self.slot,self.ends_at,self.sets,self.stage_started_at=self.history.pop();return True
    def add_set(self,exercise_index):
        if not 0<=exercise_index<len(self.plan) or self.plan[exercise_index]['target_sets']>=20:return False
        self._push();self.plan[exercise_index]['target_sets']+=1;return True
    def remove_set(self,exercise_index):
        if not 0<=exercise_index<len(self.plan):return False
        slot=(exercise_index,self.plan[exercise_index]['target_sets'])
        if slot[1]<=1 or self.recorded(slot) or self.slot==slot:return False
        self._push();self.plan[exercise_index]['target_sets']-=1;return True
    def add_exercise(self,item):
        if self.stage=='finished' or len(self.plan)>=200:return False
        self._push();line=self._line(item);line['target_sets']=1;line['added_in_session']=True;self.plan.append(line);return True
    def carry(self,slot,last_session=None):
        slot=tuple(slot);last_session=last_session or {};previous=next((self.recorded((slot[0],i)) for i in range(slot[1]-1,0,-1) if self.recorded((slot[0],i))),None)
        prev=self.values((previous['exercise_index'],previous['set_index']),last_session) if previous else {};last=last_session.get(slot[1],{});line=self.plan[slot[0]]
        def choose(key,target):return next((v for v in (prev.get(key),last.get(key),line.get(target)) if v is not None),None)
        return dict(weight_kg=choose('weight_kg','target_weight_kg'),reps=choose('reps','target_reps_low'))
    def values(self,slot,last_session=None):
        grey=self.carry(slot,last_session);record=self.recorded(slot)
        return {k:record[k] if record and record[k] is not None else v for k,v in grey.items()}
    def finish_sets(self,complete_unstarted=False,last_session=None,pending_values=None):
        output=[];last_session=last_session or {};pending_values=pending_values or {}
        for record in self.sets:
            slot=(record['exercise_index'],record['set_index']);saved=copy.deepcopy(record);saved.update(self.values(slot,last_session.get(slot[0],{})))
            # Actual source finish helper fills from an explicitly supplied plan
            # RPE, unlike the live carry engine; retain that provenance visibly.
            if saved['rpe'] is None:saved['rpe']=self.plan[slot[0]].get('target_rpe')
            saved['rpe_is_planned']=record['rpe'] is None and saved['rpe'] is not None
            output.append(saved)
        for slot in self.all_slots():
            if self.recorded(slot):continue
            grey=self.carry(slot,last_session.get(slot[0],{}));typed=pending_values.get(slot,{})
            planned_rpe=self.plan[slot[0]].get('target_rpe')
            rpe=(typed.get('rpe') if typed.get('rpe') is not None else planned_rpe) if complete_unstarted else None
            output.append(dict(exercise_index=slot[0],set_index=slot[1],weight_kg=(typed.get('weight_kg') if typed.get('weight_kg') is not None else grey['weight_kg']) if complete_unstarted else 0,reps=(typed.get('reps') if typed.get('reps') is not None else grey['reps']) if complete_unstarted else 0,rpe=rpe,rpe_is_planned=complete_unstarted and typed.get('rpe') is None and rpe is not None,is_warmup=bool(typed.get('is_warmup')),start_ts=None,end_ts=None,rest_seconds=None))
        return output


def rest_event_times(ends_at,now):
    """LiftSessionController one-shot warning/end instants; no timer created."""
    return dict(warning=max(now+1,ends_at-5),end=ends_at if ends_at>now else None)


def after_session_program(sets,plan,program,keep_plan_changes=False):
    """Source applyingHeaviestSets/programAfterSession; proposal, never a write.

    Only timed performed non-warmup sets change loads. Higher load wins, then
    reps; unstarted completions and discarded zeros never move the program.
    """
    output=copy.deepcopy(program);by_id={p['id']:p for p in output};next_order=max((p.get('ord',0) for p in output),default=-1)+1
    if keep_plan_changes:
        for line in plan:
            key=line.get('program_item_id')
            if key in by_id:by_id[key]['target_sets']=line['target_sets']
        for index,line in enumerate(p for p in plan if p.get('added_in_session')):
            key=line.get('program_item_id')
            if key is None or key in by_id:continue
            item=dict(id=key,ord=next_order+index,exercise=line['exercise'],target_sets=line['target_sets'],target_weight_kg=0,target_reps_low=0,target_reps_high=None,target_rpe=None,rest_seconds=None,note=None);output.append(item);by_id[key]=item
    best={}
    for item in sets:
        index=item.get('exercise_index');reps=item.get('reps')
        if item.get('start_ts') is None or item.get('is_warmup') or reps==0 or not isinstance(index,int) or not 0<=index<len(plan):continue
        key=plan[index].get('program_item_id')
        if key not in by_id:continue
        rank=(item.get('weight_kg') if item.get('weight_kg') is not None else -1,reps if reps is not None else -1)
        if key not in best or rank>best[key][0]:best[key]=(rank,item)
    for key,(_,item) in best.items():
        target=by_id[key]
        if item.get('weight_kg') is not None:target['target_weight_kg']=item['weight_kg']
        if item.get('reps') is not None:
            target['target_reps_low']=item['reps']
            if 'target_reps' in target:target['target_reps']=item['reps']
            if target.get('target_reps_high') is not None and target['target_reps_high']<item['reps']:target['target_reps_high']=None
    return output
