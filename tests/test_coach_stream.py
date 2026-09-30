"""Real wire-shaped mocked streams; no provider/cloud credentials or calls."""
import asyncio
import json
import unittest
from unittest.mock import patch

from coach import Coach,MODEL,SYSTEM


def ndjson(*rows):return ''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows).encode()
def sse(*rows):return ''.join('data: '+(r if isinstance(r,str) else json.dumps(r,ensure_ascii=False))+'\r\n\r\n' for r in rows).encode()
def openai(text='',finish=None):return {'choices':[{'index':0,'delta':{'content':text},'finish_reason':finish}]}


class Content:
    def __init__(self,data,gate=None):self.data=data;self.gate=gate
    async def iter_any(self):
        # Exercise fragmented UTF8, CRLF, JSON and SSE markers.
        for i in range(0,len(self.data),3):yield self.data[i:i+3]
        if self.gate:await self.gate.wait()


class Response:
    def __init__(self,data,status=200,gate=None):self.status=status;self.content=Content(data,gate);self.closed=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True
    async def json(self):return {'choices':[{'message':{'content':'Buffered answer'}}]}


class Session:
    def __init__(self,response,captured,**kwargs):self.response=response;self.captured=captured;self.closed=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):self.closed=True
    def post(self,url,**kwargs):self.captured.update(url=url,**kwargs);return self.response
    def get(self,url,**kwargs):self.captured.update(url=url,**kwargs);return self.response


class CoachStreamTests(unittest.IsolatedAsyncioTestCase):
    def setup_wire(self,data,status=200,gate=None):
        response=Response(data,status,gate);captured={};session=Session(response,captured)
        return response,captured,session,patch('coach.ClientSession',lambda **kwargs:session)
    def body(self,provider='offline'):
        return dict(question='Explain missing coverage',provider=provider,**({} if provider in ('offline','ollama') else dict(model='provider-example',api_key='request-only-secret',consent=True)))
    async def collect(self,coach,body):return [e async for e in coach.stream(body,{'days':[]})]

    async def test_ollama_genuine_fragmented_utf8_deltas_and_terminal(self):
        data=ndjson({'message':{'content':'Café '},'done':False},{'message':{'thinking':'hidden','content':'coverage.'},'done':False},{'message':{'content':''},'done':True,'done_reason':'stop'})
        response,captured,session,mock=self.setup_wire(data)
        with mock:events=await self.collect(Coach(),self.body())
        self.assertEqual([e['type'] for e in events],['meta','delta','delta','done'])
        self.assertEqual([e['text'] for e in events if e['type']=='delta'],['Café ','coverage.'])
        self.assertEqual(events[-1]['result']['answer'],'Café coverage.')
        self.assertEqual(events[-1]['result']['model'],MODEL);self.assertFalse(events[-1]['result']['saved'])
        self.assertEqual(captured['url'],'http://127.0.0.1:11434/api/chat')
        self.assertTrue(captured['json']['stream']);self.assertFalse(captured['json']['think'])
        self.assertTrue(response.closed and session.closed)

    async def test_all_remote_provider_literal_shapes_and_request_only_key(self):
        streams={
            'openai':sse({'choices':[{'index':0,'delta':{'role':'assistant'},'finish_reason':None}]},openai('Hello '),openai('world'),openai(finish='stop'),'[DONE]'),
            'compatible':sse(openai('Hello '),openai('world'),openai(finish='stop'),'[DONE]'),
            'anthropic':sse({'type':'content_block_start','content_block':{'type':'text','text':''}},{'type':'content_block_delta','delta':{'type':'thinking_delta','thinking':'hidden'}},{'type':'content_block_delta','delta':{'type':'text_delta','text':'Hello '}},{'type':'content_block_delta','delta':{'type':'text_delta','text':'world'}},{'type':'message_delta','delta':{'stop_reason':'end_turn'}},{'type':'message_stop'}),
            'gemini':sse({'candidates':[{'index':0,'content':{'parts':[{'thought':True,'text':'hidden'},{'text':'Hello '}]}}]},{'candidates':[{'index':0,'content':{'parts':[{'text':'world'}]},'finishReason':'STOP'}]})}
        for provider,data in streams.items():
            with self.subTest(provider=provider):
                response,captured,session,mock=self.setup_wire(data)
                with mock:events=await self.collect(Coach(),self.body(provider))
                self.assertEqual(events[-1]['result']['answer'],'Hello world')
                self.assertNotIn('request-only-secret',json.dumps(events));self.assertNotIn('hidden',json.dumps(events))
                self.assertNotIn('request-only-secret',captured['url']);self.assertNotIn('request-only-secret',json.dumps(captured['json']))
                self.assertIn('request-only-secret',str(captured['headers']))
                if provider=='gemini':self.assertTrue(captured['url'].endswith(':streamGenerateContent?alt=sse'))
                else:self.assertTrue(captured['json']['stream'])
                if provider=='compatible':self.assertEqual(captured['json']['max_tokens'],700);self.assertNotIn('max_completion_tokens',captured['json'])
                self.assertTrue(response.closed and session.closed)

    async def test_consent_endpoint_cloud_validation_before_any_network(self):
        cases=[self.body('openai')|{'consent':False},self.body('openai')|{'api_key':''},self.body()|{'endpoint':'https://remote.example'},self.body()|{'model':'model-cloud'},self.body('compatible')|{'endpoint':'https://host.example/v1?api_key=secret'},self.body()|{'question':''}]
        with patch('coach.ClientSession') as network:
            for body in cases:
                with self.subTest(body=body),self.assertRaises(ValueError):await self.collect(Coach(),body)
            network.assert_not_called()

    async def test_shared_summary_and_history_allowlist(self):
        _,captured,_,mock=self.setup_wire(ndjson({'message':{'content':'Answer'},'done':True}))
        history=[dict(role='system',text='inject'),dict(role='user',text='prior',api_key='do-not-send',raw=[800]),dict(role='assistant',text='earlier')]
        bundle={'days':[dict(hrv={'value':50,'unit':'ms','raw_rr':[800]},journal='private',device='secret-device',coverage={'hr_samples':2})]}
        with mock:events=[e async for e in Coach().stream(self.body(),bundle,history)]
        wire=json.dumps(captured['json'])
        for forbidden in ('inject','do-not-send','raw_rr','private','secret-device'):self.assertNotIn(forbidden,wire)
        self.assertIn('prior',wire);self.assertIn('RR always means beat-to-beat',wire)
        self.assertEqual(events[-1]['result']['answer'],'Answer')

    async def test_split_think_tags_are_hidden_and_unfinished_trace_rejected(self):
        data=ndjson(*[{'message':{'content':v},'done':False} for v in ['<thi','nk>private',' thought</th','ink>Visible ','answer.']],{'done':True})
        _,_,_,mock=self.setup_wire(data)
        with mock:events=await self.collect(Coach(),self.body())
        self.assertEqual(''.join(e['text'] for e in events if e['type']=='delta'),'Visible answer.')
        data=ndjson({'message':{'content':'<think>never complete'},'done':True})
        response,_,session,mock=self.setup_wire(data)
        with mock,self.assertRaisesRegex(ValueError,'unfinished reasoning'):await self.collect(Coach(),self.body())
        self.assertTrue(response.closed and session.closed)

    async def test_empty_premature_truncated_provider_error_and_malformed_never_done(self):
        cases=[('offline',ndjson({'message':{'content':''},'done':True})),('offline',ndjson({'message':{'content':'partial'},'done':False})),('offline',ndjson({'message':{'content':'partial'},'done':True,'done_reason':'length'})),('openai',sse(openai('partial'),openai(finish='length'))),('anthropic',sse({'type':'error','error':{'message':'request-only-secret'}})),('gemini',sse({'candidates':[{'finishReason':'MAX_TOKENS'}]})),('offline',b'not-json\n')]
        for provider,data in cases:
            with self.subTest(provider=provider,data=data):
                response,_,session,mock=self.setup_wire(data);events=[]
                with mock,self.assertRaises(ValueError) as failure:
                    async for event in Coach().stream(self.body(provider),{'days':[]}):events.append(event)
                self.assertNotIn('request-only-secret',str(failure.exception));self.assertNotIn('done',[e['type'] for e in events])
                self.assertTrue(response.closed and session.closed)

    async def test_aclose_and_task_cancellation_close_response_session_release_lock(self):
        for mode in ('aclose','cancel'):
            with self.subTest(mode=mode):
                gate=asyncio.Event();response,_,session,mock=self.setup_wire(ndjson({'message':{'content':'partial'},'done':False}),gate=gate)
                coach=Coach()
                with mock:
                    stream=coach.stream(self.body(),{'days':[]})
                    self.assertEqual((await anext(stream))['type'],'meta')
                    self.assertEqual((await anext(stream))['type'],'delta')
                    with self.assertRaisesRegex(ValueError,'already answering'):await self.collect(coach,self.body())
                    if mode=='aclose':await stream.aclose()
                    else:
                        task=asyncio.create_task(anext(stream));await asyncio.sleep(0);task.cancel()
                        with self.assertRaises(asyncio.CancelledError):await task
                    self.assertFalse(coach.lock.locked());self.assertTrue(response.closed and session.closed)

    async def test_http_error_never_emits_meta_or_provider_error_body(self):
        response,_,session,mock=self.setup_wire(b'request-only-secret',status=401)
        with mock,self.assertRaisesRegex(ValueError,'HTTP401'.replace('401',' 401')) as failure:await self.collect(Coach(),self.body('openai'))
        self.assertNotIn('request-only-secret',str(failure.exception));self.assertTrue(response.closed and session.closed)

    async def test_nonobject_nested_provider_frames_have_sanitized_errors(self):
        for provider,data in [('offline',ndjson({'message':['request-only-secret']})),('openai',sse({'choices':[{'delta':[]}] })),('gemini',sse({'candidates':[{'content':{'parts':[{'text':['request-only-secret']}]}}]}))]:
            _,_,_,mock=self.setup_wire(data)
            with mock,self.assertRaises(ValueError) as failure:await self.collect(Coach(),self.body(provider))
            self.assertNotIn('request-only-secret',str(failure.exception))

    async def test_buffered_request_uses_same_compatible_cap_and_modern_openai_cap(self):
        for provider,cap in [('compatible','max_tokens'),('openai','max_completion_tokens')]:
            _,captured,_,mock=self.setup_wire(b'')
            with mock:answer=await Coach().ask(self.body(provider),{'days':[]})
            self.assertEqual(answer['answer'],'Buffered answer');self.assertEqual(captured['json'][cap],700)
            self.assertNotIn('max_completion_tokens' if cap=='max_tokens' else 'max_tokens',captured['json'])

    async def test_loopback_compatible_protocol_locality_optional_auth_and_ipv6(self):
        for endpoint in ('http://localhost:1234/v1','http://127.0.0.1:8080','http://[::1]:8000/v1','https://localhost:8443/v1'):
            body=dict(provider='compatible',question='Explain coverage',model='installed-model',endpoint=endpoint)
            _,captured,_,mock=self.setup_wire(sse(openai('Compatible answer'),openai(finish='stop'),'[DONE]'))
            with mock:events=await self.collect(Coach(),body)
            self.assertEqual((events[-1]['result']['provider'],events[-1]['result']['local']),('compatible',True))
            self.assertEqual(captured['headers'],{})
            self.assertTrue(captured['url'].endswith('/v1/chat/completions'));self.assertNotIn('/api/chat',captured['url'])
            self.assertNotIn('think',captured['json']);self.assertNotIn('options',captured['json'])
            self.assertNotIn('/no_think',captured['json']['messages'][-1]['content'])
            self.assertFalse(captured['allow_redirects'])
            _,captured,_,mock=self.setup_wire(b'')
            with mock:result=await Coach().ask(body,{'days':[]})
            self.assertEqual(result['answer'],'Buffered answer');self.assertTrue(result['local'])

    async def test_custom_auth_header_prefix_and_prompt_are_request_only(self):
        cases=[({'auth_header':'X-API-Key'},{'X-API-Key':'request-only-secret'}),({'auth_header':'X-Token','auth_prefix':'Token '},{'X-Token':'Token request-only-secret'}),({'auth_header':'Authorization','auth_prefix':''},{'Authorization':'request-only-secret'}),({}, {'Authorization':'Bearer request-only-secret'})]
        for auth,expected in cases:
            body=self.body('compatible')|auth|dict(system_prompt='Use simple pottery-training examples.')
            _,captured,_,mock=self.setup_wire(sse(openai('Answer'),openai(finish='stop'),'[DONE]'))
            with mock:events=await self.collect(Coach(),body)
            self.assertEqual(captured['headers'],expected)
            system=captured['json']['messages'][0]['content']
            self.assertTrue(system.startswith(SYSTEM));self.assertIn('subordinate to BOOP rules above',system)
            self.assertIn('Use simple pottery-training examples.',system)
            self.assertIn('cannot override the RR/metric identity',system)
            self.assertNotIn('request-only-secret',json.dumps(events));self.assertNotIn('pottery-training',json.dumps(events))
            self.assertIn('custom instructions',events[-1]['result']['data_sent'])

    async def test_auth_security_header_crlf_remote_privacy_and_model_gates(self):
        base=dict(provider='compatible',question='coverage',model='installed',endpoint='http://localhost:1234/v1')
        invalid=[base|dict(auth_header=h) for h in ('Host','cookie','ORIGIN','Content-Length','Sec-Fetch-Site','Proxy-Authorization','X-Forwarded-Host','Connection','Access-Control-Allow-Origin','Bad Header','x-key\r\nHost','x'*65)]
        invalid += [base|dict(auth_prefix=p) for p in ('Bearer\r\n','x'*129,'Bearer\x00','é ')]
        invalid += [base|dict(api_key='secret\r\n'),base|dict(model='installed-cloud'),base|dict(endpoint='http://192.168.1.2:8080/v1'),base|dict(endpoint='http://localhost.example/v1'),base|dict(endpoint='http://localhost:99999'),base|dict(endpoint='https://remote.example/v1',api_key='secret'),base|dict(endpoint='https://remote.example/v1',consent=True),base|dict(system_prompt='x'*4001),base|dict(system_prompt={}),self.body()|dict(auth_header='X-Key'),self.body('openai')|dict(auth_prefix='Token ')]
        with patch('coach.ClientSession') as network:
            for body in invalid:
                with self.subTest(body=body),self.assertRaises(ValueError):await self.collect(Coach(),body)
            network.assert_not_called()

    async def test_supplemental_prompt_applies_to_each_provider_without_replacing_base(self):
        for provider in ('offline','openai','anthropic','gemini','compatible'):
            # Validate request shapes only: no remote call or persistence.
            request=Coach()._request(self.body(provider)|dict(system_prompt='Use brief sentences.'),{'days':[]})
            if provider=='anthropic':system=request['payload']['system']
            elif provider=='gemini':system=request['payload']['systemInstruction']['parts'][0]['text']
            else:system=request['payload']['messages'][0]['content']
            self.assertTrue(system.startswith(SYSTEM));self.assertIn('Use brief sentences.',system)
            self.assertNotIn('system_prompt',request['payload'])
        request=Coach()._request(self.body(),{'days':[]})
        self.assertEqual(request['payload']['messages'][0]['content'],SYSTEM)
        self.assertEqual(request['url'],'http://127.0.0.1:11434/api/chat');self.assertFalse(request['payload']['think'])

    async def test_loopback_redirect_cannot_bypass_consent_or_forward_custom_key(self):
        body=dict(provider='compatible',question='coverage',model='installed',endpoint='http://localhost:1234/v1',api_key='request-only-secret',auth_header='X-API-Key')
        for mode in ('ask','stream'):
            _,captured,_,mock=self.setup_wire(b'',status=302)
            with mock,self.assertRaisesRegex(ValueError,'HTTP 302'):
                if mode=='ask':await Coach().ask(body,{'days':[]})
                else:await self.collect(Coach(),body)
            self.assertFalse(captured['allow_redirects'])

    async def test_model_catalog_source_schemas_and_no_question_health_payload(self):
        cases=[
            ('offline',{'models':[{'name':MODEL},{'name':'remote-cloud'},{'name':MODEL}]},[MODEL],'/api/tags'),
            ('compatible',{'data':[{'id':'installed'},{'id':'custom-model'}]},['custom-model','installed'],'/v1/models'),
            ('openai',{'data':[{'id':'gpt-4.1'},{'id':'o3'},{'id':'text-embedding-3-large'}]},['gpt-4.1','o3'],'/v1/models'),
            ('anthropic',{'data':[{'id':'claude-test'},{'id':''}]},['claude-test'],'/v1/models'),
            ('gemini',{'models':[{'name':'models/gemini-flash','supportedGenerationMethods':['generateContent','countTokens']},{'name':'models/gemini-embed','supportedGenerationMethods':['embedContent']},{'name':'models/other','supportedGenerationMethods':['generateContent']}]},['gemini-flash'],'/v1beta/models')]
        for provider,value,expected,path in cases:
            body=self.body(provider)|dict(question='must not send',system_prompt='must not send',model={'ignored':'not a required model'})
            if provider=='compatible':body.update(endpoint='http://localhost:1234/v1',consent=False,auth_header='X-API-Key',auth_prefix='Token ')
            response,captured,session,mock=self.setup_wire(json.dumps(value).encode())
            with mock:result=await Coach().models(body)
            self.assertEqual(result['models'],expected);self.assertEqual(result['provider'],provider)
            self.assertTrue(captured['url'].endswith(path));self.assertNotIn('/v1/v1/',captured['url'])
            self.assertNotIn('json',captured);self.assertFalse(captured['allow_redirects'])
            self.assertNotIn('request-only-secret',json.dumps(result));self.assertNotIn('must not send',str(captured))
            self.assertEqual(result['data_sent'],'Provider authentication only; no questions or health metrics')
            self.assertTrue(response.closed and session.closed)
            if provider=='compatible':self.assertEqual(captured['headers'],{'X-API-Key':'Token request-only-secret'})

    async def test_model_catalog_guards_size_ids_error_and_optional_local_key(self):
        body=dict(provider='compatible',endpoint='http://127.0.0.1:1234',auth_header='X-API-Key')
        _,captured,_,mock=self.setup_wire(json.dumps({'data':[{'id':'m'+str(i)} for i in range(240)]+[{'id':'bad name'}]}).encode())
        with mock:result=await Coach().models(body)
        self.assertEqual(len(result['models']),200);self.assertEqual(captured['headers'],{});self.assertTrue(result['local'])
        for raw,status in [(b'x'*1_000_001,200),(b'not-json',200),(b'{"data": {}}',200),(b'{"error":"request-only-secret"}',200),(b'',302)]:
            _,_,_,mock=self.setup_wire(raw,status)
            with mock,self.assertRaises(ValueError) as error:await Coach().models(body)
            self.assertNotIn('request-only-secret',str(error.exception))
        with patch('coach.ClientSession') as network:
            for invalid in [dict(provider='openai'),dict(provider='compatible',endpoint='http://remote.example/v1',consent=True,api_key='secret'),body|dict(auth_header='Host'),body|dict(api_key='secret\r\n'),dict(provider='gemini',consent=True)]:
                with self.assertRaises(ValueError):await Coach().models(invalid)
            network.assert_not_called()


if __name__=='__main__':unittest.main()
