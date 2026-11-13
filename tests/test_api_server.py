"""Local API contract + security tests, with no public OSM requests."""
import http.client
import json
import threading
import unittest
from unittest.mock import Mock, patch

from api_server import ApiServer, MAX_BODY, RouteService, load_tunnel_token, validate_request
from routing import InputError, demo_graph


def request_fixture(**changes):
    return {"drawing": [[100, 80], [100, 320], [400, 320]],
            "location": [127.0374, 37.5445], "distance_m": 2000,
            "data_source": "demo", "search_seconds": 3,
            "candidate_limit": 5, "search_mode": "all", **changes}


class InputTests(unittest.TestCase):
    def test_defaults_and_coordinate_order(self):
        payload, source, radius, provider, seconds = validate_request(request_fixture())
        self.assertEqual(payload["location"], [127.0374, 37.5445])
        self.assertEqual((source, radius, provider, seconds), ("demo", 2500, "local", 3))
        self.assertEqual(payload["drawing"], [[100, 80], [100, 320], [400, 320]])

    def test_invalid_inputs(self):
        changes = [{"location": [37.54, 127.03]}, {"drawing": []},
                   {"drawing": [[True, 0], [30, 0]]}, {"drawing": [["1", 0], [30, 0]]},
                   {"drawing": [[0, 0], [float("inf"), 0]]},
                   {"distance_m": True}, {"distance_m": "2000"},
                   {"distance_m": 100}, {"candidate_limit": 1.5},
                   {"distance_m": 8001}, {"distance_m": 20000},
                   {"search_seconds": 46}, {"search_seconds": 0},
                   {"allow_rotation": "false"}, {"allow_vertex_adjustment": 1},
                   {"route_type": []}, {"search_mode": []}, {"provider": []},
                   {"data_source": "fallback"}, {"radius_m": 99999},
                   {"search_mode": "local"}, {"extra_field": 123}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(InputError):
                validate_request(request_fixture(**change))
        with self.assertRaises(InputError):
            validate_request([])

    def test_focus_location_is_supported(self):
        payload, *_ = validate_request(request_fixture(search_mode="local", focus_location=[127.0374, 37.5445]))
        self.assertEqual(payload["focus_location"], [127.0374, 37.5445])


class TokenTests(unittest.TestCase):
    def test_existing_token_reused_without_overwrite(self):
        path = Mock()
        path.read_text.return_value = "a" * 43 + "\n"
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_tunnel_token(path), "a" * 43)
        path.open.assert_not_called()

    def test_missing_token_generated_with_exclusive_create(self):
        path = Mock()
        path.read_text.side_effect = FileNotFoundError
        file = Mock()
        path.open.return_value.__enter__ = Mock(return_value=file)
        path.open.return_value.__exit__ = Mock(return_value=False)
        with patch.dict("os.environ", {}, clear=True):
            token = load_tunnel_token(path)
        self.assertGreaterEqual(len(token), 32)
        path.open.assert_called_once_with("x", encoding="utf-8")
        file.write.assert_called_once_with(token + "\n")

    def test_environment_overrides_file_and_bad_tokens_fail_closed(self):
        path = Mock()
        for token in ("b" * 43, "short", "", "a" * 31, "a" * 129, "x" * 40 + "\n", "한" * 40):
            with self.subTest(token_length=len(token)), patch.dict("os.environ", {"DRAWRUN_API_TOKEN": token}, clear=True):
                if token == "b" * 43:
                    self.assertEqual(load_tunnel_token(path), token)
                else:
                    with self.assertRaises(InputError):
                        load_tunnel_token(path)
        path.read_text.assert_not_called()
        path.open.assert_not_called()

    def test_invalid_existing_file_not_overwritten(self):
        path = Mock()
        path.read_text.return_value = "short"
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(InputError):
            load_tunnel_token(path)
        path.open.assert_not_called()

    def test_create_race_reuses_other_process_token(self):
        path = Mock()
        path.read_text.side_effect = [FileNotFoundError, "c" * 43]
        path.open.side_effect = FileExistsError
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_tunnel_token(path), "c" * 43)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ApiServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.service = RouteService("test-token-not-a-real-secret")

    def request(self, method, path, body=None, headers=None, authenticated=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        request_headers = {"Content-Type": "application/json"}
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.server.service.token}"
        request_headers.update(headers or {})
        encoded = raw if raw is not None else json.dumps(body).encode() if body is not None else None
        conn.request(method, path, encoded, request_headers)
        response = conn.getresponse()
        status, data, response_headers = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return status, data, response_headers

    def test_health_and_local_ui(self):
        status, raw, _ = self.request("GET", "/v1/health", authenticated=False)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)["local_only"])
        for path in ("/", "/app.js", "/style.css"):
            status, raw, headers = self.request("GET", path, authenticated=False)
            self.assertEqual(status, 200)
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertNotIn(self.server.service.token.encode(), raw)

    def test_bootstrap_not_a_stored_key(self):
        status, raw, _ = self.request("GET", "/local/bootstrap", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"token": self.server.service.token})
        self.assertNotEqual(RouteService().token, RouteService().token)

    def test_map_config_contains_only_javascript_key(self):
        with patch("api_server.get_javascript_key", return_value="mock-web-key"):
            status, raw, headers = self.request("GET", "/local/map-config", authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"kakao_javascript_key": "mock-web-key"})
        self.assertIn("no-store", headers["Cache-Control"])
        status, _, _ = self.request("GET", "/route-map.js", authenticated=False)
        self.assertEqual(status, 200)

    def test_playground_defaults_to_existing_skk_osm_area(self):
        status, raw, _ = self.request("GET", "/", authenticated=False)
        self.assertEqual(status, 200)
        html = raw.decode("utf-8")
        self.assertIn('<option value="osm" selected>', html)
        self.assertIn('id="lng" type="number" value="126.9662925"', html)
        self.assertIn('id="lat" type="number" value="37.3033769"', html)
        self.assertIn('id="radius" type="number" value="4000"', html)
        self.assertNotIn("서울숲 예시", html)

    def test_no_static_file_or_config_exposure(self):
        for path in ("/.env", "/../.env", "/%2e%2e/.env", "/server.py", "/api/config", "/local-config.json", "/api_web/"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_external_hosts_origins_and_proxy_headers_blocked(self):
        headers = [{"Host": "example.ngrok-free.app"}, {"Origin": "https://evil.invalid"},
                   {"Sec-Fetch-Site": "cross-site"}, {"X-Forwarded-For": "8.8.8.8"},
                   {"Forwarded": "for=8.8.8.8"}]
        for header in headers:
            with self.subTest(header=header):
                status, _, _ = self.request("GET", "/local/bootstrap", headers=header)
                self.assertEqual(status, 403)
                status, _, _ = self.request("POST", "/v1/routes", request_fixture(), headers=header)
                self.assertEqual(status, 403)

    def test_missing_wrong_and_unicode_auth_blocked_before_work(self):
        for headers, authenticated in (({}, False), ({"Authorization": "Bearer wrong"}, True),
                                       ({"Authorization": "Bearer \u00e9"}, True)):
            with patch("api_server.generate") as compute, patch("api_server.get_osm") as download:
                status, raw, _ = self.request("POST", "/v1/routes", request_fixture(), headers, authenticated)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(raw)["error"]["code"], "UNAUTHORIZED")
                compute.assert_not_called()
                download.assert_not_called()

    def test_invalid_body_and_media_type(self):
        status, _, _ = self.request("POST", "/v1/routes", request_fixture(), {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        for raw in (b"broken JSON", b"[]", b'{"drawing": NaN}', b'{"location": Infinity}', b'\xff'):
            status, _, _ = self.request("POST", "/v1/routes", raw=raw)
            self.assertEqual(status, 400)
        status, _, _ = self.request("POST", "/v1/routes", raw=b"x" * (MAX_BODY + 1))
        self.assertEqual(status, 413)

    def test_unknown_post_is_not_executed(self):
        with patch("api_server.generate") as compute:
            status, _, _ = self.request("POST", "/api/generate", request_fixture())
            self.assertEqual(status, 404)
            compute.assert_not_called()

    def test_busy_does_not_start_another_compute(self):
        self.server.service.compute.acquire()
        try:
            with patch("api_server.generate") as compute:
                status, raw, headers = self.request("POST", "/v1/routes", request_fixture())
                self.assertEqual(status, 429)
                self.assertEqual(json.loads(raw)["error"]["code"], "BUSY")
                self.assertEqual(headers["Retry-After"], "5")
                compute.assert_not_called()
        finally:
            self.server.service.compute.release()

    def test_rate_limit(self):
        result = {"candidates": [], "source": "synthetic", "found": False}
        with patch("api_server.generate", return_value=result):
            for _ in range(12):
                status, _, _ = self.request("POST", "/v1/routes", request_fixture())
                self.assertEqual(status, 200)
            status, raw, _ = self.request("POST", "/v1/routes", request_fixture())
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(raw)["error"]["code"], "RATE_LIMITED")

    def test_real_engine_demo_and_graph_reuse(self):
        with patch("api_server.get_osm") as download:
            for index in range(2):
                status, raw, _ = self.request("POST", "/v1/routes", request_fixture())
                data = json.loads(raw)
                self.assertEqual(status, 200, data)
                self.assertEqual(data["source"], "synthetic")
                self.assertEqual(data["api_version"], "v1")
                self.assertEqual(data["data"]["graph_cache_hit"], bool(index))
                self.assertTrue(data["candidates"], data["message"])
                for candidate in data["candidates"]:
                    self.assertEqual(candidate["geometry"]["type"], "LineString")
                    self.assertGreater(len(candidate["geometry"]["coordinates"]), 1)
                    lng, lat = candidate["geometry"]["coordinates"][0]
                    self.assertTrue(124 < lng < 132 and 32 < lat < 39)
                download.assert_not_called()

    def test_osm_failure_not_replaced_by_fake_roads_and_slot_recovers(self):
        with patch("api_server.get_osm", side_effect=InputError("OSM test failure")), patch("api_server.demo_graph") as demo:
            status, raw, _ = self.request("POST", "/v1/routes", request_fixture(data_source="osm"))
            self.assertEqual(status, 422)
            self.assertIn("OSM", json.loads(raw)["error"]["message"])
            demo.assert_not_called()
        self.assertTrue(self.server.service.compute.acquire(blocking=False))
        self.server.service.compute.release()

    def test_osm_fetch_and_engine_parameters(self):
        graph = demo_graph([127.0374, 37.5445])
        graph.source = "osm"
        result = {"candidates": [], "source": "osm", "found": False}
        with patch("api_server.get_osm", return_value=({"elements": []}, True)) as fetch, \
             patch("api_server.graph_from_osm", return_value=graph) as build, \
             patch("api_server.generate", return_value=result) as compute:
            for _ in range(2):
                status, raw, _ = self.request("POST", "/v1/routes", request_fixture(data_source="osm"))
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw)["source"], "osm")
            fetch.assert_called_once_with([127.0374, 37.5445], 2500, "local")
            build.assert_called_once()
            self.assertEqual(compute.call_args.kwargs["seconds"], 3)


class TunnelTests(unittest.TestCase):
    """Emulate ngrok headers over loopback, never open a public tunnel."""
    @classmethod
    def setUpClass(cls):
        cls.server = ApiServer(("127.0.0.1", 0), api_only=True)
        cls.local = ApiServer(("127.0.0.1", 0), cls.server.service,
                              tunnel_port=cls.server.server_port)
        cls.threads = []
        for server in (cls.server, cls.local):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.local):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()

    def setUp(self):
        service = RouteService("test-tunnel-token-" + "x" * 32)
        self.server.service = self.local.service = service

    request = ApiTests.request

    def forwarded(self):
        return {"Host": "example.ngrok-free.app", "X-Forwarded-For": "203.0.113.8",
                "X-Forwarded-Proto": "https", "X-Forwarded-Host": "example.ngrok-free.app"}

    def test_health_available_and_token_never_exposed(self):
        status, raw, _ = self.request("GET", "/v1/health", headers=self.forwarded(), authenticated=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw), {"ok": True, "version": "v1", "local_only": False})
        self.assertNotIn(self.server.service.token.encode(), raw)

    def test_private_paths_never_exist_even_with_forged_local_host(self):
        paths = ("/", "/app.js", "/style.css", "/local/map-config", "/route-map.js", "/local/bootstrap", "/local/bootstrap?fake=1",
                 "/.env", "/.api-token", "/api/config", "/../local/bootstrap", "/%2e%2e/local/bootstrap")
        for path in paths:
            for headers in (self.forwarded(), {}, {"Host": f"localhost:{self.server.server_port}"}):
                with self.subTest(path=path, headers=headers):
                    status, raw, _ = self.request("GET", path, headers=headers)
                    self.assertEqual(status, 404)
                    self.assertNotIn(self.server.service.token.encode(), raw)
        status, _, _ = self.request("POST", "/local/bootstrap", {}, headers=self.forwarded())
        self.assertEqual(status, 404)

    def test_auth_required_before_download_or_calculation(self):
        for authenticated, extra in ((False, {}), (True, {"Authorization": "Bearer wrong"})):
            with patch("api_server.generate") as compute, patch("api_server.get_osm") as download:
                status, raw, _ = self.request("POST", "/v1/routes", request_fixture(),
                                              {**self.forwarded(), **extra}, authenticated)
                self.assertEqual(status, 401)
                self.assertEqual(json.loads(raw)["error"]["code"], "UNAUTHORIZED")
                compute.assert_not_called()
                download.assert_not_called()

    def test_native_mobile_request_uses_existing_engine(self):
        with patch("api_server.get_osm") as download:
            status, raw, _ = self.request("POST", "/v1/routes", request_fixture(), self.forwarded())
            data = json.loads(raw)
            self.assertEqual(status, 200)
            self.assertTrue(data["candidates"])
            self.assertEqual(data["source"], "synthetic")
            self.assertEqual(data["candidates"][0]["geometry"]["type"], "LineString")
            download.assert_not_called()

    def test_browser_origins_still_blocked_and_no_wildcard_cors(self):
        for extra in ({"Origin": "https://example.ngrok-free.app"},
                      {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}):
            status, _, headers = self.request("POST", "/v1/routes", request_fixture(), {**self.forwarded(), **extra})
            self.assertEqual(status, 403)
            self.assertNotIn("Access-Control-Allow-Origin", headers)
        status, _, _ = self.request("OPTIONS", "/v1/routes", headers=self.forwarded())
        self.assertEqual(status, 403)

    def test_flutter_web_preflight_and_auth_errors(self):
        for origin in ("http://localhost:5000", "http://127.0.0.1:5000"):
            headers = {**self.forwarded(), "Origin": origin, "Sec-Fetch-Site": "cross-site",
                       "Access-Control-Request-Method": "POST",
                       "Access-Control-Request-Headers": "Authorization, Content-Type, ngrok-skip-browser-warning"}
            status, raw, response = self.request("OPTIONS", "/v1/routes", headers=headers, authenticated=False)
            self.assertEqual(status, 204)
            self.assertEqual(raw, b"")
            self.assertEqual(response["Access-Control-Allow-Origin"], origin)
            status, _, response = self.request("POST", "/v1/routes", request_fixture(), headers, authenticated=False)
            self.assertEqual(status, 401)
            self.assertEqual(response["Access-Control-Allow-Origin"], origin)
            status, _, response = self.request("GET", "/v1/health", headers=headers, authenticated=False)
            self.assertEqual(status, 200)
            self.assertEqual(response["Access-Control-Expose-Headers"], "Retry-After")

    def test_preflight_rejects_wrong_port_method_and_headers(self):
        valid = {"Origin": "http://localhost:5000", "Access-Control-Request-Method": "POST"}
        for change in ({"Origin": "http://localhost:5001"},
                       {"Access-Control-Request-Method": "DELETE"},
                       {"Access-Control-Request-Headers": "x-unapproved"}):
            status, _, _ = self.request("OPTIONS", "/v1/routes", headers={**valid, **change}, authenticated=False)
            self.assertEqual(status, 403)

    def test_shared_busy_and_rate_limits(self):
        self.local.service.compute.acquire()
        try:
            status, raw, _ = self.request("POST", "/v1/routes", request_fixture(), self.forwarded())
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(raw)["error"]["code"], "BUSY")
        finally:
            self.local.service.compute.release()
        self.local.service.calls.clear()
        for _ in range(12):
            self.assertTrue(self.local.service.take_rate_slot())
        status, raw, _ = self.request("POST", "/v1/routes", request_fixture(), self.forwarded())
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(raw)["error"]["code"], "RATE_LIMITED")

    def test_private_listener_keeps_bootstrap_private_when_tunnel_enabled(self):
        # Point the common test HTTP helper at the private listener for this
        # case. Separate listeners must remain secure with the same service.
        public = self.server
        self.server = self.local
        try:
            status, raw, _ = self.request("GET", "/v1/health", authenticated=False)
            data = json.loads(raw)
            self.assertTrue(data["local_only"])
            self.assertTrue(data["tunnel_enabled"])
            self.assertEqual(data["tunnel_port"], public.server_port)
            status, raw, _ = self.request("GET", "/local/bootstrap", authenticated=False)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["token"], self.local.service.token)
            status, raw, _ = self.request("GET", "/local/bootstrap", headers=self.forwarded(), authenticated=False)
            self.assertEqual(status, 403)
            self.assertNotIn(self.local.service.token.encode(), raw)
        finally:
            self.server = public


if __name__ == "__main__":
    unittest.main()
