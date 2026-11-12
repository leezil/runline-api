from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import facility_refresh as refresh
from facility_storage import Facility, FacilityStore, CctvDetails, address_hash
from toilet_import import ImportFailure

BASE = datetime(2026, 11, 12, tzinfo=timezone.utc)


def raw(key='one', **changes):
    return dict(OPN_ATMY_GRP_CD='3740000', MNG_NO=key, INSTL_PRPS_SE_NM='생활방범',
                WGS84_LOT='127.01', WGS84_LAT='37.26', CAM_CNTOM='3', **changes)


def page(rows, number=1, total=None):
    return json.dumps({'response': {'header': {'resultCode': '0'}, 'body': {
        'pageNo': number, 'numOfRows': 2, 'totalCount': len(rows) if total is None else total,
        'items': {'item': rows}}}}).encode()


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = FacilityStore(Path(self.tmp.name) / 'test.sqlite3')
        self.store.initialize()
        self.camera = Facility('cctv', 'mois_cctv', '3740000', 'one', name='방범 CCTV',
                               longitude=127.01, latitude=37.26, coordinate_source='source',
                               coordinate_status='valid', details=CctvDetails('생활방범', 3, True))
        self.one, self.two = self.store.upsert_many([self.camera, replace(self.camera, source_record_id='two')])

    def apply(self, records, hours=0, **kwargs):
        baseline = self.store.refresh_snapshot('mois_cctv', '3740000')['fingerprint']
        return self.store.apply_refresh(records, 'mois_cctv', '3740000',
                                        (BASE + timedelta(hours=hours)).isoformat(), baseline, **kwargs)

    def snapshot(self):
        return self.store.refresh_snapshot('mois_cctv', '3740000')

    def run_refresh(self, responses, **kwargs):
        iterator = iter(responses)
        def fetch(*args):
            response = next(iterator)
            if isinstance(response, Exception):
                raise response
            return response
        return refresh.refresh_one(self.store, 'fake-key', 'cctv', '3740000',
                                    page_size=2, fetch=fetch, clock=lambda: BASE.isoformat(), **kwargs)

    def test_update_insert_stable_ids_and_scoped_checkpoint(self):
        other, = self.store.upsert_many([replace(self.camera, source_region_code='3830000')])
        self.apply([replace(self.camera, longitude=127.02), replace(self.camera, source_record_id='two'),
                    replace(self.camera, source_record_id='three')])
        self.assertEqual(self.store.get(self.one)['longitude'], 127.02)
        self.assertEqual(self.store.get(other)['longitude'], 127.01)
        self.assertEqual(self.snapshot()['checkpoint']['generation'], 1)
        self.assertIsNone(self.store.refresh_snapshot('mois_cctv', '3830000')['checkpoint'])

    def test_missing_requires_two_successes_and_24_hours(self):
        self.apply([self.camera], max_missing_ratio=1)
        self.assertTrue(self.store.get(self.two)['active'])
        self.apply([self.camera], hours=1, max_missing_ratio=1)
        self.assertTrue(self.store.get(self.two)['active'])
        result = self.apply([self.camera], hours=24, max_missing_ratio=1)
        self.assertEqual(result['deactivated'], 1)
        self.assertFalse(self.store.get(self.two)['active'])
        self.assertIsNotNone(self.store.get(self.two)['longitude'])
        self.assertEqual(self.store.query_bounds(127,37.25,127.02,37.27)['count'], 1)

    def test_reappearance_reactivates_only_automatically_hidden(self):
        manual, = self.store.upsert_many([replace(self.camera, source_record_id='manual', active=False)])
        self.apply([self.camera], max_missing_ratio=1)
        self.apply([self.camera], hours=24, max_missing_ratio=1)
        result = self.apply([self.camera, replace(self.camera, source_record_id='two'),
                             replace(self.camera, source_record_id='manual')], hours=48)
        self.assertEqual(result['reactivated'], 1)
        self.assertTrue(self.store.get(self.two)['active'])
        self.assertFalse(self.store.get(manual)['active'])
        self.assertEqual(self.snapshot()['presence'][self.two]['missing_count'], 0)

    def test_explicit_review_override_prevents_reactivation(self):
        self.apply([self.camera], max_missing_ratio=1)
        self.apply([self.camera], hours=24, max_missing_ratio=1)
        self.store.set_active(self.two, False)
        self.apply([self.camera, replace(self.camera, source_record_id='two')], hours=48)
        self.assertFalse(self.store.get(self.two)['active'])

    def test_empty_and_large_disappearance_guards_do_not_write(self):
        before = self.store.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'EMPTY_SNAPSHOT_BLOCKED'):
            self.apply([])
        with self.assertRaisesRegex(ValueError, 'EXCESSIVE_MISSING_BLOCKED'):
            self.apply([self.camera])
        self.assertEqual(before, self.store.path.read_bytes())
        self.apply([], allow_empty=True, max_missing_ratio=1)
        self.apply([], hours=24, allow_empty=True, max_missing_ratio=1)
        self.assertFalse(self.store.get(self.one)['active'])

    def test_known_missing_does_not_trigger_future_drop_guard_forever(self):
        self.apply([self.camera], max_missing_ratio=1)
        result = self.apply([self.camera], hours=24)  # No new disappearance.
        self.assertEqual(result['new_missing'], 0)
        self.assertEqual(result['deactivated'], 1)

    def test_dry_run_no_state_or_database_mutation(self):
        before = self.store.path.read_bytes()
        result = self.apply([self.camera], max_missing_ratio=1, dry_run=True)
        self.assertEqual(result['missing'], 1)
        self.assertEqual(before, self.store.path.read_bytes())
        with self.assertRaises(ImportFailure):
            self.run_refresh([ImportFailure('NETWORK_ERROR')], dry_run=True)
        self.assertEqual(before, self.store.path.read_bytes())

    def test_failure_preserves_rows_presence_checkpoint_and_success_time(self):
        self.apply([self.camera], max_missing_ratio=1)
        before = self.snapshot()
        with self.assertRaises(ImportFailure):
            self.run_refresh([page([raw(),raw('two')],total=3),ImportFailure('NETWORK_ERROR')])
        after = self.snapshot()
        for key in ('rows', 'presence', 'checkpoint'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(before['import_state'][0]['last_success_at'], after['import_state'][0]['last_success_at'])
        self.assertEqual(after['import_state'][0]['last_attempt_status'], 'failed')

    def test_failed_run_never_overwrites_a_newer_success(self):
        def fetch(*args):
            self.apply([self.camera,replace(self.camera,source_record_id='two')],hours=1)
            raise ImportFailure('NETWORK_ERROR')
        with self.assertRaises(ImportFailure):
            refresh.refresh_one(self.store,'fake-key','cctv','3740000',fetch=fetch)
        self.assertEqual(self.snapshot()['import_state'][0]['last_attempt_status'],'success')

    def test_concurrent_local_edit_blocks_stale_snapshot(self):
        baseline = self.snapshot()['fingerprint']
        self.store.set_active(self.one, False)
        with self.assertRaisesRegex(ValueError, 'REFRESH_BASELINE_CHANGED'):
            self.store.apply_refresh([self.camera], 'mois_cctv','3740000',BASE.isoformat(),baseline,max_missing_ratio=1)
        self.assertFalse(self.store.get(self.one)['active'])
        self.assertIsNone(self.snapshot()['checkpoint'])

    def test_rollback_includes_rows_presence_and_checkpoint(self):
        before = self.store.path.read_bytes()
        with patch.object(self.store, '_write_import_state', side_effect=ValueError('injected')), self.assertRaises(ValueError):
            self.apply([replace(self.camera, longitude=127.03),replace(self.camera,source_record_id='two')])
        self.assertEqual(before,self.store.path.read_bytes())

    def test_complete_page_validation_before_reconciliation(self):
        bad = raw(); del bad['MNG_NO']
        cases = [[page([raw(),raw()])], [page([bad])],
                 [page([raw(),raw('two')],total=3),page([raw('three'),raw('four')],number=2,total=4)]]
        for responses in cases:
            before = self.snapshot()
            with self.assertRaises(ImportFailure):
                self.run_refresh(responses)
            after = self.snapshot()
            self.assertEqual(before['rows'],after['rows'])
            self.assertEqual(before['checkpoint'],after['checkpoint'])
        with self.assertRaisesRegex(ImportFailure,'PAGE_BUDGET_EXCEEDED'):
            self.run_refresh([page([raw(),raw('two')],total=3)],max_pages=1)

    def test_toilet_address_change_clears_geocoding_and_same_address_preserves(self):
        toilet = Facility('toilet','mois_toilet_v2','3740000','toilet',address_road='old',
                           longitude=127,latitude=37,coordinate_status='valid',coordinate_source='geocoder',
                           geocoder_provider='test',geocoded_at=BASE.isoformat(),geocode_address_hash=address_hash('old',None))
        identity, = self.store.upsert_many([toilet])
        incoming = Facility('toilet','mois_toilet_v2','3740000','toilet',address_road='old')
        for address in ('old','new'):
            snapshot = self.store.refresh_snapshot('mois_toilet_v2','3740000')
            self.store.apply_refresh([replace(incoming,address_road=address)],'mois_toilet_v2','3740000',BASE.isoformat(),snapshot['fingerprint'])
            self.assertEqual(self.store.get(identity)['coordinate_status'],'valid' if address=='old' else 'pending')

    def test_successful_network_refresh_and_checkpoint(self):
        result = self.run_refresh([page([raw(),raw('two')])])
        self.assertEqual((result['status'],result['generation'],result['refreshed']),('success',1,2))
        self.assertEqual(self.snapshot()['checkpoint']['generation'],1)

    def test_clock_cannot_move_checkpoint_backwards(self):
        self.apply([self.camera,replace(self.camera,source_record_id='two')],hours=24)
        with self.assertRaisesRegex(ValueError,'REFRESH_CLOCK_REVERSED'):
            self.apply([self.camera,replace(self.camera,source_record_id='two')])

    def test_cli_partial_failure_logs_safe_codes_and_continues_other_scope(self):
        output = io.StringIO()
        def invoke(store,key,kind,region,**kwargs):
            report=kwargs['report'];report.update(kind=kind,region_code=region,status='success')
            if kind=='toilet':
                report.update(status='failed',error='HTTP_ERROR')
                raise ImportFailure('HTTP_ERROR')
        with patch.object(refresh,'ROOT',Path(self.tmp.name)),patch.object(refresh,'load_service_key',return_value='SECRET'),patch.object(refresh,'refresh_one',side_effect=invoke),redirect_stdout(output):
            code=refresh.main(['--region-code','3740000','--database',str(self.store.path)])
        self.assertEqual(code,1)
        report=json.loads(output.getvalue())
        self.assertEqual(report['status'],'partial')
        self.assertEqual(len(report['scopes']),2)
        self.assertNotIn('SECRET',output.getvalue())


if __name__ == '__main__':
    unittest.main()
