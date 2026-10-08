# p27-cw16-rerun-01 — fix-03/fix-04 + gap-01 회귀 재실행 (test_designer)

2026-10-08 22:49 ~ 2026-10-09 00:05 KST. 작업 트리(미커밋 fix-03/04, gap-01). 모델·provider 요청 0, 자격 증명 저장소와 타 프로세스 environ 미접근. 쓴 파일은 이 파일과 fix-04의 SUITES_AND_GAP 절뿐이다. 로그는 `/tmp/wb-rerun01/`(logs, logs2, logs3, logs4, gap2).

## 환경
- `env -i` (TMUX*/HERDR_* 없음), fake HOME(`/tmp/wb-rerun01/home-*`, 실행 뒤 삭제), `HTTP(S)/ALL_PROXY=127.0.0.1:9`, PATH `/tmp/cw02-g1-venv/bin:~/.local/bin:/usr/bin:/bin`, `PYTHONPATH=src`(gates는 `src:.`), `-t tests/<suite>`.
- `git diff -- src omp_bridge` sha256 시작 = 끝 = `318b7f7e9a23048778526b2b72fc83e5b5b4e1463993201b11f2f0106bd4d1b1`.
- **환경 결함(내 실행 1차)**: 1차 launch를 `nohup … &`로 띄워 상속 SIGINT/SIGQUIT/SIGHUP이 ignore(`SigIgn 0x7`)였다. 그래서 Ctrl-C를 쓰는 테스트가 대량 실패했다. 해당 suite(backend, terminal, g2_shell)와 node, gap을 python wrapper로 시그널을 SIG_DFL로 되돌려 다시 돌렸다(2차). 1차의 backend 실패 3건, terminal 10건, g2_shell 22건은 환경 원인이며 2차에서 모두 사라졌다. 나머지 suite는 1차 결과를 쓴다(통과).

## checks
| 대상 | exit | 수 | 시간 |
|---|---|---|---|
| tests/backend (1차, SIGINT ignore) | 1 | 1123, F=3(환경), skip 3, xfail 1 | 1023 s |
| tests/backend (2차, 정식) | **1** | 1123, F=1 E=1, skip 3, xfail 1 | 967 s |
| tests/ui / ui/status_workbench | 0 / 0 | 843 / 2 | 207 s / 0 s |
| tests/terminal (2차) | 0 | 221 | 607 s |
| recovery_boot / integration / lifecycle | 0 / 0 / 0 | 180 / 24(skip 2) / 38 | 71 / 11 / 4 s |
| observation / storage / tasks | 0 / 0 / 0 | 95 / 33 / 22 | ≤2 s |
| workflow / contracts / bridge(py) | 0 / 0 / 0 | 62 / 52 / 52(skip 1) | 140 / 0 / 53 s |
| gates g1_vt / g2_shell(2차) / g3_omp | 0 / 0 / 0 | 85 / 142(skip 1) / 117 | 1 / 120 / 62 s |
| gates g4_evidence / g4_lifetime | 0 / 0 | 10 / 8 | <1 s |
| policy pause_automation / recovery_manager | 0 / 0 | 129 / 26 | 26 / 0 s |
| `node --test tests/bridge/*.test.ts` (2차) | 0 | 48 pass, 0 fail | 22 s |
| `tests/backend/test_cw16_fix04.py` | 0 | 8 OK | 0.2 s |
| `live_cw16_gap.py` (§6 명령, 2차) | 0 | 4 OK | 124 s |

live 테스트 skip은 0건(backend 29건 포함 실행됨). skip 3은 `covered by AnalysisFailureTests`, integration 2와 bridge 1은 기존 skip이다.

## 실패 2건과 단독 재실행 (backend 2차)
1. `test_live_contract_independent…test_connection_cap_and_exclusive_attach_do_not_disturb_the_backend`: `BlockingIOError(EAGAIN)`이 `Raw.connect`에서 났다.
   - 단독 재실행: 3회 중 1 OK 2 FAIL, 이어서 6회 6/6 OK. 총 단독 9회 중 7 OK, 2 FAIL. 실패 때만 4.2–4.4 s로 빨리 끝났다.
   - 분류: **test 타이밍 경쟁(intermittent), 구현 회귀 아님.** 테스트가 비차단 connect 20개를 backlog `listen(8)`에 몰아 보내므로, backend accept 속도에 따라 EAGAIN이 난다. `ui_server.py`의 listen/accept 코드는 diff에 없다(flush 추가만). 이 테스트는 1차 backend 실행과 gap-01에서 OK였고, 변경된 것은 skip 사유 문구뿐이다. 근본 수정 여부(재시도 또는 backlog 확대)는 Root 판정이다.
2. `test_shell_kill_restart…test_concurrent_kill_and_restart_requests_are_serialised`: `held != ok`(`KILL_IN_PROGRESS`, `PANE_ALIVE`).
   - 단독 재실행 3/3 OK(0.35–0.42 s). 1차 backend 실행에서도 OK였다.
   - 분류: intermittent 경쟁(전체 실행 부하 때 1회). 구현 회귀의 증거는 없다. 원인은 확정하지 않았다.

## gap 시나리오 (`gap-summary.json`, run rerun01b)
- COLOR pass: basic16, 256 cube, grey ramp, system indices, 24-bit RGB 모두 pass (D1/D2 해소 확인).
- SCROLL pass(host mode·history·live exit, Shift+PgUp, 마우스 휠, OMP pane).
- COMPOSER pass(draft 보류, 비운 뒤 1회 전달).
- SESSION pass(`/new` 재등록, 재전송 0, recovery notice 1회, 이후 report 1회).
- unknowns 없음. 모델 요청은 시나리오 내 counting provider 값만 있고 실제 provider는 proxy로 차단.

## status: `needs_reroute`는 아님, 단 Root 판정 필요
회귀 exit 0이 아닌 suite는 backend 1개이며, 실패 2건은 각각 intermittent로 분류했다(위 증거). fix-03/04와 gap-01 변경에 귀속되는 실패는 관측하지 못했다. backend를 exit 0으로 확정하려면 Root가 두 건을 intermittent로 받아들이거나 해당 test의 안정화를 지시해야 한다.

## 잔여물
- 내가 띄운 프로세스는 모두 종료·정리했다(2차 개시 시 잘못 남은 1차-bash 고아 unittest 1개는 exact pid로 TERM 후 session 잔존 0 확인). fake HOME 디렉터리 삭제. `/tmp/wb-rerun01/`(로그)와 `/tmp/wbgap-runner`(gap-01 산출, 내 것 아님)는 남아 있다.
- 남아 있는 `omp`/`workbench.backend.cli` pid 3972088/3972097/3972276과 herdr, tmux는 내가 띄운 것이 아니라 손대지 않았다.
