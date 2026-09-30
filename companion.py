"""Local WHOOP controls and sessions. No actuator runs until the user enables it.

Wire payloads and breath timing follow NOOP Commands.swift, HapticClock.swift,
BreathProtocolCatalog.swift at the commit recorded in vendor/noop/PROVENANCE.md.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from whoop_protocol import (event, sensor_values, diagnostic_probe, enumeration_reply,
                            config_reply, config_read_body, REBOOT_CANDIDATES)

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / "vendor/noop/breath_protocols.json").read_text(encoding="utf-8"))
DEFAULT_RULES = {"enabled": False, "double_tap": "none", "wrist_off": "none", "wrist_on": "none",
                 "shortcut_path": "", "hr_zone_haptics": False, "inactivity_minutes": 0,
                 "stress_check_in":False,"stress_haptics":False}
DEFAULT_WIND_DOWN = dict(enabled=False,wake_minutes=420,sleep_need_minutes=480,lead_minutes=30,wake_overrides={})
CONFIG_KEYS = ('enable_r22_packets','enable_r22_v2_packets','enable_r22_v3_packets','enable_r22_v4_packets','enable_r22_v5_packets','enable_r22_v6_packets','enable_r22_v8_packets','make_hrfm_visible','disable_pip_r26_packets','wear_detect_bias','hr_ch_switching','ir_hw_switching','enable_passive_strap_fit_gen5','enable_sig11_during_sleep','dorset_inhibit_wpt','enable_sig12')
OXYGEN_GUESSES = ('enable_spo2','enable_spo2_packets','spo2_enable','enable_blood_oxygen','blood_oxygen_enable','enable_pulse_ox','enable_oxygen_packets','spo2_subscription_enabled')

class ProbeTimeout(ValueError):
    pass

def reboot_probe_state(elapsed,connected,saw_drop=False):
    if saw_drop and connected: return 'reconnected'
    if not saw_drop and connected and elapsed>=12: return 'no_disconnect'
    if elapsed>=60: return 'unsettled'
    return 'link_dropped' if saw_drop or not connected else 'waiting'

def validate_wind_down(body,current=None):
    if not isinstance(body,dict) or set(body)-set(DEFAULT_WIND_DOWN): raise ValueError('Unknown wind-down setting')
    config={**DEFAULT_WIND_DOWN,**(current or {}),**body}
    if type(config['enabled']) is not bool: raise ValueError('Wind-down enabled must be true or false')
    def integer(value,low,high):
        if type(value) is not int or not low<=value<=high: raise ValueError(f'Wind-down value must be an integer between {low} and {high}')
        return value
    for key,low,high in (('wake_minutes',0,1439),('sleep_need_minutes',300,660),('lead_minutes',0,120)):
        integer(config[key],low,high)
    raw=config['wake_overrides']
    if not isinstance(raw,dict) or len(raw)>7: raise ValueError('Wake overrides must map Monday=0 through Sunday=6')
    overrides={}
    for key,value in raw.items():
        if str(key) not in tuple(str(i) for i in range(7)): raise ValueError('Wake override weekday must be 0–6')
        overrides[str(key)]=integer(value,0,1439)
    config['wake_overrides']=overrides
    return config

def wind_down_times(now,config):
    """NOOP previous-evening arithmetic, represented as timezone-aware datetimes."""
    if not config['enabled']: return []
    result=[]
    for offset in range(0,9):
        wake_day=(now+timedelta(days=offset)).date()
        minutes=config['wake_overrides'].get(str(wake_day.weekday()),config['wake_minutes'])
        wake=datetime.combine(wake_day,datetime.min.time(),now.tzinfo)+timedelta(minutes=minutes)
        nudge=wake-timedelta(minutes=config['sleep_need_minutes']+config['lead_minutes'])
        result.append(nudge)
    return sorted(result)


def zone(name):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        if name != "Australia/Brisbane":
            raise ValueError("Time zone is unavailable on this installation")
        return timezone(timedelta(hours=10))


def bounded(value, lower, upper, label):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number")
    if not math.isfinite(number) or not lower <= number <= upper:
        raise ValueError(f"{label} must be between {lower} and {upper}")
    return number


def alarm_readback(payload):
    """FrameRouter.swift's captured WHOOP4 shapes; never try mirror offsets on 11 bytes."""
    def number(offset):
        return int.from_bytes(payload[offset:offset+4],"little") if len(payload)>=offset+4 else None
    if len(payload)==11 and payload[0]==1:
        value=number(2)
    elif payload and payload[0]==1 and number(1) is not None and 1_500_000_000<=number(1)<=4_102_444_800:
        value=number(1)
    else:
        value=number(0)
        if payload and payload[0]==1 and number(1)==0:
            value=0
    return {"epoch":value if value is not None and 1_500_000_000<=value<=4_102_444_800 else None,
            "no_alarm":value==0,"raw_hex":payload.hex()}


def make_plan(options):
    kind = options.get("kind", "breathe")
    if kind in ("resonance", "hr-down", "live-session", "breathe-custom"):
        from biofeedback import make_bio_plan
        return make_bio_plan(options)
    if kind == "breathe":
        preset = {"relax": "relax_4_6", "coherence": "coherence_5_5", "box": "box_4_4_4_4"}.get(options.get("preset"), options.get("preset", "relax_4_6"))
        protocol = next((p for p in CATALOG if p["id"] == preset), None)
        if protocol is None:
            raise ValueError("Choose a breathing protocol from the catalog")
        duration = bounded(options.get("duration_minutes", protocol["recommended_duration_ms"] / 60000), .1, 120, "Duration") * 60
        cycle = protocol["stages"] or [{"type": "guided", "label": "Guided practice", "duration_ms": int(duration * 1000)}]
        plan, cursor = [], 0.0
        while cursor < duration:
            for part in cycle:
                length = min(part["duration_ms"] / 1000, duration - cursor)
                if length <= 0:
                    break
                plan.append({"phase": part["label"], "type": part["type"], "duration": length, "round": len(plan) // len(cycle) + 1})
                cursor += length
        return kind, protocol["title"], plan
    if kind == "interval":
        rounds = int(bounded(options.get("rounds", 8), 1, 200, "Rounds"))
        work = bounded(options.get("work_seconds", 40), 1, 3600, "Work interval")
        rest = bounded(options.get("rest_seconds", 20), 0, 3600, "Rest interval")
        plan = []
        for key, phase in (("warmup_seconds", "Warm up"),):
            duration = bounded(options.get(key, 0), 0, 3600, phase)
            if duration:
                plan.append({"phase": phase, "type": "rest", "duration": duration, "round": 0})
        for i in range(rounds):
            plan.append({"phase": "Work", "type": "work", "duration": work, "round": i + 1})
            if rest and i < rounds - 1:
                plan.append({"phase": "Rest", "type": "rest", "duration": rest, "round": i + 1})
        cooldown = bounded(options.get("cooldown_seconds", 0), 0, 3600, "Cool down")
        if cooldown:
            plan.append({"phase": "Cool down", "type": "rest", "duration": cooldown, "round": rounds})
        return kind, str(options.get("title", "Interval session"))[:100], plan
    if kind == "workout":
        duration = bounded(options.get("duration_minutes", 60), 1, 720, "Workout duration") * 60
        return kind, str(options.get("title", "Live workout"))[:100], [{"phase": "Workout", "type": "work", "duration": duration, "round": 1}]
    raise ValueError("Choose breathe, interval or workout")


class Companion:
    def __init__(self, manager, features):
        self.manager, self.features = manager, features
        self.worn = None
        self.charging = None
        self.events = []
        self._seen_events = set()
        self._responses = {}
        self._request_lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._probe_tasks = set()
        self.probes = {}
        self.active = None
        self.recent = []
        self._timer = None
        self.alarm = {"status": "No alarm armed by BOOP"}
        self._last_nudge = 0
        self._last_tap = 0
        self._housekeeper = None
        self._movement_at = time.monotonic()
        self._guardian = None
        self._guardian_hr_at = None
        self._guardian_stale_since = None
        self._pulse_at = 0
        self._session_feedback = None
        self._stress_rr=[]
        self._stress_at=None
        self._stress_state=None
        self._stress_decision=None
        self._stress_motion=None
        self._stress_checkin=None
        self.alarm_schedule = None
        self._alarm_rearmed_day = None
        self._wind_down_for = None
        self.rules = dict(DEFAULT_RULES)
        self.wind_down = dict(DEFAULT_WIND_DOWN)
        self._independent_wind_down_for = None
        with contextlib.closing(features.store.connect()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS boop_control_settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key='automations'").fetchone()
            if row:
                self.rules.update(json.loads(row[0]))
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key='active_session'").fetchone()
            if row:
                self.active = json.loads(row[0])
                self.active.update(paused=True, anchor=time.monotonic())
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key='alarm_schedule'").fetchone()
            if row:
                self.alarm_schedule = json.loads(row[0])
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key='wind_down'").fetchone()
            if row:
                with contextlib.suppress(ValueError,TypeError): self.wind_down=validate_wind_down(json.loads(row[0]))
        # A saved local explicit opt-in is retained; backup restore never copies this table.
        saved = features.list_records("journal") + features.list_records("workout")
        self.recent = sorted((row for row in saved if row.get("source") == "BOOP live session"),key=lambda row:row.get("ended_ms",0))[-10:]

    def on_frame(self, frame, received, syncing):
        if not syncing:
            values = sensor_values(frame)
            if values and isinstance(values.get('motion_rms'),(int,float)):
                self._stress_motion=(received,values['motion_rms'])
            if values and (values.get("motion_rms") or 0) > .035:
                self._movement_at = time.monotonic()
        if frame[4] == 36 and len(frame) - 4 >= 9:
            future = self._responses.get(frame[6])
            if future is not None and not future.done() and not syncing:
                future.set_result(frame)
        info = event(frame)
        if not info or syncing or abs(received / 1000 - info["device_seconds"]) > 30:
            return
        key = (info["number"], info["device_seconds"])
        if key in self._seen_events:
            return
        self._seen_events.add(key)
        if len(self._seen_events) > 256:
            self._seen_events = {key}
        number = info["number"]
        if number in (9, 10):
            self.worn = number == 9
        if number in (7, 8):
            self.charging = number == 7
        self.events.append({**info, "received_ms": received})
        self.events = self.events[-40:]
        if number in (9, 10, 14) and self.rules["enabled"]:
            name = {9: "wrist_on", 10: "wrist_off", 14: "double_tap"}[number]
            if number == 14 and time.monotonic() - self._last_tap < 1.2:
                return
            if number == 14:
                self._last_tap = time.monotonic()
            asyncio.create_task(self.run_action(self.rules[name]))

    def observe_rr(self, received, values):
        self.observe_stress(received,values)
        if not self.active or self.active["paused"] or self.active["kind"] != "resonance":
            return
        status = self.session_status()["active"]
        part = self.active["plan"][status["phase_index"]]
        groups = self.active.setdefault("pace_samples", {})
        group = groups.setdefault(str(part["pace_index"]), {"bpm":part["pace_bpm"],"start_ts":received/1000,"end_ts":received/1000,"rr":[]})
        group["end_ts"] = received/1000
        group["rr"].extend({"ts":received/1000,"rr_ms":value} for value in values)
        group["rr"] = group["rr"][-3000:]

    def observe_stress(self,received,values):
        # Manager calls this once per actual new live RR packet, never banked history.
        if not values or not self.rules['enabled'] or not self.rules['stress_check_in']:
            return
        if not self.manager.connected or not self.manager.bonded or self.manager._sync_active or self.worn is False:
            return
        if self._stress_at is not None and received-self._stress_at>120000:
            self._stress_rr=[]; self._stress_state=None
        if received==self._stress_at:
            return
        self._stress_at=received
        self._stress_rr=(self._stress_rr+list(values))[-120:]
        from stress_onset import evaluate
        settings=self.features.settings()
        local=datetime.fromtimestamp(received/1000,zone(settings['timezone']))
        def minutes(value):
            h,m=map(int,value.split(':'));return h*60+m
        fresh=self.manager.hr_at is not None and 0<=received-self.manager.hr_at<=10000
        motion=self._stress_motion[1] if self._stress_motion and 0<=received-self._stress_motion[0]<=10000 else None
        active=bool(self.active)
        lifting=getattr(self.manager,'workouts',None)
        if lifting:
            active=active or bool(lifting.lift_status().get('active'))
        decision=evaluate(self._stress_rr,self.manager.hr if fresh else None,motion,active,self._stress_state,
                          dict(enabled=True,auto_nudge=True,quiet_hours_enabled=True,
                               quiet_start_minutes=minutes(settings['quiet_hours_start']),quiet_end_minutes=minutes(settings['quiet_hours_end'])),
                          received/1000,int(local.utcoffset().total_seconds()),live=True,replay=False)
        self._stress_state=decision['next_state'];self._stress_decision={**decision,'observed_ms':received}
        if decision['should_nudge']:
            self._stress_checkin=dict(id=uuid.uuid4().hex,created_ms=received,
                                     message='Your beat-interval variation dipped while at rest. A short breathing break is available if it suits you.',
                                     label='A quiet check-in',source='NOOP sustained HRV-dip proxy',diagnosis=False)
            if self.rules['stress_haptics']:
                asyncio.create_task(self._stress_buzz())

    async def _stress_buzz(self):
        try:
            await self.buzz()
        except (ValueError,RuntimeError):
            self.manager.log('The opted-in check-in could not cue the strap')

    def checkin(self,body=None):
        if body is not None:
            if not isinstance(body,dict) or body.get('action')!='dismiss':
                raise ValueError('Choose dismiss check-in')
            self._stress_checkin=None
        if self._stress_checkin and time.time()*1000-self._stress_checkin['created_ms']>1800000:
            self._stress_checkin=None
        return dict(active=self._stress_checkin,enabled=self.rules['enabled'] and self.rules['stress_check_in'],
                    haptics_enabled=self.rules['stress_haptics'],last_decision=self._stress_decision,
                    note='Opt-in live, sustained HRV-dip check-in; quiet hours and manual sessions suppress cues.')

    async def request(self, number, payload=b"\x00", timeout=5, require_success=True, experimental=False):
        async with self._request_lock:
            future = asyncio.get_running_loop().create_future()
            self._responses[number] = future
            try:
                if experimental: await self.manager.send(number,payload,experimental=True)
                else: await self.manager.send(number, payload)
                frame = await asyncio.wait_for(future, timeout)
                code = frame[8]
                if require_success and code != 1:
                    raise ValueError({0: "Strap rejected the command", 2: "Strap reports command pending", 3: "Firmware does not support this command"}.get(code, f"Strap result {code}"))
                return frame
            except asyncio.TimeoutError:
                raise ProbeTimeout("The write reached Bluetooth but the strap did not confirm its result")
            finally:
                self._responses.pop(number, None)

    async def buzz(self, loops=1):
        if not self.manager.connected or self.manager._sync_active:
            raise ValueError("Connect the strap and finish history sync before using haptics")
        await self.manager.send(79, bytes((2, int(bounded(loops, 1, 8, "Buzz loops")), 0, 0, 0)))

    async def device(self, body):
        action = body.get("action")
        if self.manager._sync_active:
            raise ValueError("Finish or stop history sync before changing device controls")
        if self._probe_lock.locked():
            raise ValueError('A device probe is already running; wait for its bounded result')
        if action in ('extended-battery','body-location','feature-flags','config-values','reboot-probe'):
            if not self.manager.connected: raise ValueError('Connect the strap first')
            if action in ('feature-flags','config-values','reboot-probe') and body.get('experimental') is not True:
                raise ValueError('Explicitly enable this experimental diagnostic request')
            async with self._probe_lock:
                return await self.device_probe(action,body)
        if action == "buzz":
            await self.buzz(3)
            await self.manager.send(68, b"\x01")
            result = {"message": "Buzz commands sent; physical vibration requires strap verification"}
        elif action == "stop-haptics":
            frame = await self.request(122)
            result = {"message": "Stop haptics acknowledged"}
        elif action == "broadcast":
            enabled = body.get("enabled", body.get("value", False))
            if not isinstance(enabled, bool):
                raise ValueError("Broadcast enabled must be true or false")
            frame = await self.request(14, bytes((int(enabled),)))
            result = {"message": f"HR broadcast {'enabled' if enabled else 'disabled'} request acknowledged; advertising has no readback"}
        elif action == "rename":
            name = str(body.get("name", body.get("value", ""))).strip()
            if not name or "\x00" in name or len(name.encode("utf-8")) > 24:
                raise ValueError("Name must contain 1–24 UTF-8 bytes and no NUL")
            await self.manager.send(77, bytes(2) + name.encode("utf-8") + bytes(1))
            result = {"message": "Rename sent; the strap may restart and the name is verified on reconnect"}
        elif action == "clock":
            await self.manager.correct_clock(self.manager._generation)
            result = {"message": "Clock update sent; watch Clock verified for timestamp readback"}
        elif action in ("read-alarm", "disable-alarm", "set-alarm"):
            repeat_days = body.get("repeat_days", [])
            if isinstance(repeat_days,str):
                try:
                    repeat_days = [int(v.strip()) for v in repeat_days.split(",") if v.strip()]
                except ValueError:
                    raise ValueError("Repeat days use Monday=0 through Sunday=6")
            if not isinstance(repeat_days,list) or any(type(v) is not int or not 0<=v<=6 for v in repeat_days):
                raise ValueError("Repeat days use Monday=0 through Sunday=6")
            wind_down = int(bounded(body.get("wind_down_minutes",0),0,180,"Wind-down reminder"))
            if action == "disable-alarm":
                reply = await self.request(69, b"\x01", require_success=False)
                self.alarm = {"status": "Disable alarm replied; checking readback", "enabled": None,"result_byte":reply[8]}
                self.alarm_schedule = None
                await asyncio.to_thread(self.save_alarm_schedule)
            elif action == "set-alarm":
                settings = self.features.settings()
                clock = datetime.now(zone(settings["timezone"]))
                try:
                    hour, minute = map(int, str(body.get("time", body.get("value", ""))).split(":"))
                    target = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
                except (ValueError, TypeError):
                    raise ValueError("Wake time must be HH:MM")
                if target <= clock:
                    target += timedelta(days=1)
                if repeat_days:
                    while target.weekday() not in repeat_days:
                        target += timedelta(days=1)
                if not self.manager.clock_fixed:
                    raise ValueError("Wait for the strap clock to be verified before arming an alarm")
                seconds = int(target.timestamp())
                reply = await self.request(66, b"\x01" + seconds.to_bytes(4, "little") + bytes(4), require_success=False)
                self.alarm = {"status": "Set alarm replied; checking readback", "enabled": None,
                              "wake_ms": seconds * 1000, "time": target.isoformat(), "device": self.manager.address,"result_byte":reply[8]}
            frame = await self.request(67, b"\x01", require_success=False)
            self.alarm["readback_hex"] = frame[9:-4].hex()
            self.alarm["readback_at_ms"] = int(time.time() * 1000)
            decoded = alarm_readback(frame[9:-4])
            self.alarm["reported_epoch"] = decoded["epoch"]
            if decoded["no_alarm"]:
                self.alarm.update(status="Strap reports no alarm stored",enabled=False,verified=False)
            elif decoded["epoch"]:
                expected=self.alarm.get("wake_ms",0)/1000
                matches=bool(expected and abs(decoded["epoch"]-expected)<=5)
                self.alarm.update(status="Strap reports the requested wake time" if matches else "Strap reports an alarm; inspect its time",enabled=True,verified=matches,reported_time=datetime.fromtimestamp(decoded["epoch"],zone(self.features.settings()["timezone"])).isoformat())
            else:
                self.alarm.update(status="Alarm reply layout not recognized; wake time unverified",verified=False)
            if action == "set-alarm":
                await asyncio.to_thread(self.features.save_record,"alarm",self.alarm)
                self.alarm_schedule = {"time":body.get("time",body.get("value")),"repeat_days":repeat_days,
                                       "wake_ms":self.alarm["wake_ms"],"wind_down_minutes":wind_down,
                                       "verified":self.alarm.get("verified",False),"device":self.manager.address}
                await asyncio.to_thread(self.save_alarm_schedule)
            result = {"message": self.alarm["status"], "alarm": self.alarm}
        elif action in ("diagnostics", "get-data-range"):
            commands = (26, 7, 11, 34, 76) if action == "diagnostics" else (34,)
            responses = {}
            for number in commands:
                try:
                    frame = await self.request(number)
                    responses[str(number)] = {"result": "acknowledged", "payload_hex": frame[9:-4].hex()}
                except ValueError as exc:
                    responses[str(number)] = {"error": str(exc)}
                await asyncio.sleep(.25)
            result = {"message": "Read-only device diagnostics completed", "responses": responses}
        else:
            raise ValueError("Unknown device control")
        self.manager.log(result["message"])
        return result

    def _save_control(self,key,value):
        with contextlib.closing(self.features.store.connect()) as conn:
            with conn:
                conn.execute('INSERT OR REPLACE INTO boop_control_settings VALUES(?,?)',(key,json.dumps(value)))

    async def _probe_response(self,number,payload=b'',experimental=False,parser=None):
        try:
            frame=await self.request(number,payload,timeout=8,require_success=False,experimental=experimental)
        except ProbeTimeout:
            return dict(status='silent',command=number,timeout_seconds=8)
        except (ValueError,RuntimeError) as exc:
            return dict(status='transport_error',command=number,error=str(exc))
        try:
            return parser(frame) if parser else dict(status='answered',command=number,raw_hex=frame.hex())
        except ValueError as exc:
            return dict(status='undecodable',command=number,error=str(exc),raw_hex=frame.hex())

    async def device_probe(self,action,body):
        if action=='reboot-probe':
            if body.get('confirm') is not True: raise ValueError('Confirm the selected unverified BOOP strap restart probe')
            if not getattr(self.manager,'bonded',False): raise ValueError('Connect and bond the strap before a reboot probe')
            candidate=body.get('candidate')
            if candidate not in REBOOT_CANDIDATES: raise ValueError('Choose one of the five fixed BOOP strap reboot candidates')
            number,payload=REBOOT_CANDIDATES[candidate]
            for task in tuple(self._probe_tasks): task.cancel()
            self.probes[action]=dict(status='waiting',candidate=candidate,command=number,payload_hex=payload.hex(),started_ms=int(time.time()*1000),verified_reboot=False,note='Link loss alone does not establish a physical reboot; observe the sensor light')
            await self.manager.send(number,payload,experimental=True)
            task=asyncio.create_task(self._watch_reboot())
            self._probe_tasks.add(task); task.add_done_callback(self._probe_tasks.discard)
            return dict(message='Experimental candidate sent; watch its connection outcome',probe=dict(self.probes[action]))
        if action in ('extended-battery','body-location'):
            number=98 if action=='extended-battery' else 84
            with contextlib.closing(self.features.store.connect()) as conn:
                row=conn.execute('SELECT value_json FROM boop_control_settings WHERE key=?',('probe_previous_'+action,)).fetchone()
            previous=json.loads(row[0]) if row else None
            result=await self._probe_response(number,parser=lambda frame:diagnostic_probe(frame,number,previous))
            if result.get('payload_hex'):
                await asyncio.to_thread(self._save_control,'probe_previous_'+action,result['payload_hex'])
        elif action=='feature-flags':
            result=await self._enumerate_flags()
        elif action=='config-values':
            keys=body.get('keys')
            default_plan=keys is None
            if keys is None: keys=[*CONFIG_KEYS,*OXYGEN_GUESSES]
            if not isinstance(keys,list) or not 1<=len(keys)<=32 or len(set(str(k) for k in keys))!=len(keys): raise ValueError('Supply 1–32 distinct config keys')
            for key in keys: config_read_body(key)
            trace=[]; served={}; steps=0
            # Discovery uses the two source-pinned known-good keys. A silent/undecodable
            # verb is not repeatedly probed against a list of guessed keys.
            for number,key in ((121,'whoop_live_hr_in_adv_ind_pkt'),(128,'enable_r22_packets')):
                reply=await self._probe_response(number,config_read_body(key),True,lambda frame,n=number,k=key:config_reply(frame,n,k))
                reply['guess']=False; trace.append(reply); steps+=1
                served[number]=reply['status']=='answered'
            plan=[(128,key) for key in CONFIG_KEYS]+[(121,key) for key in OXYGEN_GUESSES] if default_plan else [(number,key) for key in keys for number in (121,128)]
            for number,key in plan:
                if steps>=64: break
                if not served[number] or (number==121 and key=='whoop_live_hr_in_adv_ind_pkt') or (number==128 and key=='enable_r22_packets'): continue
                reply=await self._probe_response(number,config_read_body(key),True,lambda frame,n=number,k=key:config_reply(frame,n,k))
                reply['guess']=key in OXYGEN_GUESSES; trace.append(reply); steps+=1
                if reply['status']!='answered': served[number]=False
            result=dict(status='answered' if any(r['status']=='answered' for r in trace) else 'unavailable',trace=trace,verbs={str(number):next(r['status'] for r in trace if r['command']==number) for number in (121,128)},steps=steps,max_steps=64,note='Read-only; BOOP strap result-code meaning is unconfirmed. Values require an exact echoed padded key. Oxygen key names are guesses.')
        else: raise ValueError('Unknown probe')
        self.probes[action]=result
        return dict(message='Device diagnostic completed',probe=result)

    async def _enumerate_flags(self):
        start=await self._probe_response(117,b'\x01',True,lambda frame:enumeration_reply(frame,117))
        if start.get('status') in ('silent','undecodable','transport_error'): return start
        count=start['count']; keys=[]; trace=[start]; last_index=None; empty_run=0
        stop='stepCap'; steps=0
        for i in range(128):
            reply=await self._probe_response(118,b'\x01',True,lambda frame:enumeration_reply(frame,118))
            trace.append(reply); steps+=1
            if reply.get('status') in ('silent','undecodable','transport_error'): stop=reply['status']; break
            index=reply['index']
            if index==255: stop='endMarker'; break
            if not reply['valid_key']:
                empty_run+=1
                if index==last_index: stop='emptySlotCursorParked'; break
                if empty_run>=8: stop='emptySlotRunCap'; break
            else:
                empty_run=0
                if reply['key'] and reply['key'] not in keys: keys.append(reply['key'])
            last_index=index
            if 0<count<=128 and steps>=count+4: stop='announcedCountOvershoot'; break
        return dict(status='answered',keys=keys,trace=trace,reported_count=count,stop_code=stop,steps=steps,complete=stop=='endMarker',note='Read-only key names; no feature value is written or inferred')

    async def _watch_reboot(self):
        started=time.monotonic(); dropped=False
        while True:
            connected=self.manager.connected
            dropped=dropped or not connected
            state=reboot_probe_state(time.monotonic()-started,connected,dropped)
            self.probes['reboot-probe']['status']=state
            if state in ('no_disconnect','reconnected','unsettled'): return
            await asyncio.sleep(.25)

    def automation_settings(self, body=None):
        if body is None:
            now=datetime.now(zone(self.features.settings()['timezone']))
            upcoming=[v for v in wind_down_times(now,self.wind_down) if v>now]
            return {**self.rules, "worn": self.worn, "last_nudge_ms": self._last_nudge,
                    'wind_down':{**self.wind_down,'next_nudge':upcoming[0].isoformat() if upcoming else None}}
        body=dict(body)
        wind_down=validate_wind_down(body.pop('wind_down'),self.wind_down) if 'wind_down' in body else self.wind_down
        allowed = set(DEFAULT_RULES)
        if set(body) - allowed:
            raise ValueError("Unknown automation setting")
        config = {**self.rules, **body}
        for key in ("double_tap", "wrist_off", "wrist_on"):
            if config[key] not in ("none", "mark", "buzz", "lock", "shortcut"):
                raise ValueError("Choose none, mark, buzz, lock or shortcut")
        for key in ("enabled", "hr_zone_haptics","stress_check_in","stress_haptics"):
            if not isinstance(config[key], bool):
                raise ValueError(f"{key} must be true or false")
        config["inactivity_minutes"] = int(bounded(config["inactivity_minutes"], 0, 240, "Inactivity threshold"))
        path = config["shortcut_path"]
        if path and (not isinstance(path, str) or not Path(path).is_absolute() or Path(path).suffix.lower() != ".lnk" or not Path(path).is_file()):
            raise ValueError("Select an existing local Windows .lnk shortcut")
        if "shortcut" in (config["double_tap"], config["wrist_off"], config["wrist_on"]) and not path:
            raise ValueError("A shortcut action needs a local .lnk path")
        with contextlib.closing(self.features.store.connect()) as conn:
            with conn:
                conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('automations',?)", (json.dumps(config),))
        self.rules = config
        if not config['enabled'] or not config['stress_check_in']:
            self._stress_checkin=None;self._stress_rr=[];self._stress_state=None;self._stress_decision=None
        self._save_control('wind_down',wind_down)
        self.wind_down=wind_down
        return self.automation_settings()

    async def run_action(self, action):
        try:
            if action == "mark":
                await asyncio.to_thread(self.features.save_record, "journal", {"title": "Strap moment", "date": datetime.now(zone(self.features.settings()["timezone"])).date().isoformat(), "timestamp_ms": int(time.time()*1000), "source": "strap gesture"})
            elif action == "buzz":
                await self.buzz()
            elif action == "lock" and os.name == "nt":
                import ctypes
                ctypes.windll.user32.LockWorkStation()
            elif action == "shortcut" and os.name == "nt" and self.rules["shortcut_path"]:
                await asyncio.to_thread(os.startfile, self.rules["shortcut_path"])
        except Exception as exc:
            self.manager.log(f"Local automation could not run: {exc}")

    def session_status(self):
        if self.active:
            session = self.active
            elapsed = session["elapsed"] if session["paused"] else session["elapsed"] + time.monotonic() - session["anchor"]
            cursor, index = 0.0, len(session["plan"]) - 1
            for i, part in enumerate(session["plan"]):
                if elapsed < cursor + part["duration"]:
                    index = i
                    break
                cursor += part["duration"]
            part = session["plan"][index]
            total = sum(p["duration"] for p in session["plan"])
            cursor = sum(p["duration"] for p in session["plan"][:index])
            active = {k: session[k] for k in ("id", "kind", "title", "started_ms", "paused", "haptics", "pre_hrv_ms")}
            active.update(elapsed_seconds=round(elapsed, 2), remaining_seconds=round(max(0, total-elapsed), 2),
                          phase=part["phase"], phase_type=part["type"], phase_remaining_seconds=round(max(0, cursor+part["duration"]-elapsed), 2),
                          round=part["round"], rounds=max(p["round"] for p in session["plan"]), phase_index=index,
                          heart_rate=self.manager.hr)
            if self._session_feedback:
                active["feedback"] = self._session_feedback
        else:
            active = None
        return {"active": active, "recent": self.recent[-10:]}

    async def session_action(self, body):
        action = body.get("action", "start")
        if action == "start":
            if self.active:
                raise ValueError("Stop the current session before starting another")
            kind, title, plan = make_plan(body)
            haptics = body.get("haptics", False)
            if not isinstance(haptics, bool):
                raise ValueError("Haptics must be true or false")
            if haptics and (not self.manager.connected or self.manager._sync_active):
                raise ValueError("Haptic sessions need a connected strap outside history sync")
            if kind in ("resonance", "hr-down", "live-session") and not self.manager.connected:
                raise ValueError("Connect the strap for live biofeedback")
            if kind == "hr-down" and (self.manager.hr is None or not 55 <= self.manager.hr <= 120):
                raise ValueError("HR-down pacing requires a fresh resting heart rate between 55 and 120 bpm")
            if body.get("charge") is not None:
                bounded(body["charge"],0,100,"Charge")
            status = await self.manager.status()
            self.active = {"id": uuid.uuid4().hex, "kind": kind, "title": title, "plan": plan,
                           "started_ms": int(time.time()*1000), "anchor": time.monotonic(), "elapsed": 0.0,
                           "paused": False, "haptics": haptics, "pre_hrv_ms": status.get("rmssd"), "options": body,"pre_hr":status.get("hr")}
            self._session_feedback = None
            self._guardian = None
            self._guardian_hr_at = self._guardian_stale_since = None
            if kind == "live-session":
                from biofeedback import LiveSessionEngine
                config = self.features.settings()
                self._guardian = LiveSessionEngine({"resting_hr":config["hr_rest"],"hr_max":config["hr_max"],"charge":body.get("charge")},time.time())
            self._pulse_at = time.monotonic()
            self._timer = asyncio.create_task(self.run_timer())
            await asyncio.to_thread(self.save_session)
        elif action == "pause" and self.active:
            if not self.active["paused"]:
                self.active["elapsed"] += time.monotonic() - self.active["anchor"]
                self.active["paused"] = True
                await asyncio.to_thread(self.save_session)
        elif action == "resume" and self.active:
            if self.active["paused"]:
                self.active["anchor"] = time.monotonic()
                self.active["paused"] = False
                if self.active["kind"] == "live-session" and self._guardian is None:
                    from biofeedback import LiveSessionEngine
                    config = self.features.settings()
                    self._guardian = LiveSessionEngine({"resting_hr":config["hr_rest"],"hr_max":config["hr_max"],"charge":self.active.get("options",{}).get("charge")},time.time())
                await asyncio.to_thread(self.save_session)
        elif action == "stop":
            if self._timer and self._timer is not asyncio.current_task():
                self._timer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._timer
            await self.finish_session(False)
        else:
            raise ValueError("No active session for that action")
        return self.session_status()

    async def run_timer(self):
        previous, previous_tick = None, None
        while self.active:
            status = self.session_status()["active"]
            if not status["paused"]:
                phase = status["phase_index"]
                live_kind = self.active["kind"]
                if live_kind == "hr-down" and time.monotonic() >= self._pulse_at:
                    from biofeedback import hr_down_step
                    fresh = self.manager.hr_at is not None and time.time()*1000-self.manager.hr_at<=10000
                    step = hr_down_step(self.manager.hr if fresh else None,status["elapsed_seconds"])
                    self._session_feedback = step
                    if step["stop"]:
                        await self.finish_session(True)
                        return
                    if status["haptics"]:
                        with contextlib.suppress(ValueError,RuntimeError):
                            await self.buzz()
                    self._pulse_at = time.monotonic() + step["interval_ms"]/1000
                if live_kind == "live-session" and self._guardian:
                    now = time.time()
                    new = self.manager.hr_at != self._guardian_hr_at and self.manager.hr_at is not None and now*1000-self.manager.hr_at <= 10000
                    self._guardian_hr_at = self.manager.hr_at
                    feedback = self._guardian.update(now,self.manager.hr if new else None)
                    self._session_feedback = feedback
                    if feedback["status"] == "stale":
                        self._guardian_stale_since = self._guardian_stale_since or now
                        if now-self._guardian_stale_since >= 600:
                            await self.finish_session(False)
                            return
                    else:
                        self._guardian_stale_since = None
                    if feedback.get("cue") and status["haptics"]:
                        with contextlib.suppress(ValueError,RuntimeError):
                            await self.buzz(2 if feedback["cue"] == "easeOff" else 1)
                if phase != previous:
                    previous = phase
                    if status["haptics"] and live_kind not in ("hr-down", "live-session"):
                        loops = 3 if status["phase_type"] == "work" else 2 if status["phase_type"] == "exhale" else 1
                        with contextlib.suppress(ValueError, RuntimeError):
                            await self.buzz(loops)
                tick = (phase, math.ceil(status["phase_remaining_seconds"]))
                if self.active["kind"] == "interval" and 0 < tick[1] <= 3 and tick != previous_tick and status["haptics"]:
                    with contextlib.suppress(ValueError, RuntimeError):
                        await self.buzz()
                previous_tick = tick
                if status["remaining_seconds"] <= 0:
                    await self.finish_session(True)
                    return
            await asyncio.sleep(.1)

    async def finish_session(self, completed):
        if not self.active:
            return
        status = self.session_status()["active"]
        summary = {**status, "completed": completed, "ended_ms": int(time.time()*1000), "post_hrv_ms": (await self.manager.status()).get("rmssd")}
        summary["duration_seconds"] = status["elapsed_seconds"]
        summary["start_ms"] = status["started_ms"]
        summary["end_ms"] = summary["ended_ms"]
        kind = "journal" if status["kind"] == "breathe" else "workout"
        summary["date"] = datetime.fromtimestamp(summary["started_ms"] / 1000, zone(self.features.settings()["timezone"])).date().isoformat()
        summary["source"] = "BOOP live session"
        summary["session_type"] = self.active["kind"]
        if kind == "workout":
            summary["sport"] = self.active.get("options",{}).get("sport") or self.active.get("options",{}).get("title") or ("interval" if self.active["kind"] == "interval" else "Workout")
        summary["pre_hr"] = self.active.get("pre_hr")
        summary["post_hr"] = self.manager.hr if self.manager.hr_at and time.time()*1000-self.manager.hr_at<=10000 else None
        if self.active["kind"] == "resonance":
            from biofeedback import score_resonance
            groups = self.active.get("pace_samples", {})
            summary["resonance"] = score_resonance(list(groups.values()))
        if self.active["kind"] in ("resonance", "hr-down", "live-session", "breathe-custom"):
            kind = "journal"
        await asyncio.to_thread(self.features.save_record, kind, summary)
        self.recent.append(summary)
        haptics = self.active["haptics"]
        self.active = None
        self._session_feedback = None
        self._guardian = None
        await asyncio.to_thread(self.save_session)
        if completed and haptics:
            with contextlib.suppress(ValueError, RuntimeError):
                await self.buzz(3)

    def status(self):
        return {"worn": self.worn, "charging": self.charging, "events": self.events[-12:],
                "alarm": self.alarm, "session": self.session_status(), "automations": self.automation_settings(),"probes":self.probes,
                "capabilities": {"whoop4": True, "ppg_raw": True, "imu_raw": True,
                                 "firmware_alarm": "implemented; motor/wake verification pending",
                                 "spo2": "Raw ADC only; no validated offline percentage", "mg_ecg": False}}

    def save_session(self):
        session = None
        if self.active:
            session = dict(self.active)
            session["elapsed"] = self.session_status()["active"]["elapsed_seconds"]
            session.pop("anchor", None)
        with contextlib.closing(self.features.store.connect()) as conn:
            with conn:
                if session:
                    conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('active_session',?)", (json.dumps(session),))
                else:
                    conn.execute("DELETE FROM boop_control_settings WHERE key='active_session'")

    def save_alarm_schedule(self):
        with contextlib.closing(self.features.store.connect()) as conn:
            with conn:
                if self.alarm_schedule:
                    conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES('alarm_schedule',?)",(json.dumps(self.alarm_schedule),))
                else:
                    conn.execute("DELETE FROM boop_control_settings WHERE key='alarm_schedule'")

    async def check_alarm_schedule(self,now):
        schedule=self.alarm_schedule
        if not schedule or schedule["device"]!=self.manager.address:
            return
        wake=schedule["wake_ms"]/1000
        bedtime=wake-self.features.settings()["sleep_goal_hours"]*3600
        if schedule["wind_down_minutes"] and 0<=bedtime-now.timestamp()<=schedule["wind_down_minutes"]*60 and self._wind_down_for!=wake:
            self._wind_down_for=wake
            # Windows app reminders respect the explicit notification opt-in and quiet hours.
            platform=getattr(self.manager,"platform",None)
            if platform:
                await asyncio.to_thread(platform.notify,"wind-down","Your planned bedtime is approaching. Review your sleep plan.")
        key=now.date().isoformat()
        if now.timestamp()>wake+60 and schedule["repeat_days"] and schedule["verified"] and self._alarm_rearmed_day!=key and self.manager.connected and not self.manager._sync_active and self.manager.clock_fixed:
            self._alarm_rearmed_day=key
            try:
                await self.device({"action":"set-alarm","time":schedule["time"],"repeat_days":schedule["repeat_days"],"wind_down_minutes":schedule["wind_down_minutes"]})
            except (ValueError,RuntimeError) as exc:
                self.manager.log(f"Repeat alarm could not be re-armed: {exc}")

    async def check_wind_down(self,now):
        for nudge in wind_down_times(now,self.wind_down):
            key=nudge.isoformat()
            if 0<=(now-nudge).total_seconds()<60 and key!=self._independent_wind_down_for:
                self._independent_wind_down_for=key
                settings=self.features.settings()
                clock=now.strftime('%H:%M'); begin=settings['quiet_hours_start']; end=settings['quiet_hours_end']
                quiet=begin<=clock<end if begin<end else (clock>=begin or clock<end) if begin!=end else False
                if not settings['notifications_enabled'] or quiet: return
                platform=getattr(self.manager,'platform',None)
                if platform:
                    await asyncio.to_thread(platform.notify,'wind-down:'+key,'Your planned bedtime is approaching. Begin your usual wind-down when it suits you.')

    async def start(self):
        if self.active:
            self._timer = asyncio.create_task(self.run_timer())
        self._housekeeper = asyncio.create_task(self.housekeeping())

    async def housekeeping(self):
        while True:
            await asyncio.sleep(10)
            if self.active:
                await asyncio.to_thread(self.save_session)
            settings = await asyncio.to_thread(self.features.settings)
            local = datetime.now(zone(settings["timezone"]))
            await self.check_alarm_schedule(local)
            await self.check_wind_down(local)
            clock = local.strftime("%H:%M")
            begin, end = settings["quiet_hours_start"], settings["quiet_hours_end"]
            quiet = begin <= clock < end if begin < end else (clock >= begin or clock < end) if begin != end else False
            if not self.rules["enabled"] or quiet or self.manager._sync_active or not self.manager.connected or self.worn is False:
                continue
            if time.monotonic() - self._last_nudge < 300:
                continue
            hr = self.manager.hr
            target_low, target_high = settings["hr_target_min"], settings["hr_target_max"]
            target_nudge = self.rules["hr_zone_haptics"] and hr is not None and ((target_low and hr < target_low) or (target_high and hr > target_high))
            inactivity = self.rules["inactivity_minutes"]
            if target_nudge or inactivity and time.monotonic() - self._movement_at >= inactivity * 60:
                with contextlib.suppress(ValueError, RuntimeError):
                    await self.buzz()
                    self._last_nudge = time.monotonic()

    async def close(self):
        for task in tuple(self._probe_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await task
        for task in (self._housekeeper, self._timer):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.active:
            if not self.active["paused"]:
                self.active["elapsed"] += time.monotonic() - self.active["anchor"]
                self.active["paused"] = True
            await asyncio.to_thread(self.save_session)
