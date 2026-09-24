# Core Workbench — 차단 문제 진단과 해결 방향

Revision: d2; state: 진단·해결 방향이 r1.10/s2.3/p2.3 승인 묶음에 반영됨. 작성일: 2026-09-24.
이 문서는 기존 Core의 추가 진단이며 새 기능이나 실행 ticket 묶음이 아니다.
이 문서 작성 당시 구현은 사용자 요청으로 중단돼 있었다. 개별 사용자 결정과 아직 검증하지 않은
설계 제안을 구분한다. 이후 명시적 implement 요청과 승인 기록은 [현재 승인 기록](../../../.workflow/core-workbench/runs/implement-p2.3-20260924/approval.json)에 있다. 새 gate 통과를 뜻하지 않는다.

## 문제, 사용자와 목표

CW-01만 통합됐고 CW-02~04의 기술 검증이 끝나지 않아 CW-05~16이 대기 중이다.
사용자는 기존 수정·테스트·review 반복을 바로 재개하는 대신 Astra에서 실패 원인과
해결 방향을 먼저 정하도록 요청했다. 목표는 각 문제의 요구사항, 실패 근거, 남은
가설과 판별 기준을 확정하고, 기존과 다른 접근이 필요한 지점을 정하는 것이다.

## 공통 용어와 유지할 범위

[CONTEXT.md](../../../CONTEXT.md), [BRIEF](BRIEF.md),
[제품 동작 규약](OPERATING-CONTRACT.md)을 따른다.
Python 중심·Linux 우선, 원본 OMP TUI 두 개, 내부 외부 multiplexer 금지,
같은 Bash/sh의 cwd·export·선택 환경 보존, 사용자 입력 제어권, C-D49의
일시정지·worker 역할은 이번 진단의 기존 조건이다. 변경은 개별 사용자 결정으로만 한다.

제품 코드·테스트 수정, runtime probe, ticket 실행, commit은 이번 단계에서 하지 않는다.
실험 설계는 이후 승인된 구현에서 실행할 입력으로 남긴다.

## 현재 근거와 문제 분류

관측 기준: Git HEAD `0a67572834ddfd6888b534f8a85213f4b1a2e1ca`와 현재 staged G1 변경.
이 진단에서는 과거 테스트를 재실행하지 않았다. 아래 통과·실패 수는 기록된 결과다.

| 문제 | 확인한 사실 | 아직 모르는 것 | 진단 |
|---|---|---|---|
| D1 / CW-02 / G1 | 실제 OMP 승인·도구 side effect·화면 출력·provider 후속 요청·최종 응답 확인. 29개 fixture/predicate 테스트 통과 및 delta review 기록이 있음 | 일반 resize, 실제 사용자 terminal/tmux/herdr 조합, query reply의 OMP 내부 처리 | 제품 결함 확정과 미확인을 구분해야 한다. 기존 resize 판별법에 교란 요인이 있음 |
| D2 / CW-03 / G2 | 같은 Bash/dash에서 수동 cd/export 뒤 실행하는 정상 사례가 있음. 반면 26개 중 4개 테스트에서 unsafe dispatch가 재현됨 | 명확한 지원·신뢰 경계 안에서 안전한 handoff와 dispatch를 만들 수 있는지 | 실제 설계 결함. 관측 event와 실행권 인수가 분리되어 있음 |
| D3 / CW-04 / G3 | C-D49의 turn 중단 요청·종료 관측·불명 결과 보존, 네 종류 전달, 단기 busy/approval 보류와 즉시 재연결을 실제 OMP RPC에서 관측. Node 15/15, Python 7/7 기록 | 실제 TUI composer 보존, session 전환, 연결·process 수명별 중복 처리 | 기존 API 경계 실패 중 일부는 C-D49로 요구가 대체됨. 현재 요구의 남은 runtime 근거를 특정해야 함 |
| D4 / 실행·증거 관리 | 승인된 r1.9 입력 hash는 진단 시작 시 모두 일치. 활성 배정 없음. 98/128 child-run 사용 기록 | 변경된 요구에 맞춘 최종 통합 후보와 후속 예산 | Snapshot 수집 범위가 바뀐 문제와 gate 책임·역사적 문구를 정리해야 함 |

주요 근거: [G1](gates/G1.md), [G2](gates/G2.md), [G3](gates/G3.md),
[ledger](../../../.workflow/core-workbench/runs/implement-p2-20260923/ledger.json),
[G2 재설계 검토](../../../.workflow/core-workbench/runs/implement-p2-20260923/cw03-safety-redesign-review.md).

## 기존 반복이 문제를 닫지 못한 이유

다음은 저장소 관측에 근거한 진단이며, 사용자 요구를 새로 확정하는 문장이 아니다.

1. **G2는 검사 항목을 늘려도 검사와 실행 사이의 틈이 남았다.** 현재 backend는
   shell 함수가 FD 9로 보낸 READY/job/handoff event를 읽고 PTY에 명령을 쓴다.
   같은 shell의 hook은 event를 보낸 뒤에도 실행될 수 있다. 함수 문자열 검사나
   한 시점의 process 목록은 그 뒤 hook이 입력을 읽거나 job을 만드는 것을 막지 못한다.
   또한 시험에는 내부 helper·builtin 변조와 event 위조가 포함돼 있어 지원 경계와
   보안 경계를 명시하지 않으면 해결 목표가 계속 넓어질 수 있다.
2. **G1은 필요한 동작과 성공 predicate가 일치하지 않은 단계가 있었다.** Setup wizard가
   prompt 입력을 막았고, 도구 command preview와 실제 출력의 구분이 필요했다.
   이를 고친 뒤에도 SIGSTOP→resize→SIGCONT의 redraw를 정상 resize 근거로 사용했다.
   Resize 없는 STOP→CONT 대조에서도 redraw가 나와 해당 해석을 철회했다.
3. **미확인 목록과 해당 gate의 완료 조건을 다시 연결해야 한다.** G1 문서의
   detach/reconnect는 후속 G4/CW-15의 산출물과 관련된다. G3 backend 대조·사용자 표시·
   worker 주기 점검 보류는 현재 PLAN에서 CW-11/12의 책임이다. 이를 초기 gate의
   선행 완료 조건으로 다시 요구하면 후속 구현을 기다리는 순환이 생긴다.
   요구는 유지하고 feasibility와 production 통합의 관측 책임을 분명히 해야 한다.
4. **과거 관측과 현재 상태가 한 문서 안에서 혼동된다.** G2 문서는 앞부분의 현재
   22/26 결과 외에 초기 7개 통과와 job 확인이 wrapper에만 있다는 문구도 남아 있다.
   이전 시점의 근거는 보존하되 현재 동작의 설명으로 읽히지 않게 해야 한다.

## 결정과 미결정

### C-D50 — 사용자 합의: 관리된 shell의 신뢰 범위

사용자는 cwd·export·검증된 conda/venv 전환을 지원하고, 비호환 hook·trap 변경은
자동 복귀를 보류하는 범위를 선택했다. 같은 사용자 권한의 코드가 Workbench 내부
함수를 고의로 변조하거나 event를 위조하는 공격까지 막는 보안 경계는 요구하지 않는다.
일반적인 입력·job 경합과 상태 불명 시 자동 입력 보류는 계속 필수다.

이 결정은 현재 실패를 통과로 바꾸지 않는다. 기존 실패 각각이 일반 경합을 보여주는지,
지원 밖의 의도적 내부 변조를 보여주는지 근거와 함께 분류해야 한다. 비호환 변경을
실제로 감지·보류하는 동작과 정상 handoff의 성공을 별도로 입증해야 한다.

### C-D51 — 사용자 합의: handoff 이후 자동 제어 대기

사용자는 `wb-handoff`가 prompt로 돌아오지 않고 같은 interpreter에서 자동 제어
대기로 들어가는 방식을 선택했다. 자동 요청은 명령 문자열을 PTY에 타이핑하는 대신 전용
control 경로에서 수락한다. Prompt hook이 뒤늦게 실행되는 틈을 없애려는 설계 가설이며,
현재 Bash/dash에서 가능하다는 검증 결과는 없다.

사용자 제어권 인수는 즉시 새 자동 입력을 막아야 한다. 실행 중 foreground 실험은
그대로 두며, 수동 prompt 복귀는 해당 shell이 prompt를 처리할 수 있을 때 일어난다.
사용자는 이 동작 전환을 선택했다. 지금의 READY 검사 보강만으로 안전해졌다고 판단하지 않는다.
방향·대안·검증 한계는 [ADR-0002](../../adr/0002-managed-shell-control-wait.md)에 기록한다.

### C-D52 — 사용자 합의: terminal 인수 요청과 확인

인수 요청 즉시 새 자동 전송을 막는다. 이미 전달된 명령은 실행 중·종료·미확인으로
표시하며, 인수 요청이나 확인만으로 취소·종료됐다고 단정하지 않는다. 실행 중 실험은
유지하고 수동 입력은 확인된 현재 대상에 전달한다. 대상이 불명확하면 입력을 보류하며
이유를 표시한다. 인수·상태 확인 뒤 오래된 요청을 자동 재실행하지 않는다.

수동 입력 대상이 실행 중 foreground program으로 확인되면 실험 종료나 shell prompt
복귀를 기다리지 않고 그 대상의 수동 제어를 제공할 수 있어야 한다. 그와 별개로 shell이
자동 제어 대기를 벗어나 수동 prompt로 돌아오는 시점은 실제 실행 상태에 따른다.
UI의 위치·키 배치는 이번 결정으로 고정하지 않는다.

## 해결 방향과 판별 실험 설계 — 아직 실행하지 않음

### D2: 실행권을 잡는 handoff를 먼저 검증

**가설:** 관리된 shell에서 자동 명령 대기 지점을 명시하면, READY를 관측한 뒤
나중에 PTY에 명령을 쓰는 현재 경합을 제거할 수 있다.

먼저 현재 함수·PTY 방식, 같은 interpreter의 control 대기 방식, 필요한 경우 native
통합의 차이를 설계한다. 전용 FD 자체를 인증 또는 보안 격리로 설명하지 않는다.
Native 통합·shell 수정은 현재 채택한 결정이 아니다. 별도 shell로 실행하는 대안은
same-shell 요구를 바꾸므로 이 문서에서 승인하지 않는다.

우선 설계할 처리 순서는 다음과 같다. 이는 사용자 합의를 구현할 후보이며 검증된
구현 명세가 아니다.

1. 사용자가 `wb-handoff`를 직접 실행하면 같은 interpreter 안의 수신기가 shell identity,
   미소비 입력·job·cwd·지원 설정을 확인한다. 실패하면 이유를 알리고 수동 상태에 남는다.
2. 확인에 성공하면 수신기가 control 경로에서 기다린다. 일반 prompt로 돌아와 READY를
   다시 읽는 경로를 자동 실행 허가로 사용하지 않는다.
3. Backend는 최신 task 승인과 입력 owner를 확인해 요청 하나를 보낸다. 수신기는 현재
   generation·요청 identity를 대조하고 수락·시작을 따로 보고한다. 구현은 시작이 확정되는
   지점을 정의해야 하며, 전송 성공을 명령 시작으로 표시하지 않는다.
4. 명령은 동일 interpreter에서 실행해 cwd/export를 유지한다. 실행할 command가
   control 입력을 stdin으로 물려받지 않도록 경로를 분리하는 방법을 검증한다.
   외부 자식의 stdin은 필요한 PTY 동작을 유지한다. 자동 요청을 미리 여러 개 쌓지 않는다.
5. 종료와 남은 job을 확인한 뒤 자동 대기로 돌아가거나, 인수 요청·불명 상태에 따라
   수동/확인 필요 상태로 전환한다. Control loop 이탈·Ctrl-C·exec·shell 종료는 정상
   완료로 추정하지 않는다.

입력 owner, shell의 대기/실행 상태, 작업 성공·실패를 별개로 표시해야 한다.
Environment activation이 prompt 표시만 바꾼 경우와 실행 hook·trap을 바꾼 경우를
구분해 지원 기준을 정한다. 임의 `activate.d` 코드까지 검증됐다고 확대하지 않으며,
환경 활성화를 이유로 사용자의 cwd/export를 초기화하지 않는다.

이후 최소 실험의 성공 조건은 다음과 같다.

- 정상 Bash와 dash에서 cd/export 및 선택된 환경 전환 후 동일 shell PID/cwd/환경으로
  한 명령이 실제 시작·종료한다. 모든 경로를 차단하는 후보는 실패다.
- Handoff 처리와 자동 수락 사이에 미제출 입력, foreground 작업, background/suspended
  job, stale generation·요청이 있으면 자동 명령을 실행하지 않는다.
- 지원하는 설정에서 일반 hook·입력 race를 재현하고, 지원하지 않는 hook·trap 변경은
  자동 제어에 들어가기 전에 명확히 보류한다. 감지 가능성을 가정하지 말고 시험한다.
- 인수 요청 뒤 아직 보내지 않은 자동 요청은 PTY와 control 경로 모두에서 전송을 막는다.
  요청 전에 이미 전달한 명령의 수락·시작 여부가 불명확하면 그대로 표시하고 취소나 종료로
  추정하지 않는다. 기존 실험을 제어권 인수만으로 종료하지 않고 오래된 요청을 replay하지 않는다.
- Hook이 입력을 기다리는 동안 자동 command가 그 입력으로 소비되지 않아야 한다.
  Command lifecycle 불명·control 손실·signal 후에는 자동 replay하지 않는다.

중단 기준: 이 조건을 지킬 실행권 소유 지점을 설명하거나 재현할 수 없으면 제품
수정 범위를 늘리지 않는다. 실패한 가정과 필요한 사용자 동작 변경을 제시한다.

### D1: 화면 복원과 입력 보존을 직접 판정

G1의 목적은 새 terminal 크기에서 화면과 조작이 정상인지 확인하는 것이다.
Resize가 redraw의 유일한 원인인지 증명하는 것은 그 자체로 사용자 수락 조건이 아니다.

신호로 OMP를 멈추지 않고 100×32→84×24→100×32로 크기를 바꾸는 실험을 설계한다.
Composer 입력과 승인 modal·도구 출력 각각에서 폭에 따른 줄바꿈, 잘림·겹침,
입력 내용과 승인 상태 보존을 확인한다. Anchor 문자열 하나의 존재를 성공 기준으로
쓰지 않는다. Resize 없는 대조와 의도적으로 resize 전달을 누락한 부정 대조를 사용해
검사가 실제 화면 오류를 잡는지도 확인한다. 지속 redraw가 있더라도 안정된 화면 영역과
관측 구간을 기준으로 판정하며, 전체 출력이 조용해지는 것을 무조건 요구하지 않는다.

단일 OMP의 VT 시험 뒤에는 원본 OMP 두 개와 host terminal의 실제 3-pane 조합에서
outer resize와 이후 키 입력 대상도 확인해야 한다. 단일 child의 성공만으로 wrapper의
조합·입력 routing까지 통과했다고 판단하지 않는다.

Query는 올바른 reply 생성·PTY 전달 fixture와 실제 OMP 화면/조작 결과를 구분한다.
OMP 내부 해석 자체를 필수 근거로 삼을지는 그것이 보장하려는 사용자 동작과 공개
관측 지점을 먼저 특정한다. 해당 근거 없이 조용히 통과 항목으로 바꾸지 않는다.

일반 terminal·외부 tmux·herdr의 원본 TUI 지원 약속은 유지한다. 초기 feasibility와
최종 compatibility에서 동일 사례를 재사용하도록 역할을 정리하고, 실제 실행하지 않은
조합은 미확인으로 둔다. Curses의 truecolor 근사도 원본 색상 동작과 대조할 항목이다.

### D3: 실제 TUI 경합과 session 수명을 확인

OMP 18.2.10의 공개 API에 `isIdle`, `hasPendingMessages`, `abort`와 session lifecycle
event가 있다. `sendUserMessage`는 prompt 흐름으로 전달된다.
[고정 버전 공식 문서](https://raw.githubusercontent.com/can1357/oh-my-pi/v18.2.10/docs/extensions.md).
API 존재와 현재 RPC 성공만으로 TUI의 사용자 입력 보존을 입증할 수는 없다.

이번 진단의 추가 정적 관측은 다음과 같다.

- [RPC 구현](https://raw.githubusercontent.com/can1357/oh-my-pi/v18.2.10/packages/coding-agent/src/modes/rpc/rpc-mode.ts)의
  `getEditorText()`는 항상 빈 문자열을 반환한다. RPC에서 `editorEmpty=true`였다는
  기록은 실제 사용자의 TUI 초안이 비어 있었거나 보존됐다는 근거가 아니다.
- [Interactive 구현](https://raw.githubusercontent.com/can1357/oh-my-pi/v18.2.10/packages/coding-agent/src/modes/controllers/extension-ui-controller.ts)의
  `sendExtensionUserMessage`는 내부 비동기 전송에 error handler를 붙이고 반환한다.
  현재 bridge의 `await pi.sendUserMessage(...)`와 `api_accepted` ACK만으로 실제 enqueue나
  provider 처리를 입증할 수 없다. 호출 복귀·OMP 처리 관측·업무 완료의 의미를 다시
  구분해야 한다. 실제 TUI에서 전달 실패가 재현됐다는 주장은 아니다.

남은 실험은 실제 두 OMP TUI에서 non-empty composer를 유지한 채 자동 전달을
보류하는지, session 전환 전의 generation·연결·대기 요청이 새 session으로 들어가지
않는지에 집중한다. Composer 문자·focus·provider 요청·target session identity를 함께
관측한다. Busy/approval은 상태 진입·유지·해제 시나리오로 정의한다.

중복 방지는 연결만 끊김, session 전환, process 재시작을 구분한다. 무기한 기다리는
"장기 시험" 대신 각 수명 경계의 상태 보존·거절·불명 처리로 기준을 정한다.
Process 재시작의 durable 기록·대조는 CW-09/15와 연결한다. C-D49의 backend 표시와
60초 worker 점검 보류는 CW-11/12 및 최종 통합에서 검증하며 초기 G3와 중복 소유하지 않는다.
Native approval을 보존할 수 없는 이전 mutable tool wrapper는 재도입하지 않는다.

이후 검증은 같은 mode의 성공·실패 사례를 짝지어 수행한다. TUI 전송 API가 반환한
뒤 비동기 오류가 생긴 경우에도 전달 완료로 굳히거나 자동 재전송하지 않아야 한다.
공개 event와 message identity로 확인할 수 없는 단계는 불명으로 남긴다.

### D4: 현재 입력과 검증 책임을 고정

Snapshot 항목 수 차이는 이번 읽기 전용 대조에서 원인을 찾았다. 이전 manifest 117개와
새 tracked-only 수집 결과 94개를 비교하면, 새 방식은 기존 24개(로컬 `.serena` 설정 1개와
이전 승인·계획 기록 23개)를 제외하고 G1 테스트 1개를 추가했다. 기존 117개 중 사라진
파일은 없다. 기존 manifest 기준 변경된 파일은 G1 gate 문서와 live probe이며 새 테스트가
추가된 상태다. 전체 수가 같아야 한다는 가정을 사용하지 말고 입력 범위를 정의해야 한다.

해결안에서는 현재 소스·테스트·실제 필요한 설정과 승인 근거, 역사적 evidence, 운영
로그를 구분한다. 후속 writer를 시작하기 전에 staged·unstaged·untracked의 실제 변경을
보존하고 manifest를 갱신한다. 이번 진단에서는 snapshot·staging을 바꾸지 않았다.

각 gate 항목은 요구 ID, 소유 ticket, 필요한 선행 산출물, 관측값, 성공·실패·미확인
조건을 가져야 한다. 기존 partial 통과와 무효화된 predicate를 혼합하지 않는다.
G1/G2/G3의 통과를 선언하거나 CW-05 의존성을 임의로 없애는 해결안은 아니다.

98/128 child-run 기록상 30회가 남아 있다. 기존 문서의 후속 최소 35회는 기존 단위별
배정 추정치다. 새 해결 단위와 독립 검증을 정한 뒤 남은 전체 작업을 다시 추정한다.
현재 사용자 한도를 임의로 늘리지 않는다. 숫자 확대만으로 기술적 실패를 해결했다고
보지 않는다.

## 저장소 근거의 식별

| 입력 | 이번 진단에서 읽은 SHA-256 |
|---|---|
| G2 prototype | `64267d025dd89ea78441b07fa1ca496251d9cb934d192513efc5dd0ef1d132d0` |
| G2 tests | `2a8c42637f0927c633b90908bcefaa33d0b90bb850ca5664e457b87229ac28b5` |
| G3 bridge | `808f2044d32885ef5df95da3b1f5bca2db601ca462acaa5eac078cf35fed86f6` |
| G1 live probe | `c6143d78f937901b60c23541ebf8502fd92be2b1fa6ade2b0d8064b0b1f14824` |
| G1 predicate tests | `a06381fef38456e5070f0ff936ad5eac75cba313e54d73a8c2d3b19c7c07bf15` |

근거: [G1 counterexample](../../../.workflow/core-workbench/runs/implement-p2-20260923/cw02-no-resize-control.json),
[G1 final partial review](../../../.workflow/core-workbench/runs/implement-p2-20260923/cw02-resize-evidence-delta-review.json),
[G2 현재 코드](../../../src/workbench/terminal/shell_g2/prototype.py),
[G2 독립 테스트](../../../tests/gates/g2_shell/test_g2_shell.py),
[G3 현재 bridge](../../../omp_bridge/g3/bridge.ts),
[기존 PLAN](PLAN.json), [SPEC](SPEC.md).

## 해결을 재개할 순서와 중단 기준

다음은 후속 계획에 넘길 순서다. 새 ticket 번호나 실제 배정을 확정하지 않는다.

| 단계 | 목적 | 다음 단계로 넘어갈 조건 / 중단 조건 |
|---|---|---|
| 현재 입력·요구 대조 | 기존 staged/unstaged/untracked 변경과 유효한 근거를 보존하고 C-D50~C-D52를 필요한 SPEC·PLAN·ticket에 반영 | 입력 범위·검증 책임·예산을 확인하기 전 새 writer를 시작하지 않는다. 전체 수가 과거와 같아야 한다는 snapshot 가정은 사용하지 않는다. |
| G2 최소 판별 실험 | 같은 interpreter의 control 대기가 정상 실행과 인수·경합을 함께 다룰 수 있는지 확인 | 정상 성공과 부정 사례가 모두 관측돼야 한다. 실행권 소유를 설명하거나 관측할 수 없으면 해당 접근의 제품 수정을 확대하지 않는다. |
| G1/G3의 남은 실험 | 화면·입력 보존과 실제 TUI 전달을 mode·session 수명에 맞게 확인 | 각 실험은 성공·실패·미확인을 구분한다. 실험 자체가 잘못되면 판별법을 고치며 제품 실패로 바꾸어 기록하지 않는다. |
| 후보별 독립 검증 | 변경된 지원 경계에 맞는 독립 테스트와 안정된 후보 review | 알려진 필수 실패가 남아 있는 후보를 최종 review에 반복 제출하지 않는다. 기존 유효한 review는 보존하고 영향받은 범위를 다시 확인한다. |
| G4와 후속 통합 | 검증된 G1~G3를 묶고 backend·task·정책·detach의 실제 동작을 구현 | 초기 gate가 후속 미구현 산출물을 요구하지 않도록 책임을 정리하되 전체 수락 조건은 유지한다. |

새 반례가 나오면 먼저 지원 범위·제품 코드·실험·실행 환경 중 무엇의 문제인지
분류한다. 같은 가정이 반박된 상태에서 검사 하나를 더 붙이는 수정을 반복하지 않는다.
추가 수정은 새 가설, 그것을 구별할 관측, 성공·중단 기준이 있을 때 계획한다.
독립 G1/G3 조사의 실제 병렬 실행 여부는 host terminal·OMP resource 조건을 확인해 정한다.

## 남은 기술 검증과 인계

정책 질문 C-D50~C-D52는 사용자가 답했다. 아래 사항은 성공으로 간주하지 않고
기술 검증으로 남긴다. 기존 BRIEF의 gate 구조와 C-D51의 검증 조건을 유지한다.

- Bash/dash control 대기의 실제 구현, signal·job control·환경 전환의 지원 조합.
  현재 shell/API에서 성립하지 않으면 실패한 가정과 필요한 변경을 제시한다.
- 정상 resize·3-pane·외부 terminal의 실제 관측과 지원 버전. 미시험 조합은 pending이다.
- 실제 TUI에서 메시지 전달과 사용자 입력 경합, API 오류·session 수명별 상태 처리.
- 후속 backend 대조·worker 점검·detach 통합. 현재 feasibility 근거로 완료 처리하지 않는다.

Core [BRIEF r1.10](BRIEF.md)에 개별 합의와 문제별 해결 방향을 반영했다.
현재 [SPEC s2.3](SPEC.md)·[PLAN p2.3](PLAN.json)·기존 16 ticket의 개정과
[승인 기록](../../../.workflow/core-workbench/runs/implement-p2.3-20260924/approval.json)을 연결했다.
CW-01 완료, 유효한 partial 근거와 ticket ID를 보존하고 판별 실험부터 구현을 재개한다.
