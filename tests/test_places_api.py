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

    def status_request(self, **kwargs):
        return self.request(query='', path='/v1/places/status', **kwargs)

    def test_status_counts_and_map_eligibility(self):
        self.seed()
        self.store.upsert_many([
            replace(self.camera, source_record_id='inactive', active=False),
            replace(self.camera, source_record_id='unknown', details=CctvDetails('기타', 1, None)),
            replace(self.camera, source_record_id='other', details=CctvDetails('교통', 1, False)),
            replace(self.camera, source_record_id='invalid', longitude=None, latitude=None,
                    coordinate_source='none', coordinate_status='invalid'),
            replace(self.toilet, source_record_id='failed', coordinate_status='failed'),
            replace(self.toilet, source_record_id='ambiguous', coordinate_status='ambiguous'),
            replace(self.toilet, source_record_id='inactive', active=False)])
        before = self.path.read_bytes()
        status, data, headers = self.status_request()
        self.assertEqual(status, 200)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        toilet, cctv = data['sources']
        self.assertEqual(cctv['counts']['stored'], 5)
        self.assertEqual(cctv['counts']['active'], 4)
        self.assertEqual(cctv['counts']['inactive'], 1)
        self.assertEqual(cctv['counts']['map_ready'], self.request()[1]['count'])
        self.assertEqual(cctv['counts']['map_ready'], 1)
        self.assertEqual(toilet['counts']['coordinates'],
                         {'pending': 2, 'valid': 0, 'invalid': 0, 'ambiguous': 1, 'failed': 1})
        self.assertEqual(toilet['counts']['active_coordinates']['pending'], 1)
        self.assertEqual(cctv['regions'][0]['counts'], cctv['counts'])
        self.assertEqual(before, self.path.read_bytes())
        self.assertNotIn('가상 주소', json.dumps(data, ensure_ascii=False))

    def test_facility_monitor_logs_only_safe_summary(self):
        self.seed()
        self.request(QUERY+'&types=cctv')
        self.status_request()
        # The log is finalized after response write; only inspect summary fields here.
        logs = self.service.log_snapshot()
        places = next(e for e in logs if e['path'] == '/v1/places')
        self.assertEqual(places['facility_types'], ['cctv'])
        self.assertEqual(places['facility_count'], 1)
        self.assertFalse(places['truncated'])
        status = next(e for e in logs if e['path'] == '/v1/places/status')
        self.assertEqual(status['facility_summary'], [
            {'type':'toilet','stored':1,'map_ready':0}, {'type':'cctv','stored':1,'map_ready':1}])
        raw = json.dumps(logs, ensure_ascii=False)
        for secret in ('test-token','가상 주소','127.02','items','address'):
            self.assertNotIn(secret, raw)
        self.assertEqual(self.request(query='', path='/facility-monitor.js', server=self.external)[0], 404)

    def test_status_empty_uncollected_and_successful_empty(self):
        status, data, _ = self.status_request()
        self.assertEqual(status, 200)
        self.assertTrue(all(not s['ready'] and not s['regions'] for s in data['sources']))
        self.store.upsert_many([], import_state=('mois_toilet_v2', '3740000', STAMP))
        toilet = self.status_request()[1]['sources'][0]
        self.assertTrue(toilet['ready'])
        self.assertEqual(toilet['counts']['stored'], 0)
        self.assertEqual(toilet['regions'][0]['last_success_at'], STAMP)

    def test_status_region_isolation_failure_and_legacy_unknown(self):
        self.store.upsert_many([self.toilet])
        self.store.upsert_many([replace(self.toilet, source_region_code='3830000')],
                              import_state=('mois_toilet_v2', '3830000', STAMP))
        later = '2026-11-13T00:00:00Z'
        self.store.record_import_failure('mois_toilet_v2', '3830000', later)
        self.store.record_import_failure('mois_cctv', '3830000', later)
        toilet, cctv = self.status_request()[1]['sources']
        self.assertEqual(toilet['counts']['stored'], 2)
        self.assertIsNone(toilet['data_updated_at'])
        old, failed = toilet['regions']
        self.assertEqual(old['last_attempt_status'], 'unknown')
        self.assertIsNone(old['last_success_at'])
        self.assertEqual(failed['last_attempt_status'], 'failed')
        self.assertEqual(failed['last_success_at'], STAMP)
        self.assertEqual(failed['counts']['stored'], 1)
        self.assertTrue(toilet['last_import_failed'])
        self.assertFalse(cctv['ready'])
        self.assertEqual(cctv['regions'][0]['counts']['stored'], 0)

    def test_status_auth_both_ports_and_shared_rate_limit(self):
        for server in (self.local, self.external):
            self.assertEqual(self.status_request(server=server, auth=False)[0], 401)
            self.assertEqual(self.status_request(server=server, headers={'Authorization':'Bearer wrong'})[0], 401)
            self.assertEqual(self.status_request(server=server)[0], 200)
        for _ in range(118):
            self.assertTrue(self.service.take_places_slot())
        self.assertEqual(self.status_request()[0], 429)
        self.assertEqual(self.request()[0], 429)

    def test_status_storage_failures_and_auth_first(self):
        missing = Path(self.tmp.name) / 'private-file.sqlite3'
        self.service.facility_store = FacilityStore(missing)
        self.assertEqual(self.status_request(auth=False)[0], 401)
        status, data, _ = self.status_request()
        self.assertEqual((status, data['error']['code']), (503, 'FACILITIES_UNAVAILABLE'))
        self.assertFalse(missing.exists())
        self.assertNotIn('private-file', json.dumps(data))
        missing.write_bytes(b'corrupt')
        self.assertEqual(self.status_request()[0], 503)

    def test_status_cors_validation_and_sanitized_log(self):
        headers = {'Origin':'http://localhost:5000', 'Access-Control-Request-Method':'GET',
                   'Access-Control-Request-Headers':'authorization'}
        self.assertEqual(self.status_request(server=self.external, method='OPTIONS', auth=False, headers=headers)[0], 204)
        self.assertEqual(self.status_request(server=self.external, headers={'Origin':'https://evil.test'})[0], 403)
        self.assertEqual(self.request('key=PRIVATE_QUERY', path='/v1/places/status')[0], 400)
        logs = json.dumps(self.service.log_snapshot())
        self.assertIn('/v1/places/status', logs)
        self.assertNotIn('PRIVATE_QUERY', logs)
        self.assertNotIn('test-token', logs)


if __name__ == '__main__':
    unittest.main()
