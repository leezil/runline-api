# runline-api

그린 선의 형태와 순서를 따라 OSM 보행 도로에서 러닝 경로 후보를 계산하는 Python 테스트 프로젝트입니다. API 서버, 로컬 테스트 화면, 경로 계산 엔진과 회귀 테스트를 포함합니다.

## 실행

Python 3.10 이상이 필요합니다. 서버 실행은 표준 라이브러리만 사용합니다.

```sh
python api_server.py --port 8082
```

http://localhost:8082 에서 테스트합니다. 도로 데이터가 없는 첫 실행에서는 **가상 도로**를 선택하세요. 가상 도로 결과는 실제 러닝 코스가 아닙니다.

기존 지도 실험 화면은 `python server.py`로 실행한 뒤 http://localhost:8080 에서 사용할 수 있습니다. 카카오맵 연결 시 `.env.example`을 `.env`로 복사하고 본인의 JavaScript 키를 입력하세요. 카카오 개발자 콘솔에서 로컬 도메인을 등록해야 합니다. 웹 SDK 키는 브라우저에 전달되므로 도메인 제한을 설정하고 어드민 키를 사용하지 마세요.

## API 연동

```sh
python api_server.py --port 8082 --enable-tunnel --tunnel-port 8083
ngrok http 127.0.0.1:8083
```

두 명령은 별도 터미널에서 실행합니다. ngrok 설치와 본인 계정 인증이 필요합니다. 8082는 로컬 화면이며 8083은 API 전용입니다. API 서버 자체는 공개 터널을 시작하지 않습니다.

- `GET /v1/health`: 상태 확인
- `POST /v1/routes`: 그림과 기준 위치로 경로 생성
- 외부 요청 인증: `Authorization: Bearer <API_TOKEN>`
- 토큰은 `DRAWRUN_API_TOKEN` 환경 변수 또는 로컬 `.api-token` 파일로 관리합니다. 환경 변수와 파일이 없으면 터널 기능을 켤 때 새 토큰을 생성합니다.

[API 상세 명세 및 요청 예시](docs/route-api/README.md)를 참고하세요. 현재 테스트용 서버이며 동시 계산과 요청량에 제한이 있습니다.

공중화장실·방범카메라 기능은 [시설 데이터 연동 명세](docs/facilities/DATA_SPEC.md)에 출처, 원본 필드, 좌표 처리와 후속 작업을 정리했습니다. [시설 저장소](docs/facilities/STORAGE.md), [화장실 수집기](docs/facilities/TOILET_IMPORT.md), [주소 좌표 변환기·승인키 설정](docs/facilities/TOILET_GEOCODING.md)을 참고하세요. 로컬 DB에는 수원 588건·안양 258건을 수집했습니다(2026-11-12). 좌표 변환기는 가상 응답과 로컬 실행 계획을 검증했으며, 별도 승인키와 저장 조건 확인 후 실제 변환을 활성화합니다. 시설 조회 HTTP API는 아직 구현되지 않았습니다.

Windows 일괄 실행은 `start-route-api.cmd`를 사용합니다. 이 실행기는 사용자 로컬의 Python 3.13과 `.cache/ngrok/ngrok.exe`를 사용하므로 해당 경로에 먼저 설치해야 합니다. 창을 닫으면 실행기가 시작한 서버와 터널이 종료됩니다.

CCTV 데이터는 [CCTV 수집기·API 신청 안내](docs/facilities/CCTV_IMPORT.md)를 참고하세요. 수집·분류·원본 좌표 검증을 구현했으며, 현재 인증 호출은 HTTP 403으로 실제 CCTV 적재는 아직 진행하지 않았습니다.

## 실제 도로 데이터 준비

1. [Geofabrik South Korea](https://download.geofabrik.de/asia/south-korea.html)에서 PBF를 받아 `.cache/korea/south-korea.osm.pbf`로 저장합니다.
2. 색인 생성 환경에 `python -m pip install osmium`으로 osmium을 설치합니다.
3. `python local_osm.py`로 `.cache/korea/roads.sqlite3`를 생성합니다.
4. API 서버를 재시작하고 실제 OSM 도로 모드를 선택합니다.

실제 도로 모드는 로컬 색인이 필요하며 데이터가 없다고 가상 도로로 자동 대체하지 않습니다. 데이터 출처: © OpenStreetMap contributors, Geofabrik / ODbL. 실제 통행 가능성과 횡단 안전을 별도로 확인해야 합니다.

## 검증

```sh
python -m unittest discover -s tests -v
node tests/test_web_map.cjs
```

실제 도로 데이터가 필요한 테스트는 관련 환경 변수나 로컬 데이터가 없으면 건너뛸 수 있습니다. JavaScript 회귀 테스트에는 Node.js가 필요합니다.

## 공개 범위

실제 환경 변수, API 토큰, ngrok 계정 정보와 접속 주소, 개인 PC 경로, 지도 데이터, 요청 로그, 캡처 및 내부 개발 기록은 포함하지 않습니다. 설정 예시에는 빈 값만 제공합니다. 별도의 drawingroot 프로젝트는 포함하지 않습니다.
