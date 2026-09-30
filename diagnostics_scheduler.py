"""Local Windows counterpart of reachable NOOP ScheduledDebugExport.

Pinned source: Strand/System/ScheduledDebugExport.swift @ 7f396e98; TestCentreView.
PolyForm Noncommercial 1.0.0; Copyright 2026 NoopApp.
Source daily/local-time dedup, default-off07:00, retention14 and manual run/clear.
BOOP deliberately reuses its sanitized support ZIP callback, not raw DB/captures.
No network, notifications, hardware actions, or user-selected filesystem paths.
"""
import asyncio
from contextlib import closing
from datetime import datetime
import json
import io
import os
from pathlib import Path
import re
import uuid
import zipfile

DEFAULT_CONFIG = dict(enabled=False,time_minutes=420,keep_count=14)
KEEP_OPTIONS = (3,7,14,30,60)
MAX_BUNDLE_BYTES = 128*1024*1024
CONFIG_KEY = "diagnostics_export"
STATE_KEY = "diagnostics_export_state"
NAME = re.compile(r"boop-diagnostics-\d{8}-\d{6}-[0-9a-f]{32}\.zip\Z")


class DiagnosticsScheduler:
    """One process coordinator; callers supply timezone-aware local ``now``.

    export_callback is an async no-argument function returning sanitized ZIP bytes.
    It must share the interactive diagnostics builder's secret/action exclusions.
    Control state is separate from imported preferences, so restore cannot opt in.
    """
    def __init__(self,features,workspace_root,export_callback):
        self.features = features
        self.root = Path(workspace_root).resolve(strict=True)
        self.directory = self.root/"data"/"diagnostics"/"backups"
        self.export_callback = export_callback
        self.lock = asyncio.Lock()
        with closing(features.store.connect()) as conn,conn:
            conn.execute("CREATE TABLE IF NOT EXISTS boop_control_settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)")

    def _read(self,key,default):
        with closing(self.features.store.connect()) as conn:
            row = conn.execute("SELECT value_json FROM boop_control_settings WHERE key=?",(key,)).fetchone()
        return {**default,**(json.loads(row[0]) if row else {})}

    def _save(self,key,value):
        with closing(self.features.store.connect()) as conn,conn:
            conn.execute("INSERT OR REPLACE INTO boop_control_settings VALUES(?,?)",(key,json.dumps(value)))

    def config(self,body=None):
        current = self._read(CONFIG_KEY,DEFAULT_CONFIG)
        if body is not None:
            if not isinstance(body,dict) or set(body)-set(DEFAULT_CONFIG):
                raise ValueError("Unknown diagnostic schedule setting")
            current = {**current,**body}
            if type(current['enabled']) is not bool:
                raise ValueError("Diagnostic export switch must be true or false")
            if type(current['time_minutes']) is not int or not 0<=current['time_minutes']<1440:
                raise ValueError("Diagnostic time must be a minute from00:00 through23:59")
            if type(current['keep_count']) is not int or not 1<=current['keep_count']<=100:
                raise ValueError("Keep between1 and100 diagnostic generations")
            self._save(CONFIG_KEY,current)
        return {**current,'keep_options':list(KEEP_OPTIONS),'directory':'data/diagnostics/backups',
                'running_required':True,'state':self._read(STATE_KEY,{}),
                'source':'NOOP ScheduledDebugExport local Windows counterpart',
                'note':'Local sanitized support ZIP; no upload or automatic notification.'}

    def due(self,now):
        config = self.config()
        state = config['state']
        return (config['enabled'] and now.hour*60+now.minute>=config['time_minutes']
                and state.get('last_run_day')!=now.date().isoformat()
                and now.timestamp()>=state.get('retry_after',0))

    def _guard_directory(self,create=False):
        current = self.root
        for part in ('data','diagnostics','backups'):
            current = current/part
            if current.is_symlink() or getattr(current,'is_junction',lambda:False)():
                raise ValueError("Diagnostic directory cannot be a symlink or junction")
            if current.exists():
                if not current.is_dir() or not current.resolve().is_relative_to(self.root):
                    raise ValueError("Diagnostic directory must stay within the workspace")
            elif create:
                current.mkdir()
            else:
                return False
        return True

    def _owned_files(self):
        if not self._guard_directory():
            return []
        return sorted((p for p in self.directory.iterdir() if NAME.fullmatch(p.name) and p.is_file()
                       and not p.is_symlink() and not getattr(p,'is_junction',lambda:False)()),
                      key=lambda p:(p.name[:33],p.stat().st_mtime_ns,p.name))

    def _prune(self,keep):
        removed = 0
        for path in self._owned_files()[:-keep]:
            try:
                path.unlink(); removed+=1
            except OSError:
                pass # Source retention is best-effort; a successful drop stays successful.
        return removed

    def _write(self,payload,now):
        self._guard_directory(create=True)
        destination = self.directory/f"boop-diagnostics-{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex}.zip"
        temporary = destination.with_suffix('.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(payload);stream.flush();os.fsync(stream.fileno())
            self._guard_directory()
            os.replace(temporary,destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    async def tick(self,now,run_now=False):
        """Scheduled catch-up or explicitly requested manual run; no physical effects.

        A manual run does not consume today's scheduled drop, matching NOOP.
        Export failures retain last-success day and retry after five minutes.
        """
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Supply timezone-aware local diagnostic time")
        if type(run_now) is not bool:
            raise ValueError("Manual run flag must be true or false")
        async with self.lock:
            if not run_now and not self.due(now):
                return dict(attempted=False,success=True,reason='disabled_not_due_or_already_completed')
            try:
                self._guard_directory(create=True)
                payload = await self.export_callback()
                if not isinstance(payload,bytes) or not 0<len(payload)<=MAX_BUNDLE_BYTES:
                    raise ValueError("Diagnostic callback must return bounded nonempty ZIP bytes")
                if not zipfile.is_zipfile(io.BytesIO(payload)):
                    raise ValueError("Diagnostic callback must return a ZIP bundle")
                # A user switching off while the callback runs cancels a pending scheduled drop.
                if not run_now and not self.config()['enabled']:
                    return dict(attempted=False,success=True,reason='disabled_during_export')
                destination = self._write(payload,now)
                state = self._read(STATE_KEY,{})
                state.update(last_file=destination.name,last_success_at=now.isoformat(),retry_after=0,last_error=None)
                if not run_now:
                    state['last_run_day']=now.date().isoformat()
                self._save(STATE_KEY,state)
                removed = self._prune(self.config()['keep_count'])
                return dict(attempted=True,success=True,path=str(destination.relative_to(self.root)),removed=removed,scheduled=not run_now)
            except Exception:
                # Do not persist arbitrary exception text: callbacks can include credentials.
                state = self._read(STATE_KEY,{})
                state.update(last_error='Local diagnostic export failed',retry_after=now.timestamp()+300)
                self._save(STATE_KEY,state)
                return dict(attempted=True,success=False,reason='Local diagnostic export failed; retry in5minutes')

    async def clear_exports(self):
        """Explicit user clear; only scheduler-named ZIPs in the fixed directory."""
        async with self.lock:
            removed = 0
            for path in self._owned_files():
                try:
                    path.unlink();removed+=1
                except OSError:
                    pass
            # Leave success-day dedup intact; clear is not a request to regenerate.
            return dict(removed=removed,directory='data/diagnostics/backups')
