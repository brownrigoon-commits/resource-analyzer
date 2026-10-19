import test from 'node:test';
import assert from 'node:assert/strict';
import {createConfig, mountSpeedTest} from './static/speedtest.mjs';
import ActualEngine from './static/vendor/cloudflare-speedtest-1.14.2.mjs';

function setup({locks} = {}) {
  const elements = new Map();
  const doc = {getElementById(id) {
    if (!elements.has(id)) elements.set(id, {textContent:'', hidden:true, disabled:false, dataset:{},
      handlers:{}, setAttribute(k,v){this[k]=v;}, addEventListener(k,v){this.handlers[k]=v;}});
    return elements.get(id);
  }};
  const engines = [];
  class Engine {
    constructor(config) {this.config=config; this.summary={}; this.results={getSummary:()=>this.summary}; engines.push(this);}
    play(){this.played=true;}
    pause(){this.paused=true;}
  }
  const pendingTimers = new Map();
  const timers = {setTimeout(fn){const id=pendingTimers.size+1;pendingTimers.set(id,fn);return id;},clearTimeout(id){pendingTimers.delete(id);}};
  const controller=mountSpeedTest({doc,Engine,timers,locks,now:()=>new Date('2026-10-20T00:00:00+09:00')});
  return {controller,engines,elements,pendingTimers,$:id=>doc.getElementById(id)};
}

test('passive page load sends no measurement traffic; test excludes logging and packet loss',()=>{
  const s=setup();assert.equal(s.engines.length,0);
  const config=createConfig();assert.equal(config.autoStart,false);assert.equal(config.logAimApiUrl,null);
  assert.equal(config.logMeasurementApiUrl,null);assert.equal(config.includeCredentials,false);
  assert.deepEqual([...new Set(config.measurements.map(m=>m.type))],['latency','download','upload']);
  assert(config.measurements.reduce((sum,m)=>sum+(m.bytes||0)*(m.count||0),0)<170_000_000);
  const real = new ActualEngine(config);
  assert.equal(real.isRunning,false);assert.equal(real.config.logAimApiUrl,null);
  assert.equal(real.config.logMeasurementApiUrl,null);
});

test('click starts once; finish converts bps to Mbps and restores controls',async()=>{
  const s=setup();const completed=s.controller.start();s.controller.start();assert.equal(s.engines.length,1);
  const engine=s.engines[0];assert(engine.played);assert(s.$('speedtest-start').disabled);
  engine.summary={download:125_000_000,upload:42_000_000,latency:7.2};engine.onFinish(engine.results);await completed;
  assert.equal(s.$('speedtest-download').textContent,'125.0');assert.equal(s.$('speedtest-upload').textContent,'42.0');
  assert.equal(s.$('speedtest-panel').dataset.state,'complete');assert.equal(s.$('speedtest-start').disabled,false);
  assert.equal(s.pendingTimers.size,0);
});

test('cancel aborts engine and ignores delayed completion; retry clears old values',async()=>{
  const s=setup();const first=s.controller.start();const old=s.engines[0];s.controller.cancel();await first;
  assert(old.paused);old.summary={download:999e6,upload:999e6};old.onFinish(old.results);
  assert.equal(s.$('speedtest-panel').dataset.state,'cancelled');
  const second=s.controller.start();assert.equal(s.$('speedtest-download').textContent,'—');
  old.onFinish(old.results);assert.equal(s.$('speedtest-download').textContent,'—');s.controller.cancel();await second;
});

test('timeout, network failure and missing upload cannot become a successful zero-speed result',async()=>{
  for(const mode of ['timeout','network','partial']){
    const s=setup();const done=s.controller.start();const engine=s.engines[0];
    if(mode==='timeout') [...s.pendingTimers.values()][0]();
    if(mode==='network') engine.onError('network');
    if(mode==='partial'){engine.summary={download:100e6,upload:0};engine.onFinish(engine.results);}
    await done;assert.equal(s.$('speedtest-panel').dataset.state,'error');assert.equal(s.$('speedtest-upload').textContent,'—');
    assert.equal(s.$('speedtest-start').disabled,false);
  }
});

test('another-tab lock prevents duplicate test traffic',async()=>{
  const s=setup({locks:{request:async(name,options,fn)=>fn(null)}});await s.controller.start();
  assert.equal(s.engines.length,0);assert.equal(s.$('speedtest-start').disabled,false);
  assert(s.$('speedtest-status').textContent.includes('다른 탭'));
});
