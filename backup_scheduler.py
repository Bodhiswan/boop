"""Chosen-local-folder Backup & Sync counterpart of NOOP FolderBackup.

Source: Strand/Data/BackupSync.swift and Screens/BackupSyncView.swift, pinned
7f396e98ed9d259df08e3a0a58cfac05fc70615c. PolyForm Noncommercial 1.0.0;
Copyright 2026 NoopApp. Default OFF, keep7, choices1/3/5/7/10/14.
BOOP archive generation remains FeatureStore's responsibility, never masqueraded
as native NOOP. This coordinator has no cloud client, notifications or actuators.
"""
import asyncio
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import re
import uuid
import zipfile

KEEP_OPTIONS = (1,3,5,7,10,14)
CONFIG_KEY = 'backup_sync'
STATE_KEY = 'backup_sync_state'
PREFIX = 'boop-auto-backup-'
NAME = re.compile(r'boop-auto-backup-\d{8}-\d{6}-[0-9a-f]{32}\.boopbak\Z')


def _linked(path):
    return path.is_symlink() or getattr(path,'is_junction',lambda:False)()


def _directory(value,create=False):
    if not isinstance(value,str) or not value.strip() or len(value)>2048 or '\x00' in value:
        raise ValueError('Choose an absolute local backup folder')
    if value.startswith(('\\\\','//')):
        raise ValueError('Choose a local folder; network shares are not a local destination')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('Backup folder must be an absolute local path')
    # Inspect the supplied path before resolving: resolving first would hide links.
    for current in list(reversed(path.parents))+[path]:
        if _linked(current):
            raise ValueError('Backup folders cannot pass through symlinks or junctions')
        if current.exists():
            if not current.is_dir():
                raise ValueError('Backup folder cannot be a file')
        elif create:
            current.mkdir()
    return path.resolve(strict=create)


class BackupScheduler:
    """App-running, timezone-aware daily success/catch-up scheduler.

    Root API owns the explicit chosen-directory form and restore confirmation.
    Only lookup-returned files may be downloaded/restored; filenames never build
    arbitrary paths. OS/cloud-sync behavior of a chosen folder is outside BOOP.
    """
    def __init__(self,features,workspace_root):
        self.features = features
        self.root = Path(workspace_root).resolve(strict=True)
        self.defaults = dict(enabled=False,directory=str(self.root/'data'/'daily-backups'),keep_count=7)
        self.lock = asyncio.Lock()
        with closing(features.store.connect()) as conn,conn:
            conn.execute('CREATE TABLE IF NOT EXISTS boop_control_settings(key TEXT PRIMARY KEY,value_json TEXT NOT NULL)')

    def _read(self,key,default):
        with closing(self.features.store.connect()) as conn:
            row = conn.execute('SELECT value_json FROM boop_control_settings WHERE key=?',(key,)).fetchone()
        return {**default,**(json.loads(row[0]) if row else {})}

    def _save(self,key,value):
        with closing(self.features.store.connect()) as conn,conn:
            conn.execute('INSERT OR REPLACE INTO boop_control_settings VALUES(?,?)',(key,json.dumps(value)))

    def _config(self):
        current = self._read(CONFIG_KEY,self.defaults)
        current['directory'] = str(_directory(current['directory']))
        return current

    def config(self,body=None):
        # A vanished/invalid old destination must not prevent choosing a new one.
        current = self._read(CONFIG_KEY,self.defaults)
        if body is not None:
            if not isinstance(body,dict) or set(body)-set(self.defaults):
                raise ValueError('Unknown backup schedule setting')
            current = {**current,**body}
            if type(current['enabled']) is not bool:
                raise ValueError('Backup switch must be true or false')
            if type(current['keep_count']) is not int or current['keep_count'] not in KEEP_OPTIONS:
                raise ValueError('Keep 1, 3, 5, 7, 10 or 14 backups')
            current['directory'] = str(_directory(current['directory']))
            self._save(CONFIG_KEY,current)
        else:
            current['directory'] = str(_directory(current['directory']))
        return {**current,'keep_options':list(KEEP_OPTIONS),'running_required':True,
                'state':self._read(STATE_KEY,{}),'files':self.files(current['directory']),
                'source':'NOOP FolderBackup local Windows counterpart',
                'note':'Unencrypted local BOOP archives. A separately synced folder can upload them through its sync app.'}

    def owned_files(self,directory=None):
        folder = _directory(directory or self._config()['directory'])
        if not folder.exists():
            return []
        files = []
        for path in folder.iterdir():
            if NAME.fullmatch(path.name) and not _linked(path) and path.is_file() and path.resolve().parent==folder:
                files.append(path)
        # Same-second manual drops are distinct; actual write order breaks timestamp ties.
        return sorted(files,key=lambda p:(p.name[:32],p.stat().st_mtime_ns,p.name))

    def files(self,directory=None):
        return [dict(name=p.name,size_bytes=p.stat().st_size,modified_ms=int(p.stat().st_mtime*1000))
                for p in reversed(self.owned_files(directory))]

    def lookup(self,name):
        if not isinstance(name,str) or not NAME.fullmatch(name):
            return None
        return next((path for path in self.owned_files() if path.name==name),None)

    def due(self,now):
        config = self._config()
        state = self._read(STATE_KEY,{})
        return (config['enabled'] and state.get('last_run_day')!=now.date().isoformat()
                and now.timestamp()>=state.get('retry_after',0))

    def _prune(self,directory,keep):
        removed = 0
        for path in self.owned_files(directory)[:-keep]:
            try:
                # Guard again before touching any file; never delete a linked target.
                folder = _directory(directory)
                if not _linked(path) and path.resolve().parent==folder:
                    path.unlink();removed+=1
            except (OSError,ValueError):
                pass
        return removed

    def _export_stage(self,stage):
        self.features.backup_export(stage)
        with zipfile.ZipFile(stage) as archive:
            if archive.testzip() or 'boop-backup.sqlite' not in archive.namelist():
                raise ValueError('Backup archive could not be verified')
        with stage.open('r+b') as stream:
            os.fsync(stream.fileno())

    async def tick(self,now,run_now=False):
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('Supply timezone-aware local backup time')
        if type(run_now) is not bool:
            raise ValueError('Manual run flag must be true or false')
        async with self.lock:
            stage = None
            try:
                if not run_now and not self.due(now):
                    return dict(attempted=False,success=True,reason='disabled_already_completed_or_retry_pending')
                config = self._config()
                folder = _directory(config['directory'],create=True)
                token = uuid.uuid4().hex
                stage = folder/f'.boop-backup-stage-{token}.tmp'
                task = asyncio.create_task(asyncio.to_thread(self._export_stage,stage))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A Python worker thread cannot be cancelled. Join it before cleanup,
                    # preventing a late stray write after the application closes.
                    try:
                        await task
                    except Exception:
                        pass
                    raise
                current = self._config()
                if current['directory']!=config['directory'] or not run_now and not current['enabled']:
                    return dict(attempted=False,success=True,reason='schedule_changed_during_export')
                if _directory(config['directory'])!=folder:
                    raise ValueError('Chosen backup folder changed')
                destination = folder/f'{PREFIX}{now:%Y%m%d-%H%M%S}-{token}.boopbak'
                os.replace(stage,destination)
                state = self._read(STATE_KEY,{})
                state.update(last_file=destination.name,last_success_at=now.isoformat(),retry_after=0,last_error=None)
                if not run_now:
                    state['last_run_day']=now.date().isoformat()
                self._save(STATE_KEY,state)
                removed = self._prune(config['directory'],current['keep_count'])
                return dict(attempted=True,success=True,name=destination.name,directory=str(folder),removed=removed,scheduled=not run_now)
            except Exception:
                # Callback/OS exception text can contain private paths or credentials.
                state = self._read(STATE_KEY,{})
                state.update(last_error='Local backup failed',retry_after=now.timestamp()+300)
                self._save(STATE_KEY,state)
                return dict(attempted=True,success=False,reason='Local backup failed; retry in 5 minutes')
            finally:
                if stage is not None and not _linked(stage):
                    stage.unlink(missing_ok=True)
