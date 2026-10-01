/* Retrospective charts. Reads local observations; never writes demo records. */
(() => {
'use strict';
const el=id=>document.getElementById(id),math=window.BoopTerminalMath,DAY=math.DAY;
const node=(tag,attrs,text)=>{const n=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n;};
const clock=hour=>{const minutes=((Math.round(hour*60)%1440)+1440)%1440;return String(Math.floor(minutes/60)).padStart(2,'0')+':'+String(minutes%60).padStart(2,'0');};
const duration=minutes=>Math.floor(Math.round(minutes)/60)+':'+String(Math.round(minutes)%60).padStart(2,'0');
const stamp=day=>Date.parse(day+'T00:00:00+10:00');
const shortDate=day=>new Date(day+'T12:00:00+10:00').toLocaleDateString('en-AU',{day:'numeric',month:'short'});
let config,nights=[],selectedNight='';
const timingDays=14;
let timingModel=null;
function size(svg){const width=Math.max(240,svg.getBoundingClientRect().width||500);svg.setAttribute('viewBox',`0 0 ${width} 190`);svg.replaceChildren();return width;}
function empty(svg,width,message){svg.append(node('text',{x:width/2,y:76,'text-anchor':'middle',class:'term-chart-empty'},message));}
function bind(svg,getModel,inspect,select){
  svg.setAttribute('tabindex','0');svg.setAttribute('aria-describedby','termInspect');let index=0;
  const clear=()=>{for(const item of svg.querySelectorAll('.term-pattern-cursor,.term-scatter-focus'))item.remove();};
  const show=(i,keyboard=false)=>{const model=getModel();if(!model)return;index=Math.max(0,Math.min(model.count-1,i));clear();
    const x=model.x(index);svg.append(node('line',{x1:x,x2:x,y1:8,y2:137,class:'term-pattern-cursor'}));
    el('termInspect').setAttribute('aria-live',keyboard?'polite':'off');el('termInspect').textContent=inspect(index);};
  svg.onpointermove=event=>{const model=getModel();if(!model)return;const x=event.clientX-svg.getBoundingClientRect().left;if(x>=model.left&&x<=model.right)show(Math.floor((x-model.left)/(model.right-model.left)*model.count));};
  svg.onfocus=()=>show(index,true);
  svg.onkeydown=event=>{const model=getModel();if(!model)return;
    if(['ArrowLeft','ArrowRight','Home','End'].includes(event.key)){event.preventDefault();show(event.key==='Home'?0:event.key==='End'?model.count-1:index+(event.key==='ArrowLeft'?-1:1),true);}
    if(event.key==='Escape'){clear();el('termInspect').textContent='Hover or focus a graph · arrows scrub · Esc clears';}
    if(select&&['Enter',' '].includes(event.key)){event.preventDefault();select(index);}
  };
  if(select)svg.onclick=event=>{const model=getModel();if(!model)return;const x=event.clientX-svg.getBoundingClientRect().left;if(x>=model.left&&x<=model.right)select(Math.min(model.count-1,Math.floor((x-model.left)/(model.right-model.left)*model.count)));};
}
function drawTiming(){
  const svg=el('termTimingGraph'),width=size(svg),left=42,right=width-8,through=el('termDate').value,end=stamp(through),start=end-(timingDays-1)*DAY;
  const rows=math.sleepTiming(nights,through,timingDays),demo=el('termDemoSleep').checked;
  timingModel=null;el('termTimingSource').textContent=demo?'DEMO':'ESTIMATED';
  svg.setAttribute('aria-label',`${demo?'Demo':'Estimated'} sleep timing across ${timingDays} nights through ${through}; bedtime and wake time with median reference lines. Select a night to inspect its sleep above.`);
  if(!rows.length){empty(svg,width,'Sleep timing appears after recorded nights');el('termTimingSummary').textContent='No observed nights in this window.';return;}
  const lo=Math.min(18,Math.floor(Math.min(...rows.map(n=>n.bed))/3)*3),hi=Math.max(36,Math.ceil(Math.max(...rows.map(n=>n.wake))/3)*3),x=i=>left+(i+.5)*(right-left)/timingDays,y=h=>12+(h-lo)/(hi-lo)*88;
  for(let hour=lo;hour<=hi;hour+=3){svg.append(node('line',{x1:left,x2:right,y1:y(hour),y2:y(hour),class:'term-gridline'}),node('text',{x:left-6,y:y(hour)+3,'text-anchor':'end'},clock(hour)));}
  const bed=math.quantile(rows.map(n=>n.bed),.5),wake=math.quantile(rows.map(n=>n.wake),.5);
  for(const [value,kind]of [[bed,'bed'],[wake,'wake']])svg.append(node('line',{x1:left,x2:right,y1:y(value),y2:y(value),class:'term-timing-median term-timing-'+kind}));
  const bar=Math.max(3,Math.min(13,(right-left)/timingDays*.42));
  for(const row of rows){const i=Math.round((stamp(row.day)-start)/DAY),focus=row.day===selectedNight;
    svg.append(node('rect',{x:x(i)-bar/2,y:y(row.bed),width:bar,height:Math.max(1,y(row.wake)-y(row.bed)),class:'term-timing-bar'+(focus?' term-timing-selected':'')}));
    for(const segment of row.segments||[])if(['wake','awake'].includes(segment.stage)&&segment.end>segment.start){const a=row.bed+(segment.start-row.start)/3600,b=row.bed+(segment.end-row.start)/3600;if(a>=row.bed&&b<=row.wake)svg.append(node('rect',{x:x(i)-bar/2,y:y(a),width:bar,height:Math.max(1,y(b)-y(a)),class:'term-timing-gap'}));}
    svg.append(node('circle',{cx:x(i),cy:y(row.bed),r:2.6,class:'term-bed-dot'}),node('circle',{cx:x(i),cy:y(row.wake),r:2.6,class:'term-point'}));
  }
  const balances=rows.filter(r=>Number.isFinite(r.total_sleep_min)&&Number.isFinite(r.need_min)).map(r=>({...r,delta:r.total_sleep_min-r.need_min}));
  const debtRows=rows.filter(r=>Number.isFinite(r.sleep_debt_min));
  const positive=Math.max(30,...balances.map(r=>r.delta)),negative=Math.max(30,...balances.map(r=>-r.delta),...debtRows.map(r=>-r.sleep_debt_min)),debtY=v=>115+(positive-v)/(positive+negative)*49;
  svg.append(node('text',{x:0,y:121,class:'term-debt-caption'},'DEBT'),node('line',{x1:left,x2:right,y1:debtY(0),y2:debtY(0),class:'term-gridline'}),node('text',{x:left-5,y:debtY(0)+3,'text-anchor':'end'},'0'));
  for(const row of balances){const i=Math.round((stamp(row.day)-start)/DAY);svg.append(node('rect',{x:x(i)-bar/2,y:Math.min(debtY(row.delta),debtY(0)),width:bar,height:Math.max(.8,Math.abs(debtY(row.delta)-debtY(0))),class:row.delta<0?'term-debt-short':'term-debt-extra'}));}
  let debtPath='',previous=null;
  for(const row of debtRows){const i=Math.round((stamp(row.day)-start)/DAY);debtPath+=(previous===null||i-previous>1?'M':'L')+x(i)+' '+debtY(row.sleep_debt_min)+' ';previous=i;svg.append(node('circle',{cx:x(i),cy:debtY(row.sleep_debt_min),r:1.6,class:'term-debt-dot'}));}
  svg.append(node('path',{d:debtPath,class:'term-debt-trace'}));
  svg.append(node('text',{x:left-5,y:164,'text-anchor':'end'},'-'+numHours(negative)));
  for(const i of [...new Set([0,Math.floor((timingDays-1)/2),timingDays-1])]){const day=new Date(start+i*DAY+10*3600000).toISOString().slice(0,10);svg.append(node('text',{x:x(i),y:184,'text-anchor':i===0?'start':i===timingDays-1?'end':'middle'},shortDate(day)));}
  const spread=Math.round((math.quantile(rows.map(n=>n.wake),.75)-math.quantile(rows.map(n=>n.wake),.25))*60);
  const latest=debtRows.at(-1);
  el('termTimingSummary').textContent=`Median ${clock(bed)} → ${clock(wake)} · ${rows.length}/14 nights`+(latest?` · debt ${latest.sleep_debt_min<0?'-':''}${duration(Math.abs(latest.sleep_debt_min))}`:'');
  el('termTimingSummary').title='Red line: BOOP estimated sleep debt. Bars: nightly sleep minus its configured goal; green surplus, red shortfall. Wake spread '+spread+' minutes (middle 50%). Missing nights stay empty; select a night to inspect it above.';
  const nightAt=i=>rows.find(row=>Math.round((stamp(row.day)-start)/DAY)===i);
  timingModel={left,right,count:timingDays,x};
  bind(svg,()=>timingModel,i=>{const row=nightAt(i),day=new Date(start+i*DAY+10*3600000).toISOString().slice(0,10);return (demo?'DEMO':'ESTIMATED')+' · '+day+' · '+(row?`bed ${clock(row.bed)} → wake ${clock(row.wake)}`+(Number.isFinite(row.total_sleep_min)?` · asleep ${duration(row.total_sleep_min)}`:'')+(Number.isFinite(row.need_min)?` · goal ${duration(row.need_min)}`:'')+(Number.isFinite(row.sleep_debt_min)?` · estimated debt ${row.sleep_debt_min<0?'-':''}${duration(Math.abs(row.sleep_debt_min))}`:'')+' · click / Enter to inspect':'no recorded night');},i=>{const row=nightAt(i);if(row)config.selectNight(row.day);});
}
function numHours(minutes){return (minutes/60).toFixed(1)+'h';}
window.BoopTerminalPatterns={
  init(options){config=options;const template=el('termPatternPanels');document.querySelector('#terminal .term-charts-grid').append(template.content.cloneNode(true));
    window.BoopJournal?.refreshCompact(el('termDate').value);},
  refreshDay(force=false){return window.BoopJournal?.refreshCompact(el('termDate').value,force);},
  sleep(value,focus){nights=value;selectedNight=focus;drawTiming();},
  resize(){drawTiming();}
};
})();
