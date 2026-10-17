// Execute the actual page script in an offline DOM harness; no browser or network.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../web/workbench.js'), 'utf8');
const symbols = ['SNDK','ANTH','DRAM','OAI','NBIS','EWY','XIAOMI','IONQ','GPRO','TCNT'];
const tick = async () => {for (let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve));};
class Element {
  constructor(id='') {this.id=id;this.value='';this.textContent='';this.disabled=false;this.children=[];this.handlers={};this.dataset={};this.clientWidth=800;}
  addEventListener(name,fn) {this.handlers[name]=fn;}
  replaceChildren() {this.children=[];}
  append(...children) {this.children.push(...children);if(this.id==='symbol' && !this.value) this.value=children[0].value;}
  getContext() {return new Proxy({}, {get:()=>()=>{}});}
  fire(name) {this.handlers[name]?.({preventDefault(){}});}
}
function basis(symbol, paired=true) {
  return {symbol,paired,points:[],summary:{basis_count:0,minutes:0,last_minute_ts:null,data_age_sec:null,p05_bps:null,p50_bps:null,p95_bps:null}};
}
function inventory(symbol) {
  return {symbol,legs:Object.fromEntries(['entropy','lighter'].map(v=>[v,{configured:false,status:'UNKNOWN'}]))};
}
async function setup() {
  const elements=new Map();
  const el=id=>{if(!elements.has(id))elements.set(id,new Element(id));return elements.get(id);};
  el('zone').value='Asia/Shanghai';el('symbol').value='SNDK';
  const buttons=['start','pause','resume','end'].map(action=>{const b=new Element();b.dataset.action=action;return b;});
  const fixture={active:null,sessions:[],events:[]};
  const overrides=new Map();
  const context=vm.createContext({
    document:{getElementById:el,createElement:()=>new Element(),querySelectorAll:()=>buttons},
    window:{devicePixelRatio:1,addEventListener(){}},
    setTimeout(){},setInterval(){},crypto:{randomUUID:()=> 'offline-command-key'},
    ChartTime:{format:ts=>String(ts)},
    fetch:async url=>{
      let value;
      if(overrides.has(url)) value=await overrides.get(url)();
      else if(url==='/api/assets')value={assets:symbols.map(symbol=>({symbol,label:symbol,state:'PAIRED'}))};
      else if(url==='/api/status')value=fixture;
      else if(url.startsWith('/api/basis?'))value=basis(new URL('http://test'+url).searchParams.get('symbol'));
      else if(url.startsWith('/api/inventory?'))value=inventory(new URL('http://test'+url).searchParams.get('symbol'));
      else throw Error('Unexpected endpoint '+url);
      return {ok:true,json:async()=>value};
    }
  });
  vm.runInContext(script,context);await tick();
  return {el,buttons,fixture,overrides,context};
}
test('all ten choices loaded and delayed market response cannot overwrite new selection',async()=>{
  const {el,overrides}=await setup();
  assert.deepEqual(el('symbol').children.map(x=>x.value),symbols);
  let release;
  overrides.set('/api/basis?symbol=NBIS',()=>new Promise(resolve=>release=resolve));
  el('symbol').value='NBIS';el('symbol').fire('change');
  el('symbol').value='XIAOMI';el('symbol').fire('change');await tick();
  assert.match(el('chart-title').textContent,/XIAOMI/);
  release(basis('NBIS'));await tick();
  assert.match(el('chart-title').textContent,/XIAOMI/);
  assert.match(el('basis-summary').textContent,/XIAOMI/);
});
test('single-venue asset shows price-only legend with no basis metrics',async()=>{
  const {el,overrides}=await setup();
  overrides.set('/api/basis?symbol=IONQ',async()=>basis('IONQ',false));
  el('symbol').value='IONQ';el('symbol').fire('change');await tick();
  assert.match(el('chart-title').textContent,/仅 Entropy 价格/);
  assert.match(el('chart-note').textContent,/不生成价差/);
  assert.match(el('basis-summary').textContent,/单边分钟/);
  assert.doesNotMatch(el('basis-summary').textContent,/P05/);
});
test('viewing another asset disables active-session operations without retargeting',async()=>{
  const {el,buttons,fixture,context}=await setup();
  fixture.active={id:'session-nbis',state:'OBSERVING',rules_version:1,rules:{symbol:'NBIS',direction:'BUY_ENTROPY',entry_bps:10,exit_bps:2,order_usd:11,max_inventory_usd:22,duration_minutes:60}};
  await vm.runInContext('refresh()',context);
  assert.ok(buttons.every(b=>b.disabled));
  assert.match(el('session-summary').textContent,/切回该 Session 标的/);
  el('symbol').value='NBIS';el('symbol').fire('change');await tick();
  assert.equal(buttons.find(b=>b.dataset.action==='pause').disabled,false);
  assert.equal(fixture.active.rules.symbol,'NBIS');
});
test('delayed report cannot populate a different asset or enable its download',async()=>{
  const {el,overrides,context}=await setup();
  let release;
  overrides.set('/api/sessions/old/report',()=>new Promise(resolve=>release=resolve));
  const reading=vm.runInContext('loadReport("old")',context);
  el('symbol').value='TCNT';el('symbol').fire('change');await tick();
  release({session:{id:'old',rules:{symbol:'SNDK'}},status:'PRELIMINARY_OBSERVATION'});
  await reading;await tick();
  assert.equal(el('report').textContent,'');
  assert.equal(el('download').disabled,true);
});
