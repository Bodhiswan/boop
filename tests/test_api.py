"""Feature integration tests use temporary data and a simulated strap only."""
import asyncio
from contextlib import closing
import io
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
import zipfile
from unittest.mock import AsyncMock,patch

from aiohttp import FormData
from aiohttp.test_utils import TestClient,TestServer

from analytics import AnalyticsService
from api import FeatureAPI
from boop import Manager,make_app
from coach import Coach,metric_summary
from companion import CATALOG,Companion,alarm_readback,make_plan
from features import FeatureStore
from reports import report_pdf,report_png,report_svg
from storage import Store
from whoop_protocol import STANDARD_HR


class LocalAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.directory.name)/"data.sqlite")
        self.features=FeatureStore(self.store)
        self.manager=Manager(self.store)
        self.manager.features=self.features
        self.companion=Companion(self.manager,self.features)
        self.manager.companion=self.companion
        self.api=FeatureAPI(self.manager,self.features,AnalyticsService(self.store),self.companion)
        self.api.root=Path(self.directory.name)
        (self.api.root/"data").mkdir()
        with socket.socket() as s:
            s.bind(("127.0.0.1",0)); self.port=s.getsockname()[1]
        self.headers={"Origin":f"http://127.0.0.1:{self.port}","X-Boop":"local"}
        self.client=TestClient(TestServer(make_app(self.manager,asyncio.Event(),self.port,self.api),port=self.port))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.companion.close()
        await self.client.close()
        self.directory.cleanup()

    async def post(self,path,body):
        return await self.client.post(path,json=body,headers=self.headers)

    async def test_coach_model_refresh_has_no_health_context_and_rejects_foreign_origin(self):
        result={'provider':'compatible','local':True,'models':['local-model'],
                'data_sent':'Provider authentication only; no questions or health metrics'}
        self.api.coach.models=AsyncMock(return_value=result)
        with patch.object(self.api.analytics,'trends',side_effect=AssertionError('Model refresh must not read health data')):
            response=await self.post('/api/coach/models',{'provider':'compatible','endpoint':'http://localhost:9000'})
            self.assertEqual(response.status,200)
            self.assertEqual(await response.json(),result)
            foreign=await self.client.post('/api/coach/models',json={'provider':'compatible','endpoint':'http://localhost:9000'},headers={'Origin':'https://outside.example','X-Boop':'local'})
            self.assertEqual(foreign.status,403)
        self.api.coach.models.assert_awaited_once_with({'provider':'compatible','endpoint':'http://localhost:9000'})
        self.assertEqual(self.api.coach_history.history()['messages'],[])

    async def test_coach_stream_persists_only_completed_reply(self):
        async def stream(body,bundle,history):
            yield {'type':'meta','provider':'offline','model':'fixture','local':True}
            yield {'type':'delta','text':'A clear '}
            yield {'type':'delta','text':'answer.'}
            yield {'type':'done','result':{'answer':'A clear answer.','provider':'offline','model':'fixture','local':True,'saved':False}}
        self.api.coach.stream=stream
        response=await self.post('/api/coach/stream',{'question':'Explain recorded coverage'})
        self.assertEqual(response.status,200)
        self.assertIn('text/event-stream',response.headers['Content-Type'])
        events=[json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
        self.assertEqual([e['type'] for e in events],['meta','delta','delta','done'])
        self.assertTrue(events[-1]['result']['saved'])
        messages=(await (await self.client.get('/api/coach/history')).json())['messages']
        self.assertEqual([m['text'] for m in messages],['Explain recorded coverage','A clear answer.'])

    async def test_coach_stream_errors_origin_and_master_do_not_save_drafts(self):
        called=[]
        async def stream(body,bundle,history):
            called.append(True)
            yield {'type':'meta','provider':'offline','model':'fixture','local':True}
            yield {'type':'delta','text':'Unfinished draft'}
            raise ValueError('The provider stream ended before a completed answer')
        self.api.coach.stream=stream
        response=await self.client.post('/api/coach/stream',json={'question':'Explain'},headers={'Origin':'https://outside.example','X-Boop':'local'})
        self.assertEqual(response.status,403);self.assertEqual(called,[])
        response=await self.post('/api/coach/stream',{'question':'Explain'})
        events=[json.loads(line[6:]) for line in (await response.text()).splitlines() if line.startswith('data: ')]
        self.assertEqual(events[-1]['type'],'error')
        self.assertNotIn('done',[e['type'] for e in events])
        self.assertEqual((await (await self.client.get('/api/coach/history')).json())['messages'],[])
        await self.post('/api/coach/brief',{'master_enabled':False})
        self.assertEqual((await self.post('/api/coach/stream',{'question':'Explain'})).status,409)
        self.assertEqual(len(called),1)

    async def test_cancelled_coach_stream_closes_generator_without_history(self):
        closed=asyncio.Event();never=asyncio.Event()
        async def stream(body,bundle,history):
            try:
                yield {'type':'meta','provider':'offline','model':'fixture','local':True}
                yield {'type':'delta','text':'A draft'}
                await never.wait()
            finally:
                closed.set()
        self.api.coach.stream=stream
        response=await self.post('/api/coach/stream',{'question':'Explain'})
        self.assertIn(b'meta',await response.content.readline())
        response.close()
        await asyncio.wait_for(closed.wait(),2)
        self.assertEqual((await (await self.client.get('/api/coach/history')).json())['messages'],[])

    async def test_diagnostic_schedule_local_export_origin_and_path_guards(self):
        initial=await (await self.client.get('/api/diagnostics/schedule')).json()
        self.assertFalse(initial['enabled']);self.assertEqual(initial['time_minutes'],420)
        self.assertEqual(initial['files'],[])
        foreign=await self.client.post('/api/diagnostics/schedule',json={'action':'run'},headers={'Origin':'https://outside.example','X-Boop':'local'})
        self.assertEqual(foreign.status,403)
        self.assertEqual((await self.post('/api/diagnostics/schedule',{'directory':'C:/Windows'})).status,409)
        self.assertEqual((await self.post('/api/diagnostics/schedule',{'time_minutes':1440})).status,409)
        result=await (await self.post('/api/diagnostics/schedule',{'action':'run'})).json()
        self.assertTrue(result['result']['success']);self.assertFalse(result['enabled'])
        self.assertEqual(len(result['files']),1)
        name=result['files'][0]['name']
        self.assertTrue((self.api.root/'data/diagnostics/backups'/name).is_file())
        downloaded=await self.client.get('/api/diagnostics/export',params={'file':name})
        self.assertEqual(downloaded.status,200)
        with zipfile.ZipFile(io.BytesIO(await downloaded.read())) as archive:
            status=json.loads(archive.read('status.json'))
            self.assertEqual(set(archive.namelist()),{'status.json','diagnostics.txt'})
            self.assertFalse(set(status)&{'address','name','hr','hrv_quality','device'})
            self.assertNotIn('database',status['store'])
        self.assertEqual((await self.client.get('/api/diagnostics/export',params={'file':'../../whoop.sqlite'})).status,404)
        self.assertEqual((await self.post('/api/diagnostics/schedule',{'action':'clear'})).status,409)
        cleared=await (await self.post('/api/diagnostics/schedule',{'action':'clear','confirm':True})).json()
        self.assertEqual(cleared['files'],[]);self.assertEqual(cleared['result']['removed'],1)
        self.assertTrue(self.store.path.exists())

    async def test_coach_history_brief_and_checkin_origin_guards(self):
        self.api.coach.ask=AsyncMock(return_value={'answer':'RR means beat intervals.','provider':'offline','model':'test','local':True,'saved':False})
        asked=await self.post('/api/coach',{'question':'Explain coverage'})
        reply=await asked.json()
        self.assertTrue(reply['saved'])
        transcript=await (await self.client.get('/api/coach/history')).json()
        self.assertEqual([m['role'] for m in transcript['messages']],['user','assistant'])
        self.assertEqual((await (await self.post('/api/coach/history',{'action':'clear'})).json())['messages'],[])
        self.assertEqual(len((await (await self.post('/api/coach/history',{'action':'undo'})).json())['messages']),2)
        brief=await (await self.client.get('/api/coach/brief')).json()
        self.assertFalse(brief['enabled']);self.assertTrue(brief['master_enabled'])
        self.assertEqual((await self.post('/api/coach/brief',{'api_key':'never-save'})).status,409)
        for path,body in [('/api/coach/history',{'action':'clear'}),('/api/coach/brief',{'enabled':True}),('/api/automations/checkin',{'action':'dismiss'})]:
            response=await self.client.post(path,json=body,headers={'Origin':'https://foreign.example','X-Boop':'local'})
            self.assertEqual(response.status,403)
        await self.post('/api/coach/brief',{'master_enabled':False})
        self.assertEqual((await self.post('/api/coach',{'question':'Explain'})).status,409)
        checkin=await (await self.client.get('/api/automations/checkin')).json()
        self.assertIsNone(checkin['active']);self.assertFalse(checkin['enabled'])

    async def test_backup_schedule_local_copy_restore_and_origin_guards(self):
        initial=await (await self.client.get('/api/backups/schedule')).json()
        self.assertFalse(initial['enabled']);self.assertEqual(initial['keep_count'],7)
        self.assertEqual(initial['keep_options'],[1,3,5,7,10,14]);self.assertEqual(initial['files'],[])
        self.assertEqual(Path(initial['directory']),self.api.root/'data/daily-backups')
        foreign=await self.client.post('/api/backups/schedule',json={'action':'run'},headers={'Origin':'https://outside.example','X-Boop':'local'})
        self.assertEqual(foreign.status,403)
        for body in ({'directory':'relative'},{'keep_count':2},{'enabled':'true'},{'action':'run','enabled':True},{'api_key':'nope'}):
            self.assertEqual((await self.post('/api/backups/schedule',body)).status,409)
        folder=self.api.root/'chosen-backups'
        configured=await (await self.post('/api/backups/schedule',{'directory':str(folder),'keep_count':3})).json()
        self.assertEqual(configured['directory'],str(folder));self.assertFalse(configured['enabled'])
        row=await (await self.post('/api/records/hydration',{'date':'2026-10-01','amount_ml':250,'source':'manual'})).json()
        exported=await (await self.post('/api/backups/schedule',{'action':'run'})).json()
        self.assertTrue(exported['result']['success']);self.assertFalse(exported['enabled'])
        name=exported['files'][0]['name']
        self.assertTrue((folder/name).is_file())
        downloaded=await self.client.get('/api/backups/export',params={'file':name})
        self.assertEqual(downloaded.status,200)
        with zipfile.ZipFile(io.BytesIO(await downloaded.read())) as archive:
            self.assertIsNone(archive.testzip());self.assertIn('boop-backup.sqlite',archive.namelist())
        self.assertEqual((await self.client.get('/api/backups/export',params={'file':'../../data.sqlite'})).status,404)
        self.assertEqual((await self.post('/api/backups/restore',{'file':name})).status,409)
        self.assertEqual((await self.post('/api/backups/restore',{'file':'../../data.sqlite','confirm':True})).status,404)
        refused=await self.client.post('/api/backups/restore',json={'file':name,'confirm':True},headers={'Origin':'https://outside.example','X-Boop':'local'})
        self.assertEqual(refused.status,403)
        self.api.analytics._cache['old']='cached day';self.api.analytics._trends_cache['old']='cached trend'
        # A changed local row wins over the archived value; existing execution preferences stay local.
        await self.post('/api/records/hydration',{'id':row['id'],'date':'2026-10-01','amount_ml':300,'source':'manual'})
        self.manager.send=AsyncMock()
        restored=await self.post('/api/backups/restore',{'file':name,'confirm':True})
        self.assertEqual(restored.status,200,await restored.text())
        self.assertEqual(self.api.analytics._cache,{});self.assertEqual(self.api.analytics._trends_cache,{})
        self.assertEqual(self.features.list_records('hydration')[0]['amount_ml'],300)
        current=await (await self.client.get('/api/backups/schedule')).json()
        self.assertEqual(current['directory'],str(folder));self.assertEqual(current['keep_count'],3)
        self.assertFalse(current['enabled']);self.manager.send.assert_not_awaited()

    async def test_import_apply_and_uploaded_restore_invalidate_projections(self):
        for path in ('/api/import','/api/restore'):
            self.api.analytics._cache['old']='cached day';self.api.analytics._trends_cache['old']='cached trend'
            form=FormData()
            if path=='/api/import':
                form.add_field('file',b'date,calories,protein_g\n2026-10-01,2000,90\n',filename='nutrition.csv',content_type='text/csv')
                form.add_field('mode','apply')
            else:
                destination=self.api.root/'uploaded.boopbak'
                self.features.backup_export(destination)
                form.add_field('file',destination.read_bytes(),filename=destination.name,content_type='application/octet-stream')
            response=await self.client.post(path,data=form,headers=self.headers)
            self.assertEqual(response.status,200,await response.text())
            self.assertEqual(self.api.analytics._cache,{});self.assertEqual(self.api.analytics._trends_cache,{})

    async def test_registered_lift_session_and_workout_detail_routes(self):
        self.manager.address='00:00:00:00:00:01'
        program=await (await self.post('/api/records/lifting_program',{'name':'Temporary test program','source':'manual','lines':[{'exercise':'Squat','target_sets':1,'target_reps':8,'target_weight_kg':20,'rest_sec':90}]})).json()
        for path,body in [('/api/lift',{'action':'start','program_id':program['id']}),('/api/workouts',{'action':'dismiss','ids':['anything']})]:
            r=await self.client.post(path,json=body,headers={'Origin':'https://foreign.example','X-Boop':'local'})
            self.assertEqual(r.status,403)
        r=await self.post('/api/lift',{'action':'start','program_id':program['id']})
        self.assertEqual(r.status,200)
        current=await (await self.client.get('/api/lift')).json()
        self.assertTrue(current['active']);self.assertFalse(current['paused'])
        self.assertEqual(current['state']['plan'][0]['exercise'],'Squat')
        self.assertEqual((await self.post('/api/lift',{'action':'advance'})).status,200)
        self.assertEqual((await self.post('/api/lift',{'action':'pause'})).status,200)
        self.assertEqual((await self.post('/api/lift',{'action':'advance'})).status,409)
        self.assertEqual((await self.post('/api/lift',{'action':'resume'})).status,200)
        self.assertEqual((await self.post('/api/lift',{'action':'discard','confirm':True})).status,200)
        self.assertFalse((await (await self.client.get('/api/lift')).json())['active'])
        listing=await (await self.client.get('/api/workouts?date=2026-10-01')).json()
        self.assertEqual(listing['workouts'],[])

    async def test_settings_crud_origin_and_missing_metrics(self):
        foreign=await self.client.post("/api/settings",json={"name":"bad"},headers={"Origin":"https://outside.example","X-Boop":"local"})
        self.assertEqual(foreign.status,403)
        response=await self.post("/api/settings",{"name":"Local","theme":"light"})
        self.assertEqual(response.status,200); self.assertEqual((await response.json())["name"],"Local")
        row=await (await self.post("/api/records/hydration",{"date":"2026-10-01","amount_ml":250,"source":"manual"})).json()
        delete=await self.client.delete("/api/records/hydration/"+row["id"],headers=self.headers)
        self.assertEqual(delete.status,200)
        self.assertEqual((await (await self.client.get("/api/records/hydration")).json())["records"],[])
        undo=await self.post("/api/records/hydration/"+row["id"]+"/undo",{})
        self.assertEqual(undo.status,200)
        day=await (await self.client.get("/api/day?date=2026-10-01")).json()
        self.assertIsNone(day["charge"]["value"])
        self.assertIsNone(day["hrv"]["value"])
        self.assertEqual(day["coverage"]["hr_samples"],0)
        invalid=await self.post("/api/records/hydration",{"amount_ml":-20})
        self.assertEqual(invalid.status,409)

    async def test_timers_persist_pause_without_actuating(self):
        self.manager.send=AsyncMock()
        response=await self.post("/api/tools/session",{"kind":"interval","rounds":2,"work_seconds":20,"rest_seconds":10})
        self.assertEqual(response.status,200)
        state=await response.json(); self.assertEqual(state["active"]["rounds"],2)
        await self.post("/api/tools/session",{"action":"pause"})
        await self.companion.close()
        restored=Companion(self.manager,self.features)
        self.assertTrue(restored.session_status()["active"]["paused"])
        self.assertEqual(restored.session_status()["active"]["id"],state["active"]["id"])
        self.manager.send.assert_not_awaited()
        await self.post("/api/tools/session",{"action":"stop"})
        self.assertEqual(len(self.features.list_records("workout")),1)

    async def test_import_preview_apply_duplicate_and_local_exports(self):
        for mode in ("preview","apply","apply"):
            form=FormData(); form.add_field("file",b"date,calories,protein_g\n2026-10-01,2000,90\n",filename="nutrition.csv",content_type="text/csv"); form.add_field("mode",mode)
            response=await self.client.post("/api/import",data=form,headers=self.headers)
            self.assertEqual(response.status,200,await response.text())
        self.assertEqual(len(self.features.list_records("nutrition")),1)
        for format in ("backup","boop","whoop","raw","rhythm","diagnostics","nutrition","lifting.xlsx","report.pdf","report.svg","recap.png"):
            response=await self.client.get("/export/"+format+"?days=7")
            self.assertEqual(response.status,200,(format,await response.text() if response.status!=200 else ""))
            payload=await response.read(); self.assertTrue(payload)
            if format=="backup":
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertIn("manifest.json",archive.namelist())
            if format=="report.pdf": self.assertTrue(payload.startswith(b"%PDF"))
            if format=="recap.png": self.assertTrue(payload.startswith(b"\x89PNG"))

    async def test_remote_coach_consent_and_device_controls(self):
        response=await self.post("/api/coach",{"provider":"openai","question":"Explain my day","api_key":"temporary"})
        self.assertEqual(response.status,409)
        self.assertIn("consent",(await response.json())["error"])
        response=await self.post("/api/device",{"action":"buzz"})
        self.assertEqual(response.status,409)
        response=await self.post("/api/automations",{"enabled":True,"double_tap":"shortcut"})
        self.assertEqual(response.status,409)
        self.assertFalse(self.companion.rules["enabled"])
        response=await self.post("/api/devices",{"action":"register","address":"00:00:00:00:00:02","name":"Owned strap"})
        self.assertEqual(response.status,200)
        devices=(await response.json())["devices"]; self.assertEqual(devices[0]["name"],"Owned strap")

    async def test_visible_reminder_and_cycle_shapes(self):
        reminder=await (await self.post("/api/records/reminder",{"date":"2026-10-01","time":"07:30","name":"Check in","enabled":False})).json()
        self.assertEqual(reminder["due_ms"],1790803800000)
        self.assertFalse(reminder["enabled"])
        from insights import cycle_from_logs
        result=cycle_from_logs([{"date":"2026-10-01","period_start":"2026-09-29"}],"2026-10-01")
        self.assertEqual(result["cycle_day"],3)
        self.assertEqual(result["logged_starts"],["2026-09-29"])

    async def test_resonance_only_scores_active_actual_rr_and_no_haptic_default(self):
        self.manager.client=type("Client",(),{"is_connected":True})()
        self.manager.send=AsyncMock()
        await self.companion.session_action({"action":"start","kind":"resonance"})
        self.companion.observe_rr(int(time.time()*1000),[900,910])
        await self.companion.session_action({"action":"pause"})
        self.companion.observe_rr(int(time.time()*1000),[1500]*50)
        state=await self.companion.session_action({"action":"stop"})
        self.assertFalse(state["recent"][-1]["resonance"]["did_lock"])
        self.manager.send.assert_not_awaited()

    async def test_explicit_spot_capture_overrides_paused_rr_only_for_its_window(self):
        self.features.update_settings({"hrv_capture_mode":"paused"})
        self.manager.client=type("Client",(),{"is_connected":True})()
        self.manager.send=AsyncMock()
        self.assertFalse(self.manager.hrv_stream_wanted())
        start=await self.post("/api/hrv",{"action":"start","duration_seconds":60})
        self.assertEqual(start.status,200)
        self.assertTrue(self.manager.hrv_stream_wanted())
        self.manager.send.assert_any_await(63,b"\x01")
        stop=await self.post("/api/hrv",{"action":"stop"})
        self.assertEqual(stop.status,200)
        self.assertFalse(self.manager.hrv_stream_wanted())
        self.assertEqual(self.features.list_records("body_metric"),[])
        status=await self.post("/api/hrv",{"action":"status"})
        self.assertFalse((await status.json())["active"])

    async def test_pre_sleep_uses_observed_window_and_keeps_baseline_unavailable(self):
        self.features.update_settings({"pre_sleep_feedback_enabled":True})
        self.manager.address="test-strap"
        start=1790772000
        self.store.save([("test-strap",(start-1200+i*60)*1000,STANDARD_HR,bytes((0,70))) for i in range(12)])
        day={"day":"2026-10-01","sleep":{"main":{"start":start,"end":start+8*3600},"naps":[]}}
        with patch.object(self.api.analytics,"trends",return_value={"days":[day]}):
            result=self.api.pre_sleep(day)
        self.assertEqual(result["eligibility"],"insufficientBaseline")
        self.assertEqual(result["observation"]["mean_bpm"],70)
        self.assertEqual(result["observation"]["valid_samples"],12)
        self.assertIsNone(result["comparison"])

    async def test_imported_series_accept_day_alias_and_keep_sdnn_separate(self):
        self.features.save_record("daily_metric",{"day":"2026-10-01","source":"oura","hrv_ms":42})
        self.features.save_record("daily_metric",{"day":"2026-10-01","source":"oura","resting_hr":56})
        self.features.save_record("daily_metric",{"day":"2026-10-01","source":"apple-health","hrv_ms":80,"hrv_method":"SDNN"})
        response=await self.client.get("/api/metric-series?metric=hrv&date=2026-10-01&days=1&source=oura")
        points=(await response.json())["points"]
        self.assertEqual(points,[{"date":"2026-10-01","value":42,"source":"oura","unit":"ms"}])
        response=await self.client.get("/api/metric-series?metric=hrv_sdnn&date=2026-10-01&days=1&source=apple-health")
        self.assertEqual((await response.json())["points"][0]["value"],80)
        fusion=await (await self.client.get("/api/fusion?date=2026-10-01")).json()
        self.assertIn("oura",fusion["imported_sources"])
        self.assertEqual(fusion["winners"]["hrv"]["value"],42)
        self.assertEqual(fusion["winners"]["hrv_sdnn"]["value"],80)

    async def test_banked_rr_replace_overlap_and_identical_live_reemissions_deduplicate(self):
        def row(t,kind,rr):
            return {"timestamp_ms":t*1000,"kind":kind,"device":"test-strap","hr":68,"rr_json":json.dumps(rr),"contact":1}
        # Captured WHOOP4 trains: a history [875,886] later overlaps live [888]
        # and live [875,886]. The banked train must contribute exactly once.
        samples=[row(100,"history",[875,886]),row(100,"live",[888]),row(100,"live",[875,886]),row(101,"live",[907]),row(101,"live",[907])]
        hr,rr,gravity=self.api.analytics._streams(samples,[])
        self.assertEqual(rr,[(100,875),(100,886),(101,907)])
        self.assertEqual(len(hr),2)


class ToolPlanTests(unittest.TestCase):
    def test_alarm_firmware_captures_pin_offset_and_no_alarm(self):
        for raw,epoch in (("0101d0728e6a0000040020",1787720400),("010150c48f6a0000040020",1787806800),("0101d015916a0000040020",1787893200),("0130d5356a00000000",1781912880),("30d5356a",1781912880)):
            self.assertEqual(alarm_readback(bytes.fromhex(raw))["epoch"],epoch)
        self.assertTrue(alarm_readback(bytes.fromhex("0100000000000000040020"))["no_alarm"])
        impossible=alarm_readback(bytes.fromhex("0101050000000000040020"))
        self.assertIsNone(impossible["epoch"]); self.assertFalse(impossible["no_alarm"])

    def test_all_noop_breath_catalog_durations_and_phases(self):
        self.assertEqual(len(CATALOG),22)
        for protocol in CATALOG:
            kind,title,plan=make_plan({"kind":"breathe","preset":protocol["id"],"duration_minutes":1})
            self.assertEqual(kind,"breathe")
            self.assertAlmostEqual(sum(part["duration"] for part in plan),60)
            self.assertTrue(all(part["duration"]>0 for part in plan))

    def test_summary_excludes_identifiers_raw_records_and_profile(self):
        summary=metric_summary({"device":"private","days":[{"day":"private-date","hrv":{"value":42},"journal":{"private":"text"},"coverage":{"hr_samples":500},"raw":"secret"}]})
        data=json.dumps(summary)
        self.assertNotIn("private",data); self.assertNotIn("secret",data)
        self.assertEqual(summary["recent_days_oldest_first"][0]["hrv"]["value"],42)


if __name__=="__main__": unittest.main()
