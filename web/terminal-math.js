/* Pure chart/statistic helpers. Demo data never enters these status inputs. */
((root,factory)=>{const api=factory();if(typeof module==='object')module.exports=api;else root.BoopTerminalMath=api;})(typeof window==='undefined'?this:window,()=>{
'use strict';
const DAY=86400000,OFFSET=10*3600000;
const valid=points=>(Array.isArray(points)?points:[]).filter(p=>Number.isFinite(p.t)&&Number.isFinite(p.hr)).sort((a,b)=>a.t-b.t);
function rollingMean(points,windowMs,gapMs){
  const result=[];let queue=[],sum=0,previous=null;
  for(const p of valid(points)){
    if(previous!==null&&p.t-previous>gapMs){queue=[];sum=0;}
    queue.push(p);sum+=p.hr;
    while(queue.length&&queue[0].t<p.t-windowMs)sum-=queue.shift().hr;
    result.push({t:p.t,hr:sum/queue.length});previous=p.t;
  }
  return result;
}
function alignDays(points,count,end){
  const midnight=Math.floor((end+OFFSET)/DAY)*DAY-OFFSET,start=midnight-(count-1)*DAY;
  const days=new Map();
  for(const p of valid(points)){
    if(p.t<start||p.t>end)continue;
    const key=Math.floor((p.t+OFFSET)/DAY),offset=(p.t+OFFSET)%DAY;
    if(!days.has(key))days.set(key,[]);days.get(key).push({t:offset,hr:p.hr});
  }
  return [...days.entries()].sort((a,b)=>a[0]-b[0]).map(([day,points])=>({day,points}));
}
function meanCurves(curves,bucketMs){
  const bins=new Map();
  for(const curve of curves){
    const perCurve=new Map();
    for(const p of valid(curve)){
      const key=Math.floor(p.t/bucketMs);
      if(!perCurve.has(key))perCurve.set(key,[]);perCurve.get(key).push(p.hr);
    }
    for(const [key,values]of perCurve){
      if(!bins.has(key))bins.set(key,[]);
      bins.get(key).push(values.reduce((a,b)=>a+b,0)/values.length);
    }
  }
  return [...bins.entries()].sort((a,b)=>a[0]-b[0]).map(([key,values])=>({t:(key+.5)*bucketMs,hr:values.reduce((a,b)=>a+b,0)/values.length,n:values.length}));
}
function statusEstimate(points,rest,max,end){
  const buckets=new Map();
  for(const p of valid(points))if(p.t>=end-3600000&&p.t<=end&&p.hr>=30&&p.hr<=240){
    const key=Math.floor(p.t/60000);if(!buckets.has(key))buckets.set(key,[]);buckets.get(key).push(p.hr);
  }
  if(buckets.size<45||!Number.isFinite(rest)||!Number.isFinite(max)||max<=rest)return {label:'LEARNING',minutes:buckets.size};
  const means=[...buckets.values()].map(a=>a.reduce((x,y)=>x+y,0)/a.length),mean=means.reduce((a,b)=>a+b,0)/means.length;
  const reserve=Math.max(0,(mean-rest)/(max-rest));
  return {label:reserve<=.1?'RELAXED':reserve<=.2?'CALM':reserve<=.4?'ELEVATED':'HIGH LOAD',mean,reserve,minutes:buckets.size};
}
function quantile(values,p){
  const sorted=values.filter(Number.isFinite).sort((a,b)=>a-b);if(!sorted.length)return null;
  const position=(sorted.length-1)*p,lo=Math.floor(position),hi=Math.ceil(position);
  return sorted[lo]+(sorted[hi]-sorted[lo])*(position-lo);
}
function envelopeCurves(curves,bucketMs,min=3){
  const bins=new Map();
  for(const curve of curves){
    const local=new Map();
    for(const point of valid(curve)){const key=Math.floor(point.t/bucketMs);if(!local.has(key))local.set(key,[]);local.get(key).push(point.hr);}
    for(const [key,values]of local){if(!bins.has(key))bins.set(key,[]);bins.get(key).push(values.reduce((a,b)=>a+b,0)/values.length);}
  }
  return [...bins.entries()].sort((a,b)=>a[0]-b[0]).filter(([,values])=>values.length>=min).map(([key,values])=>({t:(key+.5)*bucketMs,low:quantile(values,.25),high:quantile(values,.75),median:quantile(values,.5),n:values.length}));
}
function descriptiveBaseline(values,min=7){
  const clean=values.filter(Number.isFinite),n=clean.length;
  return {n,median:quantile(clean,.5),low:quantile(clean,.25),high:quantile(clean,.75),ready:n>=min,status:n>=14?'established':n>=min?'building':'learning'};
}
function nearestPoint(points,t,tolerance){
  if(!points.length)return null;let low=0,high=points.length;
  while(low<high){const mid=(low+high)>>1;if(points[mid].t<t)low=mid+1;else high=mid;}
  const candidates=[points[low],points[low-1]].filter(Boolean),nearest=candidates.reduce((a,b)=>Math.abs(a.t-t)<=Math.abs(b.t-t)?a:b);
  return Math.abs(nearest.t-t)<=tolerance?nearest:null;
}
function comparisonNights(nights,selected,choice){
  const stamp=day=>Date.parse(day+'T00:00:00+10:00'),end=stamp(selected.day);
  if(choice==='week'||choice==='month')return nights.filter(n=>{const t=stamp(n.day);return t<end&&t>=end-(choice==='week'?7:30)*DAY;});
  if(choice==='yesterday')return nights.filter(n=>stamp(n.day)===end-DAY);
  return choice?nights.filter(n=>n.day===choice&&n.day!==selected.day):[];
}
function sleepTiming(nights,through,count){
  const end=Date.parse(through+'T00:00:00+10:00'),start=end-(count-1)*DAY;
  return nights.filter(n=>{const t=Date.parse(n.day+'T00:00:00+10:00');return t>=start&&t<=end&&Number.isFinite(n.start)&&Number.isFinite(n.end)&&n.end>n.start&&n.end-n.start<=48*3600;}).map(n=>{
    let bed=((n.start*1000+OFFSET)%DAY+DAY)%DAY/3600000;if(bed<12)bed+=24;
    return {...n,bed,wake:bed+(n.end-n.start)/3600};
  }).sort((a,b)=>a.day.localeCompare(b.day));
}
return {DAY,valid,rollingMean,alignDays,meanCurves,statusEstimate,quantile,envelopeCurves,descriptiveBaseline,nearestPoint,comparisonNights,sleepTiming};
});
