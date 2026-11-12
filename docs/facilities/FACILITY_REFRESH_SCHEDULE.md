# 시설 수동·자동 갱신

수원(3740000)과 안양(3830000)의 화장실·CCTV를 기존 [갱신 처리](FACILITY_REFRESH.md)로 갱신합니다. 좌표 변환 API는 호출하지 않습니다. 기존 SQLite DB와 `.env`의 `PUBLIC_DATA_SERVICE_KEY`가 필요합니다.

## 원할 때 실행

Windows에서 프로젝트의 `refresh-facilities.cmd`를 더블 클릭합니다. API 서버가 꺼져 있어도 실행할 수 있습니다. Python 3.10 이상이 PATH에 있어야 합니다. 결과 확인 후 아무 키나 누르면 창이 닫힙니다.

PowerShell에서는 다음과 같이 실행합니다.

```powershell
.\refresh-facilities.ps1
# API를 조회하되 DB에는 반영하지 않는 검증
.\refresh-facilities.ps1 -DryRun
```

다른 OS 또는 별도 DB 경로는 다음 명령을 사용합니다.

```sh
python facility_refresh_job.py --database /path/to/facilities.sqlite3
```

## 서버가 켜져 있을 때 자동 실행

`python api_server.py`로 서버를 실행하면 매일 **자정 00:00, 한국 시간(KST)** 에 갱신을 시작합니다. `start-route-api.cmd`로 실행한 API 서버에도 적용됩니다. 변경 전부터 켜져 있던 서버는 재시작해야 합니다. 기존 실험용 `server.py`에는 적용되지 않습니다.

- API 요청 처리와 별도 프로세스로 갱신하며, 앱 조회에는 API 서버 실행이 필요합니다.
- PC가 꺼져 있거나 서버가 종료된 동안 놓친 실행은 보충하지 않습니다. 절전 복귀 등 지연은 5분까지만 허용합니다. 놓친 작업은 수동 파일로 실행하세요.
- 갱신이 이미 시작됐다면 API 서버를 종료해도 해당 배치는 완료될 수 있습니다. PC 종료는 작업을 중단시킵니다.
- 같은 DB를 사용하는 수동·자동 작업은 OS 파일 잠금으로 중복 실행을 건너뜁니다. 자동 실행은 날짜당 한 번 시도합니다. 실패·중단 후 재시도는 수동 실행 파일을 사용하세요.
- 직접 `facility_refresh.py`나 개별 수집기를 실행하면 이 실행기 잠금을 사용하지 않으므로, 정기·수동 운영에는 위 실행기를 사용하세요.

시간 변경 또는 자동 실행 해제:

```sh
python api_server.py --facility-refresh-time 03:00
python api_server.py --disable-facility-refresh
```

자동 작업은 서버의 Python 실행 파일과 `--facility-database` 경로를 그대로 사용합니다. 환경 변수의 인증키가 `.env`보다 우선합니다. Windows 작업 스케줄러 등록이나 Codex 앱 실행은 필요하지 않습니다.

## 결과 확인

- `.cache/facilities/job-runs/*.json`: 수동·자동 실행 요약 및 오류 코드
- `.cache/facilities/refresh-runs/*.json`: 소스·지역별 상세 갱신 결과
- `.cache/facilities/schedule-last.json`: 마지막 자동 실행 또는 지연 건너뜀
- DB 옆 `<DB 파일명>.refresh-job/`: 실행 잠금과 날짜별 시도 기록. 실행 중 삭제하지 마세요.

종료 코드 0은 성공·검증 완료·중복 건너뜀, 1은 실패 또는 일부 실패입니다. DB가 없으면 `FACILITY_DB_MISSING`으로 종료하며 먼저 초기 수집이 필요합니다. 원본 API 실패 시 기존 데이터 유지와 누락 유예는 기존 갱신 정책을 따릅니다. 로그·실행 기록은 자동 삭제하지 않습니다. 기본 DB·로그·키는 Git 제외 대상입니다.
