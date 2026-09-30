"""BOOP: a local Windows companion for a WHOOP 4.0 you own."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import signal
import sqlite3
import time
from collections import deque
from pathlib import Path

from aiohttp import web
from bleak import BleakClient, BleakScanner

from storage import Store
from whoop_protocol import (Assembler, NOTIFY, SERVICE, STANDARD_HR, WRITE, battery,
                            command, decode, firmware, history_end, standard_hr)

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = logging.getLogger("boop")


class Manager:
    def __init__(self, store: Store):
        self.store = store
        self.client = None
        self.devices = {}
        self.address = None
        self.name = None
        self.phase = "Disconnected"
        self.error = None
        self.hr = None
        self.hr_at = None
        self.battery = None
        self.battery_at = None
        self.firmware = None
        self.mtu = None
        self.clock_drift = None
        self.clock_fixed = False
        self.frames_received = 0
        self.invalid_frames = 0
        self.saved_frames = 0
        self.rr = deque(maxlen=3000)
        self.logs = deque(maxlen=80)
        self.queue = asyncio.Queue(maxsize=10000)
        self._write_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._worker = None
        self._connector = None
        self._want_connection = False
        self._generation = 0
        self._sequence = 0
        self._assemblers = {}
        self._clock_correcting = False
        self.clock_fixed = False
        self.clock_drift = None
        self._sync_active = False
        self._sync_generation = None
        self._sync_tainted = False
        self._sync_count = 0
        self._sync_chunks = 0
        self._sync_started = None
        self._sync_activity = None
        self._sync_result = "No history sync yet"
        self._sync_last_ack = {}
        self._last_rr_time = None
        self._disk_error = False
        self.companion = None
        self.features = None
        self._hrv_stream_armed = None
        self.spot_capture_until = 0
        self.bonded = False

    def log(self, text):
        self.logs.append({"time": int(time.time() * 1000), "message": text})
        LOG.info(text)

    @property
    def connected(self):
        return bool(self.client and self.client.is_connected)

    async def start(self, auto_connect=True):
        self._worker = asyncio.create_task(self.persist_worker())
        settings = DATA / "settings.json"
        if auto_connect and settings.exists():
            try:
                config = json.loads(settings.read_text(encoding="utf-8"))
                self.address, self.name = config["address"], config.get("name", "BOOP")
                self.connect(self.address)
            except (ValueError, KeyError):
                self.log("Saved connection settings could not be read; use Scan.")

    async def scan(self):
        if self._operation_lock.locked():
            raise ValueError("A Bluetooth operation is already running")
        async with self._operation_lock:
            self.phase = "Scanning" if not self.connected else "Connected · scanning"
            found = await BleakScanner.discover(timeout=8, return_adv=True)
            self.devices = {d.address: d for d, a in found.values() if SERVICE in a.service_uuids}
            self.phase = "Connected" if self.connected else "Disconnected"
            return [{"address": d.address, "name": d.name or "BOOP"} for d in self.devices.values()]

    def connect(self, address):
        if self._connector and not self._connector.done():
            raise ValueError("A connection is already active; disconnect before choosing another strap")
        if not isinstance(address, str) or len(address) != 17:
            raise ValueError("Choose a BOOP strap from Scan")
        self.address = address.upper()
        self.error = None
        self._want_connection = True
        self._connector = asyncio.create_task(self.connection_loop())

    async def connection_loop(self):
        failures = 0
        while self._want_connection:
            try:
                await self.session()
                failures = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                failures += 1
                self.error = str(exc) or type(exc).__name__
                self.log(f"Connection attempt failed: {self.error}")
            finally:
                self.client = None
                self.hr = None
                self.hr_at = None
                self._sync_active = False
            if self._want_connection:
                delay = min(30, 5 * max(failures, 1))
                self.phase = f"Reconnecting in {delay}s"
                await asyncio.sleep(delay)
        self.phase = "Disconnected"

    async def session(self):
        self.phase = "Finding strap"
        dev = self.devices.get(self.address)
        if dev is None:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=12)
        if dev is None:
            raise RuntimeError("Strap not advertising. Keep it near the laptop, turn phone Bluetooth off, and tap the strap to wake it.")
        self._generation += 1
        self._assemblers = {}
        self._sequence = 0
        self._clock_correcting = False
        self.clock_fixed = False
        self.clock_drift = None
        self.rr.clear()
        self._last_rr_time = None
        self.phase = "Connecting and pairing"
        async with BleakClient(dev, pair=True, timeout=30, winrt={"use_cached_services": False}) as client:
            self.client = client
            self.bonded = True  # WinRT pairing completed before this connected context.
            self.name = dev.name or self.name or "BOOP"
            self.mtu = client.mtu_size
            if client.services.get_characteristic(WRITE) is None:
                raise RuntimeError("This device does not expose the required BOOP strap service")
            for uuid in NOTIFY:
                await client.start_notify(uuid, self.on_notify)
            await asyncio.sleep(.3)
            if client.services.get_characteristic(STANDARD_HR):
                try:
                    await client.start_notify(STANDARD_HR, self.on_standard_hr)
                except Exception as exc:
                    self.log(f"Standard HR unavailable; using the BOOP strap data stream: {exc}")
            await self.send(26)
            await asyncio.sleep(.25)
            await self.send(35)  # opens the session; secret-bearing body is never saved/logged
            await asyncio.sleep(.25)
            await self.send(7)
            await asyncio.sleep(.25)
            await self.send(11)
            await asyncio.sleep(.25)
            await self.arm_live()
            self.phase = "Connected"
            self.error = None
            settings = DATA / "settings.json"
            tmp = settings.with_suffix(".tmp")
            tmp.write_text(json.dumps({"address": self.address, "name": self.name}), encoding="utf-8")
            tmp.replace(settings)
            self.log(f"Connected to {self.name}; recording to this laptop.")
            last_arm = last_battery = last_sync = time.monotonic()
            try:
                while self._want_connection and client.is_connected:
                    await asyncio.sleep(1)
                    if self._sync_active:
                        if self._sync_tainted:
                            await self.stop_sync("History paused after a corrupt/missing frame; the affected chunk was not acknowledged. Retry Sync.")
                        elif time.time() - self._sync_activity > 90 or time.time() - self._sync_started > 900:
                            await self.stop_sync("History paused after the strap stopped sending data. Saved chunks remain on this laptop; retry Sync.")
                        continue
                    now = time.monotonic()
                    config = self.features.settings() if self.features else {}
                    saving = self.battery is not None and self.battery <= config.get("battery_threshold", 20)
                    if now - last_arm >= (30 if saving else 8):
                        desired = self.hrv_stream_wanted()
                        if desired != self._hrv_stream_armed or self.hr_at is None or int(time.time()*1000) - self.hr_at > 10000:
                            await self.send(63, bytes((int(desired),)))
                            self._hrv_stream_armed = desired
                            await asyncio.sleep(.25)
                        await self.send(3, b"\x01")
                        last_arm = now
                    if now - last_battery >= 60:
                        await self.send(26)
                        last_battery = now
                    cadence = config.get("auto_sync_minutes", 0)
                    if cadence and now - last_sync >= cadence * 60 and not (self.companion and self.companion.active):
                        last_sync = now
                        await self.sync()
            finally:
                if client.is_connected:
                    with contextlib.suppress(Exception):
                        if self._sync_active:
                            await self.send(20)
                        await self.send(63, b"\x00")
                        await self.send(3, b"\x00")
                self.log("Bluetooth connection closed; saved readings remain available.")

    async def send(self, number, payload=b"\x00", *, experimental=False):
        async with self._write_lock:
            if not self.connected:
                raise RuntimeError("Connect the strap first")
            frame = command(number, self._sequence, payload, experimental=experimental)
            self._sequence = (self._sequence + 1) & 255
            await asyncio.wait_for(self.client.write_gatt_char(WRITE, frame, response=True), timeout=10)

    async def arm_live(self):
        desired = self.hrv_stream_wanted()
        await self.send(63, bytes((int(desired),)))
        self._hrv_stream_armed = desired
        await asyncio.sleep(.25)
        await self.send(3, b"\x01")

    def hrv_stream_wanted(self):
        if time.time() < self.spot_capture_until:
            return True
        if not self.features:
            return True
        config = self.features.settings()
        if self.companion and self.companion.active:
            return True
        mode = config.get("hrv_capture_mode", "continuous")
        if mode == "continuous":
            return True
        if mode == "paused":
            return False
        from datetime import datetime
        from companion import zone
        now = datetime.now(zone(config["timezone"])).strftime("%H:%M")
        start, end = config["quiet_hours_start"], config["quiet_hours_end"]
        return start <= now < end if start < end else now >= start or now < end if start != end else False

    async def correct_clock(self, generation):
        self._clock_correcting = True
        try:
            if generation != self._generation or self._sync_active:
                return
            now = int(time.time()).to_bytes(4, "little")
            # Known firmware variants accept either eight or nine bytes. Verify in
            # subsequent realtime records; a write acknowledgement is not proof.
            for zeros in (4, 5):
                await self.send(10, now + bytes(zeros))
                await asyncio.sleep(.25)
            self.log("Clock correction sent; waiting for timestamp readback.")
        except Exception as exc:
            self.log(f"Clock correction could not complete: {exc}")

    def on_notify(self, sender, data):
        uuid = sender.uuid.lower()
        assembler = self._assemblers.setdefault(uuid, Assembler())
        errors = assembler.errors
        frames = assembler.feed(bytes(data))
        if assembler.errors != errors:
            self.invalid_frames += assembler.errors - errors
            if self._sync_active:
                self._sync_tainted = True
        received = int(time.time() * 1000)
        for frame in frames:
            self.frames_received += 1
            if frame[4] == 36 and frame[6] == 35:
                continue  # drop identity/key/signature body before any logging or disk write
            if self.companion:
                self.companion.on_frame(frame, received, self._sync_active)
            level = battery(frame)
            if level is not None:
                self.battery, self.battery_at = level, received
            version = firmware(frame)
            if version:
                self.firmware = version
            reading = decode(frame)
            if reading and reading.kind == "live":
                self.hr, self.hr_at = reading.heart_rate, received
                drift = received / 1000 - reading.device_seconds
                self.clock_drift = round(drift)
                if abs(drift) <= 30:
                    self.clock_fixed = True
                elif not self._clock_correcting and not self._sync_active:
                    asyncio.create_task(self.correct_clock(self._generation))
                if reading.rr_ms and self._last_rr_time != reading.device_seconds:
                    self._last_rr_time = reading.device_seconds
                    for value in reading.rr_ms:
                        self.rr.append((received, value))
                    if self.companion:
                        self.companion.observe_rr(received,reading.rr_ms)
            if self._sync_active and frame[4] in (47, 48, 49, 50):
                self._sync_activity = time.time()
                if frame[4] == 47:
                    self._sync_count += 1
            try:
                self.queue.put_nowait((self._generation, (self.address, received, uuid, frame)))
            except asyncio.QueueFull:
                self._sync_tainted = True
                self.error = "Disk writer is falling behind; history acknowledgements stopped."

    def on_standard_hr(self, sender, data):
        hr, rr, _contact = standard_hr(bytes(data))
        if self.companion and _contact is not None:
            self.companion.worn = _contact
        now = int(time.time() * 1000)
        if self.hr_at is None or now - self.hr_at > 5000:
            self.hr, self.hr_at = hr, now
            # Store fallback measurements using a valid WHOOP type-40 envelope.
            # The source characteristic still identifies these as standard BLE HR.
            try:
                self.queue.put_nowait((self._generation, (self.address, now, STANDARD_HR, bytes(data))))
            except asyncio.QueueFull:
                self._sync_tainted = True
            for value in rr:
                self.rr.append((now, value))
            if self.companion:
                self.companion.observe_rr(now,rr)

    @staticmethod
    def archive(records, end):
        with (DATA / "history-archive.jsonl").open("ab") as file:
            for device, received, char, frame in records:
                record = {"device": device, "received_ms": received, "characteristic": char, "hex": frame.hex()}
                file.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            file.write((json.dumps({"chunk_end": end.hex()}) + "\n").encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())

    async def persist_worker(self):
        pending = []
        generation = None
        last_flush = time.monotonic()
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=.8)
            except asyncio.TimeoutError:
                item = None
            if item:
                item_gen, record = item
                if pending and item_gen != generation:
                    await self.commit(pending, generation)
                    pending = []
                generation = item_gen
                pending.append(record)
                frame = record[3]
                is_standard = record[2] == STANDARD_HR
                end = None if is_standard else history_end(frame)
                complete = not is_standard and frame[4] == 49 and frame[6] == 3
                if end is not None or complete:
                    ok = await self.commit(pending, generation, end)
                    pending = []
                    last_flush = time.monotonic()
                    if complete and ok and self._sync_active and generation == self._sync_generation:
                        self._sync_active = False
                        self._sync_result = f"History saved · {self._sync_count:,} records received"
                        self.log(self._sync_result)
                        await asyncio.to_thread(self.store.backup, DATA / "last-sync-backup.sqlite")
                        if self.connected:
                            await self.arm_live()
                self.queue.task_done()
            # Retain a whole history chunk so its raw archive and database commit
            # both cover every record before an ACK can permit strap reclamation.
            in_history = self._sync_active and generation == self._sync_generation
            if len(pending) > 50000:
                self._sync_tainted = True
                self.error = "Oversized history chunk; sync will pause without acknowledgement."
            if pending and not in_history and (item is None or len(pending) >= 256 or time.monotonic() - last_flush >= 1):
                await self.commit(pending, generation)
                pending = []
                last_flush = time.monotonic()

    async def commit(self, records, generation, end=None):
        try:
            self.saved_frames += await asyncio.to_thread(self.store.save, records, end)
            if end is not None:
                await asyncio.to_thread(self.archive, records, end)
        except Exception as exc:
            self._disk_error = True
            self._sync_tainted = True
            self.error = f"Could not save readings: {exc}. History acknowledgement withheld."
            self.log(self.error)
            return False
        if end is not None and generation == self._generation and generation == self._sync_generation and self.connected and self._sync_active and not self._sync_tainted:
            now = time.monotonic()
            if now - self._sync_last_ack.get(end, -100) >= 2:
                try:
                    await self.send(23, b"\x01" + end)
                    self._sync_last_ack[end] = now
                    self._sync_chunks += 1
                except Exception as exc:
                    self.log(f"History acknowledgement failed; data is saved: {exc}")
        return True

    async def sync(self):
        async with self._operation_lock:
            if not self.connected:
                raise ValueError("Connect the strap first")
            if self._sync_active:
                raise ValueError("History sync is already running")
            if self._disk_error:
                raise ValueError("Fix the disk error and restart BOOP before syncing history")
            await asyncio.to_thread(self.store.backup, DATA / "before-sync-backup.sqlite")
            self._sync_active = True
            self._sync_generation = self._generation
            self._sync_tainted = False
            self._sync_count = self._sync_chunks = 0
            self._sync_last_ack = {}
            self._sync_started = self._sync_activity = time.time()
            self._sync_result = "Reading stored history"
            try:
                await self.send(3, b"\x00")
                await self.send(63, b"\x00")
                await asyncio.sleep(.6)
                await self.send(22)
            except Exception:
                self._sync_active = False
                raise
            self.log("History sync started. Every chunk is saved before its acknowledgement.")

    async def stop_sync(self, reason="History sync stopped; saved chunks remain on the laptop"):
        if self._sync_active and self.connected:
            await self.send(20)
        self._sync_active = False
        self._sync_result = reason
        self.log(reason)
        if self.connected:
            await self.arm_live()

    async def disconnect(self):
        self._want_connection = False
        if self.connected:
            if self._sync_active:
                await self.stop_sync()
            with contextlib.suppress(Exception):
                await self.send(63, b"\x00")
                await self.send(3, b"\x00")
            await self.client.disconnect()
        if self._connector and not self._connector.done():
            self._connector.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._connector
        self.client = None
        self.hr = self.hr_at = None
        self.phase = "Disconnected"
        # On the next manual Connect, rediscover an advertising device.
        self.devices.clear()

    async def close(self):
        if self.companion:
            await self.companion.close()
        await self.disconnect()
        await asyncio.wait_for(self.queue.join(), timeout=15)
        await asyncio.sleep(1)  # allow the remaining live batch to commit
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker

    async def status(self):
        now = int(time.time() * 1000)
        rr = [v for t, v in self.rr if now - t <= 300000]
        from analytics import hrv
        stamps = [t / 1000 for t, v in self.rr if now - t <= 300000]
        quality = hrv(rr, stamps, max_rejected_fraction=.35)
        rmssd = round(quality["value"], 1) if quality["value"] is not None else None
        live = self.connected and self.hr_at is not None and now - self.hr_at <= 10000
        return {"phase": self.phase, "connected": self.connected, "name": self.name,
                "address": self.address, "hr": self.hr if live else None, "hr_age_s": round((now - self.hr_at) / 1000) if self.hr_at else None,
                "battery": self.battery, "battery_at": self.battery_at, "firmware": self.firmware,
                "mtu": self.mtu, "clock_drift_s": self.clock_drift, "clock_verified": self.clock_fixed,
                "rr_count": len(rr), "rmssd": rmssd, "hrv_quality": quality, "frames_received": self.frames_received,
                "invalid_frames": self.invalid_frames, "error": self.error, "logs": list(self.logs)[-12:],
                "sync": {"active": self._sync_active, "records": self._sync_count, "chunks": self._sync_chunks, "message": self._sync_result},
                "store": await asyncio.to_thread(self.store.summary, self.address),
                "recording_awake_held": bool(getattr(getattr(self,"platform",None),"awake_held",False)),
                "device": self.companion.status() if self.companion else None}


def make_app(manager, stop_event, port=8765, feature_api=None):
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @web.middleware
    async def local_only(request, handler):
        if request.host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            raise web.HTTPForbidden(text="BOOP is available only on this laptop")
        if request.method not in ("GET", "HEAD") and (request.headers.get("Origin") not in origins or request.headers.get("X-Boop") != "local"):
            raise web.HTTPForbidden(text="Use the local BOOP dashboard")
        try:
            response = await handler(request)
        except (ValueError, RuntimeError, KeyError, TypeError, sqlite3.DatabaseError) as exc:
            response = web.json_response({"error": str(exc)}, status=409)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    app = web.Application(middlewares=[local_only], client_max_size=1024*1024*1024,
                          handler_args={'handler_cancellation':True})

    async def index(request):
        return web.FileResponse(ROOT / "web/index.html")

    async def status(request):
        return web.json_response(await manager.status())

    async def action(request):
        choice = request.match_info["action"]
        if choice == "scan":
            return web.json_response({"devices": await manager.scan()})
        if choice == "connect":
            body = await request.json()
            manager.connect(body.get("address", manager.address))
        elif choice == "disconnect":
            await manager.disconnect()
        elif choice == "sync":
            await manager.sync()
        elif choice == "stop-sync":
            await manager.stop_sync()
        elif choice == "shutdown":
            stop_event.set()
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})

    async def series(request):
        hours = min(720, max(.05, float(request.query.get("hours", 1))))
        return web.json_response(await asyncio.to_thread(manager.store.series, manager.address, hours))

    async def days(request):
        return web.json_response(await asyncio.to_thread(manager.store.days, manager.address))

    async def export(request):
        if request.match_info["format"] == "csv":
            text = await asyncio.to_thread(manager.store.export_csv, manager.address)
            return web.Response(text=text, content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="boop-readings.csv"'})
        if request.match_info["format"] == "sqlite":
            destination = DATA / "export.sqlite"
            await asyncio.to_thread(manager.store.backup, destination)
            return web.FileResponse(destination, headers={"Content-Disposition": 'attachment; filename="boop-backup.sqlite"'})
        if feature_api:
            return await feature_api.export(request)
        raise web.HTTPNotFound()

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/series", series)
    app.router.add_get("/api/days", days)
    if feature_api:
        feature_api.register(app)
    app.router.add_get("/export/{format}", export)
    app.router.add_post("/api/{action}", action)
    app.router.add_static("/static", ROOT / "web")
    return app


async def run(args):
    from analytics import AnalyticsService
    from api import FeatureAPI
    from companion import Companion
    from features import FeatureStore
    from windows_platform import WindowsPlatform
    DATA.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[logging.FileHandler(DATA / "boop.log", encoding="utf-8")])
    manager = Manager(Store(DATA / "whoop.sqlite"))
    features = FeatureStore(manager.store)
    manager.features = features
    manager.companion = Companion(manager, features)
    await asyncio.to_thread(manager.store.retro_decode_sensors)
    stop = asyncio.Event()
    platform = WindowsPlatform(manager, features, stop, args.port)
    manager.platform = platform
    feature_api = FeatureAPI(manager, features, AnalyticsService(manager.store), manager.companion, platform)
    runner = web.AppRunner(make_app(manager, stop, args.port, feature_api))
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    await manager.start(auto_connect=not args.no_auto_connect)
    await manager.companion.start()
    await platform.start()
    if args.connect:
        manager.connect(args.connect)
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    print(f"BOOP is running at http://127.0.0.1:{args.port}", flush=True)
    try:
        await stop.wait()
    finally:
        await platform.close()
        await manager.close()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="BOOP local strap companion")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--connect", help="connect to an already discovered BOOP strap address")
    parser.add_argument("--no-auto-connect", action="store_true")
    asyncio.run(run(parser.parse_args()))
