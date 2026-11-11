"""Local-only web/API server, Python standard library only."""
import argparse
import hashlib
import json
import math
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_osm import read_roads as get_local_osm
from routing import InputError, demo_graph, finite_number, generate, graph_from_osm, valid_center

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CACHE = ROOT / ".cache"
GRAPHS = {}
LOCK = threading.Lock()
DOWNLOAD = threading.Semaphore(1)
COMPUTE = threading.Semaphore(1)
PROVIDERS = {"main": "https://overpass-api.de/api/interpreter",
             "coffee": "https://overpass.private.coffee/api/interpreter"}
COOLDOWN = {}
MAX_BODY = 20 * 1024 * 1024
CACHE_TTL = 86400
MAX_STALE_AGE = 7 * CACHE_TTL


def osm_connection_message(error):
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, TimeoutError):
        return "OSM 데이터 서버 응답 시간이 초과되었습니다. 잠시 후 다시 시도하거나 대체 서버를 선택하세요."
    if isinstance(reason, ssl.SSLError):
        return "OSM 서버의 보안 연결에 실패했습니다. PC 시간과 인증서·네트워크 설정을 확인해주세요."
    if isinstance(reason, socket.gaierror):
        return "OSM 서버 주소를 찾지 못했습니다. 인터넷·DNS 연결을 확인해주세요."
    if isinstance(reason, ConnectionRefusedError) or getattr(reason, "winerror", None) == 10061:
        return "OSM 연결이 거절되었습니다. 로컬 서버 실행 환경의 네트워크 제한이나 프록시 연결을 확인해주세요."
    return "OSM 데이터 서버와 통신하지 못했습니다. 잠시 후 다시 시도하거나 대체 서버·Overpass JSON 가져오기를 사용하세요."


def read_dotenv_javascript_key(path):
    """Read only the web SDK key; never expose other local environment values.

    Supports KEY=value, optional export, quotes and comments, without shell
    expansion or loading unrelated values into the process environment.
    """
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return ""
    result = ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if not separator or name.strip() != "KAKAO_JAVASCRIPT_KEY":
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            key, closed, trailing = value[1:].partition(value[0])
            if not closed or (trailing.strip() and not trailing.lstrip().startswith("#")):
                continue
            value = key.strip()
        else:
            value = value.split("#", 1)[0].strip()
        result = value
    return result


def get_javascript_key():
    # Read at request time: saving .env only requires a page reload after the
    # server has been updated. Existing JSON configuration stays compatible.
    key = os.environ.get("KAKAO_JAVASCRIPT_KEY", "").strip()
    if key:
        return key
    key = read_dotenv_javascript_key(ROOT / ".env")
    if key:
        return key
    try:
        config = json.loads((ROOT / "local-config.json").read_text(encoding="utf-8"))
        key = config.get("kakao_javascript_key", "")
        return key.strip() if isinstance(key, str) else ""
    except (ValueError, OSError, AttributeError):
        return ""


def get_osm(center, radius, provider="main"):
    # One explicit public endpoint; cache avoids repeated public-server requests.
    if provider not in PROVIDERS:
        raise InputError("지원하지 않는 OSM 데이터 서버입니다.")
    key = hashlib.sha256(json.dumps([round(center[0], 4), round(center[1], 4), radius]).encode()).hexdigest()[:24]
    path = CACHE / (key + ".json")
    cached_data = None
    cache_age = None
    try:
        cache_age = max(0, time.time() - path.stat().st_mtime)
        candidate = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(candidate, dict) and isinstance(candidate.get("elements"), list) and not candidate.get("remark"):
            cached_data = candidate
    except (OSError, ValueError, UnicodeError):
        pass
    if cached_data is not None and cache_age < CACHE_TTL:
        return cached_data, True
    lat_delta = math.degrees(radius / 6371008.8)
    lng_delta = lat_delta / math.cos(math.radians(center[1]))
    bbox = f"{center[1]-lat_delta},{center[0]-lng_delta},{center[1]+lat_delta},{center[0]+lng_delta}"
    query = f'[out:json][timeout:30];way["highway"]({bbox});(._;>;);out body;'
    body = urllib.parse.urlencode({"data": query}).encode()
    if time.monotonic() < COOLDOWN.get(provider, 0):
        if cached_data is not None and cache_age <= MAX_STALE_AGE:
            return {**cached_data, "_drawrun_fetch": {"cache_stale": True,
                "load_warning": "OSM 서버 요청 제한으로 이전에 저장한 도로 데이터를 사용합니다. 최신 도로 변경이 반영되지 않을 수 있습니다."}}, True
        raise InputError("OSM 서버의 요청 제한으로 30초 대기 중입니다. 잠시 후 다시 시도하거나 다른 서버를 선택하세요.")
    request = urllib.request.Request(PROVIDERS[provider], data=body,
        headers={"User-Agent": "DrawRunLocalPrototype/0.1", "Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise InputError("OSM 응답이 너무 큽니다. 조회 범위를 줄여주세요.")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise InputError("OSM 서버 응답 형식이 올바르지 않습니다.")
        if data.get("remark"):
            raise InputError("OSM 서버가 조회를 완료하지 못했습니다. 잠시 후 더 작은 범위로 다시 시도해주세요.")
        if not isinstance(data.get("elements"), list):
            raise InputError("OSM 서버 응답 형식이 올바르지 않습니다.")
    except urllib.error.HTTPError as error:
        if error.code in {406, 429}:
            COOLDOWN[provider] = time.monotonic() + 30
        failure = f"OSM 데이터 서버 오류 (HTTP {error.code}). 잠시 후 다시 시도하거나 대체 서버·Overpass JSON 가져오기를 사용하세요."
    except (json.JSONDecodeError, UnicodeError):
        failure = "OSM 서버가 올바른 JSON 도로 데이터를 반환하지 않았습니다. 잠시 후 다시 시도하거나 대체 서버를 선택하세요."
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        failure = osm_connection_message(error)
    else:
        CACHE.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return data, False
    # Reuse only the exact requested area, and never refresh its saved age.
    if cached_data is not None and cache_age <= MAX_STALE_AGE:
        return {**cached_data, "_drawrun_fetch": {"cache_stale": True,
            "load_warning": f"{failure} 이전에 저장한 도로 데이터를 사용합니다. 최신 도로 변경이 반영되지 않을 수 있습니다."}}, True
    raise InputError(failure) from None


def register_graph(graph):
    import uuid
    gid = uuid.uuid4().hex
    with LOCK:
        if len(GRAPHS) >= 6:
            GRAPHS.pop(next(iter(GRAPHS)))
        GRAPHS[gid] = graph
    return {"graph_id": gid, "source": graph.source, "center": graph.center,
            "radius_m": graph.radius, "nodes": len(graph.nodes), "edges": len(graph.edges),
            "roads": graph.preview(), **graph.metadata}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, format, *args):
        # Do not log request bodies, coordinates or key values.
        print(f"[{self.log_date_time_string()}] {self.command} {urllib.parse.urlsplit(self.path).path}", flush=True)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def allowed(self):
        allowed_hosts = {f"localhost:{self.server.server_port}", f"127.0.0.1:{self.server.server_port}"}
        if self.headers.get("Host") not in allowed_hosts:
            self.reply({"error": "로컬 호스트에서만 접근할 수 있습니다."}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in allowed_hosts}:
            self.reply({"error": "다른 사이트의 요청은 허용되지 않습니다."}, 403)
            return False
        return True

    def reply(self, data, status=200):
        encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if not self.allowed():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/config":
            return self.reply({"kakao_javascript_key": get_javascript_key()})
        if path == "/api/health":
            return self.reply({"ok": True, "engine": "ordered-stroke experimental local engine"})
        if path.startswith("/api/"):
            return self.reply({"error": "존재하지 않는 API입니다."}, 404)
        return super().do_GET()

    def do_POST(self):
        if not self.allowed():
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= MAX_BODY:
                return self.reply({"error": "요청 크기를 확인해주세요. 최대 20MB입니다."}, 413)
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise InputError("JSON 객체가 필요합니다.")
            path = urllib.parse.urlsplit(self.path).path
            if path in {"/api/roads", "/api/demo", "/api/import"}:
                center = valid_center(body.get("center"))
                radius = finite_number(body.get("radius_m", 2500), 800, 4000, "데이터 범위")
                cached = False
                fetch_info = {}
                if path == "/api/demo":
                    graph = demo_graph(center)
                elif path == "/api/import":
                    data = body.get("osm")
                    if not isinstance(data, dict):
                        raise InputError("Overpass JSON 객체가 필요합니다.")
                    graph = graph_from_osm(data, center, radius)
                else:
                    if not DOWNLOAD.acquire(blocking=False):
                        return self.reply({"error": "다른 도로 조회가 진행 중입니다. 완료 후 다시 시도해주세요."}, 429)
                    try:
                        data, cached = get_local_osm(center, radius)
                        fetch_info = data.get("_drawrun_fetch", {})
                        graph = graph_from_osm(data, center, radius)
                    finally:
                        DOWNLOAD.release()
                return self.reply({**register_graph(graph), "cached": cached, **fetch_info})
            if path == "/api/generate":
                with LOCK:
                    graph = GRAPHS.get(body.get("graph_id"))
                if graph is None:
                    raise InputError("도로 데이터를 먼저 불러와주세요. 서버 재시작 후에는 다시 불러와야 합니다.")
                if not COMPUTE.acquire(blocking=False):
                    return self.reply({"error": "경로 생성이 이미 진행 중입니다."}, 429)
                try:
                    result = generate(graph, body, max_distance_m=20000)
                finally:
                    COMPUTE.release()
                return self.reply(result)
            return self.reply({"error": "존재하지 않는 API입니다."}, 404)
        except (InputError, ValueError, KeyError, TypeError) as error:
            self.reply({"error": str(error) if isinstance(error, InputError) else "입력 데이터 형식을 확인해주세요."}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            print(f"Internal error: {type(error).__name__}", flush=True)
            self.reply({"error": "서버에서 처리하지 못했습니다. 서버 콘솔을 확인해주세요."}, 500)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DrawRun local test server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"DrawRun: http://localhost:{args.port} (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
