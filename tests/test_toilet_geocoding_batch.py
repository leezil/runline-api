from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from facility_storage import Facility, FacilityStore
from toilet_geocoding import GeocodingFailure, parse_response
from toilet_geocoding_review import inspect_address
import json
from contextlib import nullcontext, redirect_stdout
from unittest.mock import patch
import io
import toilet_geocoding_batch as batch
from toilet_geocoding_batch import plan, run_batch, REGIONS


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FacilityStore(Path(self.tmp.name) / 'facilities.sqlite3')
        self.store.initialize()
        self.record = Facility('toilet', 'mois_toilet_v2', '3740000', 'one',
                               address_road='test')
        self.ids = self.store.upsert_many([self.record, replace(self.record, source_record_id='two')])

    def test_rejected_address_isolated_but_auth_errors_still_stop(self):
        class Client:
            requests = 0
            def search(self, address, **kwargs):
                self.requests += 1
                raw = json.dumps({'results': {'common': {'errorCode': 'E0013'}}}).encode()
                return parse_response(raw)
        report = run_batch(self.store, Client(), lambda *a: (127, 37), regions=('3740000',))
        self.assertEqual(report['status'], 'processed')
        self.assertEqual(report['after']['pending'], 0)
        self.assertEqual(report['outcomes'], {'invalid': 2})
        review, candidate = inspect_address(self.store.get(self.ids[0]), Client())
        self.assertEqual(review['reason'], 'ADDRESS_QUERY_REJECTED')
        self.assertIsNone(candidate)
        with self.assertRaisesRegex(GeocodingFailure, 'API_AUTH_ERROR'):
            parse_response(json.dumps({'results': {'common': {'errorCode': 'E0001'}}}).encode())

    def test_budget_windows_resume_but_provider_failure_stops(self):
        for error, expected_calls, expected_exit in (
                ('REQUEST_BUDGET_EXHAUSTED', 2, 0), ('API_AUTH_ERROR', 1, 1)):
            with self.subTest(error=error):
                with patch.object(batch, 'load_settings', return_value={
                    'JUSO_STORAGE_APPROVED': 'true', 'JUSO_SEARCH_KEY': 'test', 'JUSO_COORD_KEY': 'test'}), \
                    patch.object(batch, 'JusoClient'), \
                    patch.object(batch, 'make_transformer'), \
                    patch.object(batch, 'job_lock', return_value=nullcontext()), \
                    patch.object(batch, 'plan', return_value={'missing_regions': []}), \
                    patch.object(batch, 'snapshot', return_value=Path('backup')) as backup, \
                    patch.object(batch.time, 'sleep'), \
                    patch.object(batch, 'run_batch', side_effect=[
                        {'status': 'stopped', 'error': error}, {'status': 'processed'}]) as run_mock, \
                    redirect_stdout(io.StringIO()):
                    result = batch.main(['--passes', '2'])
                self.assertEqual(result, expected_exit)
                self.assertEqual(run_mock.call_count, expected_calls)
                backup.assert_called_once()

    def test_scope_includes_gunpo_and_all_56_municipalities(self):
        self.assertEqual(len(set(REGIONS)), 56)
        self.assertIn('4020000', REGIONS)
        self.assertEqual(plan(self.store, ('3740000', '4020000'))['missing_regions'], ['4020000'])

    def test_missing_region_never_calls_provider(self):
        class Client:
            requests = 0
            def search(self, address):
                raise AssertionError('Must not call API')
        report = run_batch(self.store, Client(), lambda *a, **k: (127.0, 37.0), regions=('3740000', '4020000'))
        self.assertEqual(report['error'], 'REGION_IMPORT_REQUIRED')
        self.assertEqual(report['saved'], 0)

    def test_budget_stop_preserves_partial_progress_and_resume_skips_it(self):
        class Client:
            requests = 0
            def search(self, address):
                if self.requests == 1:
                    raise GeocodingFailure('REQUEST_BUDGET_EXHAUSTED')
                self.requests += 1
                return 0, []
        report = run_batch(self.store, Client(), lambda *a, **k: (127.0, 37.0), regions=('3740000',), batch_size=1)
        self.assertEqual(report['saved'], 1)
        self.assertEqual(report['after']['pending'], 1)
        self.assertEqual(report['error'], 'REQUEST_BUDGET_EXHAUSTED')
        resumed = run_batch(self.store, Client(), lambda *a, **k: (127.0, 37.0), regions=('3740000',), batch_size=1)
        self.assertEqual(resumed['saved'], 1)
        self.assertEqual(resumed['after']['pending'], 0)
        self.assertEqual(resumed['after']['unresolved'], 2)
        self.assertEqual(resumed['status'], 'processed')

    def test_existing_valid_coordinates_are_unchanged(self):
        row = self.store.get(self.ids[0])
        self.store.save_geocoding(row, status='valid', longitude=127.01, latitude=37.26,
                                  provider='fixture', timestamp='2026-11-25T00:00:00Z')
        before = self.store.get(self.ids[0])
        class Client:
            requests = 0
            def search(self, address):
                self.requests += 1
                return 0, []
        report = run_batch(self.store, Client(), lambda *a, **k: (127.0, 37.0), regions=('3740000',))
        self.assertEqual(report['saved'], 1)
        self.assertEqual(self.store.get(self.ids[0]), before)
        self.assertEqual(report['after']['map_ready'], 1)


if __name__ == '__main__':
    unittest.main()

