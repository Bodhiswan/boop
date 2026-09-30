"""Verify local downloads and the local-only API without affecting the strap."""
import csv
import io
import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

url = "http://127.0.0.1:8765"
with urllib.request.urlopen(url + "/export/csv") as response:
    rows = list(csv.DictReader(io.StringIO(response.read().decode())))
print("CSV rows:", len(rows), "columns:", list(rows[0]) if rows else [])
with urllib.request.urlopen(url + "/export/sqlite") as response:
    backup = response.read()
path = Path(__file__).parent / "data/verified-download.sqlite"
path.write_bytes(backup)
conn = sqlite3.connect(path)
try:
    print("Downloaded database:", conn.execute("PRAGMA integrity_check").fetchone()[0],
          conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0])
finally:
    conn.close()
request = urllib.request.Request(url + "/api/sync", data=b"{}", headers={
    "Origin": "https://example.com", "X-Boop": "local", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(request)
    raise AssertionError("Foreign-origin action was accepted")
except urllib.error.HTTPError as exc:
    print("Foreign-origin action blocked:", exc.code)
    assert exc.code == 403
