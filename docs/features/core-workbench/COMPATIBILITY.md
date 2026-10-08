# Core Workbench 호환성 검증

이 문서는 CW-16 최종 후보 C18(`a2e3daf2…3482b`, HEAD `c47456a`)에서 실제로 실행한 조합만 적는다. 실행하지 않은 조합은 지원으로 쓰지 않는다. 근거 기록은 [VERIFICATION.md](VERIFICATION.md)와 `.workflow/core-workbench/runs/implement-p2.6-20260927/`(이하 `run/`)에 있다. 이전 후보 C16에서 달라진 점은 해당 위치에 적었다.

외부 설정은 바꾸지 않았다. tmux는 실행마다 격리 socket(`-S`, `-f /dev/null`)으로 띄웠다. Herdr는 sandbox 아래의 `HERDR_CONFIG_PATH`/`XDG_*`/HOME으로 띄웠고, preflight에서 socket과 session_dir가 sandbox 안에 있음을 확인했다. 사용자 기본 tmux와 herdr server에는 질의하지 않았다. 모든 check는 `env -i`로 실행했다(`TMUX*`/`HERDR_*` 제거).

## 1. 검증 환경

| 항목 | 값 |
|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS / Linux 7.0.0-30-generic |
| Python | 3.12.3(venv, pyte) |
| Node | v22.23.2 |
| OMP(host) | `omp/18.8.0`(`~/.local/bin/omp`). 제품 경로의 모든 runtime 근거. 버전은 기록하며 고정하지 않는다 |
| OMP(VM guest) | X5의 재부팅 phase는 fake OMP로 실행했다. guest의 OMP 18.2.10은 C16 기록이며 C18 보고서에서 다시 확인하지 않았다 |
| Bash | GNU bash 5.2.21(1)-release |
| sh | `/bin/sh` → dash 0.5.12-6ubuntu5 |
| tmux | 3.4(격리 server 기본값 `default-terminal tmux-256color`, `set-clipboard external`, `escape-time 500`. 기본값은 C16 기록) |
| Herdr | 0.9.3 |
| terminal | `TERM=xterm-256color`, `COLORTERM=truecolor`. 바깥 PTY는 40×170이고, C3에서 34×150으로 바꿨다가 되돌렸다. 화면은 pyte 에뮬레이션으로 확인했다 |
| VM | QEMU `wb-reboot`, guest Ubuntu 24.04.5, kernel 6.8.0-142(guest만 재부팅) |

## 2. Outer terminal matrix

check `p27-cw16-compat-c18-r2`(exit 0)의 결과다. 같은 argv의 첫 실행 `p27-cw16-compat-c18`은 exit 1이었다(아래 "첫 실행 실패"). 보고서는 `run/cw16-formal-c18/reports/cw16-matrix-984764a2/compat-<outer>-<shell>.json`(r2)과 `cw16-matrix-5ba4d64c/`(첫 실행)다. 실제 OMP 18.8.0 두 개를 scripted local provider로 실행했고 실제 모델 요청은 0이다.

| outer / host shell | C1 입력·focus/owner | C2 OMP 키·한글/wide·paste | C2 2 MiB 초과 거절 | C3 resize | C4 OSC 52 | C5 UI detach ≥60 s → attach | C6 outer client 재접속 | C7 종료·정리 |
|---|---|---|---|---|---|---|---|---|
| plain PTY / Bash | 통과 | 통과 | 통과(이유 표시) | 통과 | 통과 | 통과(65.7 s) | 해당 없음¹ | 통과 |
| plain PTY / dash | 통과 | 통과 | 통과(이유 표시) | 통과 | 통과 | 통과(65.7 s) | 해당 없음¹ | 통과 |
| tmux 3.4 / Bash | 통과 | 통과 | 통과(이유 표시) | 통과 | 통과² | 통과(65.8 s) | 통과 | 통과 |
| tmux 3.4 / dash | 통과 | 통과 | 통과(이유 표시) | 통과 | 통과² | 통과(65.9 s) | 통과 | 통과 |
| Herdr 0.9.3 / Bash | 통과 | 통과 | 해당 없음³ | 통과 | 통과 | 통과(65.8 s) | 통과 | 통과 |
| Herdr 0.9.3 / dash | 통과 | 통과 | 해당 없음³ | 통과 | 통과 | 통과(65.9 s) | 통과 | 통과 |

열별 관측 내용
- **C1**: 시작 줄 `shell bash (/usr/bin/bash)` 또는 `shell sh (/usr/bin/dash)`. focus를 4회 옮기는 동안 owner(user)와 epoch가 바뀌지 않았다.
- **C2**
  - 세 pane에 각각 입력이 들어갔다.
  - 두 OMP에서 slash 메뉴와 Esc 닫힘, Ctrl-C, Alt-Enter 줄바꿈, 한글 composer가 동작했다.
  - 한글·wide 문자와 multiline bracketed paste가 원본 그대로 들어갔다.
- **C3**: 40×170 → 34×150 → 40×170으로 바꿨을 때 OMP 두 개의 TTY 크기와 host `stty size`가 pane 크기와 일치했다.
- **C4**: OMP `/copy`가 가장 바깥 터미널에 OSC 52로 도착했다. host pane의 OSC 52는 전달되지 않았다.
- **C5**: backend, OMP 2개, shell, supervisor의 pid와 ticks가 같았다. detach 중 출력이 보존됐고, owner와 Task는 바뀌지 않았다. provider 요청은 +0이었고 중복 입력도 없었다.
- **C6**: tmux `detach-client`, herdr client 종료 뒤 다시 attach했을 때 identity가 같았다.
- **C7**: `shutdown --yes` verified였다. 잔존은 {}이고 sandbox root가 제거됐다.

**첫 실행 실패(intermittent Esc)**
- 첫 실행에서 plain/Bash, plain/dash, tmux/dash 3셀이 C2에서 실패했다. 실패 단정은 manager OMP의 `esc_closed_menu: False`, 즉 Esc 뒤 화면에 slash menu가 닫히지 않은 채로 남았다. 같은 셀에서 typed_visible, Alt-Enter 줄바꿈, Ctrl-C, slash_menu, 한글 composer, alive는 모두 True였다. Herdr 2셀과 tmux/Bash는 통과했다.
- 같은 argv로 전체를 다시 돌린 r2는 6셀과 SEL1–3 모두 통과했다. 원인은 조사하지 않았다. 같은 코드에서 3/6셀 실패 뒤 6/6 통과했으므로 결정적 회귀보다 Esc 뒤 화면 timing에 가깝다고 본다. 이 조합에서 Esc 닫힘이 항상 보장된다고 쓰지 않는다. 두 기록을 모두 남겼다.

각주
1. plain PTY에는 outer 영속 계층이 없다. UI 자체의 detach와 재attach는 C5에서 확인했다.
2. tmux 기본값 `set-clipboard external`에서는 tmux가 앱의 OSC 52를 버린다. 제품은 "복사됨: N자 (tmux는 set-clipboard on 또는 allow-passthrough on 필요)" 안내를 띄운다. 시험 harness가 격리 server에 `tmux set-option -g set-clipboard on`을 적용한 뒤에는 바깥 터미널이 OSC 52를 받았다. 이 설정은 제품이 아니라 harness가 적용했다. 따라서 tmux 사용자는 직접 `set-clipboard on` 또는 `allow-passthrough on`을 설정해야 한다.
3. Herdr 0.9.3은 약 1 MiB를 넘는 bracketed paste를 앱에 전달하지 않는다. B1 raw probe(C16 기록) 결과 1 MiB는 도착했고 1.9 MiB와 2 MiB+64는 도착하지 않았다. C18 compat 보고서도 같은 이유로 Herdr 경로의 2 MiB 거절을 "해당 없음"으로 기록했다. 대신 shell 줄이 깨끗하게 유지되는 것을 확인했다.

**자식이 입력을 읽지 않을 때의 queue_full paste 거절**은 이 live matrix에서 실행하지 않았다. C18 regression-a의 live 테스트 `test_live_contract_independent.test_queue_full_on_a_stalled_real_pane_rejects_whole_frame_with_reason`가 실제 pane에서 통과로 관측했고(OMP 18.8.0, VERIFICATION §4.1), fixture 테스트(`test_product_paste_independent`, `test_product_pty_independent`)도 있다. 이 live 테스트는 outer 조합(tmux·Herdr)별로 돌린 것이 아니다.

## 3. Shell matrix

| 조합 | 실제 관측(C18) | 상태 |
|---|---|---|
| Bash 우선(SEL1) | PATH가 {bash, sh→dash, omp}이고 `SHELL`이 가짜 zsh여도 `shell bash (/usr/bin/bash)`를 골랐다. `$$`는 parent pid와 같았고 SHELL 값은 보존됐다. zsh sentinel은 실행되지 않았다 | 통과 |
| Bash 없음 → sh(SEL2) | PATH가 {sh→dash, omp}일 때 `shell sh (/usr/bin/dash)`를 골랐다 | 통과 |
| Bash·sh 모두 없음(SEL3) | exit 2와 함께 "OMP Workbench needs Bash or a POSIX sh on PATH…", "No backend was started."를 출력했다. data dir은 바뀌지 않았고 `status`는 exit 3이었다 | 통과 |
| actual Bash/dash canonical matrix | `test_actual_bash_and_dash_canonical_matrix`(compat check 비live 부분), `gates/g2_shell` 142 OK(skip 1) | 통과 |
| SH1–SH5 × Bash | SH1: cwd·PATH·export 상속, 비전파, `WORKBENCH_*` 없음. SH2: 인수 요청과 확인, foreground 수동 입력, `wb-handoff`. SH3: 미제출 입력·REPL·job 보류. SH4: `exit 3` 확정. SH5: trap CHLD 보류, PROMPT_COMMAND 보류, trap DEBUG 호환 | 통과 |
| SH1–SH5 × dash | SH1–SH4는 Bash와 같다. SH5: trap CHLD 보류, PS1 보류 | 통과 |
| 실제 Bash/dash 수동 입력(live) | `test_live_start`의 sh만 있는 PATH(dash 선택·수동 입력)와 `test_manual_input_backend_independent`(bash·dash ui_v1)가 C18 regression에서 통과했다 | 통과 |
| zsh 기본/login shell | C-D72 (1)에 따라 지원 shell은 Bash와 sh뿐이다. 실제 zsh login은 검증 범위 밖이다. 사용자 login shell이 zsh여도 Bash를 고르는 동작은 가짜 zsh sentinel로만 확인했다(SEL1, `test_live_start`) | 범위 밖 |

제약
- PROMPT_COMMAND(bash)나 PS1(dash)을 덮어쓰면 자동 입력은 정확히 보류된다. C16에서는 사유가 `not at a clean prompt (mode manual_input)`로만 표시됐다. fix-03 뒤 C18 보고서의 사유 문구는 "…(mode manual_input): a user PROMPT_COMMAND (bash) or PS1 (sh) change may have replaced Workbench's prompt hook, or a shell builtin or multi-line command is still running or waiting for input"이다. builtin `read`, 반복문, PS2 continuation도 같은 상태라서 원인을 단정하지 않는다.
- `setsid`(`-w` 없음)로 double-fork해 shell tree를 벗어난 daemon은 추적되지 않는다.
- C-D55에 따라 conda 전용 activation과 deactivation은 범위 밖이다.

## 4. OMP 버전별 판정

| 버전 | 판정 |
|---|---|
| 18.8.0 | 최종 검증 버전이다(C-D72 (2)). 제품 경로의 COMPAT, FLOW, POLICY, SHELL, FAULT 근거를 모두 이 버전으로 만들었다. G3 `test_tui_extension_fault_integration`은 실행 버전을 기록하는 방식으로 바꿔 3/3 통과했다. 18.2.10에 고정돼 있던 backend·terminal live 테스트 29건도 `live_harness.py`의 pin 해제 뒤 이 버전으로 모두 통과했다(VERIFICATION §4.1) |
| 18.2.10 | G1·G3 feasibility live probe와 L-CW17·L-CW18 원래 근거의 버전이다. historical로 둔다. `tests/ui/live_product_omp_independent.py` 등 discover 밖의 고정 probe는 C18에서도 실행하지 않았다. C18 재관측과의 대응은 VERIFICATION §4.1과 §9에 있다 |
| 18.4.5–18.7.0 | 중간 smoke와 isolation 근거다(제품 launcher `EVIDENCE_OMP_VERSIONS`: bridge_g3 18.2.10, isolation 18.6.1). historical |

18.8.0에서 관측한 OMP 동작
- 모델이 같은 tool call id를 다시 내면 OMP가 `_dupN`으로 이름을 바꾼다.
- provider 500을 받으면 약 16회, 약 40초 동안 재시도한 뒤 turn error를 낸다.
- 첫 모델 turn 뒤 `omp __omp_worker_daemon_broker`를 띄운다. 제품 종료 확인이 이 broker까지 기다리거나 종료하도록 fix-01에서 고쳤다. C18 X4에서 verified 시점에 broker 0을 확인했다.
- 첫 OMP 시작 때 `$HOME/.omp/natives/18.8.0`에 addon을 푼다. 제품 start 줄이 이를 미리 알린다. 시험에서는 HOME이 fake라 사용자 `~/.omp`는 쓰이지 않았다.
- `/new`는 `session_switch`로 처리된다(generation +1). 같은 pid에서 bridge manager가 다시 등록되고 `manager_recovery` notice가 1회 간다(gap-c18 SESSION).
- OMP가 만드는 파일은 사용자 umask를 따른다(gap-01 관측, umask 002에서 `history.db` 0644, cache·logs 0775 등). Workbench는 OMP child에 umask를 바꾸지 않는다(C17 review P2-1로 fix-05에서 되돌림). 제품은 data dir, `omp-root`, `omp-root/agent`를 0700으로 보장하고 소켓은 0600이다.

승인 창: Workbench OMP home은 `tools.approvalMode`를 설정하지 않는다. 18.8.0의 기본값은 `yolo`이고(Root가 깨끗한 임시 home에서 확인, 2026-10-08), live 실행에서 승인 창은 나타나지 않았다. 따라서 "해당 없음"이다(C-D72 (4), VERIFICATION §6).

## 5. 미검증

- 실물 GUI terminal emulator의 font, RGB 색상 fidelity, IME 입력. 화면은 pyte 에뮬레이션으로만 확인했다. 색 표시는 C-D73에 따라 256색 근사가 확정 동작이고 원본 24-bit 색 fidelity는 후속 과제다(§6).
- bold/underline/reverse 강조의 outer 화면 대조. gap-c18 COLOR는 색만 확인했다.
- tmux·Herdr를 통한 truecolor/256색 전달. 색 시험(gap-c18)은 plain PTY에서만 실행했다.
- 갑작스런 전원 차단(system_reset). 재부팅은 VM guest의 정상 `reboot`만 실행했다.
- macOS, Windows
- 다른 tmux 버전(3.4 외)과 다른 Herdr 버전(0.9.3 외). Herdr 0.9.1은 p2.6 historical이다.
- 다른 OMP 버전(18.8.0 외)에서의 제품 경로
- 실제 zsh login session. C-D72 (1)에 따라 범위에서 뺐다.
- tmux·Herdr 안에서의 실제 pane queue_full, 2 MiB paste 중 응답성, frontend SIGKILL 뒤 재attach. 이 live 테스트들은 C18에서 실행해 통과했지만 tmux·Herdr 조합별로 돌린 것은 아니다.
- 실모델 호환. 모든 호환 실행은 scripted local provider다.

## 6. 색상 표현(C-D73)

2026-10-08 사용자 결정 C-D73("256색 근사 수용")에 따라 제품 UI의 색 표현은 아래와 같고 이것이 확정 동작이다. 원본 색 fidelity는 [FOLLOWUP-TRUECOLOR.md](FOLLOWUP-TRUECOLOR.md)에 후속 과제로 있다(승인된 범위가 아니다).

한계
- 제품 UI는 curses color pair로 pane을 그린다. OMP가 보낸 24-bit RGB는 6×6×6 cube(xterm level 0/95/135/175/215/255)와 grey ramp(8+10k) 중 가장 가까운 256색으로 근사된다. 원본 RGB와 다를 수 있다.
- 256색 index는 pane 화면(pyte)이 hex로 바꿔 저장한다. cube와 grey ramp는 같은 RGB의 index로 그려진다. system 색(0–15)은 pyte가 바꿔 쓴 xterm RGB와 정확히 같을 때만 그 index로 그린다. 0·9·10·11·13·14·15는 RGB가 cube 색과 같아 pane 화면에서 이미 구분이 사라지므로 같은 RGB의 cube index로 그린다. 사용자 terminal theme가 system 색을 바꿨다면 바깥 화면의 색이 다를 수 있다.
- 실물 GUI terminal의 표시 fidelity는 검증하지 않았다(§5).

C18에서 관측한 것(gap-c18 COLOR, pass, plain PTY의 pyte outer, TERM=xterm-256color, COLORTERM=truecolor, OMP 18.8.0, host shell Bash)
- basic 16색과 SGR 30–37·90–97·40–47·100–107 32건: 모두 해당 색으로 그려졌다(`brightbrown`=SGR 93/103은 11, `bfightmagenta`=SGR 105는 13으로 매핑. 이 둘은 C17 review P2-2에서 기본색으로 그려지던 결함이었고 fix-05에서 고쳤다).
- 256 cube, grey ramp(236, 244, 252와 배경 240), system index 1/4/8/12: 원색 그대로.
- 24-bit RGB: `123456`→`005f5f`, `ff8000`→`ff8700`, `c81e8c`→`d70087`, 배경 `fa6432`→`ff5f5f`. 가장 가까운 256색이다.
- `303030`처럼 숫자로만 된 hex 색을 10진 index로 읽던 오류(gap-01에서는 `eeeeee`로 그려졌음)는 없다.
- 이 시험은 색만 대조했고 bold/underline/reverse 강조는 대조하지 않았다.

바깥 환경 주의(FOLLOWUP-TRUECOLOR §4의 기록. C18에서 확인한 것이 아니다)
- tmux는 `default-terminal`과 `terminal-overrides`의 `Tc`/`RGB` 설정이 없으면 RGB를 256색으로 줄일 수 있다.
- Herdr 0.9.3의 truecolor passthrough는 확인한 적이 없다.

## 7. 지원 판정

- C18에서 실제로 검증한 조합: Linux(Ubuntu 24.04.4) + OMP 18.8.0 + host Bash 5.2.21 또는 dash 0.5.12 + plain PTY / tmux 3.4 / Herdr 0.9.3. 6개 조합 모두 C1–C7을 통과했다(plain C6은 해당 없음). 단 첫 실행에서는 3개 조합이 C2의 Esc 닫힘으로 실패했고 r2에서 통과했다(§2).
- Herdr 0.9.3에서는 약 1 MiB를 넘는 paste가 outer 단계에서 전달되지 않는다. tmux에서 OMP 복사를 바깥 터미널로 보내려면 사용자가 `set-clipboard on` 또는 `allow-passthrough on`을 설정해야 한다.
- OMP pane 색은 256색 근사로 표시된다(§6, C-D73).
- Workbench 내부에서는 tmux를 실행하거나 의존하지 않는다. tmux와 Herdr는 사용자 outer 환경의 조합으로만 썼다.
- 위 목록 밖의 조합(§5)은 지원으로 판정하지 않는다.
