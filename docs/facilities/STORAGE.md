# 시설 저장소 사용법

`facility_storage.py`는 Python 표준 라이브러리와 SQLite R-tree를 사용하는 내부 저장 모듈이다. 도로 DB와 분리된 `.cache/facilities/facilities.sqlite3`를 기본 경로로 사용한다. DB와 임시 파일은 기존 `.gitignore`의 `.cache/` 규칙으로 제외된다.

## 초기화와 저장

다음 예제는 실제 시설이 아닌 가상 입력이다. 프로젝트 루트의 Python에서 실행할 수 있다.

```python
from facility_storage import Facility, FacilityStore, ToiletDetails

store = FacilityStore()
store.initialize()
facility_id, = store.upsert_many([
    Facility(
        kind='toilet',
        source='mois_toilet_v2',
        source_region_code='test-region',
        source_record_id='example-1',
        name='가상 화장실',
        address_road='가상 주소',
        details=ToiletDetails(opening_hours_text='확인 필요'),
    )
])
print(store.get(facility_id))
```

`initialize()`는 명시적으로 한 번 호출한다. 반복 호출은 자료를 지우지 않는다. 별도 application ID와 스키마 버전 1을 검사하여 다른 DB·미지원 버전을 거절한다. 조회와 저장은 미리 초기화된 DB만 열며, 조회가 없는 DB를 새로 만들지 않는다. R-tree를 지원하는 SQLite 빌드가 필요하다.

`upsert_many()`는 **전체 레코드 교체**다. 부분 수정 API가 아니므로 생략된 선택 필드는 모델 기본값으로 대체된다. 동일한 `(source, source_region_code, source_record_id)`는 같은 내부 ID를 유지한다. 입력 순서대로 ID를 반환하며, 배치 안의 동일 식별자는 마지막 레코드가 반영된다. 다른 ID의 동일 위치 시설은 합치지 않는다.

수집기는 `preserve_local_state=True`를 사용할 수 있다. 기존 `active`를 유지하며, 새 입력이 좌표 없는 `pending`이고 주소 두 필드가 기존과 같을 때 기존 좌표·상태·변환 메타데이터를 보존한다. 주소가 달라지면 새 입력의 좌표 대기 상태를 사용한다. 비교와 저장은 같은 트랜잭션 안에서 수행한다. 기본값은 `False`이므로 기존 전체 교체 동작은 그대로다.

배치 전체와 위치 인덱스를 한 트랜잭션으로 저장한다. 잘못된 레코드나 입력 반복자의 예외가 발생하면 해당 배치의 삽입·수정이 모두 롤백된다. 식별자 없는 자료는 `ValueError`로 거절한다. 원본 격리 파일과 실패 기록은 향후 수집기가 담당한다. 직접 SQL로 변경하지 않고 이 모듈을 통해 쓰기를 수행한다.

## 모델과 좌표

공통 필드는 `Facility`, 화장실 상세는 `ToiletDetails`, CCTV 상세는 `CctvDetails`를 사용한다. 원본 API 필드 변환은 수집기에서 수행한다. 숫자 문자열이나 Y/N을 이 모듈에서 추정하지 않는다. 수량은 비음수 정수 또는 `None`, 여부는 불리언 또는 `None`을 받는다. 빈 문자열은 `None`으로 바꾼다.

- `fetched_at`은 기본 UTC 시각이며 직접 지정 시 UTC ISO 8601을 요구한다. 원본 날짜·시간 필드는 해석하지 않고 보존한다.
- 좌표는 `[경도, 위도]`에 해당하는 `longitude`, `latitude` 두 필드다. 부분 좌표, NaN, 무한대, 국내 경계 상자 밖 값은 거절한다. 불량 원본은 수집기가 격리하거나 좌표를 비우고 `invalid` 상태로 저장한다.
- 좌표 없는 화장실은 `coordinate_source='none'`, `coordinate_status='pending'`으로 저장된다. `get()`으로 조회할 수 있지만 지도 조회에는 나타나지 않는다.
- 정상 좌표에는 `coordinate_status='valid'`와 `coordinate_source='source'` 또는 `'geocoder'`가 필요하다.
- 지오코더 좌표는 제공자(규칙 버전 포함), UTC 변환 시각, `address_hash(address_road, address_lot)` 값이 필요하다. 주소 해시가 일치하지 않으면 좌표와 변환 메타데이터를 지우고 `pending`으로 저장한다. 이는 저장 검증이며 실제 지오코딩이나 제공자 사용 허용을 의미하지 않는다.
- `active=False`는 원본 처리에 따른 비활성 상태이며 실제 개방·작동 여부를 뜻하지 않는다.

## 지도 영역 조회

```python
result = store.query_bounds(
    west=127.00, south=37.27, east=127.02, north=37.29,
    kinds=('toilet', 'cctv'), limit=200,
)
```

반환값은 `items`, `count`, `truncated`다. `items`는 내부 ID순이며 `limit`은 1~500이다. 경계에 걸친 점은 포함한다. R-tree 후보를 실제 좌표와 다시 비교하여 인덱스의 부동소수점 반올림으로 생기는 바깥 점을 제거한다. 위치 변경·비활성화·좌표 상태 변경 시 인덱스도 트리거로 갱신한다.

활성 상태이면서 유효 좌표인 시설만 반환한다. CCTV는 `details.crime_prevention is True`인 경우에만 반환하며 카메라 대수만큼 항목을 복제하지 않는다. `None`과 `False`는 반환하지 않는다. 방범 목적 판정은 검증된 매핑을 가진 수집기가 수행하며 이 모듈은 원본 문구에서 추정하지 않는다.

반환 항목은 상세 정보·원본 출처·좌표 상태를 담은 **내부 자료**다. 공개 HTTP 응답으로 바로 전달하지 않는다. `get(id)`는 비활성·좌표 대기 자료도 반환하고 없는 ID에는 `None`을 반환한다.

## 범위와 검증

이 단계는 수집·삭제 동기화·최종 수집 시각 관리·HTTP API·인증·10km 조회 제한·지오코더 호출을 구현하지 않는다. `GET /v1/places`는 아직 사용할 수 없다. 조회량 제한은 저장소에 적용했으며 HTTP 거리 제한과 적재 상태는 해당 후속 브랜치에서 추가한다.

```sh
python -m unittest discover -s tests -p test_facility_storage.py -v
```

테스트는 임시 DB와 가상 시설만 사용한다. 동일 식별자 수정, 동일 좌표 별도 시설, 좌표 없는 화장실, CCTV 판정, 경계 정확도, 잘림, 위치 인덱스 갱신, 주소 변경, 배치 롤백과 입력 검증을 확인한다.
