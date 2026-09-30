# Formal plan Oracle review

- 대상: Core Workbench BRIEF r1.12 / SPEC s2.5 / PLAN p2.5 정식 변경 후보
- 방식: read-only 문서·구조·digest 대조. 제품 구현과 runtime 시험은 수행하지 않음.
- 판정: **approve with required doc fixes**
- 호출 반영: 186/220회 사용, 잔여 34회

## 필수 수정

1. 입력 반환 시험은 부모 PTY 읽기 재개 전 marker 부재에서 끝내지 않는다. 장벽 진입, 이전 writer 차단·정리, 동일 부모 shell의 실제 읽기와 prompt 복귀, 새 정상 입력 처리, marker 부재 확인까지 완료 조건으로 둔다. 알려진 유출 후보에서 marker가 실제 생기는 음성 대조를 포함한다. Key, paste, 부분 write, terminal query reply를 모두 반환 경계에 연결한다.
2. Subreaper 완료 판정에는 자식 signal disposition 상속/정규화, 단일 reaper 권한, wait 범위, SIGCHLD disposition, 추가 spawn 가능성의 통제 근거가 필요하다. 정상 자식 signal 반응, 빠른 fork/exit, 주 프로그램 종료와 후손 생성 경합을 대조하고 지원 범위 밖 생성 방식을 조용히 완료 처리하지 않는다.
3. PLAN의 마지막 승인 manifest는 p2.4를 실제 승인한 `cd53-approved-manifest.json`을 가리켜야 한다. `cd53-policy-approval.json`을 마지막 승인 record로 연결하고 기존 p2.3 승인·검토 record는 별도 역사 필드로 보존한다.

## 확인된 보존·정합

- Manifest의 candidate/source/checkpoint/snapshot/scope audit digest가 일치했다.
- CW-01 SHA-256과 CW-01~16 ID·dependency graph가 보존됐다.
- 부모 준비 환경 보존, 별도 실험 실행, 내부 상태 비전파, 후손 수명과 결과 판단 분리가 일관된다.
- G1 실제 화면 대조와 G3 오류·원 출처·reconnect snapshot·report identity의 소유 및 CW-05/07 소비 경계가 연결됐다.
- 유효한 과거 근거와 직접 eval 실패가 보존됐고 단독 probe가 통합 통과로 승격되지 않았다.
- `implementation_authorized=false`, Docker runtime 생략과 container 근거 pending 경계가 유지됐다.

## 선택적 개선

- CW-02/CW-04 요약과 PLAN의 최소 실험 범위를 실행 전 더 맞추면 혼동을 줄일 수 있다. PLAN이 원본이므로 현재 blocker는 아니다.
- 이번 review 이후 예산 snapshot은 186/220회, 잔여 34회로 갱신해야 한다.

추가 oracle_senior 자문이 필요한 새 설계 모순은 발견하지 않았다. 필수 문서 수정은 검토 후보 수용 조건이며 제품 구현 승인이나 G2 통과 판정이 아니다.

