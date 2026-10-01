import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from storage import Store


class SleepSeriesTests(unittest.TestCase):
    def test_sleep_window_excludes_other_times_and_other_devices(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'test.sqlite')
            with closing(store.connect()) as conn:
                with conn:
                    for i,(device,t,hr) in enumerate([('a',1000,50),('a',6000,56),('a',16000,60),('a',30000,80),('b',6000,99)],1):
                        conn.execute('INSERT INTO frames(id,device,received_ms,characteristic,packet_type,digest,raw) VALUES(?,?,?, ?,?,?,?)',
                            (i,device,t,'test',0,str(i),b''))
                        conn.execute('INSERT INTO readings(frame_id,device,kind,timestamp_ms,received_ms,device_seconds,hr,rr_json) VALUES(?,?,?,?,?,?,?,?)',
                            (i,device,'live',t,t,0,hr,'[]'))
            points=store.series('a',start_ms=5000,end_ms=20000)
            self.assertEqual([(p['t'],p['hr']) for p in points],[(6000,56),(16000,60)])
