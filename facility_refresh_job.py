"""Shared entry point for manual and server-scheduled facility refresh jobs."""
import argparse
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from datetime import date, datetime, timezone
import io
import hashlib
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


def database_id(database):
    return hashlib.sha256(os.path.normcase(str(database)).encode('utf-8')).hexdigest()


def recover_interrupted(root, state_dir, identity):
    """Caller holds the DB lock: no cooperating refresh can still be running."""
    recovered = 0
    orphan_ids = set()
    for path in (root / '.cache/facilities/job-runs').glob('*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            continue  # Preserve unreadable evidence rather than guessing ownership.
        if not isinstance(record, dict) or record.get('database_id') != identity:
            continue
        if record.get('status') not in {'running', 'interrupted', 'failed'}:
            continue
        orphan_ids.add(path.stem)
        if record['status'] == 'running':
            record.update(status='interrupted', error='PREVIOUS_JOB_INTERRUPTED',
                          recovered_at=utc_now())
            write_json(path, record)
            recovered += 1
    for path in state_dir.glob('scheduled-*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            continue
        if isinstance(record, dict) and record.get('status') == 'started':
            record.update(status='interrupted', error='PREVIOUS_JOB_INTERRUPTED', recovered_at=utc_now())
            write_json(path, record)
            recovered += 1
    for path in (root / '.cache/facilities/refresh-runs').glob('*.json'):
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, UnicodeError):
            continue
        if (not isinstance(record, dict) or record.get('job_id') not in orphan_ids
                or record.get('status') != 'running'):
            continue
        record.update(status='interrupted', error='PREVIOUS_JOB_INTERRUPTED', recovered_at=utc_now())
        for scope in record.get('scopes', []):
            if isinstance(scope, dict) and scope.get('status') == 'running':
                scope.update(status='interrupted', error='PREVIOUS_JOB_INTERRUPTED')
        write_json(path, record)
        recovered += 1
    return recovered


def run_job(*, database=DATABASE, root=ROOT, dry_run=False, scheduled_date=None, refresh_main=None,
            recover_only=False):
    root, database = Path(root).resolve(), Path(database).resolve()
    # Lock/claims follow the DB location, so another checkout using this DB also locks.
    state_dir = database.parent / (database.name + '.refresh-job')
    log_dir = root / '.cache/facilities/job-runs'
    log_path = log_dir / (uuid.uuid4().hex + '.json')
    report = {'started_at': utc_now(), 'status': 'running',
              'trigger': 'recovery' if recover_only else ('scheduled' if scheduled_date else 'manual'),
              'dry_run': dry_run, 'database_id': database_id(database)}
    code = 1
    finalized = False
    try:
        if scheduled_date is not None:
            if date.fromisoformat(scheduled_date).isoformat() != scheduled_date or dry_run:
                raise ValueError('Invalid scheduled invocation')
        if recover_only and (scheduled_date or dry_run):
            raise ValueError('Invalid recovery invocation')
        # Do not create a missing database directory as an accidental side effect.
        if not database.is_file():
            report.update(status='failed', error='FACILITY_DB_MISSING')
        else:
            with job_lock(state_dir / 'run.lock'):
                report['recovered_records'] = recover_interrupted(root, state_dir, report['database_id'])
                claim = state_dir / ('scheduled-' + scheduled_date + '.json') if scheduled_date else None
                if recover_only:
                    report['status'] = 'recovered'
                    code = 0
                elif claim is not None and claim.exists():
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
                    try:
                        with redirect_stdout(output), redirect_stderr(io.StringIO()):
                            code = (refresh_main(args) if refresh_main else
                                    facility_refresh.main(args, job_id=log_path.stem))
                        summary = json.loads(output.getvalue())
                        if not isinstance(summary, dict) or summary.get('status') not in {'success', 'validated', 'partial', 'failed'}:
                            raise ValueError('Invalid refresh summary')
                        report.update(status=summary['status'], refresh_exit_code=code, refresh=summary)
                        if code not in (0, 1):
                            code = 1
                    except (KeyboardInterrupt, SystemExit):
                        report.update(status='interrupted', error='JOB_INTERRUPTED')
                        code = 1
                    except Exception:
                        report.update(status='failed', error='JOB_EXECUTION_ERROR')
                        code = 1
                    if claim is not None:
                        write_json(claim, {'status': report['status'], 'finished_at': utc_now(), 'exit_code': code})
                report['finished_at'] = utc_now()
                write_json(log_path, report)
                finalized = True  # Final state must be persisted before releasing the lock.
    except AlreadyRunning:
        report.update(status='skipped_overlap')
        code = 0
    except Exception:
        report.update(status='failed', error='JOB_EXECUTION_ERROR')
        code = 1
    finally:
        if not finalized:
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
    parser.add_argument('--recover-only', action='store_true', help='Reconcile interrupted logs without fetching data')
    parser.add_argument('--scheduled-date', help='Internal: KST date, at most one automatic attempt per date')
    args = parser.parse_args(argv)
    code, report = run_job(database=args.database, dry_run=args.dry_run, scheduled_date=args.scheduled_date,
                           recover_only=args.recover_only)
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
