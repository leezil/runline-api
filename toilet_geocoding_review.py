"""Review unresolved toilets; optionally save only unique exact address matches."""
import argparse
from collections import Counter
import html
import json
from pathlib import Path
import re
import sqlite3
import uuid

from facility_storage import DATABASE, FacilityStore
from toilet_geocoding import (ROOT, CODE_FIELDS, GeocodingFailure, JusoClient,
                              load_settings, make_transformer, normalize_address,
                              resolve_coordinates, utc_now)

PROVIDER = 'juso-exact-review-v1'
LABELS = {
    'EXACT_MATCH': '정확한 주소 후보 1개 · 저장 전',
    'MATCHED_ENTRANCE': '좌표 확보', 'MISSING_ADDRESS': '주소 없음',
    'ADDRESS_NOT_FOUND': '검색 결과 없음', 'ADDRESS_MISMATCH': '정확한 주소 후보 없음',
    'MULTIPLE_EXACT_MATCHES': '정확한 주소 후보가 여러 개',
    'INCOMPLETE_CANDIDATES': '전체 후보 미수신 · 자동 처리 금지',
    'COORDINATES_NOT_FOUND': '출입구 좌표 없음', 'MULTIPLE_ENTRANCES': '출입구가 여러 개',
    'BUILDING_MISMATCH': '검색 건물과 좌표 건물이 다름',
    'INVALID_COORDINATES': '좌표 범위·형식 오류', 'STALE': '작업 중 원본 변경 · 저장 안 함',
}


def inspect_address(record, client):
    """Only a complete result set with exactly one normalized match qualifies.

    Search at most 100 candidates. Never infer uniqueness from a partial page.
    Lot fallback is allowed only after a zero-result road search, like v2.
    """
    review = {key: record[key] for key in ('id', 'name', 'source_region_code',
                                         'address_road', 'address_lot')}
    review.update(previous_status=record['coordinate_status'], searches=[], saved=False)
    for field, road in [('address_road', True), ('address_lot', False)]:
        if not record[field]:
            continue
        query = normalize_address(record[field], road=road)
        total, rows = client.search(query, count=100)
        exact = []
        candidates = []
        for row in rows:
            returned = row.get('roadAddrPart1' if road else 'jibunAddr')
            matches = bool(query) and query == normalize_address(returned, road=road)
            if matches:
                exact.append(row)
            # Explicit allowlist: no keys, provider messages or raw response dumps.
            candidate = {key: row.get(key) for key in ('roadAddrPart1', 'jibunAddr', 'bdNm', 'bdMgtSn')}
            candidate.update(exact=matches, normalized=normalize_address(returned, road=road))
            candidates.append(candidate)
        complete = total == len(rows)
        review['searches'].append(dict(field=field, query=query, total=total,
                                       complete=complete, exact_count=len(exact), candidates=candidates))
        if not complete:
            review['reason'] = 'INCOMPLETE_CANDIDATES'
            return review, None
        if total == 0:
            continue
        if len(exact) != 1:
            review['reason'] = 'MULTIPLE_EXACT_MATCHES' if exact else 'ADDRESS_MISMATCH'
            return review, None
        if any(not isinstance(exact[0].get(key), str) or not exact[0][key].isdigit()
               for key in (*CODE_FIELDS, 'bdMgtSn')):
            raise GeocodingFailure('INVALID_API_RESPONSE')
        review['reason'] = 'EXACT_MATCH'
        return review, exact[0]
    review['reason'] = 'ADDRESS_NOT_FOUND' if review['searches'] else 'MISSING_ADDRESS'
    return review, None


def write_html(path, summary, entries):
    escape = lambda value: html.escape(str(value if value is not None else ''))
    cards = []
    for entry in entries:
        reason = entry['reason']
        searches = []
        for search in entry['searches']:
            candidates = ''.join('<li>' + escape(c.get('roadAddrPart1')) + '<br>'
                                 + escape(c.get('jibunAddr')) + '<br>' + escape(c.get('bdNm'))
                                 + (' <strong>정확히 일치</strong>' if c['exact'] else '') + '</li>'
                                 for c in search['candidates'])
            searches.append(f'<p>검색어: {escape(search["query"])} · 전체 {search["total"]}건 · '
                            f'정확한 후보 {search["exact_count"]}건 · 전체 수신 {search["complete"]}</p>'
                            f'<ul>{candidates}</ul>')
        cards.append(f'<article data-reason="{escape(reason)}"><h2>{escape(entry["name"])}</h2>'
                     f'<p>{escape(LABELS.get(reason, reason))} · 저장 {entry["saved"]}</p>'
                     f'<p>{escape(entry["source_region_code"])} · {escape(entry["id"])}</p>'
                     f'<p>원본 도로명: {escape(entry["address_road"])}<br>원본 지번: {escape(entry["address_lot"])}</p>'
                     '<details><summary>검색 후보 비교</summary>' + ''.join(searches) + '</details></article>')
    options = ''.join(f'<option value="{escape(reason)}">{escape(LABELS.get(reason, reason))} ({count})</option>'
                      for reason, count in summary['reasons'].items())
    document = '''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>공중화장실 좌표 검토</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:32px auto;padding:0 20px;background:#f3f7f5;color:#17382d}article{padding:20px;margin:16px 0;background:white;border:1px solid #cbdcd3;border-radius:12px}h2{font-size:19px}input,select{padding:10px;margin:5px}li{padding:8px;border-bottom:1px solid #ddd}p,li{overflow-wrap:anywhere}[hidden]{display:none}</style>
<h1>공중화장실 좌표 검토</h1><p>저장된 실행 시점의 보고서입니다. 이 화면은 읽기 전용이며 후보를 클릭해 강제로 저장하는 기능은 없습니다.</p>'''
    document += f'<p>상태: {escape(summary["status"])} · 완료 {summary["processed"]}/{summary["selected"]} · 좌표 저장 {summary["saved"]} · {escape(summary["started_at"])}</p>'
    document += f'<label>검색 <input id="query" placeholder="시설명 또는 주소"></label><label>분류 <select id="reason"><option value="">전체</option>{options}</select></label><p id="count"></p>'
    document += ''.join(cards) + '''<script>
function filter(){let n=0;const q=document.querySelector('#query').value.toLowerCase(),r=document.querySelector('#reason').value;document.querySelectorAll('article').forEach(a=>{a.hidden=!(a.textContent.toLowerCase().includes(q)&&(!r||a.dataset.reason===r));if(!a.hidden)n++});document.querySelector('#count').textContent=n+'건 표시'}document.querySelector('#query').addEventListener('input',filter);document.querySelector('#reason').addEventListener('change',filter);filter();
</script></html>'''
    path.write_text(document, encoding='utf-8')


def run_review(store, regions, client, *, transform=None, apply=False, output):
    output.mkdir(parents=True, exist_ok=False)
    records = [row for region in regions for row in store.geocoding_candidates(region, limit=1000, retry=True)
               if row['coordinate_status'] != 'pending']
    summary = dict(status='running', started_at=utc_now(), selected=len(records), processed=0,
                   saved=0, requests=0, apply=apply, reasons={})
    entries, counts = [], Counter()
    try:
        with (output / 'entries.jsonl').open('x', encoding='utf-8') as log:
            for row in records:
                entry, candidate = inspect_address(row, client)
                if apply and candidate is not None:
                    result = resolve_coordinates(candidate, client, transform)
                    entry['reason'] = result.reason
                    if result.status == 'valid':
                        entry['saved'] = store.save_geocoding(row, status='valid', longitude=result.longitude,
                            latitude=result.latitude, provider=PROVIDER, timestamp=utc_now())
                        if not entry['saved']:
                            entry['reason'] = 'STALE'
                # Unresolved rows stay untouched. The report records finer reasons.
                entries.append(entry)
                counts[entry['reason']] += 1
                summary['processed'] += 1
                summary['saved'] += int(entry['saved'])
                log.write(json.dumps(entry, ensure_ascii=False) + '\n'); log.flush()
                if summary['processed'] % 10 == 0:
                    print(json.dumps({'processed': summary['processed'], 'selected': len(records),
                                      'saved': summary['saved']}), flush=True)
        summary['status'] = 'success'
    except GeocodingFailure as error:
        summary.update(status='failed', error=str(error))
    except KeyboardInterrupt:
        summary.update(status='interrupted', error='INTERRUPTED')
    except (OSError, ValueError, TypeError, sqlite3.Error):
        summary.update(status='failed', error='LOCAL_REVIEW_ERROR')
    finally:
        summary.update(finished_at=utc_now(), requests=client.requests, reasons=dict(counts))
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        write_html(output / 'index.html', summary, entries)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--region-code', action='append', required=True)
    parser.add_argument('--database', type=Path, default=DATABASE)
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--max-requests', type=int, default=1500)
    parser.add_argument('--apply', action='store_true', help='Save validated unique exact matches')
    args = parser.parse_args(argv)
    if any(not re.fullmatch(r'[0-9]{7}', region) for region in args.region_code):
        parser.error('region-code must be 7 digits')
    output = ROOT / '.cache/facilities/geocoding-reviews' / uuid.uuid4().hex
    try:
        settings = load_settings(args.env_file)
        if args.apply and settings['JUSO_STORAGE_APPROVED'] != 'true':
            raise GeocodingFailure('STORAGE_PERMISSION_UNCONFIRMED')
        client = JusoClient(settings['JUSO_SEARCH_KEY'], settings['JUSO_COORD_KEY'], max_requests=args.max_requests)
        summary = run_review(FacilityStore(args.database), list(dict.fromkeys(args.region_code)), client,
                             transform=make_transformer() if args.apply else None,
                             apply=args.apply, output=output)
        print(json.dumps(summary, ensure_ascii=True))
        print('Review directory:', output)
        return 0 if summary['status'] == 'success' else 1
    except GeocodingFailure as error:
        print(str(error)); return 1
    except (OSError, sqlite3.Error):
        print('LOCAL_REVIEW_ERROR'); return 1


if __name__ == '__main__':
    raise SystemExit(main())
