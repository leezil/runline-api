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

import cctv_import as cctv
from facility_storage import Facility, FacilityStore


def row(key='one', **changes):
    result = dict(OPN_ATMY_GRP_CD='3740000', MNG_NO=key, INSTL_PRPS_SE_NM='생활방범',
                  WGS84_LOT='127.01', WGS84_LAT='37.27', CAM_CNTOM='3',
                  LCTN_ROAD_NM_ADDR='가상 도로 주소', MNG_INST_NM='가상 기관')
    result.update(changes)
    return result


def page(rows, *, total=None, number=1, size=2):
    return json.dumps({'response': {'header': {'resultCode': '0'}, 'body': {
        'pageNo': number, 'numOfRows': size, 'totalCount': len(rows) if total is None else total,
        'items': {'item': rows}}}}).encode()


class CctvImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'facilities.sqlite3'
        self.store = FacilityStore(self.path)

    def run_import(self, responses, **kwargs):
        iterator = iter(responses)
        def fetch(*args):
            response = next(iterator)
            if isinstance(response, Exception):
                raise response
            return response
        return cctv.import_cctv(self.store, 'fake-key', '3740000', page_size=2, fetch=fetch, **kwargs)

    def mapped(self):
        return self.store.query_bounds(126, 36, 128, 38, kinds=('cctv',))['items']

    def test_pages_stable_ids_and_same_location_distinct_records(self):
        responses = [page([row(), row('two')], total=3), page([row('three')], total=3, number=2)]
        report = self.run_import(responses)
        self.assertEqual((report['saved'], report['map_candidates']), (3, 3))
        before = self.mapped()
        self.assertEqual(len(before), 3)  # 3 installation points, not 9 camera markers.
        self.assertEqual(before[0]['details']['camera_count'], 3)
        self.run_import(responses)
        self.assertEqual([r['id'] for r in before], [r['id'] for r in self.mapped()])

    def test_purpose_classification_exact_tokens_only(self):
        for value in ['생활방범', ' 생활방범 ', '교통단속+생활방범', '생활방범,기타', '교통단속 및 생활방범']:
            self.assertIs(cctv.classify_purpose(value), True)
        for value in ['교통단속', '교통정보수집/시설물관리', '쓰레기단속;재난재해']:
            self.assertIs(cctv.classify_purpose(value), False)
        for value in [None, '', '다목적', '기타', '어린이보호', '방범', '범죄예방',
                      '생활방범아님', '비생활방범', '교통단속+미확인', '01', '생활 방범']:
            self.assertIsNone(cctv.classify_purpose(value))

    def test_unknown_and_other_purposes_saved_but_not_mapped(self):
        report = self.run_import([page([row(INSTL_PRPS_SE_NM='다목적'), row('two', INSTL_PRPS_SE_NM='교통단속')])])
        self.assertEqual((report['unknown_purpose'], report['other_purpose'], report['saved']), (1, 1, 2))
        self.assertEqual(self.mapped(), [])
        with self.store._connect(readonly=True) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM facilities').fetchone()[0], 2)

    def test_bad_coordinates_are_retained_as_invalid_and_not_swapped(self):
        cases = [(None, None), ('', ''), ('127', None), ('37', '127'), ('0', '0'),
                 ('NaN', '37'), ('inf', '37'), (True, '37'), ([], '37'), ('133', '37')]
        for lon, lat in cases:
            raw = row(WGS84_LOT=lon, WGS84_LAT=lat)
            record = cctv.convert_row(raw, '3740000', '2026-11-12T00:00:00Z')
            self.assertEqual(record.coordinate_status, 'invalid')
            self.assertIsNone(record.longitude)
            self.assertIsNone(record.latitude)
            self.run_import([page([raw])])
            self.assertEqual(self.mapped(), [])

    def test_changed_coordinates_and_purpose_remove_old_map_entry(self):
        self.run_import([page([row()])])
        identity = self.mapped()[0]['id']
        self.run_import([page([row(WGS84_LOT='', WGS84_LAT='')])])
        self.assertEqual(self.mapped(), [])
        self.assertIsNone(self.store.get(identity)['longitude'])
        self.run_import([page([row(INSTL_PRPS_SE_NM='교통단속')])])
        self.assertEqual(self.mapped(), [])
        self.run_import([page([row(WGS84_LOT='127.02')])])
        self.assertEqual(self.mapped()[0]['id'], identity)
        self.assertEqual(self.mapped()[0]['longitude'], 127.02)

    def test_preserves_manual_inactive_and_other_facility_kind(self):
        self.store.initialize()
        record = cctv.convert_row(row(), '3740000', '2026-11-12T00:00:00Z')
        identity, toilet = self.store.upsert_many([
            replace(record, active=False), Facility('toilet', 'mois_toilet_v2', '3740000', 'one')])
        self.run_import([page([row()])])
        self.assertFalse(self.store.get(identity)['active'])
        self.assertEqual(self.store.get(toilet)['kind'], 'toilet')
        self.assertEqual(self.mapped(), [])

    def test_failed_later_page_does_not_modify_existing_database(self):
        self.run_import([page([row()])])
        before = self.path.read_bytes()
        with self.assertRaises(cctv.ImportFailure):
            self.run_import([page([row(WGS84_LAT='0'), row('two')], total=3), cctv.ImportFailure('NETWORK_ERROR')])
        self.assertEqual(before, self.path.read_bytes())

    def test_duplicate_region_missing_fields_total_change_and_budget_abort(self):
        missing = row(); del missing['MNG_NO']
        schema = row(); del schema['INSTL_PRPS_SE_NM']
        cases = [([page([row(), row()])], {}), ([page([missing])], {}), ([page([schema])], {}),
                 ([page([row(OPN_ATMY_GRP_CD='3830000')])], {}),
                 ([page([row(), row('two')], total=3)], {'max_pages': 1}),
                 ([page([row(), row('two')], total=3), page([row('three'), row('four')], total=4, number=2)], {})]
        for responses, options in cases:
            with self.assertRaises(cctv.ImportFailure):
                self.run_import(responses, **options)
            self.assertFalse(self.path.exists())

    def test_dry_run_and_empty_listing_do_not_destroy_data(self):
        report = self.run_import([page([row()])], dry_run=True)
        self.assertEqual((report['status'], report['saved']), ('validated', 0))
        self.assertFalse(self.path.exists())
        self.run_import([page([row()])])
        before = self.mapped()
        self.run_import([page([])])
        self.assertEqual(before, self.mapped())

    def test_camera_count_zero_missing_and_invalid(self):
        for raw, expected in [('0', 0), ('', None), (None, None), ('3', 3)]:
            self.assertEqual(cctv.convert_row(row(CAM_CNTOM=raw), '3740000', '2026-11-12T00:00:00Z').details.camera_count, expected)
        for raw in [True, '-1', '2.5', []]:
            with self.assertRaises(cctv.ImportFailure):
                self.run_import([page([row(CAM_CNTOM=raw)])])
            self.assertFalse(self.path.exists())

    def test_cctv_endpoint_encoding_and_retry(self):
        requests, waits = [], []
        def opener(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, 'SECRET', {}, None)
            return io.BytesIO(page([row()]))
        cctv.fetch_page('a+b/=', '3740000', 1, 2, opener=opener, sleep=waits.append)
        parsed = urllib.parse.urlsplit(requests[-1].full_url)
        self.assertEqual(parsed.path, '/1741000/cctv_info/info')
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query['serviceKey'], ['a+b/='])
        self.assertEqual(query['cond[OPN_ATMY_GRP_CD::EQ]'], ['3740000'])
        self.assertEqual(waits, [1])

    def test_cli_denied_access_logs_no_credentials_and_no_db_write(self):
        stream = io.StringIO()
        with patch.object(cctv, 'ROOT', Path(self.tmp.name)), \
             patch.object(cctv, 'load_service_key', return_value='SECRET'), \
             patch.object(cctv, 'fetch_public_data_page', side_effect=cctv.ImportFailure('HTTP_ERROR')), \
             redirect_stdout(stream):
            code = cctv.main(['--region-code', '3740000', '--database', str(self.path)])
        self.assertEqual(code, 1)
        self.assertNotIn('SECRET', stream.getvalue())
        self.assertFalse(self.path.exists())
        logs = list((Path(self.tmp.name) / '.cache/facilities/cctv-import-runs').glob('*.json'))
        self.assertEqual(len(logs), 1)
        self.assertEqual(json.loads(logs[0].read_text())['error'], 'HTTP_ERROR')


if __name__ == '__main__':
    unittest.main()
