const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync(require('node:path').join(__dirname, '../api_web/route-map.js'), 'utf8');
const elements = {}, timers = new Map(), overlays = [], requests = [], listeners = {};
let timerId = 0, bounds = [127, 37.25, 127.02, 37.27];
class Element {
  constructor() { this.events = {}; this.children = []; this.checked = false; this.value = 'test-token'; }
  addEventListener(type, fn) { this.events[type] = fn; }
  append(...items) { this.children.push(...items); }
}
const el = id => elements[id] ||= new Element();
const info = { close() {}, setContent(content) { this.content = content; }, setPosition() {}, open() {} };
const maps = {
  InfoWindow: class { constructor() { return info; } },
  LatLng: class { constructor(lat, lng) { this.lat = lat; this.lng = lng; } },
  CustomOverlay: class { constructor(options) { this.options = options; this.map = options.map; overlays.push(this); } setMap(map) { this.map = map; } },
  event: { addListener(map, name, fn) { listeners[name] = fn; }, preventMap() {} },
};
const map = { getBounds: () => ({ getSouthWest: () => ({ getLng: () => bounds[0], getLat: () => bounds[1] }), getNorthEast: () => ({ getLng: () => bounds[2], getLat: () => bounds[3] }) }) };
const sandbox = {
  document: { getElementById: el, createElement: () => new Element() },
  URLSearchParams, AbortController,
  fetch: (url, options) => new Promise(resolve => requests.push({ url, options, resolve })),
  setTimeout(fn, delay) { const id = ++timerId; timers.set(id, { fn, delay }); return id; },
  clearTimeout(id) { timers.delete(id); }, setInterval() {},
};
vm.runInNewContext(source.slice(source.indexOf('function attachFacilityLayer')), sandbox);
sandbox.attachFacilityLayer(maps, map);
function flush() { for (const [id, timer] of [...timers]) if (timer.delay === 450) { timers.delete(id); timer.fn(); } }
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (type, truncated = false) => ({ ok: true, json: async () => ({ items: [{ type, name: '<script>unsafe</script>', location: [127.01, 37.26], address: { road: 'road' } }], count: 1, truncated }) });
(async () => {
  el('map-cctv').checked = true; el('map-toilet').checked = true;
  el('map-cctv').events.change(); flush();
  assert.equal(requests.length, 2);
  assert.equal(requests[0].options.headers.Authorization, 'Bearer test-token');
  assert.match(requests[0].url, /types=cctv/); assert.match(requests[1].url, /types=toilet/);
  requests[0].resolve(response('cctv', true)); requests[1].resolve(response('toilet')); await settle();
  assert.equal(overlays.length, 2); assert.equal(overlays[0].options.position.lat, 37.26);
  assert.match(el('map-facility-status').textContent, /일부만 표시/);
  overlays[0].options.content.events.click({ stopPropagation() {} });
  assert.equal(info.content.children[0].textContent, 'CCTV · <script>unsafe</script>');
  // A slow response from a previous viewport cannot reintroduce old markers.
  listeners.idle(); flush(); const old = requests.slice(2);
  el('map-cctv').checked = false; el('map-toilet').checked = false;
  el('map-cctv').events.change(); flush();
  old.forEach(request => request.resolve(response('cctv'))); await settle();
  assert(overlays.every(overlay => overlay.map === null));
  assert.equal(requests.length, 4);
  el('map-cctv').checked = true; bounds = [126, 36, 128, 38];
  el('map-cctv').events.change(); flush();
  assert.equal(requests.length, 4); assert.match(el('map-facility-status').textContent, /확대/);
  bounds = [127, 37.25, 127.02, 37.27]; listeners.idle(); flush();
  requests[4].resolve({ ok: false, status: 429 }); await settle();
  assert.match(el('map-facility-status').textContent, /429/);
  console.log('PASS: bbox, independent types, auth, coordinate order, safe labels, truncation, stale cancellation, clearing, zoom guard and HTTP errors');
})().catch(error => { console.error(error); process.exitCode = 1; });
