# 시설 조회 API

구현일: 2026-11-12. `GET /v1/places`는 로컬 시설 SQLite만 읽는다. 지도 조회 중 공공데이터나 지오코딩 API를 호출하지 않는다. 지도 마커 UI는 클라이언트의 후속 작업이다.

## 실행과 인증

```powershell
python api_server.py --port 8082
# 다른 DB를 사용할 때
python api_server.py --port 8082 --facility-database .cache/facilities/facilities.sqlite3
```

기존 `--enable-tunnel --tunnel-port 8083`도 지원한다. 서버가 터널을 직접 시작하지는 않는다. 로컬·외부 API 전용 포트 모두 `Authorization: Bearer <API_TOKEN>`이 필요하다. 로컬 테스트 화면은 기존 `/local/bootstrap`으로 토큰을 얻고, 외부 연동은 `DRAWRUN_API_TOKEN` 또는 `.api-token`의 테스트 토큰을 사용한다. 외부 포트에서는 bootstrap과 정적 파일을 제공하지 않는다. 운영 앱의 사용자 인증은 별도 과제이며 공유 테스트 토큰을 배포 앱에 내장하지 않는다.

외부 포트의 웹 출처는 기존 `http://localhost:5000`, `http://127.0.0.1:5000`만 허용한다. `OPTIONS /v1/places`는 GET preflight를 지원한다. 외부 브라우저에서 인증 헤더와 필요시 `ngrok-skip-browser-warning`을 전달한다. 네이티브 앱은 기존 API 전용 포트 정책을 따른다.

## 요청

```http
GET /v1/places?types=toilet,cctv&west=127.00&south=37.25&east=127.03&north=37.28&limit=200
Authorization: Bearer <API_TOKEN>
```

- `west`, `south`, `east`, `north`: 필수. WGS84 경도/위도이며 유한 숫자여야 한다. 경도 124~132, 위도 32~39, west < east, south < north를 검사한다.
- 가로·세로 각각 10km 이하. 구면 거리로 세로와 남쪽 가로변을 검사한다. 지도 확대 후 다시 요청한다.
- `types`: 생략하면 `toilet,cctv`. `toilet`, `cctv` 또는 두 값의 쉼표 목록만 허용한다. 빈 값, 중복 종류, 알 수 없는 종류는 400이다.
- `limit`: 기본 200, 정수 1~500. 초과 여부는 한 건을 더 조회해 판단한다.
- 알 수 없는 쿼리 키, 중복 키, 잘못된 인코딩, 2,048자를 넘는 쿼리는 400이다.
- 시설 조회는 로컬/외부 포트를 합쳐 분당 120회로 제한한다. 경로 생성의 분당 12회 예산 및 계산 슬롯과 분리한다. 다중 사용자 서비스용 제한은 아니다.

## 응답 예시

아래는 가상 데이터 예시다. 항목은 ID 오름차순이며 다음 페이지 커서는 없다. `truncated=true`면 클라이언트에서 지도 확대를 안내한다. `count`는 반환한 개수이며 전체 검색 결과 수가 아니다.

```json
{
  "items": [
    {
      "id": "example-id",
      "type": "cctv",
      "name": "방범 CCTV",
      "location": [127.01, 37.26],
      "address": {"road": "가상 도로 주소", "lot": null},
      "source": "mois_cctv",
      "source_date": "20261110",
      "coordinate_source": "source",
      "details": {"purpose_raw": "생활방범", "camera_count": 3}
    }
  ],
  "count": 1,
  "truncated": false,
  "data_updated_at": {"cctv": "2026-11-12T13:00:00+00:00"},
  "sources": [
    {
      "type": "cctv",
      "source": "mois_cctv",
      "ready": true,
      "data_updated_at": "2026-11-12T13:00:00+00:00",
      "last_import_failed": false,
      "regions": [
        {
          "region_code": "3740000",
          "last_fetched_at": "2026-11-12T12:59:00+00:00",
          "last_success_at": "2026-11-12T13:00:00+00:00",
          "last_attempt_at": "2026-11-12T13:00:00+00:00",
          "last_attempt_status": "success",
          "ready": true
        }
      ]
    }
  ]
}
```

`location`은 `[경도, 위도]` 순서다. 화장실 `details`에는 `opening_hours_text`, `opening_hours_detail`을 반환한다. 불완전한 개방시간에서 현재 이용 가능 여부를 계산하지 않는다. 전화번호, 내부 관리번호, 지오코더 해시·키, 전체 원본은 반환하지 않는다.

active이고 좌표가 valid인 항목만 반환하며 CCTV는 방범 판정이 true여야 한다. 따라서 현재 좌표 대기 중인 화장실 846건은 조회 결과에 없으며 화장실만 조회하면 200과 빈 배열이다. 좌표 저장 후 같은 API로 조회된다. 임시 비활성화한 CCTV 이상치 5건도 제외된다.

## 수집 준비 상태와 최신성

- 요청 종류 중 데이터/성공 기록이 전혀 없는 종류가 있으면 503 `FACILITIES_NOT_READY`다. CCTV만 준비된 환경에서 기본 두 종류 조회가 503이면 `types=cctv`로 조회할 수 있다.
- **준비된 종류에서 범위 내 표시 대상이 없는 경우는 200과 빈 배열**이다. 표시 가능한 좌표가 없거나 정상 수집 건수가 0인 경우도 포함한다.
- 준비 여부는 종류 단위다. 지역 경계를 판정하는 API가 아니므로 임의 지도 범위가 완전히 수집되었다는 보장은 없다. `sources[].regions`의 지역 코드로 보유 범위를 확인한다. 다른 지역에서 빈 결과가 나와도 실제 시설이 없다는 뜻은 아니다.
- 수집기는 성공한 데이터와 지역별 성공 시각을 같은 트랜잭션에 저장한다. 실패하면 기존 성공 시각·시설을 유지하고 마지막 시도 상태만 failed로 기록한다. dry-run은 수집 상태를 변경하지 않는다.
- `data_updated_at`은 종류별 알려진 지역의 마지막 성공 수집 시각 중 가장 이른 값이다. 원본 기관의 갱신시각이나 데이터 정확도 보장이 아니다. 특정 지도 범위의 수집 시각도 아니다.
- 기존 DB에 새 상태 기록이 없으면 기존 시설을 조회할 수 있지만 완료 시각은 추정하지 않는다. `last_attempt_status=unknown`, `last_success_at=null`, `data_updated_at=null`이며 행의 `last_fetched_at`만 참고값으로 제공한다. 다음 정상 수집부터 상태가 채워진다.
- 일부 지역에 성공 기록이 없으면 종류 전체의 `data_updated_at`도 null이다. `last_import_failed=true`면 실패 정보와 기존 데이터가 함께 제공되는 상태다. 미수집 지역의 첫 실패가 포함될 수도 있으므로 지역별 ready도 확인한다.
- 알려진 최신성은 마지막 실행 기록 기준이다. 스케줄러·자동 갱신·데이터 보존 기간에 따른 만료 판정은 이번 범위가 아니다. 동시 수집 중에도 응답 항목과 수집 상태는 같은 SQLite 읽기 스냅샷을 사용한다.

추가 테이블 `facility_import_state`는 기존 v1 DB에 호환되는 보조 테이블이다. 수집 작업이 쓸 때 생성하며 API 읽기는 생성/마이그레이션하지 않는다. 오래된 수집기로 되돌리면 이 기록이 갱신되지 않으므로 이번 버전의 수집기를 함께 사용한다.

## 오류

기존 형식 `{"error":{"code":"...","message":"..."}}`를 사용한다.

- 400 `INVALID_INPUT`: 쿼리 오류. `BOUNDS_TOO_LARGE`: 10km 제한 초과.
- 401 `UNAUTHORIZED`: Bearer 인증 실패.
- 403: 기존 로컬/Origin 제한 위반.
- 429 `RATE_LIMITED`: 분당 제한, `Retry-After: 60` 제공.
- 503 `FACILITIES_NOT_READY`: 요청 종류 미수집.
- 503 `FACILITIES_UNAVAILABLE`: DB 없음·손상·읽기 오류. 실제 파일 경로와 예외는 노출하지 않는다.

모든 응답은 `Cache-Control: no-store`다. 요청 로그에는 API 경로·상태만 남고 지도 좌표 쿼리나 토큰은 남기지 않는다.

## 검증 기록

가상 HTTP 테스트로 인증, CORS, 입력, 개수 제한, 준비 상태, 갱신 실패, 읽기 전용 동작, 수집 성공 기록의 원자성을 검증했다. 실제 로컬 DB로 임시 API 전용 서버를 열어 수원·안양 각 범위에서 200개와 truncated=true, 화장실 범위에서 0개와 truncated=false를 확인했다. 실제 DB 파일의 검사 전후 SHA-256은 동일했다. 전체 CCTV 4,983건을 한 응답으로 반환하지 않는다.


전체 목록 기반 갱신과 누락 시설의 비활성화는 [갱신 안내](FACILITY_REFRESH.md)를 참고한다. 갱신 성공/실패는 이 API의 수집 상태에 반영되며 자동 스케줄은 아직 등록하지 않았다.
