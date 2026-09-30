"""Temporary-database business tests: no strap or user database is touched."""
import datetime as dt
from contextlib import closing
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from analytics import AnalyticsService
from features import FeatureStore
from storage import Store
from workout_service import WorkoutService


class FixtureAnalytics:
    """Source rows resolved from committed records, actual supplied HR samples."""
    def __init__(self,features):self.features=features;self._cache={};self.detected=[];self.samples=[]
    def day(self,device,date,settings):
        date=date or '2026-10-01';rows=[]
        for r in self.features.list_records('workout',{'limit':100000}):
            if r.get('day')!=date:continue
            rows.append(r|dict(start=r['start_ms']/1000,end=r['end_ms']/1000))
        with closing(self.features.store.connect()) as conn:
            tokens=list(conn.execute('SELECT start_ms,end_ms FROM workout_dismissals WHERE device=?',(device,)))
        rows += [r for r in self.detected if not any(r['start']*1000<end and start<r['end']*1000 for start,end in tokens)]
        return dict(day=date,workouts={'value':rows},provenance={'hr_max_bpm':200,'effort_resting_hr_bpm':60})
    def _read(self,device,start,end):return [(t,h) for t,h in self.samples if start<=t<end],[],[]
    def _streams(self,rows,sensors):return rows,[],[]
    _timezone=staticmethod(AnalyticsService._timezone)


class WorkoutServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'fixture.sqlite');self.features=FeatureStore(self.store)
        self.manager=SimpleNamespace(address='owned');self.analytics=FixtureAnalytics(self.features)
        self.service=WorkoutService(self.manager,self.features,self.analytics)
        self.clock=10000;self.patcher=patch('workout_service._now',lambda:self.clock);self.patcher.start();self.addCleanup(self.patcher.stop)
    def save(self,kind,**values):return self.features.save_record(kind,values)
    def workouts(self):return self.features.list_records('workout',{'limit':100000})
    def workout(self,id,start,end,**extra):
        return self.save('workout',id=id,device='owned',day='2026-10-01',start_ms=start*1000,end_ms=end*1000,sport='Running',source='manual',**extra)
    def program(self):return self.save('lifting_program',id='plan',lines=[dict(exercise='Squat',target_sets=2,target_weight_kg=40,target_reps=8,rest_sec=90,target_max_rpe=7)])
    def lift(self,action,**values):return self.service.lift_action(dict(action=action,**values))

    def test_merge_literal_duration_weighted_hr_and_reversible_saved_records(self):
        self.workout('one',1000,4600,calories=600,avg_hr=150,peak_hr=178,duration_seconds=3600)
        self.workout('two',5000,7400,calories=300,avg_hr=120,peak_hr=160,duration_seconds=2400)
        op=self.service.edit(dict(action='merge',date='2026-10-01',ids=['one','two']))
        merged=self.workouts();self.assertEqual(len(merged),1)
        self.assertEqual((merged[0]['duration_seconds'],merged[0]['calories'],merged[0]['avg_hr']), (6000,900,138))
        self.assertEqual((merged[0]['start_ms'],merged[0]['end_ms']),(1000000,7400000))
        self.analytics._cache['stale']=True
        self.service.edit(dict(action='undo',operation_id=op['operation_id']))
        self.assertEqual({r['id'] for r in self.workouts()},{'one','two'});self.assertEqual(self.analytics._cache,{})
        with self.assertRaises(ValueError):self.service.edit(dict(action='undo',operation_id=op['operation_id']))

    def test_import_foreign_device_and_newer_edit_are_protected(self):
        self.workout('one',1000,2000,calories=1)
        self.save('workout',id='import',day='2026-10-01',start_ms=3000000,end_ms=4000000,source='whoop',source_category='imported')
        self.save('workout',id='foreign',device='other',day='2026-10-01',start_ms=5000000,end_ms=6000000,source='manual')
        got=self.service.detail({'date':'2026-10-01'})
        self.assertEqual({r['id'] for r in got['workouts']},{'one','import'})
        self.assertFalse(next(r for r in got['workouts'] if r['id']=='import')['editable'])
        with self.assertRaisesRegex(ValueError,'read-only'):self.service.edit(dict(action='merge',date='2026-10-01',ids=['one','import']))
        with self.assertRaises(ValueError):self.service.edit(dict(action='merge',date='2026-10-01',ids=['one','foreign']))
        self.workout('two',2200,3000,calories=2)
        op=self.service.edit(dict(action='merge',date='2026-10-01',ids=['one','two']))
        changed=self.workouts()[0];changed['calories']=200;self.features.save_record('workout',changed)
        with self.assertRaisesRegex(ValueError,'later edit'):self.service.edit(dict(action='undo',operation_id=op['operation_id']))

    def test_detected_dismiss_bound_persistent_and_undo(self):
        self.analytics.detected=[dict(id='detected:1000000:2000000',start=1000,end=2000,detected=True,source='detected',sport='Running')]
        op=self.service.edit(dict(action='dismiss',date='2026-10-01',id=self.analytics.detected[0]['id']))
        self.assertEqual(self.service.detail({'date':'2026-10-01'})['workouts'],[])
        self.manager.address='other';self.assertEqual(len(self.service.detail({'date':'2026-10-01'})['workouts']),1)
        with self.assertRaises(ValueError):self.service.edit(dict(action='undo',operation_id=op['operation_id']))
        self.manager.address='owned';self.service.edit(dict(action='undo',operation_id=op['operation_id']))
        self.assertEqual(len(self.service.detail({'date':'2026-10-01'})['workouts']),1)

    def test_hrr_literal_post_workout_drops_and_sparse_coverage(self):
        self.workout('one',9700,10000)
        self.analytics.samples=[(t,170 if t>=9970 else 145) for t in range(9700,10001)]
        self.analytics.samples += [(10000+m*60+i,h) for m,h in [(1,146),(2,132),(5,112)] for i in [-1,0,1]]
        got=self.service.detail(dict(date='2026-10-01',id='one'))['detail']
        self.assertEqual(got['hrr']['value'],dict(end_hr_bpm=170,after_1_minute_bpm=24,after_2_minute_bpm=38,after_5_minute_bpm=58))
        self.assertEqual(got['coverage']['post_workout_samples'],9)
        self.analytics.samples=[];self.assertIsNone(self.service.detail(dict(date='2026-10-01',id='one'))['detail']['hrr']['value'])

    def test_lift_pending_values_rest_undo_and_restart_pause(self):
        self.program();self.lift('start',program_id='plan');self.clock=10010;self.lift('advance')
        self.lift('update_set',exercise_index=0,set_index=1,weight_kg=55,reps=9)
        self.lift('update_set',exercise_index=0,set_index=1,rpe=8)
        self.clock=10040;got=self.lift('advance')
        self.assertEqual(got['rest_remaining_seconds'],90);self.assertEqual(got['state']['sets'][0]['weight_kg'],55)
        self.lift('undo');self.assertEqual(self.service.lift_status()['pending_values']['0:1']['reps'],9)
        self.lift('advance');self.clock=10070;self.lift('advance')
        self.assertEqual(self.service.lift_status()['state']['sets'][0]['rest_seconds'],30)
        restarted=WorkoutService(self.manager,self.features,self.analytics);got=restarted.lift_status()
        self.assertTrue(got['paused']);self.assertTrue(got['restarted'])
        with self.assertRaisesRegex(ValueError,'resume'):restarted.lift_action(dict(action='advance'))
        self.clock=10150;got=restarted.lift_action(dict(action='resume'))
        self.assertEqual(got['state']['stage'],'warmup');self.assertEqual(got['interrupted_slot'],[0,2])
        self.assertEqual(len(got['state']['sets']),1)

    def test_finish_atomic_sets_program_provenance_and_undo(self):
        self.program();self.lift('start',program_id='plan');self.clock=10010;self.lift('advance')
        self.lift('update_set',exercise_index=0,set_index=1,weight_kg=55,reps=9)
        self.clock=10040;self.lift('advance');self.clock=10060;op=self.lift('finish')
        self.assertFalse(op['active']);self.assertTrue(op['saved'])
        rows=self.features.list_records('lifting_set',{'limit':100});self.assertEqual(len(rows),2)
        done=next(r for r in rows if r['set_index']==1);skipped=next(r for r in rows if r['set_index']==2)
        self.assertEqual((done['start_ms'],done['end_ms'],done['reps']),(10010000,10040000,9))
        self.assertEqual(done['value_provenance']['rpe'],'planned');self.assertEqual(done['value_provenance']['weight'],'typed')
        self.assertEqual(skipped['reps'],0);self.assertIsNone(skipped['start_ms'])
        line=self.features.list_records('lifting_program',{'limit':100})[0]['lines'][0]
        self.assertEqual(line['target_weight_kg'],55);self.assertEqual(line['target_reps_low'],9)
        self.service.edit(dict(action='undo',operation_id=op['operation_id']))
        self.assertEqual(self.workouts(),[]);self.assertEqual(self.features.list_records('lifting_set',{'limit':100}),[])
        self.assertEqual(self.features.list_records('lifting_program',{'limit':100})[0]['lines'][0]['target_weight_kg'],40)

    def test_finish_failure_rolls_back_records_and_live_sheet(self):
        self.program();self.lift('start',program_id='plan');self.clock=10010;self.lift('advance');self.clock=10040;self.lift('advance')
        original=self.features._save;calls=[]
        def fail(conn,kind,payload,*args,**kwargs):
            calls.append(kind)
            if len(calls)==2:raise RuntimeError('injected storage failure')
            return original(conn,kind,payload,*args,**kwargs)
        self.clock=10060
        with patch.object(self.features,'_save',fail),self.assertRaises(RuntimeError):self.lift('finish')
        self.assertEqual(self.workouts(),[]);self.assertEqual(self.features.list_records('lifting_set',{'limit':100}),[])
        self.assertTrue(self.service.lift_status()['active']);self.assertEqual(self.service.lift_status()['state']['stage'],'resting')
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM workout_edits').fetchone()[0],0)

    def test_validation_and_explicit_unstarted_completion_has_no_timing(self):
        self.program();self.lift('start',program_id='plan')
        for body in [dict(action='update_set',exercise_index=0,set_index=1,reps=-1),dict(action='update_set',exercise_index=0,set_index=3,reps=1),dict(action='stop')]:
            with self.assertRaises(ValueError):self.service.lift_action(body)
        self.clock=10060
        with self.assertRaisesRegex(ValueError,'No performed'):self.lift('finish')
        self.lift('finish',complete_unstarted=True)
        rows=self.features.list_records('lifting_set',{'limit':100});self.assertEqual([r['reps'] for r in rows],[8,8])
        self.assertTrue(all(r['start_ms'] is None and r['value_provenance']['timing']=='not_observed' for r in rows))

    def test_real_canonical_offline_saved_metrics_and_device_binding(self):
        self.workout('one',1790805600,1790806800,calories=123,effort=8.5,duration_seconds=1000)
        service=WorkoutService(self.manager,self.features,AnalyticsService(self.store))
        got=service.detail({'date':'2026-10-01'})['workouts']
        self.assertEqual(len(got),1);self.assertEqual((got[0]['calories'],got[0]['effort'],got[0]['duration_seconds']),(123,8.5,1000))
        self.assertEqual((got[0]['source_kind'],got[0]['editable']),('manual',True))

    def test_active_device_switch_rolls_back_atomic_edit(self):
        self.workout('one',1000,2000);self.workout('two',2200,3000)
        original=self.features._save
        def switch(conn,kind,payload,*args,**kwargs):
            result=original(conn,kind,payload,*args,**kwargs);self.manager.address='other';return result
        with patch.object(self.features,'_save',switch),self.assertRaisesRegex(ValueError,'Active device changed'):
            self.service.edit(dict(action='merge',date='2026-10-01',ids=['one','two']))
        self.assertEqual({r['id'] for r in self.workouts()},{'one','two'})
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM workout_edits').fetchone()[0],0)

    def test_relabel_detection_failure_does_not_persist_tombstone(self):
        self.analytics.detected=[dict(id='detected:1000000:2000000',start=1000,end=2000,detected=True,source='detected',sport='Running')]
        with patch.object(self.features,'_save',side_effect=RuntimeError('failure')),self.assertRaises(RuntimeError):
            self.service.edit(dict(action='relabel',date='2026-10-01',id=self.analytics.detected[0]['id'],sport='Walking'))
        self.assertEqual(self.workouts(),[])
        with closing(self.store.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM workout_dismissals').fetchone()[0],0)
        got=self.service.edit(dict(action='relabel',date='2026-10-01',id=self.analytics.detected[0]['id'],sport='Walking'))
        self.assertEqual(self.service.detail({'date':'2026-10-01'})['workouts'][0]['source_kind'],'manual')
        self.service.edit(dict(action='undo',operation_id=got['operation_id']))
        self.assertEqual(self.service.detail({'date':'2026-10-01'})['workouts'][0]['source_kind'],'detected')

    def test_last_session_carry_is_visible_and_never_observed_rpe(self):
        self.program()
        self.save('lifting_set',device='owned',session_id='prior',exercise='Squat',set_index=1,start_ms=9000000,end_ms=9030000,weight_kg=52,reps=6,rpe=9)
        started=self.lift('start',program_id='plan')
        self.assertEqual(started['suggested_values']['0:1'],dict(weight_kg=52,reps=6,provenance={'weight_kg':'last_session','reps':'last_session'}))
        self.clock=10010;self.lift('advance');self.clock=10040;self.lift('advance');self.clock=10060;self.lift('finish')
        done=next(r for r in self.features.list_records('lifting_set',{'limit':100}) if r.get('workout_id') and r['set_index']==1)
        self.assertEqual((done['weight_kg'],done['reps'],done['rpe']),(52,6,7))
        self.assertEqual(done['value_provenance'],dict(weight='last_session',reps='last_session',rpe='planned',timing='observed'))


if __name__=='__main__':unittest.main()
