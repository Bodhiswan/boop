"""WHOOP 4 wire facts from NOOP; no cloud or WHOOP login is involved."""
from __future__ import annotations

import math
import struct
import time
from dataclasses import dataclass

from vendor.noop import whoop_frame as wire

SERVICE = "61080001-8d6d-82b8-614a-1c8cb0f8dcc6"
WRITE = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
NOTIFY = [f"6108000{n}-8d6d-82b8-614a-1c8cb0f8dcc6" for n in (3, 4, 5)]
STANDARD_HR = "00002a37-0000-1000-8000-00805f9b34fb"
SAFE_COMMANDS = frozenset((3, 7, 10, 11, 14, 20, 22, 23, 26, 34, 35, 63,
                           66, 67, 68, 69, 76, 77, 79, 81, 82, 84, 98, 122))


EXPERIMENTAL_COMMANDS = frozenset((29,32,117,118,121,128))
REBOOT_CANDIDATES = {'reboot29Empty':(29,b''),'powerCycle32Empty':(32,b''),
                     'reboot29Payload1':(29,b'\x01'),'powerCycle32Payload1':(32,b'\x01'),
                     'reboot29Payload0':(29,b'\x00')}

def config_read_body(key):
    if not isinstance(key,str) or not key or len(key)>32 or any(not 33<=ord(c)<=126 for c in key):
        raise ValueError("Config keys must contain 1–32 printable ASCII characters without spaces")
    return b'\x01'+key.encode('ascii').ljust(32,b'\x00')

def command(number: int, sequence: int, payload: bytes = b"\x00", *, experimental=False) -> bytes:
    if number not in SAFE_COMMANDS and not (experimental is True and number in EXPERIMENTAL_COMMANDS):
        raise ValueError("Command is outside this app's connection/collection allowlist")
    if number in (29,32) and (number,payload) not in REBOOT_CANDIDATES.values():
        raise ValueError("Choose a fixed WHOOP4 reboot probe candidate")
    if number in (117,118) and payload!=b'\x01':
        raise ValueError("Feature enumeration requires revision-1 body")
    if number in (121,128):
        if len(payload)!=33 or payload[0]!=1:
            raise ValueError("Config read requires a NUL-padded 32-byte key")
        key=payload[1:].split(b'\x00',1)[0].decode('ascii',errors='replace')
        if config_read_body(key)!=payload:
            raise ValueError("Config read key padding is invalid")
    if number == 23 and (len(payload) != 9 or payload[0] != 1):
        raise ValueError("History acknowledgement must echo the exact eight-byte block")
    if number == 66 and (len(payload) != 9 or payload[0] != 1):
        raise ValueError("WHOOP 4 alarm requires the nine-byte revision-1 body")
    if number in (67, 68, 69) and payload != b"\x01":
        raise ValueError("WHOOP 4 alarm control requires revision 1")
    if number == 79 and (len(payload) != 5 or payload[0] != 2 or not 1 <= payload[1] <= 8 or payload[2:] != bytes(3)):
        raise ValueError("Haptic pattern must use the bounded known notification preset")
    if number == 77 and (not payload.startswith(bytes(2)) or not payload.endswith(bytes(1)) or not 1 <= len(payload[2:-1]) <= 24):
        raise ValueError("Advertising name requires a bounded UTF-8 name")
    if number == 14 and payload not in (b"\x00", b"\x01"):
        raise ValueError("HR broadcast must be on or off")
    return wire.build_command_frame(number, sequence, payload)

def probe_payload(frame,number):
    """CRC/type/opcode gated WHOOP4 reply payload, retaining its two-byte header."""
    if not wire.verify_whoop4_frame(frame): raise ValueError('crc')
    if len(frame)<11 or frame[4]!=36: raise ValueError('envelope')
    if frame[6]!=number: raise ValueError('wrongCommand')
    return frame[7:-4]

def diagnostic_probe(frame,number,previous=None):
    payload=probe_payload(frame,number)
    diff=[]
    if previous is not None:
        try: old=bytes.fromhex(previous)
        except ValueError: old=b''
        diff=[dict(offset=i,before=old[i] if i<len(old) else None,after=payload[i] if i<len(payload) else None) for i in range(max(len(old),len(payload))) if (old[i] if i<len(old) else None)!=(payload[i] if i<len(payload) else None)]
    result=dict(status='answered' if payload else 'stub',command=number,raw_hex=frame.hex(),payload_hex=payload.hex(),payload_bytes=len(payload),diff=diff)
    if number==98 and len(payload)>=9: result['pack_voltage_mv']=int.from_bytes(payload[7:9],'little')
    if number==84 and len(payload)>=4:
        rev,location,confidence,status=payload[:4]
        result['body_location']=dict(revision=rev,location=location,label={0:'UNKNOWN',1:'WRIST',2:'BICEP',3:'CALF',4:'SIDE_TORSO',5:'GLUTE',7:'ANKLE',128:'NOT_CONCLUSIVE',160:'UNKNOWN_GARMENT'}.get(location,f'RAW_{location}'),confidence_raw=confidence,status_raw=status)
    return result

def enumeration_reply(frame,number):
    payload=probe_payload(frame,number)
    if len(payload)<2: raise ValueError('truncated')
    record=payload[2:]
    if number==117:
        if len(record)<3: raise ValueError('truncated')
        return dict(revision=record[0],count=int.from_bytes(record[1:3],'little'),raw_hex=frame.hex())
    if number!=118 or len(record)<2: raise ValueError('truncated')
    name=record[3:].split(b'\x00',1)[0]
    key=name.decode('ascii') if 0<len(name)<=32 and all(32<=c<=126 for c in name) else None
    return dict(revision=record[0],index=record[1],valid_key=len(record)>=3 and record[2]!=0,key=key,raw_hex=frame.hex())

def config_reply(frame,number,key):
    payload=probe_payload(frame,number)
    if len(payload)<2: raise ValueError('truncated')
    record=payload[2:]; field=config_read_body(key)[1:]; value=None; offset=None
    for i in range(max(0,len(record)-32)):
        if record[i:i+32]==field:
            offset=i; value=record[i+32]; break
    return dict(status='answered',command=number,key=key,value=value,echo_offset=offset,raw_hex=frame.hex(),record_hex=record.hex(),raw_response_code=payload[1],result_code=None,result_note='WHOOP4 result-code semantics unconfirmed; echoed value only')


class Assembler:
    """Resynchronise on a valid header; return frames and count integrity faults."""
    def __init__(self):
        self.buffer = bytearray()
        self.errors = 0
        self._transport_tail = 0

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        result = []
        while self.buffer:
            if self._transport_tail:
                # WHOOP 4 raw-data bursts have a 1,936-byte transport envelope:
                # a CRC-valid type-43 frame, then an opaque word (when present)
                # and four zero bytes. The opaque word changes between bursts.
                # Recognise it only after the measured frame type and size.
                needed = self._transport_tail
                if len(self.buffer) < needed:
                    break
                self._transport_tail = 0
                if self.buffer[needed - 4:needed] == bytes(4):
                    del self.buffer[:needed]
                    continue
            sof = self.buffer.find(0xAA)
            if sof < 0:
                self.errors += 1
                self.buffer.clear()
                break
            if sof:
                self.errors += 1
                del self.buffer[:sof]
            if len(self.buffer) < 4:
                break
            length = int.from_bytes(self.buffer[1:3], "little")
            if not 7 <= length <= 4092 or wire.crc8(self.buffer[1:3]) != self.buffer[3]:
                self.errors += 1
                del self.buffer[0]
                continue
            total = length + 4
            if len(self.buffer) < total:
                break
            frame = bytes(self.buffer[:total])
            del self.buffer[:total]
            if wire.verify_whoop4_frame(frame):
                result.append(frame)
                if frame[4] == 43 and total in (1928, 1932):
                    self._transport_tail = 1936 - total
            else:
                self.errors += 1
        return result


@dataclass
class Reading:
    kind: str
    device_seconds: int
    heart_rate: int | None
    rr_ms: list[int]
    contact: int | None = None
    gx: float | None = None
    gy: float | None = None
    gz: float | None = None
    layout: int | None = None


def _hr(value: int) -> int | None:
    return value if 25 <= value <= 250 else None


def _rr(frame: bytes, start: int, count: int, capacity: int) -> list[int]:
    end = len(frame) - 4  # never decode through the CRC trailer
    if count > capacity or start + count * 2 > end:
        return []
    return [v for v in struct.unpack_from(f"<{count}H", frame, start) if 250 <= v <= 2500] if count else []


def decode(frame: bytes) -> Reading | None:
    if not wire.verify_whoop4_frame(frame):
        return None
    limit = len(frame) - 4
    if frame[4] == 40 and limit >= 14:
        seconds = int.from_bytes(frame[6:10], "little")
        return Reading("live", seconds, _hr(frame[12]), _rr(frame, 14, frame[13], (limit - 14) // 2))
    if frame[4] != 47 or limit < 15:
        return None
    version = frame[5]
    seconds = int.from_bytes(frame[11:15], "little")
    if version in (12, 24) and limit >= 84:
        gravity = struct.unpack_from("<fff", frame, 40)
        gravity = tuple(x if math.isfinite(x) and abs(x) <= 16 else None for x in gravity)
        return Reading("history", seconds, _hr(frame[21]), _rr(frame, 23, frame[22], 5),
                       frame[55], *gravity, layout=version)
    if version in (5, 7, 9) and limit >= 23:
        return Reading("history", seconds, _hr(frame[21]), _rr(frame, 23, frame[22], (limit - 23) // 2), layout=version)
    if version == 25 and len(frame) == 84:
        # Only timestamp and observed movement fields are mapped; no fabricated HR.
        gravity = tuple(v / 16384 for v in struct.unpack_from("<hhh", frame, 73))
        return Reading("history", seconds, None, [], None, *gravity, layout=version)
    return None  # unknown layouts remain in raw storage for later decoding


def plausible_time(seconds: int, now: float | None = None) -> bool:
    return 1_577_836_800 <= seconds <= (now if now is not None else time.time()) + 300


def battery(frame: bytes) -> float | None:
    if not wire.verify_whoop4_frame(frame):
        return None
    if frame[4] == 36 and frame[6] == 26 and len(frame) - 4 >= 11 and frame[8] == 1:
        value = int.from_bytes(frame[9:11], "little") / 10
        return value if 0 <= value <= 100 else None
    # Event-3 battery records also arrive as stored history. Their values must
    # never replace the current charge obtained with GET_BATTERY_LEVEL.
    return None


def firmware(frame: bytes) -> str | None:
    if wire.verify_whoop4_frame(frame) and frame[4] == 36 and frame[6] == 7 and frame[8] == 1 and len(frame) - 4 >= 26:
        body = frame[9:-4]
        if body[0] == 1:
            return ".".join(str(x) for x in struct.unpack_from("<4I", body, 1))
    return None


def history_end(frame: bytes) -> bytes | None:
    # The upstream helper counts the trailer in its length test. Require all eight
    # acknowledgement bytes to be inside the inner payload as well.
    if len(frame) - 4 < 25:
        return None
    return wire.history_end_data_whoop4(frame)


def standard_hr(data: bytes) -> tuple[int | None, list[int], bool | None]:
    if len(data) < 2:
        return None, [], None
    flags = data[0]
    offset = 3 if flags & 1 else 2
    if len(data) < offset:
        return None, [], None
    hr = int.from_bytes(data[1:offset], "little")
    contact = bool(flags & 2) if flags & 4 else None
    if flags & 8:
        offset += 2
    rr = []
    if flags & 16:
        while offset + 2 <= len(data):
            interval = int.from_bytes(data[offset:offset + 2], "little") * 1000 / 1024
            if 250 <= interval <= 2500:
                rr.append(round(interval, 3))
            offset += 2
    return (None if contact is False else _hr(hr)), rr, contact


def sensor_values(frame: bytes) -> dict | None:
    """NOOP's mapped WHOOP-4 sensors; unmapped optical fields remain raw.

    Provenance: WhoopProtocol/Resources/whoop_protocol.json, PostHooks.swift,
    Streams.swift. Temperature uses NOOP's provisional scale, not a calibration.
    """
    if not wire.verify_whoop4_frame(frame):
        return None
    limit = len(frame) - 4
    if frame[4] == 47 and frame[5] in (12, 24) and limit >= 84:
        values = {"device_seconds": int.from_bytes(frame[11:15], "little"),
                  "source": "history_v24", "skin_contact": frame[55]}
        for name, offset in (("gx", 40), ("gy", 44), ("gz", 48),
                             ("gravity2_x", 56), ("gravity2_y", 60), ("gravity2_z", 64)):
            value = struct.unpack_from("<f", frame, offset)[0]
            values[name] = value if math.isfinite(value) and abs(value) <= 16 else None
        for name, offset in (("ppg_green", 33), ("ppg_red_ir", 35), ("spo2_red_raw", 68),
                             ("spo2_ir_raw", 70), ("skin_temp_raw", 72), ("ambient_raw", 74),
                             ("led_drive_1", 76), ("led_drive_2", 78), ("resp_rate_raw", 80), ("signal_quality", 82)):
            values[name] = int.from_bytes(frame[offset:offset + 2], "little")
        raw = values["skin_temp_raw"]
        estimated = 33.0 + (raw - 826) * .05
        values["skin_temp_c"] = round(estimated, 3) if frame[55] and 20 <= estimated <= 45 else None
        values["temperature_method"] = "NOOP provisional ADC estimate: 826=33C, slope0.05; uncalibrated"
        values["spo2_percent"] = None
        return values
    if frame[4] == 47 and frame[5] == 25 and len(frame) == 84:
        reading = decode(frame)
        gravity = (reading.gx, reading.gy, reading.gz)
        if .5 <= math.sqrt(sum(v * v for v in gravity)) <= 1.5:
            return {"device_seconds": reading.device_seconds, "source": "history_v25",
                    "gx": gravity[0], "gy": gravity[1], "gz": gravity[2]}
    if frame[4] == 43 and len(frame) == 1928:
        # 100 samples/axis, signed i16, 1/4096 g and 2000/32768 degrees/s.
        axes = [tuple(v / 4096 for v in struct.unpack_from("<100h", frame, off)) for off in (89, 289, 489)]
        means = [sum(axis) / 100 for axis in axes]
        magnitudes = [math.sqrt(sum(axis[i] ** 2 for axis in axes)) for i in range(100)]
        avg_magnitude = sum(magnitudes) / 100
        motion_rms = math.sqrt(sum(sum((axis[i] - means[j]) ** 2 for j, axis in enumerate(axes)) for i in range(100)) / 100)
        return {"device_seconds": int.from_bytes(frame[11:15], "little"), "source": "live_imu",
                "gx": means[0], "gy": means[1], "gz": means[2], "motion_rms": motion_rms,
                "motion_variance": sum((v - avg_magnitude) ** 2 for v in magnitudes) / 100,
                "sampling_hz": 100, "heart_rate": _hr(frame[21]), "rr_ms": _rr(frame, 23, frame[22], 4)}
    return None


def event(frame: bytes) -> dict | None:
    if wire.verify_whoop4_frame(frame) and frame[4] == 48 and len(frame) - 4 >= 12:
        return {"number": frame[6], "device_seconds": int.from_bytes(frame[8:12], "little")}
    return None


def waveform(frame: bytes) -> dict | None:
    if not wire.verify_whoop4_frame(frame) or frame[4] != 43:
        return None
    if len(frame) == 1928:
        return {"kind": "imu", "sampling_hz": 100,
                "axes": {name: [round(v * scale, 6) for v in struct.unpack_from("<100h", frame, off)]
                         for name, off, scale in (("accel_x", 89, 1/4096), ("accel_y", 289, 1/4096),
                                                  ("accel_z", 489, 1/4096), ("gyro_x", 692, 2000/32768),
                                                  ("gyro_y", 892, 2000/32768), ("gyro_z", 1092, 2000/32768))}}
    if len(frame) == 1932:
        samples = []
        for i in range(419):
            value = int.from_bytes(frame[42 + i * 4:45 + i * 4], "little")
            samples.append(value - (1 << 24) if value & (1 << 23) else value)
        return {"kind": "ppg", "sampling_hz": 437, "samples": samples, "units": "AC raw ADC"}
    return None
