# 서버 운영·배포 준비 안내

확인 기준: 2026-11-13 저장소 구현. 모든 명령은 `runline-api` 루트에서 실행한다. 이 문서는 현재 PC 운영과 다른 서버로 이전하는 절차이며, 상시 서비스 등록이나 실제 배포를 수행하지 않는다.

## 현재 운영 범위

`api_server.py`가 경로 생성과 시설 조회를 제공한다. 로컬 테스트 화면은 `127.0.0.1:8082`, 선택적으로 여는 API 전용 포트는 `127.0.0.1:8083`이다. 외부 팀 테스트는 ngrok이 HTTPS 요청을 8083으로 전달한다. **8082 테스트 화면과 로컬 관리 기능은 외부에 노출하지 않는다.** 기존 `server.py`는 별도의 지도 실험 서버이며 시설 자동 갱신 운영 서버가 아니다.

시설 데이터는 `.cache/facilities/facilities.sqlite3`에 영구 저장된다. 서버가 꺼져도 파일은 남지만 앱 조회는 서버가 실행 중이어야 가능하다. PC 절전·종료·네트워크 단절 중에는 외부 요청을 받을 수 없다. 저장소를 clone하거나 pull하는 것만으로 DB·키·도로 데이터가 설치되지는 않는다.

좌표 검색 API는 사용자 확인 기준 승인 대기 중이다. 심사 담당자나 평일 처리 여부·소요 시간은 확인되지 않았으므로 승인 일정을 가정하지 않는다. 승인 전에도 CCTV 조회와 시설 상태 조회를 운영할 수 있다. 화장실 좌표가 `pending`이면 정상 수집 데이터라도 지도 조회에서는 제외된다.

## 실행 환경과 설정

API 서버와 시설 수집·백업 도구는 Python 3.10 이상 및 표준 라이브러리를 사용한다. 추가 의존성은 화장실 좌표 변환용 `requirements-geocoding.txt`, OSM 인덱스 생성용 `osmium`이다. JavaScript 테스트에는 Node.js가 필요하다. Python·SQLite 환경은 다음으로 확인한다.

```powershell
python --version
python -c "import sqlite3; print(sqlite3.sqlite_version)"
```

첫 설정 시 `.env.example`을 `.env`로 복사하고 필요한 값만 입력한다. 기존 `.env`를 덮어쓰지 않는다.

- `PUBLIC_DATA_SERVICE_KEY`: 화장실·CCTV 수집 및 갱신용 일반 인증키(Decoding). 두 API 사용 승인이 각각 필요하다. 시설 조회만 한다면 필요하지 않지만 자정 갱신에는 필요하다.
- `JUSO_SEARCH_KEY`, `JUSO_COORD_KEY`: 주소 검색과 좌표 검색의 별도 승인키. 실제 좌표 변환 배치에서 사용한다.
- `JUSO_STORAGE_APPROVED`: 저장·앱 재제공 조건을 확인한 뒤 설정하는 스위치. 승인 여부를 자동 조회하지 않는다. 상세 조건은 [좌표 변환 안내](facilities/TOILET_GEOCODING.md)를 따른다.
- `KAKAO_JAVASCRIPT_KEY`: 기존 지도 실험 화면용. 시설 API와 공공데이터 수집 키가 아니다.

수집·좌표 변환 설정은 환경 변수가 `.env`보다 우선한다. **`DRAWRUN_API_TOKEN`은 API 서버 프로세스의 환경 변수로 설정해야 하며 API 서버가 `.env`에서 읽지 않는다.** 설정하지 않은 경우 `--enable-tunnel` 모드에서는 로컬 `.api-token` 파일을 사용하거나 생성한다. 로컬 전용 모드에서는 파일 대신 실행 세션용 토큰을 생성한다. 토큰 설정을 바꿨다면 서버를 재시작하고 호출 측 설정도 맞춘다.

실제 키·토큰·ngrok 계정 authtoken은 저장소나 문서에 기록하지 않는다. ngrok 계정 인증은 실행 계정에서 별도로 준비한다. 앱 개발자에게 전달하는 API Bearer 토큰과 ngrok 계정 authtoken은 서로 다른 값이다.

## 시작·상태 확인·종료

로컬에서 조회를 먼저 확인하려면 다음처럼 자동 갱신 없이 시작한다.

```powershell
python api_server.py --port 8082 --disable-facility-refresh
```

팀 테스트용 API 포트와 자정 갱신을 사용하려면 다음 명령으로 시작한다.

```powershell
python api_server.py --port 8082 --enable-tunnel --tunnel-port 8083
```

별도 PowerShell에서 ngrok을 실행한다. PATH에 없다면 프로젝트에 설치한 `.cache/ngrok/ngrok.exe`를 사용한다.

```powershell
& .\.cache\ngrok\ngrok.exe http 127.0.0.1:8083
```

Windows의 `start-route-api.cmd`는 위 두 프로세스를 함께 실행한다. 이 실행기는 `%LOCALAPPDATA%\Programs\Python\Python313\python.exe`와 `.cache/ngrok/ngrok.exe`를 전제로 하므로 다른 PC·Python 경로에서는 위의 개별 명령을 사용한다. 같은 포트에 서버를 중복 실행하지 않는다.

```powershell
Invoke-RestMethod 'http://127.0.0.1:8082/v1/health'
Invoke-RestMethod 'http://127.0.0.1:8083/v1/health'
```

8083 확인은 API 전용 포트를 켠 경우에만 가능하다. `ok: true`는 HTTP 서버 응답 확인이며 시설 DB·도로 인덱스·외부 인증키의 정상 여부를 보증하지 않는다. 시설은 아래 상태 API로 추가 확인한다. ngrok 외부 연결은 다른 기기에서 HTTPS 주소의 `/v1/health`를 호출해 확인한다.

개별 실행은 각 터미널의 Ctrl+C로 종료한다. 일괄 실행 창의 Ctrl+C 또는 창 닫기는 API와 ngrok 및 자식 프로세스를 종료할 수 있다. 종료 전에 갱신 로그를 확인하고 진행 중인 갱신이 끝날 때까지 기다린다. 개별 실행한 API만 종료할 때는 이미 시작된 갱신 프로세스가 남아 완료될 수도 있다. 복원·이전 전에는 수집·갱신·좌표 변환 프로세스까지 모두 종료됐는지 확인한다.

재시작은 종료 후 같은 명령을 실행한다. 코드 업데이트는 갱신 종료 확인 → 백업 → 서버 종료 → main 업데이트 → 서버 시작 → health와 시설 상태 확인 순서로 진행한다. 키 파일은 유지하며, DB 스키마 변경이 있는 배포는 해당 변경 안내를 먼저 확인한다.

## 앱 개발자에게 전달할 내용

공개 저장소 대신 팀의 비공개 전달 수단으로 HTTPS 기본 주소와 테스트용 API Bearer 토큰을 전달한다. 주소는 터널 변경 시 다시 확인한다. 일괄 실행의 현재 주소 기록은 `.cache/route-startup/current-api-url.txt`이며 파일이 남아 있다고 터널이 현재 실행 중인 것은 아니다.

호출 측 PowerShell에 별도로 설정한 `DRAWRUN_API_TOKEN`을 사용하는 예시다. 실제 토큰을 코드나 배포 앱에 하드코딩하지 않는다.

```powershell
$base = 'https://<현재-API-도메인>'
$headers = @{ Authorization = "Bearer $env:DRAWRUN_API_TOKEN" }
Invoke-RestMethod "$base/v1/health"
Invoke-RestMethod "$base/v1/places/status" -Headers $headers
Invoke-RestMethod "$base/v1/places?west=127&south=37.25&east=127.02&north=37.27&types=cctv&limit=200" -Headers $headers
Invoke-RestMethod "$base/v1/routes" -Method Post -Headers $headers -ContentType 'application/json' -InFile './docs/route-api/request.demo.json' -TimeoutSec 120
```

- `/v1/health`: 인증 없이 서버 응답 확인.
- `/v1/places/status`: 지역·시설별 저장·지도 표시 건수, 좌표 상태와 최근 수집 상태. [상태 계약](facilities/FACILITY_STATUS_API.md).
- `/v1/places`: 지도 범위 내 활성·유효 좌표 시설. CCTV는 방범 목적 확인 대상만 제공한다. `location`은 **[경도, 위도]** 순서이며 지도 경계는 가로·세로 각각 최대 10km, limit은 최대 500이다. [시설 계약](facilities/PLACES_API.md).
- `/v1/routes`: 경로 생성. 위 예시는 가상 도로 연결 검증용이며 실제 경로로 제공하면 안 된다. 실제 도로 요청은 별도 인덱스가 필요하다. [경로 계약](route-api/README.md).

시설 조회와 시설 상태 조회는 두 포트 합산 분당 120회 제한을 공유한다. 경로 계산은 별도 분당 12회·동시 계산 1개 제한이 있다. 429에서는 `Retry-After`를 따른다. 웹 클라이언트의 허용 Origin은 현재 `http://localhost:5000`, `http://127.0.0.1:5000`이다. 실제 서비스 웹 도메인을 설정하는 환경 변수는 아직 없다. 모바일 네이티브 HTTP 호출은 웹 CORS와 구분한다.

## 일상 운영과 장애 대응

- **갱신:** 기본 매일 00:00 KST, API 서버 실행 중에만 동작한다. 서버 중단으로 놓친 작업은 보충하지 않으며 지연 허용은 5분이다. 수동 갱신은 `refresh-facilities.cmd`. 시간 변경·실행 기록은 [갱신 설정](facilities/FACILITY_REFRESH_SCHEDULE.md)을 따른다.
- **중단 기록:** 스케줄러 시작·다음 갱신 전에 잠금을 확인해 식별 가능한 중단 로그를 정리한다. 로그만 복구하려면 `python facility_refresh_job.py --recover-only`. DB 반영 여부는 상태 API로 확인한다. 당일 중단 작업은 수동 재실행한다.
- **백업:** `backup-facilities.cmd`로 원하는 시점에 생성한다. 자동 백업 스케줄은 없다. 코드 업데이트·좌표 일괄 변환·서버 이전 전에 백업하고 별도 디스크에도 보관한다. [백업·복원 절차](facilities/FACILITY_BACKUP.md).
- **로그:** 일괄 실행의 시작 로그는 `.cache/route-startup/`, 시설 작업 요약은 `.cache/facilities/job-runs/`, 상세 갱신은 `refresh-runs/`, 좌표 변환은 `geocoding-runs/`다. 로컬 화면 요청 로그는 메모리 기록이므로 서버 재시작 후 지속 보관되지 않는다. 로그·백업 자동 보존 기간 설정은 없으므로 디스크 사용량을 관리한다.
- **401:** 호출 측 Bearer 토큰과 실행 중인 서버 설정 확인. 토큰을 로그로 출력해서 비교하지 않는다.
- **403:** 로컬 관리 포트 외부 호출 여부, 웹 Origin·전달 헤더 확인. 외부 요청은 8083 터널을 사용한다.
- **503 / 시설 미표시:** DB 경로와 상태 API 확인. 미수집·좌표 대기·비활성·방범 목적 미확인·지도 범위 제외를 구분한다. health 정상만으로 데이터 준비 완료를 판단하지 않는다.
- **갱신 실패:** `PUBLIC_DATA_SERVICE_KEY` 설정과 소스별 고정 오류 코드 확인. 이전 성공 데이터는 유지된다. 부분 성공일 수 있으므로 지역·종류별 결과를 확인한 뒤 재실행한다.
- **복원 필요:** 서버와 쓰기 작업을 종료하고 백업 검사 → 복원 → 상태 조회 순서로 진행한다. `before-restore` 사본으로 복원 전 상태로 되돌릴 수 있다. 손상된 현재 DB는 자동 덮어쓰지 않으므로 원본·부속 파일을 보관하고 별도 경로로 복원해 점검한다.

## 다른 PC·서버로 이전

1. 원본 환경의 갱신·좌표 변환 작업을 완료하고, 최종 시설 상태와 코드 커밋을 기록한다. 검증된 시설 백업을 만든 뒤 원본 서버와 배치를 종료해 이전 중 데이터가 갈라지지 않게 한다.
2. 새 환경에 저장소의 검증된 커밋과 Python을 준비한다. 실행 계정에 프로젝트의 `.cache` 쓰기 권한과 외부 API 접속 환경이 필요하다. Windows 전용 실행기를 다른 OS에서 그대로 사용하지 않는다.
3. 시설 백업을 안전한 경로로 전달하고 `python facility_backup.py verify --file <백업경로>`로 검사한다. 원본 DB의 WAL·SHM 파일을 따로 누락한 채 단순 복사하지 않는다.
4. 새 환경의 기본 DB 경로가 비어 있는지 확인하고 `python facility_backup.py restore --file <백업경로>`로 복원한다. 복원 도구가 디렉터리를 준비하므로 빈 DB를 미리 생성하지 않는다. 필요하면 `--database <경로>`를 지정한다.
5. `.env`와 실행 계정 환경 변수를 별도로 준비한다. API 토큰은 유지 또는 교체 여부를 정해 호출 측과 맞춘다. ngrok 계정 인증도 새 실행 계정에서 준비한다. 승인된 실행 환경·키 조건이 달라지는지는 제공자 안내를 확인한다.
6. 실제 경로 계산을 제공하려면 별도 `.cache/korea/roads.sqlite3`를 준비한다. 시설 백업에는 OSM 도로 DB가 포함되지 않는다. [도로 데이터 관리](route-api/README.md)를 따른다.
7. 먼저 `--disable-facility-refresh`로 로컬 서버를 시작해 health·시설 상태·지도 조회·데모 경로를 확인한다. 실제 도로를 준비했다면 실제 경로도 확인한다.
8. 기본 이외의 시설 DB를 쓰면 서버에는 `--facility-database`, 백업·복원·수집·좌표 도구에는 `--database`를 일관되게 지정한다. 더블 클릭 실행 파일은 기본 경로를 사용한다.
9. 새 외부 주소로 다른 기기에서 검증 후 호출 측 기본 주소를 전환한다. 원본 서버의 자동 갱신은 계속 꺼둔다. 검증 후 새 서버에서 자동 갱신을 활성화한다.

롤백 시 새 서버·배치를 먼저 종료하고 원본 서버와 기존 주소로 전환한다. 새 환경에서 추가 수집·좌표 변환을 수행했다면 어느 DB를 유지할지 먼저 결정한다. 두 환경의 SQLite 파일을 덮어쓰거나 임의로 병합하지 않는다.

## 상시 배포 전 남은 결정

현재 구현은 팀 테스트용 Python HTTP 서버·공유 Bearer 토큰·loopback 포트·ngrok 기반이다. 이 문서만으로 운영 서비스 배포가 완료되는 것은 아니다. 배포할 OS·호스팅·도메인을 정한 뒤 서비스 자동 시작·재시작, HTTPS 프록시, 실제 웹 Origin, 사용자별 인증·권한, 요청량·동시 계산 확장, 모니터링·백업 보존 정책을 별도 작업으로 마련한다. 프로세스를 여러 개 실행하면 요청 제한은 프로세스별로 나뉘므로 현재 제한을 전체 서비스 한도로 간주하지 않는다. 8082는 프록시 대상에서 제외한다.

좌표 승인 후에는 [좌표 변환 안내](facilities/TOILET_GEOCODING.md)에 따라 저장 조건 확인 → 백업 → 5건 시범 변환 → 지도 위치 검토 → 전체 변환 → 상태 API 확인 순서로 진행한다. 자정 갱신은 좌표 변환을 자동 호출하지 않는다.
