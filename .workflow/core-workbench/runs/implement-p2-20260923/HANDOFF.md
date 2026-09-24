# Handoff: Core Workbench 구현 중단 및 하네스 재정비

## Session Metadata

- 작성: 2026-09-23 20:02 KST
- 저장소: /home/dwchoo/ai-exp-assistant
- Branch / HEAD: main / fea46fd (Initial commit)
- 구현 run: .workflow/core-workbench/runs/implement-p2-20260923/
- 승인 문서: BRIEF r1.8, SPEC s2.1, PLAN p2.1. 묶음 SHA-256: 922bd0dca00c52e3c6ab60f700a12e58aff6421034b623c76bd63ea87b3e6356.
- 복구 snapshot: .workflow/core-workbench/runs/implement-p2-20260923/handoff-current.json 및 handoff-current.tar.gz. Snapshot ID: b0f055c0a7ee82e67607c9fd3e05a1957a5178f9e1eb161384691d65f0ef5b37. 현재 handoff와 run 기록은 snapshot에서 제외했다.
- 사용자 요청으로 구현을 멈췄다. 활성 writer·subagent assignment는 없다.

## Current State Summary

Core Workbench의 명시적 구현 요청으로 개발을 시작했으나, 사용자가 하네스 정비 후 재시작할 수 있는 handoff를 요청해 중단했다. CW-01 계약만 독립 테스트·review를 거쳐 통합됐다. CW-02(G1)는 실제 OMP 두 process와 세 pane 후보를 동작시켰고 최신 독립 테스트 21개가 통과했지만 실제 approval UI·도구 출력·resize/query·detach·외부 terminal 검증은 남았다. CW-03(G2)은 Bash/dash same-shell prototype을 만들었으나 기존 반환 방식의 안전성 테스트가 실패했고, 새로 승인된 wb-handoff는 아직 구현되지 않았다. CW-04(G3)는 두 OMP의 extension 연결·API 접수와 provider 요청 시작을 관측했으나 live harness의 PTY 미drain 때문에 응답 timeout 원인이 불명확하며 변경 도구 pause에는 별도 설계 결함이 있다. CW-05~16은 선행 gate가 끝나지 않아 시작하지 않았다. 완성된 제품이나 최종 통합 검증은 없다.

## Codebase Understanding

## Architecture Overview

- Python backend가 세 PTY·shell 실행·상태를 소유하고 frontend가 원본 manager/worker OMP와 host terminal을 표시하는 설계다. 모델 작업은 두 OMP process가 맡는다.
- CW-01은 Python/TypeScript versioned envelope, 안정된 message ID와 delivery attempt ID, role/session generation, command 상태, PTY byte/control 분리를 제공한다.
- G1 후보는 curses 3-pane, pyte VT screen, PtySession, InputRouter다. 현재 prototype UI 종료는 child 종료이며 제품의 detach/reconnect는 아직 없다.
- G2는 실제 interactive Bash/dash 하나와 FD 9 control event를 사용한다. Prompt나 화면 출력으로 lifecycle을 판정하지 않는다.
- G3는 OMP v18.2.10 공개 extension과 UDS bridge를 사용한다. API 접수, provider 요청 시작, 모델 처리, 업무 완료를 구분한다.

## Critical Files

| 경로 | 역할 / 주의 |
|---|---|
| [AGENTS.md](../../../../AGENTS.md), [.codex/workflow-contract.md](../../../../.codex/workflow-contract.md) | 시작 시 읽을 언어·권한·격리·검증 규칙 |
| [BRIEF.md](../../../../docs/features/core-workbench/BRIEF.md), [SPEC.md](../../../../docs/features/core-workbench/SPEC.md), [PLAN.json](../../../../docs/features/core-workbench/PLAN.json) | 현재 승인 요구와 ticket graph |
| [보충 승인](../../../../.workflow/core-workbench/runs/implement-p2-20260923/g1-g2-policy-approval.json) | C-D47/C-D48 사용자 답변과 현 digest |
| [ledger.json](../../../../.workflow/core-workbench/runs/implement-p2-20260923/ledger.json), [state.json](../../../../.workflow/core-workbench/state.json) | 구현 상태. 실제 파일과 snapshot으로 대조할 것 |
| [G1.md](../../../../docs/features/core-workbench/gates/G1.md), [G2.md](../../../../docs/features/core-workbench/gates/G2.md), [G3.md](../../../../docs/features/core-workbench/gates/G3.md) | gate 관측. G1/G2의 테스트 수는 아래 최신 결과를 우선할 것 |
| [G1 테스트](../../../../tests/gates/g1_vt/test_g1_candidate.py), [G1 UI](../../../../src/workbench/ui/terminal_g1/app.py) | paste/F10/query 수정 후보. Fresh review 미완료 |
| [G2 테스트](../../../../tests/gates/g2_shell/test_g2_shell.py), [G2 prototype](../../../../src/workbench/terminal/shell_g2/prototype.py) | old 반환 방식의 실패 사례. wb-handoff 미구현 |
| [G3 live probe](../../../../tests/gates/g3_omp/live_omp_probe.py), [G3 extension](../../../../omp_bridge/g3/bridge.ts) | PTY drain·tool pause 문제 |
| [G3 review](../../../../.workflow/core-workbench/runs/implement-p2-20260923/cw04-review-1.json), [G3 Oracle](../../../../.workflow/core-workbench/runs/implement-p2-20260923/cw04-oracle-1.json) | blocker 요약. Oracle 예산 4/4 사용 |

## Key Patterns Discovered

- 공유 물리 checkout 하나만 확인돼 worker, test_designer, Root 중 writer는 항상 하나로 직렬화했다. 각 writer 앞뒤 전체 byte snapshot을 남겼다. HEAD에는 거의 코드가 없고 작업 파일 대부분이 untracked이므로 git diff만으로 변경 범위를 판단하면 안 된다.
- PLAN은 CW-01 → CW-02/CW-03/CW-04 → CW-05 순서다. G1~G3의 필수 실증 전에는 후속 production ticket을 완료하거나 통합할 수 없다.
- Fixture 통과는 실제 OMP·shell·outer terminal 증거가 아니다. 특히 G3 fake API 테스트는 모델 완료나 native tool 실행을 증명하지 않는다.

## Work Completed

## Tasks Finished

- CW-01: 계약·harness 통합. Python 14/14, Node TypeScript 12/12, 독립 review 통과. Candidate 51586db6d81f77699c93f54610146f2816d89b2deb1cb0515dcede35a5f581c8.
- CW-02: 실제 OMP 두 세션의 composer·한글·multiline paste·slash palette·clear를 관측했다. C-D47에 따라 paste 전체 거절, 포화 F10, query reply 부분 쓰기 보존을 후보에 반영했다. Root가 최신 G1 suite 21/21 통과를 재확인했다. G1 gate는 pending이다.
- CW-03: Bash 5.2와 dash의 same-shell PID/cwd/env 유지, 일부 lifecycle·job 관측을 prototype으로 확인했다. 독립 테스트는 13개 중 4개 실패를 추가로 발견했다. G2 gate는 pending이다.
- CW-04: 실제 두 OMP의 extension handshake를 관측했다. 이전 nextTurn 후보에서 네 메시지 종류의 API 접수를 확인했고, 현재 후보에서는 task API 접수·provider 요청 1회 뒤 35초 내 응답 미관측으로 model probe가 exit 1이었다. Node fixture 6/6과 Python fixture 6/6. 독립 omp --print auth probe는 약 2.3초에 응답했다. G3 gate는 pending이다.
- 사용자 선택 C-D47(2 MiB paste 전체 거절·이유 표시)과 C-D48(같은 shell의 직접 wb-handoff 후 자동 재개 검토)을 BRIEF r1.8/SPEC s2.1/PLAN p2.1에 반영했다. Ticket ID·의존·write scope는 유지했다.

## Files Modified

| 범위 | 결과 |
|---|---|
| pyproject.toml, src/workbench/contracts/, omp_bridge/contract/, tests/contracts/, tests/gates/harness/ | CW-01 통합 |
| src/workbench/terminal/vt_g1/, src/workbench/ui/terminal_g1/, tests/gates/g1_vt/ | CW-02 후보와 21개 테스트 |
| src/workbench/terminal/shell_g2/, tests/gates/g2_shell/ | CW-03 prototype과 failing test |
| omp_bridge/g3/, src/workbench/ipc/bridge_g3/, tests/gates/g3_omp/ | CW-04 bridge와 probe |
| docs/features/core-workbench/, .workflow/core-workbench/ | 승인·ticket·gate·snapshot 기록 |

## Decisions Made

| 결정 | 근거 / 현재 상태 |
|---|---|
| C-D47: 2 MiB paste 한도·전체 거절·가시적 이유 | 사용자 직접 답변. G1 후보와 테스트에 반영. 정확히 2 MiB에서 bracket marker 포함 여부는 미정 |
| C-D48: 명시적 wb-handoff | 사용자 직접 답변. 문서 반영, G2 코드·테스트 미반영 |
| OMP v18.2.10 고정 | 승인 SPEC과 host probe. 자동 update 없음 |
| G1/G2/G3 미통과 | 실증·review가 남아 CW-05 이후 착수하지 않음 |

## Pending Work

## Immediate Next Steps

1. 하네스부터 신뢰 가능하게 고친다. G3 live probe는 PTY 출력을 지속적으로 drain하고 본문은 폐기하며 bounded timeout·process 정리·provider/assistant event를 구분한다. 같은 no-tools single-task probe를 한 번만 재실행해 이전 timeout을 분류한다. G3 fake test에는 같은 batch의 A와 B가 pause 전에 모두 준비된 경합을 추가한다.
2. G2 테스트를 C-D48에 맞춰 갱신한다. 기존 cd/export 뒤 return_to_manager만으로 clean을 기대하는 테스트를 교체하고 같은 shell에서 직접 실행한 wb-handoff의 fresh checkpoint를 요구한다. Stale READY, manual background/suspend, 미제출 tail, PS2, nested shell, 취소/FD 유실에서 자동 PTY write와 START가 없음을 검증한 뒤 prototype을 고친다.
3. G1 21개 통과 후보를 동결하고 fresh delta reviewer에게 검토받는다. 실제 approval UI·도구 출력·OMP query/resize·일반 terminal/tmux/herdr·detach 조건을 하네스로 재현한다. 미시험 조합은 분리한다.
4. G3 제품 문제를 별도로 고친다. 전달 본문에 inReplyToMessageId를 보존한다. 준비 단계 tool_call을 실행 중 도구로 간주하지 않는다. Oracle의 같은 이름 builtin wrapper + ctx.invokeTool 제안은 native approval/concurrency 보존과 A 실행 중 pause 뒤 준비된 B 차단을 고정 OMP loop에서 입증한 뒤 채택한다. Read/report는 계속돼야 한다.
5. G1~G3 통과와 독립 review·integration evidence가 갖춰진 뒤에만 CW-05를 열고 PLAN 순서대로 진행한다. 이전 후보를 폐기하고 처음부터 재구현하지 않는다.

## Blockers/Open Questions

- G1: fixture 21/21은 통과했지만 실제 OMP approval·도구 출력·query/resize·외부 terminal·detach는 미확인. 정확히 2 MiB에서 marker 포함 여부도 좁은 명세 선택이 남았다.
- G2: InputBoundary가 owner return에 오래된 READY를 재사용하고 manual background job을 놓친다. 13개 중 4개 실패. wb-handoff의 같은 shell clean checkpoint 구현이 필요하다.
- G3 하네스: live PTY를 읽지 않아 timeout 원인을 분류할 수 없다. Fake 테스트는 B의 tool_call을 pause 뒤에만 호출한다. 실제 busy/approval/composer 경합·ACK loss·reconnect·tool A/B는 미실증이다.
- G3 제품: OMP tool_call은 batch의 도구를 실행 전에 모두 준비하므로 현재 pause가 이미 준비된 B를 막지 못한다. inReplyToMessageId도 누락됐다. Generic 공개 pre-exec 차단 hook은 찾지 못했다.
- 하네스 수정만으로 G1~G3의 제품 gate가 자동 통과하는 것은 아니다.

## Deferred Items

- CW-05~16과 Phase 2는 선행 gate 뒤에 착수한다.
- Mac·Windows는 현재 범위가 아니다. Linux 우선이며 Windows는 제외한다.
- Commit/push/PR/deploy는 요청받지 않아 수행하지 않았다.

## Context for Resuming Agent

## Important Context

사용자는 하네스 문제 때문에 진척이 없다고 느껴 현재 구현 중단과 handoff를 요청했다. 다음 세션은 이 문서와 실제 파일·snapshot을 대조한 뒤 하네스 수정부터 시작한다. CW-01만 integrated이며 나머지 후보를 완료로 승격하지 않는다. G3의 PTY 미drain은 하네스 결함이고 prepared B pause race는 제품 설계 결함이다. G2 기존 반환 테스트는 새 C-D48과 충돌한다. C-D47/C-D48은 이미 승인됐으므로 다시 묻지 않는다. Oracle 예산 4/4를 사용했고 자문은 run 기록에 있다. 같은 미해결 문제의 예산을 새 run 이름으로 조용히 초기화하지 않는다.

Writer lease와 active assignment는 모두 반환됐다. 대부분 파일은 untracked이다. Git reset, clean, stash, 자동 commit으로 사용자 변경을 없애지 않는다. 재개할 때 handoff-current.json manifest와 현재 byte를 비교하고 변경이 있으면 먼저 귀속을 판단한다. Docker로 MCP를 실행·설치하지 않는다.

## Assumptions Made

- 두 사용자 답변은 C-D47/C-D48 정책 승인이다. PLAN의 ticket graph·scope는 유지됐다.
- 하네스 수정은 재현성을 높일 뿐 승인된 제품 동작이나 수락 기준을 축소하지 않는다.
- /tmp/cw02-g1-venv는 임시 환경이다. 재개 시 존재 여부와 pyte 버전을 확인한다.

## Potential Gotchas

- G1.md는 최신 테스트 추가 전 작성돼 20개라고 적었다. 현재 21개 통과다. G2.md의 7개 통과는 독립 실패 테스트 추가 전 기록이다.
- G3.md의 35초 timeout을 provider 실패로 단정하지 않는다. 독립 auth probe는 성공했고 live PTY는 drain하지 않았다.
- OMP v18.2.10의 tool_execution_start는 실제 native 시작을 보장하는 차단 hook이 아니다. 여기서 in-flight를 세기만 하면 pause 문제가 해결되지 않는다.
- G1 pyte는 임시 가상환경에만 설치돼 있다. 시스템 package 설치나 OMP update는 하지 않았다.
- 최신 G1 테스트 변경은 CW-02 worker snapshot 이후에 들어왔다. 최종 코드+테스트 후보의 fresh review는 아직 없다.
- Workflow state는 운영 인덱스다. 독립 검증·통합 증거 자체가 아니다.

## Environment State

## Tools/Services Used

- Python 3.12.3, Node 22.23.2, OMP 18.2.10, Bash 5.2.21, /usr/bin/sh → dash.
- G1 pyte 0.8.2는 /tmp/cw02-g1-venv에만 설치했다.
- 공유 physical checkout에서 serial mutation lease를 사용했다. 격리된 worktree는 provision되지 않았다.

## Active Processes

- 인계 시 active subagent writer 없음. 관측한 G1/G3 probe PID는 종료됐다. 서버·watcher를 의도적으로 남기지 않았다.
- 재개 시 host process 상태를 새로 확인한다. Ledger 값만으로 lease를 해제하지 않는다.

## Environment Variables

- G3 probe가 임시 OMP child에 사용하는 이름: WORKBENCH_G3_BRIDGE_SOCKET, WORKBENCH_G3_ROLE, WORKBENCH_G3_TOKEN, WORKBENCH_G3_GENERATION, WORKBENCH_G3_EXPECTED_OMP_VERSION. 값은 저장하지 않는다.
- G1 probe의 TERM, COLORTERM, LANG은 해당 child에서만 설정한다.

## Related Resources

- [Workflow contract](../../../../.codex/workflow-contract.md), [implement skill](../../../../.agents/skills/implement/SKILL.md), [change-verification skill](../../../../.agents/skills/change-verification/SKILL.md)
- [Run ledger](../../../../.workflow/core-workbench/runs/implement-p2-20260923/ledger.json), [현재 snapshot](../../../../.workflow/core-workbench/runs/implement-p2-20260923/handoff-current.json)
- [OMP v18.2.10 extension guide](https://github.com/can1357/oh-my-pi/blob/v18.2.10/docs/extensions.md), [tool 준비·실행 순서](https://github.com/can1357/oh-my-pi/blob/v18.2.10/packages/agent/src/agent-loop.ts#L2679-L2761)
