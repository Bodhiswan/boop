import asyncio,contextlib,tempfile,time,unittest
from pathlib import Path
from unittest.mock import AsyncMock,patch
from boop import Manager
from storage import Store
from tests.test_local import frame
from whoop_protocol import NOTIFY,STANDARD_HR

class RecordingContinuationTests(unittest.IsolatedAsyncioTestCase):
 async def test_post_sync_failures_do_not_stop_live_writes(self):
  for failure in ['backup','rearm']:
   with self.subTest(failure=failure),tempfile.TemporaryDirectory() as folder:
    store=Store(Path(folder)/'test.sqlite');manager=Manager(store)
    manager.address='strap';manager._generation=manager._sync_generation=1;manager._sync_active=True
    manager.client=type('Client',(),{'is_connected':True})()
    manager.arm_live=AsyncMock(side_effect=RuntimeError('connection interrupted') if failure=='rearm' else None)
    with patch.object(store,'backup',side_effect=RuntimeError('backup interrupted') if failure=='backup' else None):
     worker=asyncio.create_task(manager.persist_worker())
     try:
      now=int(time.time()*1000)
      manager.queue.put_nowait((1,('strap',now,NOTIFY[2],frame(bytes((49,0,3))))))
      manager.queue.put_nowait((1,('strap',now+1,STANDARD_HR,bytes((0,72)))))
      await asyncio.wait_for(manager.queue.join(),3)
      for _ in range(20):
       if store.summary()['live_readings']==1:break
       await asyncio.sleep(.1)
      self.assertFalse(worker.done());self.assertEqual(store.summary()['live_readings'],1)
      self.assertEqual(store.series('strap',hours=1)[0]['hr'],72)
     finally:
      worker.cancel()
      with contextlib.suppress(asyncio.CancelledError):await worker
