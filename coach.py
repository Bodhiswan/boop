"""A request-scoped coach. Default inference stays on this computer."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout

MODEL = "qwen3:4b-instruct"
LOCAL = "http://127.0.0.1:11434"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
SYSTEM = ("You are BOOP's concise local wellbeing coach. Explain the supplied local "
          "BOOP strap metrics and their coverage. Never invent readings or personal facts. "
          "Computed metrics are local estimates; imported scores retain their source. Missing values "
          "are unknown. RR always means beat-to-beat R-R intervals in milliseconds, "
          "never respiratory rate; respiration is a separate breaths-per-minute estimate. "
          "RMSSD HRV is calculated from clean consecutive beat intervals, not respiration. "
          "Do not diagnose illness or recommend drugs. If the question "
          "cannot be answered from these observations, explain what is missing. "
          "Use natural plain language, at most three short paragraphs, normally under 180 words. No thinking trace. "
          "BOOP is the user's local WHOOP strap companion. Charge means recovery against a personal baseline; "
          "Effort is cardiovascular strain; Rest is the local sleep score. Nightly/resting HRV is distinct "
          "from live RMSSD. Respect each score's supplied scale. A sample count describes coverage, not health "
          "or fitness, and is not proof that every input is sufficient. Only compare days with usable data. "
          "When there is just one usable day, describe it briefly without inventing a trend or repeatedly "
          "listing raw sample counts. Demo sleep in the terminal is a synthetic display preview and is not "
          "part of the supplied real health observations. Answer the question directly and finish your answer.")


def metric_summary(bundle, limit=7):
    """An explicit small allowlist; no IDs, timestamps, journal text or raw streams."""
    days = bundle.get("days", [])[-limit:]
    output = []
    for day in days:
        row = {}
        for key in ("hrv", "resting_hr", "respiration", "charge", "effort", "rest", "skin_temperature"):
            value = day.get(key, {})
            if isinstance(value, dict):
                row[key] = {k: value[k] for k in ("value", "unit", "reason", "display_value", "display_max", "display_scale", "source") if k in value}
        sleep = day.get("sleep", {})
        main = sleep.get("main") or {}
        row["sleep"] = {"minutes": main.get("total_sleep_min"), "need_hours": sleep.get("need_hours")}
        coverage = day.get("coverage", {})
        row["coverage"] = {k: coverage.get(k) for k in ("hr_samples", "rr_intervals", "gravity_samples")}
        output.append(row)
    return {"recent_days_oldest_first": output, "training_load": bundle.get("training_load"),
            "reference": "NOOP local estimates; missing observations stay unknown"}


def question_context(bundle, question, selected_topics=None, limit=7):
    """Choose relevant observations locally before contacting a provider."""
    q=question.lower()
    topics=set()
    if re.search(r'sleep|rest\b|debt|night|tired|fatigue',q):topics.add('sleep')
    if re.search(r'effort|strain|train|workout|exercise|activity|zone',q):topics.add('effort')
    if re.search(r'hrv|rhr|resting|recovery|charge|stress|ready',q):topics.add('recovery')
    if re.search(r'live|current|now|heart rate|\bhr\b|pulse|battery',q):topics.add('live')
    if not topics or re.search(r'overview|summary|today|how am i|compare|trend',q):topics.update(('sleep','effort','recovery'))
    if selected_topics is not None:topics=set(selected_topics)
    summary=metric_summary(bundle,limit)
    keys={'sleep':('rest','sleep'),'effort':('effort',),'recovery':('hrv','resting_hr','charge','respiration','skin_temperature')}
    wanted={'coverage'}
    for topic in topics:wanted.update(keys.get(topic,()))
    rows=[]
    for source,row in zip(bundle.get('days',[])[-limit:],summary['recent_days_oldest_first']):
        item={k:v for k,v in row.items() if k in wanted}
        item['day']=source.get('day')
        if 'sleep' in topics:
            sleep=source.get('sleep') or {};debt=sleep.get('debt') or {}
            if debt.get('nights'):item['sleep']['balance_min']=debt.get('balance_min')
            totals={}
            for stage in ((sleep.get('main') or {}).get('staging') or {}).get('value') or []:
                if isinstance(stage,dict) and stage.get('stage') in ('wake','awake','rem','light','deep'):
                    a,b=stage.get('start'),stage.get('end')
                    if isinstance(a,(int,float)) and isinstance(b,(int,float)) and b>a:
                        name='wake' if stage['stage']=='awake' else stage['stage']
                        totals[name]=totals.get(name,0)+(b-a)/60
            if totals:item['sleep']['stage_minutes']=totals
        rows.append(item)
    result={'context_selection':sorted(topics),'recent_days_oldest_first':rows,'reference':summary['reference'],'demo_sleep_excluded':True}
    if 'effort' in topics:result['training_load']=summary['training_load']
    if 'live' in topics:result['live']=bundle.get('live',{'reason':'Live readings unavailable'})
    return result


def provider_error(status, url):
    if urlsplit(url).hostname=='openrouter.ai':
        if status==401:return 'OpenRouter rejected authentication (401). Verify your OpenRouter API key in Settings; model selection does not validate the key.'
        if status==402:return 'OpenRouter requires account credit or a higher key spending limit (402).'
        if status==403:return 'OpenRouter denied access (403); check account and model permissions.'
    return f'Coach provider returned HTTP {status}; check the model and endpoint'


class Coach:
    def __init__(self):
        self.lock = asyncio.Lock()

    async def options(self):
        ready, models = False, []
        try:
            async with ClientSession(timeout=ClientTimeout(total=3)) as client:
                async with client.get(LOCAL + "/api/tags") as response:
                    if response.status == 200:
                        payload = await response.json()
                        models = [m["name"] for m in payload.get("models", [])]
                        ready = MODEL in models
        except (OSError, asyncio.TimeoutError):
            pass
        return {"default": "offline", "local_ready": ready, "local_models": models,
                "default_model": MODEL, "providers": ["offline", "ollama", "openai", "anthropic", "gemini", "compatible"],
                "privacy": "Local inference by default. Remote providers require consent for each request; keys are never stored."}

    def _request(self, body, bundle, history=(),catalog=False):
        """One allowlisted request shape shared by buffered and streamed inference."""
        if not isinstance(body,dict):
            raise ValueError("Coach request must be an object")
        question = 'Model catalog' if catalog else body.get("question", "")
        if not isinstance(question, str) or not question.strip() or len(question) > 4000:
            raise ValueError("Ask a question of 1–4,000 characters")
        provider = body.get("provider", "offline")
        if provider not in ("offline", "ollama", "openai", "anthropic", "gemini", "compatible"):
            raise ValueError("Unknown Coach provider")
        native = provider in ("offline", "ollama")
        endpoint = body.get("endpoint") or (LOCAL if native else "https://api.anthropic.com" if provider == "anthropic" else "https://generativelanguage.googleapis.com" if provider == "gemini" else "https://api.openai.com")
        if not isinstance(endpoint,str) or len(endpoint)>2000:
            raise ValueError("Use a valid base endpoint URL")
        try:
            parsed = urlsplit(endpoint)
            port = parsed.port  # Validate malformed/out-of-range ports before IO.
        except ValueError:raise ValueError("Use a valid base endpoint URL") from None
        if not parsed.hostname:
            raise ValueError("Use a valid base endpoint URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use a base endpoint URL without credentials or query parameters")
        if native and (parsed.scheme != "http" or parsed.hostname not in LOOPBACK):
            raise ValueError("Local Coach endpoints must use this computer's loopback address")
        local = native or provider=="compatible" and parsed.hostname in LOOPBACK
        if local and parsed.scheme not in ("http","https"):
            raise ValueError("Loopback compatible endpoints require HTTP or HTTPS")
        if not local and parsed.scheme != "https":
            raise ValueError("External endpoints require HTTPS")
        if not local and body.get("consent") is not True:
            raise ValueError("External Coach requests require explicit consent")
        model = 'model-list' if catalog else body.get("model") or (MODEL if native else None)
        if model is None:
            raise ValueError("Enter a model supported by the selected provider")
        if not isinstance(model, str) or not re.fullmatch(r"[\w./:@+-]{1,128}", model):
            raise ValueError("Invalid model name")
        if local and "cloud" in model.lower():
            raise ValueError("Local Coach requires installed local weights; cloud models use an explicitly consented external provider")
        key = body.get("api_key", "")
        if isinstance(key,str) and parsed.hostname=='openrouter.ai':
            key=re.sub(r'^Bearer\s+','',key.strip(),flags=re.I).strip()
        if not isinstance(key,str) or len(key)>1000 or any(ord(c)<32 or ord(c)==127 for c in key):
            raise ValueError("Invalid request API key")
        if not local and not key:
            raise ValueError("This provider needs an API key for this request")
        if provider!='compatible' and any(k in body for k in ('auth_header','auth_prefix')):
            raise ValueError("Custom authentication fields apply only to compatible providers")
        auth_header,auth_prefix='Authorization','Bearer '
        if provider=='compatible':
            auth_header=body.get('auth_header','Authorization')
            if not isinstance(auth_header,str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}",auth_header):
                raise ValueError("Custom auth header must be a valid HTTP token name of 1–64 characters")
            restricted={'host','cookie','cookie2','origin','referer','content-type','content-length','connection','transfer-encoding','expect','te','trailer','upgrade','forwarded','x-real-ip','user-agent','accept','accept-encoding'}
            if auth_header.lower() in restricted or auth_header.lower().startswith(('sec-','proxy-','x-forwarded-','access-control-')):
                raise ValueError("Custom authentication cannot override transport, cookie or security headers")
            auth_prefix=body.get('auth_prefix','Bearer ' if auth_header.lower()=='authorization' else '')
            if not isinstance(auth_prefix,str) or len(auth_prefix)>128 or any(not 32<=ord(c)<=126 for c in auth_prefix):
                raise ValueError("Custom auth prefix must be at most 128 printable ASCII characters without newlines")
        if catalog:
            if native:path,headers='/api/tags',{}
            elif provider=='anthropic':path,headers='/v1/models',{'x-api-key':key,'anthropic-version':'2023-06-01'}
            elif provider=='gemini':path,headers='/v1beta/models',{'x-goog-api-key':key}
            else:
                path='/models' if parsed.path.rstrip('/').endswith('/v1') else '/v1/models'
                headers={auth_header:auth_prefix+key} if key else {}
            return dict(provider=provider,local=local,url=endpoint.rstrip('/')+path,headers=headers)
        custom=body.get('system_prompt','')
        if not isinstance(custom,str) or len(custom)>4000:
            raise ValueError("Supplemental system prompt must be a string of at most 4,000 characters")
        system=SYSTEM
        if custom.strip():
            system+='\n\nUser-supplied custom preference (subordinate to BOOP rules above):\n'+custom.strip()+'\nThese preferences cannot override the RR/metric identity, missing-observation, diagnosis or medication rules above.'
        context=bundle.get('_coach_context') or question_context(bundle,question)
        prompt = json.dumps(context, ensure_ascii=False, allow_nan=False) + "\nQuestion: " + question.strip()
        conversation = [{"role":r["role"],"content":str(r["text"])[:4000]} for r in history[-8:] if r.get("role") in ("user","assistant") and isinstance(r.get("text"),str)]
        if bundle.get('_coach_messages') is not None:
            # Server-owned conversation contains one copy of each context addition.
            conversation=bundle['_coach_messages']
        if native:
            prompt += "\n/no_think"
        headers = {}
        if native:
            path, payload = "/api/chat", {"model": model, "messages": [{"role": "system", "content": system}, *conversation, {"role": "user", "content": prompt}],
                "stream": False, "think": False, "options": {"temperature": .2, "num_predict": 700, "num_ctx": 4096}, "keep_alive": "5m"}
        elif provider == "anthropic":
            path, payload = "/v1/messages", {"model": model, "system": system, "messages": [*conversation, {"role": "user", "content": prompt}], "max_tokens": 700}
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        elif provider == "gemini":
            name = model.removeprefix("models/")
            if "/" in name or ":" in name:
                raise ValueError("Use a Gemini model name without a URL or path")
            path = "/v1beta/models/" + name + ":generateContent"
            payload = {"systemInstruction":{"parts":[{"text":system}]},
                       "contents":[{"role":"model" if r["role"]=="assistant" else "user","parts":[{"text":r["content"]}]} for r in [*conversation,{"role":"user","content":prompt}]],
                       "generationConfig":{"maxOutputTokens":700,"temperature":.2}}
            headers = {"x-goog-api-key":key}
        else:
            path = "/chat/completions" if parsed.path.rstrip("/").endswith("/v1") else "/v1/chat/completions"
            payload = {"model": model, "messages": [{"role": "system", "content": system}, *conversation, {"role": "user", "content": prompt}], "max_completion_tokens": 700}
            if provider=='compatible':
                # Pinned CustomClient uses the standard compatible-server cap.
                payload['max_tokens']=payload.pop('max_completion_tokens')
                if parsed.hostname=='openrouter.ai':
                    # Reasoning and visible text share OpenRouter's output budget.
                    # 700 tokens can cut a GLM reply off mid-sentence.
                    payload['max_tokens']=8192
                    if model.lower().startswith('z-ai/glm-5'):
                        payload['reasoning']={'effort':'low','exclude':True}
            headers = {auth_header: auth_prefix + key} if key else {}
        sent='Question, recent conversation and relevant seven-day observations: '+', '.join(context['context_selection'])
        if custom.strip():sent='Question, custom instructions, recent conversation and seven-day metric/coverage summary'
        return dict(provider=provider,local=local,model=model,url=endpoint.rstrip("/")+path,payload=payload,headers=headers,data_sent=sent)

    async def verify_key(self,body):
        from aiohttp import ClientError
        if not isinstance(body,dict):raise ValueError('Key verification request must be an object')
        request=self._request({'api_key':body.get('api_key',''),'consent':body.get('consent'),
            'provider':'compatible','endpoint':'https://openrouter.ai/api/v1'}, {},catalog=True)
        url='https://openrouter.ai/api/v1/key'
        try:
            async with ClientSession(timeout=ClientTimeout(total=15)) as client:
                async with client.get(url,headers=request['headers'],allow_redirects=False) as response:
                    if response.status!=200:raise ValueError(provider_error(response.status,url))
                    return {'valid':True,'data_sent':'Authentication only'}
        except (ClientError,OSError,asyncio.TimeoutError):raise ValueError('Cannot reach OpenRouter to verify the key; try again.') from None

    async def models(self,body):
        """Explicit model refresh; only authentication leaves this process.

        Pinned provider fetchModels/model parsers; Gemini additionally retains
        only models advertising generateContent. At most200 bounded unique IDs,
        a1MB response and15-second request limit; keys never enter the result.
        """
        from aiohttp import ClientError
        request=self._request(body,{},catalog=True);provider=request['provider']
        try:
            async with ClientSession(timeout=ClientTimeout(total=15)) as client:
                async with client.get(request['url'],headers=request['headers'],allow_redirects=False) as response:
                    if response.status!=200:raise ValueError(f'Coach model provider returned HTTP {response.status}; check the endpoint and authentication')
                    chunks=[];size=0
                    async for chunk in response.content.iter_any():
                        size+=len(chunk)
                        if size>1_000_000:raise ValueError('Coach model catalog exceeded the supported size')
                        chunks.append(chunk)
                    try:value=json.loads(b''.join(chunks))
                    except (ValueError,UnicodeError):raise ValueError('Coach provider returned a malformed model catalog') from None
        except (ClientError,OSError,asyncio.TimeoutError):raise ValueError('Coach model connection unavailable; check the selected provider.') from None
        if not isinstance(value,dict) or value.get('error'):raise ValueError('Coach provider returned an invalid model catalog')
        rows=value.get('models' if provider in ('offline','ollama','gemini') else 'data',[])
        if not isinstance(rows,list):raise ValueError('Coach provider returned an invalid model catalog')
        models=set()
        for row in rows:
            if not isinstance(row,dict):continue
            name=row.get('name') if provider in ('offline','ollama','gemini') else row.get('id')
            if not isinstance(name,str):continue
            if provider=='gemini':
                methods=row.get('supportedGenerationMethods',[])
                if not isinstance(methods,list) or 'generateContent' not in methods:continue
                name=name.removeprefix('models/')
                if not name.startswith('gemini'):continue
            if provider=='openai' and not name.startswith(('gpt','o')):continue
            if not re.fullmatch(r'[\w./:@+-]{1,128}',name):continue
            if request['local'] and 'cloud' in name.lower():continue
            models.add(name)
            if len(models)>=200:break
        return dict(provider=provider,local=request['local'],models=sorted(models),data_sent='Provider authentication only; no questions or health metrics')

    async def ask(self, body, bundle, history=()):
        request=self._request(body,bundle,history)
        provider,local,model=request['provider'],request['local'],request['model']
        if self.lock.locked():
            raise ValueError("The Coach is already answering; wait for the current reply")
        async with self.lock:
            try:
                async with ClientSession(timeout=ClientTimeout(total=150)) as client:
                    async with client.post(request['url'], json=request['payload'], headers=request['headers'],allow_redirects=False) as response:
                        if response.status != 200:
                            # Never echo provider responses: they can contain credentials or prompts.
                            raise ValueError(provider_error(response.status,request['url']))
                        value = await response.json()
            except (OSError, asyncio.TimeoutError) as exc:
                raise ValueError("Coach connection unavailable. Start the local model or check the selected provider.") from exc
        if provider in ('offline','ollama'):
            answer = value.get("message", {}).get("content", "")
        elif provider == "anthropic":
            answer = "\n".join(p.get("text", "") for p in value.get("content", []) if p.get("type") == "text")
        elif provider == "gemini":
            answer = "\n".join(p.get("text", "") for p in (value.get("candidates") or [{}])[0].get("content",{}).get("parts",[]) if not p.get("thought"))
        else:
            answer = (value.get("choices") or [{}])[0].get("message", {}).get("content", "")
        answer = str(answer)
        if "</think>" in answer:
            answer = answer.rsplit("</think>",1)[-1]
        elif "<think>" in answer:
            raise ValueError("This model returned an unfinished reasoning trace. Select the installed instruct model.")
        answer = re.sub(r"<think>.*?</think>", "", answer, flags=re.S).strip()
        if not answer:
            raise ValueError("The provider returned no answer")
        return {"answer": answer, "provider": provider, "model": model, "local": local,
                "data_sent": request['data_sent'], "saved": False}

    async def stream(self,body,bundle,history=()):
        """Real provider deltas; only a verified complete reply emits done.

        Consumers must aclose this generator on disconnect. Both cancellation and
        aclose unwind the response/session context and release the request lock.
        """
        from aiohttp import ClientError
        from coach_stream import provider_events,ThoughtFilter
        request=self._request(body,bundle,history)
        provider,local,model=request['provider'],request['local'],request['model']
        payload=request['payload'];payload['stream']=True
        url=request['url']
        if provider=='gemini':
            payload.pop('stream',None)
            url=url.removesuffix(':generateContent')+':streamGenerateContent?alt=sse'
        metadata=dict(provider=provider,model=model,local=local,data_sent=request['data_sent'])
        if self.lock.locked():raise ValueError('The Coach is already answering; wait for the current reply')
        answer=[];filter=ThoughtFilter();size=0
        async with self.lock:
            try:
                async with ClientSession(timeout=ClientTimeout(total=150)) as client:
                    async with client.post(url,json=payload,headers=request['headers'],allow_redirects=False) as response:
                        if response.status!=200:raise ValueError(provider_error(response.status,request['url']))
                        yield dict(type='meta',**metadata)
                        async for raw in provider_events(response.content,provider):
                            text=filter.feed(raw)
                            if text:
                                size+=len(text)
                                if size>24000:raise ValueError('Coach reply exceeded the supported size')
                                answer.append(text);yield dict(type='delta',text=text)
                        tail=filter.finish()
                        if tail:answer.append(tail);yield dict(type='delta',text=tail)
            except (ClientError,OSError,asyncio.TimeoutError):
                # Do not echo network exceptions, provider error bodies or keys.
                raise ValueError('Coach connection unavailable. Start the local model or check the selected provider.') from None
        text=''.join(answer).strip()
        if not text:raise ValueError('The provider returned no answer')
        yield dict(type='done',result=dict(answer=text,**metadata,saved=False))
