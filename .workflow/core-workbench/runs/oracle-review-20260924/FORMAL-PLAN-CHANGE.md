# Core Workbench p2.5 정식 계획 변경안

- 상태: **검토 대기**. BRIEF r1.12, SPEC s2.5, PLAN p2.5와 관련 문서를 정식 변경 후보로 작성하고 [Oracle 필수 수정](FORMAL-PLAN-ORACLE-REVIEW.md)을 반영했다.
- 권한 경계: 현재 요청은 계획 문서 작성만 승인한다. `PLAN.approval.implementation_authorized`는 `false`이며 제품 구현, runtime 시험, commit, 게시, 배포를 시작하지 않았다.
- 마지막 승인 기준: BRIEF r1.11 / SPEC s2.4 / PLAN p2.4. 기존 승인 기록은 변경하지 않았다.
- 결정 근거: C-D54의 A 방식, `IMPROVEMENT-PLAN.md`, `REVIEW.md`, oracle `ledger.json`, 구현 `HANDOFF-2026-09-24-2110.md`.

## 1. 확정한 변경

1. 부모 persistent Bash/sh는 `wb-handoff` 뒤 전용 control 경로에서 대기한다. PID, 준비된 cwd, exported environment와 검증된 conda/venv 상태를 유지한다.
2. 자동 실험 전체는 고정 supervisor/subreaper 아래 별도 interpreter 또는 executable에서 실행한다. Pipeline, 명령 치환, redirection을 부모가 먼저 평가하지 않으며 실험 내부 cd/export/source는 부모에 자동 반영하지 않는다.
3. Backend 전송, 수락, supervisor 시작, 실험 시작, 주 프로그램 반환, 전체 관리 대상 후손 종료 또는 `unknown`, 입력 반환, 부모 control 복귀를 서로 다른 사실로 기록한다.
4. Subreaper는 실험 시작 전에 설정 성공을 확인하고 한 실험만 담당한다. Supervisor 이전 수동 작업이 만든 daemon을 소급 관리한다고 가정하지 않는다.
5. Container 호환성 목표는 유지하지만 이번 변경안과 다음 G2 판별에서 Docker runtime 검증은 생략한다. 해당 근거는 pending으로 남긴다.

## 2. 선행 통합 검증

승인 후 첫 실행 단위는 CW-03의 입력 반환·signal·subreaper 최소 합성 판별이다. 단독 probe 결과를 합산해 통과로 판정하지 않는다.

| 축 | 최소 사례 | 필수 판정 |
|---|---|---|
| 실행 경계 | 정상 argv/script, Bash/dash `return`, `exec`, pipeline, 명령 치환 | 부모 PID/cwd/export 유지, 실험 전체가 자식 범위, prompt 입력 유출 없음 |
| 입력 반환 | `dd` 잔여 입력, key/paste/부분 write/terminal query reply, `wb-handoff`와 후속 줄의 한 write, foreground 종료 경합 | 장벽 진입→이전 writer 차단·정리→같은 부모 shell의 실제 읽기·prompt 복귀→새 정상 입력 처리 뒤에도 독립 marker 부작용이 없음. 알려진 유출 후보에서는 marker가 생김. `TCIFLUSH`만으로 parser/read-ahead와 송신 queue가 비었다고 판정하지 않음 |
| Signal·process group | 시작, 실행, 반환 장벽, 부모 WAIT에서 SIGINT, SIGQUIT, SIGTSTP/SIGCONT | 관측자 유지 또는 명시적 `unknown`, suspend와 exit 구분, 기본 untrapped script의 Ctrl-C 이후 tail 미실행 |
| 후손 수명 | 주 프로그램 반환 뒤 double-fork/setsid 후손 생존, 빠른 fork/exit, 종료와 추가 spawn 경합 | 단일 reaper 권한, wait 범위, SIGCHLD disposition, 자식 signal disposition 상속·정규화, spawn 폐쇄 조건을 기록. 생존 중 완료 금지, 전체 종료·회수 뒤 수명 종료. `waitpid(..., WNOHANG) == 0`이나 `ECHILD`를 단독 완료 근거로 사용하지 않고 관측 밖 생성을 완료로 숨기지 않음 |
| 관측 장애 | 실험 생존 중 supervisor 사망, control/recovery/event FD 손실 | 전체 종료로 선언하지 않고 `unknown`; 새 supervisor가 기존 후손을 소급 관리했다고 표시하지 않음 |
| 수동 단계 | 정상 준비 뒤 handoff, 수동 daemon 잔존 | 정상 사례는 성공하고 관측 불명 잔존은 자동 수락하지 않음 |

공유 foreground process group 후보를 먼저 검증한다. 관측자 정지나 job-control 충돌이 확인될 때만 분리 process group과 `tcsetpgrp`, SIGTTIN/SIGTTOU 동작을 비교한다. 임의 sleep 대신 전환 지점의 시험용 barrier를 사용한다.

최소 판별이 통과해야 G2의 환경, 입력/job, takeover, lifecycle, launch matrix와 CW-07 production adapter로 확장한다. 하나라도 불명확하면 자동 후속 실행과 production frontier를 열지 않는다.

## 3. G1·G3 보완과 통합 경계

- CW-02/G1은 동일 rows/columns, OMP version, theme, TERM/COLORTERM, font에서 원본 OMP와 실제 RGB/UI 화면을 비교한다. Cursor, SGR, redraw, 한글/wide 문자, 일반 terminal, 외부 tmux, 비중첩 Herdr 근거를 구분하며 접근할 수 없는 runtime 근거는 pending으로 둔다.
- CW-04/G3는 API 반환과 후속 오류를 분리하고, event의 원 session/run/turn과 callback 당시 current session을 구분한다. 같은 process reconnect의 미해결 상태 snapshot과 report/reply의 원 요청·task/revision/run identity를 확인한다. 공개 정보로 출처를 확정할 수 없으면 `unknown`을 유지한다.
- CW-05는 CW-02~04의 현재 통합 후보, required gate item, candidate/source identity와 근거 수준을 집계한 뒤에만 production frontier를 연다.
- CW-07은 G2에서 검증된 부모 control 대기, 별도 supervisor, 입력 반환과 `unknown`/no replay 계약만 소비한다.

## 4. 보존 사항

- CW-01은 완료 상태와 원문 계약을 보존하며 재실행하지 않는다. Ticket SHA-256은 `7dae35e6d1a4d2a3475609849178bb9ea05a6f790eb4931dc0139925ba75bcb2`다.
- CW-01~CW-16의 ID와 dependency graph는 p2.4와 같다. 새 단계 이름은 canonical ticket ID가 아니다.
- 기존 G1/G3 partial runtime 근거, G2 EOF/WAIT recovery와 A/subreaper 단독 probe는 관측한 입력과 후보 범위에 한해 보존한다.
- G2 직접 eval의 RUN 탈출, Bash/dash `return` 입력 유출, Ctrl-C 오류는 삭제하지 않고 새 후보의 음성 대조로 유지한다.
- 기존 승인 record, policy amendment, checkpoint와 사용자의 다른 변경은 수정하지 않았다.

## 5. 호출 예산과 실행 조건

- 누적 사용: 186/220회. 구현 ledger 184회, 별도 oracle_senior 자문 1회, 정식 계획 Oracle 검토 1회를 합산했다.
- 현재 예약: 0회. 잔여: 34회. 이번 정식 계획 검토에서 read-only child 호출 1회를 사용했다.
- 승인 후 첫 G2 합성 판별에는 최대 8회(worker 2, test_designer 2, reviewer/delta 2, correction reserve 2)를 운영 추정치로 제안한다. 현재 예약이나 전체 완료 약속이 아니다.
- 34회로 G1~G3 및 CW-05~16 전체 완료를 보장하지 않는다. 220회 상한을 run 변경으로 초기화하거나 자동 증액하지 않는다.

## 6. 검토 대상

정식 후보의 파일과 digest는 `plan-change-manifest.json`에 기록한다. 검토자는 다음을 확인한다.

1. C-D54 A 방식이 BRIEF, SPEC, ADR-0002, PLAN, OPERATING-CONTRACT와 CW-03/05/07에서 같은 의미인지.
2. 입력 반환, signal/process group, subreaper 후손 수명과 수동 daemon 공백이 CW-03에서 한 후보의 결합 검증으로 요구되는지.
3. CW-01, ticket ID/dependency, 유효한 기존 근거와 실패 근거가 보존됐는지.
4. G1 색상/실물 대조와 G3 오류/출처/reconnect/report identity 보완이 CW-02/04 및 소비 ticket에 연결됐는지.
5. `implementation_authorized=false`, 186/220회, 잔여 34회, Docker runtime pending 경계가 일치하는지.
