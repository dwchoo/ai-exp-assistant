# Handoff: Core Workbench p2.6 — CW16 scoped 검증 완료 후 final composition 미완료

## Session Metadata

- Created: 2026-09-29 10:22:30 KST
- Project: `/home/dwchoo/ai-exp-assistant`
- Branch: `main`
- HEAD at handoff creation: `7b2048c` (`feat(core-workbench): checkpoint G2 shell runtime controls`)
- Session duration: 2026-09-26~2026-09-29의 resumed `$implement` 및 최종 검증
- Referenced Codex thread: `01a0e351-ba77-7c81-9a36-a7941cdd5310`

## Recent Commits (context only)

- `7b2048c feat(core-workbench): checkpoint G2 shell runtime controls`
- `79808a7 docs(core-workbench): hand off committed partial implementation`
- `398053d chore(core-workbench): retain p2.3 run evidence and paused state`

## Handoff Chain

- **Continues from**: `.codex/handoffs/2026-09-26-core-workbench-p26/HANDOFF.md`
- **Earlier handoff**: `.codex/handoffs/2026-09-26-core-workbench-p25/HANDOFF.md`
- **Supersedes**: 위 문서의 당시 상태 요약만 대체한다. 승인·결정·과거 증거는 대체하지 않는다.

## Current State Summary

승인된 BRIEF r1.13 / SPEC s2.6 / PLAN p2.6에 따라 Core Workbench 구현을 재개해 CW12~CW15 formal gate와 CW16의 제한된 runtime compatibility candidate를 마쳤다. CW16 candidate `6dc51f767d38e130987441c56145414474b6ce84960d86f7f9d3a23ead8afa88`는 실제 Bash/dash 및 OMP 18.2.10의 plain PTY·외부 tmux·격리 Herdr matrix 6/6을 통과했고 fresh reviewer도 scoped P0/P1/P2를 찾지 않았다. 그러나 전체 Core의 final production entrypoint/composition, backend-preserving detach/reconnect, 실제 reboot recovery가 존재하거나 실행되지 않아 formal gate는 `passed: false`다. 따라서 CW16 scoped compatibility candidate만 ready이며 **Core 전체 `done_verified=false`**다. 현재 요청은 handoff 문서 작성뿐이므로 구현·테스트·commit·push·배포를 새로 수행하지 않았다.

## Codebase Understanding

## Architecture Overview

목표 흐름은 `manager OMP -> Task/revision/approval -> worker OMP -> persistent host run -> evidence -> manager completion/recovery`다. 주요 경계는 다음과 같다.

- `src/workbench/app/`: lifecycle 및 production orchestration. 현재 component들을 연결하지만 최종 사용자 경로를 실행하는 production entrypoint/composition은 없다.
- `src/workbench/tasks/`, `src/workbench/workflow/`, `src/workbench/policy/`: Task identity/revision, 승인 후 실행, pause/resume·중단 정책을 담당한다.
- `src/workbench/runtime/`, `src/workbench/storage/`, `src/workbench/observation/`: run identity/lifetime, durable state/raw evidence, 관측과 recovery를 담당한다.
- `src/workbench/terminal/shell_persistent/`, `src/workbench/terminal/shell_g2/`: persistent host shell과 Bash/dash control boundary를 담당한다.
- `src/workbench/ui/terminal_g1/`, `src/workbench/ui/status_workbench/`: terminal/status UI를 담당한다. backend 수명을 보존한 UI detach/reconnect의 최종 composition은 아직 없다.
- `src/workbench/ipc/bridge_g3/`, `omp_bridge/`: OMP mailbox/bridge 경계다.

Local component gate 통과와 최종 제품 runtime 통과는 별개다. `check-cw16-final-01`의 test process는 exit 0이지만 acceptance items를 대조한 `gate-cw16-final-result.json`은 미완료 항목 때문에 실패한다.

## Critical Files

| File | Purpose | Relevance |
|------|---------|-----------|
| `docs/features/core-workbench/BRIEF.md` | 승인된 제품 요구 | 범위와 `done_verified` 기준의 정본 |
| `docs/features/core-workbench/SPEC.md` | 실행·상태 계약 | final composition이 지켜야 할 계약 |
| `docs/features/core-workbench/PLAN.json` | p2.6 ticket/gate graph | 기존 승인 bundle과 acceptance identity |
| `docs/features/core-workbench/tickets/CW-16.md` | 최종 compatibility ticket | 현재 scoped runtime matrix와 전체 acceptance 차이를 확인할 곳 |
| `docs/features/core-workbench/VERIFICATION.md` | 검증 결과 및 미완료 경계 | 전체 완료로 오인하지 않게 하는 사용자 문서 |
| `docs/features/core-workbench/COMPATIBILITY.md` | 지원 환경/검증 matrix | Bash-host outer matrix와 별도 Bash/dash matrix를 구분 |
| `tests/integration/test_cw16_runtime_matrix.py` | CW16 actual runtime harness | 6개 matrix와 timeout/output/resource 안전성 검증 |
| `src/workbench/app/production.py` | production component 조합 | final entrypoint 부재를 해결할 때 우선 대조할 코드 |
| `src/workbench/app/lifecycle.py` | run identity/lifecycle/shutdown | exact identity, at-most-once stop, recovery 불변식 |
| `.workflow/core-workbench/runs/implement-p2.6-20260927/check-cw16-final-observed.json` | item별 최종 관측 | passed/failed/not_run 및 unknown의 정본 |
| `.workflow/core-workbench/runs/implement-p2.6-20260927/gate-cw16-final-result.json` | formal gate 결과 | `passed: false`의 직접 근거 |
| `.workflow/core-workbench/runs/implement-p2.6-20260927/ledger.json` | invocation journal | revision 1319와 누적 호출 이력의 정본 |

## Key Patterns Discovered

- 종료·takeover·recovery는 PID만이 아니라 durable `RunIdentity`와 monotonic generation을 사용한다. 잘못된 task/revision/run identity를 완료로 승격하지 않는다.
- 외부 요청 접수와 실행 승인은 분리한다. claim은 submission이며 승인/실행 완료가 아니다.
- unknown은 success가 아니다. 수명·귀속·정책 관측이 불충분하면 자동 후속 실행을 보류한다.
- raw-log failure accounting은 concurrent writer의 더 최신 durable descendant를 덮어쓰지 않아야 한다.
- runtime harness는 외부 tmux/Herdr cleanup CLI를 호출하지 않는다. 테스트가 소유한 exact sandbox/process만 pidfd와 identity 검증으로 정리한다.
- output capture는 child가 상속한 hard `RLIMIT_FSIZE=8 MiB`와 parent의 `limit+1` bounded read를 함께 사용한다.

## Work Completed

## Tasks Finished

- [x] CW12의 승인/실행 경계와 exact run-incarnation permit을 독립 test/review 및 formal gate까지 마침.
- [x] CW13의 lifecycle·force semantics와 exact shutdown identity를 독립 검증함.
- [x] CW14의 raw-log concurrent recovery/failure accounting을 수정하고 검증함.
- [x] CW15의 lifecycle/takeover/duplicate/no-replay/at-most-once 정합성을 최종 검증함.
- [x] CW16 scoped runtime matrix를 실제 Bash 5.2.21, dash, OMP 18.2.10, tmux 3.4, Herdr 0.9.1에서 실행함.
- [x] CW16 integration suite 6/6 통과, independent test와 fresh reviewer 완료.
- [x] `VERIFICATION.md`와 `COMPATIBILITY.md`에 지원 범위와 pending 항목을 기록함.
- [x] 전체 acceptance를 formal gate로 대조해 Core가 아직 완료되지 않았음을 증거로 남김.

## Files Modified

이 표는 현재 dirty workspace의 핵심 구현 산출물만 요약한다. 모든 dirty 파일을 이번 handoff 작성이 만들었다는 뜻이 아니며, 기존 사용자 변경도 섞여 있다.

| File | Changes | Rationale |
|------|---------|-----------|
| `src/workbench/app/**` | production/lifecycle, evidence, recovery orchestration | manager-worker-host lifecycle 불변식 구현 |
| `src/workbench/tasks/**`, `src/workbench/workflow/**` | Task/revision/approval/execution 상태 | 외부 접수와 승인 실행 분리 |
| `src/workbench/policy/**` | pause/review/resume 및 실행 정책 | unknown·중단·재개 정책 보존 |
| `src/workbench/storage/**`, `src/workbench/observation/**` | durable state, raw evidence, recovery | concurrent failure 및 관측 정합성 |
| `src/workbench/runtime/**` | run identity/lifetime 처리 | exact identity와 takeover generation 보장 |
| `src/workbench/terminal/shell_persistent/**` | persistent shell backend | G2 shell 경계를 production component로 제공 |
| `tests/integration/test_cw16_runtime_matrix.py` | actual compatibility matrix와 안전한 harness | CW16 scoped runtime evidence 생성 |
| `docs/features/core-workbench/VERIFICATION.md` | 최종 검증 결과 | 통과와 미실행 항목을 분리 |
| `docs/features/core-workbench/COMPATIBILITY.md` | 환경 matrix | 검증한 조합만 명시 |
| `graphify-out/**` | AST graph 갱신 | 코드 변경 후 project graph 최신화 |

핵심 현재 SHA-256:

- `tests/integration/test_cw16_runtime_matrix.py`: `812d20bce2e961f6f434b43ec8aa9a9b465e27183289026ca37eadcae6415363`
- `docs/features/core-workbench/VERIFICATION.md`: `f76e499c99d81f7d92ed3d3ce2e6de2217d83d61ca8561230edf766d5ea6b4c7`
- `docs/features/core-workbench/COMPATIBILITY.md`: `451969edd5bc60a9f5b33219db3472288f41481ff55625f3c57b8af17a8d9728`

## Decisions Made

| Decision | Options Considered | Rationale |
|----------|-------------------|-----------|
| CW16 scoped candidate와 Core 전체 완료를 분리 | 6/6만으로 완료 처리 / item-level formal gate | 최종 product flow가 실행되지 않았으므로 false-positive 완료를 막기 위해 formal gate를 우선함 |
| Outer compatibility는 Bash host의 plain/tmux/Herdr로 한정 | dash까지 outer 3종과 cross product 주장 / 실제 실행 조합만 주장 | 실제 evidence가 Bash-host outer 3종과 별도의 Bash/dash canonical matrix이기 때문 |
| timeout cleanup은 test-owned exact identity만 대상으로 함 | tmux/Herdr CLI cleanup / pidfd+start-time+owner token | 기본 사용자 session/state를 건드리지 않기 위해서 |
| output을 pipe가 아닌 bounded file로 capture | pipe EOF 대기 / hard file limit과 bounded read | detached descendant의 pipe 보유로 인한 hang과 무제한 출력을 막기 위해서 |
| final composition 부재를 새 승인 없이 임의 구현하지 않음 | 기존 component를 추정 연결 / ticket·acceptance를 먼저 명시 | 제품 동작 결정과 검증 범위를 발명하지 않기 위해서 |

## Pending Work

## Immediate Next Steps

1. 이 handoff와 기존 p2.6 handoff를 읽고 현재 `git status`, `ledger.json`, formal gate 및 핵심 file hash를 대조한다. handoff 읽기만으로 구현·테스트·agent dispatch를 시작하지 않는다.
2. `P-C-AC-19`, `I-FLOW`, `I-SHELL`, `I-POLICY`, `I-FAULT`, `I-COMPAT`을 닫는 **final production composition 후속 ticket/spec 변경**을 작성하고 사용자 승인을 받는다. 기존 PLAN에 없는 제품 동작을 추정하지 않는다.
3. 명시적 `$implement` 권한 아래 실제 manager OMP→Task/revision→worker OMP→persistent host→evidence→completion/recovery entrypoint와 UI/backend composition을 구현한다.
4. backend-preserving detach/reconnect를 plain PTY·외부 tmux·격리 Herdr에서 검증하고, 안전한 별도 절차로 real reboot recovery를 실행한다.
5. combined policy/runtime 및 fault/compatibility matrix를 다시 수집한 뒤 formal gate를 재실행한다. 모든 required item이 passed이고 unknown이 없을 때만 Core `done_verified=true`를 검토한다.

## Blockers/Open Questions

- [ ] Blocker: 최종 product entrypoint/composition이 없다. Needs: 승인된 후속 ticket과 구현 범위.
- [ ] Blocker: backend-preserving UI detach/reconnect를 실행할 실제 composition이 없다. Needs: backend lifetime과 UI attachment를 분리한 production 경로.
- [ ] Blocker: real reboot recovery evidence가 없다. Needs: 데이터/프로세스 안전 경계를 명시한 host reboot 검증 절차와 실행 권한.
- [ ] Open question: missing final composition을 기존 CW16 수정으로 다룰지 새 ticket으로 추가할지 결정해야 한다. Suggested: dependency와 write scope가 드러나는 후속 ticket을 추가하고 PLAN digest를 재승인한다.

## Deferred Items

- Task Inbox와 Web Board는 Phase 2이며 Core 완료·검증 이후로 유지한다.
- conda 전용 점검은 C-D55에 따라 완료 조건에서 제외됐다. 일반 environment inheritance는 계속 요구된다.
- Docker runtime 검증은 기존 사용자 결정에 따라 생략 상태이며 passed로 표기하지 않는다.
- cgroup 필수화/선택 기능은 이번 Core 범위가 아니다.
- commit, push, 게시, 배포는 별도 사용자 요청 전까지 수행하지 않는다.

## Context for Resuming Agent

## Important Context

- 승인 bundle digest는 `64bcf577a30e7c6bbff3e4e775f8e6c17ca212ce3bf79b5bc5be31196a792b07`이다. 승인된 문서와 실제 파일을 다시 대조하되 과거 approval을 현재 변경에 자동 재발급하지 않는다.
- implement run은 `.workflow/core-workbench/runs/implement-p2.6-20260927/`이다. ledger는 1319 events/revision에 해당하며 마지막 reconciled snapshot은 active call과 unresolved item이 없다. 누적 사용량과 실패 이력을 초기화하지 않는다.
- 최종 scoped candidate ID `6dc51f767d38e130987441c56145414474b6ce84960d86f7f9d3a23ead8afa88`는 CW16 watch scope만 식별한다. 전체 dirty workspace나 Core 완료를 식별하지 않는다.
- formal execution은 candidate before/after가 동일하고 integration 6/6, exit 0이었다. 하지만 `gate-cw16-final-result.json`은 `P-C-AC-19` 및 다섯 I-* item 때문에 `passed: false`다.
- `P-C-AC-20`만 최종 CW16 item 중 passed다. `P-C-AC-19`, `I-COMPAT`은 failed이고 `I-FLOW`, `I-SHELL`, `I-POLICY`, `I-FAULT`는 not_run이다.
- fresh reviewer는 scoped candidate에서 P0/P1/P2를 찾지 않았지만 명시적으로 `CW-16/전체 Core done_verified: false`라고 판정했다.
- workspace는 대량의 staged/unstaged/untracked 변경이 섞인 shared dirty tree다. `reset`, `clean`, `stash`, 일괄 stage/commit을 하지 말고 변경 전 mutation owner와 exact scope를 확인한다.
- handoff 작성 시 collaboration child는 모두 completed이며 새 writer를 시작하지 않았다. final harness는 owned residue 0과 Herdr 기본 state 불변을 검증했다.

## Assumptions Made

- 다음 수신자는 한국어 문서와 현재 AGENTS/workflow contract를 따른다.
- 기존 BRIEF/SPEC/PLAN 승인과 C-D55 결정은 보존되지만, final composition이라는 새 write scope는 별도 승인 가능한 ticket로 명시해야 한다.
- 지원 대상은 Linux이며 Windows는 제외한다. Workbench 내부에서 tmux에 의존하지 않고 외부 tmux/Herdr 안에서 동작한다.

## Potential Gotchas

- `/usr/bin/python`에는 `pyte`가 없어 outer matrix가 import error로 실패한다. 승인된 interpreter는 `/tmp/cw02-g1-venv/bin/python`이다.
- Bash/dash canonical matrix와 plain/tmux/Herdr outer matrix를 곱집합으로 해석하지 않는다. outer 3종의 host shell은 Bash다.
- test command exit 0과 formal gate pass를 혼동하지 않는다. item observation과 gate result가 최종 판정 근거다.
- 외부 tmux/Herdr의 default state를 cleanup 대상으로 삼지 않는다. isolated test-owned state만 생성·삭제한다.
- PID 재사용과 detached descendant 때문에 `os.kill`/`killpg`만으로 cleanup하지 않는다. `pidfd_open`, start-time 재검증, `pidfd_send_signal` 경계를 유지한다.
- 정상 종료 경로에서 owned leak이 발견되면 테스트 실패다. leak을 조용히 정리하고 pass하지 않는다.
- `graphify-out/` dirty 상태는 정상일 수 있다. 코드 변경 후 `graphify update .`를 실행하되 graph 조회/cache 변경을 candidate 변화로 고려한다.
- 이전 handoff, status flag, reviewer 문자열만으로 success를 추론하지 않는다. actual file/evidence와 formal gate를 대조한다.

## Environment State

## Tools/Services Used

- Python: `/tmp/cw02-g1-venv/bin/python` (approved G1 interpreter with `pyte`)
- Bash: `/usr/bin/bash` 5.2.21
- POSIX sh: `/usr/bin/sh` -> dash
- OMP: `/home/dwchoo/.local/bin/omp` 18.2.10
- tmux: `/usr/bin/tmux` 3.4, 외부 host compatibility 용도
- Herdr: `/home/dwchoo/.local/bin/herdr` 0.9.1, isolated non-nested probe 용도
- Graphify: 코드 변경 후 `graphify update .` 수행됨

## Active Processes

- handoff 작성 시 collaboration child 4개는 모두 `completed`; active writer 없음.
- 이 handoff 작업이 시작한 dev server, watcher, OMP, tmux 또는 Herdr session은 없음.
- 최종 CW16 independent test는 test-owned process/session/temp residue 0을 보고했다. 재개 시 실제 host 상태를 다시 확인한다.

## Environment Variables

- `PYTHONDONTWRITEBYTECODE`
- `PYTHONPATH`

값이나 secret은 이 문서에 기록하지 않는다.

## Verification Evidence

최종 scoped check command:

```bash
/usr/bin/env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  /tmp/cw02-g1-venv/bin/python -m unittest discover \
  -s tests/integration -p 'test_*.py' -v
```

관측 결과: 6 tests, 32.293s, `OK`; candidate before/after `6dc51f...afa88`; log SHA-256 `6f8612bb284911b1822c8645d01561e3f14bdcaee7be3fd05743d610024a80bd`.

이 handoff 작성 중에는 위 test를 재실행하지 않았다. 기존 formal artifacts와 file hashes만 읽어 정리했다.

## Related Resources

- `AGENTS.md`
- `.codex/workflow-contract.md`
- `.agents/skills/implement/SKILL.md`
- `.agents/skills/change-verification/SKILL.md`
- `docs/features/core-workbench/DECISION-2026-09-26.md`
- `docs/features/core-workbench/OPERATING-CONTRACT.md`
- `.workflow/core-workbench/runs/decisions-cd55-20260926/approval.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/requirements-cw16.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/check-cw16-final-request.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/check-cw16-final-execution.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/check-cw16-final-observed.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/gate-cw16-final-result.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/ledger-check-cw16-final-result.json`
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-cw16-output-final-review-01.json`

## Resume Prompt

```text
이 handoff와 이전 p2.6 handoff를 읽고 현재 files, git status, ledger revision 1319, CW16 formal gate를 대조해줘. scoped candidate 6dc51f...afa88은 integration 6/6과 fresh review를 통과했지만 Core done_verified는 false다. handoff 읽기만으로 구현을 재개하지 말고, final production entrypoint/composition·backend-preserving detach/reconnect·real reboot를 닫을 승인된 후속 ticket부터 확인해줘. 기존 dirty workspace를 reset/clean/stash하지 말고 commit/push/deploy도 하지 마.
```

---

**Security Reminder**: 이 문서는 secret 값이나 credential을 포함하지 않는다. `validate_handoff.py`로 completeness와 secret pattern을 검증한다.
