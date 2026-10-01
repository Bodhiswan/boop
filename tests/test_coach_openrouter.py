"""OpenRouter budget/context regression checks without paid provider requests."""
import unittest
from coach import Coach, metric_summary, question_context
from tests.test_coach_stream import CoachStreamTests, sse, openai


class OpenRouterTests(CoachStreamTests):
    def test_question_selects_sleep_and_excludes_unrelated_metrics(self):
        bundle={'days':[{'day':'2026-10-01','effort':{'value':20},'hrv':{'value':40},
            'sleep':{'main':{'total_sleep_min':400,'staging':{'value':[{'start':0,'end':3600,'stage':'deep'}]}},
                     'debt':{'balance_min':-40,'nights':[{}]}},'journal':'private'}]}
        context=question_context(bundle,'How much deep sleep did I get?')
        row=context['recent_days_oldest_first'][0]
        self.assertEqual(context['context_selection'],['sleep'])
        self.assertEqual(row['sleep']['stage_minutes']['deep'],60)
        self.assertEqual(row['sleep']['balance_min'],-40)
        self.assertNotIn('effort',row);self.assertNotIn('journal',row)
        self.assertNotIn('live',context)

    async def test_key_check_normalizes_bearer_and_sends_no_health_data(self):
        _,captured,_,mock=self.setup_wire(b'')
        with mock:result=await Coach().verify_key({'api_key':' Bearer test-key ','consent':True})
        self.assertTrue(result['valid'])
        self.assertEqual(captured['url'],'https://openrouter.ai/api/v1/key')
        self.assertEqual(captured['headers'],{'Authorization':'Bearer test-key'})
        self.assertNotIn('json',captured)

    async def test_key_rejection_is_actionable_and_does_not_echo_credentials(self):
        _,_,_,mock=self.setup_wire(b'',status=401)
        with mock,self.assertRaisesRegex(ValueError,'Verify your OpenRouter API key'):
            await Coach().verify_key({'api_key':'private-test-key','consent':True})

    def router_body(self):
        return self.body('compatible') | {'endpoint':'https://openrouter.ai/api/v1','model':'z-ai/glm-5.3'}

    def test_glm_budget_context_and_scope(self):
        request=Coach()._request(self.router_body(),{'days':[]})
        self.assertEqual(request['payload']['max_tokens'],8192)
        self.assertEqual(request['payload']['reasoning'],{'effort':'low','exclude':True})
        self.assertIn('synthetic display preview',request['payload']['messages'][0]['content'])
        other=Coach()._request(self.router_body()|{'endpoint':'https://example.com/v1'},{'days':[]})
        self.assertEqual(other['payload']['max_tokens'],700)
        self.assertNotIn('reasoning',other['payload'])

    def test_summary_has_scale_and_rr_coverage(self):
        result=metric_summary({'days':[{'effort':{'value':25,'display_value':5.25,'display_max':21,'display_scale':'whoop'},'coverage':{'rr_intervals':42}}]})
        day=result['recent_days_oldest_first'][0]
        self.assertEqual(day['effort']['display_max'],21)
        self.assertEqual(day['coverage']['rr_intervals'],42)

    async def test_glm_success_ignores_reasoning_and_finishes(self):
        stream=sse({'choices':[{'index':0,'delta':{'reasoning':'private reasoning'},'finish_reason':None}]},openai('Your effort is a local estimate.'),openai(finish='stop'),'[DONE]')
        _,captured,_,mock=self.setup_wire(stream)
        with mock:events=await self.collect(Coach(),self.router_body())
        self.assertEqual(events[-1]['result']['answer'],'Your effort is a local estimate.')
        self.assertEqual(captured['json']['max_tokens'],8192)

    async def test_truncated_glm_is_explicit_and_unsaved(self):
        _,_,_,mock=self.setup_wire(sse(openai('Partial answer'),openai(finish='length'),'[DONE]'))
        with mock,self.assertRaisesRegex(ValueError,'output token limit'):
            await self.collect(Coach(),self.router_body())
