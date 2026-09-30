"""Dashboard contracts for local features. All mutations pass BOOP's origin guard.

Records GET/POST /api/records/{kind}; DELETE and POST ../{id}/undo.
Registry GET /api/devices => devices; POST {action:register|switch|forget|delete-data,
 address,name?,confirm?}. Forget preserves all health records.
Storage GET /api/storage; POST {action:cleanup,confirm:true} reclaims diagnostics only.
Sources GET /api/fusion?date=; POST /api/sources {action:delete,source,confirm:true}.
Insights GET /api/insights?days=30. Metric series GET /api/metric-series?metric=&days=&source=.
Spot capture POST /api/hrv {action:start|status|stop,duration_seconds:60..300}.
Exports GET /export/{backup|boop|raw|rhythm|diagnostics|report.pdf|report.svg|recap.png|
 records|nutrition|lifting.xlsx|route.gpx|route.fit}; route exports require id.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import csv
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import sqlite3
import time
import uuid
import zipfile

from aiohttp import web

from coach import Coach
from coach_history import BRIEF_QUESTION, CoachHistory
from features import KINDS, MAX_BYTES, MAX_BACKUP_BYTES
from reports import report_pdf, report_png, report_svg
from insights import InsightsService
from fusion import fuse_day
from workout_service import WorkoutService


def csv_text(rows):
    rows = list(rows)
    stream = io.StringIO(newline="")
    columns = sorted(set().union(*(r.keys() for r in rows))) if rows else ["no_observations"]
    writer = csv.DictWriter(stream,fieldnames=columns)
    writer.writeheader()
    for row in rows:
        writer.writerow({k:json.dumps(v,separators=(",",":")) if isinstance(v,(dict,list)) else v for k,v in row.items()})
    return stream.getvalue()


def attachment(body, name, mime="application/octet-stream"):
    return web.Response(body=body.encode("utf-8") if isinstance(body,str) else body,
                        content_type=mime,headers={"Content-Disposition":f'attachment; filename="{name}"'})


class FeatureAPI:
    def __init__(self,manager,features,analytics,companion,platform=None):
        self.manager,self.features,self.analytics,self.companion,self.platform=manager,features,analytics,companion,platform
        self.coach=Coach()
        self.coach_history=CoachHistory(features)
        self._brief_task=None
        self._diagnostics_task=None
        self._diagnostics_service=None
        self._backup_task=None
        self._backup_service=None
        self._brief_retry_at=0
        self.insights_service=InsightsService(features,analytics)
        self.workouts=WorkoutService(manager,features,analytics)
        manager.workouts=self.workouts
        self.root=Path(__file__).resolve().parent
        self.capture=None
        self._export_lock=asyncio.Lock()
        with closing(features.store.connect()) as conn:
            with conn:
                conn.execute("CREATE TABLE IF NOT EXISTS boop_devices(address TEXT PRIMARY KEY,name TEXT NOT NULL,forgotten INTEGER NOT NULL DEFAULT 0)")

    async def json(self,request):
        if request.content_length and request.content_length>2_000_000:
            raise ValueError("JSON request exceeds 2 MB")
        body=await request.json()
        if not isinstance(body,dict):
            raise ValueError("Request must be a JSON object")
        return body

    async def bundle(self,request,day=False):
        settings=await asyncio.to_thread(self.features.settings)
        if day:
            return await asyncio.to_thread(self.analytics.day,self.manager.address,request.query.get("date"),settings)
        count=min(366,max(1,int(request.query.get("days",30))))
        return await asyncio.to_thread(self.analytics.trends,self.manager.address,count,settings,request.query.get("date"))

    async def day(self,request):
        settings=await asyncio.to_thread(self.features.settings)
        result,history=await asyncio.to_thread(self.analytics.day_and_history,self.manager.address,request.query.get("date"),settings)
        result["fusion"]=await asyncio.to_thread(self.fusion,result["day"],result)
        result["pre_sleep_feedback"]=await asyncio.to_thread(self.pre_sleep,result,history)
        result["health_projections"]=await asyncio.to_thread(self.health,result,history,settings)
        return web.json_response(result)

    def health(self,current,history,settings):
        from health_projections import health_projections
        coverage=current["coverage"]
        samples,sensors,_=self.analytics._read(self.manager.address,coverage["read_start"],coverage["day_end"])
        hr,rr,gravity=self.analytics._streams(samples,sensors)
        return health_projections(current,rr,gravity,self.features.list_records("lab",{"limit":100000}),[d for d in history if d["day"]<current["day"]],settings)

    async def health_catalog(self,request):
        from health_projections import marker_catalog
        return web.json_response({"markers":marker_catalog()})

    def pre_sleep(self,current,history_days=None):
        from biofeedback import pre_sleep_feedback
        settings=self.features.settings()
        if not settings["pre_sleep_feedback_enabled"]:
            return pre_sleep_feedback({"enabled":False})
        days=history_days if history_days is not None else self.analytics.trends(self.manager.address,45,settings,current["day"])["days"]
        windows=[]
        for day in days:
            sessions=[s for s in [day["sleep"].get("main"),*day["sleep"].get("naps",[])] if s and s.get("start") is not None and s.get("end") is not None]
            windows.append((day,sessions))
        bounds=[s["start"] for _,sessions in windows for s in sessions]
        hr=[]
        if bounds:
            samples,sensors,_=self.analytics._read(self.manager.address,min(bounds)-1800,max(bounds))
            hr=[{"ts":ts,"bpm":bpm} for ts,bpm in self.analytics._streams(samples,sensors)[0]]
        history=[]; current_result=None
        for day,sessions in windows:
            output=pre_sleep_feedback({"enabled":True,"day":day["day"],"sessions":sessions,"hr":hr,"history":history})
            observation=output.get("observation")
            if observation:
                history.append({"day":day["day"],"mean_bpm":observation["mean_bpm"]})
            if day["day"]==current["day"]:
                current_result=output
        return current_result or pre_sleep_feedback({"enabled":True,"day":current["day"]})

    async def trends(self,request):
        return web.json_response(await self.bundle(request))

    async def insights(self,request):
        result=await asyncio.to_thread(self.insights_service.bundle,self.manager.address,int(request.query.get("days",30)),self.features.settings(),request.query.get("date"))
        return web.json_response(result)

    async def settings(self,request):
        if request.method=="GET":
            return web.json_response(await asyncio.to_thread(self.features.settings))
        body=await self.json(request)
        # Apply native preference before storing it; failure never reports successful startup registration.
        await asyncio.to_thread(self.features._validate_settings,body)
        if self.platform and "start_with_windows" in body:
            await asyncio.to_thread(self.platform.startup,body["start_with_windows"])
        result=await asyncio.to_thread(self.features.update_settings,body)
        return web.json_response(result)

    async def records(self,request):
        kind=request.match_info["kind"]
        if kind not in KINDS:
            raise ValueError("Unknown record kind")
        if request.method=="GET":
            return web.json_response({"records":await asyncio.to_thread(self.features.list_records,kind,dict(request.query))})
        if request.method=="DELETE":
            await asyncio.to_thread(self.features.delete_record,kind,request.match_info["id"])
            return web.json_response({"deleted":True,"undo_available":True})
        if request.match_info.get("id"):
            return web.json_response(await asyncio.to_thread(self.features.undo_record,kind,request.match_info["id"]))
        body=await self.json(request)
        if body.get("date"):
            datetime.strptime(body["date"],"%Y-%m-%d")
        if kind == "cycle" and isinstance(body.get("period_start"),str):
            datetime.strptime(body["period_start"],"%Y-%m-%d")
        if kind == "reminder" and body.get("time"):
            from companion import zone
            settings=self.features.settings()
            date=body.get("date") or datetime.now(zone(settings["timezone"])).date().isoformat()
            try:
                due=datetime.strptime(date+" "+body["time"],"%Y-%m-%d %H:%M").replace(tzinfo=zone(settings["timezone"]))
            except ValueError:
                raise ValueError("Reminder time must use HH:MM")
            body["due_ms"]=int(due.timestamp()*1000)
            body.setdefault("enabled",True)
        if kind in ("alarm","reminder") and "enabled" in body and type(body["enabled"]) is not bool:
            raise ValueError("Enabled must be true or false")
        for key in ("amount_ml","calories","protein_g","carbs_g","fat_g","weight_kg","reps","sets"):
            if key in body and (not isinstance(body[key],(int,float)) or isinstance(body[key],bool) or not 0<=body[key]<=1_000_000):
                raise ValueError(f"{key} must be a nonnegative number")
        if "start_ms" in body and "end_ms" in body and body["end_ms"]<=body["start_ms"]:
            raise ValueError("End must be after start")
        return web.json_response(await asyncio.to_thread(self.features.save_record,kind,body))

    async def upload(self,request):
        if not request.content_type.startswith("multipart/"):
            raise ValueError("Choose a local file")
        reader=await request.multipart()
        filename,data,mode=None,bytearray(),"preview"
        async for part in reader:
            if part.name=="file" and filename is None:
                filename=Path(part.filename or "import").name
                while chunk:=await part.read_chunk(65536):
                    data.extend(chunk)
                    if len(data)>MAX_BACKUP_BYTES:
                        raise ValueError("File exceeds 1 GB")
            elif part.name=="mode":
                mode=(await part.text())[:20]
        if not filename or not data:
            raise ValueError("Select a nonempty file")
        if request.path=="/api/restore":
            result=await asyncio.to_thread(self.features.backup_restore,filename,bytes(data))
            await asyncio.to_thread(self.manager.store.retro_decode_sensors)
            await asyncio.to_thread(self.analytics.invalidate_cache)
        elif mode=="apply":
            result=await asyncio.to_thread(self.features.import_apply,filename,bytes(data))
            await asyncio.to_thread(self.manager.store.retro_decode_sensors)
            await asyncio.to_thread(self.analytics.invalidate_cache)
        elif mode=="preview":
            result=await asyncio.to_thread(self.features.import_preview,filename,bytes(data))
        else:
            raise ValueError("Choose preview or apply")
        return web.json_response(result)

    async def sensors(self,request):
        hours=min(720,max(.05,float(request.query.get("hours",24))))
        return web.json_response(await asyncio.to_thread(self.manager.store.sensor_series,self.manager.address,hours))

    async def waveforms(self,request):
        return web.json_response(await asyncio.to_thread(self.manager.store.latest_waveforms,self.manager.address))

    async def tools(self,request):
        result=self.companion.session_status() if request.method=="GET" else await self.companion.session_action(await self.json(request))
        return web.json_response(result)

    async def catalog(self,request):
        from companion import CATALOG
        return web.json_response({"protocols":CATALOG})

    async def workout_action(self,request):
        method=self.workouts.detail if request.method=='GET' else self.workouts.edit
        body=dict(request.query) if request.method=='GET' else await self.json(request)
        return web.json_response(await asyncio.to_thread(method,body))

    async def lift_action(self,request):
        if request.method=='GET':
            return web.json_response(await asyncio.to_thread(self.workouts.lift_status))
        return web.json_response(await asyncio.to_thread(self.workouts.lift_action,await self.json(request)))

    async def device(self,request):
        return web.json_response(await self.companion.device(await self.json(request)))

    async def automations(self,request):
        return web.json_response(self.companion.automation_settings(None if request.method=="GET" else await self.json(request)))

    async def checkin(self,request):
        return web.json_response(self.companion.checkin(None if request.method=='GET' else await self.json(request)))

    async def coach_options(self,request):
        return web.json_response({**(await self.coach.options()),"brief":await asyncio.to_thread(self.coach_history.config)})

    async def coach_models(self,request):
        # Explicit refresh sends authentication only, never the daily bundle.
        return web.json_response(await self.coach.models(await self.json(request)))

    async def coach_ask(self,request):
        body=await self.json(request)
        return web.json_response(await self.ask_coach(body))

    async def coach_stream(self,request):
        body=await self.json(request)
        config=await asyncio.to_thread(self.coach_history.config)
        if not config['master_enabled']:
            raise ValueError('Coach is switched off; enable it in Coach preferences')
        if 'save' in body and type(body['save']) is not bool:
            raise ValueError('Save conversation must be true or false')
        settings=await asyncio.to_thread(self.features.settings)
        bundle=await asyncio.to_thread(self.analytics.trends,self.manager.address,7,settings)
        from companion import zone
        current=datetime.now(zone(settings['timezone']))
        history=await asyncio.to_thread(self.coach_history.context,current,8)
        events=self.coach.stream(body,bundle,history)
        # Validate consent/model/connection before committing an SSE response.
        try:
            first=await anext(events)
        except StopAsyncIteration:
            raise ValueError('The Coach returned no response')
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream; charset=utf-8',
            'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})
        try:
            await response.prepare(request)
            async def send(event):
                await response.write(('data: '+json.dumps(event,ensure_ascii=False,allow_nan=False)+'\n\n').encode('utf-8'))
            await send(first)
            async for event in events:
                if event.get('type')=='done':
                    result=dict(event['result'])
                    if request.transport is None or request.transport.is_closing():
                        break
                    if body.get('save',True):
                        result=await asyncio.to_thread(self.coach_history.append,body['question'],result)
                    await send({'type':'done','result':result})
                    break
                await send(event)
        except ValueError as exc:
            with contextlib.suppress(ConnectionError,OSError):
                await response.write(('data: '+json.dumps({'type':'error','error':str(exc)})+'\n\n').encode('utf-8'))
        except (KeyError,TypeError,RuntimeError,sqlite3.DatabaseError):
            with contextlib.suppress(ConnectionError,OSError):
                await response.write(b'data: {"type":"error","error":"The reply could not finish or be saved locally. Please try again."}\n\n')
        except (ConnectionError,OSError):
            pass
        finally:
            await events.aclose()
        with contextlib.suppress(ConnectionError,OSError):
            await response.write_eof()
        return response

    async def ask_coach(self,body,brief_day=None):
        config=await asyncio.to_thread(self.coach_history.config)
        if not config['master_enabled']:
            raise ValueError('Coach is switched off; enable it in Coach preferences')
        if 'save' in body and type(body['save']) is not bool:
            raise ValueError('Save conversation must be true or false')
        settings=await asyncio.to_thread(self.features.settings)
        bundle=await asyncio.to_thread(self.analytics.trends,self.manager.address,7,settings)
        from companion import zone
        current=datetime.now(zone(settings['timezone']))
        history=await asyncio.to_thread(self.coach_history.context,current,8)
        result=await self.coach.ask(body,bundle,history)
        if body.get('save',True):
            result=await asyncio.to_thread(self.coach_history.append,body['question'],result,brief_day)
        return result

    async def coach_transcript(self,request):
        if request.method=='POST':
            body=await self.json(request)
            if body.get('action') not in ('clear','undo'):
                raise ValueError('Choose clear or undo conversation')
            return web.json_response(await asyncio.to_thread(self.coach_history.clear,body['action']=='undo'))
        return web.json_response(await asyncio.to_thread(self.coach_history.history))

    async def coach_brief(self,request):
        if request.method=='GET':
            return web.json_response(await asyncio.to_thread(self.coach_history.config))
        body=await self.json(request)
        if body.get('action')=='generate':
            from companion import zone
            current=datetime.now(zone(self.features.settings()['timezone']))
            return web.json_response(await self.ask_coach({'question':BRIEF_QUESTION,'provider':'offline'},current.date().isoformat()))
        return web.json_response(await asyncio.to_thread(self.coach_history.config,body))

    async def start_brief_scheduler(self,app):
        self._brief_task=asyncio.create_task(self.run_brief_scheduler())

    async def close_brief_scheduler(self,app):
        if self._brief_task:
            self._brief_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._brief_task

    async def run_brief_scheduler(self):
        from companion import zone
        while True:
            await asyncio.sleep(30)
            settings=await asyncio.to_thread(self.features.settings)
            now=datetime.now(zone(settings['timezone']))
            if time.monotonic()<self._brief_retry_at or not await asyncio.to_thread(self.coach_history.due,now) or self.coach.lock.locked():
                continue
            try:
                reply=await self.ask_coach({'question':BRIEF_QUESTION,'provider':'offline'},now.date().isoformat())
                if self.platform and settings['notifications_enabled']:
                    clock=now.strftime('%H:%M'); begin=settings['quiet_hours_start']; end=settings['quiet_hours_end']
                    quiet=begin<=clock<end if begin<end else (clock>=begin or clock<end) if begin!=end else False
                    if not quiet:
                        self.platform.notify('Your local morning brief',reply['answer'].split('\n')[0][:180])
            except (ValueError,OSError):
                self._brief_retry_at=time.monotonic()+300
                self.manager.log('Local Coach brief could not finish; it will retry while the opted-in schedule is enabled')

    def diagnostic_service(self):
        from diagnostics_scheduler import DiagnosticsScheduler
        if self._diagnostics_service is None or self._diagnostics_service.root!=self.root.resolve():
            self._diagnostics_service=DiagnosticsScheduler(self.features,self.root,self.diagnostic_bytes)
        return self._diagnostics_service

    async def diagnostic_bytes(self):
        status=await self.manager.status()
        metadata={key:status.get(key) for key in ('phase','connected','firmware','mtu','clock_verified',
            'clock_drift_s','frames_received','invalid_frames','recording_awake_held','sync')}
        metadata['store']={key:value for key,value in status.get('store',{}).items() if key!='database'}
        def scrub(message):
            text=str(message)
            for value in (self.manager.address,self.manager.name,str(self.root)):
                if value:text=text.replace(value,'[local device]' if value!=str(self.root) else '[BOOP workspace]')
            return re.sub(r'(?i)(?:[0-9a-f]{2}:){5}[0-9a-f]{2}','[device address]',text)
        logs=[{'time':row.get('time'),'message':scrub(row.get('message',''))} for row in self.manager.logs]
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('status.json',json.dumps(metadata,indent=2,allow_nan=False))
            archive.writestr('diagnostics.txt','Local support metadata. No identity handshake, API keys, action preferences or raw health database.\n'+'\n'.join(row['message'] for row in logs))
        return stream.getvalue()

    def diagnostic_files(self):
        return [{'name':path.name,'size_bytes':path.stat().st_size,'modified_ms':int(path.stat().st_mtime*1000)}
                for path in reversed(self.diagnostic_service()._owned_files())]

    async def diagnostic_schedule(self,request):
        service=self.diagnostic_service()
        result=None
        if request.method=='POST':
            body=await self.json(request)
            if body.get('action')=='run':
                if set(body)!={'action'}:raise ValueError('Run diagnostics accepts only an action')
                from companion import zone
                settings=await asyncio.to_thread(self.features.settings)
                result=await service.tick(datetime.now(zone(settings['timezone'])),run_now=True)
            elif body.get('action')=='clear':
                if body.get('confirm') is not True or set(body)-{'action','confirm'}:
                    raise ValueError('Confirm clearing only scheduled diagnostic copies')
                result=await service.clear_exports()
            else:
                await asyncio.to_thread(service.config,body)
        config=await asyncio.to_thread(service.config)
        return web.json_response({**config,'files':await asyncio.to_thread(self.diagnostic_files),**({'result':result} if result is not None else {})})

    async def diagnostic_download(self,request):
        name=request.query.get('file')
        paths=await asyncio.to_thread(self.diagnostic_service()._owned_files)
        path=next((path for path in paths if path.name==name),None)
        if path is None:raise web.HTTPNotFound(text='Choose a saved local diagnostic export')
        return web.FileResponse(path,headers={'Content-Disposition':f'attachment; filename="{path.name}"'})

    async def start_diagnostic_scheduler(self,app):
        self._diagnostics_task=asyncio.create_task(self.run_diagnostic_scheduler())

    async def close_diagnostic_scheduler(self,app):
        if self._diagnostics_task:
            self._diagnostics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self._diagnostics_task

    async def run_diagnostic_scheduler(self):
        from companion import zone
        while True:
            await asyncio.sleep(30)
            settings=await asyncio.to_thread(self.features.settings)
            await self.diagnostic_service().tick(datetime.now(zone(settings['timezone'])))

    def backup_service(self):
        from backup_scheduler import BackupScheduler
        if self._backup_service is None or self._backup_service.root!=self.root.resolve():
            self._backup_service=BackupScheduler(self.features,self.root)
        return self._backup_service

    async def backup_schedule(self,request):
        service=self.backup_service()
        result=None
        if request.method=='POST':
            body=await self.json(request)
            if body.get('action')=='run':
                if set(body)!={'action'}:
                    raise ValueError('Run backup accepts only an action')
                from companion import zone
                settings=await asyncio.to_thread(self.features.settings)
                result=await service.tick(datetime.now(zone(settings['timezone'])),run_now=True)
            else:
                await asyncio.to_thread(service.config,body)
        config=await asyncio.to_thread(service.config)
        return web.json_response({**config,**({'result':result} if result is not None else {})})

    async def backup_download(self,request):
        path=await asyncio.to_thread(self.backup_service().lookup,request.query.get('file'))
        if path is None:
            raise web.HTTPNotFound(text='Choose a saved local BOOP backup')
        return web.FileResponse(path,headers={'Content-Disposition':f'attachment; filename="{path.name}"'})

    async def backup_restore(self,request):
        body=await self.json(request)
        if body.get('confirm') is not True or set(body)!={'file','confirm'}:
            raise ValueError('Confirm restoring the selected local backup')
        path=await asyncio.to_thread(self.backup_service().lookup,body.get('file'))
        if path is None:
            raise web.HTTPNotFound(text='Choose a saved local BOOP backup')
        if path.stat().st_size>MAX_BACKUP_BYTES:
            raise ValueError('File exceeds 1 GB')
        result=await asyncio.to_thread(self.features.backup_restore,path.name,await asyncio.to_thread(path.read_bytes))
        await asyncio.to_thread(self.manager.store.retro_decode_sensors)
        await asyncio.to_thread(self.analytics.invalidate_cache)
        return web.json_response(result)

    async def start_backup_scheduler(self,app):
        self._backup_task=asyncio.create_task(self.run_backup_scheduler())

    async def close_backup_scheduler(self,app):
        if self._backup_task:
            self._backup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self._backup_task

    async def run_backup_scheduler(self):
        from companion import zone
        while True:
            await asyncio.sleep(30)
            settings=await asyncio.to_thread(self.features.settings)
            await self.backup_service().tick(datetime.now(zone(settings['timezone'])))

    def registry(self):
        with closing(self.features.store.connect()) as conn:
            with conn:
                conn.execute("INSERT OR IGNORE INTO boop_devices(address,name) SELECT DISTINCT device,'BOOP strap' FROM frames WHERE device IS NOT NULL")
                if self.manager.address:
                    conn.execute("INSERT OR IGNORE INTO boop_devices(address,name) VALUES(?,?)",(self.manager.address,self.manager.name or "BOOP strap"))
            return [dict(r)|{"active":r["address"]==self.manager.address} for r in conn.execute("SELECT * FROM boop_devices WHERE forgotten=0 ORDER BY name")]

    async def devices(self,request):
        if request.method=="POST":
            body=await self.json(request)
            address=str(body.get("address","")).upper()
            if not re.fullmatch(r"(?:[A-F0-9]{2}:){5}[A-F0-9]{2}",address):
                raise ValueError("Choose a device address from Scan")
            action=body.get("action")
            if action in ("switch","register"):
                with closing(self.features.store.connect()) as conn:
                    with conn:
                        conn.execute("INSERT INTO boop_devices(address,name,forgotten) VALUES(?,?,0) ON CONFLICT(address) DO UPDATE SET name=excluded.name,forgotten=0",(address,str(body.get("name") or "BOOP strap")[:100]))
                if action=="switch":
                    await self.manager.disconnect()
                    self.manager.connect(address)
            elif action in ("forget","delete-data"):
                if body.get("confirm") is not True:
                    raise ValueError("Confirm this device action")
                if self.manager.address==address:
                    await self.manager.disconnect()
                    self.manager.address=self.manager.name=None
                    (self.root/"data/settings.json").unlink(missing_ok=True)
                if action=="delete-data":
                    await asyncio.to_thread(self.features._snapshot,"before-device-delete")
                with closing(self.features.store.connect()) as conn:
                    with conn:
                        conn.execute("UPDATE boop_devices SET forgotten=1 WHERE address=?",(address,))
                        if action=="delete-data":
                            for table in ("sensors","readings","frames","chunks"):
                                columns={r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                                if "device" in columns:
                                    conn.execute(f"DELETE FROM {table} WHERE device=?",(address,))
            else:
                raise ValueError("Unknown registry action")
        return web.json_response({"devices":await asyncio.to_thread(self.registry)})

    def fusion(self,date,computed=None):
        records=self.features.list_records("daily_metric",{"date":date,"limit":100000})
        sources={}
        for record in records:
            source=record.get("source", "import")
            sources.setdefault(source,[]).append(record)
        chosen={k:{"value":v.get("value"),"source":v.get("source","computed NOOP"),"kind":"computed"} for k,v in (computed or {}).items() if isinstance(v,dict) and "value" in v}
        resolved=fuse_day({"day":date,**(computed or {})},records,self.features.settings())
        return {**resolved,"date":date,"computed":chosen,"imported_sources":sources}

    async def sources(self,request):
        if request.method=="GET":
            day=await self.bundle(request,True)
            return web.json_response(await asyncio.to_thread(self.fusion,day["day"],day))
        body=await self.json(request)
        if body.get("action")!="delete" or body.get("confirm") is not True or not isinstance(body.get("source"),str):
            raise ValueError("Confirm the selected import source deletion")
        source=body["source"]
        await asyncio.to_thread(self.features._snapshot,"before-source-delete")
        def remove():
            count=0
            for kind in KINDS:
                for row in self.features.list_records(kind,{"source":source,"limit":100000}):
                    self.features.delete_record(kind,row["id"]); count+=1
            return count
        count=await asyncio.to_thread(remove)
        return web.json_response({"deleted_records":count,"source":source,"original_strap_frames_preserved":True})

    def storage(self):
        root=self.root/"data"
        files=[{"name":p.name,"bytes":p.stat().st_size} for p in root.iterdir() if p.is_file()]
        with closing(self.features.store.connect()) as conn:
            records=[dict(r) for r in conn.execute("SELECT kind,COUNT(*) records,SUM(LENGTH(payload_json)) payload_bytes FROM feature_records WHERE deleted_ms IS NULL GROUP BY kind")]
        removable=[r for r in files if r["name"].endswith((".log","-probe.json","-notifications.json")) or r["name"]=="notifications.json"]
        return {"database":self.manager.store.summary(self.manager.address),"records":records,"files":files,
                "reclaimable_bytes":sum(r["bytes"] for r in removable),"cleanup_targets":[r["name"] for r in removable],
                "policy":"Cleanup removes optional diagnostic captures. Original frames, history archives and health records remain durable."}

    async def storage_action(self,request):
        if request.method=="POST":
            body=await self.json(request)
            if body.get("action")!="cleanup" or body.get("confirm") is not True:
                raise ValueError("Confirm diagnostic cleanup")
            # Live logs are truncated safely. No path supplied by the caller is accepted.
            info=await asyncio.to_thread(self.storage)
            for name in info["cleanup_targets"]:
                target=self.root/"data"/name
                if target.suffix==".log":
                    target.write_bytes(b"")
                else:
                    target.unlink(missing_ok=True)
        return web.json_response(await asyncio.to_thread(self.storage))

    async def metric_series(self,request):
        metric=request.query.get("metric","hrv")
        bundle=await self.bundle(request)
        rows=[]
        for day in bundle["days"]:
            item=day.get(metric)
            if isinstance(item,dict) and isinstance(item.get("value"),(int,float)):
                rows.append({"date":day["day"],"value":item["value"],"source":item.get("source","computed NOOP"),"unit":item.get("unit"),"coverage":item.get("coverage")})
        grouped={}
        for record in self.features.list_records("daily_metric",{"limit":100000}):
            key=record.get("date") or record.get("day") or ""
            if bundle["days"][0]["day"]<=key<=bundle["through"]:
                grouped.setdefault((key,record.get("source","import")),[]).append(record)
        for (key,source),records in grouped.items():
                canonical={"resting_hr":"rhr","blood_oxygen":"spo2","skin_temperature":"skin_temp","calories":"energy_kcal","sleep":"sleep_total_min"}.get(metric,metric)
                resolved=fuse_day({"day":key},records)["winners"].get(canonical)
                if resolved:
                    units={"rhr":"bpm","hrv":"ms","hrv_sdnn":"ms","spo2":"%","skin_temp":"°C","energy_kcal":"kcal","sleep_total_min":"min"}
                    rows.append({"date":key,"value":resolved["value"],"source":resolved["source"],"unit":units.get(canonical)})
        # A canonical imported summary and its original source row are the same
        # observation, not two paired days. The resolved source row carries units.
        unique={(r["date"],r["source"]):r for r in rows}
        rows=list(unique.values())
        if request.query.get("source"):
            rows=[r for r in rows if r["source"]==request.query["source"]]
        return web.json_response({"metric":metric,"points":sorted(rows,key=lambda r:r["date"]),"missing":"No interpolated values for absent days"})

    async def hrv_capture(self,request):
        body=await self.json(request)
        action=body.get("action","status")
        if action not in ("start","status","stop"):
            raise ValueError("Choose start, status or stop for HRV capture")
        now=int(time.time()*1000)
        if action=="start":
            if not self.manager.connected:
                raise ValueError("Connect the strap for a guided RR capture")
            duration=int(body.get("duration_seconds",120))
            if not 60<=duration<=300:
                raise ValueError("Capture duration must be 60–300 seconds")
            self.capture={"started_ms":now,"duration_seconds":duration,"finished":False}
            self.manager.spot_capture_until=time.time()+duration
            if not self.manager._sync_active:
                await self.manager.arm_live()
        if not self.capture:
            return web.json_response({"active":False,"message":"Sit still with the strap on, then start a 1–5 minute capture."})
        capture=self.capture
        rr=[v for t,v in self.manager.rr if capture["started_ms"]<=t<=min(now,capture["started_ms"]+capture["duration_seconds"]*1000)]
        from analytics import hrv
        stamps=[t/1000 for t,v in self.manager.rr if capture["started_ms"]<=t<=min(now,capture["started_ms"]+capture["duration_seconds"]*1000)]
        result=hrv(rr,stamps,max_rejected_fraction=.35)
        complete=capture["finished"] or now-capture["started_ms"]>=capture["duration_seconds"]*1000 or action=="stop"
        if complete and not capture["finished"]:
            capture["finished"]=True
            self.manager.spot_capture_until=0
            capture["result"]=result
            if result.get("value") is not None:
                capture["saved"]=await asyncio.to_thread(self.features.save_record,"body_metric",{"marker":"hrv_spot","date":datetime.now(self.analytics._timezone(self.features.settings())[1]).date().isoformat(),"timestamp_ms":capture["started_ms"],"duration_seconds":min(capture["duration_seconds"],(now-capture["started_ms"])/1000),"source":"whoop_live_rr","result":result})
        return web.json_response({**capture,"active":not complete,"elapsed_seconds":min(capture["duration_seconds"],(now-capture["started_ms"])/1000),"rr_count":len(rr),"result":capture.get("result",result)})

    async def export(self,request):
        name=request.match_info["format"]
        if name=="records":
            kind=request.query.get("kind")
            return attachment(await asyncio.to_thread(self.features.export_csv,kind),f"boop-{kind}.csv","text/csv")
        if name=="nutrition":
            return attachment(await asyncio.to_thread(self.features.export_nutrition_csv),"boop-nutrition.csv","text/csv")
        if name=="lifting.xlsx":
            return attachment(await asyncio.to_thread(self.features.export_lifting_xlsx),"boop-lifting.xlsx","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        if name in ("route.gpx","route.fit"):
            function=self.features.export_route_gpx if name.endswith("gpx") else self.features.export_route_fit
            return attachment(await asyncio.to_thread(function,request.query.get("id")),"boop-"+name,"application/gpx+xml" if name.endswith("gpx") else "application/octet-stream")
        if name=="backup":
            async with self._export_lock:
                directory=self.root/"data/exports"; directory.mkdir(exist_ok=True)
                destination=directory/f"boop-{uuid.uuid4().hex}.boopbak"
                await asyncio.to_thread(self.features.backup_export,destination)
            return web.FileResponse(destination,headers={"Content-Disposition":'attachment; filename="boop-local-backup.boopbak"'})
        if name in ("report.pdf","report.svg","recap.png"):
            bundle=await self.bundle(request)
            render,mime={"report.pdf":(report_pdf,"application/pdf"),"report.svg":(report_svg,"image/svg+xml"),"recap.png":(report_png,"image/png")}[name]
            return attachment(await asyncio.to_thread(render,bundle),"boop-"+name,mime)
        if name=="raw":
            def raw_sensors():
                with closing(self.manager.store.connect()) as conn:
                    return [{"device":r[0],"timestamp_ms":r[1],"received_ms":r[2],"source":r[3],**json.loads(r[4])} for r in conn.execute("SELECT device,timestamp_ms,received_ms,source,values_json FROM sensors WHERE device=? ORDER BY timestamp_ms,frame_id",(self.manager.address,))]
            rows=await asyncio.to_thread(raw_sensors)
            return attachment(csv_text(rows),"boop-sensors.csv","text/csv")
        if name=="rhythm":
            def rhythm():
                with closing(self.manager.store.connect()) as conn:
                    rows=conn.execute("SELECT timestamp_ms,received_ms,kind,rr_json FROM readings WHERE device=? AND rr_json IS NOT NULL ORDER BY timestamp_ms",(self.manager.address,))
                    return [{"timestamp_ms":r[0],"received_ms":r[1],"source":r[2],"rr_ms":json.loads(r[3])} for r in rows]
            return attachment(csv_text(await asyncio.to_thread(rhythm)),"boop-rhythm.csv","text/csv")
        if name=='diagnostics':
            return attachment(await self.diagnostic_bytes(),'boop-diagnostics.zip','application/zip')
        if name in ("boop","whoop"):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,"w",zipfile.ZIP_DEFLATED) as archive:
                if name in ("boop","whoop"):
                    for kind in ("daily_metric","sleep","workout","journal"):
                        archive.writestr(kind+".csv",await asyncio.to_thread(self.features.export_whoop_csv,kind))
                    from compatible_exports import export_computed
                    bundle=await self.bundle(request)
                    computed=await asyncio.to_thread(export_computed,bundle,self.features.settings())
                    for filename,text in computed.items():
                        archive.writestr("computed/"+filename,text)
                    archive.writestr("README.txt","Root tables retain imported/manual facts. computed/ contains NOOP-layout estimates labeled boop (APPROXIMATE); Charge, Effort and Rest are separate from imported official scores. Computed range defaults to 30 days; select days=1..366 and date=YYYY-MM-DD. Missing values remain blank. UTC instants are preserved; Cycle start time is the source-compatible local display-day key. No summary-only sleep is given invented onsets.\n")
            return attachment(stream.getvalue(),"boop-compatible.zip","application/zip")
        raise web.HTTPNotFound()

    def register(self,app):
        routes=[web.get("/api/day",self.day),web.get("/api/trends",self.trends),web.get("/api/insights",self.insights),
                web.get("/api/settings",self.settings),web.post("/api/settings",self.settings),
                web.get("/api/records/{kind}",self.records),web.post("/api/records/{kind}",self.records),
                web.delete("/api/records/{kind}/{id}",self.records),web.post("/api/records/{kind}/{id}/undo",self.records),
                web.post("/api/import",self.upload),web.post("/api/restore",self.upload),web.get("/api/sensors",self.sensors),web.get("/api/waveforms",self.waveforms),
                web.get("/api/tools/session",self.tools),web.post("/api/tools/session",self.tools),web.get("/api/tools/catalog",self.catalog),
                web.get("/api/workouts",self.workout_action),web.post("/api/workouts",self.workout_action),web.get("/api/lift",self.lift_action),web.post("/api/lift",self.lift_action),
                web.post("/api/device",self.device),web.get("/api/automations",self.automations),web.post("/api/automations",self.automations),
                web.get("/api/automations/checkin",self.checkin),web.post("/api/automations/checkin",self.checkin),
                web.get("/api/coach/options",self.coach_options),web.post("/api/coach/models",self.coach_models),web.post("/api/coach",self.coach_ask),web.post("/api/coach/stream",self.coach_stream),
                web.get("/api/coach/history",self.coach_transcript),web.post("/api/coach/history",self.coach_transcript),
                web.get("/api/coach/brief",self.coach_brief),web.post("/api/coach/brief",self.coach_brief),
                web.get('/api/diagnostics/schedule',self.diagnostic_schedule),web.post('/api/diagnostics/schedule',self.diagnostic_schedule),web.get('/api/diagnostics/export',self.diagnostic_download),
                web.get('/api/backups/schedule',self.backup_schedule),web.post('/api/backups/schedule',self.backup_schedule),
                web.get('/api/backups/export',self.backup_download),web.post('/api/backups/restore',self.backup_restore),
                web.get("/api/devices",self.devices),web.post("/api/devices",self.devices),
                web.get("/api/fusion",self.sources),web.post("/api/sources",self.sources),
                web.get("/api/storage",self.storage_action),web.post("/api/storage",self.storage_action),
                web.get("/api/metric-series",self.metric_series),web.post("/api/hrv",self.hrv_capture)]
        routes.append(web.get("/api/health/catalog",self.health_catalog))
        app.add_routes(routes)
        app.on_startup.append(self.start_brief_scheduler)
        app.on_cleanup.append(self.close_brief_scheduler)
        app.on_startup.append(self.start_diagnostic_scheduler)
        app.on_cleanup.append(self.close_diagnostic_scheduler)
        app.on_startup.append(self.start_backup_scheduler)
        app.on_cleanup.append(self.close_backup_scheduler)
