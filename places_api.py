"""Read-only facility API contract; never calls a provider or initializes a DB."""
import math
import re
import sqlite3
from urllib.parse import parse_qsl

from facility_storage import FacilityStore, SOURCES


class PlacesError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def validate_query(query):
    try:
        if len(query) > 2048 or re.search(r'%(?![0-9a-fA-F]{2})', query):
            raise ValueError()
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=6, errors='strict')
        params = dict(pairs)
        if len(params) != len(pairs) or set(params) - {'west', 'south', 'east', 'north', 'types', 'limit'}:
            raise ValueError()
        bounds = tuple(float(params[key]) for key in ('west', 'south', 'east', 'north'))
        west, south, east, north = bounds
        if (not all(math.isfinite(value) for value in bounds)
                or not 124 <= west < east <= 132 or not 32 <= south < north <= 39):
            raise ValueError()
        kinds = tuple(params.get('types', 'toilet,cctv').split(','))
        if not kinds or len(set(kinds)) != len(kinds) or any(kind not in SOURCES for kind in kinds):
            raise ValueError()
        text = params.get('limit', '200')
        if not re.fullmatch(r'[0-9]{1,3}', text) or not 1 <= int(text) <= 500:
            raise ValueError()
        radius = 6371008.8
        height = radius * math.radians(north - south)
        # The south edge is the widest edge at Korean latitudes.
        width = 2 * radius * math.asin(math.cos(math.radians(south)) * math.sin(math.radians(east-west)/2))
        if width > 10000 or height > 10000:
            raise PlacesError('BOUNDS_TOO_LARGE', '지도 조회 범위는 가로·세로 각각 10km 이하여야 합니다.')
        return bounds, kinds, int(text)
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PlacesError('INVALID_INPUT', '지도 경계, 시설 종류 또는 limit을 확인해주세요.') from None


def public_item(row):
    result = {key: row[key] for key in ('id', 'name', 'source', 'source_date', 'coordinate_source')}
    result.update(type=row['kind'], location=[row['longitude'], row['latitude']],
                  address={'road': row['address_road'], 'lot': row['address_lot']})
    if row['kind'] == 'toilet':
        result['details'] = {key: row['details'][key] for key in ('opening_hours_text', 'opening_hours_detail')}
    else:
        result['details'] = {key: row['details'][key] for key in ('purpose_raw', 'camera_count')}
    return result


def query_places(store, validated):
    bounds, kinds, limit = validated
    try:
        data = store.query_bounds(*bounds, kinds=kinds, limit=limit, include_import_status=True)
        statuses = data['import_status']
        if any(not statuses[kind]['ready'] for kind in kinds):
            raise PlacesError('FACILITIES_NOT_READY', '요청한 시설 종류의 수집 데이터가 아직 준비되지 않았습니다.', 503)
        return {'items': [public_item(row) for row in data['items']], 'count': data['count'],
                'truncated': data['truncated'],
                'data_updated_at': {kind: statuses[kind]['data_updated_at'] for kind in kinds},
                'sources': [{'type': kind, **statuses[kind]} for kind in kinds]}
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise PlacesError('FACILITIES_UNAVAILABLE', '시설 저장소를 읽을 수 없습니다.', 503) from None


def query_facility_status(store):
    try:
        return store.facility_status()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        raise PlacesError('FACILITIES_UNAVAILABLE', '시설 저장소를 읽을 수 없습니다.', 503) from None
