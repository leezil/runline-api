# 시설 데이터 상태 확인 API

`GET /v1/places/status`는 전체 지역의 화장실·CCTV 저장 및 수집 상태를 조회한다. 지도 경계나 쿼리 매개변수는 받지 않는다. 로컬·외부 API 포트 모두 `Authorization: Bearer <API_TOKEN>` 인증이 필요하다. 기존 지도 조회와 두 포트 합산 분당 120회 제한을 공유한다. 응답은 `Cache-Control: no-store`다.

```sh
curl -H "Authorization: Bearer <API_TOKEN>" http://localhost:8082/v1/places/status
```

## 응답

최상위 `sources` 배열에 `toilet`, `cctv` 순서로 상태를 반환한다. 각 항목은 다음 필드를 갖는다.

- `type`, `source`: 시설 종류와 데이터 출처 코드
- `ready`: 해당 종류에 저장 데이터 또는 성공한 수집 기록이 하나 이상 있는지 여부. 좌표 준비 완료를 뜻하지 않는다.
- `data_updated_at`: 모든 알려진 지역의 마지막 수집 성공 시각 중 가장 오래된 값. 성공 시각이 불명확한 지역이 하나라도 있으면 `null`.
- `last_import_failed`: 알려진 지역 중 마지막 수집 시도가 실패한 곳이 있는지 여부
- `counts`: 해당 종류 전체 집계
- `regions`: 지역 코드순 지역별 상태·집계 배열

각 지역에는 `region_code`, `ready`, `last_fetched_at`, `last_success_at`, `last_attempt_at`, `last_attempt_status`, `counts`가 포함된다. `last_attempt_status`는 `success`, `failed`, `unknown`이다. 기존 데이터에 성공 이력이 없으면 시각을 추정하지 않고 `null`·`unknown`으로 표시한다. 실패 후에도 이전 성공 시각과 데이터는 유지된다. 실제 데이터 갱신을 수행한 수집기 기록이며, dry-run·스케줄 시작·중복 건너뜀 기록은 포함하지 않는다.

## 집계 의미

`counts`는 전체 종류와 각 지역에서 동일한 구조다.

```json
{
  "stored": 2,
  "active": 1,
  "inactive": 1,
  "map_ready": 0,
  "coordinates": {"ambiguous": 0, "failed": 0, "invalid": 0, "pending": 2, "valid": 0},
  "active_coordinates": {"ambiguous": 0, "failed": 0, "invalid": 0, "pending": 1, "valid": 0}
}
```

- `stored = active + inactive`. 비활성 시설도 저장 건수에 포함한다.
- `map_ready`: 활성·유효 좌표 시설. CCTV는 방범 목적이 명확히 확인된 시설만 포함한다. 지도 조회와 동일한 대상 조건이며 화면 범위·응답 limit을 적용하기 전 총 건수다. CCTV 개별 카메라 대수가 아닌 시설 레코드 수다.
- `coordinates`: 비활성 시설을 포함한 전체 좌표 상태별 건수. 합계는 `stored`다.
- `active_coordinates`: 활성 시설만 집계. 합계는 `active`다. 화장실의 좌표 변환 대기·실패 현황은 이 값을 사용한다.
- `pending`: 미처리, `failed`: 처리 실패, `invalid`: 유효하지 않은 좌표, `ambiguous`: 주소 후보 불명확, `valid`: 유효 좌표. `failed`만으로 모든 미해결 항목을 표현하지 않는다.

## 빈 데이터 및 오류

정상 DB의 미수집 종류는 HTTP 200, `ready: false`, 0건, 빈 지역 배열로 반환한다. 성공적으로 0건을 수집한 지역은 `ready: true`와 성공 시각이 있으며 집계는 0이다. 최초 수집에 실패한 지역도 실패 상태와 0건으로 표시한다.

- 400 `INVALID_INPUT`: 쿼리 매개변수 사용
- 401 `UNAUTHORIZED`: 인증 누락·오류
- 403: 기존 출처·로컬 접근 제한 위반
- 429 `RATE_LIMITED`: 조회 횟수 초과
- 503 `FACILITIES_UNAVAILABLE`: DB 없음·손상·읽기 실패

인증을 먼저 확인하며, 읽기 전용 트랜잭션 하나에서 상태와 건수를 집계한다. DB 생성·마이그레이션·외부 API 호출은 하지 않는다. 응답에 인증키, 로컬 파일 경로, 시설 주소, 상세 오류나 원본 로그는 포함하지 않는다. 서버 실행 상태는 기존 `/v1/health`, 자동 갱신 작업 실행 기록은 로컬 갱신 로그에서 별도로 확인한다.
