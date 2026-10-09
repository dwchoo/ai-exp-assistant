# 작업 인계: core-workbench p2.7 완료 — Core Workbench 1차 core 구현·최종 검증(CW-16 done_verified) 이후 후속 작업

인계 목적: 새 세션(Claude Code 또는 Codex)이 현재 상태를 빠르게 파악하고 후속 과제(FOLLOWUPS.md)나 Phase 2 계획을 사용자 지시에 따라 시작할 수 있게 한다. 다음 세션 기대 결과: 사용자가 고른 후속 과제의 결정·계획·구현을 같은 workflow(결정 기록 → amendment → ledger → 독립 test → review → 커밋)로 진행.

## 시작 요약

Manager OMP·worker OMP·host persistent terminal을 잇는 Workbench 1차 core가 p2.7 계획의 마지막 ticket까지 끝났다. CW-17(진입점·backend·ui_v1), CW-06(UI), CW-18(흐름·정책, C-D64~C-D70 사용성 결정 포함), CW-19(backend 재시작·재부팅 복구, C-D71), CW-16(최종 통합 검증, C-D72/C-D73)을 통합했다. 최종 후보 C18 = candidate a2e3daf23dc98d8c98c760d62e227d4425c0b6b8265b6b3d9a696bc7a573482b(commit c47456a), 문서 커밋 9a70f36. 근거: docs/features/core-workbench/VERIFICATION.md §1, .workflow/core-workbench/runs/implement-p2.6-20260927/integration-p27-cw16.json.

## 사용자 결정과 권한

사용자 인용(2026-10-08): '전부 진행해서 테스트까지 해서 완료하고 보고해'(CW-19·CW-16·정리 목록 전체), '가능하면 sonnet을 worker로 사용해줘', '모두 문서로 남기고 이제 커밋하고 푸쉬하자. 새로운 새션에서 할 수 있게. 코덱스에서 할 수도 있어.'(2026-10-09). 결정 C-D71(남은 프로세스는 manager 판단, 재부팅 확인 전 자동 동작 보류, 모델 오류 보류 자동 해제, raw log 한도), C-D72(Bash·sh만 지원, OMP 18.8.0 기준+영향 대조, worker 직접 요청 3경계, 승인 창 해당 없음), C-D73(256색 근사 수용). 승인 bundle: planning-p2.7-20260929/approval-amendment-v.json(digest 6a94257554222410497069c685c3cd73d39dcc9b32ff42b08007eddc526a5b94). Commit 권한은 '검증 후 커밋' 지시(2026-10-05)와 위 인용, push는 2026-10-09 지시로 이번 커밋까지. 그 이후의 push·게시·새 기능 착수는 다시 사용자 지시가 필요하다. 표준 제약: credential 내용 읽기 금지, ~/.omp·~/.agents·~/.claude·~/.codex·사용자 tmux/herdr/shell 설정 수정 금지, --profile 금지, pattern kill 금지(자신이 띄운 PID만), ~/wb-urux-sandbox와 사용자 프로세스 불간섭, MCP Docker 금지, 실제 모델 smoke는 사용자 승인과 요청 상한.

## 구현·검증 상태

완료: CW-19(L-CW19-RESTART 56/56, L-CW19-REBOOT VM 실제 재부팅 통과), CW-16(C18 정식 check 6종 exit 0 — compat는 첫 실행 Esc intermittent 실패 뒤 formal r2 통과, gate passed issues [], G4 aggregate 7 item, final-evidence validator 16 OK, 71개 전체 집합 stale/unknown 0, G1-COLOR 강조는 저장소 변경 없는 별도 probe로 관측). 실제 모델 smoke는 C16에서 32/40 요청으로 RM1·RM3·RM4 통과(확신용, gate 근거 아님), RM2 미실행. 부분/미확인: FOLLOWUPS.md §2(간헐 실패 4종)와 §3(실모델 재확인 3가지). PLAN.json의 개별 gate item evidence_status 필드는 갱신하지 않았다(validator manifest 보존 목적; 판정은 VERIFICATION.md와 integration-p27-cw16.json이 원본).

## 진행 중인 작업

관측 시각 2026-10-09T11:10+09:00: Claude subagent 0개(claude-swarm tmux 서버 없음), ledger active_calls {}(events 2448), 이 세션이 띄운 테스트·qemu 프로세스 0. pid 3972088 등 workbench.backend 프로세스는 사용자의 ~/wb-urux-sandbox Workbench로 이 세션 소유가 아니다(건드리지 말 것). 격리 VM은 꺼짐(qemu 없음). 커밋되지 않은 큰 snapshot 파일 몇 개(.workflow/... *-snapshot*.json, checkpoint)와 graphify-out/은 의도적으로 untracked다. 예산: total 696/820, worker_senior 214/232, test_designer 233/250, reviewer 162/176, worker 60/63, unit V-CW-16 25/30 — 새 unit은 limit 기록부터.

## 시도와 배운 점

배운 점: (1) 테스트 suite를 nohup/&로 띄우면 SIGINT가 무시돼 Ctrl-C 테스트가 대량 실패한다 — foreground나 run_in_background로 실행하고 SigIgn 확인. (2) git stash로 red 확인할 때 KeyboardInterrupt를 내는 테스트가 shell 체인을 끊어 stash pop이 안 될 수 있다 — 확인 후 stash list 점검. (3) smoke 화면 캡처는 plain pyte가 CSI S를 무시해 가짜 줄 겹침을 만든다 — tests/ui/support.rep_screen_classes 사용. (4) OMP 18.8.0은 __omp_worker_daemon_broker를 띄우며 OMP 종료 뒤 1~3초 남는다 — 종료 검증은 정확한 identity로 기다린다. (5) OMP 자식 umask 077은 사용자 프로젝트 파일 권한까지 바꿔 되돌렸다(C17 review P2-1). (6) ledger reserve는 다른 호출이 starting일 때 capacity_unknown이 나므로 spawn→running 기록 뒤 다음 reserve. unit limit 소진 시 먼저 limit 증액. (7) 독립 테스트 수정은 Root 승인 delta로만.

## 다음 행동

1) 새 세션 첫 확인: git status/log(HEAD가 이 인계 커밋인지, origin과 같은지), FOLLOWUPS.md와 VERIFICATION.md §1·§11 읽기, ledger-read(active 없음 확인), pgrep로 잔여 프로세스 확인(사용자 sandbox 프로세스는 제외). 2) 사용자에게 다음 목표를 묻는다: 후속 과제(FOLLOWUPS.md §1 트루컬러/raw log 순환, §2 간헐 실패 원인, §3 실모델 재확인) 또는 Phase 2(Task Inbox & Web Board) 계획. 3) 착수 시 결정을 DECISIONS.md에 C-D74부터 기록하고 planning-p2.7-20260929 amendment(w부터)와 ledger limit을 먼저 남긴다. 4) 역할 배치: worker는 가능한 한 Sonnet(wb-worker 등), 새 ticket의 첫 독립 test와 최종 frozen review는 Opus. 5) 완료 기준은 기존과 같다: 독립 test·review 통과, 전체 suite 실제 exit code, 필요 시 실모델 smoke(사용자 승인·상한), 커밋은 검증 뒤.

## 읽기 경로

- `docs/features/core-workbench/FOLLOWUPS.md` — 남은 후속 과제 한곳 정리(가장 먼저 읽기)
- `docs/features/core-workbench/VERIFICATION.md` — 최종 판정, 71개 전체 집합, 잔여·Root 판정
- `docs/features/core-workbench/COMPATIBILITY.md` — 지원 환경·버전·outer matrix·미검증 목록
- `docs/features/core-workbench/FOLLOWUP-TRUECOLOR.md` — 트루컬러 후속 설계 출발점
- `docs/features/core-workbench/DECISIONS.md` — C-D64~C-D73 사용자 결정(제품 동작의 근거)
- `docs/features/core-workbench/PLAN.json` — ticket·gate item 정의(G1-COLOR는 C-D73 개정 문구)
- `docs/features/core-workbench/BRIEF.md` — 요구사항과 C-AC-01~34
- `docs/features/core-workbench/SPEC.md` — 구조·통합 경계
- `AGENTS.md` — 프로젝트 규칙(한국어, 범위, Phase 2 경계)
- `.workflow/core-workbench/state.json` — workflow 상태(implementation_p27 units, user review checkpoints)
- `.workflow/core-workbench/runs/implement-p2.6-20260927/integration-p27-cw16.json` — CW-16 최종 통합 기록과 carried follow-ups
- `.workflow/core-workbench/runs/implement-p2.6-20260927/integration-p27-cw19.json` — CW-19 통합 기록
- `.workflow/core-workbench/runs/planning-p2.7-20260929/approval-amendment-v.json` — 현행 승인 bundle digest
- `.claude/agents/wb-worker.md` — Sonnet 역할 정의(wb-worker/wb-test-rerun/wb-delta-reviewer/wb-explorer)
- `tests/integration/cw16_harness.py` — live 시험 harness(sandbox, scripted provider) 재사용
- `.codex/handoffs/2026-09-29-core-workbench-p27-cw17/HANDOFF.md` — 직전 handoff(역사)
- `.workflow/core-workbench/runs/implement-p2.6-20260927/ledger.json` — ledger reconciliation
- `.workflow/core-workbench/runs/implement-p2.6-20260927/gate-p27-cw16-c18-final-result.json` — checks reconciliation
- `.workflow/core-workbench/runs/implement-p2.6-20260927/check-p27-cw16-regression-a-c18-execution.json` — checks reconciliation
- `.workflow/core-workbench/runs/implement-p2.6-20260927/check-p27-cw16-regression-b-c18-execution.json` — checks reconciliation
- `.codex/handoffs/2026-09-29-core-workbench-p27-cw17/HANDOFF.md` — 이전 인계 이력; 현재 정본을 우선
