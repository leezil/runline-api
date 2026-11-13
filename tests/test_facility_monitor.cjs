const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor() { this.value = ''; this.textContent = ''; this.children = []; this.dataset = {}; this.listeners = {}; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  replaceChildren() { this.children = []; }
  insertRow() { const e = new Element(); this.children.push(e); return e; }
  insertCell() { return this.insertRow(); }
}
const elements = new Map();
const document = new Element();
document.getElementById = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
const el = document.getElementById;
el('facility-log-channel').value = el('facility-log-kind').value = 'all';
el('token').value = 'private-token';
const calls = [];
let fail = false;
const context = {document, URLSearchParams, performance, AbortSignal, fetch:async (path, options) => {
  calls.push({path,options}); if (fail) throw Error('offline');
  return {status:200, headers:{get:()=>null}, json:async()=>({items:[], count:0})};
}};
vm.runInNewContext(fs.readFileSync('api_web/facility-monitor.js','utf8'), context);
document.listeners['facility-log-update']({detail:[
  {path:'/v1/routes',channel:'external',status:200},
  {path:'/v1/places',method:'GET',channel:'external',facility_types:['cctv'],status:200,stage:'completed',time:1,elapsed_s:0.02,facility_count:200,truncated:true},
  {path:'/v1/places',method:'GET',channel:'local',facility_types:['toilet'],status:200,stage:'completed',time:1,elapsed_s:0.01,facility_count:0},
  {path:'/v1/places',method:'GET',channel:'external',status:401,stage:'completed',time:1,elapsed_s:0.01,error_code:'UNAUTHORIZED'}
]});
assert.equal(el('facility-log-body').children.length,3);
assert.match(el('facility-log-summary').textContent,/HTTP 오류 1건/);
assert.match(el('facility-log-body').children[0].children[6].textContent,/추가 결과 있음/);
el('facility-log-kind').value = 'toilet';
el('facility-log-kind').listeners.change();
assert.equal(el('facility-log-body').children.length,1);
assert.equal(el('facility-log-body').children[0].children[6].textContent,'0건');
assert.equal(calls.length,0); // Polling logs never calls a facility endpoint.
(async()=>{
  await el('facility-status-send').listeners.click();
  assert.equal(calls[0].path,'/v1/places/status');
  assert.equal(calls[0].options.headers.Authorization,'Bearer private-token');
  assert.match(el('facility-call-status').textContent,/HTTP 200/);
  assert.doesNotMatch(el('facility-call-response').textContent,/private-token/);
  fail = true;
  await el('facility-status-send').listeners.click();
  assert.match(el('facility-call-status').textContent,/요청 실패/);
  assert.equal(el('facility-status-send').disabled,false);
  console.log('PASS: facility filtering, safe summaries, empty results, explicit requests and network failure');
})().catch(error=>{console.error(error);process.exitCode=1;});
