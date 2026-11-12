from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import facility_backup as backup
from facility_refresh_job import job_lock, AlreadyRunning
from facility_storage import FacilityStore, Facility


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'facilities.sqlite3'
        self.folder = self.root / 'backups'
        self.store = FacilityStore(self.path)
        self.store.initialize()
        self.record = Facility('toilet', 'mois_toilet_v2', '3740000', 'one', name='original',
                               longitude=127, latitude=37, coordinate_status='valid', coordinate_source='source')
        self.identity, = self.store.upsert_many([self.record], import_state=('mois_toilet_v2','3740000','2026-11-13T00:00:00Z'))

    def run_backup(self, action='backup', **kwargs):
        return backup.run(action, database=self.path, backup_dir=self.folder, **kwargs)

    def test_backup_restore_and_safety_copy_preserve_metadata_and_index(self):
        original = self.store.facility_status()
        saved = Path(self.run_backup()['file'])
        self.store.upsert_many([replace(self.record, name='changed')])
        result = self.run_backup('restore', source=saved)
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(self.store.get(self.identity)['name'], 'original')
        self.assertEqual(FacilityStore(result['safety_backup']).get(self.identity)['name'], 'changed')
        self.assertEqual(self.store.facility_status(), original)
        self.assertEqual(self.store.query_bounds(126,36,128,38)['count'], 1)
        self.store.set_active(self.identity, False)
        self.assertEqual(self.store.query_bounds(126,36,128,38)['count'], 0)
        self.assertFalse(list(self.folder.glob('*.pending.sqlite3')))

    def test_backup_reads_committed_wal_with_live_connection(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("UPDATE facilities SET name='committed-wal'")
            db.commit()
            db.execute("UPDATE facilities SET name='uncommitted'")
            saved = self.run_backup()['file']
            self.assertEqual(FacilityStore(saved).get(self.identity)['name'], 'committed-wal')
            db.rollback()
            result = self.run_backup('restore', source=saved)
            self.assertEqual(result['status'], 'restored')
            self.assertEqual(db.execute('SELECT name FROM facilities').fetchone()[0], 'committed-wal')

    def test_missing_database_restore(self):
        saved = self.run_backup()['file']
        self.path.unlink()
        result = self.run_backup('restore', source=saved)
        self.assertIsNone(result['safety_backup'])
        self.assertEqual(self.store.get(self.identity)['name'], 'original')

    def test_missing_and_invalid_source_never_overwrite_current(self):
        before = self.path.read_bytes()
        bad = self.root / 'bad.sqlite3'
        bad.write_bytes(b'not sqlite')
        with self.assertRaises(sqlite3.Error):
            self.run_backup('restore', source=bad)
        self.assertEqual(before, self.path.read_bytes())
        unrelated = self.root / 'other.sqlite3'
        with closing(sqlite3.connect(unrelated)) as db:
            db.execute('CREATE TABLE unrelated (value TEXT)')
        with self.assertRaises(backup.BackupError):
            self.run_backup('restore', source=unrelated)
        self.assertEqual(before, self.path.read_bytes())
        with self.assertRaises(backup.BackupError):
            self.run_backup('restore', source=self.root / 'absent')
        self.assertEqual(before, self.path.read_bytes())

    def test_refresh_lock_prevents_backup_and_restore(self):
        saved = self.run_backup()['file']
        with job_lock(self.path.parent / (self.path.name + '.refresh-job') / 'run.lock'):
            for action in ('backup', 'restore'):
                with self.assertRaises(AlreadyRunning):
                    self.run_backup(action, source=saved)

    def test_copy_failure_preserves_target_and_safety_backup(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE test_payload (value BLOB)')
            db.execute('INSERT INTO test_payload VALUES (zeroblob(4194304))')
        saved = self.run_backup()['file']
        self.store.upsert_many([replace(self.record, name='current')])
        real_copy = backup.copy_database
        def copy(source, destination, **kwargs):
            if destination == self.path:
                return real_copy(source, destination, timeout=-1)
            return real_copy(source, destination, **kwargs)
        with patch.object(backup, 'copy_database', side_effect=copy), self.assertRaises(backup.BackupError):
            self.run_backup('restore', source=saved)
        self.assertEqual(self.store.get(self.identity)['name'], 'current')
        safety, = self.folder.glob('before-restore-*.sqlite3')
        self.assertEqual(FacilityStore(safety).get(self.identity)['name'], 'current')

    def test_refuse_same_file_or_orphan_sidecars(self):
        with self.assertRaisesRegex(backup.BackupError, 'SOURCE_EQUALS_DATABASE'):
            self.run_backup('restore', source=self.path)
        saved = self.run_backup()['file']
        self.path.unlink()
        Path(str(self.path) + '-wal').write_bytes(b'evidence')
        with self.assertRaisesRegex(backup.BackupError, 'ORPHAN_DATABASE_SIDECAR'):
            self.run_backup('restore', source=saved)
        self.assertFalse(self.path.exists())

    def test_verify_is_readonly_and_cli_errors_are_sanitized(self):
        saved = self.run_backup()['file']
        before = Path(saved).read_bytes()
        self.assertEqual(self.run_backup('verify', source=saved)['status'], 'verified')
        self.assertEqual(before, Path(saved).read_bytes())
        output = io.StringIO()
        with redirect_stdout(output):
            code = backup.main(['verify', '--file', str(self.root / 'private-path')])
        self.assertEqual(code, 1)
        self.assertNotIn('private-path', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['error'], 'BACKUP_FILE_MISSING')


if __name__ == '__main__':
    unittest.main()
