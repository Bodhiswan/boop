import tempfile
import unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch

from coach import Coach
from coach_history import CoachHistory
from features import FeatureStore
from storage import Store


class CoachHistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.features=FeatureStore(Store(Path(self.folder.name)/'data.sqlite'))
        self.history=CoachHistory(self.features)

    def tearDown(self):
        self.folder.cleanup()

    def test_atomic_pair_order_clear_undo_and_restart(self):
        reply={'answer':'Recorded beats are needed.','provider':'offline','model':'local','local':True}
        self.history.append('What is missing?',reply)
        rows=self.history.history()['messages']
        self.assertEqual([r['role'] for r in rows],['user','assistant'])
        self.assertEqual(CoachHistory(self.features).history()['messages'],rows)
        self.assertEqual(self.history.clear()['messages'],[])
        self.assertTrue(self.history.history()['undo_available'])
        self.assertEqual(self.history.clear(True)['messages'],rows)
        before=self.history.history()['count']
        with self.assertRaises(KeyError):self.history.append('Failed reply',{'provider':'offline'})
        self.assertEqual(self.history.history()['count'],before)

    def test_disabled_schedule_catchup_success_dedup_and_local_only(self):
        now=datetime(2026,10,1,8,0)
        self.assertFalse(self.history.due(now))
        config=self.history.config({'enabled':True,'time_minutes':420})
        self.assertEqual(config['provider'],'offline')
        self.assertTrue(self.history.due(now))
        self.assertFalse(self.history.due(datetime(2026,10,1,6,59)))
        self.history.append('Brief',{'answer':'Still building a baseline.','provider':'offline'},'2026-10-01')
        self.assertFalse(self.history.due(now))
        self.assertTrue(self.history.due(datetime(2026,10,2,8,0)))
        self.history.config({'master_enabled':False})
        self.assertFalse(self.history.due(datetime(2026,10,2,8,0)))
        for body in ({'enabled':1},{'time_minutes':1440},{'time_minutes':2.5},{'api_key':'never-store'},{'provider':'openai'}):
            with self.assertRaises(ValueError):self.history.config(body)

    def test_transcript_cap_keeps_whole_recent_pairs(self):
        for n in range(53):self.history.append(str(n),{'answer':str(n),'provider':'offline'})
        rows=self.history.history()['messages']
        self.assertEqual(len(rows),100)
        self.assertEqual([r['text'] for r in rows[:2]],['3','3'])
        self.assertEqual([r['text'] for r in rows[-2:]],['52','52'])

    def test_context_forward_day_boundary_keeps_transcript_and_restart_pairs(self):
        zone=timezone(timedelta(hours=10))
        before=datetime(2026,10,1,23,59,tzinfo=zone);after=datetime(2026,10,2,0,1,tzinfo=zone)
        reply={'answer':'Earlier answer','provider':'offline'}
        with patch('coach_history.time.time',return_value=before.timestamp()):self.history.append('Earlier question',reply)
        self.assertEqual([r['text'] for r in self.history.context(before)],['Earlier question','Earlier answer'])
        self.assertEqual(self.history.context(after),[])
        self.assertEqual(self.history.history()['count'],2)  # Failed/partial request does not erase it.
        with patch('coach_history.time.time',return_value=after.timestamp()):self.history.append('New question',reply|dict(answer='New answer'))
        restarted=CoachHistory(self.features)
        self.assertEqual([r['text'] for r in restarted.context(after)],['New question','New answer'])
        self.assertEqual(restarted.history()['count'],4)
        restarted.clear();self.assertEqual(restarted.context(after),[])
        restarted.clear(True);self.assertEqual([r['text'] for r in restarted.context(after)],['New question','New answer'])

    def test_context_backward_clock_nil_and_aware_requirement(self):
        zone=timezone(timedelta(hours=10));now=datetime(2026,10,2,10,tzinfo=zone);backward=now-timedelta(days=1)
        self.assertEqual(self.history.context(now),[])
        with self.assertRaisesRegex(ValueError,'aware'):self.history.context(datetime(2026,10,2))
        with patch('coach_history.time.time',return_value=now.timestamp()):self.history.append('one',{'answer':'reply one','provider':'offline'})
        with patch('coach_history.time.time',return_value=backward.timestamp()):self.history.append('two',{'answer':'reply two','provider':'offline'})
        self.assertEqual([r['text'] for r in self.history.context(backward)],['one','reply one','two','reply two'])
        self.assertEqual([r['role'] for r in self.history.context(now,3)],['user','assistant'])

    def test_context_timezone_and_failed_completed_pair_are_not_view_day(self):
        zone=timezone(timedelta(hours=10));now=datetime(2026,10,2,0,1,tzinfo=zone)
        with patch('coach_history.time.time',return_value=now.timestamp()):self.history.append('question',{'answer':'answer','provider':'offline'})
        self.assertEqual(len(self.history.context(now)),2)
        # UTC is still October1; moving the local clock backwards must not retire.
        self.assertEqual(len(self.history.context(now.astimezone(timezone.utc))),2)
        tomorrow=now+timedelta(days=1)
        with patch('coach_history.time.time',return_value=tomorrow.timestamp()),self.assertRaises(KeyError):self.history.append('failed',{'provider':'offline'})
        self.assertEqual(len(self.history.context(now)),2);self.assertEqual(self.history.context(tomorrow),[])
        self.assertEqual(self.history.history()['count'],2)


class GeminiRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_consent_request_header_and_thought_exclusion(self):
        captured={}
        class Response:
            status=200
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def json(self):return {'candidates':[{'content':{'parts':[{'text':'private reasoning','thought':True},{'text':'A recorded answer.'}]}}]}
        class Session:
            def __init__(self,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def post(self,url,**kwargs):captured.update(url=url,**kwargs);return Response()
        body=dict(provider='gemini',question='Explain coverage',api_key='request-only',model='gemini-example')
        with self.assertRaises(ValueError):await Coach().ask(body,{'days':[]})
        body['consent']=True
        with patch('coach.ClientSession',Session):
            result=await Coach().ask(body,{'days':[]},[{'role':'user','text':'Prior question','api_key':'ignored'}])
        self.assertEqual(result['answer'],'A recorded answer.')
        self.assertEqual(captured['url'],'https://generativelanguage.googleapis.com/v1beta/models/gemini-example:generateContent')
        self.assertEqual(captured['headers'],{'x-goog-api-key':'request-only'})
        self.assertNotIn('request-only',str(result))
        self.assertNotIn('ignored',str(captured['json']))


if __name__=='__main__':unittest.main()
