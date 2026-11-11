"""Import one municipality's public restroom v2 listing; no geocoding or deletion."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from facility_storage import DATABASE, Facility, FacilityStore, ToiletDetails


ENDPOINT = 'https://apis.data.go.kr/1741000/public_restroom_info_v2/info_v2'
ROOT = Path(__file__).resolve().parent
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
COUNTS = {
    'male_toilet_count': 'MALE_TOILT_CNT',
    'male_urinal_count': 'MALE_URNL_CNT',
    'female_toilet_count': 'FEMALE_TOILT_CNT',
    'male_accessible_toilet_count': 'MALE_FRDBL_TOILT_CNT',
    'male_accessible_urinal_count': 'MALE_FRDBL_URNL_CNT',
    'female_accessible_toilet_count': 'FEMALE_FRDBL_TOILT_CNT',
}


class ImportFailure(Exception):
    """Only fixed, non-sensitive diagnostic codes are exposed to the CLI."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_service_key(env_file=ROOT / '.env'):
    value = os.environ.get('PUBLIC_DATA_SERVICE_KEY')
    if value is None and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding='utf-8-sig').splitlines():
            key, sep, candidate = line.strip().partition('=')
            if sep and key.strip() == 'PUBLIC_DATA_SERVICE_KEY':
                value = candidate.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
    if not value or not value.strip():
        raise ImportFailure('MISSING_SERVICE_KEY')
    return value.strip()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward the credential-bearing query to another destination.
        return None


def fetch_page(service_key, region_code, page, page_size, *, opener=None, sleep=time.sleep):
    opener = opener or urllib.request.build_opener(_NoRedirect()).open
    query = urllib.parse.urlencode({
        'serviceKey': service_key, 'pageNo': page, 'numOfRows': page_size,
        'returnType': 'json', 'cond[OPN_ATMY_GRP_CD::EQ]': region_code,
    })
    request = urllib.request.Request(ENDPOINT + '?' + query,
                                     headers={'Accept': 'application/json', 'User-Agent': 'runline-api/1'})
    for attempt in range(3):
        try:
            with opener(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ImportFailure('RESPONSE_TOO_LARGE')
            return raw
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status not in {429, 500, 502, 503, 504} or attempt == 2:
                raise ImportFailure('HTTP_ERROR') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise ImportFailure('NETWORK_ERROR') from None
        sleep(2 ** attempt)


def _integer(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r'[0-9]{1,12}', value.strip()):
        return int(value)
    raise ImportFailure('INVALID_INTEGER')


def parse_page(raw, expected_page, page_size):
    """Accept documented JSON envelope; fail closed on schema/service errors."""
    try:
        text = raw.decode('utf-8-sig')
        if text.lstrip().startswith('<'):
            ET.fromstring(text)  # XML service errors sometimes arrive with HTTP 200.
            raise ImportFailure('API_XML_ERROR')
        payload = json.loads(text)
        response = payload['response']
        if response['header']['resultCode'] not in ('00', '0'):
            raise ImportFailure('API_RESULT_ERROR')
        body = response['body']
        total = _integer(body['totalCount'])
        if _integer(body['pageNo']) != expected_page or _integer(body['numOfRows']) != page_size:
            raise ImportFailure('PAGE_METADATA_MISMATCH')
        items = body['items']
        if items is None or items == '':
            items = []
        else:
            items = items['item']
            if items is None or items == '':
                items = []
            elif isinstance(items, dict):
                items = [items]
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ImportFailure('INVALID_ITEMS')
        expected_count = min(page_size, max(0, total - (expected_page - 1) * page_size))
        if len(items) != expected_count:
            raise ImportFailure('PAGE_COUNT_MISMATCH')
        return total, items
    except (KeyError, TypeError, ValueError, AttributeError, ET.ParseError):
        raise ImportFailure('RESPONSE_SCHEMA_ERROR') from None


def _text(row, key, *, required=False):
    value = row.get(key)
    if value is not None and not isinstance(value, str):
        raise ImportFailure('INVALID_TEXT_FIELD')
    value = value.strip() or None if value is not None else None
    if required and value is None:
        raise ImportFailure('MISSING_REQUIRED_FIELD')
    return value


def convert_row(row, region_code, fetched_at):
    region = _text(row, 'OPN_ATMY_GRP_CD', required=True)
    if region != region_code:
        raise ImportFailure('REGION_MISMATCH')
    count_values = {}
    for target, source in COUNTS.items():
        value = row.get(source)
        count_values[target] = None if value is None or (isinstance(value, str) and not value.strip()) else _integer(value)
    # Raw flag values are not verified yet; do not guess the meaning of Y/N or codes.
    return Facility(
        kind='toilet', source='mois_toilet_v2', source_region_code=region,
        source_record_id=_text(row, 'MNG_NO', required=True),
        name=_text(row, 'RSTRM_NM', required=True),
        address_road=_text(row, 'LCTN_ROAD_NM_ADDR'),
        address_lot=_text(row, 'LCTN_LOTNO_ADDR'),
        manager_name=_text(row, 'MNG_INST_NM'),
        source_date=_text(row, 'DAT_CRTR_YMD'),
        source_modified_at=_text(row, 'LAST_MDFCN_PNT'),
        source_updated_at=_text(row, 'DAT_UPDT_PNT'),
        source_change_type=_text(row, 'DAT_UPDT_SE'), fetched_at=fetched_at,
        details=ToiletDetails(category_raw=_text(row, 'SE_NM'),
                              opening_hours_text=_text(row, 'OPN_HR'),
                              opening_hours_detail=_text(row, 'OPN_HR_DTL'), **count_values),
    )


def import_toilets(store, service_key, region_code, *, page_size=100, max_pages=100,
                   fetch=fetch_page, dry_run=False, report=None):
    """Validate the complete bounded listing before making one atomic DB write."""
    report = report if report is not None else {}
    report.update(status='running', started_at=utc_now(), pages=0, received=0, saved=0,
                  duplicates=0, rejected=0, missing_address=0, dry_run=dry_run)
    try:
        if (not isinstance(region_code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', region_code)
                or type(page_size) is not int or not 1 <= page_size <= 100
                or type(max_pages) is not int or not 1 <= max_pages <= 1000
                or type(dry_run) is not bool):
            raise ImportFailure('INVALID_OPTIONS')
        if not isinstance(service_key, str) or not service_key.strip():
            raise ImportFailure('MISSING_SERVICE_KEY')
        records, seen, total = [], set(), None
        for page in range(1, max_pages + 1):
            current_total, items = parse_page(fetch(service_key, region_code, page, page_size), page, page_size)
            report['pages'] += 1
            if total is None:
                total = current_total
                report['expected'] = total
                if total > max_pages * page_size:
                    raise ImportFailure('PAGE_BUDGET_EXCEEDED')
            elif current_total != total:
                raise ImportFailure('TOTAL_CHANGED')
            for row in items:
                report['received'] += 1
                try:
                    record = convert_row(row, region_code, report['started_at'])
                except ImportFailure:
                    report['rejected'] += 1
                    raise
                key = record.source_record_id
                if key in seen:
                    report['duplicates'] += 1
                    raise ImportFailure('DUPLICATE_RECORD')
                seen.add(key)
                if not (record.address_road or record.address_lot):
                    report['missing_address'] += 1
                records.append(record)
            if len(records) == total:
                break
        if len(records) != total:
            raise ImportFailure('INCOMPLETE_LISTING')
        if not dry_run:
            store.initialize()
            store.upsert_many(records, preserve_local_state=True)
            report['saved'] = len(records)
        report['status'] = 'validated' if dry_run else 'success'
        return report
    except ImportFailure as exc:
        report.update(status='failed', error=str(exc))
        raise
    except (ValueError, sqlite3.Error, OSError):
        report.update(status='failed', error='STORAGE_ERROR')
        raise ImportFailure('STORAGE_ERROR') from None
    finally:
        report['finished_at'] = utc_now()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-code', required=True, help='Exact code from the official municipality code file')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--page-size', type=int, default=100)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--dry-run', action='store_true', help='Fetch and validate without writing facilities')
    args = parser.parse_args(argv)
    report = {'status': 'failed', 'started_at': utc_now()}
    # Establish the log destination before any DB mutation. Never log request URLs,
    # key values, raw responses or provider-supplied error messages.
    log_dir = ROOT / '.cache/facilities/import-runs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / (uuid.uuid4().hex + '.json')).open('x', encoding='utf-8') as log:
            try:
                key = load_service_key(args.env_file)
                import_toilets(FacilityStore(args.database), key, args.region_code,
                               page_size=args.page_size, max_pages=args.max_pages,
                               dry_run=args.dry_run, report=report)
            except ImportFailure as exc:
                report.update(status='failed', error=str(exc), finished_at=utc_now())
            except (OSError, UnicodeError):
                report.update(status='failed', error='LOCAL_FILE_ERROR', finished_at=utc_now())
            json.dump(report, log, ensure_ascii=False, indent=2)
    except OSError:
        # The DB may already have committed if the disk failed during log writing.
        print('LOG_WRITE_ERROR: verify database state before rerunning.')
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] in {'success', 'validated'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
