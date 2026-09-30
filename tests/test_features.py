import io
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from features import FeatureStore, KINDS, record_stream_sample
from storage import Store
from whoop_protocol import STANDARD_HR


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "local.sqlite")
        self.features = FeatureStore(self.store)
        self.store.save([("owned",1700000000000,STANDARD_HR,bytes([0,75]))])

    def tearDown(self):
        self.temp.cleanup()

    def count(self):
        return self.store.summary()["readings"]

    def test_record_durability_edit_delete_undo_all_kinds(self):
        for kind in KINDS:
            row = self.features.save_record(kind,{"notes":"original"})
            self.features.save_record(kind,{"id":row["id"],"notes":"edited"})
            self.features.delete_record(kind,row["id"])
            self.assertEqual([],self.features.list_records(kind))
            self.assertEqual("edited",self.features.undo_record(kind,row["id"])["notes"])
            self.assertEqual("original",self.features.undo_record(kind,row["id"])["notes"])
        reopened = FeatureStore(self.store.path)
        self.assertEqual("original",reopened.list_records("sleep")[0]["notes"])
        self.assertEqual(1,self.count())

    def test_typed_settings_and_credentials(self):
        self.features.update_settings({"weight_kg":78.5,"hr_max":185})
        self.assertEqual(78.5,FeatureStore(self.store.path).settings()["weight_kg"])
        for bad in ({"weight_kg":True},{"external_push_enabled":1},{"token":"secret"},{"hr_max":400}):
            with self.assertRaises(ValueError):
                self.features.update_settings(bad)
        with self.assertRaises(ValueError):
            self.features.save_record("journal",{"details":{"api_key":"x"}})

    def test_actual_noop_whoop_csv_fixtures_dedup_and_source_preservation(self):
        fixtures = Path(__file__).parents[1]/"vendor/noop/test-fixtures"
        for name,kind in (("sleeps.csv","sleep"),("workouts.csv","workout"),("journal_entries.csv","journal"),("physiological_cycles.csv","daily_metric")):
            data = (fixtures/name).read_bytes()
            preview = self.features.import_preview(name,data)
            self.assertGreater(preview["count"],0)
            result = self.features.import_apply(name,data)
            self.assertTrue(Path(result["backup"]).exists())
            self.assertEqual(preview["count"],result["imported"])
            self.assertEqual(0,self.features.import_apply("renamed.csv",data)["imported"])
            self.assertTrue(self.features.list_records(kind)[0]["original"])
        sleep = self.features.list_records("sleep")[-1]
        # Fixture UTC+1 is converted explicitly, not interpreted in host timezone.
        self.assertEqual(1704147300000,sleep["start_ms"])
        self.assertEqual(1,self.count())

    def test_apple_health_routes_nutrition_and_lifting(self):
        fixtures = Path(__file__).parents[1]/"vendor/noop/test-fixtures"
        data = (fixtures/"sample_health_data.xml").read_bytes()
        self.assertGreater(self.features.import_apply("export.xml",data)["imported"],0)
        self.features.import_apply("nutrition.csv",b"Date,Energy (kcal),Protein (g),Carbs (g),Fat (g)\n2026-01-01,2000,120,220,60\n")
        self.assertEqual(120,self.features.list_records("nutrition")[0]["protein_g"])
        self.features.import_apply("hevy.csv",b"title,start_time,exercise_title,weight_lb,reps\nPush,2026-01-01 10:00:00,Bench,100,8\n")
        self.assertAlmostEqual(45.359237,self.features.list_records("lifting_set")[0]["weight_kg"])
        self.features.import_apply("route.gpx",b'<gpx><trk><trkseg><trkpt lat="-27" lon="153"><time>2026-01-01T00:00:00Z</time></trkpt></trkseg></trk></gpx>')
        self.assertEqual(1,len(self.features.list_records("route")[0]["points"]))

    def test_raw_capture_uses_native_store_dedup(self):
        data = json.dumps([dict(hex="004c",char=STANDARD_HR,ts_ms=1700000001000,device="owned")]).encode()
        self.assertEqual(1,self.features.import_apply("capture.json",data)["imported"])
        self.assertEqual(0,self.features.import_apply("capture.json",data)["imported"])
        self.assertEqual(2,self.count())

    def test_full_backup_merge_restores_readings_without_id_overwrite(self):
        row = self.features.save_record("journal",{"notes":"before"})
        self.features.save_record("journal",{"id":row["id"],"notes":"after"})
        self.features.update_settings({"name":"Backup name","external_push_enabled":True})
        self.features.save_record("alarm",{"enabled":True,"time":"07:00"})
        destination = self.folder/"backup.boopbak"
        self.features.backup_export(destination)
        other = FeatureStore(self.folder/"other.sqlite")
        other.store.save([("existing",1700000002000,STANDARD_HR,bytes([0,90]))])
        other.update_settings({"name":"Keep name"})
        result = other.backup_restore(destination.name,destination.read_bytes())
        self.assertEqual(2,other.store.summary()["readings"])
        self.assertEqual("Keep name",other.settings()["name"])
        self.assertFalse(other.settings()["external_push_enabled"])
        self.assertFalse(other.list_records("alarm")[0]["enabled"])
        self.assertEqual("before",other.undo_record("journal",row["id"])["notes"])
        # Restoring again cannot rewrite the local undo or add conflicting revisions.
        other.backup_restore(destination.name,destination.read_bytes())
        self.assertEqual("before",other.list_records("journal")[0]["notes"])
        self.assertEqual(2,other.store.summary()["readings"])
        self.assertTrue(Path(result["backup"]).exists())

    def full_app_fixture(self):
        from analytics import AnalyticsService
        from coach_history import CoachHistory
        from workout_service import WorkoutService
        self.features.update_settings({'timezone':'UTC','notifications_enabled':True,'start_with_windows':True,'auto_sync_minutes':1})
        history=CoachHistory(self.features)
        with patch('coach_history.time.time',return_value=1700000100):history.append('Archived question',{'answer':'Archived answer','provider':'offline'})
        service=WorkoutService(SimpleNamespace(address='owned'),self.features,AnalyticsService(self.store))
        for id,start,end,kcal in [('a',1700000000,1700000600,100),('b',1700001000,1700001600,120)]:
            self.features.save_record('workout',dict(id=id,device='owned',day='2023-11-14',start_ms=start*1000,end_ms=end*1000,sport='Running',source='manual',calories=kcal))
        merged=service.edit(dict(action='merge',date='2023-11-14',ids=['a','b']))
        self.features.save_record('lifting_program',dict(id='plan',device='owned',lines=[dict(exercise='Squat',target_sets=2,target_reps=8,target_weight_kg=40)]))
        with patch('workout_service._now',return_value=1700002000):service.lift_action(dict(action='start',program_id='plan'))
        with patch('workout_service._now',return_value=1700002010):service.lift_action(dict(action='advance'))
        with closing(self.store.connect()) as conn,conn:
            frame=conn.execute('SELECT id FROM frames LIMIT 1').fetchone()[0]
            conn.execute('INSERT INTO sensors VALUES(?,?,?,?,?,?)',(frame,'owned',1700000000000,1700000000000,'fixture',json.dumps({'skin_temp_raw':930,'ppg_green':2345,'gx':.1,'gy':0,'gz':1})))
            conn.execute('INSERT INTO sensor_migrations VALUES(1,?)',(frame,))
            conn.execute('CREATE TABLE boop_devices(address TEXT PRIMARY KEY,name TEXT NOT NULL,forgotten INTEGER NOT NULL DEFAULT0)'.replace('DEFAULT0','DEFAULT 0'))
            conn.execute("INSERT INTO boop_devices VALUES('owned','Saved strap',0)")
            conn.execute("INSERT INTO feature_imports VALUES('fixture-import','fixture.csv',1700000000000,1)")
            conn.execute("INSERT INTO workout_edits VALUES('dismiss-op','owned','2023-11-14','{}','{}',1700000000000,NULL)")
            conn.execute("INSERT INTO workout_dismissals VALUES('owned',1700003000000,1700003600000,'dismiss-op')")
            for key in ('automations','alarm_schedule','active_session','coach_brief','diagnostics_export','wind_down','request_secret'):
                conn.execute('INSERT OR REPLACE INTO boop_control_settings VALUES(?,?)',(key,json.dumps({'enabled':True,'api_key':'excluded-fixture-secret'})))
            conn.execute('CREATE TABLE plugin_credentials(secret TEXT)');conn.execute("INSERT INTO plugin_credentials VALUES('excluded-fixture-secret')")
        return merged

    def test_own_full_schema_archive_sensor_refs_coach_edits_lift_and_inert_controls(self):
        from analytics import AnalyticsService
        from coach_history import CoachHistory
        from workout_service import WorkoutService
        merged=self.full_app_fixture();destination=self.folder/'complete.boopbak';self.features.backup_export(destination)
        with zipfile.ZipFile(destination) as archive:
            self.assertFalse(b'excluded-fixture-secret' in archive.read('boop-backup.sqlite'), 'Secret remains in sanitized archive pages')
            exported=self.folder/'exported.sqlite';exported.write_bytes(archive.read('boop-backup.sqlite'))
            settings=json.loads(archive.read('settings.json'))
            for key in ('notifications_enabled','start_with_windows','auto_sync_minutes'):self.assertNotIn(key,settings)
        with closing(sqlite3.connect(exported)) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM boop_control_settings').fetchone()[0],0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='plugin_credentials'").fetchone())
        other=FeatureStore(self.folder/'complete-target.sqlite')
        other.store.save([('existing',1700000002000,STANDARD_HR,bytes([0,90]))])
        result=other.backup_restore(destination.name,destination.read_bytes())
        self.assertEqual(result['counts']['sensors'],1);self.assertEqual(result['counts']['boop_coach_messages'],2)
        with closing(other.store.connect()) as conn:
            sensor=conn.execute('SELECT * FROM sensors').fetchone()
            self.assertEqual(sensor['frame_id'],2);self.assertEqual(sensor['device'],'owned');self.assertEqual(sensor['timestamp_ms'],1700000000000)
            self.assertEqual(json.loads(sensor['values_json'])['skin_temp_raw'],930)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM feature_imports').fetchone()[0],1)
            self.assertEqual(conn.execute('SELECT name FROM boop_devices').fetchone()[0],'Saved strap')
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM boop_control_settings').fetchone()[0],0)
            lift=json.loads(conn.execute('SELECT state_json FROM lift_control').fetchone()[0]);self.assertTrue(lift['paused']);self.assertTrue(lift['restarted'])
        self.assertEqual([m['text'] for m in CoachHistory(other).history()['messages']],['Archived question','Archived answer'])
        service=WorkoutService(SimpleNamespace(address='owned'),other,AnalyticsService(other.store))
        self.assertTrue(service.lift_status()['paused']);self.assertEqual(service.lift_status()['state']['sets'],[])
        service.edit(dict(action='undo',operation_id=merged['operation_id']))
        self.assertEqual({r['id'] for r in other.list_records('workout')},{'a','b'})
        service.edit(dict(action='undo',operation_id='dismiss-op'))
        with closing(other.store.connect()) as conn:self.assertEqual(conn.execute('SELECT COUNT(*) FROM workout_dismissals').fetchone()[0],0)

    def test_restore_fills_deduplicated_frame_children_preserves_local_state_and_context(self):
        from coach_history import CoachHistory
        from datetime import datetime,timezone
        self.full_app_fixture();destination=self.folder/'merge.boopbak';self.features.backup_export(destination)
        other=FeatureStore(self.folder/'merge-target.sqlite')
        other.store.save([('owned',1700000000000,STANDARD_HR,bytes([0,75]))])
        history=CoachHistory(other)
        now=datetime(2026,10,2,10,tzinfo=timezone.utc)
        other.update_settings({'timezone':'UTC','name':'Keep local'})
        with patch('coach_history.time.time',return_value=now.timestamp()):history.append('Today question',{'answer':'Today answer','provider':'offline'})
        with closing(other.store.connect()) as conn,conn:
            conn.execute('DELETE FROM readings')
            conn.execute('CREATE TABLE lift_control(device TEXT PRIMARY KEY,state_json TEXT,updated_ms INTEGER NOT NULL)')
            conn.execute("INSERT INTO lift_control VALUES('owned','{\"local\":true}',1)")
            conn.execute("INSERT INTO boop_control_settings VALUES('local_preference','{\"keep\":true}')")
        other.backup_restore(destination.name,destination.read_bytes())
        self.assertEqual(other.store.summary()['readings'],1);self.assertEqual(other.settings()['name'],'Keep local')
        self.assertEqual([m['text'] for m in history.context(now)],['Today question','Today answer'])
        self.assertEqual(history.history()['count'],4)
        with closing(other.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sensors').fetchone()[0],1)
            self.assertEqual(json.loads(conn.execute('SELECT state_json FROM lift_control').fetchone()[0]),{'local':True})
            self.assertEqual(conn.execute("SELECT value_json FROM boop_control_settings WHERE key='local_preference'").fetchone()[0],'{"keep":true}')
        other.backup_restore(destination.name,destination.read_bytes())
        self.assertEqual(history.history()['count'],4)

    def test_invalid_sensor_restore_rolls_back_all_merged_tables(self):
        self.full_app_fixture();destination=self.folder/'invalid.boopbak';self.features.backup_export(destination)
        with zipfile.ZipFile(destination) as archive:raw=archive.read('boop-backup.sqlite')
        corrupt=self.folder/'corrupt.sqlite';corrupt.write_bytes(raw)
        with closing(sqlite3.connect(corrupt)) as conn,conn:conn.execute('UPDATE sensors SET frame_id=999999')
        other=FeatureStore(self.folder/'rollback-target.sqlite')
        with self.assertRaisesRegex(ValueError,'no original frame'):other.backup_restore('invalid.sqlite',corrupt.read_bytes())
        self.assertEqual(other.list_records('workout'),[])
        with closing(other.store.connect()) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM frames').fetchone()[0],0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='boop_coach_messages'").fetchone())

    def test_restored_edit_cannot_undo_over_conflicting_local_workout(self):
        from analytics import AnalyticsService
        from workout_service import WorkoutService
        merged=self.full_app_fixture();destination=self.folder/'collision.boopbak';self.features.backup_export(destination)
        other=FeatureStore(self.folder/'collision-target.sqlite')
        other.save_record('workout',{'id':'a','device':'owned','day':'2023-11-14','start_ms':1700000000000,'end_ms':1700000600000,'label':'Local walk','calories':42})
        other.backup_restore(destination.name,destination.read_bytes())
        service=WorkoutService(SimpleNamespace(address='owned'),other,AnalyticsService(other.store))
        with self.assertRaisesRegex(ValueError,'No active operation'):
            service.edit({'action':'undo','operation_id':merged['operation_id']})
        local=next(r for r in other.list_records('workout') if r['id']=='a')
        self.assertEqual(local['label'],'Local walk');self.assertEqual(local['calories'],42)

    def test_backup_rejects_legacy_record_credentials_without_replacing_destination(self):
        destination=self.folder/'safe.boopbak';self.features.backup_export(destination);original=destination.read_bytes()
        with closing(self.store.connect()) as conn,conn:
            conn.execute("INSERT INTO feature_records VALUES('legacy','journal',?,1,1,NULL,NULL)",(json.dumps({'api_key':'legacy-secret'}),))
        with self.assertRaisesRegex(ValueError,'Credentials'):
            self.features.backup_export(destination)
        self.assertEqual(destination.read_bytes(),original)

    def test_actual_noop_schema_and_settings_merge(self):
        noop = self.folder/"noop.sqlite"
        with closing(sqlite3.connect(noop)) as conn, conn:
            conn.executescript('CREATE TABLE device(id TEXT PRIMARY KEY); CREATE TABLE hrSample(deviceId TEXT,ts INTEGER,bpm INTEGER,synced INTEGER); CREATE TABLE journal(deviceId TEXT,day TEXT,question TEXT,answer INTEGER);')
            conn.execute("INSERT INTO device VALUES('mine')")
            conn.execute("INSERT INTO hrSample VALUES('mine',1700000000,65,0)")
            conn.execute("INSERT INTO journal VALUES('mine','2023-11-14','Coffee',1)")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,"w") as archive:
            archive.write(noop,"noop-backup.sqlite")
            archive.writestr("settings.json",json.dumps({"profile.weightKg":80.5,"profile.sex":"male","external_push_enabled":True,"api_key":"ignored"}))
        data = buf.getvalue()
        self.assertEqual("backup",self.features.import_preview("migration.noopbak",data)["format"])
        self.assertEqual(2,self.features.backup_restore("migration.noopbak",data)["imported"])
        self.assertEqual(0,self.features.backup_restore("migration.noopbak",data)["imported"])
        self.assertEqual(80.5,self.features.settings()["weight_kg"])
        self.assertFalse(self.features.settings()["external_push_enabled"])
        self.assertEqual(65,self.features.list_records("health_sample")[0]["original"]["bpm"])
        self.assertEqual(1,self.count())

    def test_imports_reject_traversal_entities_corruption_and_partial_transaction(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,"w") as archive:
            archive.writestr("../outside.csv","date,value\n2026-01-01,1")
        for name,data in (("bad.zip",buf.getvalue()),("bad.xml",b'<!DOCTYPE x [<!ENTITY a "bad">]><HealthData/>'),("capture.json",b'[{"hex":"deadbeef","char":"61080003"}]'),("bad.sqlite",b"SQLite format 3\x00junk")):
            with self.assertRaises((ValueError,sqlite3.DatabaseError)):
                self.features.import_apply(name,data)
        records = {"records":[{"kind":"journal","payload":{"notes":"valid"}},{"kind":"sleep","payload":{"start_ms":100,"end_ms":50}}]}
        with self.assertRaises(ValueError):
            self.features.import_apply("records.json",json.dumps(records).encode())
        self.assertEqual([],self.features.list_records("journal"))
        self.assertEqual(1,self.count())

    def test_record_report_round_trip(self):
        self.features.save_record("mood",{"day":"2026-01-01","value":4})
        other = FeatureStore(self.folder/"reports.sqlite")
        other.import_apply("records.json",self.features.export_records().encode())
        self.assertEqual(4,other.list_records("mood")[0]["value"])
        another = FeatureStore(self.folder/"csv.sqlite")
        another.import_apply("mood.csv",self.features.export_csv("mood").encode())
        self.assertEqual(4,another.list_records("mood")[0]["value"])

    def test_noop_compatible_csv_export_preserves_actual_source_columns(self):
        fixtures = Path(__file__).parents[1]/"vendor/noop/test-fixtures"
        self.features.import_apply("workouts.csv",(fixtures/"workouts.csv").read_bytes())
        output = self.features.export_whoop_csv("workout")
        other = FeatureStore(self.folder/"noop-csv.sqlite")
        other.import_apply("workouts.csv",output.encode())
        expected = {r["original"]["Activity name"]:r["original"] for r in self.features.list_records("workout")}
        actual = {r["original"]["Activity name"]:r["original"] for r in other.list_records("workout")}
        self.assertEqual(expected,actual)

    def test_default_profile_provenance_survives_backup(self):
        self.assertEqual("default",self.features.settings()["profile_provenance"]["hr_max"])
        path = self.folder/"defaults.boopbak"
        self.features.backup_export(path)
        preview = self.features.import_preview(path.name,path.read_bytes())
        self.assertEqual(2,preview["duplicates"])
        other = FeatureStore(self.folder/"defaults.sqlite")
        other.backup_restore(path.name,path.read_bytes())
        self.assertEqual("default",other.settings()["profile_provenance"]["hr_max"])

    def test_real_program_xlsx_fixture_and_reordered_tabs(self):
        fixtures = Path(__file__).parents[1]/"vendor/noop/test-fixtures"
        for filename in ("lift_program_filled.xlsx","lift_program_tabs_reordered.xlsx"):
            other = FeatureStore(self.folder/(filename+".sqlite"))
            result = other.import_apply(filename,(fixtures/filename).read_bytes())
            self.assertGreater(result["imported"],0)
            programs = other.list_records("lifting_program")
            self.assertTrue(any(p["name"]=="Lower A" for p in programs))
            lower = next(p for p in programs if p["name"]=="Lower A")
            if filename=="lift_program_filled.xlsx":
                self.assertEqual("Leg Press midfoot",lower["lines"][0]["exercise"])
                self.assertAlmostEqual(40.5,lower["lines"][2]["target_weight_kg"])
            else:
                self.assertEqual("Back squat",lower["lines"][0]["exercise"])
            output = other.export_lifting_xlsx()
            self.assertIn("xl/workbook.xml",zipfile.ZipFile(io.BytesIO(output)).namelist())

    def test_fit_definition_activity_samples_and_route_exports(self):
        # Same public FIT definition/data fixture layout used by NOOP's importer tests.
        records = bytearray([64,0,0,20,0,4,253,4,134,0,4,133,1,4,133,3,1,2])
        start = 1780308000-631065600
        for index in range(2):
            records.extend(bytes([0])+struct.pack("<IiiB",start+index*30,round(51.5*2**31/180),round(-.1*2**31/180),140+index))
        data = bytes([12,16,0,0])+struct.pack("<I",len(records))+b".FIT"+records+b"\x00\x00"
        result = self.features.import_apply("run.fit",bytes(data))
        self.assertEqual(4,result["imported"])
        self.assertEqual([140,141],[s["hr"] for s in self.features.stream_samples()])
        route = self.features.list_records("route")[0]
        gpx = self.features.export_route_gpx(route["id"])
        fit = self.features.export_route_fit(route["id"])
        self.assertIn(b"51.5",gpx)
        self.assertEqual(b".FIT",fit[8:12])
        copied = FeatureStore(self.folder/"fit-export.sqlite")
        copied.import_apply("export.fit",fit)
        self.assertEqual(2,len(copied.list_records("route")[0]["points"]))
        with self.assertRaises(ValueError):
            self.features.import_preview("truncated.fit",data[:-5])

    def test_oura_reference_schema_does_not_confuse_readiness_with_rhr(self):
        value = {"sleep":[{"day":"2026-06-01","bedtime_start":"2026-05-31T23:15:00Z","bedtime_end":"2026-06-01T06:30:00Z","total_sleep_duration":25200,"deep_sleep_duration":5400,"average_hrv":65,"lowest_heart_rate":48,"average_breath":14.2}],"daily_readiness":[{"day":"2026-06-01","score":81,"temperature_deviation":-.2,"contributors":{"resting_heart_rate":96}}],"daily_activity":[{"day":"2026-06-01","steps":8421,"active_calories":520}]}
        data = json.dumps(value).encode()
        self.features.import_apply("renamed.json",data)
        sleep = self.features.list_records("sleep")[0]
        self.assertEqual(420,sleep["total_sleep_min"])
        self.assertEqual(48,sleep["resting_hr"])
        readiness = next(r for r in self.features.list_records("daily_metric") if r.get("source_category")=="daily_readiness")
        self.assertEqual(81,readiness["reference_daily_readiness_score"])
        self.assertNotIn("resting_hr",readiness)
        self.assertEqual(0,self.features.import_apply("renamed.json",data)["imported"])

    def test_fitbit_and_garmin_takeout_archives(self):
        fitbit = [dict(dateOfSleep="2026-06-01",startTime="2026-05-31T23:00:00.000",endTime="2026-06-01T06:00:00.000",minutesAsleep=390,levels={"summary":{"deep":{"minutes":80}},"data":[{"dateTime":"2026-05-31T23:00:00","level":"deep","seconds":1200}]})]
        garmin = [dict(calendarDate="2026-06-01",sleepStartTimestampGMT=1780272000000,sleepEndTimestampGMT=1780297200000,deepSleepSeconds=4800,averageRespirationValue=13.7)]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf,"w") as archive:
            archive.writestr("Takeout/Fitbit/sleep-2026.json",json.dumps(fitbit))
            archive.writestr("Takeout/Fitbit/steps-2026.json",json.dumps([{"dateTime":"2026-06-01T10:00:00","value":"100"},{"dateTime":"2026-06-01T11:00:00","value":"200"}]))
            archive.writestr("DI_CONNECT/DI_Connect_Wellness/2026_sleepData.json",json.dumps(garmin))
            archive.writestr("unused.json",json.dumps({"export_metadata":True}))
        result = self.features.import_apply("wearables.zip",buf.getvalue())
        self.assertGreater(result["imported"],0)
        sleeps = self.features.list_records("sleep")
        self.assertEqual({"fitbit","garmin"},{p["source"] for p in sleeps})
        self.assertEqual(80,next(p for p in sleeps if p["source"]=="garmin")["deep_min"])
        self.assertEqual(300,next(p for p in self.features.list_records("daily_metric") if p.get("steps"))["steps"])

    def test_mi_fitness_reference_schema_readonly_import(self):
        path = self.folder/"mi.db"
        with closing(sqlite3.connect(path)) as conn,conn:
            for table in ("steps_day","heart_rate_day","sleep"):
                conn.execute(f'CREATE TABLE {table}(sid TEXT,time INTEGER,value TEXT,zone_offset INTEGER,deleted INTEGER)')
            for table,value,deleted in (("steps_day",{"steps":8421,"distance":6123},0),("steps_day",{"steps":99999},1),("heart_rate_day",{"avg_hr":71,"avg_rhr":58},0),("sleep",{"bedtime":1752718320,"out_bed_timestamp":1752740880,"sleep_deep_duration":123,"items":[{"start_time":1752718320,"end_time":1752720180,"state":3}]},0)):
                conn.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?)',("default",1752740880,json.dumps(value),3600,deleted))
        before = path.read_bytes()
        self.features.import_apply("8281032873.db",before)
        self.assertEqual(before,path.read_bytes())
        self.assertEqual(8421,next(r for r in self.features.list_records("daily_metric") if "steps" in r)["steps"])
        self.assertEqual("deep",self.features.list_records("sleep")[0]["stages"][0]["stage"])

    def test_whoop_biomarker_vendor_date_units_and_missing(self):
        data = b'Biomarker Name,Value,Status,Recorded On Date\nALT,33 U/L,Optimal,7/2/26\nWhite Blood Cells,"3,490 cells/uL",Sufficient,7/2/26\nUnknown,--,No Data Available,--\n'
        result = self.features.import_apply("biomarkers.csv",data)
        self.assertEqual(2,result["imported"])
        alt = next(p for p in self.features.list_records("lab") if p["marker"]=="ALT")
        self.assertEqual((33,"U/L","2026-07-02","WHOOP: Optimal"),(alt["value"],alt["unit"],alt["day"],alt["notes"]))

    def test_stream_adapter_sensor_provenance_and_suspect_rr(self):
        hr = record_stream_sample({"source":"noop-backup","source_table":"hrSample","original":{"deviceId":"mine","ts":1700000000,"bpm":65}})
        self.assertEqual(65,hr["hr"])
        self.assertEqual(1700000000000,hr["timestamp_ms"])
        rr = record_stream_sample({"source":"noop-backup","source_table":"rrInterval","original":{"ts":1700000000,"rrMs":950,"tsSuspect":1}})
        self.assertIsNone(rr)
        sdnn = record_stream_sample({"source":"apple-health","type":"HKQuantityTypeIdentifierHeartRateVariabilitySDNN","value":"50","unit":"ms","start_ms":1700000000000})
        self.assertEqual("[]",sdnn["rr_json"])
        self.assertEqual("HKQuantityTypeIdentifierHeartRateVariabilitySDNN",sdnn["sensor_kind"])

    def test_oura_csv_semicolon_and_exercise_only_programs(self):
        data = b'date;Total Sleep Duration;Average Resting Heart Rate;Average HRV;Readiness Score\n2026-06-01;25200;49;65;81\n'
        self.features.import_apply("oura.csv",data)
        row = self.features.list_records("daily_metric")[0]
        self.assertEqual((420,49,65,81),(row["total_sleep_min"],row["resting_hr"],row["hrv_ms"],row["reference_readiness_score"]))
        self.features.import_apply("program.csv",b'Exercise,Target max RPE\nBack squat,"8,5"\nBench press,\n')
        program = self.features.list_records("lifting_program")[0]
        self.assertEqual(8.5,program["lines"][0]["target_max_rpe"])
        self.assertEqual("Bench press",program["lines"][1]["exercise"])

    def test_backup_restore_over_normal_row_budget(self):
        # Own backups must not inherit the ordinary CSV/XML row cap.
        from unittest.mock import patch
        destination = self.folder/"bigger.boopbak"
        self.features.backup_export(destination)
        other = FeatureStore(self.folder/"bigger.sqlite")
        with patch("features.MAX_ROWS",0):
            self.assertEqual(2,other.import_preview(destination.name,destination.read_bytes())["count"])
            self.assertGreater(other.backup_restore(destination.name,destination.read_bytes())["imported"],0)

    def test_liftosaur_completed_sets_only_and_hevy_volume_not_strain(self):
        data = {"history":[{"startTime":1780308000000,"endTime":1780311600000,"entries":[{"name":"Bench","unit":"lb","sets":[{"completedReps":8,"weight":{"value":100,"unit":"lb"}},{"reps":10,"weight":200}]}]}]}
        self.features.import_apply("liftosaur.json",json.dumps(data).encode())
        self.assertEqual(1,len(self.features.list_records("lifting_set")))
        workout = self.features.list_records("workout")[0]
        self.assertAlmostEqual(362.873896,workout["volume_load_kg"])
        self.assertNotIn("strain",workout)

    def test_real_apple_internal_dtd_is_safe_and_hr_reaches_streams(self):
        data = b'<?xml version="1.0"?><!DOCTYPE HealthData [<!ELEMENT HealthData (Record*)><!ELEMENT Record EMPTY><!ATTLIST Record type CDATA #REQUIRED>]><HealthData><Record type="HKQuantityTypeIdentifierHeartRate" startDate="2026-01-01 10:00:00 +1000" endDate="2026-01-01 10:00:00 +1000" value="70" unit="count/min" sourceName="Public Health"/></HealthData>'
        self.features.import_apply("export.xml",data)
        samples = self.features.stream_samples()
        self.assertEqual(70,samples[0]["hr"])
        self.assertEqual("apple-health",samples[0]["source"])


if __name__ == "__main__":
    unittest.main()
