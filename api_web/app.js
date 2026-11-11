"use strict";
const $ = id => document.getElementById(id);
const canvas = $("drawing"), context = canvas.getContext("2d");
let points = [], drawing = false, pointer = null, ready = false, busy = false;
let responseData = null, submittedRequest = null, responseStatus = null, inputRevision = 0;

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").dataset.error = String(error);
}

function refreshButtons() {
  $("send").disabled = !ready || busy;
  $("auth-test").disabled = !ready || busy;
}

function paintDrawing() {
  context.clearRect(0, 0, canvas.width, canvas.height);
  context.fillStyle = "#c6ddd1";
  for (let x = 20; x < 500; x += 25) for (let y = 20; y < 400; y += 25) {
    context.beginPath(); context.arc(x, y, 1, 0, Math.PI * 2); context.fill();
  }
  if (points.length) {
    context.beginPath(); context.moveTo(...points[0]);
    points.slice(1).forEach(point => context.lineTo(...point));
    context.strokeStyle = "#167c6a"; context.lineWidth = 3; context.lineJoin = "round"; context.stroke();
    context.beginPath(); context.arc(...points[0], 4, 0, Math.PI * 2); context.fillStyle = "#167c6a"; context.fill();
  }
  $("point-count").textContent = `${points.length} points`;
}

function coordinate(event) {
  const rect = canvas.getBoundingClientRect();
  return [Math.round(Math.max(0, Math.min(500, (event.clientX - rect.left) * 500 / rect.width)) * 10) / 10,
          Math.round(Math.max(0, Math.min(400, (event.clientY - rect.top) * 400 / rect.height)) * 10) / 10];
}

canvas.addEventListener("pointerdown", event => {
  if (event.button !== 0 || drawing) return;
  drawing = true; pointer = event.pointerId; canvas.setPointerCapture(pointer);
  points = [coordinate(event)]; paintDrawing();
});
canvas.addEventListener("pointermove", event => {
  if (!drawing || event.pointerId !== pointer || points.length >= 2999) return;
  const next = coordinate(event), last = points[points.length - 1];
  if (Math.hypot(next[0] - last[0], next[1] - last[1]) >= 2) points.push(next);
  paintDrawing();
});
function finishDrawing(event) {
  if (!drawing || event.pointerId !== pointer) return;
  if (event.type === "pointerup" && points.length < 3000) points.push(coordinate(event));
  drawing = false; pointer = null; paintDrawing(); buildRequest();
}
canvas.addEventListener("pointerup", finishDrawing);
canvas.addEventListener("pointercancel", finishDrawing);

function preset(shape) {
  if (shape === "corner") points = [[100, 80], [100, 320], [400, 320]];
  else if (shape === "square") points = [[110, 70], [390, 70], [390, 330], [110, 330], [110, 70]];
  else points = Array.from({ length: 11 }, (_, i) => {
    const angle = -Math.PI / 2 + i * Math.PI / 5, radius = i % 2 ? 75 : 165;
    return [Math.round(250 + Math.cos(angle) * radius), Math.round(200 + Math.sin(angle) * radius)];
  });
  paintDrawing(); buildRequest();
}
document.querySelectorAll("[data-shape]").forEach(button => button.addEventListener("click", () => preset(button.dataset.shape)));
$("clear").addEventListener("click", () => { points = []; paintDrawing(); buildRequest(); });

function markEdited() {
  inputRevision++;
  responseData = null; submittedRequest = null;
  $("download").disabled = true; $("result-visual").hidden = true;
  $("result-summary").textContent = "입력이 바뀌었습니다. 다시 생성해 확인해주세요.";
  $("response").textContent = "응답 없음";
}

function buildRequest() {
  const payload = {
    drawing: points, location: [Number($("lng").value), Number($("lat").value)],
    distance_m: Number($("distance").value), approach_m: Number($("approach").value),
    radius_m: Number($("radius").value), candidate_limit: Number($("limit").value),
    tolerance: Number($("tolerance").value), search_seconds: Number($("seconds").value),
    route_type: $("route-type").value, search_mode: $("mode").value,
    allow_rotation: $("rotation").checked, allow_vertex_adjustment: $("adjustment").checked,
    data_source: $("source").value, provider: $("provider").value,
  };
  $("request").value = JSON.stringify(payload, null, 2);
  markEdited();
}
$("controls").addEventListener("submit", event => { event.preventDefault(); buildRequest(); setStatus("요청 JSON을 갱신했습니다. ‘API로 경로 생성’을 눌러주세요."); });
$("controls").addEventListener("change", buildRequest);
$("request").addEventListener("input", markEdited);

function renderCandidate() {
  const candidate = responseData?.candidates?.[Number($("candidates").value)];
  const svg = $("route-view"); svg.replaceChildren();
  if (!candidate) return;
  const series = [
    [candidate.reference || [], "#c47b29", true],
    [candidate.approach || [], "#5377b3", true],
    [candidate.geometry.coordinates, "#167c6a", false],
  ];
  const coordinates = series.flatMap(series => series[0]);
  const lng0 = coordinates[0][0], lat0 = coordinates[0][1], cos = Math.cos(lat0 * Math.PI / 180);
  const project = p => [(p[0] - lng0) * cos, -(p[1] - lat0)];
  const projected = coordinates.map(project);
  const minX = Math.min(...projected.map(p => p[0])), maxX = Math.max(...projected.map(p => p[0]));
  const minY = Math.min(...projected.map(p => p[1])), maxY = Math.max(...projected.map(p => p[1]));
  const scale = Math.min(540 / Math.max(maxX - minX, 1e-8), 280 / Math.max(maxY - minY, 1e-8));
  const toView = p => { const xy = project(p); return [300 + (xy[0] - (minX + maxX) / 2) * scale, 170 + (xy[1] - (minY + maxY) / 2) * scale]; };
  for (const [line, color, dashed] of series) {
    if (!line.length) continue;
    const element = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    element.setAttribute("points", line.map(p => toView(p).join(",")).join(" "));
    element.setAttribute("fill", "none"); element.setAttribute("stroke", color);
    element.setAttribute("stroke-width", dashed ? "2" : "3");
    element.setAttribute("stroke-linejoin", "round");
    if (dashed) element.setAttribute("stroke-dasharray", "7 5");
    svg.append(element);
  }
  const route = candidate.geometry.coordinates;
  for (const [point, name] of [[route[0], "출발"], [route[route.length - 1], candidate.closed ? "출발 / 도착" : "도착"]]) {
    const xy = toView(point), label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", xy[0] + 5); label.setAttribute("y", xy[1] - 6);
    label.setAttribute("font-size", "11"); label.setAttribute("fill", "#16352f"); label.textContent = name;
    if (candidate.closed && name === "출발") continue;
    svg.append(label);
  }
  $("visual-note").textContent = responseData.source === "synthetic"
    ? "가상 격자 도로입니다. 실제 지도나 실제 달리기 경로로 사용하면 안 됩니다."
    : "실제 OSM 도로 좌표의 도식입니다. 배경 지도는 없으며, 두 선을 같은 축에서 비교합니다.";
  $("candidate-info").textContent = `${candidate.acceptable ? "모양 조건 충족" : "비교용 · 추천 아님"} · 러닝 ${candidate.distance_m.toFixed(0)}m · 접근 ${candidate.approach_m.toFixed(0)}m · 평균 오차 ${candidate.mean_error_m.toFixed(1)}m`;
}
$("candidates").addEventListener("change", renderCandidate);

async function sendRequest(authenticated) {
  let payload;
  try { payload = JSON.parse($("request").value); }
  catch { setStatus("요청 JSON 문법을 확인해주세요.", true); return; }
  busy = true; refreshButtons();
  const revision = inputRevision, start = performance.now();
  $("result-visual").hidden = true; $("download").disabled = true;
  responseData = null; submittedRequest = null;
  $("response").textContent = "요청 처리 중"; $("result-summary").textContent = "요청 처리 중입니다.";
  const elapsed = setInterval(() => setStatus(`PC에서 처리 중 · ${Math.round((performance.now() - start) / 1000)}초 경과 · 도로 조회가 필요한 경우 더 오래 걸릴 수 있습니다.`), 1000);
  setStatus(authenticated ? "API에 요청을 보냈습니다." : "인증 없이 호출하여 차단 여부를 확인합니다.");
  try {
    const headers = { "Content-Type": "application/json" };
    if (authenticated) headers.Authorization = `Bearer ${$("token").value.trim()}`;
    const response = await fetch("/v1/routes", { method: "POST", headers, body: JSON.stringify(payload) });
    const data = await response.json();
    clearInterval(elapsed);
    if (revision !== inputRevision) {
      setStatus(`HTTP ${response.status} · 처리 중 입력이 바뀌어 이전 결과를 표시하지 않습니다. 다시 생성해주세요.`);
      return;
    }
    submittedRequest = payload; responseData = data; responseStatus = response.status;
    $("response").textContent = JSON.stringify(data, null, 2); $("download").disabled = false;
    if (!response.ok) {
      const message = data.error?.message || "요청이 실패했습니다.";
      setStatus(`HTTP ${response.status} · ${message}`, authenticated || response.status !== 401);
      $("result-summary").textContent = !authenticated && response.status === 401 ? "인증 차단 정상: 토큰 없는 요청은 거절됩니다." : message;
      $("response-details").open = true; return;
    }
    setStatus(`HTTP 200 · 총 ${data.total_elapsed_s}초 · ${data.candidates.length}개 후보 반환`);
    $("result-summary").textContent = `${data.message} · 도로 ${data.data.nodes.toLocaleString()}개 지점 · 그래프 재사용 ${data.data.graph_cache_hit ? "예" : "아니오"}${data.data.load_warning ? " · " + data.data.load_warning : ""}`;
    $("candidates").replaceChildren();
    data.candidates.forEach((candidate, i) => {
      const option = document.createElement("option"); option.value = i;
      option.textContent = `후보 ${i + 1} · ${candidate.acceptable ? "조건 충족" : "비교용"} · ${Math.round(candidate.distance_m)}m`;
      $("candidates").append(option);
    });
    $("result-visual").hidden = !data.candidates.length;
    if (data.candidates.length) renderCandidate();
    else $("response-details").open = true;
  } catch {
    clearInterval(elapsed);
    setStatus("API 연결이 끊겼습니다. 서버 실행 여부를 확인해주세요. 서버 계산은 계속될 수 있으니 즉시 반복 요청하지 마세요.", true);
    $("result-summary").textContent = "응답을 받지 못했습니다.";
  } finally { clearInterval(elapsed); busy = false; refreshButtons(); }
}
$("send").addEventListener("click", () => sendRequest(true));
$("auth-test").addEventListener("click", () => sendRequest(false));
$("download").addEventListener("click", () => {
  if (!submittedRequest || !responseData) return;
  const data = { request: submittedRequest, http_status: responseStatus, response: responseData };
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = "drawrun-api-test.json"; anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

async function initialize() {
  preset("corner");
  try {
    const health = await fetch("/v1/health"), setup = await fetch("/local/bootstrap");
    if (!health.ok || !setup.ok) throw new Error("Server unavailable");
    const healthData = await health.json();
    const configuration = await setup.json();
    $("token").value = configuration.token; ready = true; refreshButtons();
    $("health").textContent = "로컬 API 연결됨";
    $("tunnel-status").textContent = healthData.tunnel_enabled
      ? `API 전용 ${healthData.tunnel_port} 포트 준비됨. ngrok 연결 여부는 별도로 확인하세요. 토큰은 서버 재시작 후에도 유지됩니다. 이 화면의 ${location.port} 포트는 외부에 연결하지 마세요.`
      : "로컬 전용 모드입니다. 외부 연동은 start-api.ps1 -EnableTunnel로 재시작해야 합니다. 현재 토큰은 재시작 시 바뀝니다.";
    setStatus("준비 완료. 가상 도로로 빠르게 테스트하거나 실제 OSM 도로를 선택하세요.");
  } catch {
    $("health").textContent = "서버 연결 실패";
    setStatus("API 서버 연결을 확인하고 페이지를 새로고침해주세요.", true);
  }
}
initialize();
