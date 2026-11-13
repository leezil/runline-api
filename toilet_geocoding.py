"""Conservative Juso address geocoding into the local facility SQLite database."""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from facility_storage import DATABASE, FacilityStore, address_hash

ROOT = Path(__file__).resolve().parent
SEARCH_URL = 'https://business.juso.go.kr/addrlink/addrLinkApi.do'
COORD_URL = 'https://business.juso.go.kr/addrlink/addrCoordApi.do'
PROVIDER = 'juso-entrance-v2'
CODE_FIELDS = ('admCd', 'rnMgtSn', 'udrtYn', 'buldMnnm', 'buldSlno')
MAX_BYTES = 1024 * 1024


class GeocodingFailure(Exception):
    """Only fixed diagnostic codes; never provider messages, keys or URLs."""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_settings(path=ROOT / '.env'):
    names = ('JUSO_SEARCH_KEY', 'JUSO_COORD_KEY', 'JUSO_STORAGE_APPROVED')
    values = {}
    if Path(path).is_file():
        for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
            key, sep, value = line.strip().partition('=')
            if sep and key.strip() in names:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    return {name: os.environ.get(name, values.get(name, '')).strip() for name in names}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JusoClient:
    def __init__(self, search_key, coord_key, *, max_requests=150, opener=None,
                 sleep=time.sleep, clock=time.monotonic):
        if not search_key or not coord_key:
            raise GeocodingFailure('MISSING_JUSO_KEYS')
        if type(max_requests) is not int or not 1 <= max_requests <= 10000:
            raise GeocodingFailure('INVALID_REQUEST_BUDGET')
        self.search_key, self.coord_key = search_key, coord_key
        self.max_requests, self.requests = max_requests, 0
        self.open = opener or urllib.request.build_opener(_NoRedirect()).open
        self.sleep, self.clock, self.last_request = sleep, clock, None

    def request(self, url, params):
        request = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode('utf-8'),
                                         headers={'Accept': 'application/json',
                                                  'User-Agent': 'runline-api/1'})
        for attempt in range(3):
            if self.requests >= self.max_requests:
                raise GeocodingFailure('REQUEST_BUDGET_EXHAUSTED')
            if self.last_request is not None:
                self.sleep(max(0, 1.0 - (self.clock() - self.last_request)))
            self.last_request = self.clock()
            self.requests += 1
            try:
                with self.open(request, timeout=30) as response:
                    raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise GeocodingFailure('RESPONSE_TOO_LARGE')
                return parse_response(raw)
            except urllib.error.HTTPError as exc:
                code = exc.code
                exc.close()
                if code not in {429, 500, 502, 503, 504}:
                    raise GeocodingFailure('HTTP_ERROR') from None
                error = 'HTTP_TEMPORARY_ERROR'
            except (urllib.error.URLError, OSError, TimeoutError):
                error = 'NETWORK_ERROR'
            except GeocodingFailure as exc:
                if str(exc) != 'API_TEMPORARY_ERROR':
                    raise
                error = str(exc)
            if attempt == 2:
                raise GeocodingFailure(error) from None
            self.sleep(2 ** attempt)

    def search(self, address):
        return self.request(SEARCH_URL, {'confmKey': self.search_key, 'keyword': address,
                            'currentPage': 1, 'countPerPage': 2, 'resultType': 'json'})

    def coordinates(self, candidate):
        return self.request(COORD_URL, {'confmKey': self.coord_key, 'resultType': 'json',
                                       **{key: candidate[key] for key in CODE_FIELDS}})


def parse_response(raw):
    try:
        result = json.loads(raw.decode('utf-8-sig'))['results']
        common = result['common']
        code = common['errorCode']
        if str(code) in {'-999', 'E0007'}:
            raise GeocodingFailure('API_TEMPORARY_ERROR')
        if str(code) in {'E0001', 'E0014'}:
            raise GeocodingFailure('API_AUTH_ERROR')
        if str(code) != '0':
            raise GeocodingFailure('API_REJECTED_REQUEST')
        total = common['totalCount']
        if isinstance(total, bool) or not re.fullmatch(r'[0-9]{1,9}', str(total)):
            raise ValueError()
        total = int(total)
        rows = result.get('juso')
        if total == 0 and rows is None:
            rows = []
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or len(rows) > total or (total > 0 and not rows)):
            raise ValueError()
        return total, rows
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise GeocodingFailure('INVALID_API_RESPONSE') from None


def normalize_address(value, *, road=False):
    if not isinstance(value, str):
        return ''
    value = ' '.join(value.split())
    if value.startswith('경기 '):
        value = '경기도 ' + value[3:]
    if road:
        # This geocoder resolves building entrances, not floors or indoor toilets.
        # Strip a comma-delimited building description only after recognizing a
        # complete road address. Keep the original stored address untouched.
        base, comma, description = value.partition(',')
        if comma and (re.match(r'\s*[0-9]+(?:-[0-9]+)?(?:\s|,|$)', description)
                      or re.search(r'(?:^|\s)[^\s,()]+(?:대로|로|길)\s*[0-9]', description)):
            return value  # A list of building numbers/addresses is not a label.
        base = base.strip()
        base = re.sub(r'\s*\([^()]*\)$', '', base).strip()
        base = re.sub(r'(대로|로|길)\s+(?=[0-9]+(?:번|안)?길)', r'\1', base)
        match = re.fullmatch(r'(.+\s)([^\s,()]+(?:대로|로|길))\s*([0-9]+(?:\s*-\s*[0-9]+)?)', base)
        if match:
            number = re.sub(r'\s+', '', match[3])
            return f'{match[1]}{match[2]} {number}'
        # Do not guess where an unrecognized address or building number ends.
        value = re.sub(r'\s*\([^()]*\)$', '', value).strip()
    return value


@dataclass(frozen=True)
class Result:
    status: str
    reason: str
    longitude: float | None = None
    latitude: float | None = None


def make_transformer():
    try:
        from pyproj import Transformer
        from pyproj.exceptions import ProjError
    except ImportError:
        raise GeocodingFailure('MISSING_PYPROJ') from None
    transformer = Transformer.from_crs('EPSG:5179', 'EPSG:4326', always_xy=True)
    def transform(x, y, **kwargs):
        try:
            return transformer.transform(x, y, **kwargs)
        except ProjError:
            raise ValueError('Invalid projected coordinate') from None
    return transform


def geocode(record, client, transform):
    addresses = [(record['address_road'], True), (record['address_lot'], False)]
    if not any(address for address, _ in addresses):
        return Result('invalid', 'MISSING_ADDRESS')
    candidate = None
    for address, road in addresses:
        if not address:
            continue
        total, rows = client.search(normalize_address(address, road=road))
        if total == 0:
            continue
        if total != 1 or len(rows) != 1:
            return Result('ambiguous', 'MULTIPLE_ADDRESSES')
        candidate = rows[0]
        returned = candidate.get('roadAddrPart1') if road else candidate.get('jibunAddr')
        if normalize_address(address, road=road) != normalize_address(returned, road=road):
            return Result('ambiguous', 'ADDRESS_MISMATCH')
        break
    if candidate is None:
        return Result('failed', 'ADDRESS_NOT_FOUND')
    if any(not isinstance(candidate.get(key), str) or not candidate[key].isdigit()
           for key in (*CODE_FIELDS, 'bdMgtSn')):
        raise GeocodingFailure('INVALID_API_RESPONSE')
    total, rows = client.coordinates(candidate)
    if total == 0:
        return Result('failed', 'COORDINATES_NOT_FOUND')
    if total != 1 or len(rows) != 1:
        return Result('ambiguous', 'MULTIPLE_ENTRANCES')
    coord = rows[0]
    if any(coord.get(key) != candidate[key] for key in (*CODE_FIELDS, 'bdMgtSn')):
        return Result('ambiguous', 'BUILDING_MISMATCH')
    try:
        x, y = coord['entX'], coord['entY']
        if isinstance(x, bool) or isinstance(y, bool):
            raise ValueError()
        x, y = float(x), float(y)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError()
        lon, lat = transform(x, y, errcheck=True)
        if not (math.isfinite(lon) and math.isfinite(lat) and 124 <= lon <= 132 and 32 <= lat <= 39):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError):
        return Result('invalid', 'INVALID_COORDINATES')
    return Result('valid', 'MATCHED_ENTRANCE', lon, lat)


def run(store, region_code, *, client=None, transform=None, limit=50, retry=False,
        dry_run=False, report=None):
    report = report if report is not None else {}
    report.update(status='running', started_at=utc_now(), provider=PROVIDER,
                  region_code=region_code, selected=0, saved=0, stale=0, cache_hits=0,
                  requests=0, outcomes={}, reasons={})
    try:
        rows = store.geocoding_candidates(region_code, limit=limit, retry=retry)
        report['selected'] = len(rows)
        if dry_run:
            report.update(status='planned', missing_address=sum(
                not (row['address_road'] or row['address_lot']) for row in rows))
            return report
        if client is None or transform is None:
            raise GeocodingFailure('MISSING_GEOCODER')
        cache = {}
        for row in rows:
            key = address_hash(row['address_road'], row['address_lot'])
            if key in cache:
                result = cache[key]
                report['cache_hits'] += 1
            else:
                result = geocode(row, client, transform)
                cache[key] = result
            if store.save_geocoding(row, status=result.status, longitude=result.longitude,
                                    latitude=result.latitude, provider=PROVIDER, timestamp=utc_now()):
                report['saved'] += 1
                for field, value in (('outcomes', result.status), ('reasons', result.reason)):
                    report[field][value] = report[field].get(value, 0) + 1
            else:
                report['stale'] += 1
        report['status'] = 'success'
        return report
    except GeocodingFailure as exc:
        report.update(status='failed', error=str(exc))
        raise
    except (OSError, ValueError, sqlite3.Error):
        report.update(status='failed', error='LOCAL_STORAGE_ERROR')
        raise GeocodingFailure('LOCAL_STORAGE_ERROR') from None
    finally:
        report.update(finished_at=utc_now(), requests=getattr(client, 'requests', 0))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-code', required=True)
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--limit', type=int, default=50)
    parser.add_argument('--max-requests', type=int, default=150)
    parser.add_argument('--retry', action='store_true', help='Include failed/invalid/ambiguous rows')
    parser.add_argument('--dry-run', action='store_true', help='Plan only: no API calls or DB writes')
    args = parser.parse_args(argv)
    if not re.fullmatch(r'[0-9]{7}', args.region_code) or not 1 <= args.limit <= 1000:
        parser.error('region-code must be 7 digits; limit must be between 1 and 1000')
    report = {'status': 'failed', 'started_at': utc_now()}
    log_dir = ROOT / '.cache/facilities/geocoding-runs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / (uuid.uuid4().hex + '.json')).open('x', encoding='utf-8') as log:
            try:
                client = transform = None
                if not args.dry_run:
                    settings = load_settings(args.env_file)
                    if not settings['JUSO_SEARCH_KEY'] or not settings['JUSO_COORD_KEY']:
                        raise GeocodingFailure('MISSING_JUSO_KEYS')
                    if settings['JUSO_STORAGE_APPROVED'] != 'true':
                        raise GeocodingFailure('STORAGE_PERMISSION_UNCONFIRMED')
                    transform = make_transformer()
                    client = JusoClient(settings['JUSO_SEARCH_KEY'], settings['JUSO_COORD_KEY'],
                                        max_requests=args.max_requests)
                run(FacilityStore(args.database), args.region_code, client=client, transform=transform,
                    limit=args.limit, retry=args.retry, dry_run=args.dry_run, report=report)
            except GeocodingFailure as exc:
                report.update(status='failed', error=str(exc), finished_at=utc_now())
            except (OSError, UnicodeError):
                report.update(status='failed', error='LOCAL_FILE_ERROR', finished_at=utc_now())
            json.dump(report, log, ensure_ascii=False, indent=2)
    except OSError:
        print('LOG_WRITE_ERROR: verify database state before rerunning.')
        return 1
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] in {'success', 'planned'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
