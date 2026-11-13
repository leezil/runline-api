"use strict";
(() => {
  const el = id => document.getElementById(id);
  let maps, locationMap, resultMap, marker, places, geocoder;
  let overlays = [], pending = null, searchVersion = 0;
  const status = message => { el("map-status").textContent = message; };
  const latLng = point => new maps.LatLng(point[1], point[0]);
  const valid = point => point.length === 2 && point.every(Number.isFinite)
    && point[0] >= 124 && point[0] <= 132 && point[1] >= 32 && point[1] <= 39;
  function syncLocation() {
    const point = [Number(el("lng").value), Number(el("lat").value)];
    if (!maps || !valid(point)) return;
    marker.setPosition(latLng(point)); locationMap.setCenter(latLng(point));
  }
  function select(point, name) {
    if (!valid(point)) return;
    el("lng").value = point[0]; el("lat").value = point[1];
    el("lng").dispatchEvent(new Event("change", { bubbles: true }));
    syncLocation(); status(`${name} · 기준 위치에 반영했습니다.`);
  }
  function clear() {
    pending = null;
    overlays.forEach(item => item.setMap(null)); overlays = [];
    el("result-map").hidden = true;
  }
  function render(candidate, source) {
    clear(); pending = { candidate, source };
    // Synthetic coordinates stay in the explicitly labelled schematic.
    if (!maps || source !== "osm") return;
    const container = el("result-map"); container.hidden = false;
    if (!resultMap) resultMap = new maps.Map(container, { center: latLng(candidate.geometry.coordinates[0]), level: 5 });
    resultMap.relayout();
    const bounds = new maps.LatLngBounds();
    for (const [points, color, dashed] of [
      [candidate.reference || [], "#c47b29", true],
      [candidate.approach || [], "#5377b3", true],
      [candidate.geometry.coordinates, "#167c6a", false],
    ]) {
      if (!points.length) continue;
      const path = points.map(latLng); path.forEach(point => bounds.extend(point));
      overlays.push(new maps.Polyline({ map: resultMap, path, strokeWeight: dashed ? 3 : 5,
        strokeColor: color, strokeOpacity: 0.9, strokeStyle: dashed ? "dash" : "solid" }));
    }
    const route = candidate.geometry.coordinates;
    overlays.push(new maps.Marker({ map: resultMap, position: latLng(route[0]), title: candidate.closed ? "출발 / 도착" : "출발" }));
    if (!candidate.closed) overlays.push(new maps.Marker({ map: resultMap, position: latLng(route[route.length - 1]), title: "도착" }));
    resultMap.setBounds(bounds);
  }
  window.routeMap = { clear, render };
  el("lng").addEventListener("change", syncLocation);
  el("lat").addEventListener("change", syncLocation);
  el("location-keyword").addEventListener("input", () => {
    searchVersion++; el("location-results").replaceChildren();
  });
  el("location-search").addEventListener("submit", event => {
    event.preventDefault();
    const keyword = el("location-keyword").value.trim();
    if (!places || !keyword) return;
    const version = ++searchVersion;
    el("location-results").replaceChildren(); status("장소·주소 검색 중입니다.");
    function display(items) {
      if (version !== searchVersion) return;
      el("location-results").replaceChildren();
      status(items.length ? "검색 결과를 선택해주세요." : "검색 결과가 없습니다. 다른 장소명이나 주소를 입력해주세요.");
      for (const item of items) {
        const point = [Number(item.x), Number(item.y)];
        if (!valid(point)) continue;
        const button = document.createElement("button"); button.type = "button";
        const name = item.place_name || item.address_name;
        button.textContent = item.place_name ? `${name} · ${item.road_address_name || item.address_name}` : name;
        button.addEventListener("click", () => select(point, name));
        el("location-results").append(button);
      }
    }
    places.keywordSearch(keyword, (items, state) => {
      if (version !== searchVersion) return;
      if (state === maps.services.Status.OK) return display(items);
      if (state !== maps.services.Status.ZERO_RESULT) return status("검색 연결에 실패했습니다. 카카오 설정과 네트워크를 확인해주세요.");
      geocoder.addressSearch(keyword, (addresses, addressState) => {
        if (version !== searchVersion) return;
        if (addressState === maps.services.Status.ERROR) return status("주소 검색에 실패했습니다. 잠시 후 다시 시도해주세요.");
        display(addressState === maps.services.Status.OK ? addresses : []);
      });
    }, { size: 10 });
  });
  async function initialize() {
    try {
      const response = await fetch("/local/map-config");
      if (!response.ok) throw new Error();
      const { kakao_javascript_key: key } = await response.json();
      if (!key) { status(".env의 KAKAO_JAVASCRIPT_KEY를 설정한 뒤 새로고침해주세요."); return; }
      if (!/^[a-fA-F0-9]{32}$/.test(key)) throw new Error();
      await new Promise((resolve, reject) => {
        const script = document.createElement("script");
        const timer = setTimeout(() => reject(new Error()), 15000);
        script.src = `https://dapi.kakao.com/v2/maps/sdk.js?appkey=${encodeURIComponent(key)}&autoload=false&libraries=services`;
        script.onerror = () => { clearTimeout(timer); reject(new Error()); };
        script.onload = () => {
          if (!window.kakao?.maps?.load) { clearTimeout(timer); reject(new Error()); return; }
          window.kakao.maps.load(() => { clearTimeout(timer); resolve(); });
        };
        document.head.append(script);
      });
      maps = window.kakao.maps;
      el("location-map").hidden = false;
      locationMap = new maps.Map(el("location-map"), { center: latLng([Number(el("lng").value), Number(el("lat").value)]), level: 5 });
      marker = new maps.Marker({ map: locationMap, position: locationMap.getCenter() });
      attachFacilityLayer(maps, locationMap);
      places = new maps.services.Places(); geocoder = new maps.services.Geocoder();
      maps.event.addListener(locationMap, "click", event => select([event.latLng.getLng(), event.latLng.getLat()], "지도에서 선택한 위치"));
      el("location-search-button").disabled = false;
      status("카카오맵 연결 완료. 장소·주소를 검색하거나 지도를 클릭하세요.");
      if (pending) render(pending.candidate, pending.source);
      new ResizeObserver(() => {
        const center = locationMap.getCenter(); locationMap.relayout(); locationMap.setCenter(center);
        if (pending && !el("result-map").hidden) render(pending.candidate, pending.source);
      }).observe(el("location-map"));
    } catch {
      status(`지도 연결 실패: JavaScript 키, 카카오맵 사용 설정, SDK 도메인 ${location.origin} 등록을 확인하고 새로고침해주세요. 좌표 직접 입력과 경로 도식은 계속 사용할 수 있습니다.`);
    }
  }
  initialize();
})();

// Independent of route overlays: changing a drawing never removes facilities.
function attachFacilityLayer(maps, map) {
  const el = id => document.getElementById(id);
  const checks = { cctv: el("map-cctv"), toilet: el("map-toilet") };
  const names = { cctv: "CCTV", toilet: "공중화장실" };
  let overlays = [], version = 0, controller, debounce;
  const info = new maps.InfoWindow({ removable: true, disableAutoPan: true, zIndex: 10 });
  function clear() {
    overlays.forEach(overlay => overlay.setMap(null)); overlays = []; info.close();
  }
  function invalidate() {
    version++; controller?.abort(); clearTimeout(debounce); clear();
  }
  function schedule() {
    invalidate(); debounce = setTimeout(refresh, 450);
  }
  async function refresh() {
    invalidate();
    const current = version, kinds = Object.keys(checks).filter(kind => checks[kind].checked);
    if (!kinds.length) { el("map-facility-status").textContent = "시설을 선택하면 기준 위치 주변에 표시합니다."; return; }
    const center = [Number(el("lng").value), Number(el("lat").value)];
    const radius = Number(el("map-facility-radius").value);
    if (!center.every(Number.isFinite) || center[0] < 124 || center[0] > 132 || center[1] < 32 || center[1] > 39
        || ![500, 1000, 2000, 3000].includes(radius)) {
      el("map-facility-status").textContent = "국내 기준 위치와 표시 반경을 확인해주세요."; return;
    }
    const rad = Math.PI / 180, earth = 6371008.8;
    const latitudeDelta = radius / earth / rad;
    const longitudeDelta = Math.asin(Math.sin(radius / earth) / Math.cos(center[1] * rad)) / rad;
    const west = Math.max(124, center[0] - longitudeDelta), east = Math.min(132, center[0] + longitudeDelta);
    const south = Math.max(32, center[1] - latitudeDelta), north = Math.min(39, center[1] + latitudeDelta);
    const withinRadius = point => {
      const a = Math.sin((point[1] - center[1]) * rad / 2) ** 2
        + Math.cos(center[1] * rad) * Math.cos(point[1] * rad) * Math.sin((point[0] - center[0]) * rad / 2) ** 2;
      return 2 * earth * Math.asin(Math.sqrt(Math.min(1, a))) <= radius;
    };
    const token = el("token").value.trim();
    if (!token) { el("map-facility-status").textContent = "API 연결 후 시설 새로고침을 눌러주세요."; return; }
    controller = new AbortController();
    const activeController = controller;
    const signal = activeController.signal, timeout = setTimeout(() => activeController.abort(), 10000);
    el("map-facility-status").textContent = `기준 위치 반경 ${radius}m의 시설을 조회 중입니다.`;
    const results = await Promise.all(kinds.map(async kind => {
      try {
        const query = new URLSearchParams({ west, south, east, north, types: kind, limit: 500 });
        const response = await fetch(`/v1/places?${query}`, { headers: { Authorization: `Bearer ${token}` }, signal });
        if (!response.ok) return { kind, error: `HTTP ${response.status}` };
        const data = await response.json();
        if (!Array.isArray(data.items)) throw new Error();
        return { kind, data };
      } catch { return { kind, error: "연결 실패 또는 시간 초과" }; }
    }));
    clearTimeout(timeout);
    if (current !== version) return;
    const summaries = [];
    for (const { kind, data, error } of results) {
      if (error) { summaries.push(`${names[kind]} ${error} · 시설 새로고침으로 재시도`); continue; }
      let displayed = 0;
      for (const item of data.items) {
        if (!Array.isArray(item.location) || item.location.length !== 2 || !item.location.every(Number.isFinite)) continue;
        if (!withinRadius(item.location)) continue;
        displayed++;
        const position = new maps.LatLng(item.location[1], item.location[0]);
        const button = document.createElement("button"); button.type = "button";
        button.className = `facility-map-pin facility-map-pin-${kind}`;
        button.textContent = kind === "cctv" ? "CCTV" : "WC";
        button.title = `${names[kind]} · ${item.name}`;
        for (const eventName of ["mousedown", "mouseup", "touchstart", "touchend"]) {
          button.addEventListener(eventName, event => { event.stopPropagation(); maps.event.preventMap(); });
        }
        button.addEventListener("click", event => {
          event.stopPropagation(); maps.event.preventMap();
          const content = document.createElement("div"); content.className = "facility-map-info";
          const title = document.createElement("strong"); title.textContent = `${names[kind]} · ${item.name}`;
          const address = document.createElement("p"); address.textContent = item.address?.road || item.address?.lot || "주소 정보 없음";
          content.append(title, address);
          info.setContent(content); info.setPosition(position); info.open(map);
        });
        overlays.push(new maps.CustomOverlay({ map, position, content: button, yAnchor: 1, zIndex: kind === "toilet" ? 3 : 2 }));
      }
      summaries.push(`${names[kind]} ${displayed}건${data.truncated ? " (일부만 표시 · 반경을 줄여주세요)" : ""}`);
    }
    el("map-facility-status").textContent = `기준 위치 반경 ${radius}m · ` + summaries.join(" · ") + " · 좌표가 확인된 시설만 표시";
  }
  Object.values(checks).forEach(check => { check.disabled = false; check.addEventListener("change", schedule); });
  el("map-facility-refresh").disabled = false;
  el("map-facility-refresh").addEventListener("click", schedule);
  el("map-facility-radius").disabled = false;
  for (const id of ["lng", "lat", "map-facility-radius"]) el(id).addEventListener("change", schedule);
  // Zooming and panning only change the view; selected reference coordinates stay fixed.
}
