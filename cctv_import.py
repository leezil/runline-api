"""Import a complete municipality CCTV listing into the local facility database."""
import argparse
import json
import math
from pathlib import Path
import re
import sqlite3
import uuid

from facility_storage import DATABASE, Facility, FacilityStore, CctvDetails
from toilet_import import (ImportFailure, load_service_key, parse_page, utc_now,
                           _text, _integer, fetch_page as fetch_public_data_page)

ROOT = Path(__file__).resolve().parent
ENDPOINT = 'https://apis.data.go.kr/1741000/cctv_info/info'
# Deliberately narrow: see docs/facilities/CCTV_IMPORT.md for official evidence.
CRIME_PURPOSES = frozenset({'생활방범'})
OTHER_PURPOSES = frozenset({'교통단속', '교통정보수집', '쓰레기단속', '시설물관리', '재난재해'})


def classify_purpose(value):
    """Exact tokens only; unknown/multipurpose/children protection are not inferred."""
    if not value:
        return None
    tokens = {part.strip() for part in re.split(r'[,;/+|·ㆍ]|\s+및\s+', value) if part.strip()}
    if tokens & CRIME_PURPOSES:
        return True
    if tokens and tokens <= OTHER_PURPOSES:
        return False
    return None


def coordinates(row):
    values = [row.get('WGS84_LOT'), row.get('WGS84_LAT')]
    missing = [value is None or (isinstance(value, str) and not value.strip()) for value in values]
    if all(missing):
        return None, None, 'invalid', 'missing'
    try:
        if any(missing) or any(isinstance(value, bool) or not isinstance(value, (str, int, float)) for value in values):
            raise ValueError()
        lon, lat = map(float, values)
        if not (math.isfinite(lon) and math.isfinite(lat) and 124 <= lon <= 132 and 32 <= lat <= 39):
            raise ValueError()
        return lon, lat, 'valid', None
    except (ValueError, TypeError, OverflowError):
        return None, None, 'invalid', 'invalid'


def convert_row(row, region_code, fetched_at):
    region = _text(row, 'OPN_ATMY_GRP_CD', required=True)
    if region != region_code:
        raise ImportFailure('REGION_MISMATCH')
    # Absence of documented fields is a schema failure, unlike a provided empty value.
    if any(key not in row for key in ('INSTL_PRPS_SE_NM', 'WGS84_LOT', 'WGS84_LAT')):
        raise ImportFailure('MISSING_CCTV_FIELDS')
    purpose = _text(row, 'INSTL_PRPS_SE_NM')
    crime = classify_purpose(purpose)
    count = row.get('CAM_CNTOM')
    count = None if count is None or (isinstance(count, str) and not count.strip()) else _integer(count)
    lon, lat, status, _ = coordinates(row)
    return Facility(
        kind='cctv', source='mois_cctv', source_region_code=region,
        source_record_id=_text(row, 'MNG_NO', required=True),
        name='방범 CCTV' if crime is True else 'CCTV',
        address_road=_text(row, 'LCTN_ROAD_NM_ADDR'), address_lot=_text(row, 'LCTN_LOTNO_ADDR'),
        manager_name=_text(row, 'MNG_INST_NM'), source_date=_text(row, 'DAT_CRTR_YMD'),
        source_modified_at=_text(row, 'LAST_MDFCN_PNT'), source_updated_at=_text(row, 'DAT_UPDT_PNT'),
        source_change_type=_text(row, 'DAT_UPDT_SE'), fetched_at=fetched_at,
        longitude=lon, latitude=lat, coordinate_status=status,
        coordinate_source='source' if status == 'valid' else 'none',
        details=CctvDetails(purpose_raw=purpose, camera_count=count, crime_prevention=crime),
    )


def fetch_page(service_key, region_code, page, page_size, **kwargs):
    return fetch_public_data_page(service_key, region_code, page, page_size,
                                  endpoint=ENDPOINT, **kwargs)


def import_cctv(store, service_key, region_code, *, page_size=100, max_pages=100,
                fetch=fetch_page, dry_run=False, report=None):
    """All pages validate before one transaction; no missing-record deletion."""
    report = report if report is not None else {}
    report.update(status='running', source='mois_cctv', region_code=region_code,
                  started_at=utc_now(), pages=0, received=0, saved=0, duplicates=0,
                  rejected=0, missing_coordinates=0, invalid_coordinates=0,
                  crime_prevention=0, other_purpose=0, unknown_purpose=0,
                  map_candidates=0, dry_run=dry_run)
    try:
        if (not isinstance(region_code, str) or not re.fullmatch(r'[0-9]{7}', region_code)
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
                if record.source_record_id in seen:
                    report['duplicates'] += 1
                    raise ImportFailure('DUPLICATE_RECORD')
                seen.add(record.source_record_id)
                issue = coordinates(row)[3]
                if issue:
                    report[issue + '_coordinates'] += 1
                purpose = record.details.crime_prevention
                report['crime_prevention' if purpose is True else 'other_purpose' if purpose is False else 'unknown_purpose'] += 1
                if purpose is True and record.coordinate_status == 'valid':
                    report['map_candidates'] += 1
                records.append(record)
            if len(records) == total:
                break
        if len(records) != total:
            raise ImportFailure('INCOMPLETE_LISTING')
        if not dry_run:
            store.initialize()
            # Preserve manual deactivation. Invalid incoming coordinates are not
            # pending, so the store cannot retain stale coordinates from a prior run.
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
    parser.add_argument('--region-code', required=True)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--page-size', type=int, default=100)
    parser.add_argument('--max-pages', type=int, default=100)
    parser.add_argument('--dry-run', action='store_true', help='Fetch and validate without writing the facility DB')
    args = parser.parse_args(argv)
    report = {'status': 'failed', 'source': 'mois_cctv', 'started_at': utc_now()}
    log_dir = ROOT / '.cache/facilities/cctv-import-runs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / (uuid.uuid4().hex + '.json')).open('x', encoding='utf-8') as log:
            try:
                import_cctv(FacilityStore(args.database), load_service_key(args.env_file), args.region_code,
                            page_size=args.page_size, max_pages=args.max_pages,
                            dry_run=args.dry_run, report=report)
            except ImportFailure as exc:
                report.update(status='failed', error=str(exc), finished_at=utc_now())
            except (OSError, UnicodeError):
                report.update(status='failed', error='LOCAL_FILE_ERROR', finished_at=utc_now())
            json.dump(report, log, ensure_ascii=False, indent=2)
    except OSError:
        print('LOG_WRITE_ERROR: verify database state before rerunning.')
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] in {'success', 'validated'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
