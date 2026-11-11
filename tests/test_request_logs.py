import http.client
import json
import threading
import unittest
from unittest.mock import patch
from api_server import ApiServer, RouteService


class RequestLogTests(unittest.TestCase):
    def test_shared_private_logs_and_live_stage(self):
        service = RouteService('test-secret-token')
        private = ApiServer(('127.0.0.1', 0), service)
        public = ApiServer(('127.0.0.1', 0), service, api_only=True)
        threads = []
        for server in (private, public):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            threads.append(thread)

        def request(server, method, path, body=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            conn.request(method, path, body, headers or {})
            response = conn.getresponse()
            status, data = response.status, json.loads(response.read())
            conn.close()
            return status, data

        entered, release = threading.Event(), threading.Event()
        def generate(*args, **kwargs):
            entered.set()
            release.wait(5)
            return {'candidates': [], 'recommended_count': 0, 'found': False}
        try:
            self.assertEqual(request(public, 'POST', '/v1/routes?secret=hidden', '{}')[0], 401)
            self.assertEqual(request(public, 'GET', '/local/request-logs')[0], 404)
            status, data = request(private, 'GET', '/local/request-logs')
            self.assertEqual(status, 200)
            self.assertEqual(len(data['entries']), 2)
            self.assertIn('UNAUTHORIZED', json.dumps(data))
            self.assertNotIn('hidden', json.dumps(data))
            self.assertNotIn(service.token, json.dumps(data))
            self.assertEqual(request(private, 'GET', '/local/request-logs', headers={'Origin':'http://localhost:5000'})[0], 403)
            with patch('api_server.generate', side_effect=generate):
                results = []
                payload = json.dumps({'drawing':[[100,80],[100,320],[400,320]], 'location':[127.0374,37.5445], 'data_source':'demo'})
                worker = threading.Thread(target=lambda: results.append(request(public, 'POST', '/v1/routes', payload, {'Authorization':f'Bearer {service.token}', 'Content-Type':'application/json'})))
                worker.start()
                self.assertTrue(entered.wait(5))
                entries = request(private, 'GET', '/local/request-logs')[1]['entries']
                self.assertEqual(entries[0]['stage'], 'calculating')
                release.set()
                worker.join(5)
                self.assertEqual(results[0][0], 200)
                entries = request(private, 'GET', '/local/request-logs')[1]['entries']
                self.assertEqual(entries[0]['stage'], 'completed')
                self.assertEqual(entries[0]['candidate_count'], 0)
                self.assertNotIn('127.0374', json.dumps(entries))
            for _ in range(205):
                request(public, 'GET', '/v1/health')
            self.assertEqual(len(service.log_snapshot()), 200)
        finally:
            release.set()
            for server in (private, public):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join()


if __name__ == '__main__':
    unittest.main()
