"""Shared entry point for manual and server-scheduled facility refresh jobs."""
import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import date, datetime, timezone
import io
import json
import os
from pathlib import Path
import uuid

from facility_storage import DATABASE
import facility_refresh

ROOT = Path(__file__).resolve().parent


class AlreadyRunning(Exception):
    pass


@contextmanager
def job_lock(path):
    """Kernel-owned lock, released even if the process dies; never unlink it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if handle.seek(0, 2) == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise AlreadyRunning() from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning() from None
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def run_job(*, database=DATABASE, root=ROOT, dry_run=False, scheduled_date=None, refresh_main=None):
    root, database = Path(root).resolve(), Path(database).resolve()
    # Lock/claims follow the DB location, so another checkout using this DB also locks.
    state_dir = database.parent / (database.name + '.refresh-job')
    log_dir = root / '.cache/facilities/job-runs'
    log_path = log_dir / (uuid.uuid4().hex + '.json')
    report = {'started_at': utc_now(), 'status': 'running',
              'trigger': 'scheduled' if scheduled_date else 'manual', 'dry_run': dry_run}
    code = 1
    try:
        if scheduled_date is not None:
            if date.fromisoformat(scheduled_date).isoformat() != scheduled_date or dry_run:
                raise ValueError('Invalid scheduled invocation')
        # Do not create a missing database directory as an accidental side effect.
        if not database.is_file():
            report.update(status='failed', error='FACILITY_DB_MISSING')
        else:
            with job_lock(state_dir / 'run.lock'):
                claim = state_dir / ('scheduled-' + scheduled_date + '.json') if scheduled_date else None
                if claim is not None and claim.exists():
                    report.update(status='skipped_already_scheduled', scheduled_date=scheduled_date)
                    code = 0
                else:
                    write_json(log_path, report)
                    if claim is not None:
                        # Claim before running: at most one scheduled attempt per KST date.
                        # A failed/interrupted job is retried manually, not in a tight loop.
                        report['scheduled_date'] = scheduled_date
                        write_json(claim, {'status': 'started', 'started_at': report['started_at']})
                    args = ['--region-code', '3740000', '--region-code', '3830000',
                            '--database', str(database), '--env-file', str(root / '.env')]
                    if dry_run:
                        args.append('--dry-run')
                    output = io.StringIO()
                    # Only the known structured summary is logged; unexpected stderr
                    # and exception text are discarded to avoid credential leakage.
                    with redirect_stdout(output), redirect_stderr(io.StringIO()):
                        code = (refresh_main or facility_refresh.main)(args)
                    summary = json.loads(output.getvalue())
                    if not isinstance(summary, dict) or summary.get('status') not in {'success', 'validated', 'partial', 'failed'}:
                        raise ValueError('Invalid refresh summary')
                    report.update(status=summary['status'], refresh_exit_code=code, refresh=summary)
                    if code not in (0, 1):
                        code = 1
                    if claim is not None:
                        write_json(claim, {'status': report['status'], 'finished_at': utc_now(), 'exit_code': code})
    except AlreadyRunning:
        report.update(status='skipped_overlap')
        code = 0
    except Exception:
        report.update(status='failed', error='JOB_EXECUTION_ERROR')
        code = 1
    finally:
        report['finished_at'] = utc_now()
        try:
            write_json(log_path, report)
        except OSError:
            report.update(status='failed', error='JOB_LOG_WRITE_ERROR')
            code = 1
    return code, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--scheduled-date', help='Internal: KST date, at most one automatic attempt per date')
    args = parser.parse_args(argv)
    code, report = run_job(database=args.database, dry_run=args.dry_run, scheduled_date=args.scheduled_date)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
