import asyncio
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from pathlib import Path
import io
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from diagnostics_scheduler import DiagnosticsScheduler


class Store:
    def __init__(self,path):self.path=path
    def connect(self):return sqlite3.connect(self.path)


def bundle():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:archive.writestr('diagnostics.txt','Sanitized test log')
    return stream.getvalue()


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.features=SimpleNamespace(store=Store(self.root/'test.sqlite'))
        self.calls=0
        async def callback():self.calls+=1;return bundle()
        self.callback=callback
        self.scheduler=DiagnosticsScheduler(self.features,self.root,callback)
        self.now=datetime(2026,10,1,8,tzinfo=timezone(timedelta(hours=10)))

    def tearDown(self):self.temp.cleanup()

    async def test_disabled_has_no_export_and_config_typed(self):
        self.assertEqual(self.scheduler.config()['keep_options'],[3,7,14,30,60])
        self.assertFalse((await self.scheduler.tick(self.now))['attempted'])
        self.assertFalse(self.scheduler.directory.exists())
        self.assertEqual(self.calls,0)
        for body in [{'enabled':1},{'time_minutes':1440},{'keep_count':0},{'keep_count':True},{'path':'C:/Windows'},{'api_key':'secret'}]:
            with self.assertRaises(ValueError):self.scheduler.config(body)
        with self.assertRaises(ValueError):await self.scheduler.tick(self.now.replace(tzinfo=None))

    async def test_success_day_dedup_catchup_and_persistence(self):
        self.scheduler.config({'enabled':True})
        self.assertFalse((await self.scheduler.tick(self.now.replace(hour=6)))['attempted'])
        first=await self.scheduler.tick(self.now)
        self.assertTrue(first['success'])
        self.assertTrue((self.root/first['path']).exists())
        new=DiagnosticsScheduler(self.features,self.root,self.callback)
        self.assertEqual(new.config()['state']['last_run_day'],'2026-10-01')
        self.assertFalse((await new.tick(self.now.replace(hour=23)))['attempted'])
        self.assertTrue((await new.tick(self.now+timedelta(days=4)))['success'])
        self.assertEqual(self.calls,2) # current-day catch-up, not four retrospective bundles

    async def test_failure_retry_and_no_exception_secret_storage(self):
        self.scheduler.config({'enabled':True})
        async def fail():raise RuntimeError('api_key=private-secret')
        self.scheduler.export_callback=fail
        self.assertFalse((await self.scheduler.tick(self.now))['success'])
        state=self.scheduler.config()['state']
        self.assertNotIn('last_run_day',state)
        self.assertNotIn('secret',str(state))
        self.assertFalse((await self.scheduler.tick(self.now+timedelta(minutes=4)))['attempted'])
        self.scheduler.export_callback=self.callback
        self.assertTrue((await self.scheduler.tick(self.now+timedelta(minutes=5)))['success'])

    async def test_manual_run_does_not_consume_daily_drop_or_require_enable(self):
        manual=await self.scheduler.tick(self.now,run_now=True)
        self.assertTrue(manual['success'])
        self.assertFalse(manual['scheduled'])
        self.assertNotIn('last_run_day',self.scheduler.config()['state'])
        self.scheduler.config({'enabled':True})
        scheduled=await self.scheduler.tick(self.now)
        self.assertTrue(scheduled['success'])
        self.assertNotEqual(manual['path'],scheduled['path'])

    async def test_retention_clear_only_owned_files_originals_untouched(self):
        self.scheduler.config({'enabled':True,'keep_count':3})
        self.scheduler.directory.mkdir(parents=True)
        original=self.scheduler.directory/'whoop.sqlite';original.write_bytes(b'original')
        manual=self.scheduler.directory/'manual-support.zip';manual.write_bytes(bundle())
        for n in range(6):self.assertTrue((await self.scheduler.tick(self.now+timedelta(days=n)))['success'])
        self.assertEqual(len(self.scheduler._owned_files()),3)
        self.assertEqual(original.read_bytes(),b'original')
        self.assertTrue(manual.exists())
        cleared=await self.scheduler.clear_exports()
        self.assertEqual(cleared['removed'],3)
        self.assertTrue(original.exists() and manual.exists())
        self.assertEqual(self.scheduler.config()['state']['last_run_day'],'2026-10-06')

    async def test_disable_during_callback_and_concurrent_daily_tick(self):
        self.scheduler.config({'enabled':True})
        async def disable():self.scheduler.config({'enabled':False});return bundle()
        self.scheduler.export_callback=disable
        result=await self.scheduler.tick(self.now)
        self.assertEqual(result['reason'],'disabled_during_export')
        self.assertEqual(self.scheduler._owned_files(),[])
        self.scheduler.config({'enabled':True});self.scheduler.export_callback=self.callback
        results=await asyncio.gather(self.scheduler.tick(self.now),self.scheduler.tick(self.now))
        self.assertEqual(sum(r['attempted'] for r in results),1)

    async def test_symlink_directory_guard(self):
        outside=tempfile.TemporaryDirectory()
        try:
            try:(self.root/'data').symlink_to(outside.name,target_is_directory=True)
            except OSError:self.skipTest('Symlink creation unavailable on this Windows account')
            self.scheduler.config({'enabled':True})
            result=await self.scheduler.tick(self.now)
            self.assertFalse(result['success'])
            self.assertEqual(self.calls,0)
            self.assertEqual(list(Path(outside.name).iterdir()),[])
        finally:outside.cleanup()

    async def test_invalid_bundle_never_marks_success_or_leaves_partial_file(self):
        self.scheduler.config({'enabled':True})
        async def invalid():return b'not a zip'
        self.scheduler.export_callback=invalid
        self.assertFalse((await self.scheduler.tick(self.now))['success'])
        self.assertFalse(self.scheduler.config()['state'].get('last_run_day'))
        self.assertEqual(list(self.scheduler.directory.iterdir()),[])

    async def test_non_directory_path_and_atomic_failure(self):
        data=self.root/'data';data.write_text('original file')
        self.scheduler.config({'enabled':True})
        self.assertFalse((await self.scheduler.tick(self.now))['success'])
        self.assertEqual(data.read_text(),'original file')
        self.assertEqual(self.calls,0)
        data.unlink()
        with patch('diagnostics_scheduler.os.replace',side_effect=OSError('disk error')):
            result=await self.scheduler.tick(self.now+timedelta(minutes=5))
        self.assertFalse(result['success'])
        self.assertEqual(list(self.scheduler.directory.iterdir()),[])
        self.assertNotIn('last_run_day',self.scheduler.config()['state'])

    async def test_same_second_manual_retention_keeps_latest_file(self):
        self.scheduler.config({'keep_count':1})
        for _ in range(3):
            result=await self.scheduler.tick(self.now,run_now=True)
            self.assertTrue((self.root/result['path']).exists())
        self.assertEqual(len(self.scheduler._owned_files()),1)

if __name__=='__main__':unittest.main()
