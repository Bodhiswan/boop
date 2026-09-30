"""Count-only live export audit; restore exclusively into temporary offline stores.

No manager, Bluetooth, platform callbacks, remote service, or live restore is used.
The downloaded health archive and all extracted databases disappear on exit.
"""
from contextlib import closing
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from urllib.request import urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from features import BACKUP_ACTION_KEYS, BACKUP_TABLES, FeatureStore, MAX_BACKUP_BYTES
from storage import Store

OUTPUT = ROOT / 'output/qa/backup-roundtrip.json'
BASE = 'http://127.0.0.1:8765'
COMPARE = {
    'frames': 'device,received_ms,characteristic,packet_type,version,digest,raw',
    'chunks': 'device,saved_ms,end_block,frames',
    'feature_records': 'id,kind,payload_json,created_ms,updated_ms,deleted_ms,import_key',
    'feature_revisions': 'record_id,kind,payload_json,deleted_ms,saved_ms',
    'feature_imports': 'digest,filename,imported_ms,count',
    'boop_devices': 'address,name,forgotten',
    'boop_coach_messages': 'id,role,text,provider,created_ms,deleted_ms',
    'workout_dismissals': 'device,start_ms,end_ms,operation_id',
    'workout_edits': 'id,device,day,before_json,after_json,created_ms',
}


def counts(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {t: conn.execute('SELECT COUNT(*) FROM "'+t+'"').fetchone()[0]
            for t in sorted(BACKUP_TABLES & names)}


def get(path):
    with urlopen(BASE + path, timeout=300) as response:
        data = bytearray()
        while chunk := response.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > MAX_BACKUP_BYTES:
                raise ValueError('Oversized response')
        return bytes(data)


def connection_state():
    try:
        value = json.loads(get('/api/status'))
        return {'available': True, 'connected': bool(value.get('connected'))}
    except Exception as error:
        return {'available': False, 'error_class': type(error).__name__}


def main():
    started = time.monotonic()
    report = {'started_utc': datetime.now(timezone.utc).isoformat(),
              'production_restore_performed': False, 'hardware_actions_performed': False,
              'raw_archive_retained_in_output': False, 'checks': {}, 'passed': False}
    stage = 'download'
    try:
        report['connection_before'] = connection_state()
        archive_data = get('/export/backup')
        report['archive_bytes'] = len(archive_data)
        print('Downloaded local backup; inspecting sanitized tables.', flush=True)
        with tempfile.TemporaryDirectory(prefix='boop-backup-audit-') as folder:
            folder = Path(folder)
            with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
                assert archive.testzip() is None
                db = folder / 'archive.sqlite'
                db.write_bytes(archive.read('boop-backup.sqlite'))
                settings = json.loads(archive.read('settings.json'))
                manifest = json.loads(archive.read('manifest.json'))
                assert manifest['format'] == 'boop-backup'
            stage = 'archive_sanitization'
            with closing(sqlite3.connect(db)) as source:
                report['archive_counts'] = counts(source)
                assert source.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                assert source.execute('PRAGMA foreign_key_check').fetchall() == []
                assert report['archive_counts'].get('boop_control_settings', 0) == 0
                keys = {r[0] for r in source.execute('SELECT key FROM feature_settings')}
                assert not (keys | set(settings)) & BACKUP_ACTION_KEYS
                assert {r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")} <= BACKUP_TABLES | {'sqlite_sequence'}
            report['checks']['archive_sanitized'] = True
            stage = 'first_restore'
            features = FeatureStore(Store(folder / 'restored.sqlite'))
            features.backup_restore('live.boopbak', archive_data)
            print('Offline restore complete; checking original row identities and child references.', flush=True)
            with closing(features.store.connect()) as target:
                target.execute('ATTACH DATABASE ? AS original', (str(db),))
                report['restored_counts'] = counts(target)
                assert target.execute('PRAGMA foreign_key_check').fetchall() == []
                assert target.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
                for table, columns in COMPARE.items():
                    if table not in report['archive_counts']:
                        continue
                    stage = 'compare_' + table
                    missing = target.execute('SELECT COUNT(*) FROM (SELECT '+columns+' FROM original.'+table+' EXCEPT SELECT '+columns+' FROM main.'+table+')').fetchone()[0]
                    assert missing == 0, table
                # Native child IDs may change: compare via immutable frame identity.
                target.create_function('normalized_json', 1, lambda value: json.dumps(json.loads(value), sort_keys=True, separators=(',', ':')))
                for table in ('readings', 'sensors'):
                    if table not in report['archive_counts']:
                        continue
                    stage = 'compare_' + table
                    columns = [r[1] for r in target.execute('PRAGMA original.table_info('+table+')') if r[1] != 'frame_id']
                    projection = ','.join('normalized_json(c."'+column+'")' if column in ('values_json','rr_json') else 'c."'+column+'"' for column in columns)
                    query = lambda schema: 'SELECT f.device,f.digest,'+projection+' FROM '+schema+'.'+table+' c JOIN '+schema+'.frames f ON f.id=c.frame_id'
                    assert target.execute('SELECT COUNT(*) FROM ('+query('original')+' EXCEPT '+query('main')+')').fetchone()[0] == 0
                report['remapped_frame_ids'] = target.execute('SELECT COUNT(*) FROM original.frames a JOIN main.frames b ON a.device=b.device AND a.digest=b.digest WHERE a.id<>b.id').fetchone()[0]
                assert target.execute('SELECT COUNT(*) FROM boop_coach_messages WHERE context_eligible<>0').fetchone()[0] == 0
                sheets = [json.loads(r[0]) for r in target.execute('SELECT state_json FROM lift_control WHERE state_json IS NOT NULL')]
                assert all(s.get('paused') and s.get('restarted') and s.get('instance') == 'backup-restored' for s in sheets)
                assert target.execute('SELECT COUNT(*) FROM boop_control_settings').fetchone()[0] == 0
                # Compare every restored row after a second merge, excluding rowids.
                before = {t: target.execute('SELECT * FROM "'+t+'" ORDER BY rowid').fetchall() for t in report['restored_counts']}
            report['checks'].update(original_rows_preserved=True, frame_child_mapping_integrity=True,
                                    restored_coach_archived_only=True, restored_lift_paused=True,
                                    controls_not_imported=True)
            stage = 'repeat_restore'
            features.backup_restore('live.boopbak', archive_data)
            with closing(features.store.connect()) as target:
                report['repeat_counts'] = counts(target)
                assert report['repeat_counts'] == report['restored_counts']
                assert all(before[t] == target.execute('SELECT * FROM "'+t+'" ORDER BY rowid').fetchall() for t in before)
            report['checks']['repeat_restore_idempotent'] = True
            report['coverage'] = {
                'nonempty_coach_transcript': report['archive_counts'].get('boop_coach_messages', 0) > 0,
                'nonempty_live_lift_sheet': report['archive_counts'].get('lift_control', 0) > 0,
                'nonempty_workout_edit_history': report['archive_counts'].get('workout_edits', 0) > 0,
                'numerical_frame_id_remapping_observed': report['remapped_frame_ids'] > 0,
            }
            report['intentional_exclusions'] = ['Execution control rows and action settings',
                                                'Local sensor migration frame-ID cursor is rederived']
        report['passed'] = True
    except Exception as error:
        # Do not print exception text: malformed values could contain private data.
        report['failure'] = {'stage': stage, 'error_class': type(error).__name__}
    finally:
        report['connection_after'] = connection_state()
        report['elapsed_seconds'] = round(time.monotonic()-started, 2)
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
