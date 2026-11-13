"use strict";
(() => {
  const el = id => document.getElementById(id);
  const names = {cctv:'CCTV', toilet:'화장실'};
  const stages = {received:'요청 접수', reading_facilities:'시설 DB 조회 중', completed:'응답 완료', interrupted:'처리 중단'};
  let entries = [];
  function render() {
    const selected = entries.filter(e => ['/v1/places','/v1/places/status'].includes(e.path))
      .filter(e => el('facility-log-channel').value === 'all' || e.channel === el('facility-log-channel').value)
      .filter(e => el('facility-log-kind').value === 'all' ||
        (el('facility-log-kind').value === 'status' ? e.path === '/v1/places/status' :
          e.path === '/v1/places' && e.facility_types?.includes(el('facility-log-kind').value)));
    const body = el('facility-log-body'); body.replaceChildren();
    el('facility-log-summary').textContent = `표시 ${selected.length}건 · HTTP 오류 ${selected.filter(e => e.status >= 400).length}건 · 처리 중 ${selected.filter(e => e.status == null && e.stage !== 'interrupted').length}건`;
    if (!selected.length) {
      const cell = body.insertRow().insertCell(); cell.colSpan = 7;
      cell.textContent = '표시할 시설 요청이 없습니다. 외부 앱에서 호출하거나 아래 직접 호출을 사용하세요.';
    }
    for (const e of selected) {
      const row = body.insertRow(); row.dataset.error = String(e.status >= 400);
      const kind = e.path.endsWith('/status') ? '데이터 상태' : (e.facility_types || []).map(t => names[t]).join(' + ') || '종류 미확인';
      const result = e.error_code || (e.facility_count !== undefined ? `${e.facility_count}건${e.truncated ? ' · 추가 결과 있음' : ''}` :
        e.facility_summary?.map(s => `${names[s.type]} 저장 ${s.stored} / 지도 ${s.map_ready}`).join(' · ') || '—');
      for (const value of [new Date(e.time * 1000).toLocaleTimeString('ko-KR'), e.channel === 'external' ? '외부 API' : '로컬',
        `${e.method} ${e.path} · ${kind}`, stages[e.stage] || e.stage, e.status ?? '—',
        e.elapsed_s == null ? (e.stage === 'interrupted' ? '중단' : '진행 중') : `${e.elapsed_s.toFixed(3)}초`, result]) {
        row.insertCell().textContent = value;
      }
    }
  }
  document.addEventListener('facility-log-update', event => { entries = event.detail; render(); });
  document.addEventListener('facility-log-connection', event => { el('facility-log-connection').textContent = event.detail; });
  for (const id of ['facility-log-channel','facility-log-kind']) el(id).addEventListener('change', render);
  let busy = false;
  async function call(path) {
    if (busy) return;
    const token = el('token').value.trim();
    if (!token) { el('facility-call-status').textContent = '서버 연결 후 페이지의 Bearer 토큰을 확인해주세요.'; return; }
    busy = true;
    el('facility-query-send').disabled = el('facility-status-send').disabled = true;
    el('facility-call-request').textContent = `GET ${path}`;
    el('facility-call-response').textContent = '응답 대기 중';
    el('facility-call-status').textContent = '요청 중';
    const start = performance.now();
    try {
      const response = await fetch(path, {headers:{Authorization:`Bearer ${token}`}, cache:'no-store', signal:AbortSignal.timeout(10000)});
      const data = await response.json();
      el('facility-call-response').textContent = JSON.stringify(data, null, 2);
      const retry = response.headers.get('Retry-After');
      el('facility-call-status').textContent = `HTTP ${response.status} · ${((performance.now()-start)/1000).toFixed(3)}초${data.error ? ` · ${data.error.code}` : ''}${retry ? ` · ${retry}초 후 재시도` : ''}`;
    } catch (_) {
      el('facility-call-status').textContent = '요청 실패 · 서버 연결 또는 응답 시간 초과를 확인해주세요.';
      el('facility-call-response').textContent = '응답을 받지 못했습니다.';
    } finally {
      busy = false; el('facility-query-send').disabled = el('facility-status-send').disabled = false;
    }
  }
  el('facility-query-form').addEventListener('submit', event => {
    event.preventDefault();
    const params = new URLSearchParams();
    for (const key of ['west','south','east','north']) params.set(key, el(`facility-${key}`).value);
    params.set('types', el('facility-query-type').value); params.set('limit','200');
    call('/v1/places?' + params);
  });
  el('facility-status-send').addEventListener('click', () => call('/v1/places/status'));
  render();
})();
