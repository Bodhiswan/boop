"""NOOP SseDeltas provider shapes, with honest terminal/error and thought gates.

Pinned source: Strand/AI/Providers and StrandAnalytics/SseDeltas.swift at7f396e98.
Ollama uses its native /api/chat NDJSON. No credentials or network IO here.
"""
import codecs
import json


def mapping(value,key):
    item=value.get(key,{})
    if not isinstance(item,dict):raise ValueError('Coach provider returned malformed stream data')
    return item


def objects(value,key):
    items=value.get(key,[])
    if not isinstance(items,list) or not all(isinstance(v,dict) for v in items):raise ValueError('Coach provider returned malformed stream data')
    return items


async def text_lines(content):
    """Incremental UTF8: network chunks may split a line, JSON or multibyte text."""
    decoder=codecs.getincrementaldecoder('utf-8')();pending=''
    try:
        async for chunk in content.iter_any():
            pending+=decoder.decode(chunk)
            if len(pending)>1_000_000:raise ValueError('Coach stream frame exceeded the supported size')
            while '\n' in pending:
                line,pending=pending.split('\n',1);yield line.rstrip('\r')
        pending+=decoder.decode(b'',final=True)
        if pending:yield pending.rstrip('\r')
    except UnicodeError:raise ValueError('Coach provider returned malformed stream encoding') from None


async def frames(content,ndjson=False):
    if ndjson:
        async for line in text_lines(content):
            if line.strip():yield line
        return
    lines=[];size=0
    async for line in text_lines(content):
        if not line:
            if lines:yield '\n'.join(lines)
            lines=[];size=0
        elif line.startswith('data:'):
            value=line[5:];value=value[1:] if value.startswith(' ') else value
            size+=len(value)
            if size>1_000_000:raise ValueError('Coach stream frame exceeded the supported size')
            lines.append(value)
        # SSE comments, id and event fields do not contain response text.
    if lines:yield '\n'.join(lines)


async def provider_events(content,provider):
    """Yield textual deltas only; require an observed successful terminal event."""
    finished=False;stop_reason=None
    async for frame in frames(content,provider in ('offline','ollama')):
        if frame=='[DONE]':
            if provider not in ('openai','compatible'):raise ValueError('Unexpected Coach stream terminator')
            if stop_reason not in (None,'stop'):raise ValueError('Coach reply did not finish normally')
            finished=True;break
        try:value=json.loads(frame)
        except (ValueError,TypeError):raise ValueError('Coach provider returned malformed stream data') from None
        if not isinstance(value,dict):raise ValueError('Coach provider returned malformed stream data')
        if value.get('error') or value.get('type')=='error':raise ValueError('Coach provider reported a streaming error; check the selected model')
        delta=''
        if provider in ('offline','ollama'):
            delta=mapping(value,'message').get('content','')
            if value.get('done') is True:
                if value.get('done_reason') not in (None,'stop'):raise ValueError('Coach reply did not finish normally')
                finished=True
        elif provider=='anthropic':
            kind=value.get('type')
            if kind=='content_block_delta' and mapping(value,'delta').get('type')=='text_delta':delta=value['delta'].get('text','')
            elif kind=='content_block_start' and mapping(value,'content_block').get('type')=='text':delta=value['content_block'].get('text','')
            elif kind=='message_delta':stop_reason=mapping(value,'delta').get('stop_reason')
            elif kind=='message_stop':
                if stop_reason not in ('end_turn','stop_sequence'):raise ValueError('Coach reply did not finish normally')
                finished=True
        elif provider=='gemini':
            if mapping(value,'promptFeedback').get('blockReason'):raise ValueError('Coach provider blocked the requested reply')
            first=next((c for c in objects(value,'candidates') if c.get('index',0)==0),{})
            parts=[p.get('text','') for p in objects(mapping(first,'content'),'parts') if not p.get('thought')]
            if not all(isinstance(p,str) for p in parts):raise ValueError('Coach provider returned a non-text reply')
            delta=''.join(parts)
            reason=first.get('finishReason')
            if reason:
                if reason!='STOP':raise ValueError('Coach reply did not finish normally')
                finished=True
        else:
            first=next((c for c in objects(value,'choices') if c.get('index',0)==0),{})
            delta=mapping(first,'delta').get('content','') or ''
            reason=first.get('finish_reason')
            if reason:
                if reason!='stop':raise ValueError('Coach reply did not finish normally')
                stop_reason=reason;finished=True
        if not isinstance(delta,str):raise ValueError('Coach provider returned a non-text reply')
        if delta:yield delta
        # OpenAI may follow finish_reason with usage and[DONE]; keep parsing until
        # EOF so late provider errors do not turn partial content into saved text.
        if finished and provider not in ('openai','compatible'):break
    if not finished:raise ValueError('Coach stream ended before a completed reply')


class ThoughtFilter:
    """Suppress <think> traces even when tags are split across network deltas."""
    def __init__(self):self.buffer='';self.thinking=False
    def feed(self,text):
        self.buffer+=text;visible=[]
        while self.buffer:
            tag='</think>' if self.thinking else '<think>'
            at=self.buffer.find(tag)
            if at>=0:
                if not self.thinking:visible.append(self.buffer[:at])
                self.buffer=self.buffer[at+len(tag):];self.thinking=not self.thinking;continue
            hold=next((i for i in range(min(len(tag)-1,len(self.buffer)),0,-1) if self.buffer.endswith(tag[:i])),0)
            safe=self.buffer[:-hold] if hold else self.buffer
            if not self.thinking:visible.append(safe)
            self.buffer=self.buffer[-hold:] if hold else '';break
        return ''.join(visible)
    def finish(self):
        if self.thinking or self.buffer and '<think>'.startswith(self.buffer):raise ValueError('This model returned an unfinished reasoning trace. Select the installed instruct model.')
        text=self.buffer;self.buffer='';return text
