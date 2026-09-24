# Core Workbench — 구현 명세

Revision: s2.2; 상태: 사용자 C-D49 정책 반영, G1~G4 검증 대기.
입력: [BRIEF.md](BRIEF.md) r1.9, SHA-256 `f2ff96a06ad624ac36415d11cd699bc501b402e656a482768e89459b132df069`. r1.7 원본 승인과 G1/G2 보충 승인은 [workflow 기록](../../../.workflow/core-workbench/runs/implement-p2-20260923/g1-g2-policy-approval.json)에 연결한다. p2 구현 요청은 [run 승인 기록](../../../.workflow/core-workbench/runs/implement-p2-20260923/approval.json)을 따른다.
이전 s1/p1 후보의 digest와 원문은 [p1 기록](../../../.workflow/core-workbench/runs/planning-p1/manifest.json)에 보존했다. s2는 Oracle 검토에서 확인된 게이트·행동 추적·식별 계약을 보강한다.

## 문제와 해결

사용자는 manager OMP와 목표·코드를 논의하고 worker OMP에 실험을 맡긴 뒤, 같은 host의 실제 persistent terminal에서 일어나는 일을 한 화면에서 확인한다. Manager는 승인된 범위에서 준비·수정·재시도를 판단하고 worker의 검증 근거를 완료 조건과 대조한다. 화면을 detach해도 backend, 두 OMP와 terminal은 같은 host에서 계속된다. 자동화가 활성일 때만 주기 모델 점검을 계속한다.

Python backend가 PTY 세 개, 작업·승인 상태, lifecycle, mailbox, 기록을 소유한다. 분리된 Python frontend는 원본 OMP TUI 둘과 host terminal을 표시하고 키를 해당 PTY에 전달한다. 모델 호출은 두 OMP가 맡으며, 작은 공개 OMP extension이 session 상태 확인과 메시지 전달을 연결한다. 이 구성은 BRIEF의 방향을 구체화한 **설계안**이다. G1~G4가 실제 조합의 가능성을 입증하기 전에는 라이브러리·protocol·지원 버전을 완료된 선택으로 간주하지 않는다.

## 관련 사용자 흐름

1. 사용자가 manager OMP에 목표를 설명하고 위임 범위·완료 조건을 승인하며 진행을 지시한다. Manager는 승인된 revision을 worker에게 보낸다.
2. Worker는 기준 commit과 별도 실행용 worktree, 선택된 실험 환경·경로를 확인하고 같은 host shell에 실행을 요청한다. 사용자는 terminal의 실행과 출력을 본다.
3. Worker는 자동화 활성 중 활성 run을 60초마다 모델로 검토하고 의심 상황을 읽기 전용 근거로 조사한다. 코드 수정 필요성을 근거와 함께 manager에게 보고하면 해당 지시는 끝나며, 다시 맡기려면 manager의 새 지시가 필요하다. Manager는 승인 범위·재시도 한도·사용자 제어권을 확인한 뒤 수정·중단·재실행 또는 보고를 결정한다.
4. 사용자는 언제든 terminal 제어권을 인수·반환하고 화면을 detach·재접속한다. 명시적 전체 종료, agent/backend 재시작, 재부팅은 서로 다른 복구 경계를 따른다.
5. 사용자가 worker OMP에 직접 요청하면 기존 명세 또는 범위 안의 작은 새 명세에 연결한다. 범위가 넓어지는 요청은 manager를 통해 확인한 뒤 실행한다.

## 범위와 비목표

포함 범위는 [BRIEF의 C-AC-01~34](BRIEF.md#공개-동작과-수락-예시)와 [제품 동작 규약](OPERATING-CONTRACT.md)이다. Phase 2 외부 ingress·MCP·웹 서버, 다수 worker, Windows, macOS 초기 출시, zsh adapter, inline image 필수 호환, 전용 worktree 자동 관리·삭제, 실행 중 다음 실험 병렬 개발 보장, 외부 editor 쓰기 차단은 포함하지 않는다. Workbench 내부에서 tmux 등 외부 multiplexer를 실행·의존하지 않는 결정은 [ADR-0001](../../adr/0001-no-external-multiplexer.md)을 따른다.

## 공개 계약과 불변식

구체 class명·파일 경로는 게이트 후 고정한다. 아래는 모든 구현이 지켜야 할 행동 계약이다.

| 경계 | 전달·관측 계약 |
|---|---|
| Frontend ↔ backend | 연결과 실제 실행 수명은 독립이다. 화면은 세 PTY의 화면 상태, focus, host 입력 owner, 자동화·모델 점검 상태, 마지막 확인 시각을 구분한다. 재접속은 기존 backend/session 식별자를 확인하고 새 run을 시작하지 않는다. |
| PTY display ↔ control | PTY 원문 byte stream과 키·paste·resize·query reply는 terminal 경로로 처리한다. Paste는 2 MiB 한도 또는 현재 queue 공간을 넘으면 frame 전체를 거절하고 화면에 이유를 표시한다. 거절된 byte를 child에 일부 전달하지 않으며 hotkey 처리와 query reply도 잃지 않는다. Command 시작·종료, shell/cwd 상태, 승인·메시지 전달은 별도 control 경로로 확인한다. ANSI 출력·prompt·무출력은 lifecycle 증거가 아니다. |
| Shell control | Workbench 전용 persistent interactive Bash를 우선 시작하고 없으면 sh를 시작한다. 시작 후 같은 shell을 유지한다. 수동 입력 뒤 사용자가 같은 shell에서 `wb-handoff`를 직접 실행해야 자동 입력 재개를 검토한다. `ready`, `started`, `finished`, `exited`, `unknown` 관측과 cwd/입력 경계를 구분한다. 하나의 foreground 실행만 수락하며 Busy·미제출 입력·REPL·suspended job·owner/cwd 불일치·hook 유실·시작 여부 불명은 자동 입력을 보류한다. 새 shell 우회·숨은 queue·자동 replay는 없다. |
| 위임·작업 | Task ID는 목표·재시도 이력을 묶고 revision은 불변 명세, run ID는 한 번의 실행 시도, command ID는 shell 수락·시작·종료, `message_id`는 논리 메시지 하나의 안정된 ID, session generation은 대상 연결을 식별한다. 전달 시도는 별도 기록으로 구분하고 필요할 때만 시도 ID를 둔다. 승인 범위와 진행 지시가 함께 있어야 새 자동 실행을 시작한다. 기존 run의 revision을 덮어쓰지 않는다. |
| Mailbox | `task`·`question`·`answer`·`report`를 원래 task/revision/run 및 reply 관계에 연결한다. 동일 `message_id`의 중복 처리와 불명확한 전달의 자동 replay를 막는다. 로컬 접수, OMP API 수락, 모델의 실제 처리, 업무 완료는 별개다. 수신 session/idle/pending/approval·입력 상태를 주입 직전에 재확인하고 사용자 대화를 침범하지 않는다. 직접 worker 요청도 명세·승인 범위에 연결한다. |
| 완료와 복구 | Exit code 0은 명령 결과일 뿐 목표 완료가 아니다. Manager가 승인된 완료 조건과 worker 근거를 대조한다. 실패 복구 자동 재시도는 최초 실행을 제외하고 Task당 기본 3회다. Commit·revision·명칭 변경으로 초기화하지 않으며 정상 개선 실험과 구분한다. |
| 저장 | 명세·승인·취소·run·결과의 durable metadata가 새 자동 실행의 선행 조건이다. Metadata 쓰기 실패 시 기존 실행·가능한 관측은 유지하되 새 자동 실행을 보류한다. Raw log 저장 실패/상한은 실행을 중단시키지 않는다. |

TaskSpec에는 목표·완료 조건·허용 변경/실행/자원·중단 조건·선택한 실험 환경·기준 commit·expected cwd·결과 위치·승인 참조와 revision을 둔다. 비밀 환경 전체를 저장하지 않는다. Run은 시작 당시 TaskSpec revision, commit, 실제 실행 cwd/환경 식별, command ID, 관측 결과를 참조한다. 실험 환경은 Workbench 자체 Python 환경과 분리하며 사용자가 고른 conda/venv를 재사용한다. Commit은 dirty/untracked 입력·외부 데이터까지 고정하지 않으므로 필요한 항목의 확인 결과를 기록한다.

세션 공통 설정은 각 Task의 이후 판단에 적용한다. 설정 변경만으로 현재 run을 종료하지 않으며 이미 사용한 자동 복구 재시도 횟수는 유지한다. 변경 전후의 판단에 적용된 설정을 확인할 수 있어야 하지만 이를 현재 run의 TaskSpec revision 덮어쓰기로 표현하지 않는다.

Control protocol은 Python 내부 API와 OMP extension의 로컬 연결을 나누며 version, request/`message_id`, role, task/revision/run, 대상 session generation을 검증한다. UDS + JSON framing은 우선 후보이고 G3 결과로 확정한다. 연결 role을 payload 문자열만으로 승격하지 않는다. OMP의 공개 extension API만 사용한다. v18.2.10 [공식 extension 문서](https://github.com/can1357/oh-my-pi/blob/v18.2.10/docs/extensions.md)는 `followUp`, `isIdle`/`hasPendingMessages`, managed timer를 설명하지만 실제 독립 두 프로세스·composer/approval 경합·비침범 전달과 `ctx.abort()` 기반 진행 중 manager turn 중단 요청·확인·불명 결과 표시를 G3에서 증명해야 한다.

## 상태 전이와 장애 경계

- **자동화:** 승인 전 → 승인·진행 후 활성 → 일시정지 요청 및 manager OMP 진행 중 turn 중단 요청 → 실제 중단 확인 또는 결과 불명 표시 → 명시적 재개 후 현재 상태·승인 범위 대조. 요청과 확인을 구분하며 rollback으로 표시하지 않는다. 일시정지 중 manager·worker의 새 자동 모델 작업과 1분 worker 모델 점검을 시작하지 않는다. 로그·프로세스 상태 수집과 기존 host 실험은 계속하고 실험 중단은 별도 조작이다. 사용자의 직접 조사·수정·rollback·명시적 agent 수동 지시는 자동 재개가 아니다. 재개 시 실제 파일·도구 결과·terminal 프로세스·작업 상태와 승인 범위를 대조하고 중단 명령·대기 요청을 자동 replay하지 않는다. 취소는 기록·결과 파일을 자동 삭제하지 않는다. 세션 설정 변경은 다음 판단부터 적용하고 현재 run·사용한 재시도 횟수를 유지한다.
- **실행:** 요청 접수 → shell 입력 경계 수락 → 실제 시작 → 실제 종료 확인 → worker 결과·근거 → manager 완료 판단. 어느 단계든 결과가 불명확하면 `NEEDS_REVIEW`로 남기고 자동 재전송하지 않는다. 일반 중단 요청은 실제 종료가 아니며, 강제 종료는 승인 범위와 대상 확인이 모두 있을 때만 manager가 결정한다. 종료 전 코드·설정 수정이나 재실행을 하지 않는다.
- **점검:** 자동화 활성 중인 run마다 60초 tick을 요청한다. Worker가 바쁘면 최신 상태 한 건만 pending으로 유지하고 지연과 마지막 실제 모델 점검 시각을 표시한다. 일시정지 시 새 tick과 pending 모델 점검은 시작하지 않는다. 종료 이벤트는 즉시 기록·표시하고 설명을 위한 새 자동 모델 작업은 재개 후 현재 상태를 대조해야 시작한다. 무출력 자체는 hang 판정이나 실행 시간 상한이 아니다.
- **수명:** 정상 detach는 frontend 연결만 끊는다. 명시적 전체 종료는 활성 작업을 보여주고 확인을 받은 뒤 중단·종료를 검증한다. Backend/agent 재시작은 기록과 실제 프로세스를 대조해 확인 가능한 관측만 복구한다. 재부팅 후 새 실험은 사용자 확인이 필요하다. 이전 session의 불명확한 명령·메시지는 자동 replay하지 않는다. Backend crash에서 자식 프로세스 생존을 보장하지 않는다.
- **모델/저장 오류:** OMP 모델 오류·인증 만료 시 실험과 일반 관측을 유지하고 자동 판단·새 명령을 멈추며 마지막 확인 시각을 표시한다. 복구 후 현재 상태를 다시 확인한다. Metadata 장애와 raw log 장애는 별도 표시한다.
- **원문 로그:** run당 64 MiB, 프로젝트당 512 MiB의 실제 저장 byte 상한 중 먼저 도달한 곳에서 추가 저장을 멈추고 잘림을 표시한다. 화면·관측·실험 산출 파일은 상한과 분리한다. 작업·명세·요약·실행용 worktree는 자동 삭제하지 않는다.

## 모듈 경계와 기술 상세화

- **Backend/session supervisor:** 세 PTY와 자식 process identity, timer, frontend attach, control routing을 소유한다. UI는 이 상태의 client다. Linux POSIX 처리와 앞으로의 OS adapter를 나눈다.
- **Terminal adapter:** PTY 생성, controlling TTY/job control, shell별 lifecycle·입력 경계, terminal emulation과 화면 snapshot을 분리한다. Textual/libvterm/CFFI·`openpty` + child bootstrap은 G1/G2 후보이며 확정 버전과 라이선스·설치 방법은 게이트 결과에 남긴다.
- **OMP adapter:** 두 원본 OMP 실행과 공개 TS extension, session binding, 비침범 mailbox 전달을 맡는다. Python에 세 번째 판단 agent·provider credential 저장소를 만들지 않는다.
- **Task/policy/store:** 승인·revision/run 연결, 재시도·일시정지·취소·완료 조건, metadata 기록을 한 경계에서 검사한다. CW-05는 metadata 쓰기의 성공/실패가 새 자동 실행 수락에 전달되는 최소 저장 port 계약을 검증한다. CW-09는 실제 schema와 Task/revision/run에 연결된 논리 메시지·전달 상태 기록을 소유하고 CW-08이 이를 사용해 OMP 전달을 구현한다. SQLite는 우선 후보이며 장애 시 durable write 실패를 caller에게 전달해야 한다.
- **Observation/log:** lifecycle과 PTY raw log를 섞지 않고, 주기 점검·hang 조사·usage 표시·bounded 저장을 담당한다. OMP 사용자 대화/승인 경합은 adapter 최종 확인을 거친다.

M0의 CW-02~05는 BRIEF G1~G4의 통과/실패/미확인을 실제 조합과 재현 가능한 증거로 기록한다. G3에는 공개 OMP API로 manager의 진행 중 turn 중단을 요청하고 실제 중단 확인·미확인 도구 결과를 구분하는 경계를 포함한다. 도구 A 완료 대기나 준비된 B의 별도 차단은 통과 조건이 아니다. CW-05에서 이 증거와 최소 저장 port를 확인한 뒤 계약·버전·명령을 고정한다. 게이트 실패가 사용자 행동을 바꾸는 대안을 요구하면 후속 구현을 멈추고 BRIEF 재합의를 요청한다. G1~G4가 끝나기 전에 관련 production 동작을 완료 처리하지 않는다.

## 수락 기준과 검증 추적

소유 ticket은 해당 동작을 구현·검증한다. CW-16은 모든 통합 후보를 다시 확인한다. `I-FLOW`는 실제 두 OMP의 목표→명세→실행→보고와 worker 직접 요청의 명세·범위 연결, `I-SHELL`은 같은 shell·제어권·환경, `I-POLICY`는 승인·revision·세션 설정·점검·복구, `I-FAULT`는 장애·수명·저장, `I-COMPAT`은 일반 terminal/tmux/herdr와 Bash/sh 조합의 사용자 조작을 뜻한다. 각 check의 실제 명령·환경·결과는 구현 run에 기록한다. 현재 모두 **증거 대기**다.

| ID | 소유 ticket | 행동 수준 확인 경계 | 최종 check |
|---|---|---|---|
| C-AC-01 | CW-06 | 원본 manager OMP pane에서 목표·코드 대화 | I-FLOW |
| C-AC-02 | CW-08 | Task 연계 task/question/answer/report 왕복과 동일 메시지 중복 방지 | I-FLOW |
| C-AC-03 | CW-10 | 실제 host shell 실행·출력과 worker 실행 의뢰 | I-FLOW, I-SHELL |
| C-AC-04 | CW-13 | worker 근거를 완료 조건과 대조한 manager 보고 | I-FLOW |
| C-AC-05 | CW-10 | manager 준비·worker 실험/terminal 실행·관측·보고, 수정 필요 보고 시 지시 종료·새 지시 없이는 자동 반환 금지 | I-FLOW |
| C-AC-06 | CW-06 | 세 영역, focus와 입력 owner 별도 표시 | I-COMPAT |
| C-AC-07 | CW-13 | 실패 복구 3회·설정 변경 뒤 사용 횟수 유지·추가 자동 실행 금지·시간 상한 없음 | I-POLICY |
| C-AC-08 | CW-07 | 사용자 인수 시 자동 입력 중지·반환 후 재확인 | I-SHELL |
| C-AC-09 | CW-10 | commit 기준 실행용 worktree 가이드·준비 실패 처리 | I-FLOW |
| C-AC-10 | CW-13 | 실행 종료 확인 뒤 소스·설정 수정 | I-POLICY |
| C-AC-11 | CW-10 | 위임 변경만 local commit·push/merge 분리 | I-FLOW |
| C-AC-12 | CW-11 | 자동화 활성 중 60초 모델 점검·일시정지 중 모델 점검 중단·무출력 조사 | I-POLICY |
| C-AC-13 | CW-13 | manager의 근거·권한·재시도 판단 | I-POLICY |
| C-AC-14 | CW-06 | 두 원본 OMP TUI·명령 유지 | I-COMPAT |
| C-AC-15 | CW-15 | detach 중 두 OMP·실험 지속, 활성 때만 점검·일시정지 보존과 재접속 | I-FAULT |
| C-AC-16 | CW-15 | 재시작 뒤 기록/실제 프로세스 대조·불명 자동 replay 금지 | I-FAULT |
| C-AC-17 | CW-15 | 모델 오류 중 실행·관측 유지와 재확인 | I-FAULT |
| C-AC-18 | CW-15 | 사용자 입력 owner를 detach 뒤에도 보존 | I-FAULT |
| C-AC-19 | CW-16 | 일반 terminal/tmux/herdr 조작·재접속 matrix | I-COMPAT |
| C-AC-20 | CW-16 | Linux 텍스트·Bash 우선·sh fallback 실제 matrix | I-COMPAT, I-SHELL |
| C-AC-21 | CW-11 | 활성 중 121번째 점검·13번째 peer wake·usage 표시 | I-POLICY |
| C-AC-22 | CW-15 | 전체 종료 확인·중단/정리·미종료 표시 | I-FAULT |
| C-AC-23 | CW-15 | 재부팅 뒤 확인 전 새 실험 금지 | I-FAULT |
| C-AC-24 | CW-14 | 명세/요약/worktree 보존·64/512 MiB raw log cap | I-FAULT |
| C-AC-25 | CW-12 | 승인 범위·Task/revision/run·취소와 중단 요청/확인/불명 결과 연결 | I-POLICY |
| C-AC-26 | CW-07 | cwd/환경 유지·입력 경합 보류·lifecycle 증거 | I-SHELL |
| C-AC-27 | CW-11 | 활성 중 busy 점검 최신 한 건·지연, 일시정지 중 자동 점검 보류·즉시 종료 표시 | I-POLICY |
| C-AC-28 | CW-14 | metadata 장애 차단과 raw log 장애 관측 | I-FAULT |
| C-AC-29 | CW-09 | 범위 승인+진행 지시로 자동화 시작 | I-POLICY |
| C-AC-30 | CW-12 | 일시정지 중 새 자동 모델 작업 보류·로그/프로세스 수집과 기존 실험 유지·수동 조작 독립 | I-POLICY |
| C-AC-31 | CW-13 | 새 revision은 다음 run에 적용하고 현재 실행 적용 지시는 종료 확인 뒤 재실행하며 즉시 제한은 우선 | I-POLICY |
| C-AC-32 | CW-07 | 기존 conda/venv 재사용·app 환경 분리 | I-SHELL |
| C-AC-33 | CW-12 | manager turn 중단 요청/확인·불명 도구 결과 표시·명시적 재개 대조와 replay 금지 | I-POLICY |
| C-AC-34 | CW-13 | 허용된 확인 대상만 강제 종료·실제 종료 검증 | I-POLICY |

## 테스트 결정

현재 저장소에는 제품 코드·테스트·lockfile이 없다(HEAD는 README.md만 추적). CW-01은 이후 ticket이 공유할 실행/검증 명령과 versioned test seam을 만든다. Test seam은 가짜 성공 표시가 아닌 경계의 행동을 재현해야 한다.

- G1: 실제 고정 OMP의 composer·승인·slash command, 한글·다중 행 paste·색·scroll·resize·query reply를 세 pane에 통과시킨다. 2 MiB 초과·queue 공간 부족 paste의 전체 거절과 이유 표시, 그 뒤 hotkey·query reply의 지속 동작을 재현한다. 일반 terminal, 외부 tmux/herdr 결과와 미시험 환경을 따로 기록한다.
- G2: 실제 Bash 5.x와 사용 가능한 sh 구현(현재 `/usr/bin/dash`)에서 같은 shell의 cwd/export/선택된 conda·venv, 직접 실행한 `wb-handoff`, 미제출 입력·REPL·suspend·종료/signal, Bash 부재 시 sh 시작을 시험한다. `wb-handoff` 뒤에도 미처리 입력·job·상태 불명이 남으면 자동 입력을 보류한다. Mock lifecycle만으로 통과하지 않는다.
- G3: 고정 OMP 두 독립 프로세스의 공개 extension/API와 task 왕복·busy/approval/composer 경합·session 변경·중복/불명 결과를 시험한다. 동일 `message_id`의 중복 전송·재연결·ACK 유실을 구분한다. 실제 manager OMP turn 중 pause를 요청해 중단 요청과 확인을 구분하고, 중단 중 도구 작업의 확인된 변경과 불명 결과를 기록한다. 일시정지 중 bridge의 새 자동 manager/worker 전달을 보류하고, 실제 OMP를 구동하는 gate harness에서 파일·도구 결과·terminal 프로세스·작업 상태·승인 범위를 대조한 뒤에만 명시적 재개 신호를 보내며 중단 명령·대기 요청을 replay하지 않는지 시험한다. OMP API 수락을 모델 처리 완료로 간주하지 않는다. 실제 Workbench backend의 상태 대조·사용자 표시와 1분 worker 모델 점검 보류는 CW-11/12의 production 및 I-POLICY에서 검증한다.
- G4: frontend 분리 상태에서 세 PTY·두 OMP·자동화 활성 중 실제 60초 wake가 살아 있고 재접속이 중복 실행하지 않는지 시험한다. Backend/agent crash와 재부팅은 구분한다. Metadata 저장 port의 성공/실패가 새 자동 실행 수락에 전달되는 계약 시험을 포함한다.
- Production: clock 주입으로 활성 중 60초 tick·최신 한 건 병합·121번째 점검과 일시정지 중 모델 점검 중단을 확인하고, 임시 metadata/log store와 쓰기 실패 주입으로 승인·한도·복구 불변식을 확인한다. Worker의 수정 필요 보고 뒤 지시 종료와 manager 새 지시 전 자동 반환 금지, 직접 요청의 기존 범위·새 작은 명세·범위 확대 세 사례, 세션 설정 변경 뒤 현재 run/재시도 유지도 검증한다. 실제 shell/OMP/PTY 프로세스 시험과 별도 외부 terminal 수동 조작 matrix를 더한다.

CW-01/CW-05가 확인한 toolchain에서 `uv run pytest`, `uv run ruff check .`, `uv run mypy ...` 및 bridge typecheck/계약 시험의 정확한 명령을 기록한다. 현재는 설치·잠금·실행 결과가 없으므로 이 명령들이 통과했다고 선언하지 않는다. `test_designer`의 독립 테스트 평가/작성과 `reviewer`의 독립 검토는 각 coherent verification unit과 최종 통합 후보에서 수행한다.

## 호환성·통합·출시 제약

Linux 우선이며 Bash 5.x, 실제 sh 구현, 고정 OMP/terminal library 조합을 검증 결과와 함께 명시한다. 현지 관측은 Python 3.12.3, OMP 18.2.10, Bash 5.2.21, sh→dash이며 지원 선언이 아니다. 검증된 runtime 조합과 native dependency 출처·라이선스·설치 경로를 CW-05와 CW-16 산출물로 남긴다. 외부 tmux/herdr 설정을 변경하지 않는다. 배포·시스템 package 설치·원격 게시·production migration은 이 계획의 자동 권한에 포함되지 않는다.

## 미해결 게이트와 위임된 선택

필수 제품 정책의 추가 선택은 현재 BRIEF에서 해결되었다. 남은 것은 G1 원본 TUI fidelity, G2 Bash/sh 안전한 같은 shell 입력, G3 독립 OMP 공개 bridge, G4 자체 detach·복구의 **실증**이다. 정확한 Python minor, dependency lock, VT/PTY 구현, IPC frame, storage schema, 키 배치·최소 화면 크기, signal 대기 간격은 게이트 결과를 근거로 결정한다. Herdr 등 외부 terminal이 실제 실행되지 않으면 해당 호환 결과는 pending으로 남는다. 행동 축소가 필요한 실패는 기술 선택으로 덮지 않고 BRIEF 재합의로 되돌린다.

## Ticket graph

[PLAN.json](PLAN.json)이 dependency, resource, scope, owner의 유일한 scheduling 원본이다. CW-01 → {CW-02 G1, CW-03 G2, CW-04 G3} → CW-05 G4/계약 고정 → {CW-06 UI, CW-07 shell, CW-09 task}이며 CW-08 mailbox는 CW-09의 저장 계약을 소비한다. 이후 task→run, 점검·정책·저장·수명 통합을 거쳐 CW-16이 모든 수락 기준을 확인한다. 준비된 ticket도 물리 workspace와 resource claim이 맞아야 동시 실행할 수 있다.
