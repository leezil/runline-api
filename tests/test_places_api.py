from dataclasses import replace
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from api_server import ApiServer, RouteService
from facility_storage import Facility, FacilityStore, CctvDetails
from places_api import PlacesError, query_places, validate_query

QUERY = 'west=127&south=37.25&east=127.02&north=37.27'
STAMP = '2026-11-12T00:00:00+00:00'


class PlacesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.local = ApiServer(('127.0.0.1', 0))
        cls.external = ApiServer(('127.0.0.1', 0), api_only=True)
        cls.threads = [threading.Thread(target=server.serve_forever, daemon=True)
                       for server in (cls.local, cls.external)]
        for thread in cls.threads:
            thread.start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.local, cls.external):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'facilities.sqlite3'
        self.store = FacilityStore(self.path)
        self.store.initialize()
        self.service = RouteService('test-token', facility_database=self.path)
        self.local.service = self.external.service = self.service
        self.toilet = Facility('toilet', 'mois_toilet_v2', '3740000', 'toilet',
                              name='가상 화장실', address_road='가상 주소')
        self.camera = Facility('cctv', 'mois_cctv', '3740000', 'camera', name='방범 CCTV',
                              longitude=127.01, latitude=37.26, coordinate_source='source',
                              coordinate_status='valid', details=CctvDetails('생활방범', 3, True))

    def seed(self):
        self.store.upsert_many([self.toilet], import_state=('mois_toilet_v2', '3740000', STAMP))
        self.store.upsert_many([self.camera], import_state=('mois_cctv', '3740000', STAMP))

    def request(self, query=QUERY, *, server=None, auth=True, headers=None, method='GET', path='/v1/places'):
        server = server or self.local
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        h = {'Authorization': 'Bearer test-token'} if auth else {}
        h.update(headers or {})
        conn.request(method, path + ('?' + query if query else ''), headers=h)
        response = conn.getresponse()
        status, raw, returned = response.status, response.read(), dict(response.getheaders())
        conn.close()
        return status, json.loads(raw) if raw else None, returned

    def test_public_contract_coordinates_and_pending_excluded(self):
        self.seed()
        status, data, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(data['count'], 1)
        item = data['items'][0]
        self.assertEqual(item['location'], [127.01, 37.26])
        self.assertEqual(item['details'], {'purpose_raw': '생활방범', 'camera_count': 3})
        self.assertEqual(set(item), {'id', 'type', 'name', 'location', 'address', 'source', 'source_date', 'coordinate_source', 'details'})
        self.assertEqual(data['data_updated_at'], {'toilet': STAMP, 'cctv': STAMP})
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request(QUERY+'&types=toilet')[1]['items'], [])

    def test_same_endpoint_both_ports_and_auth_before_database(self):
        self.seed()
        for server in (self.local, self.external):
            self.assertEqual(self.request(server=server)[0], 200)
            self.assertEqual(self.request(server=server, auth=False)[0], 401)
            self.assertEqual(self.request(server=server, headers={'Authorization': 'Bearer wrong'})[0], 401)

    def test_validation_duplicates_unknowns_nonfinite_and_extent(self):
        self.seed()
        bad = ['', QUERY+'&types=', QUERY+'&types=cctv,cctv', QUERY+'&types=foo',
               QUERY+'&limit=0', QUERY+'&limit=501', QUERY+'&limit=1.5', QUERY+'&limit=+1',
               QUERY+'&limit=2&limit=3', QUERY+'&west=127', QUERY+'&key=SECRET',
               QUERY.replace('west=127', 'west=NaN'), QUERY.replace('west=127', 'west=inf'),
               QUERY.replace('east=127.02', 'east=126'), QUERY.replace('north=37.27', 'north=37.25'),
               QUERY.replace('west=127', 'west=37'), QUERY+'&types=%ZZ', 'x='+'a'*2100]
        for query in bad:
            self.assertEqual(self.request(query)[0], 400, query)
        status, data, _ = self.request('west=126&south=37&east=128&north=38')
        self.assertEqual((status, data['error']['code']), (400, 'BOUNDS_TOO_LARGE'))

    def test_truncation_stable_order_and_boundary(self):
        self.seed()
        self.store.upsert_many([replace(self.camera, source_record_id='two', longitude=127),
                               replace(self.camera, source_record_id='outside', longitude=127.020001)])
        _, full, _ = self.request()
        _, limited, _ = self.request(QUERY+'&limit=1')
        self.assertEqual(full['count'], 2)
        self.assertFalse(full['truncated'])
        self.assertEqual(limited['count'], 1)
        self.assertTrue(limited['truncated'])
        self.assertEqual(limited['items'], full['items'][:1])

    def test_inactive_invalid_and_unconfirmed_cctv_excluded(self):
        self.seed()
        self.store.upsert_many([replace(self.camera, active=False),
                               replace(self.camera, source_record_id='other', details=CctvDetails('기타', 1, None)),
                               replace(self.camera, source_record_id='invalid', coordinate_status='invalid',
                                       coordinate_source='none', longitude=None, latitude=None)])
        self.assertEqual(self.request()[1]['items'], [])

    def test_uninitialized_type_vs_successfully_empty_import(self):
        self.store.upsert_many([self.camera], import_state=('mois_cctv', '3740000', STAMP))
        self.assertEqual(self.request()[0], 503)
        self.assertEqual(self.request(QUERY+'&types=cctv')[0], 200)
        self.store.upsert_many([], import_state=('mois_toilet_v2', '3740000', STAMP))
        status, data, _ = self.request(QUERY+'&types=toilet')
        self.assertEqual((status, data['count'], data['truncated']), (200, 0, False))

    def test_failed_refresh_serves_old_data_with_failure_state(self):
        self.seed()
        self.store.record_import_failure('mois_cctv', '3740000', '2026-11-13T00:00:00Z')
        status, data, _ = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(data['count'], 1)
        state = next(s for s in data['sources'] if s['type']=='cctv')
        self.assertTrue(state['last_import_failed'])
        self.assertEqual(state['data_updated_at'], STAMP)

    def test_initial_failed_import_not_ready(self):
        self.store.record_import_failure('mois_cctv', '3740000', STAMP)
        status, data, _ = self.request(QUERY+'&types=cctv')
        self.assertEqual((status, data['error']['code']), (503, 'FACILITIES_NOT_READY'))

    def test_legacy_database_has_honest_unknown_success_time(self):
        self.store.upsert_many([self.camera])
        before = self.path.read_bytes()
        status, data, _ = self.request(QUERY+'&types=cctv')
        self.assertEqual(status, 200)
        self.assertIsNone(data['data_updated_at']['cctv'])
        self.assertEqual(data['sources'][0]['regions'][0]['last_attempt_status'], 'unknown')
        self.assertEqual(before, self.path.read_bytes())

    def test_missing_or_corrupt_database_is_503_without_path_leak(self):
        other = Path(self.tmp.name) / 'secret-path.sqlite3'
        self.service.facility_store = FacilityStore(other)
        status, data, _ = self.request()
        self.assertEqual(status, 503)
        self.assertFalse(other.exists())
        self.assertNotIn('secret-path', json.dumps(data))
        other.write_text('not a database', encoding='utf-8')
        self.assertEqual(self.request()[0], 503)

    def test_rate_budget_shared_between_ports_but_not_routes(self):
        self.seed()
        for _ in range(120):
            self.assertTrue(self.service.take_places_slot())
        for server in (self.local, self.external):
            status, _, headers = self.request(server=server)
            self.assertEqual(status, 429)
            self.assertEqual(headers['Retry-After'], '60')
        self.assertTrue(self.service.take_rate_slot())

    def test_cors_preflight_and_origin_restrictions(self):
        self.seed()
        headers = {'Origin': 'http://localhost:5000', 'Access-Control-Request-Method': 'GET',
                   'Access-Control-Request-Headers': 'authorization,ngrok-skip-browser-warning'}
        status, _, returned = self.request(server=self.external, method='OPTIONS', auth=False, headers=headers)
        self.assertEqual(status, 204)
        self.assertEqual(returned['Access-Control-Allow-Methods'], 'GET, OPTIONS')
        self.assertEqual(self.request(server=self.external, headers={'Origin':'https://evil.test'})[0], 403)
        self.assertEqual(self.request(headers={'X-Forwarded-For':'1.2.3.4'})[0], 403)

    def test_request_log_never_contains_query_or_token(self):
        self.seed()
        self.request()
        text = json.dumps(self.service.log_snapshot())
        self.assertIn('/v1/places', text)
        self.assertNotIn('127.02', text)
        self.assertNotIn('test-token', text)

    def test_rows_and_success_marker_rollback_together(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.store.upsert_many([replace(self.camera, longitude=127.015)],
                                   import_state=('mois_cctv','3740000','bad timestamp'))
        self.assertEqual(self.request()[1]['items'][0]['location'], [127.01,37.26])
        self.assertEqual(self.request()[1]['data_updated_at']['cctv'], STAMP)


if __name__ == '__main__':
    unittest.main()
