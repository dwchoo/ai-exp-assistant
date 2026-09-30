# G1·G2·G3 개선 계획 초안

- 작성 근거: 사용자 개선 계획 요청, [oracle_senior 자문](REVIEW.md), [A 방식 결정](../../notes/2026-09-24-local-lifetime-draft.md).
- 기준: HEAD `79808a7`, BRIEF r1.11 / SPEC s2.4 / PLAN p2.4, CW-01 통합과 G1/G3 partial·G2 blocked 기록.
- 상태: 검토 가능한 개선 계획 초안. 승인된 PLAN의 대체본이나 구현 재개 기록이 아니다.
- 목표: 기존 G1·G2·G3의 차단 원인을 해결하고 필수 근거를 갖춰 CW-05의 선행 조건을 충족한다. 전체 제품 완료는 후속 CW-05~16의 검증을 포함한다.
- 현재 추가 제품 결정: 없음. 아래 조건부 변경이 실제 필요할 때 그 차이에 대해서만 결정받는다.

## 1. 확정 전제와 설계 가설

### 이미 결정된 전제

- 준비된 persistent shell의 PID·cwd·exported environment를 유지한다.
- A 방식으로 실험은 별도 interpreter 또는 실행 파일에서 수행한다. 실험 내부 cd/export는 부모에 자동 반영하지 않는다.
- 관리 대상 실험 후손의 전체 종료를 확인한 뒤 결과 판단·후속 수정을 진행한다. 부모 shell과 Workbench는 실험 종료 대상이 아니다.
- Subreaper를 우선 검증한다. 준비된 환경을 사용하고 이번 Docker runtime 검증은 생략한다.
- 확인된 foreground 대상에 수동 입력을 제공하고 인수 요청만으로 실행을 종료하지 않는다. 불명 요청은 자동 replay하지 않는다.
- 원본 OMP TUI·공개 extension과 Linux/Bash/sh 지원 범위를 유지한다.

### 시험으로 결정할 구현 사항

- Supervisor의 입력 반환 장벽과 signal·process group 구성.
- Shell parser/read-ahead를 포함한 handoff 진입·복귀 경계.
- 수동 단계 잔존 작업을 놓치지 않는 관측 범위와 보류 조건.
- G3의 공개 event로 확인 가능한 출처, 오류 상태 snapshot, report/reply의 식별 연결.
- G1의 RGB 보존 출력 경로와 실제 화면 차이의 최소 수정.

이 가설들을 이미 검증된 해결책으로 명세에 기록하지 않는다.

## 2. 작업 단계와 완료 기준

단계 이름은 설명용이며 새 canonical ticket ID가 아니다. 기존 CW 번호를 보존한다.

| 단계 | 기존 소유 | 선행 | 핵심 산출물 | 다음 단계로 넘어갈 기준 |
|---|---|---|---|---|
| A 계약 정합 반영 | Root 계획 개정 | 현재 결정·자문 | BRIEF/SPEC/ADR/CW-03/PLAN의 변경안과 요구 추적표 | 같은 shell 보존과 자식 실행의 의미가 일치하고 검증 소유·사용자 승인 참조가 정리됨 |
| G2 최소 합성 판별 | CW-03 | 정합한 실행 기준 | 입력 반환·signal·subreaper를 결합한 작은 probe와 독립 반례 시험 | 정상 실행과 핵심 실패 사례가 함께 통과. 미확인 가정에 의존한 제품 확장 없음 |
| G2 지원·복구 완성 | CW-03 | 합성 판별 통과 | 환경·job·인수·장애·시작 matrix, candidate와 독립 review | G2 6개 필수 item 전체에 현재 근거 확보 |
| G3 오류·식별·재접속 보강 | CW-04 | CW-01, 현행 G3 계약 | 일반 오류 보고, 출처 불명 처리, 같은 process 상태 재조회, 보고 식별 검증 | G3 6개 필수 item 전체에 현재 근거 확보 |
| G1 색상·실물 호환성 보강 | CW-02 | CW-01, 실제 검증 환경 | 화면 대조와 최소 수정, 외부 환경 기록 | G1 7개 필수 item 전체에 현재 근거 확보 |
| 통합 인계 | Root / CW-05 진입 | CW-02·03·04 통합·검증 | 최종 후보·재사용 근거·실패/미확인·후속 계약 영향 | 승인된 PLAN 의존성과 자원 조건에 따라 CW-05 readiness 재산정 |

G1/G3의 독립 작업은 G2 완료를 기다릴 논리적 이유가 없다. 다만 live 자원과 workspace 조건은 아래 제약을 따른다.

### 단계 A: 계약 정합 반영

1. BRIEF·SPEC의 보존 대상은 준비된 부모 shell, 실험 평가 대상은 별도 실행 단위로 구체화한다.
2. ADR-0002의 과거 결정은 이력으로 남기고 A 결정에 의해 대체된 부분을 표시한다.
3. CW-03·E-G2-CONTROL·G2 gate item의 실행 방식 표현과 검증 요구를 함께 바꾼다. 요구를 조용히 삭제하거나 실패 테스트 기대를 완화하지 않는다.
4. OPERATING-CONTRACT·DECISIONS·후속 CW-05/CW-07 등 소비 계약을 검색해 실제 영향이 있는 표현만 정합하게 조정한다.
5. 기존 CW-01, 안정된 ticket ID, G1/G3의 유효 근거를 보존한다. 같은 shell 복구는 “부분 구현·통과, RUN 경계 미완성”으로 정확히 기록한다.
6. 어떤 문구가 기존 사용자 A 결정을 반영하고 어떤 부분이 미검증 구현 제안인지 구분한 diff와 approval manifest를 준비한다. 현재 초안은 이 정식 개정을 실행하지 않았다.

### 단계 G2-1: 최소 합성 판별

고정 supervisor를 부모 shell이 시작하고, 실험 script 전체를 자식에서 평가한다. Supervisor 경로를 고정하고 실험 환경을 보존하며 불필요한 control FD 상속을 막는다. 새로운 범용 process framework나 패키징 확장을 먼저 만들지 않는다.

핵심 불변식:

- 부모가 실험 문자열을 직접 eval하지 않는다. Pipeline·명령 치환도 실험 관측 범위에 포함한다.
- 주 프로그램 종료와 전체 실험 수명 종료를 구분한다. 살아 있는 후손이 있으면 완료하지 않는다.
- Supervisor는 종료 후 입력 반환 장벽에 머무는 후보를 시험한다. Backend가 이전 owner의 write를 차단·정리한 뒤 부모가 PTY 읽기를 재개한다.
- Key/paste/부분 write/query reply를 포함한 모든 writer를 같은 입력 소유 규칙으로 관리한다.
- TCIFLUSH로 shell parser 내부 입력까지 지웠다고 가정하지 않는다.
- Signal·FD 단절·supervisor 사망으로 근거가 끊기면 unknown을 유지한다. 인수는 자동 취소·재실행이 아니다.

| 최소 시험 | 성공 증거 | 실패 시 조치 |
|---|---|---|
| 정상 argv/script, return·exec·pipeline·명령 치환 | 부모 PID/cwd/export 유지, 자식 실행 범위 확인 | 경계를 수정하고 같은 사례 재검증 |
| 주 프로그램 종료 뒤 double-fork/setsid 후손 생존 | 생존 중 완료 없음, 전체 회수 후 수명 종료 | 종료 판정 보강 전 자동 후속 실행 차단 |
| 시작·실행·반환 장벽·부모 WAIT에서 Ctrl-C, Ctrl-Z/재개 | 관측 유지 또는 명시적 unknown, suspend와 exit 구분, 기본 script의 중단 뒤 tail 없음 | 공유 PG 한계를 확인한 뒤 PG 분리 대안을 제한적으로 시험 |
| dd 잔여 입력과 handoff+후속 줄의 단일 write/paste | 독립 파일 marker 등의 부작용 없음 | Parser/read-ahead 가정을 실패로 기록. 같은 PID 복구가 가능한지 판별 |
| Foreground 확인 직후 대상 종료, 부분 write 경합 | 입력이 새 부모 prompt로 유출되지 않음 | 반환 장벽·writer 제어를 함께 수정 |
| 실험 생존 중 supervisor 사망 | 전체 종료를 선언하지 않고 unknown | 새 supervisor로 소급 복구됐다고 처리하지 않음 |
| 수동 준비 정상 사례와 수동 daemon 잔존 사례 | 정상 handoff 성공, 관측 불명 잔존에서 자동 수락 없음 | 수동 단계 관측 공백을 독립 blocker로 유지 |

임의 시간 지연에 기대지 않고 전환 지점에 시험용 barrier를 둔다. Probe가 입력 유출을 탐지할 수 있는 음성 대조를 포함한다. 기존 단독 probe 통과 수를 합산해 성공을 선언하지 않는다.

### 단계 G2-2: 지원·인수·복구 matrix

최소 합성 판별이 통과한 경로에 다음을 결합한다.

- G2-CONTROL: 정상 실행, 최초 승인/진행 fixture, cwd/export 보존, stdin/control 분리.
- G2-INPUT-JOBS: 여러 줄·미제출 입력·REPL·background/suspend·stale owner/generation·수동 잔존.
- G2-HOOK-ENV: conda/venv activation/deactivation 정상 사례와 비호환 hook/trap의 보류. 의도적 내부 변조와 일반 race를 구분.
- G2-TAKEOVER: 전송 전/후·수락/시작 이후·ACK 유실별 요청/확인, 실행 유지, 확인된 foreground 입력, no replay.
- G2-LIFECYCLE: 정상/비정상 exit, 후손, control/recovery/event FD 손실, flush 실패, supervisor 장애와 같은 shell 복구.
- G2-LAUNCH: Bash 우선·Bash 없는 PATH의 sh·둘 다 없을 때 안내, 선택한 실행 파일 고정.

수명 종료·부모 shell 반환·실험 성공을 별도 사실로 기록한다. G2는 worker/manager의 결과 판단을 구현하지 않는다. Docker 지원 완료는 이번 host 시험 결과로 선언하지 않는다.

### 단계 G3: 오류·출처·재접속

1. API 반환·ID별 입력 관측·모델의 보고·업무 완료를 구분한다. 기존 api_accepted 표기의 의미는 SPEC의 api_returned 수준에 맞추고 caller 영향도 함께 검증한다.
2. 일반 실행에서도 후속 오류·관측 손실을 host에 알린다. 익명 오류를 특정 요청 실패로 단정하지 않는다.
3. Event의 원래 session/run/turn 출처를 공개 API에서 확인 가능한지 먼저 판별한다. Callback 당시 현재 session을 덧붙인 값을 원래 출처로 쓰지 않는다. 확인 불가 event는 출처 불명으로 남긴다.
4. 같은 OMP process의 연결 단절 동안 미해결 상태를 보존하고 reconnect snapshot으로 대조한다. Process 재시작의 durable 복구는 기존 CW-09/15 소유를 유지한다.
5. 자동 전달의 서로 다른 ID 간 경합을 제한하고, 기존 report/reply의 원 요청 ID·task/revision/run·보고 ID를 검증한다. G3에서는 식별 전달을 검증하며 worker 결과 분석·manager 판단은 CW-10/13을 유지한다.
6. 기존 G3-TUI-DELIVERY, G3-TUI-CONTENTION, G3-SESSION, G3-RECONNECT, G3-PAUSE, G3-EXTENSION을 정상·오류·unknown·session 전환·지연 중복 사례로 대조한다. Draft·approval·사용자 입력 보존과 no replay를 유지한다.

공개 API가 원 출처를 제공하지 않으면 확정 귀속 보장은 차단 상태다. 현재 계약이 허용하는 unknown은 유지하되 필요한 정상 처리 관측까지 생략하지 않는다. 모든 오류의 확정 귀속을 새 요구로 자동 추가하지 않으며, 실제로 더 강한 보장이 필요할 때만 API/지원 버전 변경을 제시한다.

### 단계 G1: 화면·색·외부 환경

1. RGB 축소 경로의 단위 수준 사실과 실제 OMP 화면 영향을 구분한다. 동일 pane rows/columns·버전·theme·TERM/COLORTERM·font로 대조한다.
2. 한글·wide 문자·긴 줄·composer·approval·tool 출력에서 색/구분/줄바꿈/잘림/겹침 차이를 확인한다. 확인된 출력 경로만 수정한다. Cursor·SGR·redraw 회귀를 함께 본다.
3. 일반 terminal·외부 tmux·비중첩 Herdr에서 입력·paste·resize·alternate-screen 복원을 시험한다. 환경이 없으면 해당 runtime 증거를 pending으로 두고 필요한 실제 접근만 요청한다.
4. 시작 지연은 process→extension/bridge→setup/composer의 시점을 기록해 원인을 분리한다. Timeout 확대만으로 해결됐다고 처리하지 않는다.
5. G1-RESIZE, G1-THREE-PANE, G1-TEXT-TOOLS, G1-PASTE, G1-VT-QUERY, G1-COLOR, G1-OUTER 전체를 대조한다. 2 MiB 초과 또는 queue 부족 paste의 전체 거절도 누락하지 않는다.

외부 tmux의 과거 통과는 동일 입력·후보 영향 범위에서 재사용한다. RGB/renderer 수정으로 영향받은 화면 근거는 다시 확인한다.

## 3. 독립 검증·통합·자원

- 각 일관된 검증 단위는 최소 판별→구현·quick check→독립 test_designer→필요 수정→후보 동결→fresh reviewer→필요 delta 검증→Root 통합 순서다.
- 필수 실패가 남아 있는데 최종 review를 반복하지 않는다. 과거 실패와 새 계약에 대한 대응표를 남기고 실제 안전 불변식은 유지한다.
- 기존 PLAN이 필수로 요구하는 item을 기준으로 근거를 집계한다. 이번 표의 핵심 사례만 통과하고 전체 gate가 통과했다고 선언하지 않는다.
- G1과 G3는 host:omp-runtime·host:interactive-terminal을 exclusive로 공유하므로 live 실행을 직렬화한다.
- G2는 host:shell-fixture를 요구한다. 통합 PTY 시험이 다른 live 자원도 쓰게 되면 정식 계획에 resource claim을 추가한 뒤 배정한다.
- 같은 물리 workspace의 writer는 한 명이다. 병렬 편집은 검증된 별도 workspace와 비충돌 자원에서만 진행한다.
- CW-05 진입 전 CW-02·03·04의 통합된 후보와 변경 영향 근거를 확인한다. Gate 집계 validator와 최종 공통 계약은 기존 CW-05 소유다.

## 4. 예산·중단·사용자 결정 경계

자문 이후 호출은 185/220회, 잔여 35회다. 이번 계획 작성에는 child를 추가 호출하지 않았다. 이전 구현 ledger만 읽어 36회가 남았다고 계산하지 않는다.

실행 시 첫 배치는 G2 최소 합성 판별에 한정해 내부 계획치 최대 8회(worker 2·독립 test 2·review/delta 2·수정 여유 2)를 제안한다. 이는 현재 예약·호출이 아니며, 같은 후보를 불필요하게 반복 review하라는 지시도 아니다. 정식 실행 소유자가 실제 남은 필수 검증·호출 내역을 대조해 배분한다. 명시적 220회 상한은 자동으로 높이지 않는다.

새 근거를 얻지 못한 동일 수정 반복은 멈추고 판별 실험이나 접근을 바꾼다. 호출 예산이 남았다는 이유로 필수 가정의 실패를 무시하지 않는다. 남은 35회로 G1~G3와 후속 CW-05~16 전체 완료를 약속하지 않는다. 상한 증액이 실제 필요해지면 남은 필수 작업·추정 호출과 함께 별도 요청한다.

| 사용자 의견이 필요한 조건 | 그때 제시할 내용 | 현재 방침 |
|---|---|---|
| 같은 PID의 안전 복구가 불가능해 입력 방법·shell 지원 제한 필요 | 실패 재현, 가능한 대안, 사용자 조작 차이 | 현재 방식 보존을 우선 시험 |
| 수동 daemon 때문에 cgroup·추가 권한이 필수 | 준비 환경별 관측 실패와 권한 없는 대안의 한계 | Subreaper 우선·불명 보류 |
| 비수출 함수·변수까지 자동 복제해야 실제 사용을 충족 | 구체 명령과 보존 범위 차이 | 이미 결정한 cwd/exported environment 범위 |
| G3에 unknown 없는 완전 귀속이 실제 필요 | 공개 API의 부족한 정보, API/버전 변경 비용 | 현재 계약의 unknown/no replay 유지 |
| 일반 terminal·비중첩 Herdr 접근 불가 | 필요한 환경과 가장 작은 사용자 협조 | 해당 증거 pending, 다른 독립 작업 진행 |
| 220회 상한으로 필수 검증 불가능 | 누적 사용량·남은 작업·필요 증가분 | 자동 증액 없음 |

현재는 위 가정을 사용자에게 다시 선택하게 할 필요가 없다. 구현 방식의 정상적인 선택과 검증은 실행 담당자가 처리하고, 사용자 행동·권한·지원 약속을 바꿔야 하는 경우만 구체 근거를 제시한다.

## 5. 다음 산출물

다음 작업은 이 초안을 근거로 A 정합성 변경을 반영한 정식 BRIEF/SPEC/PLAN/ticket bundle을 준비하는 것이다. 저장소 workflow의 계획 세션에서 기존 ID·근거를 보존하고 승인할 변경분을 명확히 한다. 이 문서 작성으로 기존 구현 run의 중단 상태나 승인 기록을 바꾸지 않았다.
