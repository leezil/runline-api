from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import check_facility_integration as check
from facility_storage import FacilityStore, Facility, CctvDetails


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'source.sqlite3'
        store = FacilityStore(self.path)
        store.initialize()
        store.upsert_many([Facility('cctv','mois_cctv','3740000','one',
                                   longitude=127.01,latitude=37.26,coordinate_source='source',
                                   coordinate_status='valid', details=CctvDetails('생활방범',1,True))])

    def test_offline_check_does_not_call_provider(self):
        before = self.path.read_bytes()
        with patch.object(check.facility_refresh, 'main', side_effect=AssertionError('No live calls')):
            report = check.run(self.path)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['http_checks_total'], 24)
        self.assertEqual(report['http']['bbox_count'], 1)
        self.assertTrue(report['backup_restore_equal'])
        self.assertEqual(before, self.path.read_bytes())

    def test_unexpected_dry_run_write_is_detected_and_isolated(self):
        before = self.path.read_bytes()
        def unexpected_write(args):
            target = Path(args[args.index('--database') + 1])
            self.assertNotEqual(target.resolve(), self.path.resolve())
            self.assertIn('--dry-run', args)
            store = FacilityStore(target)
            identity = store.query_bounds(127,37.25,127.02,37.27)['items'][0]['id']
            store.set_active(identity, False)
            print('{"status":"validated"}')
            return 0
        with patch.object(check.facility_refresh, 'main', side_effect=unexpected_write):
            report = check.run(self.path, live_dry_run=True)
        self.assertEqual(report['status'], 'failed')
        self.assertTrue(report['production_unchanged'])
        self.assertEqual(before, self.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
