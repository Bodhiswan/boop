import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock
import zlib

from companion import Companion, ProbeTimeout, validate_wind_down, wind_down_times, reboot_probe_state
from features import FeatureStore
from storage import Store
from whoop_protocol import command, config_read_body, diagnostic_probe, enumeration_reply, config_reply, REBOOT_CANDIDATES
from vendor.noop import whoop_frame as wire


def reply(number,payload):
    frame=bytearray(wire.build_command_frame(number,0,payload)); frame[4]=36
    frame[-4:]=(zlib.crc32(frame[4:-4])&0xffffffff).to_bytes(4,'little')
    return bytes(frame)

class ProtocolProbeTests(unittest.TestCase):
    def test_experimental_allowlist_and_fixed_payloads(self):
        for number,payload in REBOOT_CANDIDATES.values():
            with self.assertRaises(ValueError): command(number,0,payload)
            self.assertTrue(wire.verify_whoop4_frame(command(number,0,payload,experimental=True)))
        for number,payload in ((117,b'\x01'),(118,b'\x01'),(121,config_read_body('enable_sig12')),(128,config_read_body('enable_sig12'))):
            with self.assertRaises(ValueError): command(number,0,payload)
            self.assertTrue(wire.verify_whoop4_frame(command(number,0,payload,experimental=True)))
        for number,payload in ((29,b'\x02'),(32,b'\x00'),(117,b''),(121,b'\x01abc'),(119,b'\x01'),(120,b'\x01')):
            with self.assertRaises(ValueError): command(number,0,payload,experimental=True)
        for key in ('','x'*33,'x\x00y','é','a b'):
            with self.assertRaises(ValueError): config_read_body(key)

    def test_real_probe_offsets_and_diffs(self):
        payload=bytes((1,2,3,4,5,6,7,0x10,0x0e))
        got=diagnostic_probe(reply(98,payload),98,'01020304050607110e')
        self.assertEqual(got['pack_voltage_mv'],3600)
        self.assertEqual(got['diff'],[dict(offset=7,before=17,after=16)])
        body=diagnostic_probe(reply(84,bytes((1,6,88,9))),84)
        self.assertEqual(body['body_location'],dict(revision=1,location=6,label='RAW_6',confidence_raw=88,status_raw=9))
        self.assertEqual(diagnostic_probe(reply(98,b''),98)['status'],'stub')

    def test_enum_records_crc_and_config_echo(self):
        self.assertEqual(enumeration_reply(reply(117,b'\x0a\x01\x01\x10\x00'),117)['count'],16)
        item=enumeration_reply(reply(118,b'\x0a\x01\x01\x03\x01enable_sig12\x00'),118)
        self.assertEqual((item['index'],item['key']),(3,'enable_sig12'))
        self.assertEqual(enumeration_reply(reply(118,b'\x0a\x01\x01\xff'),118)['index'],255)
        frame=reply(128,b'\x0a\x01'+b'prefix'+config_read_body('enable_sig12')[1:]+b'2\x00')
        got=config_reply(frame,128,'enable_sig12')
        self.assertEqual((got['value'],got['echo_offset'],got['result_code']),(50,6,None))
        self.assertIsNone(config_reply(frame,128,'different')['value'])
        corrupted=bytearray(frame); corrupted[-1]^=1
        for bad in (bytes(corrupted),reply(121,b'\x0a\x01')):
            with self.assertRaises(ValueError): config_reply(bad,128,'enable_sig12')

    def test_wind_down_previous_weekday_and_validation(self):
        cfg=validate_wind_down(dict(enabled=True,wake_minutes=420,sleep_need_minutes=480,lead_minutes=30,wake_overrides={'0':480}))
        now=datetime.fromisoformat('2026-10-04T22:00:00+10:00') # Sunday; Monday 08:00 -> Sunday 23:30
        self.assertIn(datetime.fromisoformat('2026-10-04T23:30:00+10:00'),wind_down_times(now,cfg))
        for body in ({'enabled':'true'},{'sleep_need_minutes':299},{'lead_minutes':121},{'wake_overrides':{'7':420}},{'wake_minutes':420.5}):
            with self.assertRaises(ValueError): validate_wind_down(body)
        self.assertEqual(wind_down_times(now,validate_wind_down({})),[])
        self.assertEqual(reboot_probe_state(12,True),'no_disconnect')
        self.assertEqual(reboot_probe_state(3,False),'link_dropped')
        self.assertEqual(reboot_probe_state(20,True,True),'reconnected')
        self.assertEqual(reboot_probe_state(60,False,True),'unsettled')


class FakeManager:
    connected=True; bonded=True; _sync_active=False; address='00:11:22:33:44:55'
    def __init__(self): self.calls=[]; self.frames=[]; self.platform=None
    async def send(self,number,payload=b'\x00',**kwargs):
        command(number,0,payload,**kwargs) # Simulated transport enforces the real gate.
        self.calls.append((number,payload,kwargs))
        if self.frames:
            self.companion.on_frame(self.frames.pop(0),int(time.time()*1000),False)

class CompanionProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.features=FeatureStore(Store(Path(self.temp.name)/'test.sqlite'))
        self.manager=FakeManager(); self.companion=Companion(self.manager,self.features)
        self.manager.companion=self.companion
    async def asyncTearDown(self):
        await self.companion.close(); self.temp.cleanup()

    async def test_gates_and_actual_simulated_reply_diff_persistence(self):
        with self.assertRaises(ValueError): await self.companion.device({'action':'feature-flags'})
        with self.assertRaises(ValueError): await self.companion.device({'action':'reboot-probe','experimental':True,'candidate':'reboot29Empty'})
        self.assertEqual(self.manager.calls,[])
        self.manager.frames=[reply(84,b'\x01\x01\x00\x00')]
        got=await self.companion.device({'action':'body-location'})
        self.assertEqual(got['probe']['body_location']['label'],'WRIST')
        self.manager.frames=[reply(84,b'\x01\x02\x00\x00')]
        got=await self.companion.device({'action':'body-location'})
        self.assertEqual(got['probe']['diff'],[dict(offset=1,before=1,after=2)])

    async def test_walk_empty_slots_marker_and_client_bound(self):
        self.manager.frames=[reply(117,b'\x0a\x01\x01\x02\x00'),reply(118,b'\x0a\x01\x01\x01\x00'),reply(118,b'\x0a\x01\x01\x02\x01key\x00'),reply(118,b'\x0a\x01\x01\xff')]
        got=await self.companion.device({'action':'feature-flags','experimental':True})
        self.assertEqual((got['probe']['keys'],got['probe']['stop_code']),(['key'],'endMarker'))
        self.manager.frames=[reply(117,b'\x0a\x01\x01\x00\x00')]+[reply(118,b'\x0a\x01\x01\x01\x01same\x00') for _ in range(128)]
        got=await self.companion.device({'action':'feature-flags','experimental':True})
        self.assertEqual((got['probe']['steps'],got['probe']['stop_code']),(128,'stepCap'))
        self.assertFalse(got['probe']['complete'])

    async def test_timeout_is_silent_and_no_reply_does_not_walk(self):
        with patch.object(self.companion,'request',AsyncMock(side_effect=ProbeTimeout('no reply'))):
            got=await self.companion.device({'action':'feature-flags','experimental':True})
            self.assertEqual(got['probe']['status'],'silent')
            got=await self.companion.device({'action':'config-values','experimental':True})
            self.assertEqual(got['probe']['steps'],2)
            self.assertEqual(got['probe']['verbs'],{'121':'silent','128':'silent'})

    async def test_config_only_reads_and_max_steps(self):
        async def fake(number,payload,**kwargs):
            self.manager.calls.append((number,payload,kwargs))
            return reply(number,b'\x0a\x01'+payload[1:]+b'2')
        with patch.object(self.companion,'request',fake):
            got=await self.companion.device({'action':'config-values','experimental':True,'keys':['key_'+str(i) for i in range(32)]})
        self.assertEqual(got['probe']['steps'],64)
        self.assertEqual({n for n,p,k in self.manager.calls},{121,128})
        self.assertTrue(all(len(p)==33 and k['experimental'] for n,p,k in self.manager.calls))

    async def test_independent_off_by_default_durable_and_quiet(self):
        self.assertFalse(self.companion.automation_settings()['wind_down']['enabled'])
        self.companion.automation_settings({'wind_down':{'enabled':True,'wake_minutes':480,'wake_overrides':{'0':480}}})
        self.assertIsNone(self.companion.alarm_schedule)
        reopened=Companion(self.manager,self.features)
        self.assertTrue(reopened.wind_down['enabled'])
        class Platform:
            def __init__(self): self.calls=[]
            def notify(self,*args): self.calls.append(args)
        self.manager.platform=Platform()
        nudge=datetime.fromisoformat('2026-10-04T23:30:00+10:00')
        await self.companion.check_wind_down(nudge)
        self.assertEqual(self.manager.platform.calls,[]) # Explicit notification default is off.
        self.features.update_settings({'notifications_enabled':True,'quiet_hours_start':'00:00','quiet_hours_end':'00:00'})
        self.companion._independent_wind_down_for=None
        await self.companion.check_wind_down(nudge)
        await self.companion.check_wind_down(nudge)
        self.assertEqual(len(self.manager.platform.calls),1)

    async def test_reboot_gate_and_simulated_watch(self):
        self.manager.bonded=False
        body={'action':'reboot-probe','experimental':True,'confirm':True,'candidate':'reboot29Payload1'}
        with self.assertRaises(ValueError): await self.companion.device(body)
        self.manager.bonded=True
        with patch.object(self.companion,'_watch_reboot',AsyncMock()):
            got=await self.companion.device(body)
            self.assertFalse(got['probe']['verified_reboot'])
            await asyncio.sleep(0)
        self.assertEqual(self.manager.calls[-1],(29,b'\x01',{'experimental':True}))

if __name__=='__main__': unittest.main()
