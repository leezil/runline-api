from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from facility_storage import (Facility, FacilityStore, ToiletDetails, CctvDetails,
                              address_hash)


class FacilityStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'facilities' / 'test.sqlite3'
        self.store = FacilityStore(self.path)
        self.store.initialize()

    def toilet(self, key='one', **changes):
        return replace(Facility('toilet', 'mois_toilet_v2', 'test-region', key,
                                name='가상 화장실', longitude=127, latitude=37,
                                coordinate_source='source', coordinate_status='valid'), **changes)

    def cctv(self, key='camera', **changes):
        return replace(Facility('cctv', 'mois_cctv', 'test-region', key,
                                longitude=127, latitude=37, coordinate_source='source',
                                coordinate_status='valid',
                                details=CctvDetails('가상 검증 목적', 3, True)), **changes)

    def query(self, **kwargs):
        return self.store.query_bounds(126.9, 36.9, 127.1, 37.1, **kwargs)

    def test_initialize_reopen_and_missing_read(self):
        identity, = self.store.upsert_many([self.toilet()])
        self.store.initialize()
        other = FacilityStore(self.path)
        self.assertEqual(other.get(identity)['name'], '가상 화장실')
        self.assertIsNone(other.get('absent'))
        missing = FacilityStore(Path(self.tmp.name) / 'absent.sqlite3')
        with self.assertRaises(sqlite3.OperationalError):
            missing.get('absent')
        self.assertFalse(missing.path.exists())

    def test_unrelated_and_newer_database_are_not_overwritten(self):
        for version in (0, 1, 2):
            path = Path(self.tmp.name) / f'other-{version}.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE other(value)')
                db.execute('INSERT INTO other VALUES(42)')
                db.execute(f'PRAGMA user_version={version}')
            with self.assertRaises(ValueError):
                FacilityStore(path).initialize()
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT value FROM other').fetchone()[0], 42)

    def test_pending_toilet_is_preserved_but_not_mapped(self):
        record = self.toilet(longitude=None, latitude=None, coordinate_status='pending',
                             coordinate_source='none', address_road='  ',
                             details=ToiletDetails(male_toilet_count=0))
        identity, = self.store.upsert_many([record])
        saved = self.store.get(identity)
        self.assertIsNone(saved['address_road'])
        self.assertEqual(saved['details']['male_toilet_count'], 0)
        self.assertIsNone(saved['details']['female_toilet_count'])
        self.assertEqual(self.query()['count'], 0)

    def test_upsert_retains_id_and_does_not_merge_colocated_facilities(self):
        a, b, c = self.store.upsert_many([self.toilet(), self.toilet('two'),
                                         self.toilet(source_region_code='other-region')])
        self.assertEqual(len({a, b, c}), 3)
        updated, = self.store.upsert_many([self.toilet(name='변경 이름', address_lot='가상 주소')])
        self.assertEqual(updated, a)
        self.assertEqual(self.store.get(a)['name'], '변경 이름')
        self.assertEqual(self.query()['count'], 3)

    def test_index_tracks_moves_status_and_reactivation(self):
        identity, = self.store.upsert_many([self.toilet()])
        for changes in ({'longitude':128}, {'active':False},
                        {'coordinate_status':'ambiguous'},
                        {'longitude':None, 'latitude':None, 'coordinate_source':'none',
                         'coordinate_status':'pending'}):
            self.store.upsert_many([self.toilet(**changes)])
            self.assertEqual(self.query()['count'], 0)
            self.assertIsNotNone(self.store.get(identity))
            self.store.upsert_many([self.toilet()])
            self.assertEqual(self.query()['count'], 1)

    def test_cctv_requires_explicit_confirmation_and_keeps_camera_count(self):
        ids = self.store.upsert_many([self.cctv(),
                                     self.cctv('unknown', details=CctvDetails('다목적', 1, None)),
                                     self.cctv('excluded', details=CctvDetails('가상 교통', 1, False)),
                                     self.toilet()])
        result = self.query(kinds=('cctv',))
        self.assertEqual([item['id'] for item in result['items']], [ids[0]])
        self.assertEqual(result['items'][0]['details']['camera_count'], 3)
        self.store.upsert_many([self.cctv(details=CctvDetails('변경 목적', 3, False))])
        self.assertEqual(self.query(kinds=('cctv',))['count'], 0)

    def test_exact_bounds_remove_rtree_rounding_false_positives(self):
        edge, = self.store.upsert_many([self.toilet(longitude=127.01, latitude=37.01)])
        self.store.upsert_many([self.toilet('outside', longitude=127.01000001, latitude=37.01)])
        result = self.store.query_bounds(127, 37, 127.01, 37.01)
        self.assertEqual([item['id'] for item in result['items']], [edge])

    def test_limit_sorting_and_truncation(self):
        ids = self.store.upsert_many([self.toilet(str(i)) for i in range(5)])
        result = self.query(limit=2)
        self.assertEqual([item['id'] for item in result['items']], sorted(ids)[:2])
        self.assertTrue(result['truncated'])
        self.assertEqual(result['count'], 2)
        self.assertFalse(self.query(limit=5)['truncated'])

    def test_batch_rolls_back_updates_inserts_and_spatial_changes(self):
        identity, = self.store.upsert_many([self.toilet()])
        with self.assertRaises(ValueError):
            self.store.upsert_many([self.toilet(longitude=128), self.toilet('new'),
                                    self.toilet('bad', source_record_id=' ')])
        self.assertEqual(self.store.get(identity)['longitude'], 127)
        self.assertEqual(self.query()['count'], 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM facilities').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM facility_bounds').fetchone()[0], 1)

    def test_input_iterator_failure_rolls_back(self):
        def broken():
            yield self.toilet()
            raise RuntimeError('simulated input failure')
        with self.assertRaises(RuntimeError):
            self.store.upsert_many(broken())
        self.assertEqual(self.query()['count'], 0)

    def test_invalid_inputs_are_rejected_without_writes(self):
        cases = [dict(longitude=float('nan')), dict(latitude=float('inf')),
                 dict(longitude=True), dict(longitude=37, latitude=127),
                 dict(longitude=0, latitude=0), dict(latitude=None),
                 dict(source='wrong'), dict(source_record_id=''), dict(active=1),
                 dict(coordinate_source='none'), dict(coordinate_status='wrong'),
                 dict(fetched_at='2026-11-11T12:00:00'),
                 dict(details=CctvDetails()), dict(details=ToiletDetails(male_toilet_count=-1)),
                 dict(details=ToiletDetails(male_toilet_count=True)),
                 dict(details=ToiletDetails(emergency_bell='Y'))]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.upsert_many([self.toilet(**changes)])
        self.assertEqual(self.query()['count'], 0)

    def test_geocoded_address_change_invalidates_coordinates(self):
        record = self.toilet(address_road='가상 주소', coordinate_source='geocoder',
                             geocoder_provider='test-provider-v1',
                             geocoded_at='2026-11-11T00:00:00Z',
                             geocode_address_hash=address_hash('가상 주소', None))
        identity, = self.store.upsert_many([record])
        self.assertEqual(self.query()['count'], 1)
        self.store.upsert_many([replace(record, address_road='변경 주소')])
        saved = self.store.get(identity)
        self.assertEqual(saved['coordinate_status'], 'pending')
        self.assertIsNone(saved['longitude'])
        self.assertIsNone(saved['geocode_address_hash'])
        self.assertEqual(self.query()['count'], 0)
        self.assertEqual(record.coordinate_status, 'valid')

    def test_query_validation(self):
        for args in [(127,37,126,38), (127,37,128,37), (0,37,128,38),
                     (127,float('nan'),128,38)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.query_bounds(*args)
        for kwargs in ({'limit':0}, {'limit':501}, {'limit':True},
                       {'kinds':()}, {'kinds':('unknown',)}, {'kinds':'toilet'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.query(**kwargs)

    def test_text_is_parameterized_and_empty_batch_is_valid(self):
        record = self.toilet(name="'); DROP TABLE facilities;--")
        identity, = self.store.upsert_many([record])
        self.assertEqual(self.store.get(identity)['name'], record.name)
        self.assertEqual(self.store.upsert_many([]), [])
        self.assertEqual(self.query()['count'], 1)


if __name__ == '__main__':
    unittest.main()
