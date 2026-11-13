# Runline 경로 생성 API · 팀 테스트 연동

공개용 API 요청·응답 명세입니다. 실제 접속 주소와 토큰은 각 실행 환경에서 설정합니다.

서버 시작·종료·환경 설정·갱신·백업·이전은 [서버 운영 안내](../SERVER_OPERATIONS.md)를 참고하세요.

Flutter 모바일 앱 또는 다른 프로젝트의 백엔드가 HTTPS로 요청하면 ngrok이 이 PC의 `127.0.0.1:8083`으로 전달한다. 계산 결과는 같은 POST 요청의 응답으로 돌아온다. **8082는 이 PC에서만 쓰는 테스트 화면**으로 남겨 둔다.

## 서버를 제공하는 PC

서버를 재시작해야 할 때 PowerShell에서 실행한다. 이미 실행 중이면 이 단계는 건너뛴다.

```powershell
cd runline-api
python api_server.py --port 8082 --enable-tunnel --tunnel-port 8083
```

다른 PowerShell에서 터널을 연다. ngrok 실행 파일을 `.cache/ngrok/ngrok.exe`에 설치하거나 PATH에 추가한다. PATH에 있다면 `ngrok http 127.0.0.1:8083`도 같다.

```powershell
$ngrok = '.\.cache\ngrok\ngrok.exe'
& $ngrok http 127.0.0.1:8083
```

ngrok이 계정 인증을 요구하면 본인의 ngrok 계정에서 에이전트를 먼저 인증한다. 팀원에게 전달할 것은 **Forwarding에 표시된 HTTPS 기본 주소**와 **경로 API 테스트 토큰**이다. ngrok 계정의 authtoken을 전달하지 않는다. 서버의 API 토큰은 `DRAWRUN_API_TOKEN` 환경 변수가 우선이고, 없으면 서버 폴더의 `.api-token`을 사용한다. 토큰을 채팅 로그·Git·배포 앱에 넣지 않는다.

## 8082 카카오맵 테스트 화면

1. 프로젝트 `.env`의 `KAKAO_JAVASCRIPT_KEY`에 카카오 JavaScript 키를 설정한다. REST API 키와는 다르다.
2. [카카오 지도 공식 안내](https://apis.map.kakao.com/web/guide/)에 따라 해당 키의 JavaScript SDK 도메인에 `http://localhost:8082`를 등록한다. `http://127.0.0.1:8082`로도 접속한다면 이 주소도 등록한다.
3. 서버 코드 업데이트 후 서버를 재시작한다. 이후 키 값만 수정할 때는 페이지 새로고침으로 반영된다.
4. ‘카카오맵으로 기준 위치 선택’에서 장소·주소를 검색하고 결과를 선택하거나 지도를 클릭한다. 경도·위도와 요청 JSON의 `location`이 함께 갱신된다.
5. ‘API로 경로 생성’을 누르면 실제 OSM 후보가 카카오 지도에 표시된다. 후보 선택으로 경로를 바꿀 수 있다. 초록색은 생성 경로, 주황색 점선은 원본 배치선, 파란색 점선은 접근 구간이다.

가상 도로는 기존 도식에만 표시한다. 지도 키 미설정·연결 실패 시에도 좌표 직접 입력과 경로 도식을 사용할 수 있다. 장소 검색과 지도 배경은 카카오, 경로 계산은 기존 로컬 OSM 엔진을 사용한다. 카카오 길찾기 API를 호출하는 기능은 아니다.

`GET /local/map-config`는 브라우저용 JavaScript 키만 제공하며 외부 API 전용 8083에서는 404로 차단한다. `.env` 및 다른 인증키는 응답하지 않는다.

검증: `python -m unittest discover -s tests -p test_api_server.py` 및 `node tests/test_route_map.cjs`.

## API 호출

1. **상태:** `GET /v1/health` (인증 없음). 외부용 포트의 정상 응답: `{"ok":true,"version":"v1","local_only":false}`.
2. **경로 생성:** `POST /v1/routes`. 헤더는 `Authorization: Bearer <경로 API 토큰>`, `Content-Type: application/json`.

실제 도로는 **이 PC에 내려받은 대한민국 OSM**을 사용한다. 매 요청마다 외부 Overpass 서버를 조회하지 않는다. 지도 배경 타일 제공 여부와는 별개다. 아래는 실제 도로 요청 예시다.

```json
{
  "drawing": [[100, 80], [100, 320], [400, 320]],
  "location": [127.0374, 37.5445],
  "distance_m": 2000,
  "data_source": "osm",
  "candidate_limit": 5
}
```

**연결만 먼저 확인하려면** [가상 도로 요청](request.demo.json)을 사용한다. 외부 OSM 조회 없이 동작한다. 다음은 이 문서가 있는 폴더에서 호출하는 PowerShell 예시다. 호출 측에 `DRAWRUN_API_TOKEN` 환경 변수를 별도로 설정한다.

```powershell
$base = 'https://<ngrok에 표시된 도메인>'
Invoke-RestMethod "$base/v1/health"
$headers = @{Authorization = "Bearer $env:DRAWRUN_API_TOKEN"}
$r = Invoke-RestMethod "$base/v1/routes" -Method Post -Headers $headers -ContentType 'application/json' -InFile './request.demo.json' -TimeoutSec 120
$r | Select-Object found, source, recommended_count, message
```

### 요청 필드

| 필드 | 의미와 허용값 | 기본값 |
|---|---|---|
| drawing | 그린 순서의 캔버스 [x,y] 2~3,000개. 각 좌표 -10,000~10,000 | 필수 |
| location | 기준 위치 [경도,위도]. 경도 124~132, 위도 32~39 | 필수 |
| distance_m | 목표 러닝 거리 300~8,000m | 3000 |
| approach_m | 기준 위치에서 코스 시작점까지 허용하는 접근 거리 0~1,500m | 1000 |
| tolerance | 목표 거리 허용 오차 0.05~0.5 (0.2 = ±20%) | 0.2 |
| candidate_limit | 반환 후보 상한, 정수 1~30 | 20 |
| route_type | auto / open / loop | auto |
| search_mode | overview(위치별 대표) / all / local(특정 위치 상세) | overview |
| focus_location | local 모드에서 상세 탐색할 [경도,위도] | local일 때 필수 |
| allow_rotation / allow_vertex_adjustment | 회전 / 꼭짓점 조정 허용 여부. JSON 불리언 | 각각 true |
| radius_m | 도로 조회 반경 800~4,000m | 2500 |
| data_source / provider | osm(내 PC의 한국 도로) 또는 demo(가상 도로) / local. 기존 main·coffee 값도 로컬 데이터로 처리 | osm / local |
| search_seconds | 경로 계산 시간 예산 3~45초. 도로 조회 시간 별도 | 45 |

### 응답 읽기

아래는 앱에서 사용할 필드만 뽑은 **구조 예시**다. 좌표와 거리는 설명용이다.

```json
{
  "api_version": "v1",
  "found": true,
  "source": "osm",
  "recommended_count": 1,
  "candidates": [{
    "acceptable": true,
    "distance_m": 2010,
    "geometry": {"type": "LineString", "coordinates": [[127.0374, 37.5445], [127.0375, 37.5446]]}
  }]
}
```

- **추천 코스:** 실제 도로 결과에서 `acceptable: true`인 후보를 선택한다. `geometry.coordinates`는 **[경도, 위도]** 순서다. 카카오맵에서 `LatLng(위도, 경도)`를 쓸 때는 순서를 바꾼다.
- **추천 없음:** HTTP 200이어도 `found: false`일 수 있다. 빈 배열이면 `message`를 표시하고, 후보가 있어도 `acceptable: false`면 비교용으로만 표시한다.
- **데모:** 요청 값 `demo`의 응답 출처는 `synthetic`이다. 이 좌표는 실제 러닝 코스로 제공하지 않는다.
- **추가 정보:** `request_id`는 요청 식별자, `total_elapsed_s`는 전체 처리 시간, `data`는 도로 출처·캐시 정보다. `warnings`와 데이터 경고가 있으면 확인한다.

### 한국 도로 데이터 관리

- 원본: [Geofabrik South Korea](https://download.geofabrik.de/asia/south-korea.html). © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), ODbL. 사용자에게 경로를 표시할 때 출처를 함께 표시한다.
- 서버 폴더의 `.cache/korea/south-korea.osm.pbf`는 원본, `.cache/korea/roads.sqlite3`는 위치별 보행로 조회 인덱스다. 서버는 인덱스를 읽기 전용으로 열며, 인덱스가 없을 때 외부 요청이나 가상 도로로 대체하지 않는다.
- 갱신: 새 대한민국 PBF와 배포처 체크섬을 확인한 뒤 원본을 교체하고, 서버 폴더에서 `& .\.cache\korea\import-env\Scripts\python.exe .\local_osm.py`를 실행한다. 완료 후 서버를 재시작하면 메모리에 남은 이전 그래프까지 갱신된다.
- 실제 도로 응답은 `source: "osm"`, `data.osm_provider: "local_korea"`다. `data.osm_timestamp`는 추출본 기준 시각이다.

### 오류와 클라이언트 제약

오류 본문 예: `{"error":{"code":"UNAUTHORIZED","message":"올바른 테스트용 Bearer 토큰이 필요합니다."}}`.

- 400: 좌표 순서, 입력 범위, 필드명 또는 JSON 형식 확인. 401: API 토큰 확인. 413: 본문 최대 256 KiB. 415: Content-Type 확인.
- 422: 입력 조건 또는 도로 조회 문제. 429: 동시 계산 1개 또는 두 포트 합산 분당 12회 제한. 재시도 전 응답의 `Retry-After`를 따른다.
- **Flutter Web / 브라우저:** 테스트용 `http://localhost:5000` 및 `http://127.0.0.1:5000`에서 직접 호출할 수 있다. 다른 Origin은 403이다. `flutter run -d chrome --web-port 5000`으로 실행한다. Flutter Android/iOS의 네이티브 HTTP 호출과 서버 간 호출도 지원한다.
- PC, API 서버, ngrok이 실행되는 동안 접속할 수 있다. 이 테스트에서 확인한 범위는 로컬의 터널 대상 포트이며, 외부 연결은 터널을 연 뒤 다른 기기에서 위 상태 요청으로 확인한다.


시설 조회 API는 [시설 API 계약](../facilities/PLACES_API.md)을 참고하세요. 기존 두 포트의 Bearer 인증 정책으로 `GET /v1/places`를 호출할 수 있습니다.
