"""Offline facility persistence. Input is normalized data, not provider responses."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid


DATABASE = Path(__file__).resolve().parent / '.cache/facilities/facilities.sqlite3'
SOURCES = {'toilet': 'mois_toilet_v2', 'cctv': 'mois_cctv'}
STATUSES = {'valid', 'pending', 'ambiguous', 'invalid', 'failed'}
APPLICATION_ID = 1381322310

# Optional additive metadata: old v1 readers remain compatible. API reads never
# create/migrate databases; importers create this table on their next write.
_IMPORT_SCHEMA = '''CREATE TABLE IF NOT EXISTS facility_import_state (
    source TEXT NOT NULL, region_code TEXT NOT NULL,
    last_success_at TEXT, last_attempt_at TEXT NOT NULL,
    last_attempt_status TEXT NOT NULL, PRIMARY KEY(source, region_code))'''


@dataclass
class ToiletDetails:
    category_raw: str | None = None
    opening_hours_text: str | None = None
    opening_hours_detail: str | None = None
    male_toilet_count: int | None = None
    male_urinal_count: int | None = None
    female_toilet_count: int | None = None
    male_accessible_toilet_count: int | None = None
    male_accessible_urinal_count: int | None = None
    female_accessible_toilet_count: int | None = None
    emergency_bell: bool | None = None
    diaper_changing_table: bool | None = None


@dataclass
class CctvDetails:
    purpose_raw: str | None = None
    camera_count: int | None = None
    # Only an importer with a verified purpose mapping may set this to True.
    crime_prevention: bool | None = None


@dataclass
class Facility:
    kind: str
    source: str
    source_region_code: str
    source_record_id: str
    name: str | None = None
    address_road: str | None = None
    address_lot: str | None = None
    manager_name: str | None = None
    source_date: str | None = None
    source_modified_at: str | None = None
    source_updated_at: str | None = None
    source_change_type: str | None = None
    fetched_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    longitude: float | None = None
    latitude: float | None = None
    coordinate_source: str = 'none'
    coordinate_status: str = 'pending'
    geocoder_provider: str | None = None
    geocoded_at: str | None = None
    geocode_address_hash: str | None = None
    active: bool = True
    details: ToiletDetails | CctvDetails = field(default_factory=ToiletDetails)


def address_hash(address_road, address_lot):
    """Fingerprint of both trimmed addresses; no address is sent to a provider."""
    addresses = [value.strip() or None if value is not None else None
                 for value in (address_road, address_lot)]
    return hashlib.sha256(json.dumps(addresses, ensure_ascii=False).encode('utf-8')).hexdigest()


def _utc_timestamp(value, label):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError(f'{label} must be a UTC ISO 8601 timestamp') from None


def _number(value, low, high, label):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not low <= value <= high):
        raise ValueError(f'{label} must be finite and between {low} and {high}')


def _normalize(record):
    if not isinstance(record, Facility):
        raise ValueError('Expected a Facility')
    if record.kind not in SOURCES or record.source != SOURCES[record.kind]:
        raise ValueError('Unsupported kind/source pair')
    expected = ToiletDetails if record.kind == 'toilet' else CctvDetails
    if not isinstance(record.details, expected):
        raise ValueError('Details do not match facility kind')
    data = asdict(record)
    for key in ('source_region_code', 'source_record_id'):
        if not isinstance(data[key], str) or not data[key].strip():
            raise ValueError(f'{key} is required')
    for key, value in data.items():
        if key not in {'longitude', 'latitude', 'active', 'details'}:
            if value is not None and not isinstance(value, str):
                raise ValueError(f'{key} must be text or None')
            data[key] = value.strip() or None if value is not None else None
    _utc_timestamp(data['fetched_at'], 'fetched_at')
    if type(data['active']) is not bool:
        raise ValueError('active must be a boolean')
    if data['coordinate_status'] not in STATUSES:
        raise ValueError('Unsupported coordinate_status')
    if data['coordinate_source'] not in {'source', 'geocoder', 'none'}:
        raise ValueError('Unsupported coordinate_source')
    lon, lat = data['longitude'], data['latitude']
    if (lon is None) != (lat is None):
        raise ValueError('Coordinates must be supplied together')
    if lon is not None:
        _number(lon, 124, 132, 'longitude')
        _number(lat, 32, 39, 'latitude')
    if data['coordinate_status'] == 'valid' and (lon is None or data['coordinate_source'] == 'none'):
        raise ValueError('Valid coordinates require a location and provenance')
    if data['coordinate_source'] == 'none' and lon is not None:
        raise ValueError('Coordinates require provenance')
    if data['coordinate_source'] == 'geocoder':
        if not all(data[key] for key in ('geocoder_provider', 'geocoded_at', 'geocode_address_hash')):
            raise ValueError('Geocoded coordinates require provider, timestamp and address hash')
        _utc_timestamp(data['geocoded_at'], 'geocoded_at')
        if not (data['address_road'] or data['address_lot']):
            raise ValueError('Geocoded coordinates require an address')
        if data['geocode_address_hash'] != address_hash(data['address_road'], data['address_lot']):
            # A full-record update with a changed address cannot retain old coordinates.
            data.update(longitude=None, latitude=None, coordinate_source='none',
                        coordinate_status='pending', geocoder_provider=None,
                        geocoded_at=None, geocode_address_hash=None)
    elif any(data[key] is not None for key in ('geocoder_provider', 'geocoded_at', 'geocode_address_hash')):
        raise ValueError('Geocoder metadata requires coordinate_source=geocoder')
    for key, value in data['details'].items():
        if key.endswith('_count'):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{key} must be a nonnegative integer or None')
        elif key in {'emergency_bell', 'diaper_changing_table', 'crime_prevention'}:
            if value is not None and type(value) is not bool:
                raise ValueError(f'{key} must be a boolean or None')
        else:
            if value is not None and not isinstance(value, str):
                raise ValueError(f'{key} must be text or None')
            data['details'][key] = value.strip() or None if value is not None else None
    data['details'] = json.dumps(data['details'], ensure_ascii=False, allow_nan=False)
    return data


_SCHEMA = """
CREATE TABLE facilities (
    row_id INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK(kind IN ('toilet', 'cctv')),
    source TEXT NOT NULL,
    source_region_code TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    name TEXT, address_road TEXT, address_lot TEXT, manager_name TEXT,
    source_date TEXT, source_modified_at TEXT, source_updated_at TEXT,
    source_change_type TEXT, fetched_at TEXT NOT NULL,
    longitude REAL, latitude REAL,
    coordinate_source TEXT NOT NULL,
    coordinate_status TEXT NOT NULL,
    geocoder_provider TEXT, geocoded_at TEXT, geocode_address_hash TEXT,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    details TEXT NOT NULL,
    UNIQUE(source, source_region_code, source_record_id)
);
CREATE VIRTUAL TABLE facility_bounds USING rtree(row_id, minx, maxx, miny, maxy);
CREATE TRIGGER facilities_insert AFTER INSERT ON facilities
WHEN NEW.active=1 AND NEW.coordinate_status='valid'
BEGIN
    INSERT INTO facility_bounds VALUES(NEW.row_id, NEW.longitude, NEW.longitude, NEW.latitude, NEW.latitude);
END;
CREATE TRIGGER facilities_update AFTER UPDATE ON facilities
BEGIN
    DELETE FROM facility_bounds WHERE row_id=OLD.row_id;
    INSERT INTO facility_bounds
    SELECT NEW.row_id, NEW.longitude, NEW.longitude, NEW.latitude, NEW.latitude
    WHERE NEW.active=1 AND NEW.coordinate_status='valid';
END;
CREATE TRIGGER facilities_delete AFTER DELETE ON facilities
BEGIN
    DELETE FROM facility_bounds WHERE row_id=OLD.row_id;
END;
PRAGMA user_version=1;
PRAGMA application_id=1381322310;
"""


class FacilityStore:
    """Each operation owns a connection. Batches commit completely or roll back."""

    def __init__(self, path=DATABASE):
        self.path = Path(path).resolve()

    @contextmanager
    def _connect(self, *, readonly=False):
        mode = 'ro' if readonly else 'rw'
        with closing(sqlite3.connect(self.path.as_uri() + f'?mode={mode}', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if (db.execute('PRAGMA user_version').fetchone()[0] != 1
                    or db.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID):
                raise ValueError('Unsupported facility schema; initialize or migrate explicitly')
            with db:
                yield db

    def initialize(self):
        """Explicit and idempotent; refuse to adopt an unrelated or newer DB."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            if version == 1 and app_id == APPLICATION_ID:
                return
            if version != 0 or app_id != 0 or db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                raise ValueError('Unsupported or unrelated database')
            # executescript otherwise commits a pending transaction before executing.
            try:
                db.executescript('BEGIN IMMEDIATE;\n' + _SCHEMA + '\nCOMMIT;')
            except Exception:
                db.rollback()
                raise

    def upsert_many(self, records, *, preserve_local_state=False, import_state=None):
        """Replace full records by source identity; omitted fields use model defaults.

        No missing-record deletion, source-code interpretation or geocoding occurs.
        Importers can preserve active state and coordinates for unchanged addresses.
        Returns stable IDs in input order. Validation failures roll back the batch.
        """
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._upsert_records(db, records, preserve_local_state=preserve_local_state,
                                        import_state=import_state)

    def _upsert_records(self, db, records, *, preserve_local_state=False, import_state=None):
        ids = []
        columns = [item.name for item in fields(Facility)]
        updates = ','.join(f'{key}=excluded.{key}' for key in columns)
        sql = (f"INSERT INTO facilities(id,{','.join(columns)}) "
               f"VALUES({','.join('?' for _ in range(len(columns)+1))}) "
               f"ON CONFLICT(source,source_region_code,source_record_id) DO UPDATE SET {updates}")
        for record in records:
            data = _normalize(record)
            if preserve_local_state:
                old = db.execute('SELECT * FROM facilities WHERE source=? AND source_region_code=? AND source_record_id=?',
                                 [data[key] for key in ('source', 'source_region_code', 'source_record_id')]).fetchone()
                if old:
                    data['active'] = old['active']
                    if (data['coordinate_source'] == 'none' and data['coordinate_status'] == 'pending'
                            and data['address_road'] == old['address_road']
                            and data['address_lot'] == old['address_lot']):
                        for key in ('longitude', 'latitude', 'coordinate_source', 'coordinate_status',
                                    'geocoder_provider', 'geocoded_at', 'geocode_address_hash'):
                            data[key] = old[key]
            db.execute(sql, [uuid.uuid4().hex] + [data[key] for key in columns])
            row = db.execute('SELECT id FROM facilities WHERE source=? AND source_region_code=? AND source_record_id=?',
                             [data[key] for key in ('source', 'source_region_code', 'source_record_id')]).fetchone()
            ids.append(row['id'])
        if import_state is not None:
            source, region, timestamp = import_state
            self._write_import_state(db, source, region, timestamp, 'success')
        return ids

    @staticmethod
    def _write_import_state(db, source, region, timestamp, status):
        if source not in SOURCES.values() or not isinstance(region, str) or not region:
            raise ValueError('Invalid import identity')
        _utc_timestamp(timestamp, 'import timestamp')
        db.execute(_IMPORT_SCHEMA)
        db.execute('''INSERT INTO facility_import_state VALUES(?,?,?,?,?)
            ON CONFLICT(source,region_code) DO UPDATE SET
            last_success_at=CASE WHEN excluded.last_attempt_status='success'
                THEN excluded.last_success_at ELSE facility_import_state.last_success_at END,
            last_attempt_at=excluded.last_attempt_at,last_attempt_status=excluded.last_attempt_status''',
            (source, region, timestamp if status == 'success' else None, timestamp, status))

    def record_import_failure(self, source, region, timestamp, *, expected_refresh=None):
        """Do not create a missing DB just to log a failed initial import."""
        if not self.path.is_file():
            return
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if expected_refresh is not None and self._refresh_snapshot(db, source, region)['fingerprint'] != expected_refresh:
                return False
            self._write_import_state(db, source, region, timestamp, 'failed')
            return True

    @staticmethod
    def _refresh_snapshot(db, source, region):
        if source not in SOURCES.values() or not isinstance(region, str) or not region:
            raise ValueError('INVALID_REFRESH_SCOPE')
        rows = [dict(row) for row in db.execute('''SELECT * FROM facilities
            WHERE source=? AND source_region_code=? ORDER BY id''', (source, region))]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        state = []
        if 'facility_import_state' in tables:
            state = [dict(row) for row in db.execute('SELECT * FROM facility_import_state WHERE source=? AND region_code=?', (source, region))]
        presence = {}
        if 'facility_refresh_presence' in tables:
            presence = {row['facility_id']: dict(row) for row in db.execute('''SELECT p.* FROM facility_refresh_presence p
                JOIN facilities f ON f.id=p.facility_id WHERE f.source=? AND f.source_region_code=?
                ORDER BY p.facility_id''', (source, region))}
        checkpoint = None
        if 'facility_refresh_state' in tables:
            row = db.execute('SELECT * FROM facility_refresh_state WHERE source=? AND region_code=?', (source, region)).fetchone()
            checkpoint = dict(row) if row else None
        payload = {'rows': rows, 'import_state': state, 'presence': presence, 'checkpoint': checkpoint}
        payload['fingerprint'] = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        return payload

    def refresh_snapshot(self, source, region):
        """Optimistic concurrency token includes local edits, imports and checkpoints."""
        with self._connect(readonly=True) as db:
            db.execute('BEGIN')
            return self._refresh_snapshot(db, source, region)

    def apply_refresh(self, records, source, region, timestamp, expected_fingerprint, *,
                      allow_empty=False, max_missing_ratio=0.2, dry_run=False):
        """Apply a complete listing + missing evidence + checkpoint atomically.

        Callers must validate all pages before calling. No source deletion code
        is interpreted. Two observations at least 24h apart hide missing rows.
        """
        _utc_timestamp(timestamp, 'refresh timestamp')
        if (type(allow_empty) is not bool or type(dry_run) is not bool
                or isinstance(max_missing_ratio, bool) or not isinstance(max_missing_ratio, (int, float))
                or not math.isfinite(max_missing_ratio) or not 0 <= max_missing_ratio <= 1):
            raise ValueError('INVALID_REFRESH_OPTIONS')
        records = list(records)
        incoming = {}
        for record in records:
            normalized = _normalize(record)
            if normalized['source'] != source or normalized['source_region_code'] != region:
                raise ValueError('REFRESH_SCOPE_MISMATCH')
            key = normalized['source_record_id']
            if key in incoming:
                raise ValueError('DUPLICATE_RECORD')
            incoming[key] = normalized
        now = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        with self._connect(readonly=dry_run) as db:
            db.execute('BEGIN' if dry_run else 'BEGIN IMMEDIATE')
            snapshot = self._refresh_snapshot(db, source, region)
            if snapshot['fingerprint'] != expected_fingerprint:
                raise ValueError('REFRESH_BASELINE_CHANGED')
            checkpoint = snapshot['checkpoint']
            if checkpoint and now < datetime.fromisoformat(checkpoint['checkpoint_at'].replace('Z', '+00:00')):
                raise ValueError('REFRESH_CLOCK_REVERSED')
            old = {row['source_record_id']: row for row in snapshot['rows']}
            presence = snapshot['presence']
            prior = {key for key, row in old.items() if presence.get(row['id'], {}).get('missing_count', 0) == 0}
            missing = set(old) - set(incoming)
            new_missing = prior - set(incoming)
            if old and not incoming and not allow_empty:
                raise ValueError('EMPTY_SNAPSHOT_BLOCKED')
            if prior and len(new_missing) / len(prior) > max_missing_ratio:
                raise ValueError('EXCESSIVE_MISSING_BLOCKED')
            disabled, restored = [], []
            evidence = {}
            for key in missing:
                row = old[key]
                previous = presence.get(row['id'], {})
                first = previous.get('first_missing_at') or timestamp
                count = previous.get('missing_count', 0) + 1
                auto = previous.get('auto_disabled', 0)
                age = (now - datetime.fromisoformat(first.replace('Z', '+00:00'))).total_seconds()
                if row['active'] and count >= 2 and age >= 86400:
                    disabled.append(row['id'])
                    auto = 1
                evidence[row['id']] = (first, count, auto)
            for key in set(incoming) & set(old):
                row = old[key]
                if presence.get(row['id'], {}).get('auto_disabled') and not row['active']:
                    restored.append(row['id'])
            generation = (snapshot['checkpoint'] or {}).get('generation', 0) + 1
            report = {'new': len(set(incoming)-set(old)), 'refreshed': len(set(incoming)&set(old)),
                      'missing': len(missing), 'new_missing': len(new_missing),
                      'deactivated': len(disabled), 'reactivated': len(restored),
                      'generation': generation, 'checkpoint_at': timestamp}
            if dry_run:
                return report
            db.execute('''CREATE TABLE IF NOT EXISTS facility_refresh_presence (
                facility_id TEXT PRIMARY KEY, first_missing_at TEXT,
                missing_count INTEGER NOT NULL, auto_disabled INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS facility_refresh_state (
                source TEXT NOT NULL, region_code TEXT NOT NULL, generation INTEGER NOT NULL,
                checkpoint_at TEXT NOT NULL, PRIMARY KEY(source,region_code))''')
            ids = self._upsert_records(db, records, preserve_local_state=True,
                                       import_state=(source, region, timestamp))
            for identity in ids:
                db.execute('INSERT OR REPLACE INTO facility_refresh_presence VALUES(?,NULL,0,0)', (identity,))
            for identity, values in evidence.items():
                db.execute('INSERT OR REPLACE INTO facility_refresh_presence VALUES(?,?,?,?)', (identity, *values))
            db.executemany('UPDATE facilities SET active=0 WHERE id=?', [(identity,) for identity in disabled])
            db.executemany('UPDATE facilities SET active=1 WHERE id=?', [(identity,) for identity in restored])
            db.execute('''INSERT OR REPLACE INTO facility_refresh_state VALUES(?,?,?,?)''',
                       (source, region, generation, timestamp))
            return report

    def set_active(self, facility_id, active):
        """Explicit local review overrides automatic absence/reappearance handling."""
        if type(active) is not bool:
            raise ValueError('active must be boolean')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute('UPDATE facilities SET active=? WHERE id=?', (active, facility_id)).rowcount
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='facility_refresh_presence'").fetchone():
                db.execute('UPDATE facility_refresh_presence SET auto_disabled=0 WHERE facility_id=?', (facility_id,))
            return bool(changed)

    @staticmethod
    def _import_status(db, kinds):
        has_state = db.execute("SELECT 1 FROM sqlite_master WHERE name='facility_import_state' AND type='table'").fetchone()
        result = {}
        for kind in kinds:
            source = SOURCES[kind]
            regions = {row['source_region_code']: {
                'region_code': row['source_region_code'], 'last_fetched_at': row['fetched'],
                'last_success_at': None, 'last_attempt_at': None,
                'last_attempt_status': 'unknown', 'ready': True}
                for row in db.execute('''SELECT source_region_code,MAX(fetched_at) AS fetched
                    FROM facilities WHERE source=? GROUP BY source_region_code''', (source,))}
            if has_state:
                for row in db.execute('SELECT * FROM facility_import_state WHERE source=?', (source,)):
                    region = regions.setdefault(row['region_code'], {
                        'region_code': row['region_code'], 'last_fetched_at': None, 'ready': False})
                    region.update(last_success_at=row['last_success_at'], last_attempt_at=row['last_attempt_at'],
                                  last_attempt_status=row['last_attempt_status'])
                    region['ready'] = region['ready'] or row['last_success_at'] is not None
            values = [regions[key] for key in sorted(regions)]
            ready = any(row['ready'] for row in values)
            # Conservative watermark across all known regions; old data is honest unknown.
            times = [row['last_success_at'] for row in values]
            result[kind] = {'source': source, 'ready': ready, 'regions': values,
                            'data_updated_at': min(times) if times and all(times) else None,
                            'last_import_failed': any(row['last_attempt_status'] == 'failed' for row in values)}
        return result

    @staticmethod
    def _decode(row):
        result = dict(row)
        result.pop('row_id')
        result['active'] = bool(result['active'])
        result['details'] = json.loads(result['details'])
        return result

    def facility_status(self):
        """One read-only snapshot of import state and counts, including empty scopes."""
        def empty_counts():
            return {'stored': 0, 'active': 0, 'inactive': 0, 'map_ready': 0,
                    'coordinates': {status: 0 for status in sorted(STATUSES)},
                    'active_coordinates': {status: 0 for status in sorted(STATUSES)}}

        with self._connect(readonly=True) as db:
            db.execute('BEGIN')
            statuses = self._import_status(db, tuple(SOURCES))
            regions = {}
            for kind, state in statuses.items():
                state['counts'] = empty_counts()
                for region in state['regions']:
                    region['counts'] = empty_counts()
                    regions[kind, region['region_code']] = region
            # Do not require SQLite JSON1. Stream only fields needed for counts.
            for row in db.execute('''SELECT kind,source_region_code,active,
                    coordinate_status,details FROM facilities'''):
                counts = regions[row['kind'], row['source_region_code']]['counts']
                active = bool(row['active'])
                coordinate = row['coordinate_status']
                map_ready = active and coordinate == 'valid'
                if map_ready and row['kind'] == 'cctv':
                    map_ready = json.loads(row['details'])['crime_prevention'] is True
                for target in (counts, statuses[row['kind']]['counts']):
                    target['stored'] += 1
                    target['active' if active else 'inactive'] += 1
                    target['map_ready'] += int(map_ready)
                    target['coordinates'][coordinate] += 1
                    if active:
                        target['active_coordinates'][coordinate] += 1
            return {'sources': [{'type': kind, **state} for kind, state in statuses.items()]}

    def get(self, facility_id):
        """Internal lookup includes pending, invalid and inactive records."""
        with self._connect(readonly=True) as db:
            row = db.execute('SELECT * FROM facilities WHERE id=?', (facility_id,)).fetchone()
            return self._decode(row) if row else None

    def geocoding_candidates(self, region_code, *, limit=50, retry=False):
        """Bounded snapshot; valid/source coordinates and inactive rows are excluded."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        statuses = ('pending', 'failed', 'invalid', 'ambiguous') if retry else ('pending',)
        with self._connect(readonly=True) as db:
            rows = db.execute(f"""SELECT * FROM facilities
                WHERE kind='toilet' AND active=1 AND source_region_code=?
                  AND coordinate_source IN ('none','geocoder')
                  AND coordinate_status IN ({','.join('?' for _ in statuses)})
                ORDER BY id LIMIT ?""", (region_code, *statuses, limit))
            return [self._decode(row) for row in rows]

    def save_geocoding(self, snapshot, *, status, longitude=None, latitude=None,
                      provider, timestamp):
        """Update only coordinate fields if the snapshot is still current.

        A concurrent import/address edit, deactivation or another geocoder wins.
        Network calls must happen outside this short write transaction.
        """
        if status not in {'valid', 'failed', 'invalid', 'ambiguous'}:
            raise ValueError('Unsupported geocoding result')
        if status != 'valid' and (longitude is not None or latitude is not None):
            raise ValueError('Unresolved result cannot contain coordinates')
        keys = ('longitude', 'latitude', 'coordinate_source', 'coordinate_status',
                'geocoder_provider', 'geocoded_at', 'geocode_address_hash')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM facilities WHERE id=?', (snapshot['id'],)).fetchone()
            if row is None:
                return False
            current = self._decode(row)
            if (current != snapshot or not current['active'] or current['kind'] != 'toilet'
                    or current['coordinate_status'] == 'valid'
                    or current['coordinate_source'] == 'source'):
                return False
            values = {key: value for key, value in current.items() if key != 'id'}
            values['details'] = ToiletDetails(**values['details'])
            has_address = bool(current['address_road'] or current['address_lot'])
            values.update(longitude=longitude, latitude=latitude, coordinate_status=status,
                          coordinate_source='geocoder' if has_address else 'none',
                          geocoder_provider=provider if has_address else None,
                          geocoded_at=timestamp if has_address else None,
                          geocode_address_hash=address_hash(current['address_road'], current['address_lot'])
                          if has_address else None)
            data = _normalize(Facility(**values))
            db.execute(f"UPDATE facilities SET {','.join(key+'=?' for key in keys)} WHERE id=?",
                       (*[data[key] for key in keys], current['id']))
            return True

    def query_bounds(self, west, south, east, north, *, kinds=('toilet', 'cctv'), limit=200,
                     include_import_status=False):
        """Return active map-ready records; this is not the future HTTP response.

        R-tree gives candidates; exact REAL comparisons remove float32 expansion.
        HTTP distance limits and ingestion freshness belong to later API work.
        """
        for value, low, high, name in ((west,124,132,'west'), (east,124,132,'east'),
                                        (south,32,39,'south'), (north,32,39,'north')):
            _number(value, low, high, name)
        if west >= east or south >= north:
            raise ValueError('Bounds must have positive width and height')
        if isinstance(kinds, str):
            raise ValueError('kinds must be a collection')
        kinds = tuple(kinds)
        if not kinds or any(kind not in SOURCES for kind in kinds):
            raise ValueError('Unsupported or empty kinds')
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('limit must be an integer between 1 and 500')
        with self._connect(readonly=True) as db:
            db.execute('BEGIN')
            # Purpose stays in the normalized detail model, without relying on JSON1.
            rows = db.execute(f"""
                SELECT f.* FROM facility_bounds b JOIN facilities f ON f.row_id=b.row_id
                WHERE b.minx<=? AND b.maxx>=? AND b.miny<=? AND b.maxy>=?
                  AND f.longitude>=? AND f.longitude<=? AND f.latitude>=? AND f.latitude<=?
                  AND f.active=1 AND f.coordinate_status='valid'
                  AND f.kind IN ({','.join('?' for _ in kinds)})
                ORDER BY f.id
            """, (east,west,north,south,west,east,south,north,*kinds))
            items = []
            for row in rows:
                item = self._decode(row)
                if item['kind'] == 'cctv' and item['details']['crime_prevention'] is not True:
                    continue
                items.append(item)
                if len(items) > limit:
                    break
            result = {'items': items[:limit], 'count': min(len(items), limit), 'truncated': len(items) > limit}
            if include_import_status:
                result['import_status'] = self._import_status(db, kinds)
            return result
