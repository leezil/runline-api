from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
from pathlib import Path
import tempfile
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

import facility_refresh_job as job
import facility_refresh_schedule as schedule


class JobTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'facilities.sqlite3'
        self.db.write_bytes(b'unchanged')
        self.calls = []

    def refresh(self, args):
        self.calls.append(args)
        print(json.dumps({'status': 'validated' if '--dry-run' in args else 'success'}))
        return 0

    def run_job(self, **kwargs):
        return job.run_job(database=self.db, root=self.root, refresh_main=self.refresh, **kwargs)

    def test_daily_claim_and_manual_retry(self):
        self.assertEqual(self.run_job(scheduled_date='2026-11-13')[0], 0)
        self.assertEqual(self.run_job(scheduled_date='2026-11-13')[1]['status'], 'skipped_already_scheduled')
        self.assertEqual(self.run_job()[1]['status'], 'success')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.run_job(scheduled_date='2026-11-14')[0], 0)
        self.assertEqual(len(self.calls), 3)

    def test_dry_run_scope_and_absolute_paths(self):
        self.assertEqual(self.run_job(dry_run=True)[1]['status'], 'validated')
        self.assertEqual(self.calls[0], ['--region-code', '3740000', '--region-code', '3830000',
                         '--database', str(self.db), '--env-file', str(self.root / '.env'), '--dry-run'])
        self.assertEqual(self.db.read_bytes(), b'unchanged')

    def test_lock_overlap_and_release_after_exception(self):
        lock = self.db.parent / (self.db.name + '.refresh-job') / 'run.lock'
        with self.assertRaises(RuntimeError):
            with job.job_lock(lock):
                self.assertEqual(self.run_job()[1]['status'], 'skipped_overlap')
                raise RuntimeError('simulated crash')
        self.assertEqual(self.run_job()[1]['status'], 'success')
        self.assertEqual(len(self.calls), 1)

    def test_missing_database(self):
        self.db.unlink()
        code, report = self.run_job()
        self.assertEqual(code, 1)
        self.assertEqual(report['error'], 'FACILITY_DB_MISSING')
        self.assertFalse(self.db.exists())
        self.assertFalse(self.calls)

    def test_failure_is_sanitized_and_manual_retry_allowed(self):
        with patch.object(self, 'refresh', side_effect=RuntimeError('SECRET_VALUE')):
            code, report = self.run_job(scheduled_date='2026-11-13')
        self.assertEqual(code, 1)
        self.assertEqual(report['error'], 'JOB_EXECUTION_ERROR')
        for file in self.root.rglob('*.json'):
            self.assertNotIn('SECRET_VALUE', file.read_text(encoding='utf-8'))
        self.assertEqual(self.run_job(scheduled_date='2026-11-13')[1]['status'], 'skipped_already_scheduled')
        self.assertEqual(self.run_job()[0], 0)

    def test_partial_exit_and_log(self):
        def partial(args):
            print('{"status":"partial"}')
            return 1
        with patch.object(self, 'refresh', side_effect=partial):
            code, report = self.run_job()
        self.assertEqual((code, report['status']), (1, 'partial'))
        logs = list((self.root / '.cache/facilities/job-runs').glob('*.json'))
        self.assertEqual(json.loads(logs[0].read_text())['status'], 'partial')

    def test_invalid_scheduled_invocations(self):
        for kwargs in ({'scheduled_date': '../bad'}, {'scheduled_date': '2026-11-13', 'dry_run': True}):
            self.assertEqual(self.run_job(**kwargs)[0], 1)
        self.assertFalse(self.calls)

    def test_recovery_is_scoped_and_preserves_completed_logs(self):
        folder = self.root / '.cache/facilities/job-runs'
        old = folder / 'old.json'
        identity = job.database_id(self.db)
        job.write_json(old, {'status':'running', 'database_id':identity})
        other = folder / 'other.json'
        job.write_json(other, {'status':'running', 'database_id':'other-database'})
        legacy = folder / 'legacy.json'
        job.write_json(legacy, {'status':'running'})
        completed = folder / 'completed.json'
        job.write_json(completed, {'status':'success', 'database_id':identity})
        detail = self.root / '.cache/facilities/refresh-runs/detail.json'
        job.write_json(detail, {'status':'running', 'job_id':'old',
                               'scopes':[{'status':'success'}, {'status':'running'}]})
        before = self.db.read_bytes()
        code, report = self.run_job(recover_only=True)
        self.assertEqual((code, report['status'], report['recovered_records']), (0, 'recovered', 2))
        self.assertEqual(json.loads(old.read_text())['status'], 'interrupted')
        self.assertEqual(json.loads(detail.read_text())['scopes'],
                         [{'status':'success'}, {'status':'interrupted','error':'PREVIOUS_JOB_INTERRUPTED'}])
        for path, expected in [(other,'running'), (legacy,'running'), (completed,'success')]:
            self.assertEqual(json.loads(path.read_text())['status'], expected)
        self.assertEqual(self.run_job(recover_only=True)[1]['recovered_records'], 0)
        self.assertFalse(self.calls)
        self.assertEqual(before, self.db.read_bytes())

    def test_recovery_never_marks_live_job_interrupted(self):
        old = self.root / '.cache/facilities/job-runs/live.json'
        job.write_json(old, {'status':'running', 'database_id':job.database_id(self.db)})
        lock = self.db.parent / (self.db.name + '.refresh-job') / 'run.lock'
        with job.job_lock(lock):
            self.assertEqual(self.run_job(recover_only=True)[1]['status'], 'skipped_overlap')
            self.assertEqual(json.loads(old.read_text())['status'], 'running')

    def test_keyboard_interrupt_and_exception_finalize_claim(self):
        for index, exception in enumerate([KeyboardInterrupt(), RuntimeError('SECRET')]):
            day = f'2026-11-{13 + index}'
            with patch.object(self, 'refresh', side_effect=exception):
                code, report = self.run_job(scheduled_date=day)
            self.assertEqual(code, 1)
            self.assertIn(report['status'], {'interrupted', 'failed'})
            claim = self.db.parent / (self.db.name + '.refresh-job') / f'scheduled-{day}.json'
            self.assertEqual(json.loads(claim.read_text())['status'], report['status'])
        self.assertEqual(self.run_job()[0], 0)

    def test_real_refresh_interrupt_links_detail_log_for_recovery(self):
        with patch.object(job.facility_refresh, 'ROOT', self.root), \
                patch.object(job.facility_refresh, 'load_service_key', return_value='fake-key'), \
                patch.object(job.facility_refresh, 'refresh_one', side_effect=KeyboardInterrupt()):
            code, report = job.run_job(root=self.root, database=self.db)
        self.assertEqual((code, report['status']), (1, 'interrupted'))
        detail = next((self.root / '.cache/facilities/refresh-runs').glob('*.json'))
        self.assertEqual(json.loads(detail.read_text())['status'], 'running')
        self.run_job(recover_only=True)
        self.assertEqual(json.loads(detail.read_text())['status'], 'interrupted')

    def test_final_report_is_written_before_lock_release(self):
        real_write = job.write_json
        lock = self.db.parent / (self.db.name + '.refresh-job') / 'run.lock'
        checked = []
        def write(path, value):
            if path.parent.name == 'job-runs' and value['status'] == 'success':
                with self.assertRaises(job.AlreadyRunning):
                    with job.job_lock(lock):
                        pass
                checked.append(True)
            real_write(path, value)
        with patch.object(job, 'write_json', side_effect=write):
            self.assertEqual(self.run_job()[0], 0)
        self.assertEqual(checked, [True])

    def test_process_kill_releases_lock_rolls_back_and_allows_manual_retry(self):
        self.db.unlink()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE evidence (value TEXT)')
            db.execute("INSERT INTO evidence VALUES ('committed')")
        ready = self.root / 'ready'
        script = '''
import sqlite3, sys, time
from pathlib import Path
from facility_refresh_job import run_job
root, database, ready = map(Path, sys.argv[1:])
def interrupted(args):
    db = sqlite3.connect(database)
    db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE evidence SET value='uncommitted'")
    ready.write_text('ready')
    time.sleep(30)
run_job(root=root, database=database, scheduled_date='2026-11-13', refresh_main=interrupted)
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(self.root), str(self.db), str(ready)],
                                 cwd=job.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), 'Child did not reach open transaction')
            self.assertEqual(self.run_job(recover_only=True)[1]['status'], 'skipped_overlap')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.assertEqual(self.run_job(recover_only=True)[1]['recovered_records'], 2)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute('SELECT value FROM evidence').fetchone()[0], 'committed')
        self.assertEqual(self.run_job(scheduled_date='2026-11-13')[1]['status'], 'skipped_already_scheduled')
        self.assertEqual(self.run_job()[0], 0)


class ScheduleTests(unittest.TestCase):
    def test_kst_boundary(self):
        now = datetime(2026, 11, 12, 14, 59, tzinfo=timezone.utc)
        target = schedule.next_run(now)
        self.assertEqual(target.isoformat(), '2026-11-13T00:00:00+09:00')
        self.assertEqual(schedule.next_run(target), target + timedelta(days=1))

    def test_time_validation(self):
        self.assertEqual(schedule.parse_time('00:00'), (0, 0))
        for value in ['24:00', '12:60', '0:00', '00:00\n', None]:
            with self.assertRaises(ValueError):
                schedule.parse_time(value)

    def test_grace_window(self):
        target = datetime(2026, 11, 13, tzinfo=schedule.KST)
        for seconds, expected in [(-1, False), (0, True), (300, True), (301, False)]:
            self.assertEqual(schedule.is_due(target + timedelta(seconds=seconds), target), expected)

    def test_child_arguments(self):
        runner = schedule.FacilityRefreshSchedule('test.sqlite3')
        with patch.object(schedule.subprocess, 'run', return_value=Mock(returncode=1)) as run:
            self.assertEqual(runner._launch(datetime(2026, 11, 13, tzinfo=schedule.KST)), 1)
        args, kwargs = run.call_args
        self.assertEqual(args[0][-2:], ['--scheduled-date', '2026-11-13'])
        self.assertNotIn('shell', kwargs)
        self.assertEqual(kwargs['stdout'], schedule.subprocess.DEVNULL)

    def test_loop_launch_or_skip_without_real_wait(self):
        before = datetime(2026, 11, 12, 23, 59, tzinfo=schedule.KST)
        for delay, expected in [(60, 'completed'), (361, 'skipped_late')]:
            current = [before]
            launch = Mock(return_value=0)
            runner = schedule.FacilityRefreshSchedule('test.sqlite3', clock=lambda: current[0], launch=launch)
            def wait(seconds):
                current[0] = before + timedelta(seconds=delay)
            runner.stop_event = Mock()
            runner.stop_event.is_set.return_value = False
            runner.stop_event.wait.side_effect = wait
            def logged(*args):
                runner.stop_event.is_set.return_value = True
            with patch.object(runner, '_log', side_effect=logged) as log, patch.object(schedule, 'run_job') as recovery:
                runner._loop()
            recovery.assert_called_once_with(database=runner.database, recover_only=True)
            self.assertEqual(log.call_args.args[0], expected)
            self.assertEqual(launch.call_count, int(expected == 'completed'))


if __name__ == '__main__':
    unittest.main()
