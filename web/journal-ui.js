/* One local journal shared by the terminal and the full Insights page. */
(() => {
'use strict';
const el=id=>document.getElementById(id),esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const today=()=>new Date().toLocaleDateString('en-CA',{timeZone:'Australia/Brisbane'}),clock=t=>new Date(t*1000).toLocaleTimeString('en-AU',{timeZone:'Australia/Brisbane',hour:'2-digit',minute:'2-digit'});
const duration=m=>Math.floor(Math.round(m)/60)+':'+String(Math.round(m)%60).padStart(2,'0'),num=v=>Number.isFinite(v)?v.toLocaleString('en-AU',{maximumFractionDigits:1}):'—';
let current=today(),data=null,tab='checkin',analysis=null,requestId=0,analysisId=0,compactDate='',compactData=null,compactAt=0,compactPending='',editing=null,dirty=false,compactDirty=false;
let itemChoice='',outcome='sleep_min',alignment=1,patternView='habit',search='',comparisonMode='auto',changePeriod='week',heatmapSource='journal';
let compactRequestId=0;
const compactDrafts=new Map(),dailyDrafts=new Map();
const relativeDay=offset=>new Date(Date.parse(today()+'T12:00:00+10:00')+offset*86400000).toISOString().slice(0,10);
const shortDay=day=>new Date(day+'T12:00:00+10:00').toLocaleDateString('en-AU',{timeZone:'Australia/Brisbane',day:'numeric',month:'short'});
const metricNames={sleep_min:'Sleep minutes',resting_hr:'RHR',hrv:'RHRV',effort:'Effort',rest:'Sleep score'};
async function api(path,body,method='POST'){
  const response=await fetch(path,body===undefined?{}:{method,headers:{'Content-Type':'application/json','X-Boop':'local'},body:JSON.stringify(body)});
  const result=await response.json();if(!response.ok)throw Error(result.error||'The local journal could not save.');return result;
}
function tell(message,error=false){const n=el('journalStatus');n.textContent=message;n.classList.toggle('journal-error',error);}
function option(value,label,selected){return `<option value="${esc(value)}"${String(value)===String(selected)?' selected':''}>${esc(label)}</option>`;}
function valueLabel(row){
  const parts=[];if(typeof row.answer==='boolean')parts.push(row.answer?'Yes':'No');if(Number.isFinite(row.value))parts.push(num(row.value)+(row.unit?' '+row.unit:''));if(row.text)parts.push(row.text);if(row.product)parts.push(row.product);return parts.join(' · ')||'Logged';
}
function choices(name,label,value,options){
  return `<div class="journal-toggle" role="group" aria-label="${esc(label)}"><input type="hidden" name="${name}" value="${esc(value)}">${options.map(([v,title])=>`<button type="button" data-jchoice="${esc(v)}" aria-label="${esc(label+': '+(v===''?'Not logged':title))}" title="${esc(v===''?'Not logged':title)}" aria-pressed="${String(value)===String(v)}">${esc(title)}</button>`).join('')}</div>`;
}
function toggleAnswer(event){
  const button=event.target.closest('[data-jchoice]');if(!button)return false;
  const group=button.closest('.journal-toggle'),input=group.querySelector('input');input.value=button.dataset.jchoice;
  for(const choice of group.querySelectorAll('button'))choice.setAttribute('aria-pressed',String(choice===button));
  input.dispatchEvent(new Event('input',{bubbles:true}));return true;
}
function input(item,row={},prefix='entry'){
  const name=prefix+'-'+item.id,label=esc(item.name),value=row.value??'',text=row.text??'';
  if(['boolean','supplement'].includes(item.type))return choices('answer',item.name,row.answer===undefined?'':String(row.answer),[['','—'],['true',item.type==='supplement'?'Taken':'Yes'],['false',item.type==='supplement'?'Not taken':'No']]);
  if(item.type==='rating')return choices('value',item.name,value,[['','—'],...[1,2,3,4,5].map(n=>[n,String(n)])]);
  if(item.type==='choice')return choices('text',item.name,text,[['','—'],...item.options.map(v=>[v,v])]);
  if(['quantity','duration'].includes(item.type))return `<div class="journal-quantity"><input name="value" type="number" min="0" max="1000000" step="any" aria-label="${label}" value="${esc(value)}" placeholder="Not logged"><span>${esc(item.unit)}</span></div>`;
  return `<input name="text" type="${item.type==='time'?'time':'text'}" aria-label="${label}" value="${esc(text)}" maxlength="2000" placeholder="${item.type==='event'?'Event / observation':'Not logged'}">`;
}
function fieldRows(bundle,compact=false,cycleOnly=false){
  return bundle.items.filter(i=>i.enabled&&(!cycleOnly||i.category==='Cycle')).map(item=>{
    const entries=bundle.entries.filter(r=>r.item_id===item.id),row=entries.filter(r=>r.mode!=='event').sort((a,b)=>(a.updated_ms||0)-(b.updated_ms||0)).at(-1),events=entries.filter(r=>r.mode==='event'),carried=!entries.length?(bundle.carried||[]).find(r=>r.item_id===item.id):null;
    return `<div class="journal-field" data-jitem="${esc(item.id)}" data-prior="${esc(row?.id||'')}"><label>${esc(item.name)}${!compact?`<small>${esc(item.category)}</small>`:''}</label><div>${input(item,row||carried||undefined)}${carried?`<small class="journal-carried">Last ${esc(carried.day.slice(5))} · confirm for this day</small>`:''}${!compact&&['quantity','duration'].includes(item.type)?'<small>Daily total · or log individual events</small>':''}${events.length?`<small>${events.length} timed event${events.length===1?'':'s'} · ${esc(events.map(valueLabel).join(', '))}</small>`:''}</div>${!compact?`<button type="button" data-add-event="${esc(item.id)}" aria-label="Add ${esc(item.name)} event">+ event</button>`:''}</div>`;
  }).join('');
}
function historyRows(rows,limit=100){
  return rows.length?`<div class="journal-history">${[...rows].reverse().slice(0,limit).map(row=>`<div class="journal-history-row"><div><strong>${esc(row.question)}</strong><span>${esc(valueLabel(row))}</span>${row.note?`<small>${esc(row.note)}</small>`:''}<small>${esc(row.day)}${row.event_time?' · '+esc(row.event_time):' · time not logged'} · entered ${new Date(row.created_ms).toLocaleString('en-AU',{timeZone:'Australia/Brisbane'})}</small></div><button type="button" data-edit-entry="${esc(row.id)}">Edit</button><button type="button" data-remove-entry="${esc(row.id)}">Remove</button></div>`).join('')}</div>`:'<p class="journal-muted">No entries yet. Skipped questions remain unknown.</p>';
}
function renderCheckin(){
  const count=data.items.filter(i=>i.enabled).length;
  return `<div class="journal-section-head"><div><h2>Your check-in</h2><p>${data.logged} of ${data.enabled} items confirmed · previous answers stay unlogged until you confirm</p></div><button type="button" data-jtab="track">Edit tracking</button></div>
    <form id="journalDailyForm" data-jform="daily"><div class="journal-fields">${fieldRows(data)}</div>${!count?'<p>Choose what you want to track to start your journal.</p>':''}<div class="journal-actions"><button type="submit" class="primary">Confirm &amp; save</button><button type="button" data-add-note>Add note</button></div></form>
    <section class="journal-card"><h2>Entries for ${esc(current)}</h2>${historyRows(data.entries)}</section>`;
}
function renderTrack(){
  const enabled=data.items.filter(i=>i.enabled),categories=[...new Set(data.items.map(i=>i.category))];
  return `<div class="journal-section-head"><div><h2>Choose your tracking</h2><p>Only enabled items appear in your check-in. Turning one off keeps its history.</p></div></div>
    <div class="journal-track-layout"><section class="journal-card"><h3>Your order</h3>${enabled.length?enabled.map((i,index)=>`<div class="journal-order-row"><span>${esc(i.name)}</span><button type="button" data-move="${esc(i.id)}" data-direction="-1" aria-label="Move ${esc(i.name)} up"${index===0?' disabled':''}>↑</button><button type="button" data-move="${esc(i.id)}" data-direction="1" aria-label="Move ${esc(i.name)} down"${index===enabled.length-1?' disabled':''}>↓</button></div>`).join(''):'<p>No items enabled.</p>'}</section>
    <section class="journal-card"><label class="journal-search">Search catalogue<input id="journalSearch" type="search" value="${esc(search)}" placeholder="Coffee, sleep, cycle, supplements…"></label><div id="journalCatalogue">${categories.map(category=>`<details open><summary>${esc(category)}</summary><div class="journal-catalogue-group">${data.items.filter(i=>i.category===category).map(i=>`<label class="journal-choice" data-search="${esc((i.name+' '+i.category).toLowerCase())}"><input type="checkbox" data-enable="${esc(i.id)}"${i.enabled?' checked':''}><span>${esc(i.name)}<small>${esc(i.type==='supplement'?'Taken + optional amount':i.type)}${i.unit?' · '+esc(i.unit):''}</small></span></label>`).join('')}</div></details>`).join('')}</div></section></div>
    <form class="journal-card journal-custom" data-jform="custom"><h3>Create a custom item</h3><label>Name<input name="name" required maxlength="80"></label><label>Input<select name="type">${[['boolean','Yes / No'],['rating','Rating 1–5'],['quantity','Quantity'],['time','Time'],['duration','Duration'],['event','Event'],['supplement','Supplement'],['text','Notes']].map(([v,n])=>option(v,n,'boolean')).join('')}</select></label><label>Unit (optional)<input name="unit" maxlength="40" placeholder="g, mL, minutes…"></label><button type="submit">Add & enable</button></form>`;
}
function plot(points,{kind='line',height=160,xLabel='',yLabel='',signed=false,maxGap=1.01}={}){
  const usable=points.filter(p=>Number.isFinite(p.x)&&Number.isFinite(p.y));if(!usable.length)return '<div class="journal-chart-empty">Waiting for observed measurements</div>';
  const w=620,left=46,right=610,top=14,bottom=height-32,xs=usable.map(p=>p.x),ys=usable.map(p=>p.y),minX=Math.min(...xs),maxX=Math.max(...xs),minY=Math.min(signed||kind==='bar'?0:Infinity,...ys),maxY=Math.max(signed?0:-Infinity,...ys),spanX=maxX-minX||1,spanY=maxY-minY||1;
  const x=v=>left+(v-minX)/spanX*(right-left),y=v=>bottom-(v-minY)/spanY*(bottom-top);
  let body=[minY,(minY+maxY)/2,maxY].map(v=>`<line x1="${left}" x2="${right}" y1="${y(v)}" y2="${y(v)}" class="journal-gridline"/><text x="${left-7}" y="${y(v)+4}" text-anchor="end">${esc(num(v))}</text>`).join('');
  if(kind==='line'){let previous=null,path='';for(const p of usable){path+=(previous===null||p.x-previous>maxGap?'M':'L')+x(p.x)+' '+y(p.y)+' ';previous=p.x;}body+=`<path d="${path}" class="journal-line"/>`;}
  if(kind==='bar')body+=usable.map(p=>`<rect x="${x(p.x)-4}" y="${y(p.y)}" width="8" height="${bottom-y(p.y)}" class="journal-dot"><title>${esc(p.label)}</title></rect>`).join('');
  body+=usable.map(p=>`<circle cx="${x(p.x)}" cy="${y(p.y)}" r="3.5" class="journal-dot"><title>${esc(p.label||num(p.y))}</title></circle>`).join('');
  const labels=[usable[0],usable.at(-1)];body+=labels.map((p,i)=>`<text x="${i?right:left}" y="${height-15}" text-anchor="${i?'end':'start'}">${esc(p.tick??num(p.x))}</text>`).join('');
  return `<svg viewBox="0 0 ${w} ${height}" role="img" aria-label="${esc(yLabel+' against '+xLabel)}">${body}<text x="${left}" y="10">${esc(yLabel)}</text><text x="${(left+right)/2}" y="${height-1}" text-anchor="middle">${esc(xLabel)}</text></svg>`;
}
function evidenceTable(pairs,exposure=true){return pairs.length?`<details class="journal-evidence"><summary>Measurements & dates (${pairs.length})</summary><div class="journal-table"><table><thead><tr><th>Journal date</th>${exposure?'<th>Answer / amount</th>':''}<th>Outcome date</th><th>Measurement</th><th>Source</th></tr></thead><tbody>${pairs.map(p=>`<tr><td>${esc(p.day)}</td>${exposure?`<td>${esc(typeof p.exposure==='boolean'?p.exposure?'Yes':'No':num(p.exposure))}</td>`:''}<td>${esc(p.outcome_day||p.day)}</td><td>${esc(num(p.value))}</td><td>${esc(p.source||'BOOP local estimate')}</td></tr>`).join('')}</tbody></table></div></details>`:'';}
function analysisControls(){
  return `<div class="journal-analysis-controls"><label>View<select id="journalPatternView">${[['habit','Habit / quantity'],['timeline','Daily timeline'],['sleep','Sleep schedule & consistency'],['heatmap','Calendar heatmap'],['change','Personal change']].map(([v,n])=>option(v,n,patternView)).join('')}</select></label><label>Item<select id="journalCompareItem">${data.items.filter(i=>i.enabled||analysis?.entries.some(r=>r.item_id===i.id)).map(i=>option(i.id,i.name,itemChoice||analysis?.selected.id)).join('')}</select></label><label>Outcome<select id="journalOutcome">${Object.entries(metricNames).map(([v,n])=>option(v,n,outcome)).join('')}</select></label><label>Align<select id="journalAlignment">${option(1,'Following day',alignment)}${option(0,'Same day',alignment)}</select></label><label>Compare<select id="journalComparisonMode">${option('auto','Answers / amounts',comparisonMode)}${option('quantity','Supplement amounts',comparisonMode)}</select></label>${patternView==='heatmap'?`<label>Colour by<select id="journalHeatmapSource">${option('journal','Journal item',heatmapSource)}${option('sensor','Sensor outcome',heatmapSource)}</select></label>`:''}${patternView==='change'?`<label>Period<select id="journalChangePeriod">${option('week','7 vs previous 7 days',changePeriod)}${option('month','30 vs previous 30 days',changePeriod)}</select></label>`:''}</div>`;
}
function renderPatterns(){
  if(!analysis)return analysisControls()+'<p class="journal-muted">Loading real history…</p>';
  const c=analysis.comparison,days=analysis.days,observed=days.filter(d=>Object.values(metricNames).length&&Number.isFinite(d[outcome]));
  let body='';
  if(patternView==='habit'){
    const numeric=c.quantity,state=c.status==='observed'?'Observed association':'Learning';
    body=`<h2>${esc(analysis.selected.name)} & ${esc(metricNames[outcome])}</h2><p>${esc(state)} · ${alignment?'following-day':'same-day'} measurements</p><div class="journal-summary-row"><span><strong>${c.logged_days}</strong> logged days</span><span><strong>${c.paired_days}</strong> paired days</span><span><strong>${c.calendar_days-c.logged_days}</strong> unlogged days</span><span><strong>${c.missing_outcomes}</strong> unpaired logs</span></div>`;
    if(numeric){body+=plot(c.pairs.map(p=>({x:p.exposure,y:p.value,label:`${p.day}: ${p.exposure} ${c.exposure_unit||''} → ${p.outcome_day}: ${p.value} ${c.unit}`})),{kind:'scatter',xLabel:analysis.selected.name+' '+(c.exposure_unit||''),yLabel:metricNames[outcome]+' '+c.unit});if(Number.isFinite(c.correlation))body+=`<p>Correlation ${num(c.correlation)} across ${c.paired_days} pairs.</p>`;}
    else body+=`<div class="journal-comparison"><div><span>Logged Yes</span><strong>${num(c.yes_mean)} <small>${esc(c.unit)}</small></strong><small>${c.yes_n} observed days</small></div><div><span>Logged No</span><strong>${num(c.no_mean)} <small>${esc(c.unit)}</small></strong><small>${c.no_n} observed days</small></div></div>${Number.isFinite(c.delta)?`<p>Observed difference: ${c.delta>=0?'+':''}${num(c.delta)} ${esc(c.unit)}.</p>`:''}`;
    body+=`<p class="journal-muted">${esc(c.note)}</p>`+evidenceTable(c.pairs);
    if(analysis.prior_baseline.length){const values=analysis.prior_baseline.map(d=>d[outcome]);body+=`<details><summary>Before your first log: ${values.length}/7 sensor days · mean ${num(values.reduce((a,b)=>a+b,0)/values.length)} ${esc(c.unit)}</summary><p>These earlier days provide sensor context. Their habit status is unknown.</p>${evidenceTable(analysis.prior_baseline.map(d=>({day:d.day,value:d[outcome],source:d.sources[outcome]})),false)}</details>`;}
  }else if(patternView==='change'){
    body='<h2>Last '+analysis.change_window+' vs previous '+analysis.change_window+' days</h2>'+analysis.changes.map(change=>`<div class="journal-change"><strong>${esc(change.name)}</strong><span>${change.delta==null?'Learning':(change.delta>=0?'+':'')+num(change.delta)+' '+change.unit}</span><small>${change.current.length}/${analysis.change_window} current · ${change.previous.length}/${analysis.change_window} previous</small>${evidenceTable([...change.previous,...change.current],false)}</div>`).join('')+'<p class="journal-muted">At least three observed days in each period are needed. Missing measurements stay missing.</p>';
  }else if(patternView==='heatmap'){
    const grouped=new Map();for(const r of analysis.entries.filter(r=>r.item_id===analysis.selected.id)){if(!grouped.has(r.day))grouped.set(r.day,[]);grouped.get(r.day).push(r);}
    const journalMetric=heatmapSource==='journal',type=analysis.selected.type;
    const values=days.map(d=>{if(!journalMetric)return d[outcome];let rows=grouped.get(d.day)||[];if(!rows.length)return undefined;
      if(['boolean','supplement'].includes(type)){const explicit=rows.filter(r=>typeof r.answer==='boolean');return explicit.length?Number(explicit.some(r=>r.answer)):undefined;}
      if(['rating','quantity','duration'].includes(type)){let nums=rows.filter(r=>Number.isFinite(r.value));if(type!=='rating'){const daily=nums.filter(r=>r.mode==='daily').sort((a,b)=>(a.updated_ms||0)-(b.updated_ms||0));if(daily.length)nums=[daily.at(-1)];}return nums.length?nums.reduce((s,r)=>s+r.value,0)/(type==='rating'?nums.length:1):undefined;}
      return rows.map(r=>r.text).filter(Boolean).join(' · ')||undefined;});
    const numeric=values.filter(Number.isFinite),low=Math.min(...numeric),high=Math.max(...numeric),padding=(new Date(analysis.first+'T12:00:00+10:00').getUTCDay()+6)%7;
    const label=v=>v==null?'—':journalMetric&&['boolean','supplement'].includes(type)?v?'Yes':'No':typeof v==='string'?v:num(v);
    body=`<h2>${esc(journalMetric?analysis.selected.name:metricNames[outcome])} calendar</h2><p>${analysis.first} → ${analysis.through} · ${values.filter(v=>v!=null).length}/${days.length} observed days</p><div class="journal-heatmap">${['M','T','W','T','F','S','S'].map(d=>`<span class="journal-weekday">${d}</span>`).join('')}${'<span></span>'.repeat(padding)}${days.map((d,i)=>`<button type="button" data-jday="${d.day}" aria-label="${esc(d.day+': '+label(values[i]))}" title="${esc(d.day+': '+label(values[i]))}" style="--intensity:${values[i]==null?0:typeof values[i]==='string'?.6:.2+.8*(values[i]-low)/(high-low||1)}"><span>${d.day.slice(8)}</span><small>${esc(label(values[i]))}</small></button>`).join('')}</div><p class="journal-muted">Blank cells are unknown. Select a date to inspect or correct it.</p>`;
  }else if(patternView==='sleep'){
    const sleep=days.filter(d=>Number.isFinite(d.bedtime)&&Number.isFinite(d.wake));const points=sleep.map(d=>{let bed=(d.bedtime/3600+10)%24;if(bed<12)bed+=24;return {day:d.day,bed,wake:bed+(d.wake-d.bedtime)/3600,total:d.sleep_min};});
    body='<h2>Sleep schedule</h2>'+plot(days.map((d,i)=>({x:i,y:d.sleep_min,tick:d.day.slice(5),label:d.day+': '+num(d.sleep_min)+' min'})),{kind:'bar',xLabel:'Date',yLabel:'Sleep / min'});
    if(points.length){const spread=key=>{const a=points.map(p=>p[key]),mean=a.reduce((a,b)=>a+b,0)/a.length;return Math.sqrt(a.reduce((s,v)=>s+(v-mean)**2,0)/a.length)*60;};body+=`<p>Bedtime variation ${num(spread('bed'))} min · wake variation ${num(spread('wake'))} min · ${points.length} nights</p><p class="journal-muted">Variation is the standard deviation of the observed clock times.</p><div class="journal-table"><table><thead><tr><th>Night</th><th>Bedtime</th><th>Wake</th><th>Asleep</th></tr></thead><tbody>${sleep.map(d=>`<tr><td>${d.day}</td><td>${clock(d.bedtime)}</td><td>${clock(d.wake)}</td><td>${Number.isFinite(d.sleep_min)?duration(d.sleep_min):'—'}</td></tr>`).join('')}</tbody></table></div>`;}
  }else{
    const events=analysis.entries.filter(r=>r.day===current),day=days.find(d=>d.day===current);body=`<h2>${esc(current)} timeline</h2><p>${day&&Number.isFinite(day.sleep_min)?'Sleep '+duration(day.sleep_min)+' · ':''}${events.length} journal entries</p><div id="journalDayTimeline">Loading recorded HR…</div>${historyRows(events)}<p class="journal-muted">Only explicitly logged event times are placed on the timeline; untimed entries remain in the list.</p>`;
    setTimeout(()=>drawDayTimeline(events),0);
  }
  return analysisControls()+`<section class="journal-card">${body}</section>`;
}
async function drawDayTimeline(events){
  const target=el('journalDayTimeline');if(!target)return;const selected=current,start=Date.parse(selected+'T00:00:00+10:00');
  try{const points=await api('/api/series?start='+start+'&end='+(start+86400000));if(target!==el('journalDayTimeline')||selected!==current)return;
    target.innerHTML=plot(points.map(p=>({x:(p.t-start)/3600000,y:p.hr,label:clock(p.t/1000)+' · '+p.hr+' bpm'})),{xLabel:'Hour / Brisbane',yLabel:'HR / bpm',maxGap:2/60});
    const svg=target.querySelector('svg');if(svg&&points.length){const min=Math.min(...points.map(p=>(p.t-start)/3600000)),max=Math.max(...points.map(p=>(p.t-start)/3600000));for(const event of events.filter(r=>r.event_time)){const hour=Number(event.event_time.slice(0,2))+Number(event.event_time.slice(3))/60;if(hour<min||hour>max)continue;const n=document.createElementNS('http://www.w3.org/2000/svg','line');n.setAttribute('x1',46+(hour-min)/(max-min||1)*564);n.setAttribute('x2',n.getAttribute('x1'));n.setAttribute('y1','14');n.setAttribute('y2','128');n.setAttribute('class','journal-event-line');const title=document.createElementNS(n.namespaceURI,'title');title.textContent=event.event_time+' '+event.question+': '+valueLabel(event);n.append(title);svg.append(n);}}
  }catch{target.textContent='HR timeline unavailable. Your journal entries are still shown.';}
}
function renderCycle(){
  const cycle=analysis?.cycle,enabled=data.items.some(i=>i.category==='Cycle'&&i.enabled);
  return `<section class="journal-card"><div class="journal-section-head"><div><h2>Cycle journal</h2><p>Optional bleeding, symptoms and personal observations.</p></div><button type="button" data-cycle-setup>Choose cycle items</button></div>${cycle?`<p>${cycle.day?'Day '+cycle.day+' since your last logged period start.':'No period start logged.'}</p>${cycle.lengths.length?`<p>Observed start-to-start intervals: ${cycle.lengths.map(n=>n+' days').join(', ')}.</p>`:''}`:'<p>Loading cycle history…</p>'}
    ${enabled?`<form data-jform="daily"><div class="journal-fields">${fieldRows(data,false,true)}</div><button type="submit" class="primary">Confirm cycle check-in</button></form>`:'<p class="journal-muted">Enable only the items you want to record. Nothing is inferred from skipped entries.</p>'}
    <p class="journal-muted">Cycle dates and temperature are observations; this view does not identify ovulation or fertile days.</p></section><section class="journal-card"><h2>Recorded cycle history</h2>${historyRows(cycle?.entries||[])}</section>`;
}
function render(){
  if(!data)return;el('journalDate').value=current;for(const b of el('journalTabs').querySelectorAll('button'))b.setAttribute('aria-pressed',String(b.dataset.jtab===tab));
  el('journalBody').innerHTML=tab==='track'?renderTrack():tab==='patterns'?renderPatterns():tab==='cycle'?renderCycle():renderCheckin();filterCatalogue();dirty=false;
  if(['checkin','cycle'].includes(tab)&&dailyDrafts.has(current)){restoreDraft(el('journalBody'),dailyDrafts.get(current));dirty=true;}
  for(const button of root.querySelectorAll('[data-journal-day]'))button.setAttribute('aria-pressed',String(current===relativeDay(Number(button.dataset.journalDay))));
}
function captureDraft(form){return [...form.querySelectorAll('[data-jitem]')].map(field=>({item:field.dataset.jitem,value:field.querySelector('[name=answer],[name=value],[name=text]').value}));}
function restoreDraft(form,draft){for(const field of form.querySelectorAll('[data-jitem]')){const saved=draft.find(r=>r.item===field.dataset.jitem);if(!saved)continue;field.querySelector('[name=answer],[name=value],[name=text]').value=saved.value;for(const b of field.querySelectorAll('[data-jchoice]'))b.setAttribute('aria-pressed',String(b.dataset.jchoice===saved.value));}}
async function selectJournalDay(day){
  if(!day||day===current)return;
  if(dirty&&['checkin','cycle'].includes(tab))dailyDrafts.set(current,captureDraft(el('journalBody')));
  current=day;el('journalDate').value=day;analysis=null;data=null;dirty=false;el('journalEditor').hidden=true;el('journalBody').textContent='Loading journal for '+day+'…';tell('Logging for '+day+'. Confirm & save submits that date.');await load(true);
}
function filterCatalogue(){for(const row of el('journalCatalogue')?.querySelectorAll('[data-search]')||[])row.hidden=!row.dataset.search.includes(search.toLowerCase());for(const details of el('journalCatalogue')?.querySelectorAll('details')||[])details.hidden=![...details.querySelectorAll('[data-search]')].some(r=>!r.hidden);}
async function load(force=false){
  const id=++requestId,selected=current;try{const result=await api('/api/journal?date='+selected);if(id!==requestId)return;data=result;if(force||!dirty)render();if(['patterns','cycle'].includes(tab))loadAnalysis();}catch(error){tell(error.message,true);}
}
async function loadAnalysis(){
  const id=++analysisId,selected=current;try{const result=await api('/api/journal/insights?date='+selected+'&days=30&item='+encodeURIComponent(itemChoice)+'&metric='+outcome+'&lag='+alignment+'&mode='+comparisonMode+'&period='+changePeriod);if(id!==analysisId||selected!==current)return;analysis=result;itemChoice=result.selected.id;if(['patterns','cycle'].includes(tab)&&!dirty)render();}catch(error){tell(error.message,true);}
}
function readFields(form,bundle){
  const entries=[],clear_ids=[];
  for(const field of form.querySelectorAll('[data-jitem]')){const spec=bundle.items.find(i=>i.id===field.dataset.jitem),raw=field.querySelector('[name=answer],[name=value],[name=text]'),value=raw.value,prior=bundle.entries.find(r=>r.id===field.dataset.prior);
    if(value===''){if(prior)clear_ids.push(prior.id);continue;}
    const carried=(bundle.carried||[]).find(r=>r.item_id===spec.id),entry={...(prior||{}),item_id:spec.id,mode:prior?.mode||'daily'};if(!prior&&carried&&spec.type==='supplement')for(const key of ['value','unit','product'])if(carried[key]!==undefined)entry[key]=carried[key];
    if(raw.name==='answer')entry.answer=value==='true';else if(raw.name==='value')entry.value=Number(value);else entry.text=value;
    entries.push(entry);
  }return {entries,clear_ids};
}
async function saveForm(form,bundle,compact=false){
  const body=readFields(form,bundle);if(!body.entries.length&&!body.clear_ids.length)throw Error('Choose at least one answer to confirm. This day is still unlogged.');await api('/api/journal',{day:bundle.day,...body});
  if(compact){compactDrafts.delete(bundle.day);if(compactDate===bundle.day)compactDirty=false;}else{dailyDrafts.delete(bundle.day);if(current===bundle.day)dirty=false;}
  tell('Confirmed for '+bundle.day+'. Unanswered items remain unknown.');analysis=null;await refreshCompact(compactDate||today(),true);if(current===bundle.day)await load(true);
}
function editor(itemId,row){
  const item=data.items.find(i=>i.id===itemId);if(!item)return;editing=row||null;
  el('journalEditor').hidden=false;el('journalEditor').innerHTML=`<form data-jform="event" data-item="${esc(item.id)}"><div class="journal-section-head"><h2>${row?'Edit':'Add'} ${esc(item.name)}</h2><button type="button" data-close-editor>Close</button></div><div class="journal-event-fields"><label>Date<input name="day" type="date" required value="${esc(row?.day||current)}"></label><label>Event time (optional)<input name="event_time" type="time" value="${esc(row?.event_time||'')}"></label><label>${esc(item.name)}${input(item,row)}</label>${item.type==='supplement'?`<label>Product<input name="product" maxlength="120" value="${esc(row?.product||'')}"></label><label>Amount (optional)<input name="amount" type="number" min="0" step="any" value="${esc(row?.value??'')}"></label><label>Unit<input name="unit" maxlength="40" value="${esc(row?.unit||'')}" placeholder="mg, IU, tablets…"></label>`:''}<label>Note<textarea name="note" maxlength="2000">${esc(row?.note||'')}</textarea></label></div><button type="submit" class="primary">Save entry</button><p class="journal-muted">Event time is kept separately from when you enter or edit this record.</p></form>`;
  el('journalEditor').scrollIntoView({block:'center',behavior:'smooth'});el('journalEditor').querySelector('input')?.focus();
}
async function configure(items,custom){await api('/api/journal',{action:'configure',items,...(custom?{custom}:{})});await load(true);await refreshCompact(compactDate||current,true);tell('Tracking preferences saved.');}
async function onClick(event){
  if(toggleAnswer(event))return;const button=event.target.closest('button');if(!button)return;
  try{
    if(button.hasAttribute('data-journal-day'))await selectJournalDay(relativeDay(Number(button.dataset.journalDay)));
    if(button.dataset.jtab){tab=button.dataset.jtab;render();if(['patterns','cycle'].includes(tab))loadAnalysis();}
    if(button.hasAttribute('data-cycle-setup')){search='Cycle';tab='track';render();}
    if(button.dataset.move){const enabled=data.items.filter(i=>i.enabled),i=enabled.findIndex(r=>r.id===button.dataset.move),other=i+Number(button.dataset.direction);[enabled[i],enabled[other]]=[enabled[other],enabled[i]];await configure([...enabled,...data.items.filter(i=>!i.enabled)].map((item,index)=>({id:item.id,order:index,enabled:item.enabled})));}
    if(button.dataset.addEvent)editor(button.dataset.addEvent);
    if(button.hasAttribute('data-add-note'))editor('custom_notes');
    if(button.dataset.editEntry){const row=[...data.entries,...(analysis?.entries||[])].find(r=>r.id===button.dataset.editEntry);if(row)editor(row.item_id,row);}
    if(button.dataset.removeEntry){const id=button.dataset.removeEntry;await api('/api/records/journal/'+encodeURIComponent(id),{},'DELETE');analysis=null;await load(true);await refreshCompact(compactDate||current,true);tell('Entry removed.');const undo=document.createElement('button');undo.textContent='Undo';undo.onclick=async()=>{await api('/api/records/journal/'+encodeURIComponent(id)+'/undo',{});await load(true);await refreshCompact(compactDate||current,true);tell('Entry restored.');};el('journalStatus').append(' ',undo);}
    if(button.hasAttribute('data-close-editor')){el('journalEditor').hidden=true;editing=null;}
    if(button.dataset.jday){tab='checkin';await selectJournalDay(button.dataset.jday);render();}
  }catch(error){tell(error.message,true);}
}
async function onSubmit(event){
  const form=event.target;if(!form.dataset.jform)return;event.preventDefault();event.stopPropagation();const button=form.querySelector('[type=submit]');button.disabled=true;
  try{
    if(form.dataset.jform==='daily')await saveForm(form,data);
    if(form.dataset.jform==='custom'){const values=Object.fromEntries(new FormData(form));await configure([],values);}
    if(form.dataset.jform==='event'){
      const values=Object.fromEntries(new FormData(form)),item=data.items.find(i=>i.id===form.dataset.item),entry={item_id:item.id,mode:editing?.mode||'event',...(editing?{id:editing.id}:{}),event_time:values.event_time,note:values.note,product:values.product||'',unit:values.unit||item.unit||''};
      if(values.answer!==undefined){if(values.answer==='')throw Error('Choose an answer before saving the event.');entry.answer=values.answer==='true';}
      if(values.value!==undefined){if(values.value==='')throw Error('Enter a value before saving.');entry.value=Number(values.value);}
      if(values.text!==undefined)entry.text=values.text;
      if(values.amount)entry.value=Number(values.amount);
      await api('/api/journal',{day:values.day,entries:[entry]});el('journalEditor').hidden=true;editing=null;analysis=null;await load(true);await refreshCompact(compactDate||current,true);tell('Entry saved locally.');
    }
  }catch(error){tell(error.message,true);}finally{button.disabled=false;}
}
function renderCompact(){
  const target=el('termJournalContent');if(!target||!compactData||compactDirty)return;
  target.innerHTML=`<form id="termJournalForm"><div class="term-journal-fields">${fieldRows(compactData,true)}</div><div class="term-journal-actions"><span>${compactData.logged}/${compactData.enabled} confirmed</span><button type="submit" aria-label="Confirm journal for ${compactData.day}">[ Confirm ]</button><button type="button" id="termJournalOpen">[ Open Insights ]</button></div><p class="term-journal-note" id="termJournalNote">Confirm records ${shortDay(compactData.day)} · prior answers are drafts</p></form>`;
  if(compactDrafts.has(compactDate)){restoreDraft(el('termJournalForm'),compactDrafts.get(compactDate));compactDirty=true;}
  el('termJournalForm').onclick=event=>{toggleAnswer(event);};
  el('termJournalForm').oninput=()=>compactDirty=true;
  el('termJournalForm').onsubmit=async event=>{event.preventDefault();event.stopPropagation();const b=event.target.querySelector('[type=submit]'),bundle=compactData;b.disabled=true;try{await saveForm(event.target,bundle,true);if(compactDate===bundle.day)el('termJournalNote').textContent='Confirmed for '+bundle.day+'.';}catch(error){if(compactDate===bundle.day&&el('termJournalNote'))el('termJournalNote').textContent=error.message;}finally{b.disabled=false;}};
  el('termJournalOpen').onclick=()=>{tab='checkin';location.hash='#insights';if(current!==compactDate)selectJournalDay(compactDate);else load(true);};
}
async function selectCompactDay(day){
  if(!day||day===compactDate)return;
  if(compactDirty&&el('termJournalForm'))compactDrafts.set(compactDate,captureDraft(el('termJournalForm')));
  compactDirty=false;await refreshCompact(day,true);
}
function compactDateControls(day){
  const controls=el('termJournalDateControls');if(!controls)return;
  el('termJournalDate').value=day;
  el('termJournalDate').onchange=event=>selectCompactDay(event.target.value);
  for(const button of controls.querySelectorAll('[data-journal-day]')){const date=relativeDay(Number(button.dataset.journalDay));button.setAttribute('aria-pressed',String(day===date));button.onclick=()=>selectCompactDay(relativeDay(Number(button.dataset.journalDay)));}
}
async function refreshCompact(day,force=false){
  if(!el('termJournalContent'))return;
  if(!force&&compactDate===day&&(compactPending===day||compactDirty||Date.now()-compactAt<60000))return;
  if(compactDate!==day){compactDirty=false;compactData=null;el('termJournalContent').textContent='Reading your journal…';}
  compactDate=day;compactPending=day;compactDateControls(day);const id=++compactRequestId;
  try{const result=await api('/api/journal?date='+day);if(compactDate===day&&id===compactRequestId){compactData=result;compactAt=Date.now();renderCompact();}}
  catch(error){if(compactDate===day&&id===compactRequestId)el('termJournalContent').textContent=error.message;}
  finally{if(id===compactRequestId)compactPending='';}
}
const page=el('insights'),legacy=document.createElement('details');legacy.id='journalLegacy';legacy.innerHTML='<summary>More sensor analyses & experiments</summary>';
for(const child of [...page.children])if(!child.classList.contains('page-heading'))legacy.append(child);
const root=document.createElement('div');root.id='journalApp';root.innerHTML=`<div class="journal-toolbar"><p>Your daily context, alongside your measured history.</p><div class="journal-date-shortcuts"><label>Journal date<input type="date" id="journalDate" value="${current}"></label><button type="button" data-journal-day="-1">Yesterday</button><button type="button" data-journal-day="0">Today</button></div></div><div id="journalTabs" class="journal-tabs" role="group" aria-label="Insights sections">${[['checkin','Check-in'],['track','Tracking'],['patterns','Patterns'],['cycle','Cycle']].map(([id,name])=>`<button type="button" data-jtab="${id}" aria-pressed="${id==='checkin'}">${name}</button>`).join('')}</div><div id="journalStatus" role="status" aria-live="polite"></div><section id="journalEditor" class="journal-card" hidden></section><div id="journalBody">Loading your journal…</div>`;page.append(root,legacy);
root.addEventListener('click',onClick);root.addEventListener('submit',onSubmit);
root.addEventListener('input',event=>{if(event.target.id==='journalSearch'){search=event.target.value;filterCatalogue();}else if(event.target.closest('[data-jform]'))dirty=true;});
root.addEventListener('change',async event=>{const target=event.target;
  try{if(target.dataset.enable)await configure([{id:target.dataset.enable,enabled:target.checked}]);
    if(target.id==='journalDate')await selectJournalDay(target.value);
    if(['journalCompareItem','journalOutcome','journalAlignment','journalPatternView','journalComparisonMode','journalChangePeriod','journalHeatmapSource'].includes(target.id)){
      itemChoice=el('journalCompareItem').value;outcome=el('journalOutcome').value;alignment=Number(el('journalAlignment').value);patternView=el('journalPatternView').value;comparisonMode=el('journalComparisonMode').value;changePeriod=el('journalChangePeriod')?.value||changePeriod;heatmapSource=el('journalHeatmapSource')?.value||heatmapSource;
      if(['journalPatternView','journalHeatmapSource'].includes(target.id)){render();}else{analysis=null;render();loadAnalysis();}
    }
  }catch(error){tell(error.message,true);}
});
legacy.addEventListener('toggle',()=>{if(legacy.open)window.dispatchEvent(new CustomEvent('boop:legacy-insights'));});
window.BoopJournal={open:()=>load(),refreshCompact:(_dashboardDay,force=false)=>refreshCompact(compactDate||today(),force)};
if(location.hash==='#insights')load();
})();
