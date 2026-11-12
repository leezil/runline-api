"""Opt-in integration check: production is read only; HTTP and refresh use copies."""
import argparse
from contextlib import closing, redirect_stdout
import hashlib
import http.client
import io
import json
from pathlib import Path
import secrets
import sys
import tempfile
import threading

from api_server import ApiServer, RouteService
import facility_backup
import facility_refresh
from facility_refresh_job import job_lock, write_json, utc_now
from facility_storage import DATABASE, FacilityStore
from places_api import query_places, validate_query

ROOT = Path(__file__).resolve().parent
QUERY = 'west=127&south=37.25&east=127.02&north=37.27&types=cctv&limit=200'


def digest(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else hashlib.sha256(file.read()).hexdigest()


def logical_digest(path):
    with closing(facility_backup.connect(path)) as db:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()


def check_http(database):
    token = secrets.token_urlsafe(32)
    service = RouteService(token, facility_database=database)
    expected_status = FacilityStore(database).facility_status()
    expected_places = query_places(FacilityStore(database), validate_query(QUERY))
    checks = 0
    for external in (False, True):
        with ApiServer(('127.0.0.1', 0), service, api_only=external) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path, auth=True):
                    with closing(http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)) as conn:
                        conn.request('GET', path, headers={'Authorization':'Bearer ' + token} if auth else {})
                        response = conn.getresponse()
                        return response.status, json.loads(response.read())
                assert request('/v1/health', False)[0] == 200
                for path in ('/v1/places/status', '/v1/places?' + QUERY):
                    assert request(path, False)[0] == 401
                assert request('/v1/places/status') == (200, expected_status)
                assert request('/v1/places?' + QUERY) == (200, expected_places)
                assert request('/v1/places/status?unexpected=1')[0] == 400
                checks += 6
            finally:
                server.shutdown()
                thread.join(timeout=5)
    return {'checks':checks, 'bbox_count':expected_places['count'],
            'bbox_truncated':expected_places['truncated'], 'sources':expected_status['sources']}


def run(database=DATABASE, *, live_dry_run=False):
    if sys.flags.optimize:
        raise RuntimeError('Run without -O so verification assertions remain enabled')
    database = Path(database).resolve()
    report = {'started_at':utc_now(), 'live_dry_run':live_dry_run, 'status':'running'}
    # Cooperating refresh/restore jobs cannot change production during this check.
    with job_lock(database.parent / (database.name + '.refresh-job') / 'run.lock'):
        before = digest(database)
        original = logical_digest(database)
        try:
            with tempfile.TemporaryDirectory(prefix='runline-integration-') as folder:
                folder = Path(folder)
                saved = facility_backup.snapshot(database, folder)
                assert logical_digest(saved) == original
                restored = folder / 'restored.sqlite3'
                result = facility_backup.run('restore', database=restored, backup_dir=folder / 'backups', source=saved)
                assert result['status'] == 'restored'
                assert logical_digest(restored) == original
                report['backup_restore_equal'] = True
                report['http'] = check_http(restored)
                restored_before = digest(restored)
                if live_dry_run:
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = facility_refresh.main(['--database', str(restored), '--env-file', str(ROOT / '.env'),
                                                      '--region-code','3740000','--region-code','3830000','--dry-run'])
                    summary = json.loads(output.getvalue())
                    report['refresh'] = summary
                    assert code == 0 and summary['status'] == 'validated'
                assert digest(restored) == restored_before
                assert logical_digest(restored) == original
                report['copy_unchanged'] = True
                assert check_http(restored) == report['http']
                report['http_checks_total'] = report['http']['checks'] * 2
                report['status'] = 'passed'
        except Exception:
            report['status'] = 'failed'
            report['error'] = 'INTEGRATION_CHECK_FAILED'
        finally:
            report['production_unchanged'] = digest(database) == before and logical_digest(database) == original
            if not report['production_unchanged']:
                report['status'] = 'failed'
            report['finished_at'] = utc_now()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--live-dry-run', action='store_true', help='Use real provider API on the temporary DB only')
    args = parser.parse_args(argv)
    try:
        report = run(args.database, live_dry_run=args.live_dry_run)
        write_json(ROOT / '.cache/facilities/integration-last.json', report)
    except Exception:
        report = {'status':'failed', 'error':'INTEGRATION_SETUP_OR_LOG_ERROR'}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
