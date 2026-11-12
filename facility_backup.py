"""Validated SQLite online backups and guarded facility restores."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from facility_storage import DATABASE, APPLICATION_ID, FacilityStore
from facility_refresh_job import AlreadyRunning, job_lock

BACKUPS = DATABASE.parent / 'backups'


class BackupError(Exception):
    pass


def connect(path, mode='ro'):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=' + mode, uri=True, timeout=5)


def validate(path):
    """Reject corruption, unrelated schemas, and broken facility query structures."""
    with closing(connect(path)) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise BackupError('BACKUP_INTEGRITY_FAILED')
        if (db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                or db.execute('PRAGMA user_version').fetchone()[0] != 1):
            raise BackupError('UNSUPPORTED_FACILITY_DATABASE')
        triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        if not {'facilities_insert', 'facilities_update', 'facilities_delete'} <= triggers:
            raise BackupError('UNSUPPORTED_FACILITY_DATABASE')
        db.execute('SELECT row_id,minx,maxx,miny,maxy FROM facility_bounds LIMIT 1').fetchall()
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise BackupError('BACKUP_INTEGRITY_FAILED')
    # Exercises optional metadata and the normalized detail model as well.
    return FacilityStore(path).facility_status()


def copy_database(source, destination, *, timeout=120):
    deadline = time.monotonic() + timeout
    def progress(status, remaining, total):
        # SQLITE_DONE is already committed; never report a timeout after success.
        if status != sqlite3.SQLITE_DONE and time.monotonic() > deadline:
            raise BackupError('DATABASE_COPY_TIMEOUT')
    with closing(connect(source)) as src, closing(connect(destination, 'rwc')) as dst:
        src.backup(dst, pages=256, progress=progress, sleep=0.05)


def snapshot(source, directory, prefix='backup'):
    directory.mkdir(parents=True, exist_ok=True)
    name = prefix + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex
    pending = directory / (name + '.pending.sqlite3')
    output = directory / (name + '.sqlite3')
    try:
        copy_database(source, pending)
        validate(pending)
        pending.replace(output)
        return output
    finally:
        pending.unlink(missing_ok=True)


def run(action, *, database=DATABASE, backup_dir=BACKUPS, source=None):
    database, backup_dir = Path(database).resolve(), Path(backup_dir).resolve()
    if action not in {'backup', 'restore', 'verify'}:
        raise BackupError('INVALID_ACTION')
    if action in {'restore', 'verify'}:
        if source is None:
            raise BackupError('BACKUP_FILE_REQUIRED')
        source = Path(source).resolve()
        if not source.is_file():
            raise BackupError('BACKUP_FILE_MISSING')
    if action == 'verify':
        return {'status':'verified', 'file':str(source), 'data':validate(source)}
    if action == 'restore' and (source == database or
                               (database.exists() and source.samefile(database))):
        raise BackupError('SOURCE_EQUALS_DATABASE')
    lock = database.parent / (database.name + '.refresh-job') / 'run.lock'
    with job_lock(lock):
        if action == 'backup':
            if not database.is_file():
                raise BackupError('FACILITY_DB_MISSING')
            result = snapshot(database, backup_dir)
            return {'status':'backed_up', 'file':str(result)}
        # Stage a consistent, verified source before touching the current DB.
        staged = snapshot(source, backup_dir, 'restore-source')
        safety = None
        try:
            if database.exists():
                # Refuse to overwrite a corrupt/unrelated DB; preserve it for offline recovery.
                validate(database)
                safety = snapshot(database, backup_dir, 'before-restore')
                # SQLite's backup transaction handles locks and WAL safely. Never replace
                # a live DB file or delete its -wal/-shm files by hand.
                copy_database(staged, database)
            else:
                if any(Path(str(database) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
                    raise BackupError('ORPHAN_DATABASE_SIDECAR')
                # Publish a completed DB in the destination directory (same filesystem).
                ready = snapshot(staged, database.parent, 'restore-ready')
                try:
                    # Atomic no-overwrite publication on the same filesystem.
                    os.link(ready, database)
                finally:
                    ready.unlink(missing_ok=True)
            validate(database)
            return {'status':'restored', 'database':str(database),
                    'safety_backup':str(safety) if safety else None}
        finally:
            staged.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('backup','verify','restore'))
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--backup-dir', type=Path, default=BACKUPS)
    parser.add_argument('--file', type=Path, help='Backup file to verify or restore')
    args = parser.parse_args(argv)
    try:
        report = run(args.action, database=args.database, backup_dir=args.backup_dir, source=args.file)
        code = 0
    except AlreadyRunning:
        report, code = {'status':'failed', 'error':'FACILITY_JOB_BUSY'}, 1
    except BackupError as error:
        report, code = {'status':'failed', 'error':str(error)}, 1
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        report, code = {'status':'failed', 'error':'DATABASE_BACKUP_RESTORE_ERROR'}, 1
    print(json.dumps(report, ensure_ascii=False))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
