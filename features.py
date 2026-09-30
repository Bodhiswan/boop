"""Offline records, bounded importers and additive backup restore for BOOP.

Feature records never rewrite strap frames/readings. Imports retain source fields so
unsupported measurements are not silently promoted into physiological estimates.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing, contextmanager
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import struct
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile

from storage import Store

MAX_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000
MAX_BACKUP_BYTES = 1024 * 1024 * 1024
MAX_BACKUP_ROWS = 10_000_000
KINDS = frozenset("journal habit question mood cycle nutrition hydration body_metric lab workout lifting_program lifting_set route sleep alarm reminder daily_metric health_sample".split())
NOOP_TABLE_KINDS = dict(hrSample="health_sample",rrInterval="health_sample",event="health_sample",battery="health_sample",spo2Sample="health_sample",skinTempSample="health_sample",stepSample="health_sample",sleepStateSample="sleep",respSample="health_sample",gravitySample="health_sample",journal="journal",workout="workout",sleepSession="sleep",dailyMetric="daily_metric",metricSeries="health_sample",labMarker="lab",appleDaily="daily_metric",appleStepHour="health_sample",liveSession="workout",ppgHrSample="health_sample",v18AuxSample="health_sample")
DEFAULT_SETTINGS = dict(name="", sex="unspecified", birth_date="", age=0, waist_cm=0.0, height_cm=0.0,
                        weight_kg=0.0, hr_max=190, hr_rest=60, units="metric",
                        timezone="Australia/Brisbane", sleep_goal_hours=8.0,
                        theme="light", distance_units="metric", temperature_units="", hr_zone_thresholds="", effort_scale="hundred", day_cycle_mode="sleep_onset", hosted_cards="", custom_behaviors="", external_push_enabled=False,
                        os_automation_enabled=False, scripts_enabled=False,
                        auto_sync_minutes=15, step_calibration=0.0,
                        quiet_hours_start="22:00", quiet_hours_end="07:00",
                        notifications_enabled=False, battery_threshold=20,
                        hr_zone_haptics=False, hr_target_min=0, hr_target_max=0,
                        inactivity_minutes=0, start_with_windows=False,
                        hrv_window="whole", effort_method="edwards", hr_only_sleep_enabled=False,
                        experimental_sleep_v2_enabled=True,motion_aware_wake_enabled=False,
                        charge_baseline_since_ms=0.0, hrv_capture_mode="continuous",
                        pre_sleep_feedback_enabled=False, keep_laptop_awake=True,
                        habitual_wake_hour=-1.0, habitual_sleep_hour=-1.0, circadian_shift_hours=0.0)
ACTION_KEYS = frozenset(k for k in DEFAULT_SETTINGS if k.endswith("_enabled")) | frozenset(("hr_zone_haptics", "start_with_windows"))
BACKUP_TABLES=frozenset(('frames','readings','chunks','sensors','sensor_migrations','feature_migrations','feature_settings','feature_records','feature_revisions','feature_imports','boop_devices','boop_coach_messages','boop_control_settings','workout_dismissals','workout_edits','lift_control'))
BACKUP_ACTION_KEYS=ACTION_KEYS|frozenset(('auto_sync_minutes','keep_laptop_awake'))
BACKUP_EXTRA_SCHEMA=(
    'CREATE TABLE IF NOT EXISTS boop_devices(address TEXT PRIMARY KEY,name TEXT NOT NULL,forgotten INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE IF NOT EXISTS boop_coach_messages(id TEXT PRIMARY KEY,role TEXT NOT NULL,text TEXT NOT NULL,provider TEXT NOT NULL,created_ms INTEGER NOT NULL,deleted_ms INTEGER,context_eligible INTEGER NOT NULL DEFAULT 1)',
    'CREATE TABLE IF NOT EXISTS boop_control_settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS workout_dismissals(device TEXT NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(device,start_ms,end_ms))',
    'CREATE TABLE IF NOT EXISTS workout_edits(id TEXT PRIMARY KEY,device TEXT NOT NULL,day TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,created_ms INTEGER NOT NULL,undone_ms INTEGER)',
    'CREATE TABLE IF NOT EXISTS lift_control(device TEXT PRIMARY KEY,state_json TEXT,updated_ms INTEGER NOT NULL)',
)


def _paused_lift(value):
    value=_safe_payload(value,1_000_000)
    if not isinstance(value.get('snapshot'),dict):raise ValueError('Backup lift sheet lacks its saved snapshot')
    snapshot=value['snapshot']
    if not {'plan','start_ts','stage','slot','sets','stage_started_at'}<=set(snapshot) or not isinstance(snapshot['plan'],list) or not 1<=len(snapshot['plan'])<=200 or not isinstance(snapshot['sets'],list) or snapshot['stage'] not in ('warmup','working','resting','finished'):
        raise ValueError('Backup contains an invalid lifting snapshot')
    value.update(paused=True,restarted=True,instance='backup-restored',history=[],pending_history=[])
    value['snapshot']['can_undo']=False
    return value


def _edit_states(encoded):
    """Validate actual feature-row snapshots; never import executable control data."""
    value=json.loads(encoded)
    if not isinstance(value,dict):raise ValueError('Backup workout edit states must be an object')
    for key,row in value.items():
        if row is None:continue
        if not isinstance(row,dict) or row.get('id')!=key or row.get('kind') not in ('workout','lifting_set','lifting_program'):
            raise ValueError('Backup workout edit has an invalid record binding')
        row['payload_json']=_json(_safe_payload(json.loads(row['payload_json'])))
    return value
SCHEMA = """
CREATE TABLE IF NOT EXISTS feature_migrations(version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS feature_settings(key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feature_records(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, deleted_ms INTEGER,
 import_key TEXT UNIQUE);
CREATE INDEX IF NOT EXISTS feature_kind_time ON feature_records(kind,updated_ms);
CREATE TABLE IF NOT EXISTS feature_revisions(
 revision INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
 kind TEXT NOT NULL, payload_json TEXT NOT NULL, deleted_ms INTEGER,
 saved_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS feature_imports(
 digest TEXT PRIMARY KEY, filename TEXT NOT NULL, imported_ms INTEGER NOT NULL,
 count INTEGER NOT NULL);
"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now():
    return time.time_ns() // 1_000_000


def _safe_payload(value,max_bytes=1_000_000):
    if not isinstance(value, dict):
        raise ValueError("A record must be a JSON object")
    def check(obj):
        if isinstance(obj, dict):
            for key, child in obj.items():
                if re.search(r"password|secret|token|credential|api.?key|authorization", str(key), re.I):
                    raise ValueError("Credentials do not belong in health records")
                check(child)
        elif isinstance(obj, list):
            for child in obj:
                check(child)
    check(value)
    result = json.loads(_json(value))
    if len(_json(result)) > max_bytes:
        raise ValueError("Record exceeds its JSON size limit")
    return result


def _stamp(value, zone="UTC+00:00"):
    if value in (None, ""):
        return None
    if isinstance(value, (float, int)):
        return int(value if value > 100_000_000_000 else value * 1000)
    text = str(value).strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%d %b %Y, %H:%M", "%Y-%m-%d %H:%M:%S %z"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                pass
        else:
            return None
    if dt.tzinfo is None:
        offset = re.fullmatch(r"UTC([+-]\d{2}:\d{2})", zone or "")
        if offset:
            dt = datetime.fromisoformat(dt.isoformat() + offset[1])
        else:
            dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _zip_entries(data, max_bytes=MAX_BYTES):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 256 or sum(i.file_size for i in infos) > max_bytes:
            raise ValueError("Archive exceeds the expanded byte / 256 entry import limit")
        seen = set()
        for item in infos:
            name = item.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or ":" in name or name in seen:
                raise ValueError("Unsafe or duplicate archive path")
            seen.add(name)
            if not item.is_dir():
                yield name, archive.read(item)


@contextmanager
def _backup_file(data):
    if not isinstance(data,bytes) or len(data) > MAX_BACKUP_BYTES:
        raise ValueError("Backup exceeds the 1 GB limit")
    settings = {}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder)/"source.sqlite"
        if data.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                if len(infos)>256 or sum(i.file_size for i in infos)>MAX_BACKUP_BYTES:
                    raise ValueError("Backup exceeds 1 GB expanded / 256 entries")
                names = set()
                for item in infos:
                    name = item.filename.replace("\\","/")
                    p = PurePosixPath(name)
                    if p.is_absolute() or ".." in p.parts or ":" in name or name in names:
                        raise ValueError("Unsafe or duplicate archive path")
                    names.add(name)
                candidates = [i for i in infos if i.filename.lower().endswith((".sqlite",".noopdb"))]
                if len(candidates)!=1:
                    raise ValueError("Backup must contain exactly one SQLite database")
                # Stream the potentially large SQLite entry in bounded blocks. No extraction
                # path ever comes from the archive, and full reads verify the ZIP CRC.
                with archive.open(candidates[0]) as source, path.open("wb") as target:
                    magic = source.read(16)
                    if magic!=b"SQLite format 3\x00":
                        raise ValueError("Not a SQLite backup")
                    target.write(magic)
                    shutil.copyfileobj(source,target,1024*1024)
                if "settings.json" in names:
                    info = archive.getinfo("settings.json")
                    if info.file_size>1024*1024:
                        raise ValueError("Backup settings exceed 1 MB")
                    settings = json.loads(archive.read(info))
                    if not isinstance(settings,dict):
                        raise ValueError("Backup settings must be an object")
        else:
            if not data.startswith(b"SQLite format 3\x00"):
                raise ValueError("Not a SQLite backup")
            path.write_bytes(data)
        yield path,settings


def _is_backup(filename,data):
    if data.startswith(b"SQLite format 3\x00") and Path(filename).suffix.lower() != ".db":
        return True
    if filename.lower().endswith((".noopbak", ".boopbak")):
        return True
    if data.startswith(b"PK"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return any(n.lower().endswith((".sqlite", ".noopdb")) for n in archive.namelist())
    return False


def _xml(data):
    if re.search(br"<!ENTITY|<!DOCTYPE[^>]*(?:SYSTEM|PUBLIC)",data,re.I):
        raise ValueError("XML entities and external resources are forbidden")
    # Apple Health's actual export has an internal element/attribute DTD. It is
    # unnecessary for parsing; remove the whole declaration after excluding entities.
    return ET.fromstring(re.sub(br"<!DOCTYPE[^>\[]*(?:\[[\s\S]*?\]\s*)?>",b"",data,flags=re.I))


def _rows(value):
    if isinstance(value,list):
        return [v for v in value if isinstance(v,dict)]
    if isinstance(value,dict):
        if isinstance(value.get("data"),list):
            return _rows(value["data"])
        for child in value.values():
            if isinstance(child,list):
                return _rows(child)
        return [value]
    return []


def _number(value):
    if isinstance(value,bool) or value in (None,""):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError,TypeError):
        return None


def _xlsx_csv(data):
    files = dict(_zip_entries(data))
    ns = {"m":"http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    strings = []
    if "xl/sharedStrings.xml" in files:
        strings = ["".join(n.itertext()) for n in _xml(files["xl/sharedStrings.xml"]).findall("m:si",ns)]
    workbook = _xml(files["xl/workbook.xml"])
    relations = _xml(files["xl/_rels/workbook.xml.rels"])
    targets = {n.get("Id"):n.get("Target") for n in relations}
    for sheet in workbook.findall("m:sheets/m:sheet",ns):
        target = targets.get(sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"),"")
        name = target.lstrip("/") if target.startswith("/") else "xl/"+target
        if ".." in PurePosixPath(name).parts or name not in files:
            raise ValueError("Invalid workbook relationship")
        rows = []
        for row in _xml(files[name]).findall("m:sheetData/m:row",ns):
            cells = {}
            for cell in row.findall("m:c",ns):
                col = re.match(r"([A-Z]+)",cell.get("r",""))
                if not col:
                    continue
                index = 0
                for char in col[1]:
                    index = index*26+ord(char)-64
                if index > 256:
                    raise ValueError("Workbook exceeds 256 columns")
                value = cell.find("m:v",ns)
                text = value.text if value is not None else ""
                if cell.get("t") == "s":
                    text = strings[int(text)]
                elif cell.get("t") == "inlineStr":
                    text = "".join(cell.find("m:is",ns).itertext())
                cells[index-1] = text or ""
            if cells:
                rows.append([cells.get(i,"") for i in range(max(cells)+1)])
            if len(rows) > MAX_ROWS:
                raise ValueError("Workbook exceeds 100,000 rows")
        # Follow NOOP's first exercise-bearing sheet rule in workbook tab order.
        if rows and any(re.sub(r"[^a-z]","",v.lower()) in ("exercise","exercisename") for v in rows[0]):
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            width = len(rows[0])
            for row in rows:
                writer.writerow((row+[""]*width)[:width])
            return out.getvalue().encode()
    raise ValueError("Workbook contains no exercise/program sheet")


def _fit_records(data):
    """Bounded clean FIT decoder, following the public record/definition format.

    Developer fields are preserved by skipping their declared byte lengths. Unknown
    messages do not become measurements. Compressed timestamps roll forward.
    """
    if len(data)<12 or data[8:12]!=b".FIT" or data[0] not in (12,14):
        raise ValueError("Invalid FIT header")
    end = data[0]+int.from_bytes(data[4:8],"little")
    if end > len(data) or end <= data[0]:
        raise ValueError("Truncated FIT data")
    pos,definitions,last_stamp,count = data[0],{},None,0
    results = []
    def take(size):
        nonlocal pos
        if size<0 or pos+size>end:
            raise ValueError("Truncated FIT record")
        result = data[pos:pos+size]; pos += size
        return result
    while pos<end:
        count += 1
        if count>MAX_ROWS:
            raise ValueError("FIT exceeds 100,000 messages")
        header = take(1)[0]
        compressed = bool(header&128)
        local = (header>>5)&3 if compressed else header&15
        if not compressed and header&64:
            reserved,architecture = take(2)
            if architecture not in (0,1):
                raise ValueError("Invalid FIT architecture")
            order = "big" if architecture else "little"
            global_id = int.from_bytes(take(2),order)
            fields = [tuple(take(3)) for _ in range(take(1)[0])]
            developer = [tuple(take(3)) for _ in range(take(1)[0])] if header&32 else []
            definitions[local] = (global_id,order,fields,developer)
            continue
        if local not in definitions:
            raise ValueError("FIT data precedes its definition")
        global_id,order,fields,developer = definitions[local]
        values = {}
        if compressed:
            if last_stamp is None:
                raise ValueError("Compressed FIT timestamp has no base")
            stamp = (last_stamp&~31)|(header&31)
            if stamp<last_stamp:
                stamp += 32
            values[253] = stamp
        for field,size,base in fields:
            if compressed and field==253:
                continue
            raw = take(size)
            typ = base&31
            if not raw:
                continue
            if typ==7:
                value = raw.split(b"\x00",1)[0].decode("utf-8",errors="replace")
            elif typ in (8,9) and size in (4,8):
                value = struct.unpack((">" if order=="big" else "<")+("f" if size==4 else "d"),raw)[0]
            else:
                signed = typ in (1,3,5,14)
                value = int.from_bytes(raw,order,signed=signed)
                invalid = (1<<(size*8-1))-1 if signed else (1<<(size*8))-1
                if value==invalid or typ in (10,11,12,16) and value==0:
                    continue
            values[field] = value
        for field,size,index in developer:
            take(size)
        if 253 in values:
            last_stamp = values[253]
        if global_id in (18,19,20):
            results.append((global_id,values))
    return results


def record_stream_sample(payload):
    """Pure adapter for AnalyticsService's read-only feature_records query.

    Returns actual sample fields only. Vendor readiness scores and HRV summary
    measures are never relabelled as beats or measured cardiovascular recovery.
    """
    p = payload
    original = p.get("original",{})
    source = p.get("source","imported")
    table = p.get("source_table","")
    stamp = p.get("timestamp_ms",p.get("start_ms"))
    if stamp is None:
        stamp = _stamp(original.get("ts",original.get("startDate")))
    if stamp is None:
        return None
    out = dict(device=p.get("device",original.get("deviceId",source)),kind="imported",timestamp_ms=stamp,received_ms=stamp,hr=None,rr_json="[]",contact=None,gx=None,gy=None,gz=None,source=source)
    if _number(p.get("hr")) is not None:
        out["hr"] = _number(p["hr"])
    if isinstance(p.get("rr_ms"),list):
        out["rr_json"] = _json(p["rr_ms"])
    if table=="hrSample":
        out["hr"] = _number(original.get("bpm"))
    elif table=="rrInterval" and original.get("tsSuspect") != 1:
        rr = _number(original.get("rrMs"))
        if rr is not None and 0<rr<5000:
            out["rr_json"] = _json([rr])
    elif table=="gravitySample":
        out.update(gx=_number(original.get("x")),gy=_number(original.get("y")),gz=_number(original.get("z")))
    elif table in ("skinTempSample","respSample","spo2Sample","stepSample"):
        out.update(sensor_kind=table,sensor_values=original)
    apple_type = p.get("type",original.get("type",""))
    if apple_type=="HKQuantityTypeIdentifierHeartRate":
        number = _number(p.get("value",original.get("value")))
        if number is not None:
            out["hr"] = number
    elif apple_type in ("HKQuantityTypeIdentifierOxygenSaturation","HKQuantityTypeIdentifierRespiratoryRate","HKQuantityTypeIdentifierBodyTemperature","HKQuantityTypeIdentifierHeartRateVariabilitySDNN","HKQuantityTypeIdentifierStepCount"):
        out.update(sensor_kind=apple_type,sensor_values=dict(value=p.get("value",original.get("value")),unit=p.get("unit",original.get("unit"))))
    for key in ("spo2","respiratory_rate","skin_temp_c","steps"):
        if _number(p.get(key)) is not None:
            out[key] = _number(p[key])
    if out["hr"] is None and out["rr_json"]=="[]" and out["gx"] is None and "sensor_kind" not in out and not any(k in out for k in ("spo2","respiratory_rate","skin_temp_c","steps")):
        return None
    return out


class FeatureStore:
    def __init__(self, store: Store | Path):
        self.store = store if isinstance(store, Store) else Store(Path(store))
        self.path = self.store.path
        with closing(self.store.connect()) as conn:
            with conn:
                conn.executescript(SCHEMA)
                conn.execute("INSERT OR IGNORE INTO feature_migrations VALUES(1,?)", (_now(),))

    def settings(self):
        values = dict(DEFAULT_SETTINGS)
        explicit = set()
        with closing(self.store.connect()) as conn:
            for row in conn.execute("SELECT key,value_json FROM feature_settings"):
                if row[0] in values:
                    values[row[0]] = json.loads(row[1])
                    explicit.add(row[0])
        values["profile_provenance"] = {key:("user" if key in explicit else "default") for key in ("hr_max","hr_rest","weight_kg","height_cm","sex","age","step_calibration")}
        values["profile_source"] = "User profile" if {"hr_max","hr_rest"} <= explicit else "NOOP defaults; edit profile"
        return values

    def stream_samples(self, device=None, start_ms=None, end_ms=None):
        samples = []
        with closing(self.store.connect()) as conn:
            for row in conn.execute("SELECT payload_json FROM feature_records WHERE kind='health_sample' AND deleted_ms IS NULL"):
                sample = record_stream_sample(json.loads(row[0]))
                if sample and (device is None or sample["device"]==device) and (start_ms is None or sample["timestamp_ms"]>=start_ms) and (end_ms is None or sample["timestamp_ms"]<=end_ms):
                    samples.append(sample)
        return sorted(samples,key=lambda p:p["timestamp_ms"])

    def _validate_settings(self, values):
        if not isinstance(values, dict):
            raise ValueError("Settings must be an object")
        for key, value in values.items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"Unknown setting: {key}")
            expected = type(DEFAULT_SETTINGS[key])
            if expected is float:
                if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0 and key not in ("habitual_wake_hour","habitual_sleep_hour","circadian_shift_hours"):
                    raise ValueError(f"{key} must be a nonnegative number")
            elif type(value) is not expected:
                raise ValueError(f"Incorrect type for {key}")
            if isinstance(value, str) and len(value) > 200:
                raise ValueError("Setting text is too long")
        current = self.settings() | values
        if any(not -1 <= current[k] <= 24 for k in ("habitual_wake_hour","habitual_sleep_hour")) or not -12 <= current["circadian_shift_hours"] <= 12:
            raise ValueError("Habitual hours use 0–24 or -1 for observed history; a schedule shift uses -12–12 hours")
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            local_zone = ZoneInfo(current["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Choose a valid IANA timezone, such as Australia/Brisbane")
        if current["birth_date"]:
            try:
                birthday = datetime.strptime(current["birth_date"], "%Y-%m-%d").date()
            except ValueError:
                raise ValueError("Date of birth must use YYYY-MM-DD")
            today = datetime.now(local_zone).date()
            if birthday > today or (today - birthday).days > 125 * 366:
                raise ValueError("Date of birth is out of range")
        if current["sex"] not in ("unspecified", "male", "female"):
            raise ValueError("Choose a supported profile sex")
        if current["hrv_window"] not in ("whole", "deep") or current["effort_method"] not in ("edwards", "banister") or current["effort_scale"] not in ("hundred", "whoop", ""):
            raise ValueError("Invalid HRV/effort preference")
        if current["day_cycle_mode"] not in ("sleep_onset", "midnight", "") or current["hrv_capture_mode"] not in ("continuous", "overnight", "paused"):
            raise ValueError("Invalid recording/day preference")
        if current["hr_zone_thresholds"]:
            try:
                thresholds = [float(v.strip()) for v in current["hr_zone_thresholds"].split(",")]
            except ValueError:
                raise ValueError("Enter five comma-separated HR zone starting points")
            if len(thresholds) != 5 or any(not math.isfinite(v) or not 30 <= v <= 250 for v in thresholds) or thresholds != sorted(set(thresholds)):
                raise ValueError("HR zones need five increasing BPM starts between 30 and 250")
        if current["units"] not in ("metric", "imperial") or current["theme"] not in ("dark", "light", "system", "auto"):
            raise ValueError("Invalid units or theme")
        if not 30 <= current["hr_max"] <= 250 or not 20 <= current["hr_rest"] <= 150 or not 0 < current["sleep_goal_hours"] <= 24:
            raise ValueError("Profile value is out of range")
        if not 0 <= current["auto_sync_minutes"] <= 60 or not 0 <= current["step_calibration"] <= 10000 or not 0 <= current["battery_threshold"] <= 100 or not 0 <= current["inactivity_minutes"] <= 1440:
            raise ValueError("Reminder/calibration value is out of range")
        if not 0 <= current["hr_target_min"] <= 250 or not 0 <= current["hr_target_max"] <= 250 or current["hr_target_min"] and current["hr_target_max"] and current["hr_target_min"] > current["hr_target_max"]:
            raise ValueError("HR target range is invalid")
        for key in ("quiet_hours_start", "quiet_hours_end"):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",current[key]):
                raise ValueError("Quiet hours must use HH:MM")
        if not 0 <= current["age"] <= 125 or not 0 <= current["height_cm"] <= 300 or not 0 <= current["weight_kg"] <= 600 or not 0 <= current["waist_cm"] <= 300:
            raise ValueError("Body profile value is out of range")

    def update_settings(self, values):
        self._validate_settings(values)
        with closing(self.store.connect()) as conn:
            with conn:
                conn.executemany("INSERT INTO feature_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", [(k, _json(v)) for k, v in values.items()])
        return self.settings()

    @staticmethod
    def _kind(kind):
        if kind not in KINDS:
            raise ValueError("Unknown record kind")

    @staticmethod
    def _record(row):
        return json.loads(row["payload_json"]) | dict(id=row["id"], kind=row["kind"], created_ms=row["created_ms"], updated_ms=row["updated_ms"], deleted_ms=row["deleted_ms"])

    def list_records(self, kind, filters=None):
        self._kind(kind)
        filters = filters or {}
        limit = min(max(int(filters.get("limit", 1000)), 1), MAX_ROWS)
        with closing(self.store.connect()) as conn:
            rows = conn.execute("SELECT * FROM feature_records WHERE kind=? AND (? OR deleted_ms IS NULL) ORDER BY updated_ms DESC LIMIT ?", (kind, bool(filters.get("include_deleted", False)), limit))
            result = [self._record(row) for row in rows]
        return [row for row in result if all(str(row.get(k,row.get("day", "") if k=="date" else "")) == str(v) for k, v in filters.items() if k not in ("limit", "include_deleted"))]

    def _save(self, conn, kind, payload, import_key=None):
        self._kind(kind)
        payload = _safe_payload(payload,16*1024*1024 if kind=="route" else 1_000_000)
        record_id = str(payload.pop("id", None) or uuid.uuid4())
        if len(record_id) > 200:
            raise ValueError("Record id is too long")
        for key in ("kind", "created_ms", "updated_ms", "deleted_ms"):
            payload.pop(key, None)
        for key in ("start_ms", "end_ms", "timestamp_ms"):
            if key in payload and payload[key] is not None and (isinstance(payload[key], bool) or not isinstance(payload[key], (int, float)) or payload[key] < 0):
                raise ValueError(f"{key} must be a positive timestamp in milliseconds")
        if payload.get("start_ms") is not None and payload.get("end_ms") is not None and payload["end_ms"] < payload["start_ms"]:
            raise ValueError("End must follow start")
        # Configurations are persisted only; never arm the strap or OS by importing them.
        if import_key and kind in ("alarm", "reminder"):
            payload["enabled"] = False
        previous = conn.execute("SELECT * FROM feature_records WHERE id=?", (record_id,)).fetchone()
        now = _now()
        if previous:
            if previous["kind"] != kind:
                raise ValueError("An id cannot change record kind")
            conn.execute("INSERT INTO feature_revisions(record_id,kind,payload_json,deleted_ms,saved_ms) VALUES(?,?,?,?,?)", (record_id,kind,previous["payload_json"],previous["deleted_ms"],now))
            conn.execute("UPDATE feature_records SET payload_json=?,updated_ms=?,deleted_ms=NULL WHERE id=?", (_json(payload),now,record_id))
        else:
            conn.execute("INSERT OR IGNORE INTO feature_records VALUES(?,?,?,?,?,?,?)", (record_id,kind,_json(payload),now,now,None,import_key))
        row = conn.execute("SELECT * FROM feature_records WHERE id=? OR import_key=?", (record_id,import_key)).fetchone()
        return self._record(row)

    def save_record(self, kind, payload):
        with closing(self.store.connect()) as conn:
            with conn:
                return self._save(conn, kind, payload)

    def delete_record(self, kind, record_id):
        self._kind(kind)
        with closing(self.store.connect()) as conn:
            with conn:
                row = conn.execute("SELECT * FROM feature_records WHERE id=? AND kind=?", (record_id,kind)).fetchone()
                if not row:
                    raise ValueError("Record not found")
                now = _now()
                conn.execute("INSERT INTO feature_revisions(record_id,kind,payload_json,deleted_ms,saved_ms) VALUES(?,?,?,?,?)", (record_id,kind,row["payload_json"],row["deleted_ms"],now))
                conn.execute("UPDATE feature_records SET deleted_ms=?,updated_ms=? WHERE id=?", (now,now,record_id))
        return {"id":record_id,"deleted":True}

    def undo_record(self, kind, record_id):
        self._kind(kind)
        with closing(self.store.connect()) as conn:
            with conn:
                row = conn.execute("SELECT * FROM feature_revisions WHERE record_id=? AND kind=? ORDER BY revision DESC LIMIT 1", (record_id,kind)).fetchone()
                if not row:
                    raise ValueError("No saved revision to undo")
                conn.execute("UPDATE feature_records SET payload_json=?,deleted_ms=?,updated_ms=? WHERE id=? AND kind=?", (row["payload_json"],row["deleted_ms"],_now(),record_id,kind))
                conn.execute("DELETE FROM feature_revisions WHERE revision=?", (row["revision"],))
                return self._record(conn.execute("SELECT * FROM feature_records WHERE id=?", (record_id,)).fetchone())

    def _snapshot(self, reason):
        folder = self.path.parent / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"before-{reason}-{_now()}-{uuid.uuid4().hex[:8]}.sqlite"
        self.store.backup(path)
        return str(path)

    def _parse(self, filename, data, depth=0):
        if depth>3:
            raise ValueError("Archive nesting exceeds three levels")
        if isinstance(data,bytes) and _is_backup(filename,data):
            return "backup", [], [], ["Use backup restore to merge database contents"]
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise ValueError("Import exceeds the 64 MB limit")
        records, frames, warnings = [], [], []
        if filename.lower().endswith(".xlsx"):
            fmt,records,frames,warnings = self._parse("program.csv",_xlsx_csv(data))
            return "lifting-program-xlsx",records,frames,warnings
        if data.startswith(b"SQLite format 3\x00") and filename.lower().endswith(".db"):
            return self._parse_mi_database(data)
        if filename.lower().endswith(".fit") or len(data)>12 and data[8:12]==b".FIT":
            return self._parse_fit(filename,data)
        if data.startswith(b"PK"):
            formats = []
            for name, body in _zip_entries(data):
                if name.lower().endswith((".csv", ".xml", ".gpx", ".tcx", ".json", ".fit", ".db", ".xlsx", ".zip")) and PurePosixPath(name).name not in ("manifest.json", "settings.json"):
                    try:
                        fmt, rows, raw, notes = self._parse(name, body,depth+1)
                    except ValueError as exc:
                        if str(exc).startswith("Unsupported JSON"):
                            warnings.append(f"Skipped unsupported export metadata: {name}")
                            continue
                        raise
                    formats.append(fmt); records.extend(rows); frames.extend(raw); warnings.extend(notes)
                elif name.lower().endswith((".sqlite", ".noopdb")):
                    return "backup", [], [], ["Use backup restore to merge database contents"]
                if len(records) + len(frames) > MAX_ROWS:
                    raise ValueError("Import exceeds 100,000 rows")
            if not formats:
                raise ValueError("No supported data files in archive")
            if records and frames:
                raise ValueError("Import raw captures separately from CSV/XML records")
            return "zip:" + ",".join(sorted(set(formats))), records, frames, warnings
        suffix = Path(filename).suffix.lower()
        if suffix in (".xml", ".gpx", ".tcx"):
            root = _xml(data)
            local = lambda tag: tag.rsplit("}", 1)[-1]
            if local(root.tag) == "HealthData":
                for node in root:
                    tag = local(node.tag)
                    if tag in ("Record", "Workout"):
                        p = dict(node.attrib)
                        p.update(source="apple-health", start_ms=_stamp(p.get("startDate")), end_ms=_stamp(p.get("endDate")))
                        kind = "workout" if tag == "Workout" else "sleep" if "SleepAnalysis" in p.get("type", "") else "body_metric" if any(v in p.get("type", "") for v in ("BodyMass", "Height", "BodyFat")) else "nutrition" if "Dietary" in p.get("type", "") else "health_sample"
                        records.append((kind,p))
                        if len(records) > MAX_ROWS:
                            raise ValueError("Import exceeds 100,000 rows")
                return "apple-health", records, [], warnings
            if local(root.tag).lower() not in ("gpx", "trainingcenterdatabase"):
                raise ValueError("Unrecognised XML health/activity format")
            points = []
            for node in root.iter():
                if local(node.tag) in ("trkpt", "rtept", "Trackpoint"):
                    point = dict(node.attrib)
                    for child in node.iter():
                        if child.text and child.text.strip():
                            point[local(child.tag)] = child.text.strip()
                    points.append(point)
                    if len(points) > MAX_ROWS:
                        raise ValueError("Route exceeds 100,000 points")
            source = suffix[1:]
            route_records = [("route",dict(source=source,name=Path(filename).stem,points=points))]
            stamps = []
            for point in points:
                stamp = _stamp(point.get("time",point.get("Time")))
                if stamp is not None:
                    stamps.append(stamp)
                    hr = _number(point.get("hr",point.get("Value")))
                    if hr is not None and 0<hr<255:
                        route_records.append(("health_sample",dict(source=source,timestamp_ms=stamp,hr=hr)))
            if stamps:
                sport = next((n.get("Sport") for n in root.iter() if n.get("Sport")),"Activity")
                workout = dict(source=source,name=sport,start_ms=min(stamps),end_ms=max(stamps))
                distances = [_number(p.get("DistanceMeters")) for p in points]
                distances = [d for d in distances if d is not None]
                if distances:
                    workout["distance_m"] = max(distances)
                calories = [_number(n.text) for n in root.iter() if local(n.tag)=="Calories"]
                calories = [c for c in calories if c is not None]
                if calories:
                    workout["calories"] = sum(calories)
                route_records.append(("workout",workout))
            if len(route_records)>MAX_ROWS:
                raise ValueError("Activity import exceeds 100,000 records")
            return "activity-route",route_records,[],warnings
        text = data.decode("utf-8-sig")
        if suffix == ".json":
            value = json.loads(text)
            wearable = self._parse_wearable_json(filename,value)
            if wearable is not None:
                return wearable
            hevy = value.get("workouts") if isinstance(value,dict) else value if isinstance(value,list) and value and "exercises" in value[0] else None
            if isinstance(hevy,list):
                for session in hevy:
                    start = _stamp(session.get("start_time"))
                    sets,reps,volume = 0,0,0.0
                    for exercise in session.get("exercises",[]):
                        for index,row in enumerate(exercise.get("sets",[])):
                            p = dict(source="hevy-json",start_ms=start,exercise=exercise.get("title"),set_index=index,set_type=row.get("type","normal"),reps=_number(row.get("reps")),weight_kg=_number(row.get("weight_kg")),details=row)
                            records.append(("lifting_set",p))
                            if p["set_type"]!="warmup":
                                sets += 1; reps += p["reps"] or 0; volume += (p["weight_kg"] or 0)*(p["reps"] or 0)
                    records.append(("workout",dict(source="hevy-json",name="Strength Training",title=session.get("title"),start_ms=start,end_ms=_stamp(session.get("end_time")),set_count=sets,total_reps=reps,volume_load_kg=volume,original=session)))
                    if len(records)>MAX_ROWS:
                        raise ValueError("Hevy import exceeds 100,000 rows")
                return "hevy-json",records,[],warnings
            if isinstance(value, list) and value and "hex" in value[0]:
                from whoop_protocol import wire, STANDARD_HR
                for row in value:
                    raw = bytes.fromhex(row["hex"])
                    char = row.get("char", row.get("characteristic", ""))
                    if len(raw) > 4096 or char != STANDARD_HR and (not wire.verify_whoop4_frame(raw) or raw[4] == 36 and raw[6] == 35):
                        raise ValueError("Raw capture includes a corrupt/unsupported/credential frame")
                    frames.append((str(row.get("device", "imported-whoop")),int(row.get("ts_ms", _now())),char,raw))
                    if len(frames) > MAX_ROWS:
                        raise ValueError("Capture exceeds 100,000 frames")
                return "raw-capture", [], frames, warnings
            if isinstance(value, dict) and isinstance(value.get("history"), list):
                for session in value["history"]:
                    start = _stamp(session.get("startTime"))
                    sets,reps,volume = 0,0,0.0
                    for entry in session.get("entries", []):
                        for index, row in enumerate(entry.get("sets", [])):
                            completed = _number(row.get("completedReps"))
                            if completed is None or completed<=0:
                                continue  # templates/planned sets are not logged work
                            raw_weight = row.get("weight",row.get("weightValue"))
                            unit = raw_weight.get("unit",entry.get("unit","kg")) if isinstance(raw_weight,dict) else entry.get("unit","kg")
                            weight = _number(raw_weight.get("value")) if isinstance(raw_weight,dict) else _number(raw_weight)
                            if weight is not None and str(unit).lower() in ("lb","lbs"):
                                weight *= .45359237
                            records.append(("lifting_set", dict(source="liftosaur",start_ms=start,exercise=entry.get("exercise",entry.get("name")),set_index=index,reps=completed,weight_kg=weight,details=row)))
                            sets += 1; reps += completed; volume += (weight or 0)*completed
                    if sets:
                        records.append(("workout",dict(source="liftosaur",name="Strength Training",start_ms=start,end_ms=_stamp(session.get("endTime")),set_count=sets,total_reps=reps,volume_load_kg=volume,details=session)))
                if len(records) > MAX_ROWS:
                    raise ValueError("Import exceeds 100,000 rows")
                return "liftosaur",records,[],warnings
            if isinstance(value, dict) and isinstance(value.get("records"), list):
                for row in value["records"]:
                    self._kind(row["kind"])
                    records.append((row["kind"],row.get("payload",row)))
                if len(records) > MAX_ROWS:
                    raise ValueError("Import exceeds 100,000 rows")
                return "boop-records",records,[],warnings
            raise ValueError("Unsupported JSON; expected capture, Liftosaur history or BOOP records")
        if suffix != ".csv":
            raise ValueError("Supported imports: CSV, Apple Health XML, GPX, TCX, capture/Liftosaur JSON, ZIP")
        delimiter = ";" if text.splitlines() and text.splitlines()[0].count(";")>text.splitlines()[0].count(",") else ","
        reader = csv.DictReader(io.StringIO(text),delimiter=delimiter)
        headers = set(reader.fieldnames or [])
        normal = {h.lower().strip() for h in headers}
        norm = {re.sub(r"[^a-z0-9]+","_",h.lower()).strip("_") for h in headers}
        if "total_sleep_duration" in norm and "date" in norm or "timestamp" in norm and "bpm" in norm:
            oura_rows = []
            for index,row in enumerate(reader):
                if index>=MAX_ROWS:
                    raise ValueError("Oura CSV exceeds 100,000 rows")
                n = {re.sub(r"[^a-z0-9]+","_",k.lower()).strip("_"):v for k,v in row.items() if k is not None}
                day = n.get("date",n.get("day"))
                if "timestamp" in n and "bpm" in n:
                    oura_rows.append(("health_sample",dict(source="oura",timestamp_ms=_stamp(n["timestamp"]),hr=_number(n["bpm"]),original=row)))
                    continue
                p = dict(source="oura",day=day,original=row)
                for key,out,scale in (("total_sleep_duration","total_sleep_min",60),("deep_sleep_duration","deep_min",60),("light_sleep_duration","light_min",60),("rem_sleep_duration","rem_min",60),("awake_time","awake_min",60),("sleep_efficiency","efficiency_pct",1),("average_resting_heart_rate","resting_hr",1),("average_hrv","hrv_ms",1),("respiratory_rate","respiratory_rate",1),("temperature_deviation","skin_temp_deviation_c",1),("readiness_score","reference_readiness_score",1),("sleep_score","reference_sleep_score",1),("steps","steps",1),("activity_burn","active_kcal",1)):
                    if _number(n.get(key)) is not None:
                        p[out] = _number(n[key])/scale
                oura_rows.append(("daily_metric",p))
            return "oura-csv",oura_rows,[],warnings
        if ("exercise" in normal or "exercise name" in normal) and not normal.intersection(("start_time","start time","timestamp","date")):
            programs = {}
            for index,row in enumerate(reader):
                if index>=MAX_ROWS:
                    raise ValueError("Program exceeds 100,000 rows")
                n = {k.lower().strip():v for k,v in row.items() if k is not None}
                exercise = n.get("exercise",n.get("exercise name",""))
                if not exercise:
                    continue
                name = n.get("program") or "Imported program"
                program = programs.setdefault(name,dict(source="lifting-program",name=name,note=n.get("program note",""),lines=[]))
                line = dict(exercise=exercise,primary_muscle=n.get("primary muscle",""),secondary_muscles=[v.strip() for v in (n.get("secondary muscles") or "").split(",") if v.strip()],note=n.get("note",""),original=row)
                for key,header in (("target_sets","sets"),("target_reps","reps"),("target_weight_kg","weight kg"),("target_max_rpe","max rpe"),("rest_sec","rest sec")):
                    cell = n.get(header,n.get("target "+header,n.get("target max rpe","") if key=="target_max_rpe" else ""))
                    match = re.match(r"\s*([+-]?\d+(?:[.,]\d+)?)",cell or "")
                    if match:
                        line[key] = float(match[1].replace(",","."))
                program["lines"].append(line)
            return "lifting-program-csv",[("lifting_program",p) for p in programs.values()],[],warnings
        if "Sleep onset" in headers and "Recovery score %" not in headers:
            kind, fmt = "sleep", "whoop-sleep"
        elif "Question text" in headers:
            kind, fmt = "journal", "whoop-journal"
        elif "Workout start time" in headers:
            kind, fmt = "workout", "whoop-workout"
        elif "Recovery score %" in headers:
            kind, fmt = "daily_metric", "whoop-cycle"
        elif "exercise_title" in normal or "exercise" in normal and "reps" in normal:
            kind, fmt = "lifting_set", "lifting-csv"
        elif any("protein" in h or "carb" in h for h in normal):
            kind, fmt = "nutrition", "nutrition-csv"
        elif {"biomarker name","value","status","recorded on date"} <= normal:
            kind, fmt = "lab", "whoop-biomarkers"
        elif any("marker" in h or "biomarker" in h for h in normal):
            kind, fmt = "lab", "lab-csv"
        elif "exercise" in normal or "exercise name" in normal:
            kind, fmt = "lifting_program", "lifting-program-csv"
        elif "kind" in normal:
            kind, fmt = None, "boop-csv"
        else:
            kind, fmt = "health_sample", "generic-csv"
            warnings.append("Unrecognised CSV headers: rows retained as health samples without inferred metrics")
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                raise ValueError("CSV exceeds 100,000 rows")
            if None in row:
                raise ValueError("CSV row has more values than headers")
            p = dict(source=fmt, original=row)
            n = {k.lower().strip():v for k,v in row.items()}
            zone = row.get("Cycle timezone", "UTC+00:00")
            start = row.get("Workout start time", row.get("Sleep onset", row.get("Cycle start time", n.get("start_time", n.get("date", n.get("day"))))))
            end = row.get("Workout end time", row.get("Wake onset", n.get("end_time")))
            p.update(start_ms=_stamp(start, zone),end_ms=_stamp(end,zone),day=str(start or "")[:10])
            if row.get("Activity name"):
                p["name"] = row["Activity name"]
            if kind == "journal":
                p.update(question=row.get("Question text"),answer=row.get("Answered yes/no", "").lower() == "true",notes=row.get("Notes", ""))
            if kind == "nutrition":
                for key, patterns in dict(calories=("energy", "calorie", "kcal"),protein_g=("protein",),carbs_g=("carb",),fat_g=("fat",),weight=("weight",)).items():
                    for h,v in n.items():
                        if any(token in h for token in patterns) and v:
                            try:
                                p[key] = float(v)
                            except ValueError:
                                warnings.append(f"Row {index+2}: nonnumeric {h} retained in original")
                            break
            if kind == "lifting_set":
                p["exercise"] = n.get("exercise_title", n.get("exercise"))
                p["session_title"] = n.get("title",n.get("workout_name","Strength Training"))
                p["set_type"] = n.get("set_type","normal")
                for key in ("weight_kg", "weight_lb", "reps", "set_index"):
                    if n.get(key):
                        p[key] = float(n[key])
                if "weight_lb" in p:
                    p["weight_kg"] = p["weight_lb"] * 0.45359237
            if fmt == "whoop-biomarkers":
                raw = n.get("value","")
                day = n.get("recorded on date","")
                if raw.strip().lower() in ("--","no data available") or day.strip().lower() in ("--","no data available"):
                    warnings.append(f"Row {index+2}: WHOOP marker not measured")
                    continue
                match = re.fullmatch(r"\s*([+-]?[\d,]+(?:\.\d+)?)\s*(.*)",raw)
                if not match:
                    warnings.append(f"Row {index+2}: invalid WHOOP biomarker value")
                    continue
                for pattern in ("%m/%d/%y","%m/%d/%Y","%Y-%m-%d"):
                    try:
                        day = datetime.strptime(day,pattern).date().isoformat()
                        break
                    except ValueError:
                        pass
                else:
                    warnings.append(f"Row {index+2}: invalid WHOOP biomarker date")
                    continue
                p.update(source="whoop-biomarkers",marker=n.get("biomarker name"),value=float(match[1].replace(",","")),unit=match[2],day=day,notes="WHOOP: "+n.get("status",""))
            selected_kind = kind or n.get("kind")
            if kind is None and n.get("payload_json"):
                p = json.loads(n["payload_json"])
            self._kind(selected_kind)
            records.append((selected_kind,p))
        if kind=="lifting_set":
            sessions = {}
            for selected,p in records:
                key = (p.get("session_title"),p.get("start_ms"))
                session = sessions.setdefault(key,dict(source="lifting-csv",name="Strength Training",title=key[0],start_ms=key[1],end_ms=p.get("end_ms"),set_count=0,volume_load_kg=0.0,total_reps=0,exercises=[]))
                if p.get("exercise") and p["exercise"] not in session["exercises"]:
                    session["exercises"].append(p["exercise"])
                if p.get("set_type") not in ("warmup","warm_up"):
                    session["set_count"] += 1
                    reps = p.get("reps",0)
                    session["total_reps"] += reps
                    session["volume_load_kg"] += p.get("weight_kg",0)*reps
            records.extend(("workout",p) for p in sessions.values())
        return fmt, records, [], warnings

    def _parse_mi_database(self,data):
        rows,warnings = [],[]
        mapping = {"steps_day":{"steps":"steps","distance":"distance_m"},"calories_day":{"calories":"active_kcal"},"heart_rate_day":{"avg_rhr":"resting_hr","avg_hr":"average_hr","min_hr":"min_hr","max_hr":"max_hr"},"sleep_day":{"total_duration":"total_sleep_min","sleep_deep_duration":"deep_min","sleep_light_duration":"light_min","sleep_rem_duration":"rem_min","sleep_awake_duration":"awake_min","sleep_score":"reference_sleep_score"},"stress_day":{"avg_stress":"stress"},"spo2_day":{"avg_spo2":"spo2"},"intensity_day":{"duration":"intensity_min"},"valid_stand_day":{"count":"stand_count"},"vitality":{"latest_accumulated_vitality":"vitality"}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"mi.db"; path.write_bytes(data)
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as source:
                source.row_factory = sqlite3.Row
                source.execute("PRAGMA trusted_schema=OFF")
                if source.execute("PRAGMA quick_check").fetchone()[0]!="ok":
                    raise ValueError("Mi Fitness database integrity check failed")
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables.intersection(mapping) and "sleep" not in tables:
                    raise ValueError("No Mi Fitness health tables found")
                for table in (*mapping,"sleep","heart_rate","spo2","stress","steps"):
                    if table not in tables:
                        continue
                    for row in source.execute(f'SELECT * FROM "{table}" WHERE deleted=0 ORDER BY time'):
                        value = json.loads(row["value"])
                        stamp = _stamp(row["time"])
                        day = datetime.fromtimestamp((row["time"]+row["zone_offset"]),timezone.utc).date().isoformat()
                        p = dict(source="mi-fitness",source_table=table,day=day,timestamp_ms=stamp,original=value)
                        if table=="sleep":
                            p["start_ms"] = _stamp(value.get("bedtime",value.get("device_bedtime",value.get("bed_timestamp"))))
                            p["end_ms"] = _stamp(value.get("wake_up_time",value.get("device_wake_up_time",value.get("out_bed_timestamp",row["time"]))))
                            for key,out in mapping["sleep_day"].items():
                                if _number(value.get(key)) is not None:
                                    p[out] = _number(value[key])
                            p["stages"] = [dict(stage={1:"wake",2:"light",3:"deep",4:"rem",5:"awake_in_bed"}.get(v.get("state"),"unknown"),start_ms=_stamp(v.get("start_time")),end_ms=_stamp(v.get("end_time"))) for v in value.get("items",[]) if v.get("end_time",0)>v.get("start_time",0)]
                            for key,out in (("avg_hr","average_hr"),("min_hr","resting_hr"),("max_hr","max_hr")):
                                if (_number(value.get(key)) or 0)>0:
                                    p[out] = _number(value[key])
                            kind = "sleep"
                        elif table in mapping:
                            for key,out in mapping[table].items():
                                number = _number(value.get(key))
                                if number is not None and (number>0 or out in ("steps","distance_m","active_kcal")):
                                    p[out] = number
                            kind = "daily_metric"
                        else:
                            field = {"heart_rate":"hr","spo2":"spo2","steps":"steps","stress":"stress"}[table]
                            number = _number(value.get(field,value.get("value")))
                            if number is not None:
                                p[field] = number
                            kind = "health_sample"
                        rows.append((kind,p))
                        if len(rows)>MAX_ROWS:
                            raise ValueError("Mi Fitness import exceeds 100,000 rows")
        return "mi-fitness",rows,[],warnings

    def _parse_fit(self,filename,data):
        records = _fit_records(data)
        samples,points,sessions = [],[],[]
        epoch = 631065600
        for message,values in records:
            stamp = (values[253]+epoch)*1000 if 253 in values else None
            if message==20:
                if 3 in values and stamp is not None:
                    samples.append(("health_sample",dict(source="fit",timestamp_ms=stamp,hr=values[3])))
                if 0 in values and 1 in values:
                    lat,lon = values[0]*180/2**31,values[1]*180/2**31
                    if -90<=lat<=90 and -180<=lon<=180:
                        point = dict(lat=lat,lon=lon,timestamp_ms=stamp)
                        if 2 in values:
                            point["ele"] = values[2]/5-500
                        points.append(point)
            elif message==18:
                p = dict(source="fit",name={0:"Activity",1:"Running",2:"Cycling",5:"Swimming",10:"Strength Training",11:"Walking"}.get(values.get(5),"Activity"),start_ms=(values[2]+epoch)*1000 if 2 in values else stamp,end_ms=stamp,original={str(k):v for k,v in values.items()})
                for key,out,scale in ((9,"distance_m",100),(11,"calories",1),(16,"average_hr",1),(17,"max_hr",1),(7,"elapsed_seconds",1000),(22,"ascent_m",1)):
                    if key in values:
                        p[out] = values[key]/scale
                sessions.append(("workout",p))
        if points:
            sessions.append(("route",dict(source="fit",name=Path(filename).stem,points=points)))
        if not any(kind=="workout" for kind,p in sessions) and samples:
            stamps = [p["timestamp_ms"] for k,p in samples]
            sessions.append(("workout",dict(source="fit",name="Activity",start_ms=min(stamps),end_ms=max(stamps))))
        if not sessions and not samples:
            raise ValueError("FIT contains no supported activity/HR/route messages")
        return "fit",sessions+samples,[],["FIT developer fields and unsupported message types are skipped"]

    def _parse_wearable_json(self,filename,value):
        name = filename.lower()
        rows,warnings = [],[]
        def add(kind,p):
            rows.append((kind,p))
            if len(rows)>MAX_ROWS:
                raise ValueError("Wearable export exceeds 100,000 rows")
        categories = ("sleep","daily_readiness","readiness","daily_activity","activity","daily_sleep","daily_spo2","vo2max","heartrate","heart_rate")
        oura = isinstance(value,dict) and any(k in value for k in categories) and any(any(key in row for key in ("bedtime_start","contributors","temperature_deviation","steps","score")) for k in categories for row in _rows(value.get(k)))
        basename = Path(filename).stem.lower()
        if oura or "oura" in name or basename in categories and isinstance(value,dict) and "data" in value:
            if isinstance(value,list):
                category = next((k for k in categories if k in name),"sleep")
                value = {category:value}
            elif isinstance(value,dict) and "data" in value and basename in categories:
                value = {basename:value}
            if not isinstance(value,dict):
                return None
            for category in categories:
                for row in _rows(value.get(category)):
                    if row.get("type")=="deleted":
                        continue
                    p = dict(source="oura",source_category=category,day=row.get("day"),original=row)
                    if category=="sleep":
                        p.update(start_ms=_stamp(row.get("bedtime_start")),end_ms=_stamp(row.get("bedtime_end")))
                        for key,out,scale in (("total_sleep_duration","total_sleep_min",60),("deep_sleep_duration","deep_min",60),("light_sleep_duration","light_min",60),("rem_sleep_duration","rem_min",60),("awake_time","awake_min",60),("average_heart_rate","average_hr",1),("lowest_heart_rate","resting_hr",1),("average_hrv","hrv_ms",1),("average_breath","respiratory_rate",1),("efficiency","efficiency_pct",1)):
                            if _number(row.get(key)) is not None:
                                p[out] = _number(row[key])/scale
                        add("sleep",p)
                        # Preserve each period; analytics chooses main sleep by source type/duration.
                        add("daily_metric",p|dict(source_sleep_type=row.get("type","")))
                    elif category in ("heartrate","heart_rate"):
                        p.update(timestamp_ms=_stamp(row.get("timestamp")),hr=_number(row.get("bpm")))
                        add("health_sample",p)
                    else:
                        fields = {"steps":"steps","active_calories":"active_kcal","total_calories":"calories","equivalent_walking_distance":"distance_m","temperature_deviation":"skin_temp_deviation_c","vo2_max":"vo2_max"}
                        for key,out in fields.items():
                            if _number(row.get(key)) is not None:
                                p[out] = _number(row[key])
                        if "score" in row:
                            p["reference_"+category+"_score"] = row["score"]
                        if isinstance(row.get("spo2_percentage"),dict):
                            p["spo2"] = _number(row["spo2_percentage"].get("average"))
                        add("daily_metric",p)
            return "oura-json",rows,[],warnings
        entries = _rows(value)
        if "fitbit" in name or any("dateOfSleep" in r or "levels" in r and "startTime" in r for r in entries) or any(token in name for token in ("resting_heart_rate","heart_rate-","steps-")):
            totals = {}
            for row in entries:
                p = dict(source="fitbit",original=row)
                if "startTime" in row and "endTime" in row:
                    p.update(start_ms=_stamp(row["startTime"]),end_ms=_stamp(row["endTime"]),day=row.get("dateOfSleep"),total_sleep_min=_number(row.get("minutesAsleep")),awake_min=_number(row.get("minutesAwake")),efficiency_pct=_number(row.get("efficiency")))
                    summary = row.get("levels",{}).get("summary",{})
                    for key in ("deep","light","rem","wake"):
                        if isinstance(summary.get(key),dict):
                            p[("awake" if key=="wake" else key)+"_min"] = _number(summary[key].get("minutes"))
                    p["stages"] = [dict(stage=v.get("level"),start_ms=_stamp(v.get("dateTime")),duration_seconds=v.get("seconds")) for v in row.get("levels",{}).get("data",[])]
                    add("sleep",p)
                    add("daily_metric",p)
                else:
                    rawdate = row.get("dateTime",row.get("date"))
                    stamp = _stamp(rawdate)
                    if stamp is None and rawdate:
                        for fmt in ("%m/%d/%y %H:%M:%S","%m/%d/%y"):
                            try:
                                stamp = int(datetime.strptime(rawdate,fmt).replace(tzinfo=timezone.utc).timestamp()*1000); break
                            except ValueError:
                                pass
                    p.update(timestamp_ms=stamp,day=datetime.fromtimestamp(stamp/1000,timezone.utc).date().isoformat() if stamp else str(rawdate or "")[:10])
                    number = row.get("value")
                    number = _number(number.get("value",number.get("bpm"))) if isinstance(number,dict) else _number(number)
                    if "resting_heart_rate" in name:
                        p["resting_hr"] = number; add("daily_metric",p)
                    elif "steps" in name:
                        totals[p["day"]] = totals.get(p["day"],0)+(number or 0)
                    else:
                        p["hr"] = number; add("health_sample",p)
            for day,total in totals.items():
                add("daily_metric",dict(source="fitbit",day=day,steps=total))
            return "fitbit-json",rows,[],["Fitbit offsetless timestamps follow NOOP's UTC interpretation"]
        if "garmin" in name or "di_connect" in name or any("calendarDate" in r or "sleepStartTimestampGMT" in r for r in entries):
            for row in entries:
                p = dict(source="garmin",day=row.get("calendarDate",row.get("calendar_date")),original=row)
                if "sleepStartTimestampGMT" in row:
                    p.update(start_ms=_stamp(row["sleepStartTimestampGMT"]),end_ms=_stamp(row.get("sleepEndTimestampGMT")))
                    for key,out in (("deepSleepSeconds","deep_min"),("lightSleepSeconds","light_min"),("remSleepSeconds","rem_min"),("awakeSleepSeconds","awake_min")):
                        if _number(row.get(key)) is not None:
                            p[out] = _number(row[key])/60
                    if _number(row.get("averageRespirationValue")) is not None:
                        p["respiratory_rate"] = _number(row["averageRespirationValue"])
                    if "overallSleepScore" in row:
                        p["reference_sleep_score"] = row["overallSleepScore"]
                    add("sleep",p)
                for key,out in (("restingHeartRate","resting_hr"),("restingHeartRateInBeatsPerMinute","resting_hr"),("steps","steps"),("totalSteps","steps"),("totalDistanceMeters","distance_m"),("totalDistanceInMeters","distance_m"),("activeKilocalories","active_kcal"),("activeCalories","active_kcal"),("averageStressLevel","stress")):
                    if _number(row.get(key)) is not None:
                        p[out] = _number(row[key])
                if p["day"] or len(p)>3:
                    add("daily_metric",p)
            return "garmin-json",rows,[],warnings
        if "biomarker" in name or entries and all("Biomarker Name" in r for r in entries):
            fields = ["Biomarker Name","Value","Status","Recorded On Date"]
            out = io.StringIO(newline=""); writer = csv.DictWriter(out,fieldnames=fields,extrasaction="ignore"); writer.writeheader()
            for row in entries:
                writer.writerow(row)
            return self._parse("biomarkers.csv",out.getvalue().encode())
        return None

    @staticmethod
    def _import_key(kind, payload):
        # Filename-independent; source contents decide duplicate identity.
        p = dict(payload)
        for key in ("id", "kind", "created_ms", "updated_ms", "deleted_ms"):
            p.pop(key, None)
        return hashlib.sha256((kind + "\n" + _json(p)).encode()).hexdigest()

    def import_preview(self, filename, data):
        fmt, records, frames, warnings = self._parse(filename,data)
        if fmt == "backup":
            with _backup_file(data) as (path,settings):
                with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as source, closing(self.store.connect()) as target:
                    source.execute("PRAGMA trusted_schema=OFF")
                    if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("Backup integrity check failed")
                    tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not ({"frames","readings"} <= tables or {"hrSample","device"} <= tables):
                        raise ValueError("Unrecognised BOOP/NOOP database schema")
                    allowed = ("frames","readings","chunks","feature_records",*NOOP_TABLE_KINDS)
                    counts = {table:source.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in allowed if table in tables}
                    if sum(counts.values()) > MAX_BACKUP_ROWS:
                        raise ValueError("Backup exceeds the 10,000,000 row restore limit")
                    duplicates = 0
                    if "frames" in tables:
                        existing = {tuple(row) for row in target.execute("SELECT device,digest FROM frames")}
                        duplicates += sum(tuple(row) in existing for row in source.execute("SELECT device,digest FROM frames"))
                        duplicates += sum(tuple(row) in existing for row in source.execute("SELECT f.device,f.digest FROM readings r JOIN frames f ON f.id=r.frame_id"))
                        if "chunks" in tables:
                            chunks = {tuple(row) for row in target.execute("SELECT device,saved_ms,end_block,frames FROM chunks")}
                            duplicates += sum(tuple(row) in chunks for row in source.execute("SELECT device,saved_ms,end_block,frames FROM chunks"))
                    if "feature_records" in tables:
                        existing = {r[0] for r in target.execute("SELECT id FROM feature_records")}
                        duplicates += sum(r[0] in existing for r in source.execute("SELECT id FROM feature_records"))
            return dict(format="backup",count=sum(counts.values()),counts=counts,duplicates=duplicates,sample=[],warnings=warnings,raw_frames=counts.get("frames",0))
        keys = {self._import_key(k, _safe_payload(p,16*1024*1024 if k=="route" else 1_000_000)) for k,p in records}
        with closing(self.store.connect()) as conn:
            existing = {r[0] for r in conn.execute("SELECT import_key FROM feature_records WHERE import_key IS NOT NULL")}
        duplicates = len(records) - len(keys) + len(keys & existing)
        if frames:
            from whoop_protocol import STANDARD_HR
            incoming = [(device,hashlib.sha256(raw+(received.to_bytes(8,"little") if characteristic==STANDARD_HR else b"")).hexdigest()) for device,received,characteristic,raw in frames]
            with closing(self.store.connect()) as conn:
                stored = {tuple(row) for row in conn.execute("SELECT device,digest FROM frames")}
            unique = set(incoming)
            duplicates += len(incoming)-len(unique)+len(unique&stored)
        def sample_payload(payload):
            result = dict(payload)
            for key,value in result.items():
                if isinstance(value,list) and len(value)>5:
                    result[key] = value[:5]
                    result[key+"_count"] = len(value)
            return result
        return dict(format=fmt, count=len(records)+len(frames), counts=dict(Counter(k for k,p in records)), duplicates=duplicates,
                    sample=[dict(kind=k,payload=sample_payload(p)) for k,p in records[:5]],warnings=list(dict.fromkeys(warnings))[:50], raw_frames=len(frames))

    def import_apply(self, filename, data):
        preview = self.import_preview(filename,data)
        if preview["format"] == "backup":
            return self.backup_restore(filename,data)
        fmt, records, frames, warnings = self._parse(filename,data)
        digest = hashlib.sha256(data).hexdigest()
        backup = self._snapshot("import")
        if frames:
            added = self.store.save(frames)
            return dict(imported=added,duplicates=len(frames)-added,backup=backup,format=fmt)
        added = 0
        with closing(self.store.connect()) as conn:
            with conn:
                for kind,payload in records:
                    key = self._import_key(kind,payload)
                    if conn.execute("SELECT 1 FROM feature_records WHERE import_key=?", (key,)).fetchone():
                        continue
                    payload = dict(payload)
                    payload.pop("id", None)  # imports never overwrite user-owned ids
                    self._save(conn,kind,payload,key)
                    added += 1
                conn.execute("INSERT OR IGNORE INTO feature_imports VALUES(?,?,?,?)", (digest,Path(filename).name,_now(),added))
        return dict(imported=added,duplicates=len(records)-added,backup=backup,format=fmt,warnings=warnings[:50])

    def export_records(self, kind=None):
        if kind:
            self._kind(kind)
        with closing(self.store.connect()) as conn:
            rows = conn.execute("SELECT * FROM feature_records WHERE deleted_ms IS NULL" + (" AND kind=?" if kind else "") + " ORDER BY created_ms", (kind,) if kind else ())
            return json.dumps(dict(format="boop-records-v1",records=[dict(kind=r["kind"],payload=self._record(r)) for r in rows]),indent=2)

    def export_csv(self, kind):
        rows = self.list_records(kind,{"limit":MAX_ROWS})
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(("kind","payload_json"))
        writer.writerows((kind,_json(r)) for r in rows)
        return out.getvalue()

    def export_whoop_csv(self, kind):
        """WHOOP column layout accepted by NOOP's StrandImport CSV importer.

        Imported source values round-trip; manual rows export only supplied facts.
        Missing strain/recovery/stages remain empty, never estimated here.
        """
        headers = {
            "journal":["Cycle start time","Cycle timezone","Question text","Answered yes/no","Notes"],
            "workout":["Cycle start time","Workout start time","Workout end time","Cycle timezone","Activity name","Activity Strain","Energy burned (cal)","Max HR (bpm)","Average HR (bpm)","HR Zone 1 %","HR Zone 2 %","HR Zone 3 %","HR Zone 4 %","HR Zone 5 %"],
            "sleep":["Cycle start time","Sleep onset","Wake onset","Cycle timezone","Nap","Sleep performance %","Respiratory rate (rpm)","Asleep duration (min)","In bed duration (min)","Light sleep duration (min)","Deep (SWS) duration (min)","REM duration (min)","Awake duration (min)","Sleep efficiency %","Sleep consistency %","Sleep need (min)","Sleep debt (min)"],
            "daily_metric":["Cycle start time","Cycle end time","Cycle timezone","Recovery score %","Resting heart rate (bpm)","Heart rate variability (ms)","Skin temp (celsius)","Blood oxygen %","Day Strain","Energy burned (cal)","Max HR (bpm)","Average HR (bpm)"]}
        if kind not in headers:
            raise ValueError("WHOOP CSV export supports sleep, workout, journal and daily_metric")
        rows = self.list_records(kind,{"limit":MAX_ROWS})
        fields = headers[kind]
        out = io.StringIO(newline="")
        writer = csv.DictWriter(out,fieldnames=fields,extrasaction="ignore")
        writer.writeheader()
        def stamp(ms):
            return datetime.fromtimestamp(ms/1000,timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms is not None else ""
        for record in rows:
            original = record.get("original",{})
            if any(h in original for h in fields):
                writer.writerow(original)
                continue
            start = stamp(record.get("start_ms",record.get("timestamp_ms"))) or str(record.get("day", ""))
            row = {"Cycle start time":start,"Cycle timezone":"UTC+00:00"}
            if kind == "journal":
                row.update({"Question text":record.get("question", ""),"Answered yes/no":str(record.get("answer", "")).lower(),"Notes":record.get("notes", "")})
            elif kind == "workout":
                row.update({"Workout start time":start,"Workout end time":stamp(record.get("end_ms")),"Activity name":record.get("name",record.get("sport", ""))})
            elif kind == "sleep":
                row.update({"Sleep onset":start,"Wake onset":stamp(record.get("end_ms")),"Nap":str(record.get("nap",False)).lower()})
            writer.writerow(row)
        return out.getvalue()

    def _route_points(self,record_id):
        with closing(self.store.connect()) as conn:
            row = conn.execute("SELECT payload_json FROM feature_records WHERE id=? AND kind='route' AND deleted_ms IS NULL",(record_id,)).fetchone()
        if row is None:
            raise ValueError("Route not found")
        payload = json.loads(row[0]); points = []
        for raw in payload.get("points",[]):
            lat = _number(raw.get("lat",raw.get("LatitudeDegrees")))
            lon = _number(raw.get("lon",raw.get("LongitudeDegrees")))
            if lat is None or lon is None or not -90<=lat<=90 or not -180<=lon<=180:
                continue
            points.append(dict(lat=lat,lon=lon,ele=_number(raw.get("ele",raw.get("AltitudeMeters"))),timestamp_ms=raw.get("timestamp_ms",_stamp(raw.get("time",raw.get("Time")))),hr=_number(raw.get("hr",raw.get("Value")))))
        if not points:
            raise ValueError("Route has no valid coordinates")
        return payload,points

    def export_route_gpx(self,record_id):
        payload,points = self._route_points(record_id)
        root = ET.Element("gpx",version="1.1",creator="BOOP",xmlns="http://www.topografix.com/GPX/1/1")
        track = ET.SubElement(root,"trk"); ET.SubElement(track,"name").text = payload.get("name","Route")
        segment = ET.SubElement(track,"trkseg")
        for p in points:
            node = ET.SubElement(segment,"trkpt",lat=str(p["lat"]),lon=str(p["lon"]))
            if p["ele"] is not None:
                ET.SubElement(node,"ele").text = str(p["ele"])
            if p["timestamp_ms"] is not None:
                ET.SubElement(node,"time").text = datetime.fromtimestamp(p["timestamp_ms"]/1000,timezone.utc).isoformat().replace("+00:00","Z")
            if p["hr"] is not None:
                extensions = ET.SubElement(node,"extensions")
                trackpoint = ET.SubElement(extensions,"{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}TrackPointExtension")
                ET.SubElement(trackpoint,"{http://www.garmin.com/xmlschemas/TrackPointExtension/v1}hr").text = str(round(p["hr"]))
        return ET.tostring(root,encoding="utf-8",xml_declaration=True)

    def export_route_fit(self,record_id):
        payload,points = self._route_points(record_id)
        body = bytearray()
        def definition(local,global_id,fields):
            body.extend(bytes([64|local,0,0])+struct.pack("<H",global_id)+bytes([len(fields)]))
            for field,size,base in fields:
                body.extend(bytes([field,size,base]))
        definition(0,0,[(0,1,0),(1,2,132),(2,2,132),(4,4,134)])
        times = [int(p["timestamp_ms"]//1000)-631065600 for p in points if p["timestamp_ms"] is not None]
        valid_times = [t for t in times if 0<=t<0xffffffff]
        body.extend(bytes([0,4])+struct.pack("<HHI",255,0,min(valid_times) if valid_times else 0xffffffff))
        definition(1,20,[(253,4,134),(0,4,133),(1,4,133),(2,2,132),(3,1,2)])
        for p in points:
            stamp = int(p["timestamp_ms"]//1000)-631065600 if p["timestamp_ms"] is not None else 0xffffffff
            if not 0<=stamp<=0xffffffff:
                stamp = 0xffffffff
            altitude = round((p["ele"]+500)*5) if p["ele"] is not None else 0xffff
            hr = round(p["hr"]) if p["hr"] is not None else 0xff
            body.extend(bytes([1])+struct.pack("<IiiHB",stamp,round(p["lat"]*2**31/180),round(p["lon"]*2**31/180),altitude if 0<=altitude<0xffff else 0xffff,hr if 0<hr<255 else 255))
        if valid_times:
            definition(2,18,[(253,4,134),(2,4,134),(5,1,0)])
            body.extend(bytes([2])+struct.pack("<IIB",max(valid_times),min(valid_times),0))
        def crc(data):
            value = 0
            for byte in data:
                value ^= byte
                for bit in range(8):
                    value = (value>>1)^0xa001 if value&1 else value>>1
            return value
        header = bytes([14,0x10])+struct.pack("<HI",2100,len(body))+b".FIT"
        header += struct.pack("<H",crc(header))
        output = header+body
        return output+struct.pack("<H",crc(output))

    def export_nutrition_csv(self):
        out = io.StringIO(newline="")
        writer = csv.writer(out); writer.writerow(("Date","Calories","Protein","Carbs","Fat","Weight"))
        for row in self.list_records("nutrition",{"limit":MAX_ROWS}):
            writer.writerow((row.get("day",""),*(row.get(k,"") for k in ("calories","protein_g","carbs_g","fat_g","weight"))))
        return out.getvalue()

    def export_lifting_xlsx(self):
        columns = ["Program","Program note","Exercise","Primary muscle","Secondary muscles","Sets","Reps","Weight kg","Max RPE","Rest sec","Note"]
        rows = [columns]
        for program in self.list_records("lifting_program",{"limit":MAX_ROWS}):
            for line in program.get("lines",[]):
                rows.append([program.get("name",""),program.get("note",""),line.get("exercise",""),line.get("primary_muscle",""),", ".join(line.get("secondary_muscles",[])),*(line.get(k,"") for k in ("target_sets","target_reps","target_weight_kg","target_max_rpe","rest_sec","note"))])
        ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        sheet = ET.Element("worksheet",xmlns=ns); data = ET.SubElement(sheet,"sheetData")
        for index,row in enumerate(rows,1):
            element = ET.SubElement(data,"row",r=str(index))
            for col,value in enumerate(row):
                cell = ET.SubElement(element,"c",r=chr(65+col)+str(index),t="inlineStr")
                ET.SubElement(ET.SubElement(cell,"is"),"t").text = str(value)
        output = io.BytesIO()
        with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml",'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
            archive.writestr("_rels/.rels",'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
            archive.writestr("xl/workbook.xml",f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Programs" sheetId="1" r:id="rId1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels",'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml",ET.tostring(sheet,encoding="utf-8",xml_declaration=True))
        return output.getvalue()

    def backup_export(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Backup destination cannot be the live database")
        destination.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "boop-backup.sqlite"
            self.store.backup(db)
            # Settings exports use a whitelist and exclude action opt-ins.
            with closing(self.store.connect()) as source:
                explicit = {r[0] for r in source.execute("SELECT key FROM feature_settings")}
            safe = {k:v for k,v in self.settings().items() if k in explicit and k in DEFAULT_SETTINGS and k not in BACKUP_ACTION_KEYS}
            with closing(sqlite3.connect(db)) as conn, conn:
                conn.execute("PRAGMA secure_delete=ON")
                tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for kind,name in conn.execute("SELECT type,name FROM sqlite_master WHERE type IN ('trigger','view')").fetchall():
                    conn.execute('DROP '+kind.upper()+' "'+name.replace('"','""')+'"')
                # Own-format data only. A future/plugin credential or action table
                # is never swept into a health archive merely because it is SQLite.
                for table in tables-BACKUP_TABLES-{'sqlite_sequence'}:
                    conn.execute('DROP TABLE "'+table.replace('"','""')+'"')
                if 'boop_control_settings' in tables:conn.execute('DELETE FROM boop_control_settings')
                if 'lift_control' in tables:
                    for device,encoded in conn.execute('SELECT device,state_json FROM lift_control').fetchall():
                        if encoded is not None:conn.execute('UPDATE lift_control SET state_json=? WHERE device=?',(_json(_paused_lift(json.loads(encoded))),device))
                if 'workout_edits' in tables:
                    for op,before,after in conn.execute('SELECT id,before_json,after_json FROM workout_edits').fetchall():
                        conn.execute('UPDATE workout_edits SET before_json=?,after_json=? WHERE id=?',(_json(_edit_states(before)),_json(_edit_states(after)),op))
                conn.execute("DELETE FROM feature_settings")
                conn.executemany("INSERT INTO feature_settings VALUES(?,?)", [(k,_json(v)) for k,v in safe.items()])
                # Validate even legacy/direct-written rows before copying them
                # outside the app; normal record CRUD already enforces this.
                for table,identity in (('feature_records','id'),('feature_revisions','revision')):
                    for key,kind,payload in conn.execute('SELECT '+identity+',kind,payload_json FROM '+table).fetchall():
                        p=_safe_payload(json.loads(payload),16*1024*1024 if kind=='route' else 1_000_000)
                        if kind in ('alarm','reminder'):p['enabled']=False
                        conn.execute('UPDATE '+table+' SET payload_json=? WHERE '+identity+'=?',(_json(p),key))
            # Removed preferences must not survive in SQLite's free pages.
            # Compact only the copied database, after its sanitizing transaction.
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("VACUUM")
            temporary = destination.with_name(destination.name+"."+uuid.uuid4().hex+".tmp")
            try:
                with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED) as archive:
                    archive.write(db,"boop-backup.sqlite")
                    archive.writestr("settings.json",_json(safe))
                    archive.writestr("manifest.json",_json(dict(format="boop-backup",version=1,created_ms=_now())))
                with zipfile.ZipFile(temporary) as archive:
                    if archive.testzip():
                        raise ValueError("Backup integrity verification failed")
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
        return dict(path=str(destination.resolve()),bytes=destination.stat().st_size,format="boop-backup-v1")

    def backup_restore(self, filename, data):
        with _backup_file(data) as (source_path,settings):
            with closing(sqlite3.connect(source_path.as_uri()+"?mode=ro",uri=True)) as source:
                source.row_factory = sqlite3.Row
                source.execute("PRAGMA trusted_schema=OFF")
                if source.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity check failed")
                tables = {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not ({"frames","readings"} <= tables or {"hrSample","device"} <= tables):
                    raise ValueError("Unrecognised BOOP/NOOP database schema")
                if sum(source.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables if t in BACKUP_TABLES or t in NOOP_TABLE_KINDS) > MAX_BACKUP_ROWS:
                    raise ValueError("Backup exceeds the 10,000,000 row restore limit")
                backup = self._snapshot("restore")
                counts = Counter()
                with closing(self.store.connect()) as target:
                    with target:
                        target.execute('BEGIN IMMEDIATE')
                        for statement in BACKUP_EXTRA_SCHEMA:target.execute(statement)
                        if 'context_eligible' not in {r[1] for r in target.execute('PRAGMA table_info(boop_coach_messages)')}:
                            target.execute('ALTER TABLE boop_coach_messages ADD COLUMN context_eligible INTEGER NOT NULL DEFAULT 1')
                        if "frames" in tables:
                            frame_ids={}
                            for frame in source.execute("SELECT * FROM frames"):
                                from whoop_protocol import STANDARD_HR, wire, standard_hr
                                raw = frame["raw"]
                                standard = frame["characteristic"] == STANDARD_HR
                                if not isinstance(raw,bytes) or len(raw) > 4096:
                                    raise ValueError("Backup contains an unsupported raw frame")
                                if standard:
                                    standard_hr(raw)
                                elif not wire.verify_whoop4_frame(raw) or raw[4] == 36 and raw[6] == 35:
                                    raise ValueError("Backup contains corrupt/unsupported/credential frame")
                                digest = hashlib.sha256(raw + (frame["received_ms"].to_bytes(8,"little") if standard else b"")).hexdigest()
                                if digest != frame["digest"]:
                                    raise ValueError("Backup raw-frame digest does not match its contents")
                                cur = target.execute("INSERT OR IGNORE INTO frames(device,received_ms,characteristic,packet_type,version,digest,raw) VALUES(?,?,?,?,?,?,?)", tuple(frame[k] for k in ("device","received_ms","characteristic","packet_type","version","digest","raw")))
                                if cur.rowcount:
                                    counts["frames"] += 1
                                frame_ids[frame['id']]=target.execute('SELECT id FROM frames WHERE device=? AND digest=?',(frame['device'],digest)).fetchone()[0]
                            # Missing decoded children can be filled even when the
                            # original frame already exists; existing observations win.
                            for reading in source.execute('SELECT * FROM readings'):
                                if reading['frame_id'] not in frame_ids:raise ValueError('Backup reading has no original frame')
                                cur=target.execute("INSERT OR IGNORE INTO readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (frame_ids[reading['frame_id']],)+tuple(reading[k] for k in ("device","kind","timestamp_ms","received_ms","device_seconds","hr","rr_json","contact","gx","gy","gz","layout")))
                                counts['readings']+=cur.rowcount
                            if 'sensors' in tables:
                                for sensor in source.execute('SELECT * FROM sensors'):
                                    if sensor['frame_id'] not in frame_ids:raise ValueError('Backup sensor has no original frame')
                                    values=_safe_payload(json.loads(sensor['values_json']))
                                    cur=target.execute('INSERT OR IGNORE INTO sensors VALUES(?,?,?,?,?,?)',(frame_ids[sensor['frame_id']],)+tuple(sensor[k] for k in ('device','timestamp_ms','received_ms','source'))+(_json(values),))
                                    counts['sensors']+=cur.rowcount
                            # Sensor backfill progress is a local frame-ID cursor,
                            # never a portable migration claim. Re-derive safely.
                            target.execute('DELETE FROM sensor_migrations')
                            if "chunks" in tables:
                                for row in source.execute("SELECT * FROM chunks"):
                                    values = tuple(row[k] for k in ("device","saved_ms","end_block","frames"))
                                    if not target.execute("SELECT 1 FROM chunks WHERE device=? AND saved_ms=? AND end_block=? AND frames=?", values).fetchone():
                                        target.execute("INSERT INTO chunks(device,saved_ms,end_block,frames) VALUES(?,?,?,?)",values)
                                        counts["chunks"] += 1
                        if "feature_records" in tables:
                            inserted_ids = set()
                            for row in source.execute("SELECT * FROM feature_records"):
                                self._kind(row["kind"])
                                p = _safe_payload(json.loads(row["payload_json"]),16*1024*1024 if row["kind"]=="route" else 1_000_000)
                                if row["kind"] in ("alarm","reminder"):
                                    p["enabled"] = False
                                cur = target.execute("INSERT OR IGNORE INTO feature_records VALUES(?,?,?,?,?,?,?)", (row["id"],row["kind"],_json(p),row["created_ms"],row["updated_ms"],row["deleted_ms"],row["import_key"]))
                                counts["records"] += cur.rowcount
                                if cur.rowcount:
                                    inserted_ids.add(row["id"])
                            if "feature_settings" in tables:
                                settings = {r[0]:json.loads(r[1]) for r in source.execute("SELECT key,value_json FROM feature_settings")} | settings
                            if "feature_revisions" in tables:
                                for row in source.execute("SELECT * FROM feature_revisions"):
                                    if row["record_id"] not in inserted_ids:
                                        continue
                                    p = _safe_payload(json.loads(row["payload_json"]),16*1024*1024 if row["kind"]=="route" else 1_000_000)
                                    if row["kind"] in ("alarm","reminder"):
                                        p["enabled"] = False
                                    values = (row["record_id"],row["kind"],_json(p),row["deleted_ms"],row["saved_ms"])
                                    if not target.execute("SELECT 1 FROM feature_revisions WHERE record_id=? AND kind=? AND payload_json=? AND deleted_ms IS ? AND saved_ms=?",values).fetchone():
                                        target.execute("INSERT INTO feature_revisions(record_id,kind,payload_json,deleted_ms,saved_ms) VALUES(?,?,?,?,?)",values)
                            if "feature_imports" in tables:
                                for row in source.execute("SELECT * FROM feature_imports"):
                                    target.execute("INSERT OR IGNORE INTO feature_imports VALUES(?,?,?,?)",tuple(row))
                        for table,columns in [('boop_devices',('address','name','forgotten')),('boop_coach_messages',('id','role','text','provider','created_ms','deleted_ms','context_eligible'))]:
                            if table in tables:
                                for row in source.execute('SELECT * FROM '+table+(' ORDER BY rowid' if table=='boop_coach_messages' else '')):
                                    if table=='boop_coach_messages' and (row['role'] not in ('user','assistant') or not isinstance(row['text'],str) or len(row['text'])>24000):raise ValueError('Backup contains an invalid Coach message')
                                    cur=target.execute('INSERT OR IGNORE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(0 if k=='context_eligible' else row[k] for k in columns))
                                    counts[table]+=cur.rowcount
                        if 'workout_edits' in tables:
                            for row in source.execute('SELECT * FROM workout_edits'):
                                before,after=_edit_states(row['before_json']),_edit_states(row['after_json'])
                                if set(before)!=set(after):raise ValueError('Backup workout edit bindings differ')
                                matches=True
                                for key,expected in after.items():
                                    actual=target.execute('SELECT * FROM feature_records WHERE id=?',(key,)).fetchone()
                                    if expected is None or actual is None or any(actual[k]!=expected[k] for k in ('kind','payload_json','deleted_ms','updated_ms')):matches=False
                                # Retain conflicting audit history, but never let
                                # its undo replace an existing local record.
                                undone=row['undone_ms'] if row['undone_ms'] is not None or matches else _now()
                                cur=target.execute('INSERT OR IGNORE INTO workout_edits VALUES(?,?,?,?,?,?,?)',(row['id'],row['device'],row['day'],_json(before),_json(after),row['created_ms'],undone))
                                counts['workout_edits']+=cur.rowcount
                        if 'workout_dismissals' in tables:
                            for row in source.execute('SELECT * FROM workout_dismissals'):
                                if not isinstance(row['start_ms'],int) or not isinstance(row['end_ms'],int) or row['start_ms']<0 or row['end_ms']<=row['start_ms']:raise ValueError('Backup contains invalid workout dismissal bounds')
                                cur=target.execute('INSERT OR IGNORE INTO workout_dismissals VALUES(?,?,?,?)',tuple(row[k] for k in ('device','start_ms','end_ms','operation_id')))
                                counts['workout_dismissals']+=cur.rowcount
                        if 'lift_control' in tables:
                            for row in source.execute('SELECT * FROM lift_control'):
                                if row['state_json'] is None:continue
                                state=_paused_lift(json.loads(row['state_json']))
                                cur=target.execute('INSERT OR IGNORE INTO lift_control VALUES(?,?,?)',(row['device'],_json(state),row['updated_ms']))
                                counts['lift_control']+=cur.rowcount
                        # Actual NOOP Room/GRDB tables have a different schema: retain typed
                        # source rows additively, never pretend its DB is a BOOP native store.
                        mapping = NOOP_TABLE_KINDS
                        for table,kind in mapping.items():
                            if table in tables:
                                for row in source.execute(f'SELECT * FROM "{table}"'):
                                    p = dict(source="noop-backup",source_table=table,original={k:({"encoding":"hex","value":v.hex()} if isinstance(v,bytes) else v) for k,v in dict(row).items()})
                                    if "startTs" in row.keys():
                                        p["start_ms"] = _stamp(row["startTs"])
                                    if "endTs" in row.keys():
                                        p["end_ms"] = _stamp(row["endTs"])
                                    if "day" in row.keys():
                                        p["day"] = row["day"]
                                    if "ts" in row.keys():
                                        p["timestamp_ms"] = _stamp(row["ts"])
                                    key = self._import_key(kind,p)
                                    if not target.execute("SELECT 1 FROM feature_records WHERE import_key=?",(key,)).fetchone():
                                        self._save(target,kind,p,key)
                                        counts["records"] += 1
                        # Existing local settings win. Imported execution opt-ins never land.
                        aliases = {"profile.age":"age","profile.sex":"sex","profile.weightKg":"weight_kg","profile.heightCm":"height_cm","profile.waistCm":"waist_cm","profile.hrMax":"hr_max","profile.hrZoneThresholds":"hr_zone_thresholds","units.system":"units","units.distance":"distance_units","units.temperature":"temperature_units","effort.scale":"effort_scale","dayCycle.mode":"day_cycle_mode","today.hostedCards":"hosted_cards","journal.customBehaviors":"custom_behaviors"}
                        settings = {aliases.get(k,k):v for k,v in settings.items()}
                        for key,value in settings.items():
                            if key in DEFAULT_SETTINGS and key not in BACKUP_ACTION_KEYS:
                                try:
                                    self._validate_settings({key:value})
                                except ValueError:
                                    continue
                                target.execute("INSERT OR IGNORE INTO feature_settings VALUES(?,?)",(key,_json(value)))
                return dict(imported=sum(counts.values()),counts=dict(counts),backup=backup,format="boop-backup" if "frames" in tables else "noop-backup",restore_hooks=['invalidate_analytics'],warnings=["Merge preserves existing ids and settings; NOOP tables are retained as source records, not native strap frames","Execution controls, credentials and schedules are not restored; imported lifting sheets require explicit resume","Restored Coach messages remain readable archives and are not sent as current prompt advice"])
