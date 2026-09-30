"""Local SQLite storage. Sync ACKs may only follow a completed durable commit."""
from __future__ import annotations

import csv
from contextlib import closing
import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path

from whoop_protocol import Reading, STANDARD_HR, decode, plausible_time, sensor_values, standard_hr, waveform, wire

SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
 id INTEGER PRIMARY KEY, device TEXT NOT NULL, received_ms INTEGER NOT NULL,
 characteristic TEXT NOT NULL, packet_type INTEGER NOT NULL, version INTEGER,
 digest TEXT NOT NULL, raw BLOB NOT NULL, UNIQUE(device, digest)
);
CREATE TABLE IF NOT EXISTS readings (
 frame_id INTEGER PRIMARY KEY REFERENCES frames(id), device TEXT NOT NULL,
 kind TEXT NOT NULL, timestamp_ms INTEGER, received_ms INTEGER NOT NULL,
 device_seconds INTEGER NOT NULL, hr INTEGER, rr_json TEXT NOT NULL,
 contact INTEGER, gx REAL, gy REAL, gz REAL, layout INTEGER
);
CREATE INDEX IF NOT EXISTS readings_time ON readings(device, timestamp_ms);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, device TEXT NOT NULL, saved_ms INTEGER NOT NULL,
 end_block BLOB NOT NULL, frames INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sensors (
 frame_id INTEGER PRIMARY KEY REFERENCES frames(id), device TEXT NOT NULL,
 timestamp_ms INTEGER, received_ms INTEGER NOT NULL, source TEXT NOT NULL, values_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sensors_time ON sensors(device,timestamp_ms);
CREATE TABLE IF NOT EXISTS sensor_migrations (version INTEGER PRIMARY KEY, last_frame INTEGER NOT NULL);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def save(self, records: list[tuple[str, int, str, bytes]], end: bytes | None = None) -> int:
        """One fsync-backed transaction includes full raw records and the sync cursor."""
        if end is not None and len(end) != 8:
            raise ValueError("Invalid history cursor")
        if end is not None and (not records or not any(f[2] != STANDARD_HR and len(f[3]) >= 7 and f[3][4] == 49 and f[3][6] == 2 for f in records)):
            raise ValueError("No HISTORY_END in durable chunk")
        added = 0
        conn = self.connect()
        try:
            with conn:
                for device, received, characteristic, frame in records:
                    # The hello body contains key material. Never persist it.
                    is_standard = characteristic == STANDARD_HR
                    if not is_standard and not wire.verify_whoop4_frame(frame):
                        raise ValueError("Corrupt frame cannot enter durable storage or permit an ACK")
                    if not is_standard and frame[4] == 36 and frame[6] == 35:
                        continue
                    digest = hashlib.sha256(frame + (received.to_bytes(8, "little") if is_standard else b"")).hexdigest()
                    cur = conn.execute("INSERT OR IGNORE INTO frames(device, received_ms, characteristic, packet_type, version, digest, raw) VALUES(?,?,?,?,?,?,?)",
                                       (device, received, characteristic, -1 if is_standard else frame[4], None if is_standard else frame[5], digest, frame))
                    if not cur.rowcount:
                        continue
                    added += 1
                    if not is_standard:
                        self.save_sensor(conn, cur.lastrowid, device, received, frame)
                    if is_standard:
                        hr, rr, contact = standard_hr(frame)
                        sample = Reading("live", received // 1000, hr, rr, int(contact) if contact is not None else None)
                    else:
                        sample = decode(frame)
                    if sample is None:
                        continue
                    stamp = (sample.device_seconds * 1000 if plausible_time(sample.device_seconds, received / 1000) else None)
                    if sample.kind == "live":
                        stamp = received  # accurate wall time, while retaining unmodified device time
                    conn.execute("INSERT INTO readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (cur.lastrowid, device, sample.kind, stamp, received, sample.device_seconds,
                                  sample.heart_rate, json.dumps(sample.rr_ms), sample.contact,
                                  sample.gx, sample.gy, sample.gz, sample.layout))
                if end is not None:
                    conn.execute("INSERT INTO chunks(device,saved_ms,end_block,frames) VALUES(?,?,?,?)",
                                 (records[0][0], int(time.time() * 1000), end, len(records)))
            return added
        finally:
            conn.close()

    @staticmethod
    def save_sensor(conn, frame_id, device, received, frame):
        values = sensor_values(frame)
        if values is not None:
            seconds = values.get("device_seconds", 0)
            stamp = seconds * 1000 if plausible_time(seconds, received / 1000) else None
            conn.execute("INSERT OR IGNORE INTO sensors VALUES(?,?,?,?,?,?)",
                         (frame_id, device, stamp, received, values["source"], json.dumps(values, allow_nan=False)))

    def retro_decode_sensors(self):
        conn = self.connect()
        try:
            previous = conn.execute("SELECT last_frame FROM sensor_migrations WHERE version=1").fetchone()
            last = previous[0] if previous else 0
            highest = last
            with conn:
                for row in conn.execute("SELECT id,device,received_ms,raw FROM frames WHERE id>? AND packet_type IN(43,47) ORDER BY id", (last,)):
                    self.save_sensor(conn, row["id"], row["device"], row["received_ms"], row["raw"])
                    highest = row["id"]
                conn.execute("INSERT OR REPLACE INTO sensor_migrations VALUES(1,?)", (highest,))
            return conn.execute("SELECT COUNT(*) FROM sensors").fetchone()[0]
        finally:
            conn.close()

    def sensor_series(self, device=None, hours=24):
        conn = self.connect()
        try:
            start = int((time.time() - min(720, max(.01, hours)) * 3600) * 1000)
            clause = "AND device=?" if device else ""
            rows = conn.execute(f"SELECT timestamp_ms,source,values_json FROM sensors WHERE timestamp_ms>=? {clause} ORDER BY timestamp_ms DESC LIMIT 2000", (start, device) if device else (start,))
            return [{"timestamp_ms": r[0], "source": r[1], **json.loads(r[2])} for r in reversed(rows.fetchall())]
        finally:
            conn.close()

    def latest_waveforms(self, device=None):
        conn = self.connect()
        try:
            clause = "AND device=?" if device else ""
            rows = conn.execute(f"SELECT received_ms,raw FROM frames WHERE packet_type=43 {clause} ORDER BY id DESC LIMIT 8", (device,) if device else ())
            result = {}
            for r in rows:
                values = waveform(r[1])
                if values and values["kind"] not in result:
                    result[values["kind"]] = {"received_ms": r[0], **values}
            return result
        finally:
            conn.close()

    def summary(self, device: str | None = None) -> dict:
        conn = self.connect()
        try:
            where, params = ("WHERE device=?", (device,)) if device else ("", ())
            result = dict(conn.execute(f"SELECT COUNT(*) AS readings, SUM(kind='live') AS live_readings, SUM(kind='history') AS history_readings, SUM(kind='history' AND timestamp_ms IS NULL) AS undated_history, MIN(timestamp_ms) AS first_ms, MAX(timestamp_ms) AS last_ms FROM readings {where}", params).fetchone())
            result["raw_frames"] = conn.execute(f"SELECT COUNT(*) FROM frames {where}", params).fetchone()[0]
            result["bytes"] = sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal")) if p.exists())
            result["database"] = str(self.path)
            return result
        finally:
            conn.close()

    def series(self, device: str | None, hours: float = 1) -> list[dict]:
        start = int((time.time() - hours * 3600) * 1000)
        # Bucket long history so a full day remains responsive in the browser.
        bucket = max(1000, int(hours * 3600000 / 1200))
        conn = self.connect()
        try:
            clause = "AND device=?" if device else ""
            args = (start, device) if device else (start,)
            rows = conn.execute(f"SELECT MIN(timestamp_ms) AS t, ROUND(AVG(hr),1) AS hr, COUNT(*) AS n FROM readings WHERE timestamp_ms>=? AND hr IS NOT NULL {clause} GROUP BY timestamp_ms/{bucket} ORDER BY t", args)
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def days(self, device: str | None) -> list[dict]:
        conn = self.connect()
        try:
            clause = "AND device=?" if device else ""
            rows = conn.execute(f"SELECT date(timestamp_ms/1000,'unixepoch','+10 hours') AS day, COUNT(*) AS readings, ROUND(AVG(hr),1) AS avg_hr, MIN(hr) AS min_hr, MAX(hr) AS max_hr, SUM(kind='history') AS history FROM readings WHERE timestamp_ms IS NOT NULL {clause} GROUP BY day ORDER BY day DESC LIMIT 30", (device,) if device else ())
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def export_csv(self, device: str | None) -> str:
        out = io.StringIO(newline="")
        writer = csv.writer(out)
        writer.writerow(("device", "source", "utc_timestamp", "received_utc", "device_seconds", "heart_rate_bpm", "rr_ms", "skin_contact", "gravity_x", "gravity_y", "gravity_z", "layout"))
        conn = self.connect()
        try:
            clause = "WHERE device=?" if device else ""
            rows = conn.execute(f"SELECT device,kind,strftime('%Y-%m-%dT%H:%M:%fZ',timestamp_ms/1000.0,'unixepoch'),strftime('%Y-%m-%dT%H:%M:%fZ',received_ms/1000.0,'unixepoch'),device_seconds,hr,rr_json,contact,gx,gy,gz,layout FROM readings {clause} ORDER BY COALESCE(timestamp_ms,received_ms)", (device,) if device else ())
            writer.writerows(rows)
            return out.getvalue()
        finally:
            conn.close()

    def backup(self, destination: Path):
        source = self.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()[0]
            if check != "ok":
                raise RuntimeError("Database backup integrity check failed")
        finally:
            target.close()
            source.close()
