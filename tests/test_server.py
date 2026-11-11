import http.client
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from server import COOLDOWN, GRAPHS, Handler, get_osm, get_javascript_key, read_dotenv_javascript_key, osm_connection_message
from routing import InputError


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, data=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=30)
        body = json.dumps(data).encode() if data is not None else None
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def test_static_and_health(self):
        status, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("DrawRun", raw.decode())
        self.assertIn('id="map-status"', raw.decode())
        self.assertIn('id="distance" type="number" min="0.3" max="20"', raw.decode())
        for required_id in ("place-search-form", "place-query", "place-results", "location-name", "vertex-adjustment", "show-adjusted"):
            self.assertIn(f'id="{required_id}"', raw.decode())
        for removed_id in ("settings-toggle", "settings", "map-form", "map-key", "key-status", "connect-map"):
            self.assertNotIn(f'id="{removed_id}"', raw.decode())
        status, raw = self.request("GET", "/api/health")
        self.assertTrue(json.loads(raw)["ok"])

    def test_native_key_is_not_exposed(self):
        status, raw = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(raw)), {"kakao_javascript_key"})
        status, _ = self.request("GET", "/local-config.json")
        self.assertEqual(status, 404)

    def test_dotenv_config_reload_and_private_values_not_exposed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dotenv = root / ".env"
            dotenv.write_text("KAKAO_JAVASCRIPT_KEY=" + "a" * 32 + "\nKAKAO_NATIVE_APP_KEY=private-native\nADMIN_KEY=private-admin\n", encoding="utf-8")
            with patch("server.ROOT", root), patch.dict(os.environ, {}, clear=True):
                status, raw = self.request("GET", "/api/config")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(raw), {"kakao_javascript_key": "a" * 32})
                self.assertNotIn(b"private", raw)
                dotenv.write_text("KAKAO_JAVASCRIPT_KEY=" + "b" * 32, encoding="utf-8")
                _, raw = self.request("GET", "/api/config")
                self.assertEqual(json.loads(raw)["kakao_javascript_key"], "b" * 32)
                for path in ("/.env", "/.env.example", "/../.env"):
                    status, _ = self.request("GET", path)
                    self.assertEqual(status, 404)

    def test_foreign_origin_and_host_denied(self):
        status, _ = self.request("POST", "/api/demo", {"center": [127, 37]}, {"Origin": "https://evil.invalid"})
        self.assertEqual(status, 403)
        status, _ = self.request("GET", "/api/config", headers={"Host": "evil.invalid"})
        self.assertEqual(status, 403)

    def test_invalid_and_unknown_graph(self):
        status, _ = self.request("POST", "/api/generate", {"graph_id": "unknown"})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/demo", {"center": [0, 0]})
        self.assertEqual(status, 400)

    def test_demo_api(self):
        status, raw = self.request("POST", "/api/demo", {"center": [127.0374, 37.5445]})
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["source"], "synthetic")
        self.assertGreater(data["edges"], 0)
        self.assertTrue(data["graph_id"])

    def test_test_project_sets_20km_limit_on_engine(self):
        graph = object()
        payload = {"graph_id": "long-route-test", "distance_m": 20000}
        with patch.dict(GRAPHS, {"long-route-test": graph}), \
             patch("server.generate", return_value={"candidates": [], "found": False}) as compute:
            status, raw = self.request("POST", "/api/generate", payload)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["candidates"], [])
            compute.assert_called_once_with(graph, payload, max_distance_m=20000)

    def test_unknown_osm_provider_rejected(self):
        with self.assertRaises(InputError):
            get_osm([127.04, 37.54], 912, "arbitrary-url")

    def test_public_server_rate_limit_has_cooldown(self):
        COOLDOWN.clear()
        limited = urllib.error.HTTPError("https://overpass-api.de/api/interpreter", 429, "limited", {}, None)
        try:
            with patch("server.urllib.request.urlopen", side_effect=limited) as request:
                with self.assertRaises(InputError):
                    get_osm([127.041234, 37.541234], 913)
                with self.assertRaises(InputError):
                    get_osm([127.041234, 37.541234], 913)
                self.assertEqual(request.call_count, 1)
        finally:
            COOLDOWN.clear()


class ConfigTests(unittest.TestCase):
    def test_dotenv_comments_quotes_export_and_windows_bom(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ".env"
            for line in ('KAKAO_JAVASCRIPT_KEY=abc # comment',
                         'KAKAO_JAVASCRIPT_KEY="abc" # comment',
                         "export KAKAO_JAVASCRIPT_KEY = 'abc'"):
                with self.subTest(line=line):
                    path.write_text("# ignored\nOTHER_KEY=private\n" + line + "\n", encoding="utf-8-sig")
                    self.assertEqual(read_dotenv_javascript_key(path), "abc")
            path.write_text('KAKAO_JAVASCRIPT_KEY="unterminated\n', encoding="utf-8")
            self.assertEqual(read_dotenv_javascript_key(path), "")

    def test_environment_dotenv_and_json_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "local-config.json").write_text(json.dumps({"kakao_javascript_key": "legacy"}), encoding="utf-8")
            dotenv = root / ".env"
            dotenv.write_text("KAKAO_JAVASCRIPT_KEY=dotenv", encoding="utf-8")
            with patch("server.ROOT", root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(get_javascript_key(), "dotenv")
                with patch.dict(os.environ, {"KAKAO_JAVASCRIPT_KEY": " environment "}):
                    self.assertEqual(get_javascript_key(), "environment")
                dotenv.write_text("KAKAO_JAVASCRIPT_KEY=", encoding="utf-8")
                self.assertEqual(get_javascript_key(), "legacy")

    def test_missing_or_invalid_configuration_returns_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch("server.ROOT", root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(get_javascript_key(), "")
                for data in ("invalid-json", "[]", '{"kakao_javascript_key": 123}'):
                    (root / "local-config.json").write_text(data, encoding="utf-8")
                    self.assertEqual(get_javascript_key(), "")


class OsmLoadingTests(unittest.TestCase):
    def test_failure_messages_distinguish_timeout_and_refused_connection(self):
        self.assertIn("초과", osm_connection_message(TimeoutError()))
        self.assertIn("거절", osm_connection_message(urllib.error.URLError(ConnectionRefusedError())))

    def test_recent_cache_avoids_network(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            center, radius = [127.04, 37.54], 2500.0
            key = hashlib.sha256(json.dumps([round(center[0], 4), round(center[1], 4), radius]).encode()).hexdigest()[:24]
            data = {"elements": [{"type": "node", "id": 1, "lat": 37.54, "lon": 127.04}]}
            (cache / (key + ".json")).write_text(json.dumps(data), encoding="utf-8")
            with patch("server.CACHE", cache), patch("server.urllib.request.urlopen") as request:
                result, cached = get_osm(center, radius)
            self.assertEqual(result, data)
            self.assertTrue(cached)
            request.assert_not_called()

    def test_stale_cache_is_exact_area_only_bounded_and_preserves_age(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            center, radius = [127.04, 37.54], 2500.0
            key = hashlib.sha256(json.dumps([round(center[0], 4), round(center[1], 4), radius]).encode()).hexdigest()[:24]
            data = {"elements": []}
            path = cache / (key + ".json")
            path.write_text(json.dumps(data), encoding="utf-8")
            saved = time.time() - 2 * 86400
            os.utime(path, (saved, saved))
            with patch("server.CACHE", cache), patch("server.urllib.request.urlopen", side_effect=TimeoutError()):
                result, cached = get_osm(center, radius)
                self.assertTrue(cached)
                self.assertTrue(result["_drawrun_fetch"]["cache_stale"])
                self.assertIn("이전", result["_drawrun_fetch"]["load_warning"])
                self.assertAlmostEqual(path.stat().st_mtime, saved, places=3)
                with self.assertRaises(InputError):
                    get_osm([127.05, 37.54], radius)
                old = time.time() - 8 * 86400
                os.utime(path, (old, old))
                with self.assertRaises(InputError):
                    get_osm(center, radius)

    def test_corrupt_cache_is_refetched(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            center, radius = [127.04, 37.54], 2500.0
            key = hashlib.sha256(json.dumps([round(center[0], 4), round(center[1], 4), radius]).encode()).hexdigest()[:24]
            path = cache / (key + ".json")
            path.write_text("broken-json", encoding="utf-8")
            from io import BytesIO
            with patch("server.CACHE", cache), patch("server.urllib.request.urlopen", return_value=BytesIO(b'{"elements": []}')):
                result, cached = get_osm(center, radius)
            self.assertFalse(cached)
            self.assertEqual(result, {"elements": []})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), result)


if __name__ == "__main__":
    unittest.main()
