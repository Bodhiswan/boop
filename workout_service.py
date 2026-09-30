"""Device-bound workout edits and persisted lift control, with no BLE/timer IO.

Only API callers decide to invoke mutations. Edit proposals, revisions, detection
tombstones and lift finishes commit together. Restarted working sets are never
completed across an unobserved process gap; the restored sheet requires resume.
"""
from contextlib import closing,contextmanager
import copy
import datetime as dt
import json
import threading
import time
import uuid

from analytics import profile_for_day
from workout_tools import (LiftSession,after_session_program,finite,
                           heart_rate_recovery,manual_workout_rescore,
                           rescore_workout,workout_edit_plan,workout_source)


SCHEMA="""
CREATE TABLE IF NOT EXISTS workout_dismissals(device TEXT NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(device,start_ms,end_ms));
CREATE TABLE IF NOT EXISTS workout_edits(id TEXT PRIMARY KEY,device TEXT NOT NULL,day TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_ms INTEGER NOT NULL,undone_ms INTEGER);
CREATE TABLE IF NOT EXISTS lift_control(device TEXT PRIMARY KEY,state_json TEXT,updated_ms INTEGER NOT NULL);
"""


def _json(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
def _now():return time.time()
def _num(value,lo,hi,name,integer=False):
    if not finite(value) or not lo<=value<=hi or integer and int(value)!=value:raise ValueError(f'{name} must be {"an integer" if integer else "a number"} in {lo}..{hi}')
    return int(value) if integer else value


class WorkoutService:
    def __init__(self,manager,features,analytics):
        self.manager,self.features,self.analytics=manager,features,analytics
        self.instance=uuid.uuid4().hex;self.lock=threading.RLock();self.local=threading.local()
        with closing(features.store.connect()) as conn:conn.executescript(SCHEMA)

    def _device(self):
        value=getattr(self.local,'device',self.manager.address)
        if not isinstance(value,str) or not value:raise ValueError('Select an active device')
        return value

    @contextmanager
    def _binding(self):
        device=self.manager.address
        if not isinstance(device,str) or not device:raise ValueError('Select an active device')
        self.local.device=device
        try:
            yield
            if self.manager.address!=device:raise ValueError('Active device changed during the request; retry')
        finally:del self.local.device

    @staticmethod
    def _date(value):
        if value is None:return None
        try:return dt.date.fromisoformat(value).isoformat()
        except (TypeError,ValueError):raise ValueError('date must be YYYY-MM-DD')

    def _settings(self):return self.features.settings()
    def _samples(self,start,end):
        rows,sensors,_=self.analytics._read(self._device(),start,end)
        return self.analytics._streams(rows,sensors)[0]

    @staticmethod
    def _kind(row):
        if row.get('source_category')=='imported' or row.get('import_key') or row.get('source_table'):return 'imported'
        if row.get('detected') or str(row.get('id','')).startswith('detected:'):return 'detected'
        if not row.get('source'):return 'manual'  # legacy user CRUD rows
        return workout_source(row)

    def _view_rows(self,date):
        day=self.analytics.day(self._device(),self._date(date),self._settings());rows=[]
        for source in day['workouts'].get('value') or []:
            row=copy.deepcopy(source);kind=self._kind(row)
            if row.get('device') not in (None,'',self._device()) and kind!='imported':continue
            row.update(id=row.get('id',row.get('record_id')),start_ms=int(row['start']*1000),end_ms=int(row['end']*1000),source_kind=kind,editable=kind in ('manual','detected'),detected=kind=='detected',sport=row.get('sport',row.get('name','Activity')))
            rows.append(row)
        return day,rows

    def detail(self,query):
        with self.lock,self._binding():
            day,rows=self._view_rows(query.get('date'));selected=None
            if query.get('id'):
                selected=next((r for r in rows if r['id']==query['id']),None)
                if selected is None:raise ValueError('Workout not found for active device and selected date')
                start,end=selected['start'],selected['end'];hr=self._samples(start,end+316)
                maximum=day.get('provenance',{}).get('hr_max_bpm')
                selected=copy.deepcopy(selected)|dict(hrr=heart_rate_recovery(hr,start,end,maximum),chart=[dict(ts=t,bpm=h) for t,h in hr],coverage=dict(hr_samples=len(hr),workout_samples=sum(start<=t<=end for t,_ in hr),post_workout_samples=sum(t>end for t,_ in hr)))
            return dict(device=self._device(),date=day['day'],workouts=rows,detail=selected)

    @staticmethod
    def _raw(conn,record_id):
        row=conn.execute('SELECT * FROM feature_records WHERE id=?',(record_id,)).fetchone()
        return dict(row) if row else None

    def _revision(self,conn,row,now):
        conn.execute('INSERT INTO feature_revisions(record_id,kind,payload_json,deleted_ms,saved_ms) VALUES(?,?,?,?,?)',(row['id'],row['kind'],row['payload_json'],row['deleted_ms'],now))

    def _operation(self,conn,day,writes,retire,spans=()):
        device=self._device();op=uuid.uuid4().hex;now=int(_now()*1000);before={};ids=[]
        for kind,payload in writes:
            record_id=str(payload.get('id') or uuid.uuid4());payload=copy.deepcopy(payload)|dict(id=record_id)
            before.setdefault(record_id,self._raw(conn,record_id))
            self.features._save(conn,kind,payload);ids.append(record_id)
        for record_id in retire:
            if record_id in ids:continue
            raw=self._raw(conn,record_id)
            if raw is None or raw['deleted_ms'] is not None:raise ValueError('Selected workout changed before the edit')
            before.setdefault(record_id,raw);self._revision(conn,raw,now)
            conn.execute('UPDATE feature_records SET deleted_ms=?,updated_ms=? WHERE id=?',(now,now,record_id))
        for start,end in spans:
            conn.execute('INSERT OR IGNORE INTO workout_dismissals VALUES(?,?,?,?)',(device,int(start*1000),int(end*1000),op))
        after={key:self._raw(conn,key) for key in before}
        conn.execute('INSERT INTO workout_edits VALUES(?,?,?,?,?,?,NULL)',(op,device,day,_json(before),_json(after),now))
        return dict(operation_id=op,saved_ids=ids,retired_ids=retire,undo_available=True)

    def _undo(self,conn,operation_id):
        op=conn.execute('SELECT * FROM workout_edits WHERE id=? AND device=?',(operation_id,self._device())).fetchone()
        if op is None or op['undone_ms'] is not None:raise ValueError('No active operation to undo for this device')
        before,after=json.loads(op['before_json']),json.loads(op['after_json']);now=int(_now()*1000)
        for key,expected in after.items():
            actual=self._raw(conn,key)
            if actual is None or any(actual[k]!=expected[k] for k in ('payload_json','deleted_ms','updated_ms')):raise ValueError('A later edit changed these records; undo cannot overwrite it')
        for key,old in before.items():
            actual=self._raw(conn,key);self._revision(conn,actual,now)
            if old is None:conn.execute('UPDATE feature_records SET deleted_ms=?,updated_ms=? WHERE id=?',(now,now,key))
            else:conn.execute('UPDATE feature_records SET payload_json=?,deleted_ms=?,updated_ms=? WHERE id=?',(old['payload_json'],old['deleted_ms'],now,key))
        conn.execute('DELETE FROM workout_dismissals WHERE device=? AND operation_id=?',(self._device(),operation_id))
        conn.execute('UPDATE workout_edits SET undone_ms=? WHERE id=?',(now,operation_id))
        return dict(operation_id=operation_id,undone=True,date=op['day'])

    @staticmethod
    def _payload(row,device,day):
        out=copy.deepcopy(row)
        for key in ('editable','source_kind','detected','record_id','computed_effort','computed_calories','hrr','chart','coverage','created_ms','updated_ms','deleted_ms','kind'):out.pop(key,None)
        out.update(device=device,day=day,start_ms=int(out['start']*1000),end_ms=int(out['end']*1000),source='manual',source_category='manual')
        return out

    def edit(self,body):
        if not isinstance(body,dict):raise ValueError('Workout action must be an object')
        with self.lock,closing(self.features.store.connect()) as conn,conn,self._binding():
            conn.execute('BEGIN IMMEDIATE')
            if body.get('action')=='undo':
                restored=self._undo(conn,body.get('operation_id'));self.analytics._cache.clear();return restored
            action=body.get('action')
            if action not in ('merge','dismiss','relabel','rescore'):raise ValueError('Unknown workout action')
            if body.get('sport') is not None and (not isinstance(body['sport'],str) or not body['sport'].strip() or len(body['sport'])>300):raise ValueError('sport must be a nonempty activity name of at most300 characters')
            day,visible=self._view_rows(body.get('date'));ids=body.get('ids',[body.get('id')])
            if not isinstance(ids,list) or not 1<=len(ids)<=50 or not all(isinstance(v,str) for v in ids) or len(set(ids))!=len(ids):raise ValueError('Choose unique workout ids')
            selected=[]
            for key in ids:
                row=next((r for r in visible if r['id']==key),None)
                if row is None:raise ValueError('Workout not found for active device and selected date')
                if not row['editable']:raise ValueError('Imported workout history is read-only')
                if row['source_kind']=='manual':row['source']='manual'
                selected.append(row)
            writes=[];retire=[];spans=[]
            if action=='rescore':
                if len(selected)!=1:raise ValueError('Rescore one manual workout at a time')
                row=selected[0];settings=profile_for_day(self._settings(),day['day']);provenance=day['provenance']
                scored=rescore_workout(row,self._samples(row['start'],row['end']+1),settings,provenance.get('hr_max_bpm'),provenance.get('effort_resting_hr_bpm'),settings.get('effort_method','edwards'),True)
                if scored['value'] is None:return dict(changed=False,reason=scored['reason'])
                writes.append(('workout',self._payload(scored['value'],self._device(),day['day'])))
            else:
                planned=workout_edit_plan(action,selected,body.get('sport'))
                if planned['value'] is None:raise ValueError(planned['reason'])
                proposed=planned['value']['write']
                if proposed is not None:
                    # Source natural-key upsert is implemented over UUID records.
                    if action=='merge':
                        same=next((r for r in selected if r['source_kind']=='manual' and r['start']==proposed['start'] and r['sport']==proposed['sport']),None)
                        if same:proposed['id']=same['id']
                    writes.append(('workout',self._payload(proposed,self._device(),day['day'])))
                for row in selected:
                    if row['source_kind']=='detected':spans.append((row['start'],row['end']))
                    else:retire.append(row['id'])
            result=self._operation(conn,day['day'],writes,retire,spans)
            self.analytics._cache.clear();return result|dict(changed=True)

    def _load(self,conn):
        row=conn.execute('SELECT state_json FROM lift_control WHERE device=?',(self._device(),)).fetchone()
        if row is None or row[0] is None:return None
        state=json.loads(row[0])
        if state.get('instance')!=self.instance:
            state.update(paused=True,restarted=True,history=[],pending_history=[])
        return state

    def _persist(self,conn,state):
        conn.execute('INSERT INTO lift_control VALUES(?,?,?) ON CONFLICT(device) DO UPDATE SET state_json=excluded.state_json,updated_ms=excluded.updated_ms',(self._device(),_json(state) if state is not None else None,int(_now()*1000)))

    @staticmethod
    def _engine(state):
        engine=LiftSession.from_snapshot(state['snapshot'])
        engine.history=[]
        for entry in state.get('history',[]):
            entry=list(copy.deepcopy(entry));entry[2]=tuple(entry[2]) if entry[2] is not None else None;engine.history.append(tuple(entry))
        return engine

    @staticmethod
    def _store_engine(state,engine):state.update(snapshot=engine.snapshot(),history=engine.history)

    @staticmethod
    def _suggestions(engine,state):
        """Visible grey carry and its origin; these are never observed inputs."""
        output={};pending=state.get('pending',{});last=state.get('last_session',{})
        for e,s in engine.all_slots():
            slot=(e,s);typed=engine.recorded(slot) or pending.get(f'{e}:{s}',{});line=engine.plan[e]
            previous=next((engine.recorded((e,n)) for n in range(s-1,0,-1) if engine.recorded((e,n))),None)
            earlier=output.get(f'{e}:{previous["set_index"]}',{}) if previous else {}
            prior=last.get(str(e),{}).get(str(s),{})
            values={};origins={}
            for key,target in (('weight_kg','target_weight_kg'),('reps','target_reps_low')):
                choices=[(typed.get(key),'typed'),(earlier.get(key),'previous_set'),(prior.get(key),'last_session'),(line.get(target),'program')]
                values[key],origins[key]=next(((v,source) for v,source in choices if v is not None),(None,'missing'))
            output[f'{e}:{s}']=values|dict(provenance=origins)
        return output

    def _status(self,state):
        now=_now()
        if not state:return dict(active=False,paused=False,restarted=False,server_time=now,state=None,rest_remaining_seconds=None,program_id=None)
        engine=self._engine(state)
        return dict(active=True,paused=bool(state.get('paused')),restarted=bool(state.get('restarted')),server_time=now,state=engine.snapshot(),rest_remaining_seconds=engine.rest_remaining(now) if not state.get('paused') else None,program_id=state['program_id'],interrupted_slot=state.get('interrupted_slot'),pending_values=state.get('pending',{}),suggested_values=self._suggestions(engine,state))

    def lift_status(self):
        with self.lock,closing(self.features.store.connect()) as conn,self._binding():return self._status(self._load(conn))

    @staticmethod
    def _plan_line(raw,index,program_id):
        if not isinstance(raw,dict):raise ValueError('Program exercise must be an object')
        exercise=raw.get('exercise')
        if not isinstance(exercise,str) or not exercise.strip() or len(exercise)>300:raise ValueError('Supply an exercise name')
        out=copy.deepcopy(raw)|dict(exercise=exercise.strip(),program_item_id=raw.get('id',f'{program_id}:{index}'))
        out['target_sets']=_num(raw.get('target_sets') if raw.get('target_sets') is not None else 1,1,20,'target_sets',True)
        out['rest_seconds']=_num(raw.get('rest_seconds',raw.get('rest_sec',120)) if raw.get('rest_seconds',raw.get('rest_sec')) is not None else 120,0,3600,'rest_seconds',True)
        aliases={'target_reps_low':('target_reps_low','target_reps'),'target_reps_high':('target_reps_high',),'target_rpe':('target_rpe','target_max_rpe'),'target_weight_kg':('target_weight_kg',)}
        for field,names in aliases.items():
            value=next((raw.get(k) for k in names if raw.get(k) is not None),None)
            if value is not None:out[field]=_num(value,0,10 if field=='target_rpe' else 2000 if field=='target_weight_kg' else 1000,field,field.startswith('target_reps'))
        return out

    def _last_session(self,conn,plan,start):
        result={}
        raw=conn.execute("SELECT payload_json FROM feature_records WHERE kind='lifting_set' AND deleted_ms IS NULL ORDER BY updated_ms DESC")
        rows=[json.loads(r[0]) for r in raw]
        for index,line in enumerate(plan):
            eligible=[r for r in rows if r.get('device') in (None,'',self._device()) and r.get('exercise')==line['exercise'] and finite(r.get('start_ms')) and r['start_ms']<start*1000 and r.get('session_id') and finite(r.get('set_index'))]
            if not eligible:continue
            session=max(eligible,key=lambda r:r['start_ms'])['session_id']
            result[str(index)]={str(int(r['set_index'])):{k:r.get(k) for k in ('weight_kg','reps')} for r in eligible if r['session_id']==session}
        return result

    def lift_action(self,body):
        if not isinstance(body,dict):raise ValueError('Lift action must be an object')
        with self.lock,closing(self.features.store.connect()) as conn,conn,self._binding():
            conn.execute('BEGIN IMMEDIATE');action=body.get('action');state=self._load(conn);now=int(_now())
            if action=='start':
                if state:raise ValueError('A lift session already exists; finish or discard it first')
                row=conn.execute("SELECT * FROM feature_records WHERE id=? AND kind='lifting_program' AND deleted_ms IS NULL",(body.get('program_id'),)).fetchone()
                if row is None:raise ValueError('Lifting program not found')
                program=self.features._record(row)
                if program.get('device') not in (None,'',self._device()):raise ValueError('Program belongs to another device')
                lines=program.get('lines',program.get('items',[]))
                if not isinstance(lines,list) or not 1<=len(lines)<=200:raise ValueError('Program requires1..200 ordered exercises')
                plan=[self._plan_line(line,i,program['id']) for i,line in enumerate(lines)];engine=LiftSession(plan,now)
                state=dict(program_id=program['id'],program=program,pending={},pending_history=[],last_session=self._last_session(conn,plan,now),paused=False,restarted=False,instance=self.instance,history=[],snapshot=engine.snapshot())
            else:
                if not state:raise ValueError('No live lifting session')
                engine=self._engine(state)
                if action in ('discard','stop'):
                    if body.get('confirm') is not True:raise ValueError('Confirm discarding the live sheet')
                    self._persist(conn,None);return self._status(None)|dict(discarded=True)
                if action in ('pause','resume'):
                    if action=='resume' and not state.get('paused'):raise ValueError('Session is already running')
                    if action=='pause' and state.get('paused'):raise ValueError('Session is already paused')
                    if engine.stage=='working':state['interrupted_slot']=list(engine.slot)
                    if engine.stage=='resting':
                        record=engine.recorded(engine.slot)
                        if record and record['rest_seconds'] is None:record['rest_seconds']=max(0,now-engine.stage_started_at) if not state.get('restarted') else None
                    # Never span an unobserved pause with a performed set.
                    engine.stage='warmup';engine.slot=None;engine.ends_at=None;engine.stage_started_at=now
                    state.update(paused=action=='pause',instance=self.instance,history=[],pending_history=[])
                    engine.history=[]
                else:
                    if state.get('paused'):raise ValueError('Session restored/paused; resume before changing the sheet')
                    old_history=len(engine.history);old_pending=copy.deepcopy(state['pending'])
                    if action in ('start_set','update_set'):
                        slot=(_num(body.get('exercise_index'),0,len(engine.plan)-1,'exercise_index',True),_num(body.get('set_index'),1,20,'set_index',True))
                        if slot not in engine.all_slots():raise ValueError('Set is not on the live sheet')
                    if action=='advance':
                        previous=engine.slot;working=engine.stage=='working';engine.advance(now)
                        if working:
                            pending=state['pending'].pop(f'{previous[0]}:{previous[1]}',None)
                            if pending:engine.update_set(previous,**pending)
                    elif action=='start_set':engine.start(slot,now)
                    elif action=='update_set':
                        previous=engine.recorded(slot) or state['pending'].get(f'{slot[0]}:{slot[1]}',{})
                        values={}
                        for key,hi in (('weight_kg',2000),('reps',1000),('rpe',10)):
                            values[key]=(_num(body[key],0,hi,key,key=='reps') if body[key] is not None else None) if key in body else previous.get(key)
                        if 'is_warmup' in body and type(body['is_warmup']) is not bool:raise ValueError('is_warmup must be boolean')
                        values['is_warmup']=body.get('is_warmup',previous.get('is_warmup',False))
                        if not engine.update_set(slot,**values):state['pending'][f'{slot[0]}:{slot[1]}']=values
                    elif action=='undo':
                        if not engine.undo():raise ValueError('No live-sheet action to undo')
                        pending_history=state.setdefault('pending_history',[])
                        if pending_history:state['pending']=pending_history.pop()
                    elif action in ('add_set','remove_set'):
                        index=_num(body.get('exercise_index'),0,len(engine.plan)-1,'exercise_index',True)
                        if not getattr(engine,action)(index):raise ValueError('Cannot change this set count')
                    elif action=='add_exercise':
                        line=self._plan_line(body.get('exercise_item',body),len(engine.plan),state['program_id']);line['program_item_id']=uuid.uuid4().hex
                        if not engine.add_exercise(line):raise ValueError('Cannot add an exercise to this sheet')
                    elif action=='finish':
                        if now<=engine.start_ts:raise ValueError('Session end must follow its recorded start')
                        for field in ('keep_plan_changes','complete_unstarted'):
                            if field in body and type(body[field]) is not bool:raise ValueError(f'{field} must be boolean')
                        engine.finish(now)
                        last={int(e):{int(s):v for s,v in items.items()} for e,items in state.get('last_session',{}).items()}
                        pending={tuple(map(int,key.split(':'))):v for key,v in state['pending'].items()}
                        sets=engine.finish_sets(body.get('complete_unstarted',False),last,pending)
                        performed=any(s['reps']!=0 for s in sets)
                        if not performed:raise ValueError('No performed/explicitly completed sets; discard the sheet instead')
                        workout_id=uuid.uuid4().hex;settings=self._settings();_,tz=self.analytics._timezone(settings);day=dt.datetime.fromtimestamp(now,tz).date().isoformat();profile=profile_for_day(settings,day)
                        metrics=manual_workout_rescore(self._samples(engine.start_ts,now+1),profile,settings.get('hr_max'),settings.get('hr_rest'),settings.get('effort_method','edwards'))
                        workout=dict(id=workout_id,device=self._device(),day=day,start_ms=engine.start_ts*1000,end_ms=now*1000,start=engine.start_ts,end=now,sport='Strength Training',source='manual',source_category='manual',duration_seconds=max(0,now-engine.start_ts),lift_program_id=state['program_id'],lift_session_id=workout_id,restarted=state.get('restarted',False),profile_provenance=settings.get('profile_provenance',{}),timing_note='Only completed sets have observed set timestamps; unstarted completions are user-declared')
                        if metrics['value']:workout.update(metrics['value'])
                        writes=[('workout',workout)]
                        suggestions=self._suggestions(engine,state)
                        for item in sets:
                            line=engine.plan[item['exercise_index']]
                            typed=engine.recorded((item['exercise_index'],item['set_index'])) or pending.get((item['exercise_index'],item['set_index']),{})
                            origins=suggestions[f'{item["exercise_index"]}:{item["set_index"]}']['provenance']
                            discarded=item['start_ts'] is None and not body.get('complete_unstarted',False)
                            writes.append(('lifting_set',dict(item,device=self._device(),day=day,session_id=workout_id,workout_id=workout_id,exercise=line['exercise'],primary_muscle=line.get('primary_muscle'),secondary_muscles=line.get('secondary_muscles',[]),start_ms=item['start_ts']*1000 if item['start_ts'] is not None else None,end_ms=item['end_ts']*1000 if item['end_ts'] is not None else None,source='manual',source_category='manual',value_provenance=dict(weight='discarded' if discarded else origins['weight_kg'],reps='discarded' if discarded else origins['reps'],rpe='planned' if item.get('rpe_is_planned') else 'typed' if item.get('rpe') is not None else 'missing',timing='observed' if item['start_ts'] is not None else 'not_observed'))))
                        original=conn.execute("SELECT * FROM feature_records WHERE id=? AND kind='lifting_program' AND deleted_ms IS NULL",(state['program_id'],)).fetchone()
                        if original is not None:
                            program=self.features._record(original);raw_lines=program.get('lines',program.get('items',[]));program_lines=[dict(raw,id=raw.get('id',f'{program["id"]}:{i}')) for i,raw in enumerate(raw_lines)]
                            updated=after_session_program(sets,engine.plan,program_lines,body.get('keep_plan_changes',False))
                            if updated!=program_lines:
                                program['lines']=updated;writes.append(('lifting_program',program))
                        op=self._operation(conn,day,writes,[]);self._persist(conn,None);self.analytics._cache.clear()
                        return self._status(None)|dict(saved=True,workout_id=workout_id,**op)
                    else:raise ValueError('Unknown lifting action')
                    if len(engine.history)>old_history:state.setdefault('pending_history',[]).append(old_pending)
            self._store_engine(state,engine);state['instance']=self.instance;self._persist(conn,state)
            return self._status(state)
