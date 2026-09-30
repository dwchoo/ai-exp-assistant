# 미해결 G1·G2·G3 해결안 — oracle_senior 자문

- 요청: 사용자가 `oracle_senior` 리뷰와 해결 방법 제안을 명시적으로 요청했다.
- 기준: Git HEAD `79808a78fb9eaca8a43caddf2fce268fade542ec`.
- 자문: `/root/unresolved_solutions_oracle`, `oracle_senior` 1회. 설정은 GPT-6 Astra / max, 실제 runtime model·effort는 미확인이다.
- 범위: 기존 코드·실패·probe·공개 API 근거를 읽은 기술 자문. 새로운 runtime 시험, 제품 코드·계획·승인·기존 구현 ledger 변경, 구현 재개는 수행하지 않았다.
- 기록: Oracle의 최종 자문을 Root가 정리했다. 아래 제안은 검증된 수정이나 gate 통과를 뜻하지 않는다.
- 호출: 기존 184회 + 이번 1회 = 185/220회, 잔여 35회. [별도 자문 ledger](ledger.json)를 후속 재개 시 함께 대조한다.

## 결론

A 방식과 실험별 subreaper는 권장 방향이다. 다만 부모 shell의 입력 복귀, foreground 전환 중 signal, supervisor 밖 수동 작업, G3 지연 event의 출처와 오류 유실은 별도 해결이 필요하다.

| 기존 제안 | Oracle 판정 | 보완 |
|---|---|---|
| 고정 supervisor와 별도 interpreter | 수정 후 수용 | 부모의 임의 eval 제거에 더해 입력 반환 장벽과 signal 검증 필요 |
| 실험별 subreaper | 조건부 수용 | 수명 관측·signal 전달·수동 작업 관측을 구분 |
| A 결정의 명세 반영 | 수용 | 기존 실패·완료 작업·유효 근거 보존 |
| G3 명시적 보고와 오류 처리 | 수정 후 수용 | 보고 전 오류, 원래 session 출처, 연결 단절 중 오류 보존 필요 |
| G1 실제 화면 비교와 최소 수정 | 수용 | RGB 축소는 코드에서 확인됨. 실제 영향과 수정 후 상태 복원 검증 필요 |
| G1/G3 독립 진행 | 조건부 수용 | 논리적 독립성과 live 자원 충돌을 구분하고 PLAN의 자원 조건 준수 |

## 1. G2: 확인된 결함과 기존 복구 근거

EOF·WAIT 복구 일부는 구현돼 있다. Recovery channel, epoch/token, slave flush, ACK 뒤 fresh READY 확인이 존재하며 당시 두 환경의 20개 시험이 각각 통과했다. Handoff의 “수동 복구 미구현”은 “부분 복구 구현·통과, RUN 탈출까지 포함한 전체 복구 미완성”으로 읽어야 한다.

반면 RUN 직접 eval에서는 다음 결함이 실제 관측됐다.

- Bash Ctrl-C: CONTROL_STOPPED 없이 READY로 복귀.
- dash Ctrl-C: tail 실행과 RETURN:0.
- Bash/dash return: control 함수를 벗어나 queued PTY 입력을 prompt 명령으로 실행.

근거: [복구 결과](../implement-p2.3-20260924/cw03-recovery-root-result.json), [RUN 경계 실패](../implement-p2.3-20260924/cw03-recovery-edge-root-result.json), [현재 eval 코드](../../../../src/workbench/terminal/shell_g2/prototype.py).

## 2. G2: 최소 권장 실행 구조

```text
persistent shell
└─ 고정 supervisor/subreaper
   └─ 실험 interpreter 또는 executable
      └─ 해당 실험의 후손
```

부모 control 함수는 고정 helper만 호출한다. Pipeline·명령 치환·redirection 등 실험 script 전체를 자식 interpreter 안에서 평가한다. 단순 argv는 shell 재해석 없이 실행할 수 있다. 부모 cwd·exported environment를 상속하고 supervisor 경로는 실험 PATH에 따라 바뀌지 않도록 고정한다. 실험 자식에 불필요한 control/recovery/event FD를 넘기지 않는다.

A probe의 6/6은 직접 자식 하나의 wait와 SIGINT 처리에 관한 부분 근거다. 별도 subreaper probe의 12/12 및 독립 7/7은 PTY·signal·takeover를 검증하지 않는다. 이 결과를 합산해 통합 성공으로 해석하지 않는다.

### 입력 반환 장벽 — 아직 검증할 설계

전체 실험이 종료돼도 supervisor가 즉시 사라지지 않고 반환 대기 상태에 남도록 하는 방안을 우선 검증한다. Backend가 이전 입력권을 회수하고 대기 중인 write를 차단·정리한 뒤, 부모 shell이 PTY를 다시 읽도록 반환한다.

`TCIFLUSH`는 terminal에 도착했지만 아직 읽히지 않은 입력을 지운다. Shell parser/Readline이 이미 읽은 bytes와 Workbench 송신 queue는 별개다. 따라서 flush 성공·ACK·fresh READY만으로 안전 복귀를 입증할 수 없다.

- Handoff 진입부터 미소비 입력이 남지 않는 지원 경로를 검증한다. `wb-handoff`와 후속 줄을 한 write로 전달하는 사례도 포함한다.
- Writer lock은 key·paste·부분 write·terminal query reply를 포함한 모든 PTY writer에 적용한다.
- 이전 owner/generation의 미전송 bytes는 새 입력 대상으로 넘기지 않는다.
- 부모의 PTY 읽기 재개 전에 입력권 회수·flush를 완료한다.
- Shell 내부에 남은 입력을 제거할 수 없으면 같은 PID의 shell별 parser reset 가능성을 별도로 검증한다. 새 shell로 조용히 교체하거나 모든 정상 복귀를 영구 거절해 완료로 처리하지 않는다.

현재 foreground PG 확인 뒤 write 사이에도 대상이 종료할 수 있다. Python lock만으로 process 전이를 멈출 수 없으므로 반환 장벽과 함께 검증해야 한다.

출처: [tcflush 매뉴얼](https://man7.org/linux/man-pages/man3/tcflush.3.html), [입력·복구 코드](../../../../src/workbench/terminal/shell_g2/control_probe.py).

### Signal·process group

현재 공유 foreground PG 후보부터 합성 시험을 수행하는 것이 작다. SIGINT뿐 아니라 SIGQUIT·SIGTSTP·SIGCONT와 부모 복귀를 확인한다. Supervisor의 signal 설정이 자식에 부적절하게 상속되지 않게 한다.

공유 PG가 관측자 정지나 job-control 충돌을 일으키면 supervisor와 실험 PG 분리를 검토한다. 이 대안은 관측자를 terminal signal에서 분리하기 쉽지만 tcsetpgrp·SIGTTIN/SIGTTOU·suspend/resume·부모 job 동작의 구현 부담이 늘어난다. 현재 근거로 확정하지 않는다.

### 후손 수명·장애·수동 작업

실험 시작 전 subreaper 설정 성공을 확인하고 실험 하나만 생성하며 단일 reaper를 둔다. 주 interpreter 종료와 전체 후손 종료를 구분한다. WNOHANG=0은 완료가 아니다. ECHILD도 wait 범위·SIGCHLD disposition·다른 reaper·clone 유형·추가 spawn 가능성이 통제된 조건에서 해석한다. 중간 부모가 회수한 모든 개별 exit status 수집까지 약속하지 않는다.

Supervisor 사망이나 관측 channel 손실은 LIFETIME_UNKNOWN이다. 새 supervisor를 시작해도 기존 고아 후손의 관측권이 자동 복구되지는 않는다.

실험별 subreaper는 시작 전에 수동 shell에서 생성된 daemon을 소급 관리하지 못한다. 빈 jobs나 /proc snapshot으로 수동 단계 전체가 깨끗하다고 선언하지 않는다. 확인 불가 시 자동 재개 보류를 유지하면서 정상 수동 준비→handoff 성공도 함께 입증한다. Session 전체의 subreaper 하나도 살아 있는 shell 아래 모든 수동 후손을 열거하는 해결책은 아니다. Cgroup을 필수로 만들거나 수동 동작을 제한해야 하는 경우에만 실제 변경을 사용자에게 제시한다. Supervisord 채택은 이 공백을 해결한다는 근거가 없어 현재 보류한다.

출처: [wait 매뉴얼](https://man7.org/linux/man-pages/man2/waitpid.2.html), [A 결정과 수동 단계 경계](../../notes/2026-09-24-local-lifetime-draft.md).

## 3. G2: 다음 최소 판별 실험

이번 자문에서는 실행하지 않았다. 임의 sleep 반복보다 시험용 barrier로 전환 직전·직후를 고정한다.

| 대상 | 실험 | 통과 기준 |
|---|---|---|
| 실행 범위 | Bash/dash return·exec·pipeline·명령 치환 | 부모 controller·PID·cwd/export 유지, prompt 입력 유출 없음, 정상 명령 실행 |
| 후손 수명 | 주 프로그램 먼저 종료, double-fork/setsid 후손 유지 후 종료 | 생존 중 완료 없음, 전체 종료·회수 뒤 수명 완료, 잘못된 WNOHANG 판정을 음성 대조로 탐지 |
| Signal·PG 전환 | 시작·실행·반환 대기·부모 WAIT에서 Ctrl-C, Ctrl-Z/재개 | 관측자 유지 또는 명시적 unknown, suspend를 exit로 처리하지 않음, 기본 untrapped script의 Ctrl-C 뒤 tail 없음 |
| 입력 저장 위치 | dd 잔여 입력, handoff 앞뒤 여러 줄·paste 한 write | kernel queue가 비어도 parser marker가 실행되면 실패. ACK/READY 대신 독립 파일 marker 등으로 실제 실행 확인 |
| 인수·복구 | 전송/수락/시작 경합, ACK 유실, stale epoch, FD 손실, flush 실패 | 즉시 새 전송 차단, no replay, 기존 실행 유지, 확인 대상 입력, 정상 경로 같은 shell 복귀 |
| 장애·수동 작업 | daemon 생존 중 supervisor SIGKILL, 수동 daemon 뒤 handoff | 사망을 완료로 오인하지 않음, 수동 잔존을 놓친 자동 수락 없음, UI 단절과 supervisor 사망 구분 |

## 4. G3: 명시적 보고 외에 필요한 수정

현재 코드에서 확인한 공백은 다음과 같다.

1. Assistant error 요약 전송이 eventSurfaceProbe 조건 아래에 있어 일반 경로의 후속 오류 보고 근거가 없다.
2. sendEvent는 callback 당시의 mutable sessionId/generation을 붙인다. 이전 session 지연 event의 원래 출처를 보장하지 않는다.
3. 연결이 끊기면 event를 버린다. 같은 process의 reconnect에서는 미해결 상태 snapshot을 재조회할 수 있어야 한다. Process 재시작 durable 복구는 후속 소유 ticket과 구분한다.

근거: [bridge](../../../../omp_bridge/g3/bridge.ts) 의 sendEvent·message_end handler, [기존 provider 관측](../implement-p2.3-20260924/cw04-provider-event-root-result.json).

권장안은 기존 report/reply 계약에서 원 요청 identity·task/revision/run·보고 자체 identity를 연결하고 수신 측이 유효한 요청과 대조하는 것이다. 구체적인 공개 extension tool 형식은 아직 확정하지 않는다.

| 관측 | 가능한 주장 |
|---|---|
| sendUserMessage 반환 | API 호출 반환 |
| 해당 ID의 user/provider 입력 | 해당 관측 지점에 입력 도달. Provider 증거는 시험한 형태에 한정 |
| 유효한 structured report | 모델이 해당 요청에 관한 보고 제출 |
| 익명 assistant 오류·연결 단절 | 오류 발생·관측 손실. 귀속 불명 요청은 unknown·보류 |
| 로컬 종료 + worker 보고 + manager 대조 | 근거를 결합한 업무 판단 |

보고 전 HTTP 오류·인증/네트워크 오류·abort·process 사망이 발생하면 보고가 생성되지 않을 수 있다. 보고가 먼저 와도 후손이나 도구가 살아 있을 수 있다. Singleflight는 현재 비동기 handler의 서로 다른 ID 경합을 줄이지만 원본 TUI 수동 입력·지연 event·provider 재시도의 인과관계를 증명하지 않는다.

### 공개 API 확장은 조건부

현재 SPEC은 ACK 손실·비동기 실패·identity 불명을 unknown으로 남기는 것을 허용한다. 모든 provider 오류의 확정 귀속을 새로운 필수 요구로 추가할 필요는 없다.

모든 오류의 요청별 귀속이 실제 필수라면 호출에 결합된 completion/error handle, 또는 원래 session·run/turn과 함께 정상·오류·abort까지 유지되는 correlation ID가 필요하다. 여러 provider 요청·재시도·여러 입력의 관계도 정의돼야 한다. 모델에게 ID를 되풀이시키거나 callback 시점의 현재 session을 읽는 것으로 대체하지 않는다.

필수 G3 처리 관측이 없는데 unknown을 기록했다는 이유만으로 gate를 통과시키지 않는다. API 확장도 process 사망 전 부작용이나 업무 완료까지 확정해 주지는 않는다.

## 5. G1: 화면 대조와 수정 범위

현재 `_color_index`의 RGB→6×6×6 cube 변환은 정적으로 확인되는 정밀도 손실이다. [코드](../../../../src/workbench/ui/terminal_g1/app.py)의 실제 OMP 화면 영향은 별도 대조한다.

원본 OMP와 Workbench의 pane 내부 rows/columns, 버전·theme·TERM/COLORTERM·font를 맞추고 한글·긴 줄·approval·paste·resize·색을 비교한다. 확인된 차이가 발생한 출력 경로만 수정한다. Curses에 raw RGB 출력을 섞는 경우 cursor·SGR·redraw 상태의 일치도 검증해야 한다. 새 renderer를 먼저 확정하지 않는다.

외부 tmux의 유효 근거는 보존한다. Nested Herdr의 시작 거부는 Workbench 렌더링 실패가 아니다. 일반 terminal·비중첩 Herdr는 해당 실제 환경에서 확인한다.

시작 지연은 process 시작→extension/bridge 준비→setup/composer 표시로 계측한다. Deadline 확대·재시도만으로 원인이 해결됐다고 선언하지 않는다.

## 6. 결정과 진행 순서

지금 다시 물어야 할 제품 결정은 없다. A 방식·부모 준비 환경 유지·실험 내부 변경 비전파·실험 후손 전체 종료 기준·subreaper 우선·준비 환경 사용·이번 Docker 검증 생략은 이미 결정됐다.

다음 변화가 실제 필요한 것으로 확인될 때만 해당 차이를 결정받는다.

- Same-PID 복구·수동 조작을 유지할 수 없어 입력 방식이나 shell 지원을 제한.
- 수동 daemon 관측 때문에 cgroup·추가 권한을 필수로 변경.
- 비수출 함수·변수 의존 명령까지 자동 복제 보장.
- Unknown을 허용하지 않는 추가 G3 보장을 위해 OMP 버전/API 조건 변경.

권장 순서:

1. A 결정과 BRIEF/SPEC/ADR/CW-03/PLAN의 실행 계약 차이를 정합 반영.
2. 입력 반환 장벽·signal·후손 수명을 포함한 최소 supervisor/subreaper 합성 판별.
3. 통과한 구조에 인수·장애 회귀와 독립 검증 통합.
4. G3 관측·오류 보류·재접속 상태 보존, G1 실물 대조를 자원 조건에 맞춰 진행.

G1/G3의 논리적 독립성을 동시 live 실행 허가로 해석하지 않는다. 현재 PLAN의 공유·배타 자원을 대조한다. 잔여 35회로 후속 전체 완료를 약속할 근거는 없으며 이 자문은 구현 재개·gate 통과 승인을 대신하지 않는다.
