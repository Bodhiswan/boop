"""Check the running local app and isolated QA without strap writes or cloud calls.

Start BOOP on8765 and tools/qa_server.py on8766 first. Only the disposable
QA instance receives Coach and diagnostic actions; production is read-only.
"""
import asyncio
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
import time
import zipfile

from aiohttp import ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = 'http://127.0.0.1:8765'
QA = 'http://127.0.0.1:8766'


async def main():
    async with ClientSession(timeout=ClientTimeout(total=150)) as client:
        async def get(base, path):
            async with client.get(base + path) as response:
                response.raise_for_status()
                return await response.json()

        async def post_qa(path, body):
            async with client.post(QA + path, json=body,
                                   headers={'Origin': QA, 'X-Boop': 'local'}) as response:
                response.raise_for_status()
                return await response.json()

        before = await get(PRODUCTION, '/api/status')
        assert before['connected'] and before['clock_verified']
        assert before['invalid_frames'] == 0
        diagnostic_defaults = await get(PRODUCTION, '/api/diagnostics/schedule')
        backup_defaults = await get(PRODUCTION, '/api/backups/schedule')
        brief_defaults = await get(PRODUCTION, '/api/coach/brief')
        assert not diagnostic_defaults['enabled'] and not brief_defaults['enabled']
        assert not backup_defaults['enabled'] and backup_defaults['keep_count'] == 7
        assert not before['device']['automations']['enabled']
        assert not before['device']['automations']['stress_check_in']
        assert not before['device']['automations']['stress_haptics']

        started = time.monotonic()
        daily = await get(PRODUCTION, '/api/day')
        cold_seconds = time.monotonic() - started
        started = time.monotonic()
        warm_daily = await get(PRODUCTION, '/api/day')
        warm_seconds = time.monotonic() - started
        assert daily['day'] == warm_daily['day']
        if daily['charge'].get('coverage', {}).get('baseline_nights', 0) < 4:
            assert daily['charge']['value'] is None

        models = await post_qa('/api/coach/models', {'provider': 'offline'})
        assert models['local'] and 'qwen3:4b-instruct' in models['models']
        history_before = await get(QA, '/api/coach/history')
        started = time.monotonic()
        events = []
        async with client.post(QA + '/api/coach/stream',
                json={'question': 'In one short sentence, explain what R-R intervals measure.',
                      'provider': 'offline', 'save': False},
                headers={'Origin': QA, 'X-Boop': 'local'}) as response:
            response.raise_for_status()
            assert response.headers['Content-Type'].startswith('text/event-stream')
            async for line in response.content:
                if line.startswith(b'data: '):
                    events.append(json.loads(line[6:]))
        coach_seconds = time.monotonic() - started
        assert events[0]['type'] == 'meta' and events[0]['local']
        assert events[-1]['type'] == 'done'
        result = events[-1]['result']
        assert result['local'] and not result['saved']
        delta_count = sum(e['type'] == 'delta' for e in events)
        assert delta_count > 0
        assert ''.join(e['text'] for e in events if e['type'] == 'delta').strip() == result['answer']
        history_after = await get(QA, '/api/coach/history')
        assert history_before == history_after

        qa_before = await get(QA, '/api/status')
        initial_schedule = await get(QA, '/api/diagnostics/schedule')
        assert not initial_schedule['enabled']
        exported = await post_qa('/api/diagnostics/schedule', {'action': 'run'})
        assert exported['result']['success'] and not exported['enabled']
        name = exported['files'][0]['name']
        async with client.get(QA + '/api/diagnostics/export', params={'file': name}) as response:
            response.raise_for_status()
            diagnostic_bytes = await response.read()
        with zipfile.ZipFile(io.BytesIO(diagnostic_bytes)) as archive:
            members = sorted(archive.namelist())
            assert members == ['diagnostics.txt', 'status.json']
            diagnostic_status = json.loads(archive.read('status.json'))
            assert not set(diagnostic_status) & {'hr', 'hrv_quality', 'address', 'name', 'device'}
            assert 'database' not in diagnostic_status['store']
        cleared = await post_qa('/api/diagnostics/schedule', {'action': 'clear', 'confirm': True})
        assert cleared['files'] == [] and cleared['result']['removed'] >= 1
        qa_after = await get(QA, '/api/status')
        assert qa_before['store']['readings'] == qa_after['store']['readings']

        async with client.get(PRODUCTION + '/export/report.pdf?days=30') as response:
            response.raise_for_status()
            report_bytes = await response.read()
        assert report_bytes.startswith(b'%PDF-')
        report_path = ROOT / 'output/pdf/boop-local-report.pdf'
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_bytes(report_bytes)

        after = await get(PRODUCTION, '/api/status')
        assert after['connected'] and after['clock_verified'] and after['invalid_frames'] == 0
        assert after['store']['readings'] > before['store']['readings']
        evidence = {
            'verified_at_utc': datetime.now(timezone.utc).isoformat(),
            'production': {'connected': after['connected'], 'firmware': after['firmware'],
                'clock_verified': after['clock_verified'], 'clock_drift_s': after['clock_drift_s'],
                'invalid_frames': after['invalid_frames'], 'live_hr': after['hr'],
                'readings_before': before['store']['readings'], 'readings_after': after['store']['readings'],
                'recording_awake_held': after['recording_awake_held'],
                'automatic_haptics_enabled': False, 'diagnostic_schedule_enabled': False,
                'backup_schedule_enabled': False, 'backup_retention': backup_defaults['keep_count'],
                'coach_brief_schedule_enabled': False},
            'analytics': {'day': daily['day'], 'first_request_seconds': round(cold_seconds, 3),
                'warm_request_seconds': round(warm_seconds, 3),
                'nightly_hrv_ms': daily['hrv']['value'], 'charge': daily['charge']},
            'isolated_coach': {'provider': result['provider'], 'model': result['model'],
                'local': result['local'], 'delta_count': delta_count,
                'seconds': round(coach_seconds, 3), 'answer': result['answer'],
                'saved': result['saved'], 'history_unchanged': True,
                'available_local_models': models['models']},
            'isolated_diagnostics': {'enabled': False, 'zip_bytes': len(diagnostic_bytes),
                'members': members, 'metadata_keys': sorted(diagnostic_status),
                'cleared_copies': cleared['result']['removed'], 'health_records_unchanged': True},
            'report': {'path': str(report_path), 'bytes': len(report_bytes)},
            'scope': 'Actual local HTTP/model/recording checks; no physical motor, alarm or reboot test.'
        }
        path = ROOT / 'output/qa/final-integration.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
