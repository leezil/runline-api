from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
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
            with patch.object(runner, '_log', side_effect=logged) as log:
                runner._loop()
            self.assertEqual(log.call_args.args[0], expected)
            self.assertEqual(launch.call_count, int(expected == 'completed'))


if __name__ == '__main__':
    unittest.main()
