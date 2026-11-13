from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

from facility_storage import Facility, FacilityStore
import toilet_geocoding as geo


ADDRESS = '경기도 수원시 테스트로 10'
CANDIDATE = dict(admCd='4111110100', rnMgtSn='411113000001', udrtYn='0',
                 buldMnnm='10', buldSlno='0', bdMgtSn='4111110100100010000000010',
                 roadAddrPart1=ADDRESS, jibunAddr='경기도 수원시 테스트동 1')
COORD = dict(CANDIDATE, entX='1000000', entY='2000000')


def envelope(total=1, rows=None, code='0'):
    return json.dumps({'results': {'common': {'errorCode': code, 'totalCount': str(total)},
                                   'juso': [CANDIDATE] if rows is None else rows}}).encode()


class FakeClient:
    requests = 0

    def search(self, address):
        self.requests += 1
        return 1, [CANDIDATE.copy()]

    def coordinates(self, candidate):
        self.requests += 1
        return 1, [COORD.copy()]


def transform(x, y, **kwargs):
    return 127.5, 38.0


class GeocodingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FacilityStore(Path(self.tmp.name) / 'test.sqlite3')
        self.store.initialize()
        self.record = Facility('toilet', 'mois_toilet_v2', '3740000', 'one', address_road=ADDRESS)
        self.identity, = self.store.upsert_many([self.record])

    def row(self):
        return self.store.get(self.identity)

    def test_local_save_index_and_idempotent_resume(self):
        client = FakeClient()
        report = geo.run(self.store, '3740000', client=client, transform=transform)
        self.assertEqual(report['outcomes'], {'valid': 1})
        saved = self.row()
        self.assertEqual((saved['longitude'], saved['latitude']), (127.5, 38))
        self.assertEqual(saved['geocoder_provider'], geo.PROVIDER)
        self.assertEqual(self.store.query_bounds(127, 37, 128, 39)['count'], 1)
        self.assertEqual(geo.run(self.store, '3740000', client=client, transform=transform)['selected'], 0)
        self.assertEqual(client.requests, 2)
        self.store.upsert_many([replace(self.record, name='updated')], preserve_local_state=True)
        self.assertEqual(self.row()['coordinate_status'], 'valid')
        self.store.upsert_many([replace(self.record, address_road=ADDRESS+'-1')], preserve_local_state=True)
        self.assertEqual(self.row()['coordinate_status'], 'pending')
        self.assertIsNone(self.row()['longitude'])
        self.assertEqual(self.store.query_bounds(127, 37, 128, 39)['count'], 0)

    def test_race_never_restores_old_address_or_overwrites_import(self):
        snapshot = self.row()
        self.store.upsert_many([replace(self.record, address_road=ADDRESS+'-1')])
        self.assertFalse(self.store.save_geocoding(snapshot, status='valid', longitude=127.5,
                         latitude=38, provider=geo.PROVIDER, timestamp=geo.utc_now()))
        self.assertIsNone(self.row()['longitude'])

    def test_another_worker_or_deactivation_wins(self):
        snapshot = self.row()
        kwargs = dict(status='valid', longitude=127.5, latitude=38,
                      provider=geo.PROVIDER, timestamp=geo.utc_now())
        self.assertTrue(self.store.save_geocoding(snapshot, **kwargs))
        self.assertFalse(self.store.save_geocoding(snapshot, **kwargs))
        self.store.upsert_many([replace(self.record, active=False)])
        self.assertFalse(self.store.save_geocoding(self.row(), **kwargs))

    def test_duplicate_addresses_cached_in_batch(self):
        self.store.upsert_many([replace(self.record, source_record_id='two')])
        client = FakeClient()
        report = geo.run(self.store, '3740000', client=client, transform=transform)
        self.assertEqual((report['saved'], report['cache_hits'], client.requests), (2, 1, 2))

    def test_missing_addresses_and_explicit_retry(self):
        self.store.upsert_many([replace(self.record, address_road=None)])
        client = FakeClient()
        report = geo.run(self.store, '3740000', client=client, transform=transform)
        self.assertEqual(report['outcomes'], {'invalid': 1})
        self.assertEqual(client.requests, 0)
        self.assertEqual(self.row()['coordinate_source'], 'none')
        self.assertEqual(self.store.geocoding_candidates('3740000'), [])
        self.assertEqual(len(self.store.geocoding_candidates('3740000', retry=True)), 1)

    def test_dry_run_never_calls_provider_or_writes_database(self):
        before = self.store.path.read_bytes()
        report = geo.run(self.store, '3740000', dry_run=True)
        self.assertEqual((report['selected'], report['status']), (1, 'planned'))
        self.assertEqual(before, self.store.path.read_bytes())

    def test_region_source_and_inactive_excluded(self):
        self.store.upsert_many([replace(self.record, source_record_id='other', source_region_code='3830000'),
                               replace(self.record, source_record_id='inactive', active=False),
                               replace(self.record, source_record_id='source', coordinate_source='source',
                                       coordinate_status='valid', longitude=127, latitude=37)])
        self.assertEqual(len(self.store.geocoding_candidates('3740000', retry=True)), 1)

    def test_address_mismatch_and_ambiguous_never_choose_first(self):
        for total, candidates, reason in [(2, [CANDIDATE, CANDIDATE], 'MULTIPLE_ADDRESSES'),
                                         (1, [dict(CANDIDATE, roadAddrPart1=ADDRESS+'-1')], 'ADDRESS_MISMATCH')]:
            client = FakeClient()
            client.search = lambda _: (total, candidates)
            result = geo.geocode(self.row(), client, transform)
            self.assertEqual((result.status, result.reason), ('ambiguous', reason))
            self.assertEqual(client.requests, 0)

    def test_road_normalization_preserves_building_identity(self):
        examples = {
            '경기 수원시 장안구 서부로2142(율전동)': '경기도 수원시 장안구 서부로 2142',
            '경기도 수원시 장안구 서부로 2149, 성대역사 (율전동)': '경기도 수원시 장안구 서부로 2149',
            '경기도 수원시 권선구 동수원로 177번길40(권선동)': '경기도 수원시 권선구 동수원로177번길 40',
            '경기도 수원시 테스트로10 - 1, 별관 2층': '경기도 수원시 테스트로 10-1',
        }
        for original, expected in examples.items():
            self.assertEqual(geo.normalize_address(original, road=True), expected)
        for address in [ADDRESS+'-1', ADDRESS+'1', ADDRESS+' 101동', ADDRESS+'-1-2',
                        ADDRESS+', 12', ADDRESS+', '+ADDRESS+'-1']:
            self.assertNotEqual(geo.normalize_address(address, road=True), ADDRESS)
        self.assertEqual(geo.normalize_address('경기도 수원시 테스트동 1-2', road=False),
                         '경기도 수원시 테스트동 1-2')

    def test_search_uses_normalized_road_but_keeps_source(self):
        row = self.row()
        row['address_road'] = '경기 수원시 테스트로10, 건물명 (테스트동)'
        calls = []
        client = FakeClient()
        def search(query):
            calls.append(query)
            return 1, [CANDIDATE.copy()]
        client.search = search
        self.assertEqual(geo.geocode(row, client, transform).status, 'valid')
        self.assertEqual(calls, [ADDRESS])
        self.assertEqual(row['address_road'], '경기 수원시 테스트로10, 건물명 (테스트동)')
        client.search = lambda _: (1, [dict(CANDIDATE, roadAddrPart1=ADDRESS+'-1')])
        self.assertEqual(geo.geocode(row, client, transform).reason, 'ADDRESS_MISMATCH')

    def test_normalization_is_not_specific_to_suwon_or_yuljeon(self):
        cases = [
            ('서울특별시 중구 세종대로110, 서울시청', '서울특별시 중구 세종대로 110'),
            ('부산광역시 연제구 중앙대로1001(연산동)', '부산광역시 연제구 중앙대로 1001'),
            ('대전광역시 서구 둔산로100, 청사', '대전광역시 서구 둔산로 100'),
            ('제주특별자치도 제주시 문연로6(연동)', '제주특별자치도 제주시 문연로 6'),
            ('경기도 안양시 동안구 인덕원로 24번길56, 2층', '경기도 안양시 동안구 인덕원로24번길 56'),
        ]
        for original, canonical in cases:
            with self.subTest(address=original):
                row = self.row(); row['address_road'] = original
                client = FakeClient()
                calls = []
                def search(query):
                    calls.append(query)
                    return 1, [dict(CANDIDATE, roadAddrPart1=canonical)]
                client.search = search
                self.assertEqual(geo.geocode(row, client, transform).status, 'valid')
                self.assertEqual(calls, [canonical])

    def test_lot_fallback_only_after_zero_road_results(self):
        row = self.row()
        row['address_lot'] = CANDIDATE['jibunAddr']
        client = FakeClient()
        client.search = lambda address: (0, []) if address == ADDRESS else (1, [CANDIDATE])
        self.assertEqual(geo.geocode(row, client, transform).status, 'valid')
        client.search = lambda _: (0, [])
        self.assertEqual(geo.geocode(row, client, transform).reason, 'ADDRESS_NOT_FOUND')

    def test_coordinate_ambiguity_building_mismatch_and_invalid_values(self):
        cases = [(2, [COORD, COORD], 'ambiguous'), (1, [dict(COORD, bdMgtSn='other')], 'ambiguous'),
                 (1, [dict(COORD, entX='NaN')], 'invalid'), (1, [dict(COORD, entX=True)], 'invalid'),
                 (0, [], 'failed')]
        for total, rows, status in cases:
            client = FakeClient()
            client.coordinates = lambda _: (total, rows)
            self.assertEqual(geo.geocode(self.row(), client, transform).status, status)
        self.assertEqual(geo.geocode(self.row(), FakeClient(), lambda *a, **kw: (37, 127)).status, 'invalid')

    def test_crs_control_point_and_axis_order(self):
        try:
            converter = geo.make_transformer()
        except geo.GeocodingFailure:
            self.skipTest('Install requirements-geocoding.txt for CRS verification')
        lon, lat = converter(1000000, 2000000, errcheck=True)
        self.assertAlmostEqual(lon, 127.5, places=7)
        self.assertAlmostEqual(lat, 38, places=7)
        self.assertEqual(geo.geocode(self.row(), FakeClient(), converter).status, 'valid')
        client = FakeClient()
        client.coordinates = lambda _: (1, [dict(COORD, entX='1e300')])
        self.assertEqual(geo.geocode(self.row(), client, converter).status, 'invalid')

    def test_interruption_retains_finished_rows_and_unprocessed_pending(self):
        self.store.upsert_many([replace(self.record, source_record_id='two', address_road=ADDRESS+'-1')])
        calls = []
        def resolve(row, client, converter):
            calls.append(row['id'])
            if len(calls) == 2:
                raise geo.GeocodingFailure('REQUEST_BUDGET_EXHAUSTED')
            return geo.Result('valid', 'MATCHED_ENTRANCE', 127.5, 38)
        report = {}
        with patch.object(geo, 'geocode', side_effect=resolve), self.assertRaises(geo.GeocodingFailure):
            geo.run(self.store, '3740000', client=FakeClient(), transform=transform, report=report)
        self.assertEqual((report['saved'], report['status']), (1, 'failed'))
        self.assertEqual(len(self.store.geocoding_candidates('3740000')), 1)

    def test_response_validation_and_safe_errors(self):
        self.assertEqual(geo.parse_response(envelope())[0], 1)
        self.assertEqual(geo.parse_response(envelope(0, [])), (0, []))
        for raw in [b'<error>SECRET</error>', b'{}', envelope(0, [CANDIDATE]),
                    envelope(2, []), envelope(code='E0001')]:
            with self.assertRaises(geo.GeocodingFailure) as error:
                geo.parse_response(raw)
            self.assertNotIn('SECRET', str(error.exception))

    def test_request_post_key_encoding_budget_retry_and_auth(self):
        requests = []
        def opener(req, timeout):
            requests.append(req)
            if len(requests) == 1:
                raise urllib.error.HTTPError(req.full_url, 503, 'SECRET', {}, None)
            return io.BytesIO(envelope())
        client = geo.JusoClient('a+b/=', 'coord-secret', max_requests=2, opener=opener, sleep=lambda _: None)
        self.assertEqual(client.search(ADDRESS)[0], 1)
        self.assertEqual(requests[-1].full_url, geo.SEARCH_URL)
        self.assertEqual(urllib.parse.parse_qs(requests[-1].data.decode())['confmKey'], ['a+b/='])
        with self.assertRaisesRegex(geo.GeocodingFailure, 'REQUEST_BUDGET_EXHAUSTED'):
            client.search(ADDRESS)
        client = geo.JusoClient('key', 'key', opener=lambda *a, **kw: io.BytesIO(envelope(code='E0001')))
        with self.assertRaisesRegex(geo.GeocodingFailure, 'API_AUTH_ERROR'):
            client.search(ADDRESS)
        self.assertEqual(client.requests, 1)

    def test_cli_keys_permission_gate_and_safe_local_logs(self):
        env = Path(self.tmp.name) / '.env'
        env.write_text('JUSO_SEARCH_KEY=secret-search\nJUSO_COORD_KEY=secret-coord\n', encoding='utf-8')
        output = io.StringIO()
        before = self.store.path.read_bytes()
        with patch.object(geo, 'ROOT', Path(self.tmp.name)), patch.dict(geo.os.environ, {}, clear=True), redirect_stdout(output):
            code = geo.main(['--region-code', '3740000', '--env-file', str(env), '--database', str(self.store.path)])
        self.assertEqual(code, 1)
        self.assertIn('STORAGE_PERMISSION_UNCONFIRMED', output.getvalue())
        self.assertNotIn('secret', output.getvalue())
        self.assertEqual(before, self.store.path.read_bytes())
        logs = list((Path(self.tmp.name) / '.cache/facilities/geocoding-runs').glob('*.json'))
        self.assertEqual(len(logs), 1)
        self.assertNotIn('secret', logs[0].read_text())


if __name__ == '__main__':
    unittest.main()
