import asyncio
from datetime import datetime,timedelta,timezone
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from backup_scheduler import BackupScheduler
from features import FeatureStore


class BackupSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.features=FeatureStore(self.root/'test.sqlite')
        self.features.save_record('journal',{'day':'2026-10-01','notes':'Temporary fixture only'})
        self.service=BackupScheduler(self.features,self.root)
        self.now=datetime(2026,10,1,6,tzinfo=timezone(timedelta(hours=10)))

    def tearDown(self):self.temp.cleanup()

    async def test_default_off_and_strict_typed_config(self):
        config=self.service.config()
        self.assertFalse(config['enabled'])
        self.assertEqual(config['keep_count'],7)
        self.assertEqual(config['keep_options'],[1,3,5,7,10,14])
        self.assertEqual(config['files'],[])
        self.assertFalse((await self.service.tick(self.now))['attempted'])
        for body in ({'enabled':1},{'keep_count':2},{'keep_count':True},{'directory':'relative'},
                     {'directory':'\\\\server\\share'},{'api_key':'secret'}):
            with self.assertRaises(ValueError):self.service.config(body)
        with self.assertRaises(ValueError):await self.service.tick(self.now.replace(tzinfo=None))

    async def test_actual_zip_chosen_folder_persistence_and_local_day(self):
        chosen=self.root/'chosen folder'
        self.service.config({'directory':str(chosen),'enabled':True})
        result=await self.service.tick(self.now)
        self.assertTrue(result['success'])
        path=self.service.lookup(result['name'])
        self.assertEqual(path.parent,chosen)
        with zipfile.ZipFile(path) as archive:
            self.assertIn('boop-backup.sqlite',archive.namelist())
            self.assertIsNone(archive.testzip())
        again=BackupScheduler(self.features,self.root)
        self.assertEqual(again.config()['state']['last_run_day'],'2026-10-01')
        self.assertFalse((await again.tick(self.now+timedelta(hours=15)))['attempted'])
        self.assertTrue((await again.tick(self.now+timedelta(days=4)))['success'])
        self.assertEqual(len(again.owned_files()),2) # current-day catch-up, no fabricated missed days

    async def test_manual_disabled_run_does_not_consume_schedule(self):
        result=await self.service.tick(self.now,run_now=True)
        self.assertTrue(result['success'])
        self.assertFalse(result['scheduled'])
        self.assertNotIn('last_run_day',self.service.config()['state'])
        self.service.config({'enabled':True})
        self.assertTrue((await self.service.tick(self.now))['success'])
        self.assertEqual(len(self.service.owned_files()),2)

    async def test_retention_foreign_files_and_no_arbitrary_lookup(self):
        folder=self.root/'target';folder.mkdir()
        foreign=folder/'manual-backup.boopbak';foreign.write_bytes(b'foreign')
        original=folder/'whoop.sqlite';original.write_bytes(b'original')
        self.service.config({'directory':str(folder),'enabled':True,'keep_count':3})
        for n in range(5):
            self.assertTrue((await self.service.tick(self.now+timedelta(days=n)))['success'])
        self.assertEqual(len(self.service.owned_files()),3)
        self.assertEqual(foreign.read_bytes(),b'foreign')
        self.assertEqual(original.read_bytes(),b'original')
        for name in ('../test.sqlite',str(original),foreign.name,'boop-auto-backup-x.boopbak'):
            self.assertIsNone(self.service.lookup(name))
        self.assertEqual(len(self.service.config()['files']),3)
        self.assertEqual({p.name for p in self.service.owned_files()}, {p['name'] for p in self.service.config()['files']})

    async def test_failure_retry_and_atomic_cleanup(self):
        self.service.config({'enabled':True})
        with patch.object(self.features,'backup_export',side_effect=RuntimeError('key=secret')):
            result=await self.service.tick(self.now)
        self.assertFalse(result['success'])
        self.assertNotIn('secret',str(self.service.config()['state']))
        self.assertNotIn('last_run_day',self.service.config()['state'])
        self.assertFalse((await self.service.tick(self.now+timedelta(minutes=4)))['attempted'])
        with patch('backup_scheduler.os.replace',side_effect=OSError('disk full')):
            result=await self.service.tick(self.now+timedelta(minutes=5))
        self.assertFalse(result['success'])
        self.assertEqual(list(Path(self.service.config()['directory']).iterdir()),[])
        self.assertTrue((await self.service.tick(self.now+timedelta(minutes=10)))['success'])

    async def test_invalid_target_file_and_linked_ancestor(self):
        target=self.root/'existing';target.write_bytes(b'original')
        with self.assertRaises(ValueError):self.service.config({'directory':str(target)})
        self.assertEqual(target.read_bytes(),b'original')
        # Exercise links guard without relying on Windows symlink privileges.
        with patch('backup_scheduler._linked',side_effect=lambda p:p==self.root/'linked'):
            with self.assertRaises(ValueError):self.service.config({'directory':str(self.root/'linked'/'child')})

    async def test_cancellation_waits_for_export_and_removes_stage(self):
        started=threading.Event();release=threading.Event()
        real_export=self.features.backup_export
        def slow(path):
            started.set();release.wait(3);return real_export(path)
        with patch.object(self.features,'backup_export',side_effect=slow):
            task=asyncio.create_task(self.service.tick(self.now,run_now=True))
            await asyncio.to_thread(started.wait,3)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        folder=Path(self.service.config()['directory'])
        self.assertEqual(list(folder.iterdir()),[])
        self.assertNotIn('last_run_day',self.service.config()['state'])

    async def test_disable_while_export_running_and_concurrent_dedup(self):
        self.service.config({'enabled':True})
        original=self.features.backup_export
        def disable(path):
            result=original(path);self.service.config({'enabled':False});return result
        with patch.object(self.features,'backup_export',side_effect=disable):
            result=await self.service.tick(self.now)
        self.assertEqual(result['reason'],'schedule_changed_during_export')
        self.assertEqual(self.service.owned_files(),[])
        self.service.config({'enabled':True})
        results=await asyncio.gather(self.service.tick(self.now),self.service.tick(self.now))
        self.assertEqual(sum(r['attempted'] for r in results),1)

    async def test_real_archive_restores_feature_record(self):
        result=await self.service.tick(self.now,run_now=True)
        destination=FeatureStore(self.root/'restored.sqlite')
        path=self.service.lookup(result['name'])
        destination.backup_restore(path.name,path.read_bytes())
        self.assertEqual(destination.list_records('journal')[0]['notes'],'Temporary fixture only')

    async def test_bad_archive_and_changed_folder_leave_no_drop(self):
        self.service.config({'enabled':True})
        def invalid(path):Path(path).write_bytes(b'not a zip')
        with patch.object(self.features,'backup_export',side_effect=invalid):
            self.assertFalse((await self.service.tick(self.now))['success'])
        first=Path(self.service.config()['directory'])
        self.assertEqual(list(first.iterdir()),[])
        second=self.root/'new target'
        original=self.features.backup_export
        def changed(path):
            original(path);self.service.config({'directory':str(second)})
        with patch.object(self.features,'backup_export',side_effect=changed):
            result=await self.service.tick(self.now+timedelta(minutes=5))
        self.assertEqual(result['reason'],'schedule_changed_during_export')
        self.assertEqual(list(first.iterdir()),[])
        self.assertFalse(second.exists())

    async def test_linked_owned_filename_is_never_listed_or_pruned(self):
        result=await self.service.tick(self.now,run_now=True)
        path=self.service.lookup(result['name'])
        self.service.config({'keep_count':1})
        with patch('backup_scheduler._linked',side_effect=lambda p:p==path):
            self.assertEqual(self.service.owned_files(),[])
            self.assertIsNone(self.service.lookup(path.name))
            self.assertEqual(self.service._prune(str(path.parent),1),0)
        self.assertTrue(path.exists())

if __name__=='__main__':unittest.main()
