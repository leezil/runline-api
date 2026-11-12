"""Refresh complete facility listings with guarded absence reconciliation.

No guessed deletion codes or unverified incremental cursor semantics are used.
The checkpoint represents a successfully applied full snapshot, not a source watermark.
"""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

import cctv_import
import toilet_import
from facility_storage import DATABASE, SOURCES, FacilityStore
from toilet_import import ImportFailure, parse_page, load_service_key, utc_now

ROOT = Path(__file__).resolve().parent
ADAPTERS = {'toilet': toilet_import, 'cctv': cctv_import}
GUARDS = {'REFRESH_BASELINE_CHANGED', 'EMPTY_SNAPSHOT_BLOCKED', 'EXCESSIVE_MISSING_BLOCKED',
          'REFRESH_SCOPE_MISMATCH', 'DUPLICATE_RECORD', 'REFRESH_CLOCK_REVERSED'}


def refresh_one(store, service_key, kind, region, *, page_size=100, max_pages=100,
                dry_run=False, allow_empty=False, max_missing_ratio=0.2,
                fetch=None, clock=utc_now, report=None):
    report = report if report is not None else {}
    report.update(kind=kind, region_code=region, mode='full', status='running',
                  started_at=clock(), pages=0, received=0, dry_run=dry_run)
    baseline = None
    try:
        if (kind not in ADAPTERS or not isinstance(region, str) or not re.fullmatch(r'[0-9]{7}', region)
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 1000
                or type(dry_run) is not bool or type(allow_empty) is not bool
                or isinstance(max_missing_ratio, bool) or not isinstance(max_missing_ratio, (int, float))
                or not math.isfinite(max_missing_ratio) or not 0 <= max_missing_ratio <= 1):
            raise ImportFailure('INVALID_OPTIONS')
        if not isinstance(service_key, str) or not service_key.strip():
            raise ImportFailure('MISSING_SERVICE_KEY')
        if not store.path.is_file():
            raise ImportFailure('FACILITY_DB_MISSING')
        adapter = ADAPTERS[kind]
        baseline = store.refresh_snapshot(SOURCES[kind], region)
        fetch = fetch or adapter.fetch_page
        records, seen, total = [], set(), None
        for page in range(1, max_pages + 1):
            current_total, items = parse_page(fetch(service_key, region, page, page_size), page, page_size)
            report['pages'] += 1
            if total is None:
                total = current_total
                report['expected'] = total
                if total > page_size * max_pages:
                    raise ImportFailure('PAGE_BUDGET_EXCEEDED')
            elif total != current_total:
                raise ImportFailure('TOTAL_CHANGED')
            for item in items:
                record = adapter.convert_row(item, region, report['started_at'])
                if record.source_record_id in seen:
                    raise ImportFailure('DUPLICATE_RECORD')
                seen.add(record.source_record_id)
                records.append(record)
                report['received'] += 1
            if len(records) == total:
                break
        if len(records) != total:
            raise ImportFailure('INCOMPLETE_LISTING')
        outcome = store.apply_refresh(records, SOURCES[kind], region, clock(), baseline['fingerprint'],
                                      allow_empty=allow_empty, max_missing_ratio=max_missing_ratio,
                                      dry_run=dry_run)
        report.update(outcome, status='validated' if dry_run else 'success')
        return report
    except ImportFailure as exc:
        report.update(status='failed', error=str(exc))
        raise
    except ValueError as exc:
        code = str(exc) if str(exc) in GUARDS else 'VALIDATION_OR_STORAGE_ERROR'
        report.update(status='failed', error=code)
        raise ImportFailure(code) from None
    except (OSError, sqlite3.Error):
        report.update(status='failed', error='STORAGE_ERROR')
        raise ImportFailure('STORAGE_ERROR') from None
    finally:
        report['finished_at'] = clock()
        if (baseline is not None and not dry_run and report['status'] == 'failed'
                and report['error'] != 'REFRESH_BASELINE_CHANGED'):
            try:
                # Do not let a stale failed run overwrite a newer successful run.
                recorded = store.record_import_failure(SOURCES[kind], region, report['finished_at'],
                                                        expected_refresh=baseline['fingerprint'])
                report['failure_state_recorded'] = recorded
            except (OSError, ValueError, sqlite3.Error):
                report['failure_state_recorded'] = False


def main(argv=None, *, job_id=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-code', action='append', required=True,
                        help='Repeat for each municipality; each scope commits independently')
    parser.add_argument('--types', default='toilet,cctv')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--page-size', type=int, default=100)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--dry-run', action='store_true', help='Fetch and validate without modifying DB or checkpoints')
    parser.add_argument('--allow-empty', action='store_true', help='Permit an empty complete snapshot after source review')
    parser.add_argument('--max-missing-ratio', type=float, default=0.2,
                        help='Reject larger disappearance fraction (0..1); default 0.2')
    args = parser.parse_args(argv)
    kinds = args.types.split(',')
    if (not kinds or len(set(kinds)) != len(kinds) or any(kind not in ADAPTERS for kind in kinds)
            or len(set(args.region_code)) != len(args.region_code)
            or any(not re.fullmatch(r'[0-9]{7}', region) for region in args.region_code)
            or not 1 <= args.page_size <= 100 or not 1 <= args.max_pages <= 1000
            or not math.isfinite(args.max_missing_ratio) or not 0 <= args.max_missing_ratio <= 1):
        parser.error('Invalid types, region codes, page limits or missing ratio')
    report = {'started_at': utc_now(), 'status': 'running', 'scopes': []}
    if job_id is not None:
        report['job_id'] = job_id
    log_dir = ROOT / '.cache/facilities/refresh-runs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / (uuid.uuid4().hex + '.json')).open('x', encoding='utf-8') as log:
            json.dump(report, log, ensure_ascii=False, indent=2)
            log.flush()
            try:
                key = load_service_key(args.env_file)
                store = FacilityStore(args.database)
                for region in args.region_code:
                    for kind in kinds:
                        scope = {}
                        report['scopes'].append(scope)
                        try:
                            refresh_one(store, key, kind, region, page_size=args.page_size,
                                        max_pages=args.max_pages, dry_run=args.dry_run,
                                        allow_empty=args.allow_empty, max_missing_ratio=args.max_missing_ratio,
                                        report=scope)
                        except ImportFailure:
                            pass  # Other regions/kinds are independent scopes.
                        log.seek(0)
                        json.dump(report, log, ensure_ascii=False, indent=2)
                        log.truncate()
                        log.flush()
                failures = sum(scope['status'] == 'failed' for scope in report['scopes'])
                report['status'] = ('failed' if failures == len(report['scopes']) else 'partial') if failures else ('validated' if args.dry_run else 'success')
            except ImportFailure as exc:
                report.update(status='failed', error=str(exc))
            except (OSError, UnicodeError):
                report.update(status='failed', error='LOCAL_FILE_ERROR')
            report['finished_at'] = utc_now()
            log.seek(0)
            json.dump(report, log, ensure_ascii=False, indent=2)
            log.truncate()
    except OSError:
        print('LOG_WRITE_ERROR: verify database and checkpoints before rerunning.')
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] in {'success', 'validated'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
