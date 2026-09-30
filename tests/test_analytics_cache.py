"""Functional cache invalidation/isolation over temporary captured data only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analytics import AnalyticsService
from features import FeatureStore
from storage import Store
from workout_service import WorkoutService
from types import SimpleNamespace


class AnalyticsCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'fixture.sqlite');self.features=FeatureStore(self.store);self.service=AnalyticsService(self.store)
        self.settings=dict(timezone='UTC',day_cycle_mode='midnight',hr_max=190,hr_rest=60)
        self.clock=patch('analytics.time.time',return_value=1790812800);self.clock.start();self.addCleanup(self.clock.stop)
    def day(self,device='owned',date='2026-10-01',**settings):
        return self.service.trends(device,1,self.settings|settings,through=date)['days'][0]
    def test_import_and_record_edit_delete_refresh_within_same_time_bucket(self):
        self.assertIsNone(self.day()['resting_hr']['value'])
        imported=self.features.save_record('daily_metric',dict(day='2026-10-01',source='oura',source_category='imported',resting_hr=53,hrv_ms=61))
        self.assertEqual(self.day()['resting_hr']['value'],53)
        self.assertEqual(self.day()['hrv']['value'],61)
        imported['resting_hr']=57;self.features.save_record('daily_metric',imported)
        self.assertEqual(self.day()['resting_hr']['value'],57)
        self.features.delete_record('daily_metric',imported['id'])
        self.assertIsNone(self.day()['resting_hr']['value'])

    def test_device_date_settings_and_consumer_mutation_remain_isolated(self):
        self.features.save_record('hydration',dict(device='owned',day='2026-10-01',amount_ml=500))
        first=self.day();self.assertEqual(first['hydration']['value'],500)
        first['hydration']['value']=999;first['source_records'].clear()
        self.assertEqual(self.day()['hydration']['value'],500);self.assertEqual(len(self.day()['source_records']),1)
        self.assertIsNone(self.day(device='other')['hydration']['value'])
        self.assertIsNone(self.day(date='2026-09-30')['hydration']['value'])
        self.assertEqual(self.day(effort_scale='whoop')['effort']['display_max'],21)
        self.assertEqual(self.day()['effort']['display_max'],100)

    def test_simultaneous_cold_requests_share_work_but_return_independent_values(self):
        self.features.save_record('hydration',dict(device='owned',day='2026-10-01',amount_ml=500))
        with patch.object(self.service,'_trends_uncached',wraps=self.service._trends_uncached) as expensive:
            with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(lambda _:self.day(),range(4)))
            self.assertEqual(expensive.call_count,1)
        results[0]['hydration']['value']=1
        self.assertEqual([r['hydration']['value'] for r in results[1:]],[500,500,500])

    def test_dismiss_undo_then_different_dismiss_cannot_reuse_stale_generation(self):
        midnight=int(dt.datetime(2026,10,1,tzinfo=dt.timezone.utc).timestamp())
        with closing(self.store.connect()) as conn,conn:
            for offset in (36000,39600):
                for second in range(601):
                    t=(midnight+offset+second)*1000
                    cur=conn.execute('INSERT INTO frames(device,received_ms,characteristic,packet_type,digest,raw) VALUES(?,?,?,?,?,?)',('owned',t,'fixture',49,str(t),b''))
                    conn.execute('INSERT INTO readings(frame_id,device,kind,timestamp_ms,received_ms,device_seconds,hr,rr_json,contact,gx,gy,gz) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(cur.lastrowid,'owned','history',t,t,t//1000,150,'[]',1,float(second%2),0,1))
        manager=SimpleNamespace(address='owned');workouts=WorkoutService(manager,self.features,self.service)
        self.features.update_settings(self.settings)
        visible=workouts.detail({'date':'2026-10-01'})['workouts'];self.assertEqual(len(visible),2)
        one,two=visible
        operation=workouts.edit(dict(action='dismiss',date='2026-10-01',id=one['id']))
        self.assertEqual([r['id'] for r in workouts.detail({'date':'2026-10-01'})['workouts']],[two['id']])
        workouts.edit(dict(action='undo',operation_id=operation['operation_id']))
        workouts.edit(dict(action='dismiss',date='2026-10-01',id=two['id']))
        self.assertEqual([r['id'] for r in workouts.detail({'date':'2026-10-01'})['workouts']],[one['id']])


if __name__=='__main__':unittest.main()
