import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {renderWarningHistory, mountWarningHistory} from './static/warning-history.mjs';

class Element {
  constructor() { this.children=[];this.dataset={};this.hidden=false;this.listeners={};this.textContent=''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children=nodes; }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,fn) { this.listeners[name]=fn; }
  click() { this.listeners.click?.(); }
}
function fixture() {
  const elements=new Map(['status','updated','empty','list','toggle','body','older','latest','error'].map(key=>['resource-report-'+key,new Element()]));
  return {getElementById:id=>elements.get(id),createElement:()=>new Element()};
}
const event = (values={}) => ({event_id:2,title:'CPU 使用 <img src=x>',level:'high',value:'95.0%',last_value:'91.0%',
  started_at:1700000000,last_seen:1700000100,ended_at:1700000200,end_reason:'recovered',sample_count:5,
  reason:'기준 90% 이상',evidence:'관측 근거 전체 설명',note:'측정 기준',program:'Synthetic AI',task:'render.py',...values});
const data = values => ({events:[event()],total:1,active_count:0,collecting:true,stale:false,next_before:null,...values});
const allText = node => [node.textContent,...node.children.map(allText)].join(' ');
const timers = {setTimeout(){return 1;},clearTimeout(){}};

test('compact records show occurrence, program, task and peak as literal text',()=>{
  const doc=fixture();renderWarningHistory(doc,data());
  const list=doc.getElementById('resource-report-list');
  assert.match(allText(list),/2023/);
  assert.match(allText(list),/최고 95.0%/);assert.match(allText(list),/작업: render.py/);
  assert.match(allText(list),/Synthetic AI/);assert.match(allText(list),/<img src=x>/);
  assert.doesNotMatch(allText(list),/마지막 관측|정상 복귀|기준 90|관측 근거 전체 설명|측정 기준/);
  assert(doc.getElementById('resource-report-updated').hidden);
  const walk=node=>{assert(!Object.hasOwn(node,'innerHTML'));node.children.forEach(walk);};walk(list);
});
test('legacy records keep the original program without inventing past tasks',()=>{
  const doc=fixture();renderWarningHistory(doc,data({events:[event({program:null,task:null,
    evidence:'같은 시점 사용량 상위: Old AI 83.0% · Other AI 10.0%'})]}));
  const text=allText(doc.getElementById('resource-report-list'));
  assert.match(text,/프로그램: Old AI · 작업: 이전 기록에 작업명 없음/);
  assert.doesNotMatch(text,/Other AI|83.0%/);
});
test('temperature records distinguish simultaneous activity from proven heat cause',()=>{
  const doc=fixture();renderWarningHistory(doc,data({events:[event({id:'gpu-temperature',value:'85.0°C'})]}));
  assert.match(allText(doc.getElementById('resource-report-list')),/동시 관측: Synthetic AI.*발열 원인 미확인/);
});
test('shared GPU task context is a separate observation and is literal text',()=>{
  const doc=fixture();renderWarningHistory(doc,data({events:[event({task:'공유 GPU 화면 처리 · 대화별 구분 불가',context:'예시 제작 <img> — 파일 수정: voice.py'})]}));
  assert.match(allText(doc.getElementById('resource-report-list')),/동시 작업\(원인 확정 아님\): 예시 제작 <img>/);
});
test('older DWM and voice-design records are readable without altering historical facts',()=>{
  const doc=fixture();renderWarningHistory(doc,data({events:[event({program:'dwm',task:'dwm · 세부 작업 미확인'}),
    event({program:'MY SIA · 마린 음성 제작',task:'vd_design.py'})]}));
  const text=allText(doc.getElementById('resource-report-list'));
  assert.match(text,/Windows 화면 표시 · 작업: 열린 창과 화면 효과를 모니터에 표시/);
  assert.match(text,/새 목소리 만들기 \(Qwen VoiceDesign\)/);
  assert.doesNotMatch(text,/세부 작업 미확인|vd_design.py|프로그램: dwm/);
});
test('a connection error preserves already displayed history',async()=>{
  const doc=fixture();let fail=false;
  const controller=mountWarningHistory({doc,timers,storage:null,request:async()=>{if(fail)throw Error('offline');return data();}});
  await controller.refresh();const previous=allText(doc.getElementById('resource-report-list'));
  fail=true;await controller.refresh();
  assert.equal(allText(doc.getElementById('resource-report-list')),previous);
  assert.match(doc.getElementById('resource-report-error').textContent,/현재 상태는 미확인/);controller.stop();
});
test('collapse is remembered and hides the view without deleting records',async()=>{
  const doc=fixture();const saved=new Map();
  const storage={getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)};
  const controller=mountWarningHistory({doc,timers,storage,request:async()=>data()});await controller.refresh();
  doc.getElementById('resource-report-toggle').click();assert(doc.getElementById('resource-report-body').hidden);
  assert.equal(doc.getElementById('resource-report-list').children.length,1);
  const nextDoc=fixture();const next=mountWarningHistory({doc:nextDoc,timers,storage,request:async()=>data()});
  assert(nextDoc.getElementById('resource-report-body').hidden);
  nextDoc.getElementById('resource-report-toggle').click();assert(!nextDoc.getElementById('resource-report-body').hidden);
  controller.stop();next.stop();
});
test('older pages use stable cursors and latest returns to the head',async()=>{
  const doc=fixture();const requests=[];
  const controller=mountWarningHistory({doc,timers,storage:null,request:async url=>{requests.push(url);return data({next_before:2});}});
  await controller.refresh();doc.getElementById('resource-report-older').click();await controller.refresh();
  assert.match(requests.at(-1),/before=2/);assert(!doc.getElementById('resource-report-latest').hidden);
  doc.getElementById('resource-report-latest').click();await controller.refresh();assert(!requests.at(-1).includes('before='));controller.stop();
});
test('storage denial does not prevent the history from loading',async()=>{
  const doc=fixture();const storage={getItem(){throw Error('denied');},setItem(){throw Error('denied');}};
  const controller=mountWarningHistory({doc,timers,storage,request:async()=>data()});await controller.refresh();
  assert.equal(doc.getElementById('resource-report-list').children.length,1);controller.stop();
});
test('report is above network and the disk card contains capacity information',()=>{
  const html=readFileSync(new URL('./static/index.html',import.meta.url),'utf8');
  assert(html.indexOf('id="resource-report"')<html.indexOf('class="metric-card network-card"'));
  assert(html.includes('id="disk-capacity-summary"'));
});
