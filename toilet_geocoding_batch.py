"""Bounded, resumable pending-toilet geocoding for Seoul and Gyeonggi."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
import uuid

from facility_backup import snapshot, BACKUPS
from facility_refresh_job import job_lock, AlreadyRunning, write_json
from facility_storage import DATABASE, FacilityStore
from toilet_geocoding import (ROOT, JusoClient, GeocodingFailure, load_settings,
                              make_transformer, run, utc_now)

SEOUL = tuple(str(code * 10000) for code in range(300, 325))
GYEONGGI = tuple(code + '0000' for code in (
    '374', '378', '382', '383', '386', '390', '391', '392', '393', '394',
    '397', '398', '399', '400', '401', '402', '403', '404', '405', '406',
    '407', '408', '409', '414', '416', '417', '553', '554', '559', '560', '570'))
REGIONS = SEOUL + GYEONGGI


def plan(store, regions=REGIONS):
    source = next(s for s in store.facility_status()['sources'] if s['type'] == 'toilet')
    existing = {r['region_code']: r for r in source['regions']}
    selected = [existing[r] for r in regions if r in existing]
    return {
        'regions': len(regions),
        'missing_regions': [r for r in regions if r not in existing or not existing[r]['ready']],
        'empty_regions': [r['region_code'] for r in selected if r['ready'] and not r['counts']['stored']],
        'stored': sum(r['counts']['stored'] for r in selected),
        'map_ready': sum(r['counts']['map_ready'] for r in selected),
        'pending': sum(r['counts']['active_coordinates']['pending'] for r in selected),
        'unresolved': sum(sum(r['counts']['active_coordinates'][s]
                              for s in ('failed', 'invalid', 'ambiguous')) for r in selected),
    }


def run_batch(store, client, transform, *, regions=REGIONS, batch_size=25, report=None, progress=None):
    if not 1 <= batch_size <= 1000:
        raise ValueError('Invalid batch size')
    report = report if report is not None else {}
    report.update(status='running', started_at=utc_now(), before=plan(store, regions),
                  saved=0, stale=0, requests=0, outcomes={}, region=None)
    try:
        if report['before']['missing_regions']:
            raise GeocodingFailure('REGION_IMPORT_REQUIRED')
        for region in regions:
            report['region'] = region
            while store.geocoding_candidates(region, limit=1):
                chunk = {}
                try:
                    run(store, region, client=client, transform=transform,
                        limit=batch_size, report=chunk)
                finally:
                    report['saved'] += chunk.get('saved', 0)
                    report['stale'] += chunk.get('stale', 0)
                    report['requests'] = client.requests
                    for status, count in chunk.get('outcomes', {}).items():
                        report['outcomes'][status] = report['outcomes'].get(status, 0) + count
                    if progress:
                        progress(report)
                if not chunk.get('saved'):
                    raise GeocodingFailure('NO_PROGRESS')
        report['status'] = 'processed'
    except GeocodingFailure as error:
        report.update(status='stopped', error=str(error))
    except KeyboardInterrupt:
        report.update(status='interrupted', error='INTERRUPTED')
    finally:
        report.update(after=plan(store, regions), finished_at=utc_now(), requests=client.requests)
        if report['status'] == 'processed' and report['after']['pending']:
            report.update(status='stopped', error='PENDING_REMAIN')
        if progress:
            progress(report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--max-requests', type=int, default=10000)
    parser.add_argument('--passes', type=int, default=1, help='Budget windows (1..10); resume only after budget exhaustion')
    parser.add_argument('--batch-size', type=int, default=25)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    if not 1 <= args.max_requests <= 10000 or not 1 <= args.batch_size <= 1000 or not 1 <= args.passes <= 10:
        parser.error('max-requests: 1..10000; batch-size: 1..1000')
    database = args.database.resolve()
    store = FacilityStore(database)
    try:
        if args.dry_run:
            result = plan(store)
            print(json.dumps(result))
            return 1 if result['missing_regions'] else 0
        settings = load_settings(args.env_file)
        if settings['JUSO_STORAGE_APPROVED'] != 'true':
            raise GeocodingFailure('STORAGE_PERMISSION_UNCONFIRMED')
        client = JusoClient(settings['JUSO_SEARCH_KEY'], settings['JUSO_COORD_KEY'],
                            max_requests=args.max_requests)
        transform = make_transformer()
        lock = database.parent / (database.name + '.refresh-job') / 'run.lock'
        with job_lock(lock):
            if plan(store)['missing_regions']:
                raise GeocodingFailure('REGION_IMPORT_REQUIRED')
            backup = snapshot(database, BACKUPS, 'before-geocoding-batch')
            for pass_number in range(1, args.passes + 1):
                if pass_number > 1:
                    time.sleep(1)  # Preserve pacing across request-budget windows.
                    client = JusoClient(settings['JUSO_SEARCH_KEY'], settings['JUSO_COORD_KEY'],
                                        max_requests=args.max_requests)
                output = ROOT / '.cache/facilities/geocoding-batches' / (uuid.uuid4().hex + '.json')
                report = {'backup': str(backup), 'pass': pass_number}
                def progress(value):
                    write_json(output, value)
                    print(json.dumps({k: value.get(k) for k in
                                      ('status', 'pass', 'region', 'saved', 'requests', 'outcomes', 'error')}), flush=True)
                result = run_batch(store, client, transform, batch_size=args.batch_size,
                                   report=report, progress=progress)
                print(json.dumps(result))
                print('Report:', output, flush=True)
                if result['status'] == 'processed':
                    return 0
                if result.get('error') != 'REQUEST_BUDGET_EXHAUSTED':
                    return 1
            return 1
    except AlreadyRunning:
        print('FACILITY_JOB_BUSY')
    except GeocodingFailure as error:
        print(str(error))
    except (OSError, ValueError, KeyError, sqlite3.Error):
        print('LOCAL_BATCH_ERROR')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
