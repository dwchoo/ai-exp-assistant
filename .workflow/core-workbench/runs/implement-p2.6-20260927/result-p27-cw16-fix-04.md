# p27-cw16-fix-04: CW-16 gap D1(색 해석)·D3(OMP 파일 mode)

- 역할: worker_senior (Opus, cw16-fix3 재사용). 실모델·provider 요청 0. commit·graphify update·docs 수정 없음.

## D1 — `_color_index` (C-D73)
- `src/workbench/ui/terminal_g1/app.py`: 6자리 hex 판정을 `isdigit`보다 먼저 합니다. 그래서 `303030`, `123456`, `808080` 같은 색은 RGB로 읽습니다(이전에는 index 255, 거의 흰색이었음). index 문자열 `0`–`255`와 색 이름은 그대로입니다.
- 24-bit RGB는 `_rgb_index`로 256색 팔레트 중 가장 가까운 색에 근사합니다.
  - 후보는 6×6×6 cube(xterm level 0/95/135/175/215/255)와 grey ramp(8+10k)이고, 더 가까운 쪽을 고릅니다. 같으면 cube입니다.
  - 이전의 `round(c/255*5)` cube 근사는 xterm level과 맞지 않았습니다. 예: `5f`가 135로 근사됨.
- system 색(0–15)은 pyte가 index를 바꿔 쓴 xterm RGB와 **정확히 같을 때만** 그 index를 씁니다. 이 경우 index가 보존됩니다.
  - **한계:** 0·9·10·11·13·14·15는 RGB가 cube 색과 같아(예: 9와 196이 모두 `ff0000`) pane 화면(pyte)에서 이미 구분이 사라집니다. 이 색들은 같은 RGB의 cube index로 그립니다. 바깥 화면의 RGB는 같지만, 사용자 terminal theme가 system 색을 바꾼 경우에는 차이가 날 수 있습니다. COMPATIBILITY 기록 대상입니다(Root, docs 담당).
- `tests/integration/live_cw16_gap.py`(Root 승인 범위): COLOR `truecolor`의 기대값만 C-D73 기준으로 바꿨습니다. 테스트 안에서 독립적으로 계산한 `nearest_256()`(cube·grey 중 가까운 xterm 팔레트 색)이 기대값입니다. 설명 docstring에 C-D73 문장 1줄을 추가했습니다. 다른 기대·시나리오는 그대로입니다.

## D3 — OMP 파일 mode
- Workbench가 띄우는 모든 OMP process를 umask 077로 실행합니다(`panes.OMP_UMASK`).
  - `OmpPane` 자식은 fork 뒤 exec 전에 `os.umask(0o077)`를 호출합니다.
  - `launcher._bounded_outputs`(OMP config 읽기)와 `check_isolation`(RPC 격리 점검)은 `Popen(umask=0o077)`로 실행합니다.
- host shell(과 실험 자식)은 사용자 umask를 그대로 씁니다. 테스트로 확인했습니다.
- backend 자신의 data dir 쓰기는 이미 명시적으로 private입니다(`write_private_json`, `ensure_private_dir`, 0600 open). gap 재현에서 노출 항목은 모두 OMP가 만든 것이었으므로, backend 프로세스의 umask는 바꾸지 않았습니다. 바꾸면 host shell이 077을 물려받으므로, 그것을 되돌리는 경로가 G2 prototype까지 넓어져서입니다.
- `omp --version` 확인 실행은 사용자 HOME의 `~/.omp/natives`에 쓸 수 있어 바꾸지 않았습니다(data dir 밖).
- Root 승인 test delta: `tests/backend/test_live_start_independent.py` `test_concurrent_starts…`에서 lstat이 symlink인 항목(C-D64 `agent.db`)을 group/other 비트 단정에서 제외했습니다(2줄, 주석에 근거).

## 테스트
- 신규 `tests/backend/test_cw16_fix04.py` 8건. HEAD src에서는 7건 fail(색 5, umask 2)이고, host shell umask 대조군은 양쪽 모두 OK. 수정 후 8 OK.
## SUITES_AND_GAP
정식 근거는 `result-p27-cw16-rerun-01.md`다(2026-10-08 22:49–2026-10-09 00:05, 작업 트리 미커밋 fix-03/04 + gap-01, `git diff -- src omp_bridge` sha256 `318b7f7e…d4d1b1` 실행 전후 동일).
- 비live·live suite 전부 exit 0: ui 843, ui/status_workbench 2, recovery_boot 180, integration 24(skip 2), lifecycle 38, observation 95, storage 33, tasks 22, workflow 62, contracts 52, bridge py 52(skip 1) + `node --test` 48/48, gates g1_vt 85, g2_shell 142(skip 1), g3_omp 117, g4_evidence 10, g4_lifetime 8, policy pause_automation 129, recovery_manager 26, terminal 221, `test_cw16_fix04.py` 8.
- `tests/backend` 1123건(skip 3, expected failure 1): 두 번 실행해 둘 다 exit 1. 실패는 아래 두 건뿐이고 다른 건은 서로 달랐다(자세한 분류는 rerun-01).
  - `test_connection_cap_and_exclusive_attach_do_not_disturb_the_backend`: 단독 재실행 9회 중 2회 `BlockingIOError(EAGAIN)`(test 쪽 비차단 connect 20개 vs `listen(8)` 경쟁). fix-03/04가 건드린 코드가 아니다.
  - `test_concurrent_kill_and_restart_requests_are_serialised`: 전체 실행 중 1회 실패, 단독 3/3 OK.
- gap `live_cw16_gap.py` 4/4 OK(124 s, 모델 요청 0): COLOR(basic16·cube·grey·system·24bit 전부 pass), SCROLL, COMPOSER, SESSION 모두 pass.
