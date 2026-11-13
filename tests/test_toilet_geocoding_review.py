import json
from pathlib import Path
import tempfile
import unittest
from dataclasses import replace

from facility_storage import Facility, FacilityStore
from toilet_geocoding import GeocodingFailure
import toilet_geocoding_review as review
from test_toilet_geocoding import ADDRESS, CANDIDATE, COORD, transform


class Client:
    def __init__(self, total=1, rows=None):
        self.total, self.rows = total, [CANDIDATE.copy()] if rows is None else rows
        self.requests = 0
        self.coordinate_calls = 0

    def search(self, address, *, count):
        self.requests += 1
        return self.total, self.rows

    def coordinates(self, candidate):
        self.requests += 1; self.coordinate_calls += 1
        return 1, [COORD.copy()]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = FacilityStore(self.root / 'db.sqlite3'); self.store.initialize()
        self.facility = Facility('toilet', 'mois_toilet_v2', '3740000', 'one',
                                 name='<script>alert(1)</script>', address_road=ADDRESS)
        self.identity, = self.store.upsert_many([self.facility])
        self.store.save_geocoding(self.store.get(self.identity), status='ambiguous', provider='old', timestamp='2026-11-13T00:00:00Z')

    def test_unique_exact_can_be_second_candidate(self):
        client = Client(2, [dict(CANDIDATE, roadAddrPart1=ADDRESS+'-1'), CANDIDATE])
        entry, candidate = review.inspect_address(self.store.get(self.identity), client)
        self.assertEqual(entry['reason'], 'EXACT_MATCH')
        self.assertEqual(candidate, CANDIDATE)
        self.assertEqual([c['exact'] for c in entry['searches'][0]['candidates']], [False, True])

    def test_partial_multiple_and_mismatched_candidates_never_save(self):
        cases = [(101, [CANDIDATE], 'INCOMPLETE_CANDIDATES'),
                 (2, [CANDIDATE, CANDIDATE], 'MULTIPLE_EXACT_MATCHES'),
                 (1, [dict(CANDIDATE, roadAddrPart1=ADDRESS+'-1')], 'ADDRESS_MISMATCH'),
                 (0, [], 'ADDRESS_NOT_FOUND')]
        for i, (total, rows, reason) in enumerate(cases):
            client = Client(total, rows)
            result = review.run_review(self.store, ['3740000'], client, apply=True,
                        transform=transform, output=self.root / str(i))
            self.assertEqual(result['reasons'], {reason: 1})
            self.assertEqual(result['saved'], 0); self.assertEqual(client.coordinate_calls, 0)
        self.assertEqual(self.store.get(self.identity)['coordinate_status'], 'ambiguous')

    def test_readonly_report_safe_html_and_apply_preserves_original(self):
        before = self.store.get(self.identity)
        client = Client()
        result = review.run_review(self.store, ['3740000'], client, output=self.root / 'read')
        self.assertEqual(result['saved'], 0); self.assertEqual(client.coordinate_calls, 0)
        self.assertEqual(self.store.get(self.identity), before)
        page = (self.root / 'read/index.html').read_text(encoding='utf-8')
        self.assertIn('&lt;script&gt;', page); self.assertNotIn('<script>alert(1)', page)
        result = review.run_review(self.store, ['3740000'], Client(), apply=True,
                                  transform=transform, output=self.root / 'apply')
        self.assertEqual(result['saved'], 1)
        self.assertEqual(self.store.get(self.identity)['address_road'], before['address_road'])
        self.assertEqual(self.store.query_bounds(127, 37, 128, 39)['count'], 1)
        self.assertEqual(review.run_review(self.store, ['3740000'], Client(), apply=True,
                          transform=transform, output=self.root / 'again')['selected'], 0)

    def test_coordinate_validation_and_stale_write_protection(self):
        client = Client()
        client.coordinates = lambda _: (2, [COORD, COORD])
        result = review.run_review(self.store, ['3740000'], client, apply=True,
                                   transform=transform, output=self.root / 'multiple')
        self.assertEqual(result['reasons'], {'MULTIPLE_ENTRANCES': 1})
        client = Client()
        def changed(_):
            self.store.upsert_many([replace(self.facility, address_road=ADDRESS+'-2')])
            return 1, [COORD]
        client.coordinates = changed
        result = review.run_review(self.store, ['3740000'], client, apply=True,
                                  transform=transform, output=self.root / 'stale')
        self.assertEqual(result['reasons'], {'STALE': 1})
        self.assertIsNone(self.store.get(self.identity)['longitude'])

    def test_failure_report_no_provider_error_text(self):
        client = Client()
        def failed(*args, **kwargs):
            raise GeocodingFailure('API_AUTH_ERROR')
        client.search = failed
        result = review.run_review(self.store, ['3740000'], client, output=self.root / 'error')
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error'], 'API_AUTH_ERROR')
        self.assertTrue((self.root / 'error/index.html').is_file())
        self.assertEqual(json.loads((self.root / 'error/summary.json').read_text())['processed'], 0)

    def test_partial_failure_preserves_saved_row_and_checkpoint(self):
        other, = self.store.upsert_many([replace(self.facility, source_record_id='two')])
        self.store.save_geocoding(self.store.get(other), status='failed', provider='old',
                                  timestamp='2026-11-13T00:00:00Z')
        client = Client()
        def search(address, *, count):
            client.requests += 1
            if client.requests > 2:
                raise GeocodingFailure('REQUEST_BUDGET_EXHAUSTED')
            return 1, [CANDIDATE]
        client.search = search
        result = review.run_review(self.store, ['3740000'], client, apply=True,
                                   transform=transform, output=self.root / 'partial')
        self.assertEqual((result['status'], result['processed'], result['saved']), ('failed', 1, 1))
        self.assertEqual(len((self.root / 'partial/entries.jsonl').read_text(encoding='utf-8').splitlines()), 1)
        self.assertEqual(sum(self.store.get(i)['coordinate_status'] == 'valid' for i in [other,self.identity]), 1)


if __name__ == '__main__':
    unittest.main()
