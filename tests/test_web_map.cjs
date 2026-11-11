// Isolated renderer regression: no SDK, API key, browser or network required.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const controls = { 'show-reference': { checked: true }, 'show-adjusted': { checked: true } };
class Overlay {
  constructor(options) { this.options = options; this.map = options.map; }
  setMap(map) { this.map = map; }
}
const sandbox = {
  window: { kakao: { maps: {
    LatLng: class { constructor(lat, lng) { this.lat = lat; this.lng = lng; } },
    LatLngBounds: class { constructor() { this.points = []; } extend(p) { this.points.push(p); } },
    Polyline: Overlay, CustomOverlay: Overlay,
  } } },
  document: { createElement: () => ({}) },
  $: id => controls[id],
  colors: { reference: 'orange', adjusted: 'purple', approach: 'blue', route: 'green' },
};
const code = source.slice(source.indexOf('function drawKakaoCandidate('), source.indexOf('function renderCandidateMap('));
vm.runInNewContext(code + '\nthis.draw = drawKakaoCandidate;', sandbox);
const makeMap = () => ({ relayout() {}, setBounds(bounds) { this.bounds = bounds; } });
const mainMap = makeMap(), sideMap = makeMap(), main = [], side = [];
const candidate = {
  closed: true, vertex_adjusted: true,
  geometry: { coordinates: [[127, 37], [127.01, 37.01], [127, 37]] },
  reference: [[127, 37], [127.02, 37.01]], adjusted_reference: [[127, 37], [127.015, 37.01]],
  approach: [[126.99, 37], [127, 37]],
};
sandbox.draw(mainMap, main, [126.99, 37], candidate);
sandbox.draw(sideMap, side, [126.99, 37], candidate);
assert.equal(main.length, 6); // 4 lines, reference position, loop start/end.
assert.equal(side.length, 6);
assert(main.every(overlay => overlay.map === mainMap));
assert(side.every(overlay => overlay.map === sideMap));
const route = side.find(overlay => overlay.options.strokeColor === 'green');
assert.equal(route.options.path[0].lng, 127);
assert.equal(route.options.path[0].lat, 37);
assert(sideMap.bounds.points.some(p => p.lng === 126.99));
const oldSide = [...side];
controls['show-reference'].checked = false;
controls['show-adjusted'].checked = false;
sandbox.draw(sideMap, side, [126.99, 37], { ...candidate, closed: false });
assert(oldSide.every(overlay => overlay.map === null));
assert(main.every(overlay => overlay.map === mainMap)); // Switching side never clears main.
assert.equal(side.length, 5); // 2 lines + separate start/end + reference position.
assert(!side.some(overlay => ['orange', 'purple'].includes(overlay.options.strokeColor)));
sandbox.draw(sideMap, side, [126.99, 37], null);
assert.equal(side.length, 1); // Stale route removed when there is no candidate.
let current = candidate;
const elements = { ...controls, results: { hidden: false } };
for (const id of ['candidate-map', 'candidate-schematic', 'candidate-map-empty', 'candidate-map-label', 'candidate-map-status']) elements[id] = {};
const schematicCalls = [];
sandbox.$ = id => elements[id];
sandbox.state = { selected: 0, map: mainMap, graph: { source: 'synthetic' }, candidateOverlays: [] };
sandbox.currentCandidate = () => current;
sandbox.locationValue = () => [127, 37];
sandbox.drawRoadSchematic = (...args) => schematicCalls.push(args);
const candidateCode = source.slice(source.indexOf('function renderCandidateMap('), source.indexOf('function renderMap('));
vm.runInNewContext(candidateCode + '\nthis.render = renderCandidateMap;', sandbox);
sandbox.render();
assert.equal(elements['candidate-map'].hidden, true);
assert.equal(elements['candidate-schematic'].hidden, false);
assert.match(elements['candidate-map-label'].textContent, /가상 도로/);
assert.equal(schematicCalls.length, 1);
sandbox.state.graph.source = 'osm';
sandbox.state.map = null;
sandbox.render();
assert.match(elements['candidate-map-label'].textContent, /배경 지도 없음/);
assert.equal(schematicCalls.length, 2);
current = null;
sandbox.render();
assert.equal(elements['candidate-schematic'].hidden, true);
assert.equal(elements['candidate-map-empty'].hidden, false);
console.log('PASS: independent overlays, selection, coordinates, bounds, toggles, clearing and honest synthetic/no-SDK fallback');
