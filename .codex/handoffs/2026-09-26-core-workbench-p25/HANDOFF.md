# 작업 인계: Core Workbench p2.5 구현 — 호출 상한 도달 후 새 세션 인계

인계 목적: 기존 승인·증거·미완료 범위를 보존하고 새 세션에서 상태 대조 후 다음 작업을 결정한다.

## 시작 요약

2026-09-26 사용자 요청으로 작성한다. 작업 경로는 /home/dwchoo/ai-exp-assistant, HEAD 79808a7이다. Core Workbench와 CW-03은 미완료다. 이번 implement는 누적 child 호출 186회에서 시작해 34회를 사용, 220/220회 상한에 도달했다. 최소 input-return/signal/subreaper 판별은 full G2 matrix 확장에 사용할 수 있다는 reviewer 판단을 받았고, bounded G2-HOOK-ENV/G2-LAUNCH slice도 최종 review를 통과했다. 제품 adapter 완성이나 G2 전체 통과가 아니다. 이번 변경은 아래 네 개의 새 untracked probe/test 파일에 집중됐다.

## 사용자 결정과 권한

사용자는 BRIEF r1.12, SPEC s2.5, PLAN p2.5 및 Oracle 수정 반영본을 승인하고 `$implement docs/features/core-workbench`를 명시했다. 문서 일부의 승인 전 문구보다 이 사용자 승인 기록을 우선하되 현재 파일 변경을 대조한다. Docker runtime 검증 생략은 유지한다. Commit/push/게시/배포 승인은 없다. 새 세션은 승인을 자동 폐기하지 않지만 누적 220회 상한을 초기화하거나 초과할 권한도 없다. 현재 요청은 handoff 작성이며 구현 재개가 아니다. 최신 사용자 AGENTS.md는 개발 기본 권고를 GPT-5.6 Sol High로 변경했으나 모델을 자동 전환하지 않는다. 한국어 응답, 기술 식별자 원문, Python/Linux 우선, 내부 tmux 의존 금지, Phase 2 후순위 규칙을 유지한다.

## 구현·검증 상태

완료한 bounded 작업: 부모 WAIT Ctrl-C 탈출 수정, set -e 상태/종료 코드 보존, trap 복원, waitpid 후손 회수, Bash/dash native SIGQUIT 구분, start/return signal 16셀, event 누락 hard failure와 TSTP no-stop unknown 분리, env/venv 상속·비전파, 제한된 hook 보류, Bash 우선/sh fallback, argv literal과 child script 실행, 실제 executable·starttime·단일 CHILD_READY 검증.

마지막 Root 실행 결과(2026-09-25): env/launch predicates 8개 통과, combined/lifecycle predicates 25개 통과. Env/launch live 10개 사례 exit 0. Combined live는 44개 case record, parent-WAIT TSTP unknown 2개와 stage TSTP unknown 4개, hard failure 0개로 exit 2 inconclusive. 44개 전체를 성공 44개로 부르지 않는다. 실제 suspend 지원은 미입증이며 conda 실행 파일 부재로 unavailable이다. Docker는 skipped, passed가 아니다. graphify update 완료. 최종 당시 candidate e64b3ff875e953a769b6acc38a3e843f5eb45a6f9cbcee2099be76b6ac8642d9.

2026-09-26 상태 브리핑에서 네 파일 hash가 마지막 검증과 일치함을 확인했다. 이번 인계에서는 테스트를 재실행하지 않았다. 이후 지침·skill/config 등 workspace 변경이 있으므로 전체 historical candidate 동일성을 주장하지 않는다. 남은 범위: CW-03 input-jobs, takeover/ACK-loss/no-replay, supervisor/FD/flush 장애, manual daemon 및 관측 밖 수명, 실제 suspend/conda 지원, 전체 G2 통합. 실행 범위 exec/pipeline/명령 치환, 빠른 spawn/exit 및 추가 spawn 경합, terminal reply/foreground-exit 입력 사례 등도 ticket 전체 acceptance와 다시 대조해야 한다. CW-02/04도 앞선 부분 검증 상태로 알려졌으며 전체 완료를 가정하지 않는다. CW-05~16 dependency release는 아직 없다.

## 진행 중인 작업

2026-09-26T14:31Z collaboration.list_agents에서 Root만 관측했다. Ledger journal events/revision 150, 마지막 compact summary는 used=220, reserved=0, available=0, active_calls={}, unresolved=[]다. 기존 child agent 호출을 재시작하지 않는다. 마지막 검증 당시 probe residue 없음이 보고됐지만 인계에서 OS process를 새로 검사하지 않았다.

작업 트리는 dirty하며 staged와 unstaged 변경이 섞여 있다. AGENTS.md, workflow-contract, skill/agent/config, 승인 요구 문서, graphify 관련 사용자 변경을 보존해야 한다. 네 probe/test 및 graphify-out, workflow run, handoff 경로는 untracked다. reset/clean/stash 또는 일괄 stage/commit 금지. 다른 사용자의 graphify handoff를 덮어쓰지 않는다. 실제 다음 작업 전 최신 git status와 mutation owner를 확인한다.

## 시도와 배운 점

중요한 반례와 해결: SCRIPT_READY 이전 Ctrl-C 주입 및 signal.pause missed wakeup을 제거했다. SIGQUIT=-3/no-tail을 Bash에도 강제하던 기대는 승인 계약보다 강해 native 동작으로 수정했다(no-tail 필수는 Ctrl-C). 일반 reap에서 double-fork fixture SIGUSR2를 무조건 기다리던 순환 대기를 분리했다. 부모 FD8 read의 SIGINT 탈출을 control trap/retry로 보호하고 TAKEOVER에서 이전 trap을 복원했다. helper nonzero status는 if/else로 받아 set -e 아래 부모 종료를 막았다.

TSTP 정지가 관측되지 않으면 성공으로 바꾸지 않고 unknown으로 남긴다. required signal event timeout/전송 오류는 failed exit1이며, 실제 TSTP event 후 no-stop만 allowed unknown exit2다. 예상 LAUNCH 문자열만으로 executable을 증명할 수 없어 exec 후 child-alive barrier에서 /proc exe/parent/session/PG/starttime을 확인한다. 양수 starttime만 검사하거나 duplicate READY를 허용하던 두 false-pass도 독립 회귀와 최종 review로 수정했다. 공유 foreground PG를 유지했다; 분리 PG는 관측자 정지/job-control 충돌 증거 없이 확정하지 않았다. 직접 부모 eval 방식으로 돌아가지 않는다.

## 다음 행동

1. 이 HANDOFF와 manifest를 읽고 `.agents/skills/handoff/scripts/handoff.py handoff-validate`로 current workspace/reference/ledger 변화를 분류한다. 검증 명령 예: python .agents/skills/handoff/scripts/handoff.py handoff-validate --input /dev/stdin (JSON root와 directory 지정). 문서 읽기·상태 요청만으로 tests나 implementation을 시작하지 않는다.
2. 현재 AGENTS.md/workflow-contract/implement skill, 실제 journal과 git status를 대조한다. 네 파일은 final-root-check의 SHA와 비교한다. handoff candidate와 historical final candidate는 observation policy와 이후 문서 변경 때문에 다를 수 있다.
3. 구현을 재개하려면 사용자에게서 새 누적 호출 상한을 받아야 한다. 기존 220 사용량을 유지한다. 새 journal/세션으로 회수를 0으로 초기화하지 않는다. 이전 helper는 기존 user-origin limit 변경을 거부할 수 있으므로 최신 ledger skill의 지원 절차를 확인하고, 새 승인 근거와 원본을 보존하는 conversion/reconciliation을 적용한다. 사용자 상한 변경을 operating allocation으로 우회하지 않는다.
4. 재개 승인이 있으면 CW-03의 남은 acceptance를 현재 증거와 대조해 가장 작은 다음 unit을 정한다. Input-jobs/takeover/failure 및 manual residue matrix가 우선 후보이며, 합의되지 않은 제품 동작을 발명하지 않는다. CW-02/04 상태도 dependency release 전에 확인한다.
5. 구현은 한 workspace 한 writer, 독립 test_designer와 fresh reviewer, Root integration을 따른다. 코드 변경 후 graphify update . 실행. CW-03의 모든 required gate item을 충족하기 전 integrated나 CW-05 ready로 표시하지 않는다. 완료 기준은 PLAN 전체 acceptance와 최종 통합 검증이며 현재는 partial이다.

새 세션 시작 문구: '이 handoff를 읽고 현재 파일·ledger와 대조해 상태를 확인해줘. 누적 호출은 220/220이며 상한 변경 전에는 추가 agent 호출이나 구현을 재개하지 마. 기존 BRIEF r1.12/SPEC s2.5/PLAN p2.5 승인과 Docker runtime 생략 결정을 유지해.'

## 읽기 경로

- `AGENTS.md` — 현재 프로젝트 지침; 최신 사용자 지침을 우선한다
- `.codex/workflow-contract.md` — 호출 예산·승인·독립 검증·재개 규약
- `.agents/skills/implement/SKILL.md` — 명시적 구현 재개 시 적용
- `.agents/skills/handoff/SKILL.md` — 수신 검증; 읽기만으로 구현하지 않는다
- `docs/features/core-workbench/BRIEF.md` — 승인된 r1.12 요구
- `docs/features/core-workbench/SPEC.md` — 승인된 s2.5 명세
- `docs/features/core-workbench/PLAN.json` — 승인된 p2.5 dependency 및 acceptance 원본
- `docs/features/core-workbench/tickets/CW-03.md` — 미완료 G2 acceptance 확인
- `docs/adr/0002-managed-shell-control-wait.md` — 부모 control wait와 별도 supervisor 구조
- `.workflow/core-workbench/runs/implement-p2.5-20260925/approval.json` — 이번 사용자 승인 기록
- `.workflow/core-workbench/runs/implement-p2.5-20260925/final-root-check.json` — 최종 Root 결과 요약; 정식 check schema가 아닌 수기 집계
- `.workflow/core-workbench/runs/implement-p2.5-20260925/final-snapshot.json` — 2026-09-25 검증 당시 workspace manifest
- `.workflow/core-workbench/runs/implement-p2.5-20260925/ledger-final-summary.json` — revision 150 누적 집계; 실제 journal을 다시 읽을 것
- `tests/gates/g2_shell/live_combined_boundary_probe.py` — 이번 실행의 probe/독립 테스트; final-root-check hash와 대조
- `tests/gates/g2_shell/test_live_combined_boundary_predicates.py` — 이번 실행의 probe/독립 테스트; final-root-check hash와 대조
- `tests/gates/g2_shell/live_env_launch_probe.py` — 이번 실행의 probe/독립 테스트; final-root-check hash와 대조
- `tests/gates/g2_shell/test_live_env_launch_predicates.py` — 이번 실행의 probe/독립 테스트; final-root-check hash와 대조
- `.workflow/core-workbench/runs/implement-p2.5-20260925/ledger.json` — ledger reconciliation
