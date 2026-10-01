/* Additional single-page working surface backed by the existing local API. */
(() => {
'use strict';
const root=document.getElementById('terminal'), el=id=>document.getElementById(id);
// Settings is rendered by dashboard.js before this script runs; move the same
// controls so navigating between views preserves request-scoped credentials.
el('settings').insertBefore(el('termAISettings'),el('settings').children[1]||null);
let status=null, daily=null, inFlight=false, pending=false, dayPending=false, lastDay=0, lastGraph=0;
let aiController=null,aiSession=null;
const chartMath=window.BoopTerminalMath;
const profile={resting_hr:60,max_hr:190};
let hourPoints=[],lastHour=0,monthlyHR=null,lastMonthlyHR=0;
let liveMotion=null,lastMotion=0,motionPeriod=-1;
let personalBaseline=null,baselinePending='',focusedNight='',compareNight='week',sleepAlignment='onset';
let hrModel=null,sleepModel=null,lastEvidence=[],activeDetails=null;
const initialInspect='Hover or focus a graph · arrows scrub · Esc clears';
let realNights=[],sleepKey='',sleepPending='';
const hrPeriods=[{hours:1/6,label:'10 min'},{hours:1,label:'1 hour'},{hours:24,label:'24 hours'}];
let hrPeriod=0, graphPending=false,graphController=null,graphGeneration=0,monthlyPending=false,hourPending=false;
const hrCache=new Map();
const today=()=>new Date().toLocaleDateString('en-CA',{timeZone:'Australia/Brisbane'});
const fmt=v=>typeof v==='number'?v.toLocaleString('en-AU',{maximumFractionDigits:1}):String(v??'—');
const time=v=>new Date(v).toLocaleTimeString('en-AU',{timeZone:'Australia/Brisbane',hour:'2-digit',minute:'2-digit',second:'2-digit'});
const visible=()=>location.hash==='#terminal';
el('termDate').value=today();
const patterns=window.BoopTerminalPatterns;
patterns?.init({request,selectNight:day=>{focusedNight=day;drawSleep(cachedSleep);}});
function output(message){el('termOutput').textContent=message;}
async function request(path,body,options={}){
  const response=await fetch(path,body===undefined?options:{...options,method:'POST',headers:{'Content-Type':'application/json','X-Boop':'local'},body:JSON.stringify(body)});
  let data;try{data=await response.json();}catch{throw Error('The local service returned an unreadable response.');}
  if(!response.ok)throw Error(data.error||`Request failed (${response.status})`);
  return data;
}
function row(target,label,value,reason,collapsed=false){
  const line=document.createElement('div');line.className='term-row';
  const key=document.createElement('span');key.textContent=label;
  const cell=document.createElement('span'),reading=document.createElement('strong');reading.textContent=value;cell.append(reading);
  if(reason){
    if(collapsed){const details=document.createElement('details');details.className='term-explanation';const summary=document.createElement('summary');summary.textContent='why';const note=document.createElement('p');note.className='term-reason';note.textContent=reason;details.append(summary,note);cell.append(details);}
    else{const note=document.createElement('small');note.className='term-reason';note.textContent=reason;cell.append(note);}
  }
  if(['HR','HRV','BATTERY','STATUS','Sleep','Effort','RHR','RHRV','DEBT'].includes(label)){
    reading.setAttribute('role','button');reading.setAttribute('tabindex','0');reading.setAttribute('aria-label','Inspect '+label+' observation');
    reading.onclick=()=>showMetricDetails(label,value);reading.onkeydown=event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();showMetricDetails(label,value);}};
    const name={Sleep:'sleep',Effort:'effort',RHR:'rhr',RHRV:'rhrv'}[label],baseline=name?currentBaselines().metrics[name]:null;
    if(baseline){const hint=document.createElement('small');hint.className='term-baseline-hint';
      const number=Number.parseFloat(value);
      hint.textContent=baseline.ready&&Number.isFinite(number)?((number-baseline.median>=0?'+':'')+fmt(number-baseline.median)+' vs usual'):'learning '+baseline.n+'/7';
      cell.append(hint);
    }
  }
  line.append(key,cell);target.append(line);
}
function metric(target,label,item,unit=''){
  const value=typeof item==='number'?item:item?.display_value??item?.value??item?.rmssd_ms;
  const present=typeof value==='number'&&Number.isFinite(value);
  if(present)row(target,label,fmt(value)+(unit?' '+unit:''));
}
function score(target,label,item){
  const value=item?.value;
  if(typeof value==='number'&&Number.isFinite(value))row(target,label,fmt(value)+'%');
}
function renderStatus(s){
  status=s;
  el('termPhase').textContent=s.phase.toUpperCase();
  const target=el('termLive');target.replaceChildren();
  if(s.connected && (s.hr_age_s==null||s.hr_age_s<=15))metric(target,'HR',s.hr,'bpm');
  if(s.connected)metric(target,'HRV',s.rmssd,'ms');
  metric(target,'BATTERY',s.battery,'%');
  if(s.connected){
    const rest=daily?.day===today()&&Number.isFinite(daily?.resting_hr?.value)?daily.resting_hr.value:profile.resting_hr;
    const estimate=chartMath.statusEstimate(hourPoints,rest,profile.max_hr,Date.now());
    row(target,'STATUS',(s.hr_age_s!=null&&s.hr_age_s>15)?'STALE':estimate.label);
    target.children[target.children.length-1].setAttribute('title',estimate.mean==null?
      'HR load estimate: learning; needs at least 45 measured minutes in the last hour.':
      `HR load estimate: last-hour mean ${fmt(estimate.mean)} bpm; ${Math.round(estimate.reserve*100)}% HR reserve (rest ${rest}, max ${profile.max_hr}). Labels use BOOP heuristic bands, not validated emotional states; HRV is shown separately because movement, posture and breathing affect it.`);
  }
  el('termLivePanel').hidden=!target.children.length;
  el('termConnect').disabled=pending||s.connected;
  el('termConnect').hidden=s.connected;
  el('termDisconnect').disabled=pending||!s.connected;
  el('termDisconnect').hidden=!s.connected;
  el('termSync').disabled=pending||!s.connected||s.sync?.active;
  el('termSync').hidden=!!s.sync?.active;
  el('termStop').disabled=pending||!s.sync?.active;
  el('termStop').hidden=!s.sync?.active;
  el('termScan').disabled=pending;
  el('termUpdated').textContent='Updated '+time(Date.now())+' · Brisbane';
}
function renderDay(d){
  daily=d;sleepKey='';const target=el('termDaily');target.replaceChildren();
  const demo=el('termDemoSleep').checked;
  score(target,'Sleep',demo?{value:84}:d.rest);
  score(target,'Effort',d.effort);
  metric(target,'RHRV',demo&&d.hrv?.value==null?{value:48}:d.hrv,'ms');
  metric(target,'RHR',demo&&d.resting_hr?.value==null?{value:56}:d.resting_hr,'bpm');
  const debt=demo&&!d.sleep?.debt?.nights?.length?{balance_min:demoNights(31).at(-1).sleep_debt_min,nights:[{}]}:d.sleep?.debt;
  if(debt?.nights?.length&&Number.isFinite(debt.balance_min)){
    const minutes=Math.round(Math.abs(debt.balance_min));
    row(target,'DEBT',(debt.balance_min<0?'-':'+')+Math.floor(minutes/60)+':'+String(minutes%60).padStart(2,'0'));
    target.children[target.children.length-1].className+=' '+(debt.balance_min<0?'term-deficit':'term-surplus');
  }
  el('termDailyPanel').hidden=!target.children.length;
  drawSleep(d.sleep);
  el('termDemoNote').hidden=!demo;updateBriefing();refreshBaseline();
  patterns?.refreshDay();
}
const svgNode=(name,attrs,text)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attrs))n.setAttribute(key,value);if(text!==undefined)n.textContent=text;return n;};
let cachedHR=[],cachedSleep=null;
const graphWidth=svg=>Math.max(240,svg.getBoundingClientRect?.().width||500);
function chartPath(points,x,y,gap){
  let path='',prior=null;
  for(const p of points){path+=`${prior===null||p.t-prior>gap?'M':'L'}${x(p.t).toFixed(1)} ${y(p.hr).toFixed(1)} `;prior=p.t;}
  return path;
}
function drawHR(points){
  cachedHR=points;
  const svg=el('termHRGraph'),end=Date.now(),period=hrPeriods[hrPeriod],duration=period.hours*3600000,start=end-duration;
  const valid=chartMath.valid(points).filter(p=>p.t>=start&&p.t<=end),curves=[valid],average=[],envelope=[];
  const offset=10*3600000;
  for(let day=Math.floor((start+offset)/chartMath.DAY);day<=Math.floor((end+offset)/chartMath.DAY);day++)for(const p of monthlyHR?.points||[]){const t=day*chartMath.DAY-offset+p.t;if(t>=start-300000&&t<=end+300000)average.push({t,hr:p.mean,n:p.n});}
  average.sort((a,b)=>a.t-b.t);
  if(average.length){const first=average[0],last=average.at(-1);average.unshift({...first,t:first.t-150000});average.push({...last,t:last.t+150000});}
  const rest=Number.isFinite(daily?.resting_hr?.value)?daily.resting_hr.value:profile.resting_hr;
  svg.replaceChildren();svg.hidden=!valid.length;el('termHRRange').hidden=true;
  if(!valid.length){hrModel=null;svg.setAttribute('hidden','');el('termHRRange').hidden=false;el('termHRRange').textContent=graphPending?'Loading '+period.label+'…':'No recorded HR in the last '+period.label+'.';return;}svg.removeAttribute('hidden');
  svg.setAttribute('aria-label','Recorded heart rate over '+period.label+' with red 30-day average by time of day');
  const values=[...valid.map(p=>p.hr),...average.map(p=>p.hr),rest],minHR=Math.min(...values),maxHR=Math.max(...values),lo=minHR-5,hi=maxHR+5,width=graphWidth(svg),left=28,right=width-8;
  svg.setAttribute('viewBox',`0 0 ${width} 150`);
  const domain=duration,origin=start;
  const motion=motionPeriod===hrPeriod?(liveMotion?.points||[]).filter(p=>p.t>=start&&p.t<=end&&Number.isFinite(p.motion)):[];
  const hasMotion=motion.length>0;
  svg.setAttribute('aria-label','Recorded heart rate over '+period.label+' with red 30-day average by time of day'+(hasMotion?' and relative wrist movement below':''));
  const x=t=>left+(t-origin)/domain*(right-left),y=hr=>(hasMotion?101:130)-(hr-lo)/(hi-lo)*(hasMotion?89:118);
  for(const value of [lo,hi])svg.append(svgNode('text',{x:0,y:y(value)+3},Math.round(value)));
  for(const [value,label]of [...[.5,.6,.7,.8,.9].map((p,i)=>[60+130*p,'Z'+(i+1)]),[190,'MAX']]){
    if(value<minHR||value>maxHR)continue;
    svg.append(svgNode('line',{x1:left,x2:right,y1:y(value),y2:y(value),class:'term-zone-line'}),svgNode('text',{x:right,y:y(value)-3,'text-anchor':'end',class:'term-zone-label'},label));
  }
  svg.append(svgNode('line',{x1:left,x2:right,y1:y(rest),y2:y(rest),class:'term-live-rest-line'}),svgNode('text',{x:right,y:y(rest)-4,'text-anchor':'end',class:'term-live-rest-label'},'RHR '+Math.round(rest)));
  svg.append(svgNode('path',{d:chartPath(valid,x,y,Math.max(30000,duration/200)),class:'term-trace'}));
  const latest=valid.at(-1);svg.append(svgNode('circle',{cx:x(latest.t),cy:y(latest.hr),r:2,class:'term-point'}));
  const defs=svgNode('defs',{}),clip=svgNode('clipPath',{id:'termHRPlotClip'});clip.append(svgNode('rect',{x:left,y:0,width:right-left,height:135}));defs.append(clip);svg.append(defs);
  svg.append(svgNode('path',{d:chartPath(average,x,y,300001),class:'term-average','clip-path':'url(#termHRPlotClip)'}));
  if(hasMotion){
    const peak=Math.max(...motion.map(p=>p.motion),.00001),bar=Math.max(1,liveMotion.bucket_ms/domain*(right-left)*.7);
    svg.append(svgNode('line',{x1:left,x2:right,y1:109,y2:109,class:'term-gridline'}),svgNode('text',{x:0,y:127,class:'term-motion-label'},'MOV'));
    for(const p of motion){const height=Math.max(1,p.motion/peak*21);svg.append(svgNode('rect',{x:x(p.t)-bar/2,y:134-height,width:bar,height,class:'term-motion-bar',opacity:.3+.7*p.coverage}));}
  }
  hrModel={svg,left,right,x,y,origin,domain,curves,valid,average,envelope,motion,days:null,end};
  svg.setAttribute('title','Red: 30-day mean HR by local time of day; '+(monthlyHR?.observed_days||0)+' observed days. Missing clock intervals stay empty. RHR: '+Math.round(rest)+' bpm'+(Number.isFinite(daily?.resting_hr?.value)?' from selected-day estimate.':' from configured resting HR.'));
  bindGraph(svg,'hr');
  const axisTime=t=>period.hours===24?new Date(t).toLocaleString('en-AU',{timeZone:'Australia/Brisbane',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}):time(t);
  svg.append(svgNode('text',{x:left,y:148},axisTime(start)),svgNode('text',{x:right,y:148,'text-anchor':'end'},axisTime(end)));
  if(activeCursor?.type==='hr')inspectHR(activeCursor.fraction);
}
async function refreshGraph(force=false){
  if(graphPending&&!force)return;
  if(force)graphController?.abort();
  const selected=hrPeriod,generation=++graphGeneration,controller=new AbortController();graphController=controller;graphPending=true;
  const timer=setTimeout(()=>controller.abort(),12000);
  try{
    const points=await request('/api/series?hours='+hrPeriods[selected].hours,undefined,{signal:controller.signal});
    if(generation!==graphGeneration||selected!==hrPeriod)return;
    hrCache.set(selected,points);graphPending=false;drawHR(points);lastGraph=Date.now();
    if(selected===1){hourPoints=points;lastHour=Date.now();}
  }catch(error){if(generation===graphGeneration){graphPending=false;el('termHRRange').hidden=false;el('termHRRange').textContent='HR could not refresh. Select a period to retry.';lastGraph=Date.now();}}
  finally{clearTimeout(timer);if(generation===graphGeneration)graphPending=false;}
  if(generation!==graphGeneration)return;
  // Auxiliary history must never queue or delay the selected HR window.
  if(!monthlyPending&&Date.now()-lastMonthlyHR>60000){monthlyPending=true;lastMonthlyHR=Date.now();request('/api/terminal/hr-profile').then(data=>{monthlyHR=data;drawHR(cachedHR);}).catch(()=>{}).finally(()=>monthlyPending=false);}
  if(motionPeriod!==selected||Date.now()-lastMotion>(selected===2?60000:10000)){
    lastMotion=Date.now();motionPeriod=selected;
    request('/api/terminal/motion?hours='+hrPeriods[selected].hours).then(data=>{if(selected===hrPeriod&&generation===graphGeneration){liveMotion=data;drawHR(cachedHR);}}).catch(()=>{});
  }
  if(!hourPending&&Date.now()-lastHour>60000){hourPending=true;request('/api/series?hours=1').then(points=>{hourPoints=points;lastHour=Date.now();if(status)renderStatus(status);}).catch(()=>{}).finally(()=>hourPending=false);}
}
function selectHRPeriod(index){
  hrPeriod=index;lastGraph=0;liveMotion=null;motionPeriod=-1;
  for(const button of el('termHRPeriod').querySelectorAll?.('[data-hr-period]')||[])button.setAttribute('aria-pressed',String(Number(button.dataset.hrPeriod)===index));
  graphPending=true;drawHR(hrCache.get(index)||[]);refreshGraph(true);
}
for(const button of el('termHRPeriod').querySelectorAll?.('[data-hr-period]')||[])button.onclick=()=>selectHRPeriod(Number(button.dataset.hrPeriod));
const hoursMinutes=seconds=>{const mins=Math.round(seconds/60);return Math.floor(mins/60)+':'+String(mins%60).padStart(2,'0');};
function demoDayHR(count,end){
  const midnight=Math.floor((end+10*3600000)/chartMath.DAY)*chartMath.DAY-10*3600000,points=[];
  for(let day=count-1;day>=0;day--)for(let i=0;i<288;i++){
    const t=midnight-day*chartMath.DAY+i*300000;if(t>end)continue;
    const hour=i/12,exercise=55*Math.exp(-Math.pow((hour-16.5)/1.2,2));
    points.push({t,hr:65+day*.45+9*Math.sin((hour-7)*Math.PI/12)+exercise+4*Math.sin(i/7+day)+2*Math.sin(i*1.3+day)});
  }return points;
}
function demoNights(count){
  const midnight=new Date(el('termDate').value+'T00:00:00+10:00').getTime()/1000,nights=[];
  const pattern=[['light',35],['deep',70],['light',55],['rem',30],['wake',10],['light',50],['deep',35],['light',60],['rem',45],['wake',10],['light',45],['rem',35]];
  for(let day=count-1;day>=0;day--){
    const start=midnight-day*86400+(day?Math.round(55*Math.sin(day*.8)*60):0);let cursor=start;
    const scale=day?1+.17*Math.sin(day*.67)-.06*Math.cos(day*1.8):1;
    const segments=pattern.map(([stage,minutes],index)=>{const length=Math.round(minutes*(day?scale*(1+.12*Math.sin(day*1.4+index)):1));const segment={start:cursor,end:cursor+length*60,stage};cursor=segment.end;return segment;});
    const rhr=56+day*.28+2*Math.sin(day),points=Array.from({length:241},(_,i)=>{const t=start+(cursor-start)*i/240,stage=segments.find(s=>t>=s.start&&t<s.end)?.stage;return {t:t*1000,hr:rhr+3*Math.sin(i/8+day)+1.5*Math.sin(i*1.7+day)+(stage==='rem'?7:stage==='wake'?13:stage==='deep'?-4:0)};});
    nights.push({day:new Date((midnight-day*86400+10*3600)*1000).toISOString().slice(0,10),start,end:cursor,segments,points,rhr,total_sleep_min:segments.filter(s=>s.stage!=='wake').reduce((sum,s)=>sum+(s.end-s.start)/60,0)});
  }return nights.map((night,index)=>{let debt=0;for(const prior of nights.slice(Math.max(0,index-13),index+1)){debt=.55*Math.max(0,480+debt-prior.total_sleep_min);if(debt<10)debt=0;}return {...night,need_min:480,sleep_debt_min:-debt};});
}
function renderSleepNights(nights){
  const stages={wake:8,awake:8,rem:21,light:34,deep:47};
  const available=nights.map(n=>{const segments=(n.segments||[]).filter(s=>Object.hasOwn(stages,s.stage)&&Number.isFinite(s.start)&&Number.isFinite(s.end)&&s.end>s.start);return {...n,segments,start:segments.length?Math.min(...segments.map(s=>s.start)):0,end:segments.length?Math.max(...segments.map(s=>s.end)):0};}).filter(n=>n.segments.length&&n.end>n.start);
  el('termSleepPanel').hidden=!available.length;
  const svg=el('termSleepGraph'),hr=el('termSleepHRGraph');svg.replaceChildren();hr.replaceChildren();
  if(!available.length){sleepModel=null;hr.setAttribute('hidden','');patterns?.sleep([],null);return;}
  const selected=available.find(n=>n.day===focusedNight)||available.at(-1);
  focusedNight=selected.day||'';updateNightControls(available,selected);
  patterns?.sleep(available,focusedNight);
  const others=chartMath.comparisonNights(available,selected,compareNight),aggregate=['week','month'].includes(compareNight),comparison=aggregate?null:others[0],valid=[...others,selected];
  const width=graphWidth(svg),left=112,right=width-4,maxDuration=Math.max(...valid.map(n=>n.end-n.start));
  svg.setAttribute('viewBox',`0 0 ${width} 58`);hr.setAttribute('viewBox',`0 0 ${width} 71`);
  const alignTime=(night,t)=>sleepAlignment==='onset'?t-night.start:((night.start+10*3600)%86400<18*3600?(night.start+10*3600)%86400+86400:(night.start+10*3600)%86400)+(t-night.start);
  const origin=sleepAlignment==='onset'?0:Math.min(...valid.map(n=>alignTime(n,n.start))),domain=sleepAlignment==='onset'?maxDuration:Math.max(...valid.map(n=>alignTime(n,n.end)))-origin;
  const x=seconds=>left+seconds/domain*(right-left),totals={wake:0,rem:0,light:0,deep:0};
  for(const night of valid)for(const segment of night.segments){
    if(!Object.hasOwn(stages,segment.stage))continue;
    const stage=segment.stage==='awake'?'wake':segment.stage;if(night===selected)totals[stage]+=segment.end-segment.start;
    svg.append(svgNode('rect',{x:x(alignTime(night,segment.start)-origin),y:stages[stage]-3,width:Math.max(.5,(segment.end-segment.start)/domain*(right-left)),height:6,class:'term-stage-'+stage,opacity:valid.length===1?1:night===selected?.7:night===comparison?.4:.08}));
  }
  for(const [stage,name]of [['wake','Wake'],['rem','REM'],['light','Light'],['deep','Deep']])svg.append(svgNode('text',{x:0,y:stages[stage]+4},name),svgNode('text',{x:43,y:stages[stage]+4},'|'),svgNode('text',{x:55,y:stages[stage]+4},hoursMinutes(totals[stage])));
  const curves=valid.map(n=>chartMath.valid(n.points).map(p=>({t:(alignTime(n,p.t/1000)-origin)*1000,hr:p.hr}))),all=curves.flat();
  const rhValues=valid.map(n=>n.rhr).filter(Number.isFinite),rhr=Number.isFinite(selected.rhr)?selected.rhr:profile.resting_hr;
  let envelope=[],y=null;
  if(all.length){
    hr.removeAttribute('hidden');hr.hidden=false;
    const values=all.map(p=>p.hr),lo=Math.min(...values,rhr)-3,hi=Math.max(...values,rhr)+3;
    y=v=>62-(v-lo)/(hi-lo)*52;
    envelope=chartMath.envelopeCurves(curves.filter((_,i)=>valid[i]!==selected),120000);
    appendEnvelope(hr,envelope,t=>x(t/1000),y,240000);
    for(const value of [lo,hi])hr.append(svgNode('text',{x:left-8,y:y(value)+3,'text-anchor':'end'},Math.round(value)));
    curves.forEach((curve,i)=>{if(valid[i]===selected||!aggregate)hr.append(svgNode('path',{d:chartPath(curve,t=>x(t/1000),y,Math.max(120000,maxDuration*1000/120)),class:valid[i]===selected?'term-focus-trace':'term-compare-trace'}));});
    if(aggregate&&others.length)hr.append(svgNode('path',{d:chartPath(chartMath.meanCurves(curves.slice(0,-1),120000),t=>x(t/1000),y,240000),class:'term-average'}));
    hr.append(svgNode('line',{x1:left,x2:right,y1:y(rhr),y2:y(rhr),class:'term-rest-line'}),svgNode('text',{x:0,y:y(rhr)+3,class:'term-rest-label'},'RHR '+Math.round(rhr)));
    hr.setAttribute('aria-label',(el('termDemoSleep').checked?'Demo ':'')+'Highlighted sleep HR'+(aggregate?' and '+(compareNight==='week'?'week':'month')+' mean':comparison?' and comparison night':'')+' aligned by '+sleepAlignment+'; focus '+(selected.day||'selected night')+'; dotted red RHR reference');
  }else{hr.setAttribute('hidden','');hr.hidden=true;}
  sleepModel={svg,hr,left,right,x,y,domain,origin,alignTime,selected,comparison,valid,curves,envelope,aggregate,others};bindGraph(svg,'sleep');bindGraph(hr,'sleep');if(activeCursor?.type==='sleep')inspectSleep(activeCursor.fraction);
  const shortTime=t=>new Date(t*1000).toLocaleTimeString('en-AU',{timeZone:'Australia/Brisbane',hour:'numeric',minute:'2-digit'});
  el('termSleepStart').textContent=width<380?shortTime(selected.start):time(selected.start*1000);
  el('termSleepEnd').textContent=width<380?shortTime(selected.end):time(selected.end*1000);
  el('termSleepRange').textContent=Number.isFinite(selected.total_sleep_min)?'- '+hoursMinutes(selected.total_sleep_min*60)+' -':'';
  el('termSleepTimes').setAttribute('title','Sleep duration and stage totals belong to the highlighted night. Comparisons align by '+sleepAlignment+'.');
  const note=el('termSleepComparison');note.hidden=!compareNight;
  const label=compareNight==='week'?'Previous week average':compareNight==='month'?'Previous month average':compareNight==='yesterday'?'Yesterday':compareNight;
  const durations=others.map(n=>n.total_sleep_min).filter(Number.isFinite),avg=durations.length?hoursMinutes(durations.reduce((a,b)=>a+b,0)/durations.length*60):null;
  note.textContent=label+' · '+others.length+' observed night'+(others.length===1?'':'s')+(avg?' · sleep '+avg:' · no usable sleep')+(aggregate?' · red HR mean, pale stage overlaps':' · amber comparison');
  note.setAttribute('title','Uses available observations in the preceding '+(compareNight==='week'?'7':compareNight==='month'?'30':'1')+' calendar days; missing nights are not filled. Highlighted night is excluded.');

}
function drawSleep(sleep){
  cachedSleep=sleep;
  if(el('termDemoSleep').checked){renderSleepNights(demoNights(31));return;}
  const key=el('termDate').value+':30';
  if(sleepKey===key){renderSleepNights(realNights);return;}
  const alreadyPending=sleepPending===key;
  const main=sleep?.main,segments=main?.staging?.value||[];
  renderSleepNights(segments.length?[{day:el('termDate').value,start:Math.min(...segments.map(s=>s.start)),end:Math.max(...segments.map(s=>s.end)),segments,total_sleep_min:main.total_sleep_min,rhr:daily?.resting_hr?.value,points:[]}]:[]);
  if(alreadyPending)return;sleepPending=key;
  const selectedDate=el('termDate').value,previousDate=new Date(Date.parse(selectedDate+'T12:00:00+10:00')-chartMath.DAY).toLocaleDateString('en-CA',{timeZone:'Australia/Brisbane'});
  Promise.all([request('/api/terminal/sleep?days=30&date='+encodeURIComponent(previousDate)),request('/api/terminal/sleep?days=1&date='+encodeURIComponent(selectedDate))]).then(results=>{const result={nights:results.flatMap(item=>item.nights).sort((a,b)=>String(a.day).localeCompare(String(b.day)))};
    if(key===el('termDate').value+':30'&&!el('termDemoSleep').checked){realNights=result.nights;sleepKey=key;renderSleepNights(realNights);}
  }).catch(()=>{}).finally(()=>{if(sleepPending===key)sleepPending='';});
}
el('termNightFocus').onchange=()=>{focusedNight=el('termNightFocus').value;drawSleep(cachedSleep);};
el('termNightCompare').onchange=()=>{compareNight=el('termNightCompare').value;drawSleep(cachedSleep);};
el('termSleepAlign').onclick=()=>{sleepAlignment=sleepAlignment==='onset'?'clock':'onset';el('termSleepAlign').textContent='[ '+(sleepAlignment==='onset'?'Onset':'Clock')+' ]';drawSleep(cachedSleep);};
async function refreshDay(){
  if(dayPending)return;dayPending=true;const selected=el('termDate').value||today();
  try{const d=await request('/api/day?date='+encodeURIComponent(selected));if(selected===el('termDate').value){renderDay(d);lastDay=Date.now();}}
  finally{dayPending=false;}
}
async function refresh(){
  if(!visible()||document.hidden||inFlight)return;inFlight=true;
  try{
    renderStatus(await request('/api/status'));
    if(Date.now()-lastGraph>5000)await refreshGraph();
    if(Date.now()-lastDay>60000)await refreshDay();
  }
  catch(error){el('termPhase').textContent='SERVICE UNAVAILABLE';output(error.message+' Open the BOOP desktop shortcut if the service has stopped.');}
  finally{inFlight=false;}
}
async function run(name,body={}){
  if(pending){output('Wait for the current action to finish.');return;}
  pending=true;if(status)renderStatus(status);output(name==='scan'?'Scanning nearby straps…':`Running ${name}…`);
  try{
    const result=await request('/api/'+name,body);
    if(name==='scan'){
      el('termDevices').replaceChildren();
      for(const device of result.devices||[]){const b=document.createElement('button');b.type='button';b.textContent=`Connect ${device.name} [${device.address}]`;b.onclick=()=>run('connect',{address:device.address});el('termDevices').append(b);}
      output(result.devices?.length?'Choose a strap above.':'No strap found. Keep it nearby in pairing mode and turn phone Bluetooth off, then scan again.');
    }else{output(name==='connect'?'Connection requested. Watch the strap status.':name==='sync'?'History sync requested. Watch saved records.':'Action completed.');if(name==='connect')el('termDevices').replaceChildren();}
    renderStatus(await request('/api/status'));
  }catch(error){output(error.message);}
  finally{pending=false;if(status)renderStatus(status);}
}
const actions={scan:()=>run('scan'),connect:()=>status?.address?run('connect',{address:status.address}):run('scan'),disconnect:()=>run('disconnect'),sync:()=>run('sync'),'stop-sync':()=>run('stop-sync')};
for(const [id,name]of [['termScan','scan'],['termConnect','connect'],['termDisconnect','disconnect'],['termSync','sync'],['termStop','stop-sync']])el(id).onclick=actions[name];
el('termRefresh').onclick=()=>{personalBaseline=null;lastDay=0;patterns?.refreshDay(true);refresh();};
el('termDate').onchange=()=>{if(!el('termDate').value)el('termDate').value=today();lastDay=0;refreshDay().catch(e=>output(e.message));};
let demoBaselineCache=null;
function currentBaselines(){
  if(!el('termDemoSleep').checked)return personalBaseline||{metrics:{},hr_clock_profile:[]};
  if(demoBaselineCache?.through===el('termDate').value)return demoBaselineCache;
  const values={sleep:[],effort:[],rhr:[],rhrv:[],sleep_minutes:[]},nights=demoNights(31).slice(0,-1);
  for(let i=0;i<nights.length;i++){const night=nights[i];values.sleep.push(82+5*Math.sin(i*.7));values.effort.push(30+8*Math.sin(i*.8));values.rhr.push(night.rhr);values.rhrv.push(44+6*Math.sin(i*.6));values.sleep_minutes.push(night.total_sleep_min);}
  const metrics={};for(const [key,points]of Object.entries(values))metrics[key]={...chartMath.descriptiveBaseline(points),unit:{sleep:'%',effort:'%',rhr:'bpm',rhrv:'ms',sleep_minutes:'min'}[key],observations:nights.map((night,i)=>({day:night.day,value:points[i],source:'Synthetic demo'}))};
  const curves=chartMath.alignDays(demoDayHR(31,Date.now()),31,Date.now()).slice(0,-1).map(day=>day.points);
  demoBaselineCache={through:el('termDate').value,metrics,hr_clock_profile:chartMath.envelopeCurves(curves,300000),source:'Synthetic demo comparison',demo:true};return demoBaselineCache;
}
async function refreshBaseline(){
  const selected=el('termDate').value;
  if(personalBaseline?.through===selected||baselinePending===selected)return;baselinePending=selected;
  try{const baseline=await request('/api/terminal/baseline?date='+encodeURIComponent(selected));if(el('termDate').value===selected){personalBaseline=baseline;if(daily)renderDay(daily);drawHR(cachedHR);}}
  catch{}finally{if(baselinePending===selected)baselinePending='';}
}
function showDetails(title,lines){
  activeDetails={title,lines};el('termEvidenceTitle').textContent=title;el('termEvidenceBody').replaceChildren();
  for(const line of lines){const p=document.createElement('p');p.textContent=line;el('termEvidenceBody').append(p);}
  el('termEvidencePanel').hidden=false;
}
function showMetricDetails(label,value){
  const map={Sleep:'sleep',Effort:'effort',RHR:'rhr',RHRV:'rhrv'},key=map[label],baseline=key?currentBaselines().metrics[key]:null;
  const sourceItem={Sleep:daily?.rest,Effort:daily?.effort,RHR:daily?.resting_hr,RHRV:daily?.hrv}[label];
  const synthetic=el('termDemoSleep').checked&&(label==='Sleep'||label==='DEBT'&&!daily?.sleep?.debt?.nights?.length||['RHR','RHRV'].includes(label)&&sourceItem?.value==null);
  const kind=synthetic?'DEMO':label==='HR'||label==='BATTERY'?'MEASURED':String(sourceItem?.source||'').match(/import|official|manual/i)?'IMPORTED':'ESTIMATED';
  const lines=[`${value} · ${kind}`,synthetic?'Synthetic preview; never saved as a reading or supplied as real AI health context.':`Source: ${sourceItem?.source||(['HR','BATTERY'].includes(label)?'Live strap':label==='HRV'?'RMSSD from measured beat intervals':'BOOP local estimate')}`];
  if(label==='HR')lines.push(`Reading age: ${status?.hr_age_s==null?'unavailable':fmt(status.hr_age_s)+' seconds'}.`);
  if(label==='HRV')lines.push('Live HRV reflects the current beat-interval window. Resting HRV has different measurement conditions; the two are kept separate.');
  if(label==='STATUS')lines.push('Descriptive last-hour HR load, using configured or measured resting HR. Heuristic labels do not establish an emotional state. At least 45 measured minutes are required.');
  if(baseline){
    lines.push(`${currentBaselines().demo?'DEMO comparison':'Personal comparison'} · ${baseline.n} observed prior days · ${baseline.status}`);
    if(baseline.ready)lines.push(`Median ${fmt(baseline.median)} ${baseline.unit}; typical middle 50% ${fmt(baseline.low)}–${fmt(baseline.high)} ${baseline.unit}. The inspected day is excluded.`);
    else lines.push('Learning: seven observed prior days unlock the descriptive comparison. Missing days are not filled.');
    const dates=(baseline.observations||[]).map(item=>item.day).filter(Boolean);if(dates.length)lines.push(`Observed dates: ${dates.join(', ')}`);
  }
  if(sourceItem?.reason)lines.push(sourceItem.reason);
  showDetails(label+' · '+el('termDate').value,lines);
}
function updateBriefing(){
  const baseline=currentBaselines(),parts=[],demo=el('termDemoSleep').checked;
  const latest=demo?demoNights(1)[0]:null,sleep=latest?.total_sleep_min??daily?.sleep?.main?.total_sleep_min;
  const rhr=demo&&daily?.resting_hr?.value==null?56:daily?.resting_hr?.value;
  if(baseline.metrics.sleep_minutes?.ready&&Number.isFinite(sleep)){const delta=Math.round(sleep-baseline.metrics.sleep_minutes.median);parts.push(Math.abs(delta)<15?'sleep near your median':`sleep ${Math.abs(delta)} min ${delta>0?'above':'below'} your median`);}
  if(baseline.metrics.rhr?.ready&&Number.isFinite(rhr)){const delta=rhr-baseline.metrics.rhr.median;parts.push(`RHR ${fmt(Math.abs(delta))} bpm ${delta>=0?'above':'below'} your median`);}
  if(Number.isFinite(daily?.effort?.value))parts.push(`effort ${fmt(daily.effort.value)}%`);
  if(!parts.length)parts.push('waiting for usable observations');
  const prefix=demo?'DEMO':Object.values(baseline.metrics).some(item=>item.ready)?'TODAY':'LEARNING';
  el('termBriefing').textContent=prefix+' / '+parts.slice(0,3).join(' · ');
}
function updateNightControls(nights,selected){
  const focus=el('termNightFocus'),compare=el('termNightCompare');focus.replaceChildren();compare.replaceChildren();
  if(compareNight===selected.day||(!['','yesterday','week','month'].includes(compareNight)&&!nights.some(n=>n.day===compareNight)))compareNight='';
  for(const [value,label]of [['','No comparison'],['yesterday','Yesterday'],['week','Week average'],['month','Month average']]){
    const option=document.createElement('option');option.value=value;option.textContent=label;compare.append(option);
  }
  for(const night of nights){const label=night.day?new Date(night.day+'T12:00:00+10:00').toLocaleDateString('en-AU',{day:'numeric',month:'short'}):'Selected night';
    const option=document.createElement('option');option.value=night.day||'';option.textContent=label;focus.append(option);
    if(night.day&&night!==selected){const second=document.createElement('option');second.value=night.day;second.textContent=label;compare.append(second);}
  }
  focus.value=selected.day||'';compare.value=compareNight;
}

function appendEnvelope(svg,envelope,x,y,gap){
  let group=[];const flush=()=>{if(group.length<2){group=[];return;}
    const top=group.map(p=>`${x(p.t).toFixed(1)} ${y(p.high).toFixed(1)}`),bottom=[...group].reverse().map(p=>`${x(p.t).toFixed(1)} ${y(p.low).toFixed(1)}`);
    svg.append(svgNode('path',{d:'M'+top.join(' L')+' L'+bottom.join(' L')+' Z',class:'term-typical-band'}));
    svg.append(svgNode('path',{d:chartPath(group.map(p=>({t:p.t,hr:p.median})),x,y,gap),class:'term-median'}));group=[];
  };
  for(const point of envelope){if(group.length&&point.t-group.at(-1).t>gap)flush();group.push(point);}flush();
}
let activeCursor=null;
function clearCursor(){
  for(const id of ['termHRGraph','termSleepGraph','termSleepHRGraph'])for(const node of el(id).querySelectorAll?.('.term-cursor')||[])node.remove();
}
function cursorLine(svg,x,height){svg.append(svgNode('line',{x1:x,x2:x,y1:0,y2:height,class:'term-cursor'}));}
function sleepAt(model,night,relative){
  const elapsed=relative+model.origin-model.alignTime(night,night.start),t=(night.start+elapsed)*1000;
  if(elapsed<0||t>night.end*1000)return {elapsed,t,stage:null,point:null};
  const stage=night.segments.find(s=>t>=s.start*1000&&t<s.end*1000)?.stage;
  return {elapsed,t,stage,point:chartMath.nearestPoint(chartMath.valid(night.points),t,120000)};
}
function inspectSleep(fraction,keyboard=false){
  const model=sleepModel;if(!model)return;fraction=Math.min(1,Math.max(0,fraction));activeCursor={type:'sleep',fraction};clearCursor();
  const relative=fraction*model.domain,x=model.left+fraction*(model.right-model.left),reading=sleepAt(model,model.selected,relative);
  cursorLine(model.svg,x,58);if(model.y)cursorLine(model.hr,x,71);
  const parts=[el('termDemoSleep').checked?'DEMO':'OBSERVED',model.selected.day||'selected night',time(reading.t),reading.stage?reading.stage.toUpperCase():'no stage at this time',reading.point?'HR '+fmt(reading.point.hr)+' bpm':'HR unavailable'];
  const normal=chartMath.nearestPoint(model.envelope,relative*1000,120000);
  if(normal)parts.push(`usual ${fmt(normal.low)}–${fmt(normal.high)} bpm · median ${fmt(normal.median)} · ${normal.n} other nights`);
  if(model.aggregate){const point=chartMath.nearestPoint(chartMath.meanCurves(model.curves.slice(0,-1),120000),relative*1000,120000);if(point)parts.push((compareNight==='week'?'week':'month')+' mean HR '+fmt(point.hr)+' bpm · '+point.n+' observed nights');}
  if(model.comparison){const other=sleepAt(model,model.comparison,relative);parts.push('↔ '+model.comparison.day+' '+(other.stage||'no stage')+' '+(other.point?fmt(other.point.hr)+' bpm':'no HR'));}
  if(hrModel){const t=hrModel.days?(reading.t+10*3600000)%chartMath.DAY:reading.t;if(t>=hrModel.origin&&t<=hrModel.origin+hrModel.domain)cursorLine(hrModel.svg,hrModel.x(t),150);}
  el('termInspect').setAttribute('aria-live',keyboard?'polite':'off');el('termInspect').textContent=parts.join(' · ');
}
function inspectHR(fraction,keyboard=false){
  const model=hrModel;if(!model)return;fraction=Math.min(1,Math.max(0,fraction));activeCursor={type:'hr',fraction};clearCursor();
  const t=model.origin+fraction*model.domain;cursorLine(model.svg,model.x(t),150);
  const reading=chartMath.nearestPoint(model.valid,t,Math.max(30000,model.domain/200)),normal=chartMath.nearestPoint(model.envelope,t,300000);
  const stamp=model.days?new Date(t-10*3600000).toLocaleTimeString('en-AU',{timeZone:'Australia/Brisbane',hour:'numeric',minute:'2-digit'}):time(t);
  const parts=[model.days&&el('termDemoSleep').checked?'DEMO':'OBSERVED',stamp,reading?`HR ${fmt(reading.hr)} bpm`:'no HR at this time'];
  const month=chartMath.nearestPoint(model.average,t,300000);if(month)parts.push(`30-day clock mean ${fmt(month.hr)} bpm · ${month.n} observed days`);
  if(model.motion?.length){const movement=chartMath.nearestPoint(model.motion,t,liveMotion.bucket_ms/2);if(movement){const peak=Math.max(...model.motion.map(p=>p.motion),.00001);parts.push(`wrist motion ${Math.round(movement.motion/peak*100)}% of window peak`);}else parts.push('motion unavailable');}
  if(sleepModel){
    let absolute=t;
    if(model.days){const onset=(sleepModel.selected.start*1000+10*3600000)%chartMath.DAY;let delta=t-onset;if(delta<-12*3600000)delta+=chartMath.DAY;if(delta>12*3600000)delta-=chartMath.DAY;absolute=sleepModel.selected.start*1000+delta;}
    if(absolute>=sleepModel.selected.start*1000&&absolute<=sleepModel.selected.end*1000){const relative=sleepModel.alignTime(sleepModel.selected,absolute/1000)-sleepModel.origin,x=sleepModel.x(relative);cursorLine(sleepModel.svg,x,58);if(sleepModel.y)cursorLine(sleepModel.hr,x,71);const stage=sleepModel.selected.segments.find(s=>absolute>=s.start*1000&&absolute<s.end*1000)?.stage;if(stage)parts.push('sleep '+stage);}
  }
  el('termInspect').setAttribute('aria-live',keyboard?'polite':'off');el('termInspect').textContent=parts.join(' · ');
}
function bindGraph(svg,type){
  svg.setAttribute('tabindex','0');svg.setAttribute('aria-describedby','termInspect');
  const inspect=type==='sleep'?inspectSleep:inspectHR;
  svg.onpointermove=event=>{const model=type==='sleep'?sleepModel:hrModel;if(!model)return;const box=svg.getBoundingClientRect(),x=event.clientX-box.left;if(x>=model.left&&x<=model.right)inspect((x-model.left)/(model.right-model.left));};
  svg.onfocus=()=>inspect(activeCursor?.type===type?activeCursor.fraction:.5,true);
  svg.onkeydown=event=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();let fraction=activeCursor?.type===type?activeCursor.fraction:.5;fraction=event.key==='Home'?0:event.key==='End'?1:fraction+(event.key==='ArrowLeft'?-1:1)*(event.shiftKey?.1:.01);inspect(fraction,true);}if(event.key==='Escape'){activeCursor=null;clearCursor();el('termInspect').textContent=initialInspect;}};
}
function showCoachEvidence(item){
  const lines=['REAL CONTEXT · '+item.source,'Source references identify supplied observations; interpretations remain the coach’s assessment.'];
  for(const [key,value]of Object.entries(item.values||{}))lines.push(key.replaceAll('_',' ')+': '+(typeof value==='object'?JSON.stringify(value):String(value)));
  showDetails((item.topic||'Context')+' · '+(item.day||'live'),lines);
  if(item.day&&/^\d{4}-\d{2}-\d{2}$/.test(item.day)){
    if(el('termDemoSleep').checked){el('termDemoSleep').checked=false;el('termDemoNote').hidden=true;drawHR(cachedHR);}
    el('termDate').value=item.day;focusedNight=item.day;compareNight='';sleepKey='';lastDay=0;
    if(['sleep','recovery'].includes(item.topic))el('termSleepPanel').classList.add?.('term-evidence-highlight');
    else el('termHRPanel').classList.add?.('term-evidence-highlight');
    refreshDay().catch(error=>output(error.message));
  }
}
function renderCoachSources(result,fallback=''){
  const reply=el('termAIReply'),answer=result.answer||result.text||fallback,evidence=result.evidence||[];
  lastEvidence=evidence.length?evidence:result.context_evidence||[];reply.replaceChildren();
  const byID=new Map(evidence.map((item,i)=>[item.id,{item,index:i+1}])),pattern=/\[(E[0-9a-f]{8})\]/g;let prior=0,match;
  while((match=pattern.exec(answer))){reply.append(document.createTextNode(answer.slice(prior,match.index)));const ref=byID.get(match[1]);if(ref){const button=document.createElement('button');button.className='term-citation';button.textContent='['+ref.index+']';button.setAttribute('aria-label','Inspect '+ref.item.topic+' evidence for '+ref.item.day);button.onclick=()=>showCoachEvidence(ref.item);reply.append(button);}prior=pattern.lastIndex;}
  reply.append(document.createTextNode(answer.slice(prior)));
  const target=el('termAISources');target.replaceChildren();target.hidden=!lastEvidence.length;
  if(lastEvidence.length){const label=document.createElement('span');label.textContent=evidence.length?'Referenced observations:':'Context supplied · coach did not cite a source:';target.append(label);
    for(const item of lastEvidence.slice(-12)){const button=document.createElement('button');button.textContent=item.topic+' · '+(item.day||'live');button.onclick=()=>showCoachEvidence(item);target.append(button);}
  }
  if(result.citation_warning){const note=document.createElement('span');note.textContent=result.citation_warning;target.append(note);target.hidden=false;}
}
let experimentLoading=false;
async function loadExperiments(){
  if(experimentLoading)return;experimentLoading=true;
  try{const insights=await request('/api/insights?days=90'),target=el('termExperimentList');target.replaceChildren();const experiments=insights.experiments||[];el('termExperimentCount').textContent=experiments.length?'('+experiments.length+')':'';
    for(const experiment of experiments){const line=document.createElement('p');line.textContent=`${experiment.name} · ${experiment.status.replaceAll('_',' ')} · ${experiment.baseline_days} baseline / ${experiment.intervention_days} following days`;target.append(line);if(experiment.effect){const detail=document.createElement('p');detail.textContent=`Observed change: ${fmt(experiment.effect.delta)} ${experiment.outcome==='sleep_min'?'min':experiment.outcome==='hrv'?'ms':experiment.outcome==='resting_hr'?'bpm':'points'} · baseline ${fmt(experiment.effect.mean_without)} → following ${fmt(experiment.effect.mean_with)}. Before/after association; other changes may explain it.`;target.append(detail);}}
    if(!experiments.length){const note=document.createElement('p');note.textContent='No experiment yet. Choose one change to observe.';target.append(note);}
  }catch(error){el('termExperimentList').textContent=error.message;}finally{experimentLoading=false;}
}
el('termEvidenceClose').onclick=()=>{activeDetails=null;el('termEvidencePanel').hidden=true;for(const id of ['termSleepPanel','termHRPanel'])el(id).classList.remove?.('term-evidence-highlight');};
el('termBriefing').onclick=()=>{for(const id of ['termHRPanel','termSleepPanel'])if(!el(id).hidden)el(id).classList.add?.('term-evidence-highlight');const baseline=currentBaselines();showDetails(el('termBriefing').textContent,[baseline.demo?'Synthetic demo briefing; real AI context excludes these values.':'Briefing uses your local observed values and descriptive baselines.',baseline.note||'Typical range is the middle 50% of prior observed days.',...Object.entries(baseline.metrics).map(([key,value])=>`${key.replaceAll('_',' ')} · ${value.n} observations · ${value.status}${value.ready?' · median '+fmt(value.median)+' '+value.unit:''}`)]);};
el('termExperiments').ontoggle=()=>{if(el('termExperiments').open)loadExperiments();};
el('termExperimentForm').onsubmit=async event=>{
  event.preventDefault();const name=el('termExperimentName').value.trim();if(!name||name.length>120)return;const shift=n=>{const date=new Date(today()+'T12:00:00+10:00');date.setUTCDate(date.getUTCDate()+n);return date.toLocaleDateString('en-CA',{timeZone:'Australia/Brisbane'});};
  const button=event.submitter;if(button)button.disabled=true;
  try{await request('/api/records/habit',{date:today(),name,type:'experiment',outcome:el('termExperimentMetric').value,baseline_start:shift(0),baseline_end:shift(6),intervention_start:shift(7),intervention_end:shift(13),source:'manual'});el('termExperimentName').value='';output('Experiment saved. Observe your usual first seven days, then try the change for seven days and record it in your journal.');await loadExperiments();}
  catch(error){output(error.message);}finally{if(button)button.disabled=false;}
};
document.addEventListener('keydown',event=>{
  if(!visible()||event.ctrlKey||event.metaKey||event.altKey||['INPUT','TEXTAREA','SELECT','BUTTON'].includes(event.target?.tagName))return;
  if(event.key==='/'){event.preventDefault();el('termCommand').focus();}
  if(event.key==='b')el('termBriefing').click();if(event.key==='h')el('termHRPeriod').click();if(event.key==='s')el('termNightCompare').focus();
  if(event.key==='Escape'){activeCursor=null;clearCursor();el('termInspect').textContent=initialInspect;el('termEvidenceClose').click();}
});
el('termDemoSleep').onchange=()=>{renderDay(daily||{day:el('termDate').value});drawHR(cachedHR);};
renderDay({day:el('termDate').value});
const apiKey=()=>el('termAIKey').value.trim().replace(/^Bearer\s+/i,'').trim();
function aiPayload(question){
  const key=apiKey(),model=el('termAIModel').value.trim();
  if(!key||!model){el('termAISettings').open=true;location.hash='settings';throw Error('Enter your OpenRouter API key and model in Settings.');}
  if(!el('termAIConsent').checked){el('termAISettings').open=true;location.hash='settings';throw Error('Enable this session’s context and conversation consent in OpenRouter Settings.');}
  return {question,provider:'compatible',endpoint:'https://openrouter.ai/api/v1',api_key:key,model,consent:true,save:false,context_mode:'adaptive',context_session:aiSession};
}
async function askAI(question){
  if(aiController){output('Wait for the reply, or cancel it.');return;}
  let payload;try{payload=aiPayload(question);}catch(error){el('termCommand').value=question;throw error;}
  const controller=new AbortController();aiController=controller;
  el('termRun').disabled=true;el('termModels').disabled=true;el('termAICancel').hidden=false;
  el('termAISources').hidden=true;const reply=el('termAIReply');reply.hidden=false;reply.textContent='';reply.setAttribute('aria-busy','true');
  output('OpenRouter · asking what context is needed…');let text='';
  try{
    await request('/api/coach/key',{api_key:payload.api_key,consent:true});
    const stream=await window.boopCoachStream(payload,{signal:controller.signal,onEvent:event=>{if(event.type==='meta')output('Context selected · '+event.data_sent);if(event.type==='delta'){text+=event.text||'';reply.textContent=text;output('OpenRouter · replying…');}}});
    let result=stream.result;
    if(stream.fallback){const response=await fetch('/api/coach',{method:'POST',headers:{'Content-Type':'application/json','X-Boop':'local'},body:JSON.stringify(payload),signal:controller.signal});result=await response.json();if(!response.ok)throw Error(result.error||'AI request failed.');}
    aiSession=result.context_session||aiSession;renderCoachSources(result,text);output('OpenRouter · reply complete · not saved');
  }catch(error){output(error.name==='AbortError'?'AI reply cancelled.':error.message);if(!text)reply.hidden=true;}
  finally{aiController=null;el('termRun').disabled=false;el('termModels').disabled=false;el('termAICancel').hidden=true;reply.removeAttribute('aria-busy');}
}
el('termAICancel').onclick=()=>aiController?.abort();
el('termAIModel').setAttribute('list','termModelList');
el('termVerifyKey').onclick=async()=>{
  const button=el('termVerifyKey'),note=el('termAuthStatus');button.disabled=true;note.textContent='Checking authentication…';
  try{await request('/api/coach/key',{api_key:apiKey(),consent:true});note.textContent='Key accepted by OpenRouter.';}
  catch(error){note.textContent=error.message;}finally{button.disabled=false;}
};
el('termModels').onclick=async()=>{
  const key=apiKey();if(!key){output('Enter your OpenRouter key to load models.');return;}
  el('termModels').disabled=true;output('Loading OpenRouter models · authentication only…');
  try{await request('/api/coach/key',{api_key:key,consent:true});const result=await request('/api/coach/models',{provider:'compatible',endpoint:'https://openrouter.ai/api/v1',api_key:key,consent:true});el('termModelList').replaceChildren(...result.models.map(model=>{const option=document.createElement('option');option.value=model;return option;}));output(`${result.models.length} models loaded. Choose a model in setup.`);}
  catch(error){output(error.message);}finally{el('termModels').disabled=false;}
};
async function command(){
  const input=el('termCommand'),text=input.value.trim();if(!text)return;input.value='';
  const [verb,...args]=text.toLowerCase().split(/\s+/);
  if(actions[verb])return actions[verb]();
  if(verb==='help')return output('AI mode: type a question. Commands mode: ask <question>.\nhelp | scan | connect | disconnect | sync | stop-sync | refresh\nopen today|sleep|activity|health|insights|tools|device|data|settings|coach\nexport csv|sqlite|backup | water <mL> | demo sleep on|off | clear');
  if(verb==='ask')return askAI(text.slice(3).trim());
  if(verb==='demo'&&args[0]==='sleep'&&['on','off'].includes(args[1])){el('termDemoSleep').checked=args[1]==='on';renderDay(daily||{day:el('termDate').value});output('Demo sleep '+args[1]+'. Synthetic preview only.');return;}
  if(verb==='clear'){aiSession=null;el('termAIReply').hidden=true;el('termAISources').hidden=true;return output('Conversation context cleared.');}
  if(verb==='refresh'){personalBaseline=null;lastDay=0;return refresh();}
  if(verb==='open'&&['today','sleep','activity','health','insights','tools','device','data','settings','coach','terminal'].includes(args[0])){location.hash=args[0];return;}
  if(verb==='export'&&['csv','sqlite','backup'].includes(args[0])){const a=document.createElement('a');a.href='/export/'+args[0];a.download='';root.append(a);a.click();a.remove();output('Export requested. Check your browser downloads.');return;}
  if(verb==='water'){
    const amount=Number(args[0]);if(args.length!==1||!Number.isFinite(amount)||amount<=0||amount>10000)return output('Use water <mL>, with an amount from 1 to 10000.');
    if(pending)return output('Wait for the current action to finish.');
    pending=true;
    try{await request('/api/records/hydration',{date:el('termDate').value,amount_ml:amount,source:'manual'});output(`Saved ${fmt(amount)} mL for ${el('termDate').value}.`);await refreshDay();}
    catch(error){output(error.message);}finally{pending=false;if(status)renderStatus(status);}return;
  }
  if(el('termPromptMode').value==='ai')return askAI(text);
  output('Unknown command. Type help for available commands.');
}
el('termRun').onclick=()=>command().catch(e=>output(e.message));
el('termCommand').onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();command().catch(e=>output(e.message));}};
function route(){document.body.classList.toggle('terminal-view',visible());if(visible())refresh();}
window.addEventListener('hashchange',route);
window.addEventListener('boop:day',event=>{if(event.detail.day===el('termDate').value){renderDay(event.detail);lastDay=Date.now();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
if(typeof ResizeObserver!=='undefined')new ResizeObserver(()=>{drawHR(cachedHR);drawSleep(cachedSleep);patterns?.resize();}).observe(root);
request('/api/settings').then(settings=>{if(Number.isFinite(settings.hr_rest))profile.resting_hr=settings.hr_rest;if(Number.isFinite(settings.hr_max))profile.max_hr=settings.hr_max;}).catch(()=>{});
route();setInterval(refresh,2000);
})();
