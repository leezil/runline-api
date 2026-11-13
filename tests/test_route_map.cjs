// Kakao adapter regression tests without credentials or network calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
class Element {
  constructor() { this.value = ''; this.events = {}; this.children = []; this.hidden = true; }
  addEventListener(name, fn) { this.events[name] = fn; }
  dispatchEvent(event) { this.events[event.type]?.(event); }
  append(item) { this.children.push(item); }
  replaceChildren() { this.children = []; }
}
const el = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
el('lng').value = '127'; el('lat').value = '37';
let keywordCallback, addressCallback;
const overlays = [], mapInstances = [];
class Overlay {
  constructor(options) { this.options = options; this.map = options.map; overlays.push(this); }
  setMap(map) { this.map = map; }
  setPosition(point) { this.position = point; }
}
const maps = {
  load(fn) { fn(); },
  LatLng: class { constructor(lat, lng) { this.lat = lat; this.lng = lng; } },
  LatLngBounds: class { constructor() { this.points = []; } extend(p) { this.points.push(p); } },
  Map: class {
    constructor(container, options) { this.center = options.center; mapInstances.push(this); }
    setCenter(center) { this.center = center; } getCenter() { return this.center; }
    setBounds(bounds) { this.bounds = bounds; } relayout() {}
  },
  Marker: Overlay, Polyline: Overlay,
  InfoWindow: class { close() {} },
  event: { addListener(map, event, fn) { map[event] = fn; } },
  services: {
    Status: { OK: 'OK', ZERO_RESULT: 'ZERO', ERROR: 'ERROR' },
    Places: class { keywordSearch(query, callback) { keywordCallback = callback; } },
    Geocoder: class { addressSearch(query, callback) { addressCallback = callback; } },
  },
};
const sandbox = {
  document: { getElementById: el, createElement: () => new Element(), head: { append: script => script.onload() } },
  window: { kakao: { maps } }, location: { origin: 'http://localhost:8082' },
  fetch: async () => ({ ok: true, json: async () => ({ kakao_javascript_key: 'a'.repeat(32) }) }),
  Event: class { constructor(type) { this.type = type; } },
  ResizeObserver: class { observe() {} }, setTimeout, clearTimeout, setInterval() {},
};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname, '../api_web/route-map.js'), 'utf8'), sandbox);
setImmediate(() => {
  assert.equal(el('location-search-button').disabled, false);
  let changes = 0; el('lng').addEventListener('change', () => changes++);
  const search = query => { el('location-keyword').value = query; el('location-search').events.submit({ preventDefault() {} }); };
  search('station');
  keywordCallback([{ x: '126.9', y: '37.3', place_name: '<test>', address_name: 'address' }], 'OK');
  el('location-results').children[0].events.click();
  assert.equal(el('lng').value, 126.9); assert.equal(el('lat').value, 37.3); assert.equal(changes, 1);
  assert.equal(mapInstances[0].center.lng, 126.9);
  const stale = keywordCallback; search('new'); stale([{ x: '127', y: '37' }], 'OK');
  assert.equal(el('location-results').children.length, 0);
  keywordCallback([], 'ZERO'); addressCallback([{ x: '127', y: '37', address_name: 'road' }], 'OK');
  assert.equal(el('location-results').children[0].textContent, 'road');
  mapInstances[0].click({ latLng: { getLng: () => 127.1, getLat: () => 37.2 } });
  assert.equal(el('lng').value, 127.1); assert.equal(changes, 2);
  const candidate = { geometry: { coordinates: [[127, 37], [127.01, 37.01]] }, reference: [[127, 37]], approach: [[126.99, 37]] };
  const api = sandbox.window.routeMap;
  api.render(candidate, 'osm');
  assert.equal(el('result-map').hidden, false);
  const line = overlays.find(item => item.options.strokeColor === '#167c6a');
  assert.equal(line.options.path[0].lat, 37); assert.equal(line.options.path[0].lng, 127);
  assert(mapInstances[1].bounds.points.some(point => point.lng === 126.99));
  const previous = overlays.slice(1);
  api.render({ ...candidate, closed: true }, 'osm');
  assert(previous.every(item => item.map === null));
  api.render(candidate, 'synthetic');
  assert.equal(el('result-map').hidden, true);
  assert(overlays.slice(1).every(item => item.map === null));
  api.clear();
  console.log('PASS: place/address search, stale searches, map selection, coordinate order, bounds, candidate cleanup and synthetic fallback');
});
