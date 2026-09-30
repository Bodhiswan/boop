"""A bounded local WHOOP 4 diagnostic: pair, battery, clock, and live HR."""
import asyncio
import json
import sys
import time
from pathlib import Path

from bleak import BleakClient, BleakScanner

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor/noop"))
import whoop_frame as wf

SERVICE = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WRITE = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"


async def main():
    dev = await BleakScanner.find_device_by_filter(
        lambda d, a: SERVICE in a.service_uuids, timeout=15
    )
    if not dev:
        raise RuntimeError("WHOOP 4 not advertising; wake it and try again")
    print(f"Found {dev.name} {dev.address}", flush=True)
    frames = []
    fragments = []
    reassemblers = {}

    def notify(sender, data):
        char = sender.uuid
        if char.endswith("05-8d6d-82b8-614a-1c8cb0f8dcc6"):
            fragments.append({"hex": bytes(data).hex(), "time": int(time.time()*1000)})
        ra = reassemblers.setdefault(char, wf.Reassembler("whoop4"))
        for frame in ra.feed(bytes(data)):
            if not wf.verify_whoop4_frame(frame):
                print("Invalid CRC frame rejected", flush=True)
                continue
            # This probe never requests hello or logs key/signature bytes.
            if not (frame[4] == 36 and frame[6] == 35):
                frames.append({"hex": frame.hex(), "char": char, "ts_ms": int(time.time() * 1000)})
            if frame[4] == 40 and len(frame) >= 18:
                print(f"LIVE HR: {frame[12]} bpm; RR count {frame[13]}", flush=True)
            elif frame[4] == 36:
                print(f"Reply cmd={frame[6]} result={frame[8]} body_length={len(frame)-13}", flush=True)

    async with BleakClient(dev, pair=True, timeout=25, winrt={"use_cached_services": False}) as client:
        print(f"CONNECTED paired; MTU {client.mtu_size}", flush=True)
        print("SERVICES:", [s.uuid for s in client.services], flush=True)
        for n in (3, 4, 5):
            await client.start_notify(f"6108000{n}-8d6d-82b8-614a-1c8cb0f8dcc6", notify)
        def standard_hr(sender, data):
            print(f"STANDARD HR: {wf.parse_standard_hr(data)} bpm", flush=True)
        await client.start_notify("00002a37-0000-1000-8000-00805f9b34fb", standard_hr)
        seq = 0
        for cmd, payload in ((26, b"\x00"), (35, b"\x00"), (7, b"\x00"), (11, b"\x00"), (63, b"\x01"), (3, b"\x01")):
            await client.write_gatt_char(WRITE, wf.build_command_frame(cmd, seq, payload), response=True)
            seq += 1
            await asyncio.sleep(.3)
        for _ in range(6):
            await asyncio.sleep(3)
            if _ == 0:
                live = [bytes.fromhex(r["hex"]) for r in frames if bytes.fromhex(r["hex"])[4] == 40]
                if live:
                    device_time = int.from_bytes(live[-1][6:10], "little")
                    print(f"DEVICE TIME: {device_time}; drift {int(time.time()) - device_time}s", flush=True)
                    if abs(int(time.time()) - device_time) > 30:
                        now = int(time.time())
                        for body_len in (8, 9):
                            await client.write_gatt_char(WRITE, wf.build_command_frame(10, seq, now.to_bytes(4, "little") + bytes(body_len - 4)), response=True)
                            seq += 1
                            await asyncio.sleep(.3)
                        print("Clock set sent; validating with next live frames", flush=True)
            await client.write_gatt_char(WRITE, wf.build_command_frame(3, seq, b"\x01"), response=False)
            seq += 1
        await client.write_gatt_char(WRITE, wf.build_command_frame(63, seq, b"\x00"), response=True)
        seq += 1
        await client.write_gatt_char(WRITE, wf.build_command_frame(3, seq, b"\x00"), response=True)
    (ROOT / "data/live-probe.json").write_text(json.dumps(frames), encoding="utf-8")
    (ROOT / "data/data-notifications.json").write_text(json.dumps(fragments), encoding="utf-8")
    print(f"SAVED {len(frames)} valid frames locally", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
