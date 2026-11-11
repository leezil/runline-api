from contextlib import closing
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import toilet_import

from facility_storage import FacilityStore, address_hash
from toilet_import import (ImportFailure, convert_row, fetch_page, import_toilets,
                           load_service_key, parse_page, MAX_RESPONSE_BYTES)


def row(key='1', **changes):
    return dict(OPN_ATMY_GRP_CD='test-region', MNG_NO=key, RSTRM_NM='가상 화장실',
                LCTN_ROAD_NM_ADDR='가상 주소', MALE_TOILT_CNT='0', **changes)


def page(items, total=None, number=1, size=2, code='00'):
    return json.dumps({'response': {'header': {'resultCode': code}, 'body': {
        'totalCount': len(items) if total is None else total, 'pageNo': number,
        'numOfRows': size, 'items': {'item': items}}}}).encode()


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'facilities.sqlite3'
        self.store = FacilityStore(self.path)

    def run_import(self, responses, **kwargs):
        iterator = iter(responses)
        def fetch(*args):
            result = next(iterator)
            if isinstance(result, Exception):
                raise result
            return result
        return import_toilets(self.store, 'fake-key', 'test-region', page_size=2,
                              fetch=fetch, **kwargs)

    def all_rows(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute('SELECT id, name, longitude, active FROM facilities ORDER BY source_record_id').fetchall()

    def test_multiple_pages_pending_and_rerun_keeps_ids(self):
        responses = [page([row('1'), row('2')], total=3), page([row('3')], total=3, number=2)]
        report = self.run_import(responses)
        self.assertEqual((report['pages'], report['saved']), (2, 3))
        before = self.all_rows()
        self.assertTrue(all(item[2] is None for item in before))
        changed = row('1'); changed['RSTRM_NM'] = '변경 이름'
        self.run_import([page([changed, row('2')], total=3), page([row('3')], total=3, number=2)])
        after = self.all_rows()
        self.assertEqual([r[0] for r in before], [r[0] for r in after])
        self.assertEqual(after[0][1], '변경 이름')
        self.assertEqual(self.store.query_bounds(126, 36, 128, 38)['count'], 0)

    def test_failed_second_page_does_not_change_existing_data(self):
        self.run_import([page([row()])])
        before = self.all_rows()
        updated = row(); updated['RSTRM_NM'] = '저장되면 안 됨'
        with self.assertRaises(ImportFailure):
            self.run_import([page([updated, row('2')], total=3), ImportFailure('NETWORK_ERROR')])
        self.assertEqual(self.all_rows(), before)

    def test_bad_rows_duplicates_total_changes_and_page_budget_abort(self):
        bad = row(); bad.pop('MNG_NO')
        wrong = row(); wrong['OPN_ATMY_GRP_CD'] = 'other-region'
        cases = [([page([bad])], {}), ([page([wrong])], {}),
                 ([page([row(), row()])], {}),
                 ([page([row(), row('2')], total=3), page([row('3'), row('4')], total=4, number=2)], {}),
                 ([page([row(), row('2')], total=3)], {'max_pages':1}),
                 ([page([row()], total=3)], {})]
        for responses, options in cases:
            with self.subTest(options=options), self.assertRaises(ImportFailure):
                self.run_import(responses, **options)
            self.assertFalse(self.path.exists())

    def test_dry_run_and_zero_rows_do_not_delete_data(self):
        report = self.run_import([page([row()])], dry_run=True)
        self.assertEqual(report['status'], 'validated')
        self.assertFalse(self.path.exists())
        self.run_import([page([row()])])
        before = self.all_rows()
        report = self.run_import([page([])])
        self.assertEqual(report['saved'], 0)
        self.assertEqual(self.all_rows(), before)

    def test_conversion_zero_unknown_flags_and_missing_address(self):
        record = convert_row(row(EMRGNCBLL_INSTL_YN='unknown'), 'test-region', '2026-11-11T00:00:00Z')
        self.assertEqual(record.details.male_toilet_count, 0)
        self.assertIsNone(record.details.female_toilet_count)
        self.assertIsNone(record.details.emergency_bell)
        for value in ('-1', '1.5', True, [], 'not-a-number'):
            raw = row(); raw['MALE_TOILT_CNT'] = value
            with self.assertRaises(ImportFailure):
                convert_row(raw, 'test-region', record.fetched_at)
        raw = row(); raw.pop('LCTN_ROAD_NM_ADDR')
        report = self.run_import([page([raw])])
        self.assertEqual(report['missing_address'], 1)

    def test_preserve_local_coordinates_and_inactive_state(self):
        self.store.initialize()
        record = convert_row(row(), 'test-region', '2026-11-11T00:00:00Z')
        record = replace(record, active=False, longitude=127, latitude=37,
                         coordinate_status='valid', coordinate_source='geocoder',
                         geocoder_provider='test-v1', geocoded_at=record.fetched_at,
                         geocode_address_hash=address_hash(record.address_road, None))
        identity, = self.store.upsert_many([record])
        self.run_import([page([row()])])
        saved = self.store.get(identity)
        self.assertEqual(saved['longitude'], 127)
        self.assertFalse(saved['active'])
        changed = row(); changed['LCTN_ROAD_NM_ADDR'] = '변경 주소'
        self.run_import([page([changed])])
        saved = self.store.get(identity)
        self.assertIsNone(saved['longitude'])
        self.assertFalse(saved['active'])

    def test_storage_failure_records_fixed_error_and_rolls_back(self):
        self.run_import([page([row()])])
        before = self.all_rows()
        report = {}
        with patch.object(self.store, 'upsert_many', side_effect=sqlite3.OperationalError('private path')):
            with self.assertRaisesRegex(ImportFailure, '^STORAGE_ERROR$'):
                self.run_import([page([row('new')])], report=report)
        self.assertEqual(report['status'], 'failed')
        self.assertNotIn('private', json.dumps(report))
        self.assertEqual(self.all_rows(), before)


class TransportTests(unittest.TestCase):
    def test_decoded_key_is_encoded_exactly_once(self):
        seen = []
        def opener(request, **kwargs):
            seen.append(request.full_url)
            return io.BytesIO(page([]))
        fetch_page('fake+/=key', 'test-region', 1, 100, opener=opener)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen[0]).query)
        self.assertEqual(query['serviceKey'], ['fake+/=key'])
        self.assertEqual(query['cond[OPN_ATMY_GRP_CD::EQ]'], ['test-region'])

    def test_transient_errors_retry_but_auth_does_not_leak_key(self):
        waits, calls = [], []
        def transient(request, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise urllib.error.HTTPError('https://invalid/?serviceKey=secret', 503, 'secret', {}, None)
            return io.BytesIO(page([]))
        fetch_page('secret', 'test-region', 1, 100, opener=transient, sleep=waits.append)
        self.assertEqual(waits, [1, 2])
        def forbidden(*args, **kwargs):
            raise urllib.error.HTTPError('https://invalid/?serviceKey=secret', 403, 'secret', {}, None)
        with self.assertRaisesRegex(ImportFailure, '^HTTP_ERROR$'):
            fetch_page('secret', 'test-region', 1, 100, opener=forbidden,
                       sleep=lambda _: self.fail('auth must not retry'))

    def test_response_size_limit(self):
        with self.assertRaisesRegex(ImportFailure, 'RESPONSE_TOO_LARGE'):
            fetch_page('fake', 'test-region', 1, 100,
                       opener=lambda *a, **k: io.BytesIO(b'x' * (MAX_RESPONSE_BYTES + 1)))

    def test_envelope_errors_singleton_and_empty(self):
        self.assertEqual(parse_page(page([], code='0'), 1, 2), (0, []))
        raw = json.loads(page([row()])); raw['response']['body']['items']['item'] = row()
        self.assertEqual(len(parse_page(json.dumps(raw).encode(), 1, 2)[1]), 1)
        raw['response']['body'].update(totalCount=0, items='')
        self.assertEqual(parse_page(json.dumps(raw).encode(), 1, 2), (0, []))
        for raw in (b'<error><message>secret</message></error>', b'not json', b'{}',
                    page([], code='30'), page([row()], number=2), page([row()], size=100)):
            with self.subTest(raw=raw), self.assertRaises(ImportFailure):
                parse_page(raw, 1, 2)

    def test_key_loading_env_priority_and_empty_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / '.env'
            env.write_text('# test only\nPUBLIC_DATA_SERVICE_KEY="file-fake"\n', encoding='utf-8')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_service_key(env), 'file-fake')
            with patch.dict(os.environ, {'PUBLIC_DATA_SERVICE_KEY':'env-fake'}, clear=True):
                self.assertEqual(load_service_key(env), 'env-fake')
            with patch.dict(os.environ, {'PUBLIC_DATA_SERVICE_KEY':''}, clear=True):
                with self.assertRaises(ImportFailure):
                    load_service_key(env)

    def test_cli_missing_key_logs_no_secret_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with patch.object(toilet_import, 'ROOT', root), patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
                status = toilet_import.main(['--region-code', 'test-region',
                                             '--env-file', str(root/'absent.env'),
                                             '--database', str(root/'unused.sqlite3')])
            self.assertEqual(status, 1)
            logs = list((root/'.cache/facilities/import-runs').glob('*.json'))
            self.assertEqual(len(logs), 1)
            self.assertEqual(json.loads(logs[0].read_text())['error'], 'MISSING_SERVICE_KEY')
            self.assertFalse((root/'unused.sqlite3').exists())

    def test_cli_dry_run_log_and_output_do_not_contain_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = io.StringIO()
            with patch.object(toilet_import, 'ROOT', root), patch.dict(os.environ, {'PUBLIC_DATA_SERVICE_KEY':'secret-example'}, clear=True), \
                    patch('urllib.request.OpenerDirector.open', return_value=io.BytesIO(page([], size=100))), redirect_stdout(output):
                status = toilet_import.main(['--region-code', 'test-region', '--dry-run',
                                             '--database', str(root/'unused.sqlite3')])
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'validated')
            log = next((root/'.cache/facilities/import-runs').glob('*.json')).read_text()
            self.assertNotIn('secret-example', output.getvalue() + log)
            self.assertFalse((root/'unused.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
