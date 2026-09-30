/* Decode real server events without treating network chunks as text boundaries. */
(() => {
  window.boopCoachStream = async (payload, {signal, onEvent=()=>{}}={}) => {
    const response=await fetch('/api/coach/stream',{method:'POST',headers:{'Content-Type':'application/json','X-Boop':'local','Accept':'text/event-stream'},body:JSON.stringify(payload),signal});
    if(response.status===404)return {fallback:true};
    if(!response.ok){let message='Coach request failed ('+response.status+').';try{const v=await response.json();message=v.error||message;}catch{}throw Error(message);}
    if(!response.body)throw Error('This browser could not read the Coach stream.');
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',data=[],result=null,done=false;
    const abort=()=>{if(signal?.aborted)throw new DOMException('The reply was cancelled.','AbortError');};
    const dispatch=()=>{if(!data.length)return;const raw=data.join('\n');data=[];let event;try{event=JSON.parse(raw);}catch{throw Error('The Coach sent an unreadable event.');}if(event.type==='error')throw Error(event.error||event.message||'The Coach could not finish this reply.');onEvent(event);if(event.type==='done'){result=event.result||event;done=true;}};
    const line=value=>{if(value===''){dispatch();return;}if(value.startsWith('data:'))data.push(value.slice(5).replace(/^ /,''));};
    try{
      while(!done){abort();const chunk=await reader.read();abort();buffer+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done});let match;while((match=/\r\n|\r(?!$)|\n/.exec(buffer))){line(buffer.slice(0,match.index));buffer=buffer.slice(match.index+match[0].length);if(done)break;}if(chunk.done){if(buffer){line(buffer.replace(/\r$/,''));buffer='';}dispatch();break;}}
      if(!done)throw Error('The connection ended before the Coach finished.');return {result};
    }finally{try{await reader.cancel();}catch{}reader.releaseLock();}
  };
})();
