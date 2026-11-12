"""Local playground plus an opt-in, separate API-only ngrok upstream.

Both listeners bind to loopback. This program NEVER starts a public tunnel.
The tunnel listener has no static files or token bootstrap, regardless of Host.
"""
import argparse
from collections import OrderedDict, deque
from contextlib import ExitStack
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from routing import (InputError, demo_graph, finite_number, generate,
                     graph_from_osm, prepare_drawing, valid_center)
from local_osm import read_roads as get_osm
from facility_storage import DATABASE, FacilityStore
from places_api import PlacesError, validate_query, query_places

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "api_web"
MAX_BODY = 256 * 1024
GRAPH_TTL = 600
CORS_ORIGINS = {"http://localhost:5000", "http://127.0.0.1:5000"}
CORS_HEADERS = {"authorization", "content-type", "ngrok-skip-browser-warning"}
FIELDS = {"drawing", "location", "distance_m", "approach_m", "tolerance",
          "candidate_limit", "allow_rotation", "allow_vertex_adjustment",
          "route_type", "search_mode", "focus_location", "radius_m",
          "data_source", "provider", "search_seconds"}


def load_tunnel_token(path=None):
    """Persist only the test API token, never ngrok credentials or SDK keys."""
    token = os.environ.get("DRAWRUN_API_TOKEN")
    if token is None:
        path = path or ROOT / ".api-token"
        try:
            token = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            token = secrets.token_urlsafe(32)
            try:
                # Exclusive create prevents overwriting an existing token.
                with path.open("x", encoding="utf-8") as file:
                    file.write(token + "\n")
            except FileExistsError:
                token = path.read_text(encoding="utf-8").strip()
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise InputError("API 토큰은 영문·숫자·밑줄·하이픈 32~128자여야 합니다. 환경 변수 또는 .api-token을 확인해주세요.")
    return token


def number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{label}는 JSON 숫자여야 합니다.")
    return finite_number(value, low, high, label)


def validate_request(body):
    """Check all input before fetching roads or occupying the compute slot."""
    if not isinstance(body, dict):
        raise InputError("요청 본문은 JSON 객체여야 합니다.")
    if set(body) - FIELDS:
        raise InputError("지원하지 않는 요청 필드가 있습니다.")
    location = body.get("location")
    if not isinstance(location, list) or len(location) != 2:
        raise InputError("location은 [경도, 위도] 배열이어야 합니다.")
    for value in location:
        number(value, -180, 180, "위치 좌표")
    location = valid_center(location)
    target = number(body.get("distance_m", 3000), 300, 8000, "러닝 거리")
    drawing = body.get("drawing")
    if not isinstance(drawing, list) or not 2 <= len(drawing) <= 3000:
        raise InputError("drawing은 2~3,000개의 [x, y] 좌표 배열이어야 합니다.")
    for point in drawing:
        if not isinstance(point, list) or len(point) != 2:
            raise InputError("그림 좌표는 [x, y] 배열이어야 합니다.")
        for value in point:
            number(value, -10000, 10000, "그림 좌표")
    route_type = body.get("route_type", "auto")
    if not isinstance(route_type, str) or route_type not in {"auto", "open", "loop"}:
        raise InputError("route_type은 auto, open 또는 loop여야 합니다.")
    prepare_drawing(drawing, target, route_type)
    count = number(body.get("candidate_limit", 20), 1, 30, "후보 개수")
    if count != int(count):
        raise InputError("후보 개수는 정수여야 합니다.")
    mode = body.get("search_mode", "overview")
    if not isinstance(mode, str) or mode not in {"overview", "all", "local"}:
        raise InputError("search_mode는 overview, all 또는 local이어야 합니다.")
    for field in ("allow_rotation", "allow_vertex_adjustment"):
        if field in body and not isinstance(body[field], bool):
            raise InputError(f"{field}는 true 또는 false여야 합니다.")
    source = body.get("data_source", "osm")
    if not isinstance(source, str) or source not in {"osm", "demo"}:
        raise InputError("data_source는 osm 또는 demo여야 합니다.")
    provider = body.get("provider", "local")
    if not isinstance(provider, str) or provider not in {"local", "main", "coffee"}:
        raise InputError("provider는 local이어야 합니다. 기존 main, coffee 값도 로컬 데이터를 사용합니다.")
    radius = number(body.get("radius_m", 2500), 800, 4000, "도로 데이터 반경")
    seconds = number(body.get("search_seconds", 45), 3, 45, "탐색 시간")
    payload = {
        "drawing": drawing, "location": location, "distance_m": target,
        "approach_m": number(body.get("approach_m", 1000), 0, 1500, "출발점 이동 거리"),
        "tolerance": number(body.get("tolerance", .2), .05, .5, "거리 허용 오차"),
        "candidate_limit": int(count), "route_type": route_type, "search_mode": mode,
        "allow_rotation": body.get("allow_rotation", True),
        "allow_vertex_adjustment": body.get("allow_vertex_adjustment", True),
    }
    if mode == "local":
        focus = body.get("focus_location")
        if not isinstance(focus, list) or len(focus) != 2:
            raise InputError("local 탐색에는 focus_location: [경도, 위도]가 필요합니다.")
        for value in focus:
            number(value, -180, 180, "상세 위치 좌표")
        payload["focus_location"] = valid_center(focus)
    return payload, source, radius, provider, seconds


class RouteService:
    def __init__(self, token=None, facility_database=DATABASE):
        self.token = token or secrets.token_urlsafe(32)
        self.compute = threading.Semaphore(1)
        self.lock = threading.Lock()
        self.calls = deque()
        self.place_calls = deque()
        self.facility_store = FacilityStore(facility_database)
        self.graphs = OrderedDict()
        self.request_logs = deque(maxlen=200)
        self.log_lock = threading.Lock()

    def log_snapshot(self):
        with self.log_lock:
            return [dict(entry) for entry in reversed(self.request_logs)]

    def take_rate_slot(self):
        # One token / one PC for this initial test service; not multi-user auth.
        now = time.monotonic()
        with self.lock:
            while self.calls and self.calls[0] <= now - 60:
                self.calls.popleft()
            if len(self.calls) >= 12:
                return False
            self.calls.append(now)
            return True

    def take_places_slot(self):
        # Shared across listeners, separate from expensive route generation.
        now = time.monotonic()
        with self.lock:
            while self.place_calls and self.place_calls[0] <= now - 60:
                self.place_calls.popleft()
            if len(self.place_calls) >= 120:
                return False
            self.place_calls.append(now)
            return True

    def roads(self, location, source, radius, provider):
        # Called under compute semaphore. Location is exact, not rounded: keep
        # the graph's coordinate origin consistent with the requested location.
        key = (source, tuple(location), radius, provider)
        entry = self.graphs.get(key)
        if entry and time.monotonic() - entry[0] < GRAPH_TTL:
            self.graphs.move_to_end(key)
            return entry[1], {**entry[2], "graph_cache_hit": True}
        if source == "demo":
            graph = demo_graph(location)
            info = {"osm_cache_hit": False, "graph_cache_hit": False}
        else:
            data, cached = get_osm(location, radius, provider)
            graph = graph_from_osm(data, location, radius)
            info = {"osm_cache_hit": cached, "graph_cache_hit": False,
                    **data.get("_drawrun_fetch", {})}
        self.graphs[key] = (time.monotonic(), graph, info)
        self.graphs.move_to_end(key)
        while len(self.graphs) > 6:
            self.graphs.popitem(last=False)
        return graph, info


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service=None, api_only=False, tunnel_port=None):
        self.service = service or RouteService()
        self.api_only = api_only
        self.tunnel_port = tunnel_port
        super().__init__(address, ApiHandler)


class ApiHandler(BaseHTTPRequestHandler):
    # Socket timeout protects against abandoned / incomplete local requests.
    timeout = 10

    def parse_request(self):
        valid = super().parse_request()
        if valid:
            path = urllib.parse.urlsplit(self.path).path
            if self.server.api_only or path in {"/v1/routes", "/v1/places"}:
                self._log_started = time.monotonic()
                self._request_log = {
                    "id": secrets.token_hex(8), "time": time.time(),
                    "channel": "external" if self.server.api_only else "local",
                    "method": self.command if self.command in {"GET", "POST", "OPTIONS", "PUT", "DELETE", "HEAD", "PATCH"} else "OTHER",
                    "path": path if path in {"/v1/routes", "/v1/health", "/v1/places"} else "(other path)",
                    "stage": "received", "status": None, "elapsed_s": None,
                }
                with self.server.service.log_lock:
                    self.server.service.request_logs.append(self._request_log)
        return valid

    def update_request_log(self, **values):
        if getattr(self, "_request_log", None) is not None:
            with self.server.service.log_lock:
                self._request_log.update(values)

    def handle_one_request(self):
        self._request_log = None
        self._response_status = None
        try:
            super().handle_one_request()
        finally:
            if self._request_log is not None:
                self.update_request_log(
                    stage="completed" if self._response_status is not None else "interrupted",
                    status=self._response_status,
                    elapsed_s=round(time.monotonic() - self._log_started, 3))

    def send_response(self, code, message=None):
        self._response_status = code
        super().send_response(code, message)

    def log_message(self, format, *args):
        # Never log tokens, request bodies, coordinates or query strings.
        print(f"[{self.log_date_time_string()}] {self.command} "
              f"{urllib.parse.urlsplit(self.path).path}", flush=True)

    def send_bytes(self, raw, status=200, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if self.server.api_only:
            self.send_header("Vary", "Origin")
            origin = self.headers.get("Origin")
            if origin in CORS_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Expose-Headers", "Retry-After")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reply(self, data, status=200, headers=None):
        details = {}
        if "error" in data:
            details["error_code"] = data["error"].get("code")
        if "candidates" in data:
            details.update(candidate_count=len(data["candidates"]),
                           recommended_count=data.get("recommended_count", 0),
                           request_id=data.get("request_id"))
        self.update_request_log(**details)
        self.send_bytes(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), status, headers=headers)

    def error(self, code, message, status, headers=None):
        self.reply({"error": {"code": code, "message": message}}, status, headers)

    def local_request(self):
        # API-only isolation is a listener property, NEVER inferred from a
        # spoofable forwarded header. ngrok connects to the loopback upstream.
        if self.server.api_only:
            if self.client_address[0] != "127.0.0.1":
                self.error("LOOPBACK_UPSTREAM_ONLY", "API 전용 포트는 이 PC의 터널 에이전트로만 연결할 수 있습니다.", 403)
                return False
            origin = self.headers.get("Origin")
            if ((origin is not None and origin not in CORS_ORIGINS) or
                    (origin is None and self.headers.get("Sec-Fetch-Site") == "cross-site")):
                self.error("ORIGIN_DENIED", "허용된 로컬 웹 주소의 포트 5000에서 호출해주세요.", 403)
                return False
            return True
        hosts = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        forwarded = any(self.headers.get(name) is not None for name in
                        ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host"))
        if self.headers.get("Host") not in hosts or self.client_address[0] != "127.0.0.1" or forwarded:
            self.error("LOCAL_ONLY", "현재는 로컬 테스트만 허용합니다. ngrok 연결은 아직 설정하지 않았습니다.", 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}" for host in hosts}:
            self.error("ORIGIN_DENIED", "현재 테스트 화면과 같은 출처의 요청만 허용합니다.", 403)
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.error("ORIGIN_DENIED", "다른 사이트에서 보낸 요청은 허용하지 않습니다.", 403)
            return False
        return True

    def do_GET(self):
        if not self.local_request():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/v1/places":
            authorization = self.headers.get("Authorization", "")
            expected = f"Bearer {self.server.service.token}"
            if (len(self.headers.get_all('Authorization', [])) != 1 or
                    not hmac.compare_digest(authorization.encode('utf-8'), expected.encode('utf-8'))):
                return self.error('UNAUTHORIZED', '올바른 테스트용 Bearer 토큰이 필요합니다.', 401)
            try:
                validated = validate_query(urllib.parse.urlsplit(self.path).query)
                if not self.server.service.take_places_slot():
                    return self.error('RATE_LIMITED', '시설 조회는 두 포트를 합쳐 분당 최대 120회입니다.',
                                      429, {'Retry-After': '60'})
                return self.reply(query_places(self.server.service.facility_store, validated))
            except PlacesError as error:
                return self.error(error.code, error.message, error.status)
        if path == "/v1/health":
            health = {"ok": True, "version": "v1", "local_only": not self.server.api_only}
            if not self.server.api_only:
                health["tunnel_enabled"] = self.server.tunnel_port is not None
                health["tunnel_port"] = self.server.tunnel_port
            return self.reply(health)
        if self.server.api_only:
            return self.error("NOT_FOUND", "존재하지 않는 API입니다.", 404)
        if path == "/local/bootstrap":
            # Available ONLY on the private playground listener. Tunnel mode
            # uses a persisted test token; default local mode is ephemeral.
            return self.reply({"token": self.server.service.token})
        if path == "/local/request-logs":
            return self.reply({"entries": self.server.service.log_snapshot()})
        assets = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/request-logs.js": ("request-logs.js", "text/javascript; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path not in assets:
            return self.error("NOT_FOUND", "존재하지 않는 경로입니다.", 404)
        filename, content_type = assets[path]
        self.send_bytes((ASSETS / filename).read_bytes(), content_type=content_type)

    def do_OPTIONS(self):
        if not self.local_request():
            return
        if not self.server.api_only or self.headers.get("Origin") not in CORS_ORIGINS:
            return self.error("ORIGIN_DENIED", "허용된 웹 출처가 필요합니다.", 403)
        path = urllib.parse.urlsplit(self.path).path
        method = {"/v1/routes": "POST", "/v1/health": "GET", "/v1/places": "GET"}.get(path)
        if method is None:
            return self.error("NOT_FOUND", "존재하지 않는 API입니다.", 404)
        requested = {item.strip().lower() for item in
                     self.headers.get("Access-Control-Request-Headers", "").split(",") if item.strip()}
        if self.headers.get("Access-Control-Request-Method") != method or not requested <= CORS_HEADERS:
            return self.error("CORS_DENIED", "허용되지 않은 요청 메서드 또는 헤더입니다.", 403)
        self.send_bytes(b"", 204, headers={
            "Access-Control-Allow-Methods": method + ", OPTIONS",
            "Access-Control-Allow-Headers": ", ".join(sorted(CORS_HEADERS)),
            "Access-Control-Max-Age": "600",
        })

    def do_POST(self):
        if not self.local_request():
            return
        if urllib.parse.urlsplit(self.path).path != "/v1/routes":
            return self.error("NOT_FOUND", "존재하지 않는 API입니다.", 404)
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.service.token}"
        if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            return self.error("UNAUTHORIZED", "올바른 테스트용 Bearer 토큰이 필요합니다.", 401)
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            return self.error("UNSUPPORTED_MEDIA_TYPE", "Content-Type: application/json을 사용해주세요.", 415)
        if self.headers.get("Transfer-Encoding"):
            return self.error("INVALID_BODY", "Content-Length가 있는 JSON 요청을 사용해주세요.", 400)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                return self.error("BODY_TOO_LARGE", "JSON 요청 크기는 1~262,144바이트여야 합니다.", 413)
            raw = self.rfile.read(size)
            if len(raw) != size:
                return self.error("INVALID_BODY", "요청 본문이 완전히 전달되지 않았습니다.", 400)
            def reject_constant(value):
                raise ValueError("Non-finite JSON number")
            body = json.loads(raw, parse_constant=reject_constant)
            payload, source, radius, provider, seconds = validate_request(body)
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
            return self.error("INVALID_INPUT", str(error) if isinstance(error, InputError) else "JSON 또는 입력 형식을 확인해주세요.", 400)
        except TimeoutError:
            return self.error("REQUEST_TIMEOUT", "요청 본문 수신 시간이 초과되었습니다.", 408)
        if not self.server.service.take_rate_slot():
            return self.error("RATE_LIMITED", "테스트 API는 두 포트를 합쳐 분당 최대 12회까지 요청할 수 있습니다.", 429, {"Retry-After": "60"})
        if not self.server.service.compute.acquire(blocking=False):
            return self.error("BUSY", "다른 경로를 계산 중입니다. 완료 후 다시 요청해주세요.", 429, {"Retry-After": "5"})
        started = time.monotonic()
        try:
            self.update_request_log(stage="loading_roads")
            graph, cache_info = self.server.service.roads(payload["location"], source, radius, provider)
            self.update_request_log(stage="calculating")
            result = generate(graph, payload, seconds=seconds)
            self.reply({**result, "api_version": "v1", "request_id": secrets.token_hex(8),
                        "total_elapsed_s": round(time.monotonic() - started, 2),
                        "data": {"source": graph.source, "nodes": len(graph.nodes),
                                 "edges": len(graph.edges), "radius_m": graph.radius, **cache_info}})
        except InputError as error:
            self.error("ROUTE_INPUT_OR_DATA_ERROR", str(error), 422)
        except (ValueError, KeyError, TypeError, OverflowError):
            self.error("DATA_ERROR", "도로 데이터를 처리하지 못했습니다. 다른 위치 또는 데이터 서버를 선택해주세요.", 502)
        except Exception as error:
            print(f"API internal error: {type(error).__name__}", flush=True)
            self.error("INTERNAL_ERROR", "계산 중 오류가 발생했습니다. 서버 콘솔을 확인해주세요.", 500)
        finally:
            self.server.service.compute.release()


def main():
    parser = argparse.ArgumentParser(description="DrawRun local playground + optional ngrok API upstream")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--enable-tunnel", action="store_true", help="Enable API-only loopback listener (does NOT run ngrok)")
    parser.add_argument("--tunnel-port", type=int, default=8083)
    parser.add_argument("--facility-database", type=Path, default=DATABASE,
                        help="Read-only facility SQLite path; never created by the API")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.tunnel_port <= 65535:
        parser.error("포트는 1~65535여야 합니다.")
    if args.enable_tunnel and args.port == args.tunnel_port:
        parser.error("테스트 화면과 API 전용 포트는 달라야 합니다.")
    try:
        service = RouteService(load_tunnel_token() if args.enable_tunnel else None,
                               facility_database=args.facility_database)
    except (InputError, OSError, UnicodeError) as error:
        parser.error(str(error) if isinstance(error, InputError) else "API 토큰 파일을 읽거나 만들지 못했습니다.")
    with ExitStack() as stack:
        server = stack.enter_context(ApiServer(("127.0.0.1", args.port), service))
        tunnel = None
        thread = None
        if args.enable_tunnel:
            tunnel = stack.enter_context(ApiServer(("127.0.0.1", args.tunnel_port), service, api_only=True))
            server.tunnel_port = args.tunnel_port
            thread = threading.Thread(target=tunnel.serve_forever, daemon=True)
            thread.start()
        print(f"DrawRun API playground: http://localhost:{args.port}/", flush=True)
        if tunnel:
            print(f"API-only upstream: http://127.0.0.1:{args.tunnel_port} (ngrok is NOT running)", flush=True)
            print("Test token persists in .api-token unless DRAWRUN_API_TOKEN is set; never logged.", flush=True)
        else:
            print("Local-only. Test token is ephemeral; not logged or saved.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if tunnel:
                tunnel.shutdown()
                thread.join()


if __name__ == "__main__":
    main()
