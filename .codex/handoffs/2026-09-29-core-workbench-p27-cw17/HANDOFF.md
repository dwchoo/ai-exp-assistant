# 작업 인계: core-workbench p2.7a 구현 — CW-17 verification unit 마무리(리뷰 P2-2 잔여 수정) 후 통합, 이어서 CW-06

인계 목적: 새 세션(토큰 절약 및 .claude/agents Sonnet high 정의 로드)에서 동일 승인 범위로 $implement를 이어간다. 다음 세션 기대 결과: P2-2 수정·독립 재검증·delta review 통과 → CW-17 unit 통합(graphify update, 중간 checkpoint blob prune) → CW-06 dispatch → CW-06 통합 뒤 UR-UX 사용자 검토 준비.

## 시작 요약

p2.6 최종 gate가 제품 진입점·backend 유지 detach/reconnect·실제 재부팅 부재로 passed:false였고, 사용자가 후속 계획(p2.7: CW-17 backend·ui_v1, CW-06 재개, CW-18 흐름·정책, CW-19 재시작·재부팅, CW-16 재실행)과 UX/사용성 검토 checkpoint(p2.7a)를 승인했다. CW-17을 구현했고(진입점 python -m workbench / omp-workbench, setsid backend, ui_v1 UDS 계약) 독립 테스트에서 DETACH/START/CONTRACT가 통과했다. 검증 중 기존 결함 두 건을 발견·수정·독립 검증했다: F-CW17-INPUT-01(ESC/편집/Ctrl-C 후 host 수동 입력 영구 차단, 사용자 foreground 프로그램 입력 불가)과 F-P27-DEADLINE(자동 실험이 5 s(최대 300 s) 경과 시 SIGKILL; BRIEF '실험 실행 시간 상한 없음' 위반). 새 reviewer(p27-review-cw17-01)는 integrate 판정에 P2 4건을 냈고 수정 후 독립 재검증에서 P2-1/3/4는 PASS, P2-2(재사용된 session 번호의 leader가 이미 없을 때 외부 member SIGKILL)만 FAIL로 남았다. 아직 통합(graphify update 포함)하지 않았다.

## 사용자 결정과 권한

사용자 승인: p2.7 bundle(planning-p2.7-20260929/approval.json, 인용 '후속 ticket(plan 변경)은 승인해.', '진행해') → p2.7a 개정(approval-amendment-a.json, digest a521acdc3838b43a26e277afc233bab695c8b02ce0d42392c3678931b2e3f027, 인용 'ux 검토랑 사용성 검토도 넣어줘.'). '완료 할 때 까지 계속 진행해' 지시로 ticket 사이 확인 없이 진행하되 제품 동작 변경·범위 초과·host 영향은 멈춘다. 결정: C-D56(격리 QEMU VM 재부팅 검증, VM 안 OMP 인증 정보 복사 허용), C-D57(UR-UX: CW-06 뒤·CW-18 전, UR-USABILITY: CW-18 뒤·CW-16 전). 예산 증액은 C-D55로 Root 위임. 2026-09-29 사용자 승인: Sonnet 5.5 high 혼용(.claude/agents/wb-*.md), checkpoint는 milestone만 전체 보존·나머지 blob prune. commit/push/게시는 권한 없음.

## 구현·검증 상태

완료: 승인·계획 기록, CW-17 구현·독립 테스트, 입력 결함 수정 2회+독립 검증, 실행 시간 결함 수정+독립 검증, frozen full review, P2 수정 1회. 부분: P2 수정 독립 재검증 needs_reroute(P2-2 FAIL). 미완: CW-17 unit 통합, graphify update, CW-06/18/19/16, UR-UX/UR-USABILITY. 현재 작업 트리 후보 = p27-snapshot-review-fix-test-after.json(리뷰 수정 후보 37233705 + 독립 테스트 3개 추가). Root 판정 기록 3건: root-adjudication-p27-hook-rejected.json, root-adjudication-p27-return-deadline-test.json, result-p27-review-cw17-01.json의 root_disposition. G2 gate 근거는 실행 시간 의미 변경으로 stale → CW-16 I-SHELL/I-FAULT에서 재수집. CW-12/15의 CW-06 의존 item은 CW-06 통합 뒤 delta 재검증 대상(PLAN evidence_status stale_pending_CW-06_delta).

## 진행 중인 작업

진행 중인 writer 없음(ledger active_calls 비어 있음, rev 1370). 이 세션의 subagent(cw17-worker, cw17-test, input-fix, input-test, deadline-test, cw17-review, p2fix-test)는 모두 idle이며 세션 종료 시 소멸해도 결과는 파일에 있음. 테스트 프로세스 잔존 0(각 결과 packet). 알려진 무해 잔여: 빈 /tmp/cw03-g2-* 디렉터리 9개(owner SIGKILL 테스트 패턴, 귀속 불명, 미삭제). 격리 VM ~/.local/share/wb-vm/wb-reboot는 이전에 켜 둔 상태일 수 있음(재개 시 pgrep qemu 확인, 필요 없으면 stop.sh). 예산: total 473/478(가용 5), worker_senior 157 한도 중 사용 156, test_designer 160 중 160, reviewer 114 중 113 — 다음 dispatch 전 limit 증액 필요.

## 시도와 배운 점

실패/기각: (1) CW-15 assignment가 CW-06을 formally accepted로 기록했으나 gate 기록 없음 → PLAN에 stale 표시. (2) test_designer의 g2/g4 discover에 '-t .' 사용 시 namespace package import 실패 — PYTHONPATH=src:. 로 실행. (3) backend connection-cap 테스트 EAGAIN은 부하 flake(단독 3회 OK). (4) G2 test_ctrl_c_hold... 1 s 대기 flake(단독 OK). (5) P2-2 1차 수정: _owned_session_members가 /proc/<sid>에 다른 leader가 살아 있을 때만 재사용으로 판단 → leader 없는 외부 session member를 자기 것으로 오인. 재시도 조건: OMP를 close 전까지 waitid(WNOWAIT)로 미회수 유지해 pid/sid 번호를 고정(또는 spawn 시 pidfd 보유로 번호 재사용 차단) 후 member identity 검증.

## 다음 행동

1) 새 세션 첫 확인: git status, ledger-read(rev 1370, active 없음), p27-snapshot-review-fix-test-after.json 대비 현재 트리 snapshot/audit, pgrep로 잔여 테스트/qemu 확인. 2) ledger에 limit 증액(total, worker_senior, test_designer, reviewer) 기록. 3) P2-2 수정: worker_senior(Opus, general-purpose) — panes.py OmpPane 수명/close, 독립 테스트 tests/backend/test_pane_identity_independent_p27c.py 무수정 통과, 필수 suite(terminal, g2_shell PYTHONPATH=src:., canonical, backend, contracts, lifecycle, g4, workflow, observation). 4) 독립 재검증: wb-test-rerun(Sonnet high). 5) delta review: wb-delta-reviewer(Sonnet high) — 원 리뷰 findings와 수정 diff. 6) 통과 시 CW-17 unit 통합 기록, graphify update ., 중간 checkpoint blob prune(milestone 유지). 7) CW-06 dispatch(ui_v1 client 세 영역 UI; 첫 독립 검증은 Opus test_designer, 최종 review Opus). CW-06 통합 뒤 CW-12/15 delta 재검증, 이어 UR-UX 준비(실행 방법·확인 목록·화면 스냅샷)로 사용자 검토 요청 후 대기. CW-18로 넘길 항목: INPUT_BARRIER 항상 release(P3-2), detach 중 활성 supervisor identity 검증. 완료 기준: 각 unit gate item 통과+독립 test+fresh review+통합; Core done_verified는 CW-16 전체 gate pass 뒤에만.

## 읽기 경로

- `docs/features/core-workbench/PLAN.json` — p2.7a ticket graph, gate items(L-CW17-*, L-CW18-*, L-CW19-*), user_review_checkpoints, p2_7_scope_review
- `docs/features/core-workbench/SPEC.md` — s2.7 'Production 조합' 절: CW-17/06/18/19 책임 분담
- `docs/features/core-workbench/tickets/CW-17.md` — 현재 unit 계약
- `docs/features/core-workbench/tickets/CW-06.md` — 다음 ticket; ui_v1 client, UR-UX
- `docs/features/core-workbench/DECISIONS.md` — C-D56, C-D57 사용자 결정
- `.workflow/core-workbench/runs/planning-p2.7-20260929/approval-amendment-a.json` — 현행 승인 bundle digest와 인용
- `.workflow/core-workbench/runs/implement-p2.6-20260927/ledger.json` — 호출 회계(rev 1370)
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-review-cw17-01.json` — frozen full review findings와 Root 처분
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-review-fix-test-01.json` — P2 수정 독립 재검증; P2-2 FAIL 재현 정보
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-review-fix-worker-01.json` — P2 수정 내용
- `.workflow/core-workbench/runs/implement-p2.6-20260927/root-adjudication-p27-hook-rejected.json` — HOOK_REJECTED 수동 입력 판정
- `.workflow/core-workbench/runs/implement-p2.6-20260927/root-adjudication-p27-return-deadline-test.json` — G2 return-deadline 기대 반전 판정, G2 근거 stale
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-deadline-test-01.json` — 실행 시간 무제한 독립 검증
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-input-test-01.json` — 입력 경계 독립 검증, F-P27-DEADLINE 발견
- `.workflow/core-workbench/runs/implement-p2.6-20260927/result-p27-cw17-test-01.json` — CW-17 gate item 독립 판정
- `.workflow/core-workbench/runs/implement-p2.6-20260927/p27-review-cw17-scope.json` — CW-17 unit 추가/수정 파일 목록과 base 사본 위치
- `.workflow/core-workbench/runs/implement-p2.6-20260927/p27-checkpoint-prune-record.json` — blob prune 기록과 보존 milestone
- `.claude/agents/wb-worker.md` — Sonnet high 역할 정의(wb-worker/wb-test-rerun/wb-delta-reviewer/wb-explorer)
- `.codex/handoffs/2026-09-29-102230-core-workbench-p26-cw16-final-gate.md` — 직전 handoff(p2.6 최종 gate 미완료)
- `.workflow/core-workbench/runs/implement-p2.6-20260927/ledger.json` — ledger reconciliation
