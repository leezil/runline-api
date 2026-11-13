# 공중화장실 좌표 실패 검토

`toilet_geocoding_review.py`는 `ambiguous`, `failed`, `invalid`인 활성 화장실을 검토한다. 기존 유효 좌표, 비활성 시설, 신규 `pending` 시설은 제외한다. 지역별 최대 1,000건을 대상으로 하며 지역 코드를 반복 지정할 수 있다.

## 실행

```powershell
# 검색 후보만 검토: 주소 검색 API 호출, DB 변경 없음
python toilet_geocoding_review.py --region-code 3740000 --region-code 3830000

# SQLite 백업 후 정확한 후보만 좌표 재처리
python facility_backup.py backup
python toilet_geocoding_review.py --region-code 3740000 --region-code 3830000 --apply
```

`.env`의 `JUSO_SEARCH_KEY`, `JUSO_COORD_KEY`를 사용한다. `--apply`는 기존 도구와 동일하게 `JUSO_STORAGE_APPROVED=true` 설정과 pyproj가 필요하다. 저장 조건 확인 및 활성화 의미는 [좌표 변환 안내](TOILET_GEOCODING.md)를 따른다. `--database`, `--env-file`, `--max-requests`를 지정할 수 있다. 기본 실제 HTTP 호출 예산은 1,500회이며 재시도도 포함한다. 호출 간격은 최소 1초다.

## 자동 저장 기준

1. 도로명주소를 기존 v2 규칙으로 정규화해 검색한다. 검색 결과가 0건일 때만 지번으로 대체 검색한다.
2. 검색 후보를 최대 100건 요청한다. 전체 건수와 수신 건수가 다르면 `INCOMPLETE_CANDIDATES`로 보류한다. 일부 결과만 보고 유일하다고 판단하지 않는다.
3. 정규화한 원본 주소와 반환 주소가 정확하게 일치하는 후보가 **정확히 한 행**이어야 한다. 유사도, 첫 번째 결과, 시설명만으로 선택하지 않는다. 동일 주소 후보가 여러 행이면 건물번호가 같아도 보류한다.
4. 건물 식별 필드를 검증하고 좌표 API가 반환한 건물과 일치하는지 확인한다. 출입구가 하나이고 변환한 좌표가 유효한 경우에만 저장한다.
5. 조회 후 원본 주소·상태가 변경됐으면 `STALE`로 저장을 건너뛴다. 원본 주소와 기존 성공 좌표는 덮어쓰지 않는다.

성공 건에는 `juso-exact-review-v1`을 기록한다. 불확실한 건은 DB를 변경하지 않고 검토 보고서에 세부 사유를 남긴다. 재실행은 당시 미해결 건을 다시 검색하며 이전 보고서 파일을 실행 입력으로 사용하지 않는다.

## 검토 결과 열기

출력된 `.cache/facilities/geocoding-reviews/<실행 ID>/index.html`을 브라우저에서 연다. 시설명·주소 검색과 사유별 필터를 제공하며 각 시설의 ‘검색 후보 비교’를 펼치면 다음 정보를 확인할 수 있다.

- 원본 도로명·지번, 이전 좌표 상태, 정규화한 검색어
- 전체 검색 건수·전체 수신 여부·정확한 후보 수
- 후보의 도로명·지번·건물명, 정확 일치 여부
- 최종 분류와 실제 저장 여부

HTML은 읽기 전용이다. 후보 클릭으로 강제 저장하거나 주소를 임의 수정하지 않는다. 검토 자료는 외부 API 포트에 제공하지 않으며 로컬에만 저장한다.

같은 폴더의 `entries.jsonl`에는 처리된 항목별 결과가 즉시 기록되고 `summary.json`에는 사유별 집계·호출 수·실행 상태가 기록된다. 정상 종료 또는 처리 중 오류·Ctrl+C 시 완료된 항목의 HTML과 요약을 만든다. 강제 프로세스 종료나 디스크 오류 시에는 JSONL만 남을 수 있으므로 DB와 로그를 확인한다. 토큰, 인증키, 요청 본문, 원본 API 응답, 제공자 오류 메시지는 보고서에 넣지 않는다.

주요 분류는 `EXACT_MATCH`(검토 전용 모드에서 일치 후보 확보), `MATCHED_ENTRANCE`(좌표 확보), `ADDRESS_MISMATCH`, `MULTIPLE_EXACT_MATCHES`, `INCOMPLETE_CANDIDATES`, `ADDRESS_NOT_FOUND`, `MULTIPLE_ENTRANCES`, `BUILDING_MISMATCH`, `INVALID_COORDINATES`다. 저장 성공 여부는 `saved`를 함께 확인한다.

## 검증

```powershell
python -m unittest discover -s tests -p "test_toilet_geocoding*.py" -v
node tests/test_geocoding_review_report.cjs
```

부분 후보 수신·중복 정확 후보의 저장 차단, 두 번째 후보만 정확히 일치하는 경우, 검토 전용 DB 불변, 출입구 검증, 동시 수정 보호, 보고서 HTML 이스케이프, API 오류 시 부분 보고서를 검증한다.

## 2026-11-13 실제 실행 결과

수원·안양 미해결 283건을 검토해 101건을 추가 저장했다. 최종 유효 좌표는 수원 467건·안양 197건, 총 664건이다. 보고서 분류는 좌표 확보 101건, 정확한 후보 여러 개 82건, 정확한 후보 없음 54건, 검색 결과 없음 37건, 좌표 오류 7건, 출입구 여러 개 1건, 전체 후보 미수신 1건이다. 남은 검토 대상은 182건이다.

실제 HTTP 호출은 413회였다. 사전 백업과 비교해 보고서에서 저장 성공으로 기록한 101개 행만 변경됐으며, DB 무결성과 공간 인덱스 664건을 확인했다. 생성된 HTML의 시설 항목 283개와 필터 코드 테스트를 확인했다. 브라우저 도구의 로컬 파일 열기 제한으로 화면 렌더링은 직접 검증하지 못했다.
