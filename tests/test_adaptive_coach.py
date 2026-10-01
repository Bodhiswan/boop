"""Exercise planning, scope expansion and reuse without paid API calls."""
import json
import unittest
from unittest.mock import patch

from adaptive_coach import AdaptiveCoach,validate_plan,needed_topics
from coach import Coach
from tests.test_coach_stream import Response,Session

BODY={'question':'How was my sleep?','provider':'compatible','model':'z-ai/glm-5.3',
      'endpoint':'https://openrouter.ai/api/v1','api_key':'test-only','consent':True,'save':False}


class FakeCoach(Coach):
    def __init__(self):super().__init__();self.requests=[];self.fail=False
    async def stream(self,body,bundle):
        self.requests.append(self._request(body,bundle))
        if self.fail:raise ValueError('unfinished')
        yield {'type':'delta','text':'An answer.'}
        yield {'type':'done','result':{'answer':'An answer.','saved':False}}


class AdaptiveTests(unittest.IsolatedAsyncioTestCase):
    def test_scope_requires_date_and_range_coverage(self):
        cached=[{'topics':['sleep'],'days':7,'through':'2026-10-01'}]
        plan={'topics':['sleep','effort'],'days':7,'through':'2026-10-01','refresh':False}
        self.assertEqual(needed_topics(plan,cached),['effort'])
        self.assertEqual(needed_topics(plan|{'days':30},cached),['sleep','effort'])
        self.assertEqual(needed_topics(plan|{'through':'2026-09-30'},cached),['sleep','effort'])
        self.assertEqual(needed_topics(plan|{'refresh':True},cached),['sleep','effort'])

    def test_plan_cannot_request_private_sources_or_unbounded_history(self):
        valid={'topics':['sleep'],'days':7,'through':'2026-10-01','refresh':False}
        self.assertEqual(validate_plan(valid),valid)
        for change in ({'topics':['api_key']},{'days':366},{'days':True},{'through':'bad'},{'refresh':'yes'}):
            with self.assertRaises(ValueError):validate_plan(valid|change)

    async def test_model_planner_wire_shape_and_fragmented_json(self):
        coach=Coach();service=AdaptiveCoach(coach,None,None)
        plan={'topics':['sleep'],'days':7,'through':'2026-10-01','refresh':False}
        response=Response(json.dumps({'choices':[{'finish_reason':'stop','message':{'content':json.dumps(plan)}}]}).encode())
        captured={};session=Session(response,captured)
        with patch('adaptive_coach.ClientSession',lambda **kwargs:session):
            result=await service.plan(BODY,{'context':[],'conversation':[]},'2026-10-01')
        self.assertEqual(result,plan)
        self.assertEqual(captured['json']['response_format'],{'type':'json_object'})
        content=captured['json']['messages'][1]['content']
        self.assertIn('available_context',content);self.assertNotIn('test-only',content)
        self.assertNotIn('recent_days_oldest_first',content)
        self.assertTrue(response.closed and session.closed)

    async def test_followup_reuses_and_new_scope_only_fetches_new_topics(self):
        coach=FakeCoach();loads=[]
        async def load(days,through):
            loads.append((days,through))
            return {'days':[{'day':through,'rest':{'value':80},'effort':{'value':24},'sleep':{'main':{'total_sleep_min':460}}}]}
        async def live():return {'hr_bpm':65}
        service=AdaptiveCoach(coach,load,live)
        plans=iter([['sleep'],['sleep'],['sleep','effort']])
        async def plan(*args):return {'topics':next(plans),'days':7,'through':'2026-10-01','refresh':False}
        service.plan=plan
        first=[e async for e in service.stream(BODY,'device','2026-10-01')]
        token=first[-1]['result']['context_session']
        second=[e async for e in service.stream(BODY|{'question':'What does that mean?','context_session':token},'device','2026-10-01')]
        self.assertEqual(len(loads),1)
        self.assertEqual(second[-1]['result']['context_status'],'reused existing context')
        third=[e async for e in service.stream(BODY|{'question':'How does my effort compare?','context_session':token},'device','2026-10-01')]
        self.assertEqual(len(loads),2)
        self.assertEqual(third[-1]['result']['context_status'],'added effort')
        messages=coach.requests[-1]['payload']['messages']
        observations=[m for m in messages if m['content'].startswith('BOOP real observations')]
        self.assertEqual(len(observations),2)
        self.assertNotIn('effort',json.loads(observations[0]['content'].split(': ',1)[1])['recent_days_oldest_first'][0])
        self.assertNotIn('sleep',json.loads(observations[1]['content'].split(': ',1)[1])['recent_days_oldest_first'][0])
        self.assertNotIn('test-only',json.dumps(service.sessions))

    async def test_failed_answer_does_not_commit_context_or_history(self):
        coach=FakeCoach();coach.fail=True
        async def load(*args):return {'days':[]}
        async def live():return {}
        service=AdaptiveCoach(coach,load,live)
        async def plan(*args):return {'topics':['sleep'],'days':7,'through':'2026-10-01','refresh':False}
        service.plan=plan
        with self.assertRaisesRegex(ValueError,'unfinished'):
            _=[e async for e in service.stream(BODY,'device','2026-10-01')]
        self.assertEqual(service.sessions,{})
