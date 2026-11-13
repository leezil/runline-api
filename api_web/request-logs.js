"use strict";
(() => {
  const body = document.getElementById('request-log-body');
  const connection = document.getElementById('log-connection');
  const filter = document.getElementById('log-filter');
  const pause = document.getElementById('log-pause');
  let paused = false, entries = [];
  const stages = {received:'요청 접수', reading_facilities:'시설 DB 조회 중', loading_roads:'도로 읽는 중', calculating:'경로 계산 중', completed:'응답 완료', interrupted:'처리 중단'};
  function render() {
    body.replaceChildren();
    const selected = entries.filter(e => filter.value === 'all' || e.channel === 'external');
    if (!selected.length) {
      const row = body.insertRow(), cell = row.insertCell();
      cell.colSpan = 7; cell.textContent = '아직 수신한 요청이 없습니다.'; cell.style.padding = '16px 0';
    }
    for (const e of selected) {
      const row = body.insertRow();
      const result = e.error_code || (e.candidate_count !== undefined ? `후보 ${e.candidate_count}개 · 추천 ${e.recommended_count}개` : '—');
      const values = [new Date(e.time * 1000).toLocaleTimeString('ko-KR'), e.channel === 'external' ? '외부 API' : '로컬', `${e.method} ${e.path}`, stages[e.stage] || e.stage, e.status ?? '—', e.elapsed_s === null ? '진행 중' : `${e.elapsed_s.toFixed(2)}초`, result];
      row.title = `로그 ID: ${e.id}${e.request_id ? ` / 응답 ID: ${e.request_id}` : ''}`;
      for (const value of values) {
        const cell = row.insertCell(); cell.textContent = value;
        cell.style.padding = '10px 12px 10px 0'; cell.style.borderBottom = '1px solid #c6ddd1';
      }
      if (e.status >= 400) row.style.color = '#b53a35';
    }
  }
  pause.addEventListener('click', () => {
    paused = !paused; pause.textContent = paused ? '갱신 재개' : '갱신 일시정지';
    if (paused) connection.textContent = '화면 갱신 일시정지 · 서버는 계속 기록합니다';
  });
  filter.addEventListener('change', render);
  async function poll() {
    if (!paused) {
      try {
        const response = await fetch('/local/request-logs', {cache:'no-store', signal:AbortSignal.timeout(5000)});
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next = (await response.json()).entries;
        if (!paused) {
          entries = next; render();
          document.dispatchEvent(new CustomEvent('facility-log-update', {detail: entries}));
        }
        if (!paused) connection.textContent = `연결됨 · ${new Date().toLocaleTimeString('ko-KR')} 갱신`;
      } catch (error) { connection.textContent = `연결 끊김 · 자동 재시도 (${error.message})`; }
    }
    document.dispatchEvent(new CustomEvent('facility-log-connection', {detail: connection.textContent}));
    setTimeout(poll, 1000);
  }
  poll();
})();
