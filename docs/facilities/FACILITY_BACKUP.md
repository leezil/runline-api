# 시설 SQLite 백업·복원

## 백업

Windows에서 `backup-facilities.cmd`를 더블 클릭한다. 결과와 저장 경로가 표시되고 아무 키나 누르면 창이 닫힌다. Python이 PATH에 있어야 한다. API 서버가 켜져 있어도 백업할 수 있으며, 갱신·복원 작업이 실행 중이면 `FACILITY_JOB_BUSY`로 중단하므로 완료 후 다시 실행한다.

```powershell
.\backup-facilities.ps1
# 다른 DB 또는 별도 디스크에 백업
python facility_backup.py backup --database .cache/facilities/facilities.sqlite3 --backup-dir D:\runline-backups
```

기본 위치는 `.cache/facilities/backups/backup-<UTC 시각>-<고유 ID>.sqlite3`다. SQLite 온라인 백업 기능을 사용해 WAL의 커밋된 변경도 포함한다. 단순 파일 복사는 하지 않는다. 백업 사본의 `integrity_check`, 스키마 버전·애플리케이션 ID·색인·트리거 및 상태 조회를 검사한 뒤 최종 파일명으로 공개한다. 백업 파일은 자동 삭제하지 않는다.

시설 원본·좌표·활성 상태·수집 성공 및 실패 기록·누락 유예와 갱신 체크포인트 등 DB 전체가 포함된다. `.env`, 인증키, 작업 로그, DB 옆 날짜별 자동 실행 기록은 포함하지 않는다. 기본 백업 폴더는 `.cache/`, 별도 위치의 `*.sqlite3` 파일도 기존 `.gitignore` 규칙으로 Git에서 제외된다. PC 디스크 자체의 고장에도 대비하려면 별도 디스크에 백업을 보관한다.

## 검사

```powershell
python facility_backup.py verify --file "D:\runline-backups\backup-....sqlite3"
```

`verified`와 시설 상태 집계를 출력한다. 파일 자체는 변경하지 않는다. 검사 성공은 DB 구조·무결성이 정상이라는 의미이며 데이터가 최신이라는 보장은 아니다.

## 복원

복원은 선택한 백업 시점으로 DB 전체를 되돌린다. 안전한 운영을 위해 API 서버와 직접 실행 중인 수집기·좌표 변환기를 종료하고 다음 명령을 실행한다.

```powershell
.\restore-facilities.ps1 -BackupFile "D:\runline-backups\backup-....sqlite3"
# 다른 DB 경로로 복원
python facility_backup.py restore --file "D:\runline-backups\backup-....sqlite3" --database .cache/facilities/facilities.sqlite3
```

1. 갱신 실행기와 같은 DB별 잠금을 획득한다. 충돌 시 즉시 실패한다.
2. 선택한 원본의 일관된 사본을 만들어 먼저 검증한다.
3. 기존 DB가 있다면 검증 후 `before-restore-*.sqlite3`로 별도 백업한다. 이 단계가 실패하면 복원하지 않는다.
4. 기존 DB는 SQLite 백업 트랜잭션으로 복원한다. WAL·SHM을 직접 삭제하거나 열린 DB 파일을 교체하지 않는다. 복원 도중 실패하면 SQLite가 미완료 변경을 롤백한다.
5. 복원 후 무결성을 다시 검사하고 `restored`, `safety_backup` 경로를 출력한다.

현재 DB가 없으면 완성된 사본을 같은 파일시스템의 하드 링크로 원자적으로 생성한다. 하드 링크를 지원하지 않는 파일시스템에서는 실패하며 기존 파일을 덮어쓰지 않는다. 본체 없이 WAL·SHM·journal이 남아 있으면 자동 복원하지 않는다. **현재 DB가 손상됐거나 다른 종류의 DB라면 덮어쓰지 않고 실패**하므로 원본과 부속 파일을 보관한 뒤 별도 경로에 복원해 오프라인으로 점검한다.

복원 직후 서버를 재시작하고 `/v1/places/status`로 건수와 시각을 확인한다. 복원 전 데이터로 돌아가려면 출력된 `safety_backup`을 다시 복원한다. 날짜별 자동 실행 기록은 그대로 유지되므로 당일 재갱신이 필요하면 수동 갱신 파일을 실행한다.

직접 실행하는 수집기·좌표 변환기·외부 SQLite 도구는 갱신 실행기의 잠금을 사용하지 않는다. 복원 중에는 이러한 쓰기 작업을 반드시 중지한다. 잠금 대기와 복사 진행은 제한된 시간 내 실패하도록 처리하며, 실패 시 원본 백업과 `before-restore` 사본을 유지한다.

종료 코드 0은 성공, 1은 실패다. 오류에는 원본 예외나 인증키를 출력하지 않는다. 주요 오류 코드는 `FACILITY_JOB_BUSY`, `FACILITY_DB_MISSING`, `BACKUP_FILE_MISSING`, `BACKUP_INTEGRITY_FAILED`, `UNSUPPORTED_FACILITY_DATABASE`, `DATABASE_COPY_TIMEOUT`이다. 파일 접근·SQLite 오류는 `DATABASE_BACKUP_RESTORE_ERROR`로 반환한다.
