import asyncio
from contextlib import closing
import json
import sqlite3
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from boop import Manager
from storage import Store
from whoop_protocol import Assembler, NOTIFY, STANDARD_HR, battery, command, decode, history_end, wire


def frame(inner):
    size = (len(inner) + 4).to_bytes(2, "little")
    return b"\xaa" + size + bytes((wire.crc8(size),)) + inner + wire.crc32(inner).to_bytes(4, "little")


def historical(seconds=None, version=24, hr=68):
    raw = bytearray(84)
    raw[4], raw[5] = 47, version
    struct.pack_into("<I", raw, 11, seconds if seconds is not None else int(time.time()))
    raw[21:23] = bytes((hr, 3))
    struct.pack_into("<3H", raw, 23, 820, 850, 835)
    struct.pack_into("<fff", raw, 40, 0, 0, 1)
    raw[55] = 1
    return frame(raw[4:])


def end_frame():
    raw = bytearray(25)
    raw[4], raw[6] = 49, 2
    raw[17:25] = bytes.fromhex("123456789abcdef0")
    return frame(raw[4:])


class ProtocolTests(unittest.TestCase):
    def test_fragmented_real_measurement_reassembles(self):
        body = bytes((40, 9)) + int(time.time()).to_bytes(4, "little") + b"\0\0" + bytes((73, 2)) + struct.pack("<2H", 800, 850)
        packet = frame(body)
        for split in range(1, len(packet)):
            reader = Assembler()
            self.assertEqual(reader.feed(packet[:split]), [])
            self.assertEqual(reader.feed(packet[split:]), [packet])
        sample = decode(packet)
        self.assertEqual(sample.heart_rate, 73)
        self.assertEqual(sample.rr_ms, [800, 850])

    def test_crc_fault_does_not_decode_or_stall_next_packet(self):
        good = historical()
        bad = bytearray(good)
        bad[21] ^= 1
        reader = Assembler()
        self.assertEqual(reader.feed(bytes(bad) + good), [good])
        self.assertGreater(reader.errors, 0)
        self.assertIsNone(decode(bytes(bad)))

    def test_impossible_length_resynchronises(self):
        packet = historical()
        reader = Assembler()
        self.assertEqual(reader.feed(b"\xaa\xff\xff\x00" + packet), [packet])

    def test_observed_transport_suffix_is_not_a_crc_fault(self):
        for size, suffix in ((1932, bytes(4)), (1928, bytes.fromhex("20b1957b00000000")),
                             (1928, bytes.fromhex("aa00000000000000"))):
            packet = frame(bytes((43,)) + bytes(size - 9))
            next_packet = historical()
            burst = packet + suffix + next_packet
            for split in (244, len(packet), len(packet) + 2):
                reader = Assembler()
                actual = reader.feed(burst[:split]) + reader.feed(burst[split:])
                self.assertEqual(actual, [packet, next_packet])
                self.assertEqual(reader.errors, 0)
        reader = Assembler()
        reader.feed(historical() + b"\x11\x12\x13\x14")
        self.assertGreater(reader.errors, 0)

    def test_historical_battery_event_does_not_replace_current_charge(self):
        current = frame(bytes((36, 0, 26, 0, 1)) + (684).to_bytes(2, "little"))
        self.assertEqual(battery(current), 68.4)
        old_event = bytes.fromhex("aa2400fa3036030026b76c68506a14000214000000e30c000000002701000011020000000c02a2a7")
        self.assertTrue(wire.verify_whoop4_frame(old_event))
        self.assertIsNone(battery(old_event))

    def test_crc_bytes_cannot_become_ack_cursor(self):
        short = bytearray(23)
        short[4], short[6] = 49, 2
        self.assertIsNone(history_end(frame(short[4:])))
        self.assertEqual(history_end(end_frame()), bytes.fromhex("123456789abcdef0"))

    def test_unknown_layout_never_inherits_hr_offsets(self):
        self.assertIsNone(decode(historical(version=99)))
        known = decode(historical())
        self.assertEqual((known.heart_rate, known.gz), (68, 1.0))

    def test_rr_count_does_not_read_optical_or_crc_bytes(self):
        inner = bytearray(historical()[4:-4])
        inner[22 - 4] = 6  # historical v24 has at most five slots
        self.assertEqual(decode(frame(inner)).rr_ms, [])

    def test_device_write_allowlist(self):
        for unsafe in (29, 32, 40, 54, 120, 151):
            with self.assertRaises(ValueError):
                command(unsafe, 0)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "whoop.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def records(self, packet, device="strap-a", received=None):
        return [(device, received or int(time.time() * 1000), NOTIFY[2], packet)]

    def test_durable_copy_and_device_scoped_dedup(self):
        packet = historical()
        self.store.save(self.records(packet))
        self.store.save(self.records(packet))
        self.store.save(self.records(packet, "strap-b"))
        with closing(sqlite3.connect(self.store.path)) as independent:
            self.assertEqual(independent.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 2)
            self.assertEqual(independent.execute("SELECT raw FROM frames LIMIT 1").fetchone()[0], packet)

    def test_invalid_old_dates_retained_without_fabrication(self):
        self.store.save(self.records(historical(seconds=31500000)))
        self.assertEqual(self.store.summary("strap-a")["undated_history"], 1)
        self.assertEqual(self.store.days("strap-a"), [])
        self.assertIn("31500000", self.store.export_csv("strap-a"))

    def test_unknown_raw_layout_survives_backup(self):
        packet = historical(version=99)
        self.store.save(self.records(packet))
        backup = Path(self.tmp.name) / "backup.sqlite"
        self.store.backup(backup)
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT raw FROM frames").fetchone()[0], packet)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)

    def test_secret_bearing_hello_is_not_persisted(self):
        self.store.save(self.records(frame(bytes((36, 0, 35, 0, 1)) + bytes(131))))
        self.assertEqual(self.store.summary()["raw_frames"], 0)

    def test_standard_source_keeps_original_bytes_and_repeated_hr(self):
        now = int(time.time() * 1000)
        data = bytes((0, 70))
        self.store.save([("strap-a", now, STANDARD_HR, data), ("strap-a", now + 1000, STANDARD_HR, data)])
        self.assertEqual(self.store.summary()["readings"], 2)
        with closing(sqlite3.connect(self.store.path)) as conn:
            self.assertEqual(conn.execute("SELECT raw FROM frames LIMIT 1").fetchone()[0], data)


class DurableAckTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "whoop.sqlite")
        self.manager = Manager(self.store)
        self.manager.address = "strap-a"
        self.manager._generation = self.manager._sync_generation = 1
        self.manager._sync_active = True
        self.patcher = patch("boop.DATA", self.root)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    async def test_entire_large_chunk_committed_and_archived_before_ack(self):
        packets = [historical(seconds=int(time.time()) - i) for i in range(270)] + [end_frame()]
        records = [("strap-a", int(time.time() * 1000), NOTIFY[2], p) for p in packets]
        checks = []
        store, root = self.store, self.root
        class Client:
            is_connected = True
            async def write_gatt_char(self, uuid, packet, response):
                with closing(sqlite3.connect(store.path)) as disk:
                    checks.append(disk.execute("SELECT COUNT(*) FROM frames").fetchone()[0])
                    self_outer.assertEqual(disk.execute("PRAGMA synchronous").fetchone()[0], 2)
                archive = (root / "history-archive.jsonl").read_text().splitlines()
                self_outer.assertEqual(len(archive), 272)
                self_outer.assertEqual(packet[6], 23)
        self_outer = self
        self.manager.client = Client()
        worker = asyncio.create_task(self.manager.persist_worker())
        try:
            for record in records:
                self.manager.queue.put_nowait((1, record))
            await asyncio.wait_for(self.manager.queue.join(), 10)
        finally:
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await worker
        self.assertEqual(checks, [271])

    async def test_disk_failure_withholds_ack(self):
        calls = []
        class Client:
            is_connected = True
            async def write_gatt_char(self, *args, **kwargs):
                calls.append(args)
        self.manager.client = Client()
        records = [("strap-a", int(time.time()*1000), NOTIFY[2], end_frame())]
        with patch.object(self.store, "save", side_effect=OSError("disk full")):
            self.assertFalse(await self.manager.commit(records, 1, history_end(end_frame())))
        self.assertEqual(calls, [])
        self.assertIn("disk full", self.manager.error)

    async def test_integrity_fault_or_stale_session_cannot_ack(self):
        calls = []
        class Client:
            is_connected = True
            async def write_gatt_char(self, *args, **kwargs):
                calls.append(args)
        self.manager.client = Client()
        records = [("strap-a", int(time.time()*1000), NOTIFY[2], end_frame())]
        self.manager._sync_tainted = True
        await self.manager.commit(records, 1, history_end(end_frame()))
        self.manager._sync_tainted = False
        await self.manager.commit(records, 0, history_end(end_frame()))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
