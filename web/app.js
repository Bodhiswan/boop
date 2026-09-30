/* Shared in-app confirmations: every queued action receives its own decision. */
(() => {
  const queue=[];
  let showing=false, sequence=0;
  window.boopConfirm = (options={}) => new Promise(resolve => {
    queue.push({options:typeof options==='string'?{message:options}:options,resolve});
    showNext();
  });
  function restoreFocus(target) {
    if (!target?.isConnected) return;
    if (!target.disabled) { target.focus({preventScroll:true}); return; }
    const fallback=target.closest('form,article,section')?.querySelector('input:not([disabled]),select:not([disabled]),button:not([disabled]),a[href]');
    fallback?.focus({preventScroll:true});
    // A caller may keep its trigger disabled while the confirmed request finishes.
    if (typeof MutationObserver==='function') {
      const observer=new MutationObserver(() => {
        if (!target.isConnected || !target.disabled) {
          observer.disconnect();
          if (target.isConnected && (document.activeElement===fallback || document.activeElement===document.body)) target.focus({preventScroll:true});
        }
      });
      observer.observe(target,{attributes:true,attributeFilter:['disabled']});
      setTimeout(()=>observer.disconnect(),15000);
    }
  }
  function showNext() {
    if (showing || !queue.length) return;
    showing=true;
    const {options:o,resolve}=queue.shift(),previous=o.trigger||document.activeElement,id='boop-confirm-'+(++sequence);
    const dialog=document.createElement('dialog');
    dialog.className='confirmation-dialog';
    dialog.setAttribute('role','dialog');
    dialog.setAttribute('aria-modal','true');
    dialog.setAttribute('aria-labelledby',id+'-title');
    dialog.setAttribute('aria-describedby',id+'-description');
    dialog.innerHTML='<p class="eyebrow">REVIEW THIS ACTION</p><h2></h2><p class="confirmation-description"></p><div class="confirmation-actions"><button type="button" class="confirmation-cancel">Cancel</button><button type="button" class="confirmation-accept"></button></div>';
    const title=dialog.querySelector('h2'),description=dialog.querySelector('.confirmation-description'),cancel=dialog.querySelector('.confirmation-cancel'),accept=dialog.querySelector('.confirmation-accept');
    title.id=id+'-title';title.textContent=o.title||'Confirm this action';
    description.id=id+'-description';description.textContent=o.message||'';
    cancel.autofocus=true;accept.textContent=o.confirmLabel||'Continue';
    accept.classList.add(o.destructive?'destructive':'primary');
    const inert=[];let settled=false,shade=null;
    function finish(approved) {
      if (settled) return;
      settled=true;
      if (dialog.open && typeof dialog.close==='function') dialog.close();
      inert.forEach(([element,value])=>element.inert=value);
      dialog.remove();shade?.remove();showing=false;
      resolve(approved);
      queueMicrotask(()=>{restoreFocus(previous);showNext();});
    }
    cancel.addEventListener('click',()=>finish(false));
    accept.addEventListener('click',()=>finish(true));
    dialog.addEventListener('cancel',event=>{event.preventDefault();finish(false);});
    dialog.addEventListener('close',()=>finish(false));
    dialog.addEventListener('keydown',event=>{
      if (event.key==='Escape') {event.preventDefault();finish(false);}
      if (event.key==='Tab') {
        const first=cancel,last=accept;
        if (event.shiftKey && document.activeElement===first) {event.preventDefault();last.focus();}
        else if (!event.shiftKey && document.activeElement===last) {event.preventDefault();first.focus();}
      }
    });
    document.body.append(dialog);
    if (typeof dialog.showModal==='function') dialog.showModal();
    else {
      shade=document.createElement('div');shade.className='confirmation-backdrop';shade.setAttribute('aria-hidden','true');document.body.append(shade);
      for (const element of document.body.children) if(element!==dialog) {inert.push([element,element.inert]);element.inert=true;}
      dialog.setAttribute('open','');
    }
    cancel.focus({preventScroll:true});
  }
})();

const $ = id => document.getElementById(id);
let state = null, hours = 1, busy = false, stopped = false, lastChart = 0, connectionLost = false, lastChartPoints = null;
const time = ms => new Date(ms).toLocaleTimeString('en-AU', {timeZone:'Australia/Brisbane',hour:'2-digit',minute:'2-digit'});
const number = n => Number(n || 0).toLocaleString('en-AU');
const brandText = value => String(value ?? '').replace(/whoop/gi, 'BOOP');
function notice(text) { $('notice').textContent = brandText(text); $('notice').hidden = !text; }
async function action(name, body = {}) {
  const response = await fetch('/api/' + name, {method:'POST',headers:{'Content-Type':'application/json','X-Boop':'local'},body:JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'The action could not finish.');
  return result;
}
function render(s) {
  state = s;
  $('deviceName').textContent = brandText(s.name || 'BOOP strap');
  $('connectionBadge').textContent = s.connected ? 'CONNECTED' : s.phase.toUpperCase();
  $('connectionBadge').classList.toggle('connected', s.connected);
  $('connect').hidden = s.connected;
  $('disconnect').hidden = !s.connected;
  $('connect').textContent = s.address ? 'Reconnect BOOP' : 'Connect BOOP';
  $('connectionDetail').textContent = s.connected ? 'Your strap is connected directly to this laptop. Recording continues while BOOP is running.' : 'Keep your strap nearby and your phone’s Bluetooth off while connecting.';
  $('deviceMeta').textContent = s.firmware ? `BOOP strap · Firmware ${s.firmware} · ${s.clock_verified ? 'Clock verified' : 'Checking clock'}` : 'Bluetooth connects directly to your strap.';
  $('heartRate').textContent = s.hr ?? '—';
  $('liveBadge').textContent = s.hr != null ? 'LIVE' : s.connected ? 'WAITING' : 'OFFLINE';
  $('liveBadge').classList.toggle('active', s.hr != null);
  $('hrDetail').textContent = s.sync.active ? 'Live readings resume after history sync.' : s.hr != null ? 'Updating from the strap in real time.' : s.connected ? 'Waiting for a reading. Keep the strap on your wrist.' : 'A live reading will appear after connection.';
  $('battery').textContent = s.battery ?? '—';
  $('batteryFill').style.width = (s.battery || 0) + '%';
  $('batteryDetail').textContent = s.battery_at ? `Read from strap at ${time(s.battery_at)}` : 'Read directly from the strap.';
  $('hrv').textContent = s.rmssd ?? '—';
  $('rrDetail').textContent = s.rmssd != null ? `5 min RMSSD · ${number(s.rr_count)} intervals` : s.rr_count ? `Waiting for usable intervals · ${number(s.rr_count)} received.` : 'This firmware has not sent beat-to-beat intervals yet.';
  $('readings').textContent = number(s.store.readings);
  $('savedDetail').textContent = `${number(s.store.live_readings)} live · ${number(s.store.history_readings)} historical`;
  $('sync').disabled = !s.connected || s.sync.active;
  $('sync').hidden = s.sync.active;
  $('stopSync').hidden = !s.sync.active;
  $('syncStatus').textContent = s.sync.active ? `Saving history · ${number(s.sync.records)} records · ${number(s.sync.chunks)} chunks saved` : s.sync.message === 'No history sync yet' ? 'Connect your strap to sync its stored history.' : brandText(s.sync.message);
  $('syncDot').classList.toggle('busy', s.sync.active);
  $('syncDot').classList.toggle('muted', !s.sync.active);
  $('historyNote').textContent = s.store.undated_history ? `${number(s.store.undated_history)} older readings have an invalid strap date. They are kept in your database and CSV; new readings use the corrected clock.` : '';
  $('storageSize').textContent = `${(s.store.bytes / 1048576).toFixed(2)} MB stored · ${number(s.store.raw_frames)} original packets`;
  $('log').replaceChildren(...s.logs.map(row => { const p = document.createElement('p'); p.textContent = `${time(row.time)} ${brandText(row.message)}`; return p; }));
  if (s.error) notice(s.error);
}
function draw(points) {
  lastChartPoints = points;
  $('chartEmpty').hidden = points.length > 0;
  $('chartCount').textContent = `${number(points.length)} points · Brisbane time`;
  const svg = $('chart'), bounds = svg.getBoundingClientRect();
  // Draw in rendered pixels so a compact chart keeps legible labels at every width.
  const w = Math.max(280, Math.round(bounds.width || 800)), h = Math.max(100, Math.round(bounds.height || 145));
  const left = 32, right = 10, top = 12, bottom = 24;
  svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
  const end = Date.now(), start = end - hours * 3600000;
  let lo = points.length ? Math.max(20, Math.floor((Math.min(...points.map(p => p.hr)) - 5) / 10) * 10) : 40;
  let hi = points.length ? Math.ceil((Math.max(...points.map(p => p.hr)) + 5) / 10) * 10 : 120;
  if (hi - lo < 30) { lo = Math.max(20, lo - 10); hi = lo + 30; }
  const x = t => left + (t - start) / (end - start) * (w - left - right);
  const y = hr => top + (hi - hr) / (hi - lo) * (h - top - bottom);
  let markup = '';
  for (let i=0;i<=3;i++) { const v = lo + (hi-lo)*i/3; markup += `<line x1="${left}" x2="${w-right}" y1="${y(v)}" y2="${y(v)}" stroke="var(--line)"/><text x="0" y="${y(v)+4}" font-size="11" fill="var(--soft)">${Math.round(v)}</text>`; }
  for (let i=0;i<=4;i++) { const t = start + (end-start)*i/4; markup += `<text x="${x(t)}" y="${h-3}" text-anchor="${i===0?'start':i===4?'end':'middle'}" font-size="11" fill="var(--soft)">${hours > 24 ? new Date(t).toLocaleDateString('en-AU',{timeZone:'Australia/Brisbane',day:'numeric',month:'short'}) : time(t)}</text>`; }
  if (points.length) {
    let path = '', prior = null;
    const gap = Math.max(120000, hours * 3600000 / 200);
    for (const p of points) { path += `${prior == null || p.t-prior > gap ? 'M':'L'}${x(p.t).toFixed(1)} ${y(p.hr).toFixed(1)} `; prior = p.t; }
    markup += `<path d="${path}" fill="none" stroke="var(--green)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`;
    const p = points.at(-1); markup += `<circle cx="${x(p.t)}" cy="${y(p.hr)}" r="3" fill="var(--green)" stroke="var(--card)" stroke-width="1.5"/>`;
  }
  svg.innerHTML = markup;
}
if (typeof ResizeObserver === 'function') {
  new ResizeObserver(() => { if (lastChartPoints !== null) draw(lastChartPoints); }).observe($('chart'));
}
async function refreshChart() {
  const [series, days] = await Promise.all([fetch('/api/series?hours='+hours).then(r=>r.json()),fetch('/api/days').then(r=>r.json())]);
  draw(series);
  if (days.length) $('days').replaceChildren(...days.map(day => {
    const row=document.createElement('tr');
    const date = new Date(day.day+'T12:00:00+10:00').toLocaleDateString('en-AU',{timeZone:'Australia/Brisbane',day:'numeric',month:'short',year:'numeric'});
    for (const value of [date,number(day.readings),day.avg_hr == null ? '—' : day.avg_hr+' bpm',day.min_hr == null ? '—' : `${day.min_hr}–${day.max_hr}`]) { const cell=document.createElement('td'); cell.textContent=value; row.append(cell); }
    return row;
  }));
}
async function refresh() {
  if (stopped) return;
  try {
    const response = await fetch('/api/status'); if (!response.ok) throw new Error('BOOP is unavailable');
    const s = await response.json();
    if ((connectionLost || state?.error) && !s.error) notice('');
    connectionLost = false;
    render(s);
    if (Date.now()-lastChart>4000) { await refreshChart(); lastChart=Date.now(); }
  } catch (error) { connectionLost = true; notice('BOOP is not running. Open the BOOP shortcut on your desktop to reconnect.'); }
}
async function scan() {
  $('scan').textContent='Scanning…'; $('scan').disabled=true; $('devices').replaceChildren();
  try { const result = await action('scan');
    if (!result.devices.length) { notice('No BOOP found. Turn your phone’s Bluetooth off and tap the strap until its blue light flashes, then Scan again.'); return; }
    notice('');
    for (const device of result.devices) { const button=document.createElement('button'); button.className='device-choice'; button.textContent=`Connect ${brandText(device.name)}`; button.addEventListener('click',async()=>{ await perform('connect',{address:device.address}); $('devices').replaceChildren(); }); $('devices').append(button); }
  } finally { $('scan').textContent='Scan'; $('scan').disabled=false; }
}
async function perform(name, data) { if (busy) return; busy=true; try { notice(''); await action(name,data); await refresh(); } catch(error) { notice(error.message); } finally { busy=false; } }
$('scan').addEventListener('click',()=>scan().catch(e=>notice(e.message)));
$('connect').addEventListener('click',()=> state?.address ? perform('connect',{address:state.address}) : scan().catch(e=>notice(e.message)));
for (const [id,name] of [['disconnect','disconnect'],['sync','sync'],['stopSync','stop-sync']]) $(id).addEventListener('click',()=>perform(name));
$('shutdown').addEventListener('click',async()=>{ try { await action('shutdown'); stopped=true; notice('BOOP has stopped. Your saved data is still on this laptop. Reopen the desktop shortcut to continue.'); } catch(error) { notice(error.message); } });
document.querySelectorAll('[data-hours]').forEach(button=>button.addEventListener('click',()=>{ hours=Number(button.dataset.hours); document.querySelectorAll('[data-hours]').forEach(b=>b.classList.toggle('selected',b===button)); refreshChart().catch(e=>notice(e.message)); }));
refresh(); setInterval(refresh,2000);
