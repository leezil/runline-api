const $ = id => document.getElementById(id);
const state = { drawing: [], graph: null, result: null, selected: 0, map: null, overlays: [], busy: false, picking: false,
  places: null, searchToken: 0, searchKeyword: '', pagination: null, placeId: null,
  candidateMap: null, candidateOverlays: [], overview: null, overviewSelected: 0, localResults: new Map() };
const colors = { route: '#147b65', reference: '#e1994c', adjusted: '#b05287', approach: '#748db4' };
const presets = { forest: [127.0374, 37.5445], olympic: [127.1203, 37.5208], yeouido: [126.9225, 37.5253] };
const ctx = $('drawing').getContext('2d');
let drawingPointer = null;
let loadingMap = false;

function feedback(message, kind = '') {
  $('feedback').textContent = message;
  $('feedback').className = `feedback ${kind}`;
}

function locationValue() {
  const value = [Number($('lng').value), Number($('lat').value)];
  if (!value.every(Number.isFinite) || value[0] < 124 || value[0] > 132 || value[1] < 32 || value[1] > 39) {
    throw new Error('위도와 경도를 확인해주세요. 이번 실험은 한국 지역을 대상으로 합니다.');
  }
  return value;
}

function clearResult() {
  state.overview = null;
  state.localResults.clear();
  state.result = null;
  state.selected = 0;
  $('results').hidden = true;
  $('candidates').replaceChildren();
  $('candidate-filter').value = 'all';
  $('candidate-sort').value = 'quality';
  renderMap();
}

function invalidateGraph() {
  state.picking = false;
  $('pick-location').textContent = '지도에서 위치 선택';
  state.graph = null;
  clearResult();
  $('data-status').textContent = '위치·범위 변경 · 도로 데이터를 다시 불러오세요';
  try {
    const location = locationValue();
    if (state.map) state.map.setCenter(new window.kakao.maps.LatLng(location[1], location[0]));
    feedback('기준 위치·범위가 바뀌었습니다. 이 위치의 OSM 도로를 다시 불러오세요.');
  } catch (error) { feedback(error.message, 'error'); }
  renderMap();
}

function busy(on) {
  state.busy = on;
  for (const element of document.querySelectorAll('button, input, select')) element.disabled = on;
  if (!on) $('pick-location').disabled = !state.map;
  updateSearchControls();
  updateCandidateNavigation();
  $('generate').innerHTML = on ? '처리 중…' : '경로 생성 <span>↗</span>';
}

function updateSearchControls() {
  $('place-search-submit').disabled = state.busy || !state.places;
  $('place-prev').disabled = state.busy || !state.pagination?.hasPrevPage;
  $('place-next').disabled = state.busy || !state.pagination?.hasNextPage;
}

function setReferenceLocation(lng, lat, label, placeId = null) {
  if (!Number.isFinite(lng) || !Number.isFinite(lat) || lng < 124 || lng > 132 || lat < 32 || lat > 39) {
    feedback('지원 범위 밖의 장소입니다. 한국 지역의 장소를 선택해주세요.', 'error');
    return;
  }
  $('lng').value = lng.toFixed(7);
  $('lat').value = lat.toFixed(7);
  $('preset').value = 'custom';
  state.placeId = placeId;
  $('location-name').textContent = `기준 위치: ${label}`;
  for (const button of $('place-results').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.placeId === placeId));
  }
  invalidateGraph();
  feedback(`${label}을 기준 위치로 설정했습니다. 이 위치의 OSM 도로를 불러오세요.`);
}

async function searchPlaces(page = 1) {
  if (state.busy || !state.places) return;
  const keyword = $('place-query').value.trim();
  if (!keyword) { $('place-search-status').textContent = '검색할 장소 이름을 입력해주세요.'; return; }
  const token = ++state.searchToken;
  state.searchKeyword = keyword;
  state.pagination = null;
  $('place-results').replaceChildren();
  $('place-results').hidden = true;
  $('place-pagination').hidden = true;
  $('place-search-status').textContent = '카카오맵에서 장소를 검색하는 중…';
  busy(true);
  try {
    const result = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('장소 검색 시간이 초과되었습니다. 인터넷 연결을 확인하고 다시 검색하세요.')), 12000);
      try {
        state.places.keywordSearch(keyword, (data, status, pagination) => {
          clearTimeout(timer);
          resolve({ data, status, pagination });
        }, { size: 5, page });
      } catch (error) { clearTimeout(timer); reject(error); }
    });
    if (token !== state.searchToken) return;
    const status = window.kakao.maps.services.Status;
    if (result.status === status.ZERO_RESULT) {
      $('place-search-status').textContent = '검색 결과가 없습니다. 지역명과 장소명을 함께 입력해보세요.';
      return;
    }
    if (result.status !== status.OK) throw new Error('장소 검색에 실패했습니다. 카카오 서비스 권한·쿼터 또는 인터넷 연결을 확인해주세요.');
    state.pagination = result.pagination;
    for (const place of result.data) {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'place-result';
      button.dataset.placeId = String(place.id);
      button.setAttribute('aria-pressed', String(state.placeId === String(place.id)));
      const name = document.createElement('span');
      name.textContent = place.place_name;
      const address = document.createElement('small');
      address.textContent = `${place.road_address_name || place.address_name || '주소 정보 없음'} · 기준 위치로 선택`;
      button.append(name, address);
      button.addEventListener('click', () => {
        if (state.busy) return;
        setReferenceLocation(Number(place.x), Number(place.y), place.place_name, String(place.id));
      });
      item.append(button);
      $('place-results').append(item);
    }
    $('place-results').hidden = false;
    $('place-pagination').hidden = !(state.pagination?.last > 1);
    $('place-page').textContent = `${state.pagination?.current || page} / ${state.pagination?.last || 1}`;
    $('place-search-status').textContent = '검색 결과를 선택하면 해당 장소가 기준 위치가 됩니다.';
  } catch (error) {
    if (token === state.searchToken) $('place-search-status').textContent = error.message;
  } finally { if (token === state.searchToken) busy(false); }
}

$('place-search-form').addEventListener('submit', event => { event.preventDefault(); searchPlaces(); });
$('place-prev').addEventListener('click', () => searchPlaces(state.pagination.current - 1));
$('place-next').addEventListener('click', () => searchPlaces(state.pagination.current + 1));
$('place-query').addEventListener('input', () => {
  state.pagination = null;
  $('place-pagination').hidden = true;
  $('place-results').hidden = true;
  updateSearchControls();
});

async function api(path, body) {
  const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `서버 오류 (${response.status})`);
  return data;
}

function drawLine(context, points, color, width = 4, dash = []) {
  if (!points.length) return;
  context.save();
  context.strokeStyle = color;
  context.lineWidth = width;
  context.lineCap = 'round';
  context.lineJoin = 'round';
  context.setLineDash(dash);
  context.beginPath();
  points.forEach((p, i) => i ? context.lineTo(...p) : context.moveTo(...p));
  context.stroke();
  context.restore();
}

function redrawDrawing() {
  ctx.clearRect(0, 0, 600, 600);
  drawLine(ctx, state.drawing, colors.route, 5);
  if (state.drawing.length) {
    ctx.fillStyle = colors.route;
    ctx.beginPath();
    ctx.arc(...state.drawing[0], 5, 0, Math.PI * 2);
    ctx.fill();
  }
  $('drawing-placeholder').hidden = state.drawing.length > 0;
  $('point-count').textContent = `${state.drawing.length} points`;
}

function canvasPoint(event) {
  const r = $('drawing').getBoundingClientRect();
  return [Math.max(0, Math.min(600, (event.clientX - r.left) * 600 / r.width)),
          Math.max(0, Math.min(600, (event.clientY - r.top) * 600 / r.height))];
}

$('drawing').addEventListener('pointerdown', event => {
  if (state.busy || event.button !== 0 || drawingPointer !== null) return;
  clearResult();
  drawingPointer = event.pointerId;
  $('drawing').setPointerCapture(event.pointerId);
  state.drawing = [canvasPoint(event)];
  feedback('새 선을 기록하고 있습니다. 그리기를 마친 뒤 경로 생성을 누르세요.');
  $('draw-notice').textContent = '한 줄로 이어서 그려주세요. 선을 다시 그리면 이전 그림을 대체합니다.';
  redrawDrawing();
});
$('drawing').addEventListener('pointermove', event => {
  if (drawingPointer !== event.pointerId || state.busy) return;
  const point = canvasPoint(event);
  const previous = state.drawing.at(-1);
  if (Math.hypot(point[0] - previous[0], point[1] - previous[1]) < 1.2) return;
  if (state.drawing.length >= 3000) {
    $('draw-notice').textContent = '좌표 3,000개 제한에 도달했습니다. 더 짧은 선으로 다시 그려주세요.';
    return;
  }
  state.drawing.push(point);
  redrawDrawing();
});
function stopDrawing(event) {
  if (event.pointerId !== drawingPointer) return;
  if (event.type === 'pointerup') {
    const p = canvasPoint(event);
    if (state.drawing.length < 3000) state.drawing.push(p);
  }
  drawingPointer = null;
  redrawDrawing();
}
for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) $('drawing').addEventListener(type, stopDrawing);
$('clear').addEventListener('click', () => { state.drawing = []; clearResult(); redrawDrawing(); });

for (const button of document.querySelectorAll('[data-sample]')) button.addEventListener('click', () => {
  const sample = button.dataset.sample;
  const points = [];
  if (sample === 'star') {
    for (let i = 0; i <= 10; i++) {
      const a = -Math.PI / 2 + i * Math.PI / 5;
      const r = i % 2 === 0 ? 220 : 95;
      points.push([300 + Math.cos(a) * r, 300 + Math.sin(a) * r]);
    }
  } else if (sample === 'open') {
    points.push([100, 430], [180, 230], [290, 360], [390, 140], [500, 270]);
  } else {
    for (let i = 0; i <= 120; i++) {
      const t = i * Math.PI * 2 / 120;
      points.push(sample === 'eight' ? [300 + 200 * Math.sin(t), 300 + 190 * Math.sin(2 * t)] : [300 + 210 * Math.cos(t), 300 + 210 * Math.sin(t)]);
    }
  }
  state.drawing = points;
  clearResult();
  redrawDrawing();
  feedback('예제 선을 넣었습니다. 직접 다시 그리면 자유로운 그림으로 바뀝니다.');
});

function mapStatus(message) {
  $('map-status').textContent = message;
  $('map-status').hidden = !message;
}

async function connectMap(key) {
  if (!/^[a-f\d]{32}$/i.test(key)) {
    mapStatus('지도 키 형식이 올바르지 않습니다. .env의 KAKAO_JAVASCRIPT_KEY에 32자리 JavaScript 키를 설정하고 새로고침하세요.');
    return;
  }
  if (state.map) return;
  if (loadingMap) return;
  loadingMap = true;
  mapStatus('카카오맵에 자동으로 연결하는 중…');
  document.getElementById('kakao-sdk')?.remove();
  const script = document.createElement('script');
  script.id = 'kakao-sdk';
  script.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(key)}&autoload=false&libraries=services`;
  try {
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('지도 연결 시간이 초과되었습니다. 인터넷과 도메인 등록을 확인해주세요.')), 15000);
      const finish = fn => value => { clearTimeout(timer); fn(value); };
      script.onerror = finish(reject);
      script.onload = () => {
        if (!window.kakao?.maps?.load) { finish(reject)(new Error('지도 SDK를 불러오지 못했습니다.')); return; }
        window.kakao.maps.load(finish(resolve));
      };
      document.head.append(script);
    });
    const location = locationValue();
    const k = window.kakao.maps;
    state.map = new k.Map($('map'), { center: new k.LatLng(location[1], location[0]), level: 5 });
    state.map.addControl(new k.ZoomControl(), k.ControlPosition.RIGHT);
    if (k.services?.Places) {
      state.places = new k.services.Places();
      $('place-search-status').textContent = '장소를 검색하고 결과를 선택하세요. 검색어는 카카오로 전송됩니다.';
    } else {
      $('place-search-status').textContent = '장소 검색을 불러오지 못했습니다. 페이지를 새로고침해주세요.';
    }
    updateSearchControls();
    k.event.addListener(state.map, 'click', event => {
      if (!state.picking || state.busy) return;
      setReferenceLocation(event.latLng.getLng(), event.latLng.getLat(), '지도에서 선택한 지점');
    });
    new ResizeObserver(() => state.map.relayout()).observe($('map'));
    $('pick-location').disabled = state.busy;
    mapStatus('');
    renderMap();
  } catch {
    mapStatus(`지도 연결 실패: .env의 JavaScript 키, 카카오맵 사용 설정 ON, SDK 도메인 ${window.location.origin} 등록을 확인한 뒤 새로고침하세요.`);
  } finally { loadingMap = false; }
}

$('pick-location').addEventListener('click', () => {
  state.picking = !state.picking;
  $('pick-location').textContent = state.picking ? '선택 취소' : '지도에서 위치 선택';
  if (state.picking) feedback('지도를 클릭하면 그 지점이 기준 위치가 됩니다.');
});
$('preset').addEventListener('change', () => {
  if (presets[$('preset').value]) {
    const [lng, lat] = presets[$('preset').value];
    $('lng').value = lng;
    $('lat').value = lat;
    state.placeId = null;
    $('location-name').textContent = `기준 위치: ${$('preset').selectedOptions[0].textContent}`;
    for (const button of $('place-results').querySelectorAll('button')) button.setAttribute('aria-pressed', 'false');
    invalidateGraph();
  }
});
for (const id of ['lat', 'lng']) $(id).addEventListener('change', () => {
  $('preset').value = 'custom';
  state.placeId = null;
  $('location-name').textContent = '기준 위치: 직접 입력한 좌표';
  for (const button of $('place-results').querySelectorAll('button')) button.setAttribute('aria-pressed', 'false');
  invalidateGraph();
});
$('radius').addEventListener('change', invalidateGraph);
for (const id of ['distance', 'approach', 'route-type', 'tolerance', 'rotation', 'vertex-adjustment', 'candidate-limit']) $(id).addEventListener('change', () => {
  clearResult();
  feedback('러닝 조건이 바뀌었습니다. 경로를 다시 생성하세요.');
});
$('locate').addEventListener('click', () => {
  if (!navigator.geolocation) { feedback('이 브라우저는 위치 기능을 지원하지 않습니다. 좌표를 직접 입력하세요.', 'error'); return; }
  busy(true);
  feedback('현재 위치를 확인하는 중입니다. 브라우저에서 위치 접근을 허용해주세요.', 'busy');
  navigator.geolocation.getCurrentPosition(position => {
    setReferenceLocation(position.coords.longitude, position.coords.latitude, '현재 위치');
    feedback('현재 위치를 설정했습니다. OSM 도로 불러오기를 누르면 내 PC에서 해당 지역의 보행로를 읽습니다.');
    busy(false);
  }, () => { feedback('위치를 확인하지 못했습니다. 권한을 확인하거나 좌표를 직접 입력하세요.', 'error'); busy(false); }, { enableHighAccuracy: true, timeout: 10000 });
});

async function loadGraph(mode, osm) {
  if (state.busy) return;
  try {
    const center = locationValue();
    busy(true);
    feedback(mode === 'roads' ? 'OSM 보행 도로를 불러오는 중… 첫 조회는 최대 40초 정도 걸릴 수 있습니다.' : '실험용 도로 그래프를 준비하는 중…', 'busy');
    const graph = await api(`/api/${mode}`, { center, radius_m: Number($('radius').value), provider: $('provider').value, ...(osm ? { osm } : {}) });
    state.graph = graph;
    clearResult();
    $('data-status').textContent = `${graph.source === 'synthetic' ? '가상 도로 · 실제 러닝 불가' : 'OSM 보행 도로'} / ${graph.nodes.toLocaleString()}개 지점 · ${graph.edges.toLocaleString()}개 연결${graph.cache_stale ? ' · 이전 저장 데이터' : graph.cached ? ' · 캐시' : ''}`;
    feedback(graph.source === 'synthetic' ? '가상 도로 모드입니다. 경로 생성 흐름만 검증하며 실제 러닝 경로가 아닙니다.' : graph.load_warning || '실제 OSM 도로를 불러왔습니다. 선을 그린 뒤 경로 생성을 누르세요.', graph.source === 'synthetic' || graph.cache_stale ? 'warning' : '');
    renderMap();
  } catch (error) { feedback(error.message, 'error'); }
  finally { busy(false); }
}
$('load-roads').addEventListener('click', () => loadGraph('roads'));
$('demo').addEventListener('click', () => loadGraph('demo'));
$('osm-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  if (!file) return;
  try {
    if (file.size > 20 * 1024 * 1024) throw new Error('파일은 20MB 이하여야 합니다.');
    const osm = JSON.parse(await file.text());
    await loadGraph('import', osm);
  } catch (error) { feedback(`OSM 파일 읽기 실패: ${error.message}`, 'error'); }
  finally { event.target.value = ''; }
});

function routePayload() {
  return { drawing: state.drawing, location: locationValue(), distance_m: Number($('distance').value) * 1000,
    approach_m: Number($('approach').value), tolerance: Number($('tolerance').value),
    route_type: $('route-type').value, allow_rotation: $('rotation').checked,
    candidate_limit: Number($('candidate-limit').value), search_mode: 'overview',
    allow_vertex_adjustment: $('vertex-adjustment').checked, graph_id: state.graph?.graph_id };
}
$('route-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (state.busy) return;
  if (!state.drawing.length || state.drawing.length < 2) { feedback('빈 캔버스에 선을 먼저 그려주세요.', 'error'); return; }
  if (!state.graph) { feedback('테스트 위치의 OSM 도로를 먼저 불러오세요. 오프라인이라면 가상 도로 실험을 선택할 수 있습니다.', 'error'); return; }
  try {
    const payload = routePayload();
    clearResult();
    busy(true);
    feedback('여러 출발 위치의 대표 경로를 탐색하는 중… 약 45초까지 걸릴 수 있습니다.', 'busy');
    state.result = await api('/api/generate', payload);
    state.result.input = payload;
    feedback(state.result.message, state.result.found && state.result.source !== 'synthetic' ? '' : 'warning');
    renderResults();
  } catch (error) { feedback(error.message, 'error'); }
  finally { busy(false); }
});

function visibleCandidateIndices() {
  if (!state.result) return [];
  const filter = $('candidate-filter').value, sort = $('candidate-sort').value;
  const target = state.result.input.distance_m;
  const metric = candidate => sort === 'mean' ? candidate.mean_error_m : sort === 'approach' ? candidate.approach_m
    : sort === 'distance' ? Math.abs(candidate.distance_m - target) : candidate.score;
  return state.result.candidates.map((_, index) => index)
    .filter(index => filter === 'all' || state.result.candidates[index].acceptable === (filter === 'recommended'))
    .sort((a, b) => (sort === 'quality' ? Number(!state.result.candidates[a].acceptable) - Number(!state.result.candidates[b].acceptable) : 0)
      || metric(state.result.candidates[a]) - metric(state.result.candidates[b]) || a - b);
}

function updateCandidateNavigation() {
  const indices = visibleCandidateIndices(), position = indices.indexOf(state.selected);
  $('candidate-prev').disabled = state.busy || position <= 0;
  $('candidate-next').disabled = state.busy || position < 0 || position >= indices.length - 1;
  $('show-selected-map').disabled = state.busy || !currentCandidate();
  $('explore-location').disabled = state.busy || !currentCandidate();
  $('explore-location').hidden = state.result?.search_mode === 'local';
  $('back-locations').hidden = !state.overview;
  $('back-locations').disabled = state.busy;
}

$('explore-location').addEventListener('click', async () => {
  const candidate = currentCandidate();
  if (!candidate || state.busy) return;
  const overview = state.result, selected = state.selected;
  const focus = candidate.location_center || candidate.geometry.coordinates[0];
  const key = JSON.stringify(focus);
  try {
    busy(true);
    feedback('선택한 출발 위치 주변 250m에서 다른 경로를 탐색하는 중…', 'busy');
    const payload = { ...overview.input, search_mode: 'local', focus_location: focus };
    let result = state.localResults.get(key);
    if (!result) {
      result = await api('/api/generate', payload);
      result.input = payload;
      state.localResults.set(key, result);
    }
    state.overview = overview;
    state.overviewSelected = selected;
    state.result = result;
    state.selected = 0;
    $('candidate-filter').value = 'all';
    feedback(result.message, result.found && result.source !== 'synthetic' ? '' : 'warning');
    renderResults();
  } catch (error) { feedback(error.message, 'error'); }
  finally { busy(false); }
});
$('back-locations').addEventListener('click', () => {
  if (!state.overview || state.busy) return;
  state.result = state.overview;
  state.selected = state.overviewSelected;
  state.overview = null;
  $('candidate-filter').value = 'all';
  feedback(state.result.message, state.result.found ? '' : 'warning');
  renderResults();
});

for (const id of ['candidate-filter', 'candidate-sort']) $(id).addEventListener('change', () => renderResults());
for (const [id, delta] of [['candidate-prev', -1], ['candidate-next', 1]]) $(id).addEventListener('click', () => {
  const indices = visibleCandidateIndices(), next = indices.indexOf(state.selected) + delta;
  if (next >= 0 && next < indices.length) { state.selected = indices[next]; renderResults(); }
});
$('show-selected-map').addEventListener('click', () => $('map').closest('.map-panel').scrollIntoView({ behavior: 'smooth', block: 'start' }));

function renderResults() {
  const result = state.result;
  $('results').hidden = false;
  $('candidates').replaceChildren();
  const indices = visibleCandidateIndices();
  if (!indices.includes(state.selected)) state.selected = indices[0] ?? -1;
  const recommended = result.candidates.filter(candidate => candidate.acceptable).length;
  $('location-view').textContent = result.search_mode === 'overview'
    ? '위치별 대표 경로 · 가까운 출발점은 250m 범위로 묶어 한 개씩 표시합니다. 위치를 선택하고 주변 경로 더 찾기를 누르세요.'
    : '선택한 위치 주변 250m의 상세 경로 · 기준 위치와 최대 이동 거리 조건은 그대로 적용됩니다.';
  const totalLabel = result.search_mode === 'overview' ? `${result.candidates.length}곳의 대표 경로` : `주변 경로 ${result.candidates.length}개`;
  $('candidate-count').textContent = `${totalLabel} · 모양 조건 충족 ${recommended}개 · 비교용 ${result.candidates.length - recommended}개 / 현재 ${indices.length}개 표시`;
  if (!indices.length) {
    const empty = document.createElement('p');
    empty.className = 'help';
    empty.textContent = result.candidates.length ? '이 분류의 후보가 없습니다. 다른 보기 옵션을 선택하세요.' : '조건을 만족하는 연결 경로가 없습니다. 위치나 거리를 바꾸어 다시 탐색하세요.';
    $('candidates').append(empty);
  }
  indices.forEach(index => {
    const candidate = result.candidates[index];
    const button = document.createElement('button');
    button.className = `candidate ${index === state.selected ? 'active' : ''}`;
    button.setAttribute('aria-pressed', String(index === state.selected));
    button.innerHTML = `<div class="candidate-top"><span>후보 ${String(index + 1).padStart(2, '0')}</span><span>${candidate.acceptable ? '모양 조건 충족' : '모양 조건 미달 · 비교용'}${result.source === 'synthetic' ? ' / 가상' : ''}</span></div><div class="candidate-metrics"><div><span class="metric-value">${(candidate.distance_m / 1000).toFixed(2)} km</span><span class="metric-label">러닝 거리</span></div><div><span class="metric-value">${Math.round(candidate.approach_m)} m</span><span class="metric-label">출발점 접근 추정</span></div><div><span class="metric-value">${candidate.mean_error_m} m</span><span class="metric-label">평균 모양 오차 · 낮을수록 유사</span></div></div><div class="candidate-detail">순서 오차 ${candidate.frechet_m}m · 회전 ${candidate.rotation}° · ${candidate.closed ? '순환 경로' : '열린 경로'} · 도로 연결 미검증 구간 ${candidate.snap_gap_m}m</div>`;
    const detail = document.createElement('div');
    detail.className = 'candidate-detail';
    detail.textContent = `상위 95% 선 오차 ${candidate.p95_error_m?.toFixed(1) ?? '-'}m · 굴곡 누락 ${candidate.missing_corners ?? '-'} / ${candidate.corner_count ?? '-'} · 굴곡 각도 오차 ${candidate.corner_error_deg?.toFixed(1) ?? '-'}° · 우회 배율 ${candidate.detour_ratio?.toFixed(2) ?? '-'}`;
    button.append(detail);
    if (result.search_mode === 'overview') {
      const group = document.createElement('div');
      group.className = 'candidate-detail';
      group.textContent = `위치 ${index + 1} 대표 · 이번 탐색에서 주변 경로 ${candidate.location_candidate_count}개 발견`;
      button.append(group);
    }
    const adjustment = document.createElement('div');
    adjustment.className = 'candidate-detail';
    const shifts = candidate.vertex_adjustment;
    adjustment.textContent = candidate.vertex_adjusted
      ? `꼭짓점 ${shifts.vertices_moved}개 조정 · 평균 이동 ${shifts.vertex_mean_shift_m.toFixed(1)}m / 최대 ${shifts.vertex_max_shift_m.toFixed(1)}m · 조정 선 대비 오차 ${candidate.adjusted_mean_error_m}m (추천 판정은 원본 기준)`
      : '꼭짓점 조정 없음 · 원본 배치 기준으로 탐색한 경로';
    button.append(adjustment);
    const retrace = document.createElement('div');
    retrace.className = 'candidate-detail';
    retrace.textContent = `같은 도로 재사용 ${candidate.road_retrace_m ?? 0}m · 불필요한 왕복 ${candidate.unwanted_retrace_m ?? 0}m / 좁은 평행 왕복 ${candidate.unwanted_near_retrace_m ?? 0}m${candidate.road_retrace_m > 0 ? ' (그림의 되짚기 구간만 허용)' : ''}`;
    button.append(retrace);
    if (candidate.trimmed_corner_loops > 0) {
      const cleanup = document.createElement('div');
      cleanup.className = 'candidate-detail';
      cleanup.textContent = `불필요한 작은 고리 ${candidate.trimmed_corner_loops}개 제거 · ${candidate.trimmed_corner_loop_m}m 단축 (제거 후 거리·모양 재평가)`;
      button.append(cleanup);
    }
    const refinement = candidate.detour_refinement;
    if (refinement?.replacements > 0 || candidate.relaxed_checkpoints) {
      const improvement = document.createElement('div');
      improvement.className = 'candidate-detail';
      improvement.textContent = [
        candidate.relaxed_checkpoints ? '중간 지점 유연 탐색' : '',
        refinement?.replacements > 0 ? `실제 도로 대안 ${refinement.replacements}구간 교체 · ${refinement.removed_m}m 단축` : '',
        '원본 모양·거리·왕복 조건 재검사'
      ].filter(Boolean).join(' · ');
      button.append(improvement);
    }
    if (candidate.narrow_return_warning) {
      const warning = document.createElement('p');
      warning.className = 'narrow-warning';
      warning.textContent = '좁은 평행 우회 주의: 서로 다른 도로라도 불필요한 우회인지 지도에서 확인하세요.';
      button.append(warning);
    }
    const thumbnail = document.createElement('canvas');
    thumbnail.width = 180; thumbnail.height = 120; thumbnail.className = 'candidate-thumbnail';
    thumbnail.setAttribute('aria-hidden', 'true');
    const origin = candidate.reference[0];
    const lines = [candidate.reference, candidate.geometry.coordinates].map(points => points.map(p => geoToMeters(p, origin)));
    const transform = fitTransform(lines.flat(), 180, 120, 12), context = thumbnail.getContext('2d');
    drawLine(context, lines[0].map(transform), colors.reference, 2, [4, 3]);
    drawLine(context, lines[1].map(transform), colors.route, 2.5);
    button.prepend(thumbnail);
    button.addEventListener('click', () => { state.selected = index; renderResults(); });
    $('candidates').append(button);
  });
  const diagnostics = result.diagnostics;
  const counts = diagnostics ? ` · 완성 경로 ${diagnostics.completed_routes}개 / 거리 미달·초과 제외 ${diagnostics.length_rejected}개 / 모양 기준 미달 ${diagnostics.shape_rejected}개 / 최종 왕복 검사 제외 ${diagnostics.retrace_rejected ?? 0}개 (좁은 평행 왕복 ${diagnostics.near_retrace_rejected ?? 0}개 포함) / 작은 고리 제거 ${diagnostics.corner_loops_trimmed ?? 0}회 · ${diagnostics.corner_loop_m_removed ?? 0}m (탈락·중복 후보 처리도 포함)` : '';
  const coverage = result.starts_tested !== undefined ? ` · 출발점 ${result.starts_tested}곳 탐색 / ${result.starts_screened}곳 선별 · 최대 접근 ${result.approach_limit_m}m 범위의 ${result.approach_bands_tested.length}/4 거리 구간 탐색` : '';
  const elastic = result.vertex_adjustment_enabled ? ` · 꼭짓점 조정 ${result.elastic_poses_tested}개 배치 / 연결 경로 ${result.elastic_routes_completed}개` : ' · 꼭짓점 조정 꺼짐';
  const alternatives = ` · 같은 배치의 다른 길 ${result.alternative_poses_tested ?? 0}개 배치 / 연결 경로 ${result.alternative_routes_completed ?? 0}개`;
  const improvements = ` · 중간 지점 유연 탐색 ${result.relaxed_poses_tested ?? 0}개 / 연결 ${result.relaxed_routes_completed ?? 0}개 · 우회 개선 ${result.detour_routes_tested ?? 0}개 경로 / 대안 검사 ${diagnostics?.detour_attempts ?? 0}회 / 교체 ${diagnostics?.detour_replacements ?? 0}회 (중복 후보 포함)`;
  $('search-info').textContent = `${result.elapsed_s}초 · 배치 후보 ${result.poses_screened}개 / 정밀 탐색 ${result.poses_tested}개${coverage}${counts}${alternatives}${elastic}${result.search_limited ? ' · 일부 배치 탐색 시간 제한' : ''}. 그림에 없는 같은 도로 왕복과 좁은 평행 왕복은 전체 모양 점수가 좋아도 제외합니다. 작은 고리는 실제 도로 연결을 유지하며 제거하고 거리·모양을 다시 검사합니다. 원본의 의도한 되짚기·교차는 유지합니다. 접근 거리는 최대 허용 거리입니다. 오차와 굴곡 기준은 실험용이며 검증된 유사도 %가 아닙니다.`;
  $('search-info').textContent += improvements;
  $('selected-candidate').textContent = currentCandidate() ? `후보 ${String(state.selected + 1).padStart(2, '0')}` : '선택할 후보 없음';
  updateCandidateNavigation();
  renderMap();
  renderComparison();
}

function currentCandidate() { return state.result?.candidates[state.selected]; }
function geoToMeters(point, center) {
  return [(point[0] - center[0]) * 111195 * Math.cos(center[1] * Math.PI / 180), (point[1] - center[1]) * 111195];
}

function fitTransform(points, width, height, padding = 38) {
  if (!points.length) return p => p;
  const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const scale = Math.min((width - padding * 2) / Math.max(1, maxX - minX), (height - padding * 2) / Math.max(1, maxY - minY));
  return p => [width / 2 + (p[0] - (minX + maxX) / 2) * scale, height / 2 - (p[1] - (minY + maxY) / 2) * scale];
}

function prepCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const bounds = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(bounds.width * ratio));
  canvas.height = Math.max(1, Math.round(bounds.height * ratio));
  const context = canvas.getContext('2d');
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return [context, bounds.width, bounds.height];
}

function renderComparison() {
  const candidate = currentCandidate();
  const [context, width, height] = prepCanvas($('comparison'));
  context.clearRect(0, 0, width, height);
  if (!candidate) return;
  const center = state.graph.center;
  const route = candidate.geometry.coordinates.map(p => geoToMeters(p, center));
  const reference = candidate.reference.map(p => geoToMeters(p, center));
  const adjusted = (candidate.adjusted_reference || candidate.reference).map(p => geoToMeters(p, center));
  const transform = fitTransform([...route, ...reference, ...adjusted], width, height, 28);
  drawLine(context, reference.map(transform), colors.reference, 2.5, [6, 5]);
  if (candidate.vertex_adjusted && $('show-adjusted').checked) drawLine(context, adjusted.map(transform), colors.adjusted, 2.5, [3, 4]);
  drawLine(context, route.map(transform), colors.route, 3.5);
}

function drawKakaoCandidate(map, overlays, location, candidate) {
  for (const overlay of overlays) overlay.setMap(null);
  overlays.length = 0;
  map.relayout();
  const k = window.kakao.maps;
  const latlng = p => new k.LatLng(p[1], p[0]);
  const addLine = (points, color, dashed = false, weight = 5) => {
    if (points.length < 2) return;
    overlays.push(new k.Polyline({ map, path: points.map(latlng), strokeWeight: weight, strokeColor: color, strokeOpacity: .9, strokeStyle: dashed ? 'dash' : 'solid' }));
  };
  const addMarker = (point, label, cls = '') => {
    const content = document.createElement('span');
    content.className = `map-marker ${cls}`;
    content.textContent = label;
    overlays.push(new k.CustomOverlay({ map, position: latlng(point), content, yAnchor: 1.4 }));
  };
  addMarker(location, '기준 위치', 'user');
  if (!candidate) return;
  if ($('show-reference').checked) addLine(candidate.reference, colors.reference, true, 3);
  if (candidate.vertex_adjusted && $('show-adjusted').checked) addLine(candidate.adjusted_reference, colors.adjusted, true, 3);
  addLine(candidate.approach, colors.approach, true, 3);
  const route = candidate.geometry.coordinates;
  addLine(route, colors.route);
  addMarker(route[0], candidate.closed ? '출발 / 도착' : '출발');
  if (!candidate.closed) addMarker(route.at(-1), '도착', 'end');
  const bounds = new k.LatLngBounds();
  [...route, ...candidate.reference, ...(candidate.adjusted_reference || []), ...candidate.approach, location].forEach(p => bounds.extend(latlng(p)));
  map.setBounds(bounds, 45, 35, 55, 35);
}

function renderCandidateMap() {
  const candidate = currentCandidate();
  const synthetic = state.graph?.source === 'synthetic';
  const visible = Boolean(candidate && !$('results').hidden);
  const useMap = Boolean(visible && state.map && !synthetic);
  for (const overlay of state.candidateOverlays) overlay.setMap(null);
  state.candidateOverlays.length = 0;
  $('candidate-map').hidden = !useMap;
  $('candidate-schematic').hidden = !visible || useMap;
  $('candidate-map-empty').hidden = visible;
  $('candidate-map-label').hidden = !visible || useMap;
  $('candidate-map-label').textContent = synthetic ? '가상 도로 · 실제 지도·러닝 경로 아님' : '배경 지도 없음 · OSM 도로 도식';
  $('candidate-map-status').textContent = candidate ? `후보 ${String(state.selected + 1).padStart(2, '0')} · ${candidate.acceptable ? '모양 조건 충족' : '모양 조건 미달 · 비교용'}${synthetic ? ' · 가상 도로' : ''}` : '선택할 후보가 없습니다.';
  if (!visible) return;
  let location;
  try { location = locationValue(); } catch { return; }
  if (useMap) {
    if (!state.candidateMap) {
      const k = window.kakao.maps;
      state.candidateMap = new k.Map($('candidate-map'), { center: new k.LatLng(location[1], location[0]), level: 5 });
      state.candidateMap.addControl(new k.ZoomControl(), k.ControlPosition.RIGHT);
    }
    drawKakaoCandidate(state.candidateMap, state.candidateOverlays, location, candidate);
  } else drawRoadSchematic($('candidate-schematic'), candidate, location);
}

function renderMap() {
  renderCandidateMap();
  for (const overlay of state.overlays) overlay.setMap(null);
  state.overlays.length = 0;
  const synthetic = state.graph?.source === 'synthetic';
  const useMap = state.map && !synthetic;
  $('map').hidden = !useMap;
  $('schematic').hidden = Boolean(useMap);
  $('map-empty').hidden = Boolean(useMap || state.graph);
  $('map-label').hidden = Boolean(useMap || !state.graph);
  $('map-label').textContent = synthetic ? '가상 도로 · 실제 지도·러닝 경로 아님' : '배경 지도 없음 · OSM 도로 도식';
  $('map-badge').textContent = synthetic ? '가상 도로 모드' : useMap ? 'KAKAO MAP' : state.graph ? 'OSM 도로 도식' : '지도 미연결';
  const candidate = currentCandidate();
  let location;
  try { location = locationValue(); } catch { return; }
  if (useMap) {
    drawKakaoCandidate(state.map, state.overlays, location, candidate);
    return;
  }
  drawRoadSchematic($('schematic'), candidate, location);
}

function drawRoadSchematic(canvas, candidate, location) {
  const [context, width, height] = prepCanvas(canvas);
  context.clearRect(0, 0, width, height);
  if (!state.graph) return;
  const center = state.graph.center;
  const meter = p => geoToMeters(p, center);
  const roads = state.graph.roads;
  const base = state.graph.radius_m;
  const extent = candidate ? [...candidate.geometry.coordinates, ...candidate.reference, ...(candidate.adjusted_reference || []), location].map(meter) : [[-base, -base], [base, base]];
  const transform = fitTransform(extent, width, height, 65);
  const plot = p => transform(meter(p));
  context.save();
  context.beginPath();
  context.rect(0, 0, width, height);
  context.clip();
  for (const road of roads) drawLine(context, road.map(plot), '#c5d4c5', 1.3);
  if (candidate) {
    if ($('show-reference').checked) drawLine(context, candidate.reference.map(plot), colors.reference, 2.5, [7, 5]);
    if (candidate.vertex_adjusted && $('show-adjusted').checked) drawLine(context, candidate.adjusted_reference.map(plot), colors.adjusted, 2.5, [3, 4]);
    drawLine(context, candidate.approach.map(plot), colors.approach, 3, [6, 5]);
    drawLine(context, candidate.geometry.coordinates.map(plot), colors.route, 4);
    const start = plot(candidate.geometry.coordinates[0]);
    context.fillStyle = colors.route;
    context.beginPath(); context.arc(...start, 5, 0, Math.PI * 2); context.fill();
    context.font = '11px sans-serif'; context.fillText(candidate.closed ? '출발 / 도착' : '출발', start[0] + 9, start[1] - 8);
  }
  const user = plot(location);
  context.fillStyle = colors.approach;
  context.beginPath(); context.arc(...user, 4, 0, Math.PI * 2); context.fill();
  context.restore();
}
$('show-reference').addEventListener('change', renderMap);
$('show-adjusted').addEventListener('change', () => { renderMap(); renderComparison(); });
$('shape-comparison').addEventListener('toggle', () => { if ($('shape-comparison').open && state.result) renderComparison(); });
new ResizeObserver(() => { if (state.result) renderCandidateMap(); }).observe(document.querySelector('.candidate-map-wrap'));
new ResizeObserver(() => { renderMap(); if (state.result) renderComparison(); }).observe($('map-panel') || document.querySelector('.map-panel'));
window.addEventListener('resize', () => { renderMap(); if (state.result) renderComparison(); });

$('export').addEventListener('click', () => {
  if (!state.result) return;
  const exportData = { schema: 'drawrun-experiment-v1', created_at: new Date().toISOString(),
    graph: { center: state.graph.center, radius_m: state.graph.radius_m, source: state.graph.source, osm_timestamp: state.graph.osm_timestamp }, result: state.result };
  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `drawrun-${Date.now()}.json`; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function initialize() {
  redrawDrawing();
  renderMap();
  try {
    const response = await fetch('/api/config');
    if (!response.ok) throw new Error('설정을 불러오지 못했습니다.');
    const config = await response.json();
    if (config.kakao_javascript_key) await connectMap(config.kakao_javascript_key);
    else mapStatus('지도 키가 설정되지 않았습니다. .env의 KAKAO_JAVASCRIPT_KEY에 JavaScript 키를 넣고 새로고침하세요.');
  } catch {
    mapStatus('지도 설정을 불러오지 못했습니다. 로컬 서버 연결을 확인하고 새로고침하세요.');
  }
}
initialize();
