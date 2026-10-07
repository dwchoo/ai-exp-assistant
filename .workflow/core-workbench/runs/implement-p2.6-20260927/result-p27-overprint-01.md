# p27-overprint-01: host pane overprint / 프롬프트가 pane 밖으로 밀림

- 역할: worker_senior. 2026-10-07. 기준 dab5761. 실모델·provider 요청 없음(fixture ui_v1 서버, 실제 bash `ShellPane`/`PersistentShell`, fake OMP 없음).
- **결론: 원인이 둘입니다.**
  1. **smoke 3회 증상(cd69-03 O1, cd70-01 O3, cd70-04 O1)은 캡처 도구의 에뮬레이터 아티팩트입니다.** 제품이 내보낸 같은 바이트를 실제 tmux와 SU/REP를 처리하는 pyte로 보면 정상입니다. smoke 드라이버(`tests/backend/live_cw18_independent_p27w.py` `Ui`)는 plain `pyte.Screen`/`ByteStream`을 쓰는데, pyte 0.8.2는 `CSI Ps S`(SU)를 무시합니다. 가득 찬 host pane이 스크롤될 때 ncurses는 `ESC[23;38r ESC[2S`(스크롤 영역 + SU)를 보냅니다. plain pyte에서는 스크롤이 빠져 후속 출력이 이전 마지막 줄 위에 그려지고(`X_42ware inspection completed.`) 입력 줄 아래 프롬프트가 사라집니다. 제품 코드는 고치지 않았습니다.
  2. **실제 결함(내부 VT 화면, 모든 에뮬레이터 공통)은 host pane 높이가 줄어들 때 생깁니다.** pyte 0.8.2 `Screen.resize`는 줄을 위에서 잘라내면서 커서는 예전 행으로 되돌립니다. 그래서 가득 찬 pane(커서가 마지막 행)이 줄어들면 커서가 화면 아래 보이지 않는 행에 남습니다. 그 뒤 입력한 줄(`$ echo X_$((6*7))`)은 보이지 않는 행에 그려지고, 다음 출력은 보이는 프롬프트 줄 위에 덮어씁니다. 창 크기 축소, divider 이동, zoom 해제 때 사용자 화면에 나타날 수 있는 "출력 줄 겹침"이며, 사용자가 본 현상의 유력한 후보입니다(사용자 화면은 직접 확인하지 못함).

## 재현 (결정적, 모델 없음)
| 경로 | 조건 | 내부 VT 화면(PaneScreen) | plain pyte(smoke 드라이버와 동일) | SU/REP pyte(`support.rep_screen_classes`) | 실제 tmux(전용 소켓 `/tmp/ovp-tmux-<pid>.sock`, `-f /dev/null`) |
|---|---|---|---|---|---|
| 사용자 입력(실제 bash `ShellPane`) | pane 가득 참 → `echo X_$((6*7))` | 정상 | – | – | – |
| worker 실행(실제 `TerminalService` + `HostShellPort`, `[worker] $ …` 래퍼) | 〃 | 정상 | – | – | – |
| 실제 product UI(curses, PTY 40x170, fixture 서버) | 〃 | 정상 | **증상 그대로**: `X_42ware inspection completed.` / `$ echo X_$((6*7))`, 프롬프트 없음 | 정상 | 정상(같은 바이트 재생, UI를 tmux 안에서 직접 실행 시 TERM=xterm-256color·tmux-256color·screen-256color 모두 정상) |
| 실제 bash 바이트(worker 실행 + 후속 입력) → UI in tmux | 축소 없음 / 50x100 | – | – | – | 정상 |
| **높이 축소** 뒤 후속 명령(실제 bash `ShellPane`, 14→8줄, PTY도 같이 resize·SIGWINCH) | 수정 전 | **커서 (13,2) / 8줄 화면**: 입력 줄 사라짐, `X_42`가 `$` 줄을 덮음 | – | **결함**: `Hardware…` / `X_42` / `$`(입력 줄 없음) | **결함**: 같은 모양(UI in tmux, 170x40 → 150x30) |
| 〃 | 수정 후 | 커서 (7,2), `Hardware…` / `$ echo X_$((6*7))` / `X_42` / `$` | – | 정상 | 정상 |

## 원인 (file:line)
- 결함: `src/workbench/terminal/vt_g1/screen.py:168`(수정 전 `TerminalScreen.resize` → `super().resize`)와 `:94`(`_leave_alternate`가 primary 복원 뒤 같은 `super().resize` 호출). 두 곳 모두 pyte 0.8.2 `Screen.resize`(`save_cursor; cursor_position(0,0); delete_lines(old-new); restore_cursor`)를 그대로 써서, 커서 y를 줄인 줄 수만큼 옮기지 않았고 잘린 위쪽 줄도 history에 넣지 않았습니다.
- smoke 아티팩트: `tests/backend/live_cw18_independent_p27w.py:282-283`(plain `pyte.Screen`/`ByteStream`; 수정 금지 파일이라 손대지 않음).

## 수정
- `TerminalScreen._resize_screen`(screen.py)을 추가했습니다. 높이를 줄일 때 tmux `screen_resize_y`와 같은 순서로 처리합니다. 커서 아래 행부터 버리고, 그래도 넘치는 위쪽 행은 history로 올리고(primary 화면만, alternate는 history 없음) 커서를 같은 줄과 함께 위로 옮깁니다. 이후 margins를 초기화합니다. 높이를 늘리거나 열을 바꾸는 동작은 pyte 그대로입니다. `resize`와 `_leave_alternate` 모두 이 함수를 씁니다. stream/DCS/OSC 52/SU·SD 코드는 바꾸지 않았습니다.
- `src/workbench/ui/product/model.py` `_relayout`: 축소로 줄이 history에 들어가도 스크롤 중인 view는 resize 전 offset을 유지합니다. 기존 계약 `test_scroll_positions_survive_resizes_and_are_clamped`(수정 직후 7≠9로 실패)를 그대로 지키고, 이제 그 줄은 더 위로 스크롤하면 볼 수 있습니다.
- 테스트(red→green):
  - `tests/terminal/test_vt_resize_shrink.py`(8): 가득 찬 pane 축소 + 후속 명령이 덮어쓰지 않음, 잘린 줄이 순서대로 history에 들어감, 커서 아래 행이 먼저 버려짐, 여러 번 축소 = 한 번 축소, 확대·열 변경 동작 유지, DECSTBM 초기화, alternate 화면 안에서 축소·축소 뒤 alternate 종료. 수정 전 7/8 FAIL, 수정 후 OK.
  - `tests/ui/test_product_host_overprint.py`(2, 실제 curses UI, PTY, SU/REP pyte 프레임): 가득 찬 pane의 후속 명령 순서(수정 전에도 통과, smoke 증상의 프레임 회귀 가드), 창 축소 뒤 입력 줄·출력 순서(수정 전 FAIL `['…','Hardware inspection completed.','X_42','$']`, 수정 후 OK).

## Checks
| 명령 (`PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`) | 결과 |
|---|---|
| 새 테스트, 수정 전 | terminal 7 FAIL / ui 1 FAIL(축소) |
| 새 테스트, 수정 후 | OK |
| tests/ui | 1차 exit 1(위 scroll offset 1건, 내 변경 때문) → model.py 보정 후 **exit 0, 826 OK** |
| tests/terminal | 1·2차 exit 1: `test_manual_input_boundary_independent…editing_and_control_keys…`(bash 연속 Ctrl-C 뒤 clean prompt 타이밍. VT 화면을 import하지 않고 단독 3/3 OK, HEAD 기준선 OK) → 단독 실행 3차 **exit 0, 216 OK (skipped 2)** |
| tests/gates/g2_shell | **exit 0, 142 OK (skipped 1)** |
| tests/backend | 1차 exit 1: `test_recovery_e2e_independent_p27cd70…restart_mid_command…` `find_in_session(...)[0]` IndexError(프로세스 탐색 경합, 단독 3/3 OK) → 2차 **exit 0, 1076 OK (skipped 30, expected failure 1)** |

## 권고 (Root 판단용)
1. 이후 smoke 드라이버는 outer 에뮬레이터로 `tests/ui/support.rep_screen_classes()`(REP + SU/SD)를 써야 합니다. plain pyte로 본 "가득 찬 pane 스크롤 뒤 덮어쓰기·프롬프트 누락"은 제품 결함이 아닙니다. `live_cw18_independent_p27w.py`는 이번 쓰기 범위 밖이라 그대로 두었습니다.
2. 높이를 늘릴 때 pyte는 아래에 빈 줄을 붙입니다(tmux는 history를 다시 끌어내림). 겹침은 없어 이번에는 바꾸지 않았습니다.

## Residue
- 전용 tmux 서버는 실행마다 `kill-server`로 끄고 소켓 파일을 지웠습니다(`/tmp/ovp-tmux-*` 없음). 내가 띄운 bash/UI 자식 프로세스는 테스트·스크립트 종료 때 닫았습니다. 사용자 tmux, 다른 소켓, `~/wb-urux-sandbox`, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex`는 건드리지 않았습니다. credential과 다른 프로세스 environ은 읽지 않았습니다.
- 임시 파일은 scratchpad와 `/tmp/ovp-home`·`/tmp/ovp-base`(HEAD 기준선, 삭제함)에만 두었습니다. 실수로 repo 루트에 생긴 `raw2.bin`(scratch 캡처)은 바로 scratchpad로 옮겼습니다. commit과 graphify update는 하지 않았습니다.
- 변경 파일: `src/workbench/terminal/vt_g1/screen.py`, `src/workbench/ui/product/model.py`, `tests/terminal/test_vt_resize_shrink.py`(신규), `tests/ui/test_product_host_overprint.py`(신규), 이 결과 파일.
