# p27-flaky-01 result (test_designer, Opus) — 2026-10-08

기준 a12e668. 실모델·provider 요청 없음, src/omp_bridge/docs/.workflow(이 파일 제외) 수정 없음, commit·graphify update 없음. 임시 파일은 `/tmp/p27flaky`·`/tmp/p27flaky-*`·scratchpad에만 두었습니다. 같은 시간에 CW-19 구현자가 `src/**`와 `tests/backend/test_cd70_restart_worker.py`, `tests/recovery_boot/`, `tests/ui/test_product_model_cw19.py`를 바꾸고 있었습니다. 그 파일들은 건드리지 않았습니다.

**결론: 7건 모두 테스트 쪽 경합입니다. 제품 결함은 없습니다.** 단언은 하나도 약하게 바꾸지 않았습니다. 기다리는 조건을 실제 전제(프로그램이 foreground 소유, reader가 입력 대기, 출력 완료)로 바꾸고, 몇 곳에는 단언을 더했습니다.

## 항목별 원인 · 수정

| # | 테스트 | 원인 (재현) | 수정 |
|---|---|---|---|
| (a) | `test_terminal_independent_p27cd68.IdleOnlyRefusalMatrix.test_a_foreground_program_refuses` (+ 같은 matrix의 `test_a_stopped_job_refuses_without_typing`) | `sleep 30\r` 직후 "not idle"은 입력 줄만으로도 바로 참입니다. bash가 terminal을 sleep에 넘기기(tcsetpgrp/exec) 전에 ^C가 가면 SIGINT는 bash가 받고 sleep은 30 s 동안 계속 돌아 `wait_until(idle, 10)`이 실패합니다. 프로브: `sleep 30\r` 직후 지연 없이 ^C를 보내면 2/3 실패, 0.5 ms 이상 지연하면 0/21 실패. stopped-job 테스트는 같은 경합을 `time.sleep(0.5)`로 덮고 있었습니다. | `foreground_program()` 추가: `tcgetpgrp(master)`로 얻은 foreground 그룹의 comm. tool 호출·^C·^Z 전에 그 값이 `"sleep"`이 될 때까지 기다립니다. 이제 거절은 실행 중인 프로그램 때문이라는 것도 보장됩니다(전에는 입력 줄 때문에 거절됐을 수도 있었음). `time.sleep(0.5)`는 지웠습니다. |
| (b)(e) | `tests/terminal/test_manual_input_boundary_independent.py`, `test_manual_input_target.py` PTY 대기, bash 연속 Ctrl-C (`test_editing_and_control_keys_reach_the_prompt_and_hold_automation`) | 원인은 같습니다. READY는 PROMPT_COMMAND(dash는 PS1 `$( )`)에서 나오므로 readline이 terminal을 raw로 바꾸고 read를 시작하기 **전**입니다. 그 틈에 보낸 키는 이렇게 처리됩니다. (1) DEL·ESC·^D를 아직 canonical인 tty가 먼저 처리합니다. ^D는 readline에 NUL로 가서 python REPL이 끝나지 않고, `ESC [`가 남아 `\r`이 accept-line이 되지 않습니다(`m='A'` 미생성). (2) readline이 read에서 대기하지 않을 때 온 ^C는 다음 키 전까지 처리되지 않아 READY가 오지 않습니다(`clean manual_prompt` 타임아웃). 프로브: clean prompt 직후 `printf NO`+^C를 보내면 부하 없이 2/200, CPU 8개 부하에서 41/450 실패했고, 아래 대기를 넣으면 0/400, 부하 0/450였습니다. 그 밖의 원인: dash `kill %1` 뒤 sleep이 **zombie**로 남았습니다(dash는 다음 prompt·명령에서 reap; 4/60 재현, 상태 `Z`). own-group 프로그램의 자식이 `SIGINT=SIG_DFL`로 바꾸기 전에 ^C가 오면 CPython이 그 신호를 버렸습니다. 마지막으로 `printf ok > marker`에서 파일은 redirection이 먼저 만들어서, `exists()` 직후 읽으면 `''`였습니다(stuck-03과 같은 꼴). | `reader_waiting(master_fd)`/`reader_settled()`: foreground 그룹 leader가 tty read 계열 syscall(read fd 0, poll, select, pselect6, ppoll; `/proc/<pg>/syscall`)에서 대기 중이고 slave에 읽히지 않은 바이트가 없음(`TIOCGPTPEER`+`FIONREAD`)을 3회 연속 확인합니다. 이 대기를 `open()`, `prompt()`/`clean_prompt()`, run 뒤 `return_path`, ^C 직전, python REPL 키 직전에 넣었습니다(target 파일은 import해서 씀). 그 밖에 `children()`는 zombie를 제외하고, own-group 자식은 `/proc/<pid>/status`에서 SIGINT가 기본 동작이 될 때까지 기다립니다. pane 테스트는 ^C 뒤 fresh prompt를 기다린 다음 다음 줄을 보냅니다(dash에서 ^C 직후 보낸 줄이 실행되지 않음: 부하에서 2/120, PTY에는 써짐 `_pending=0`). marker는 내용 `"ok"`까지 기다립니다. |
| (c) | workflow cwd-verify `WorkflowHeld("parent shell cwd could not be verified")` | p27-cd69-stuck-03에서 이미 제품 쪽 원인을 고쳤습니다(`pwd -P > file`이 쓰기 전에 빈 파일을 읽음 → `run.py` `_complete_line`). 이번 재현 시도는 모두 통과했습니다: `test_run`+`test_run_independent` 8 copies × CPU 12개 부하 0/8, 단독 10/10, full workflow 6 copies(부하)·3 rounds 모두 cwd 실패 0. | 변경 없음. 회귀 가드는 stuck-03의 `test_the_parent_cwd_file_is_read_only_when_pwd_finished_writing_it`입니다. |
| (d) | `test_workflow_unbounded_independent_p27b…test_default_start_runs_past_90s_and_confirms_real_exit_across_idle_collect_gap` | 시계 `began`이 4개 run을 **모두 시작한 뒤** 시작됐습니다. 그런데 "running" 확인은 `began+90`까지, 한 바퀴(collect 0.5 s × 4) 때문에 실제로는 ~92 s까지 했습니다. 그래서 먼저 시작한 run의 `sleep 92`가 이미 끝나 있었습니다. 부하에서 6 copies 중 4개 실패: `('exited', True) != ('running', False)` "sh-fail after 92s", `stat(child_pid)` None → TypeError(setdefault가 매번 stat 평가), 그 뒤 cleanup의 "processes survived" 2차 실패. 원래 ~105 s 걸리는 테스트라 runner timeout에도 걸렸습니다. | run마다 시작 직전 시각으로 나이를 잽니다. 실험은 그 뒤 시작하므로 `started+95` 전에는 끝날 수 없습니다(`sleep 95`). 각 run을 나이 90 s 이상에서 running으로 한 번 이상 관측할 때까지 collect합니다(90 s 넘은 run은 더 collect하지 않아 idle gap 유지). 관측이 94 s를 넘으면 테스트가 느린 것으로 실패시킵니다. 최종 collect는 `max(started)+107`(전 104→107, 실행 시간 +3 s). `stat` None이면 명확한 단언으로 실패합니다. 실행 시간 ~108 s는 계약("90 s를 넘겨 실행") 때문이라 줄이지 않았습니다. |
| (f)(g) | `test_recovery_e2e_independent_p27cd70` `test_restart_mid_command…`의 `find_in_session(shell.pid, b"2.7")[0]` IndexError (overprint-01 backend 1차, cd70-fix-01 batch) | 찾은 pid는 `[worker] $` 표시 wrapper(`bash -c 'printf …; exec "$0" -c "$1"'`)입니다. 그 직후 `exec`으로 `bash -c "sleep 2.7; …"`가 되는데, exec 중에는 `/proc/<pid>/cmdline`이 빈 값으로 읽힙니다(exec 루프 프로브에서 386 read 중 19회 빈 값). wait가 찾은 뒤 다시 읽으면 `[]`가 나올 수 있었습니다. | `first_found()`: wait predicate가 찾은 pid 목록을 그대로 씁니다. pid는 exec 뒤에도 같습니다. `start_ticks`가 None이 아님을 단언합니다. 같은 꼴인 `7031` 조회에도 적용했습니다. |

### smoke 드라이버
`tests/backend/live_cw18_independent_p27w.py` `Ui`: outer 에뮬레이터를 `tests/ui/support.rep_screen_classes()`(REP + SU/SD)로 바꿨습니다(`_load`로 파일 경로에서 import). API(`screen`, `stream`, `pump/text/send/wait_text/close`)는 그대로이고 쓰지 않는 `import pyte`는 지웠습니다. live 테스트(실 OMP)는 실행하지 않았습니다. 대신 같은 `Ui`로 bash가 40줄을 채운 뒤 `ESC[1;40r ESC[2S`를 보내는 화면을 검증했습니다. 새 드라이버는 `row03…row40, NEW`로 정상이고, plain pyte는 같은 바이트에서 `row01…`, `NEW40`(가짜 overprint)였습니다.

## 반복 결과
| 대상 | 단독 10x | full suite 안 3x (backend+terminal+workflow 동시 실행) | 부하 스트레스 |
|---|---|---|---|
| (a) 2 tests | 10/10 | 3/3 (backend OK) | 수정 전 10 copies·8 burner 0/10(재현은 프로브로), 수정 뒤 8 copies·8 burner 0/8 |
| (b)(e) 두 모듈 38 tests | 10/10 (marker 수정 뒤 재실행; 수정 전 1회 `''!='ok'`가 나와 원인 찾아 고침) | 3/3 (terminal OK) | 수정 전 8 copies·8 burner **8/8 실패** → 수정 뒤 0/8, 최종본 다시 0/8 |
| (c) test_run+independent | 10/10 | 3/3 (workflow OK) | 8 copies·12 burner 0/8 |
| (d) | 10/10 (각 ~108 s) | 3/3 (workflow OK) | 수정 전 6 copies·4 burner 4/6 실패 |
| (f)(g) 2 tests | 10/10 | 3/3 (backend OK) | — |

### full suite 3 rounds (병렬 부하: 매 round마다 backend·terminal·workflow를 동시에 실행; 10x 단독 실행과도 일부 겹침)
| round | backend | terminal | workflow |
|---|---|---|---|
| 1 | exit 0, 1095 OK (skip 30, xfail 1) | exit 0, 221 OK (skip 2) | exit 0, 58 OK |
| 2 | exit 0, 1095 OK | exit 0, 221 OK | exit 0, 58 OK |
| 3 | exit 0, 1095 OK | exit 0, 221 OK | exit 0, 58 OK |

round 1 terminal은 marker 수정 전 파일로 import됐습니다(round 2·3과 단독 10x는 최종본).

### 최종 full suite (`PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`; backend·terminal·ui 동시 실행)
| suite | 결과 |
|---|---|
| backend | **exit 0**, 1095 OK (skip 30, xfail 1) |
| terminal | **exit 0**, 221 OK (skip 2) |
| workflow | **exit 0**, 58 OK |
| ui | **exit 0**, 830 OK |
| gates/g2_shell | **exit 0**, 142 OK (skip 1) |
| contracts / gates/g1_vt / gates/g4_evidence / gates/g4_lifetime / lifecycle / observation / policy/pause_automation / policy/recovery_manager / storage / tasks / ui/status_workbench | 모두 **exit 0** (52 / 85 / 10 / 8 / 38 / 95 / 129 / 26 / 33 / 22 / 2) |
| bridge (py) | **exit 1**: `test_cw18_schema_independent_p27w.SchemaAgreementTests.test_bridge_tools_per_role`. manager 도구에 `stop_survivor`가 추가됐습니다. CW-19의 미커밋 `omp_bridge/g3/bridge.ts` 변경입니다. **CW-19 몫이라 추적하지 않았습니다**(실행 뒤 이 테스트 파일은 CW-19 쪽에서 수정 중). |
| recovery_boot (CW-19 신규, untracked) | **exit 1**: `test_survivors_holds_independent_p27cw19.SurvivorStop.test_stop_unconfirmed_survivor_that_ends_later_is_not_left_running`. **CW-19 몫**입니다. |
| gates/g3_omp, integration | **실행하지 않음**: 실제 `omp` binary를 띄웁니다(일부 probe는 격리 profile 없이 실행하고, integration은 tmux·herdr도 씀). 이번 수정과 관계없고 "실 provider 요청 없음" 규칙 때문에 제외했습니다. |

## 제품 관찰 (결함 아님, 참고)
- READY 직후부터 readline이 read를 시작하기 전까지 아주 짧은 구간이 있습니다. 이 구간에 들어온 키는 위처럼 bash/tty 규칙대로 처리됩니다. 이때 Workbench는 해당 줄을 `unknown`/`unsubmitted_or_unconsumed_input`으로 보고 automation을 계속 막습니다(안전한 방향). 다음 Enter의 READY에서 풀립니다. 사람은 readline이 terminal을 준비한 뒤에야 prompt를 보므로 붙여넣기나 매우 빠른 입력에서만 생길 수 있습니다. worker `terminal` 도구가 치는 것은 일반 문자열 + 개행이라 canonical 모드에서도 같은 줄이 됩니다.
- dash는 kill한 job을 다음 prompt·명령 때 reap합니다. 그 사이 zombie는 Workbench의 session member 검사에서도 job으로 세지 않습니다(`_session_members` 주석과 일치).

## 변경 파일
`tests/backend/test_terminal_independent_p27cd68.py`, `tests/backend/test_recovery_e2e_independent_p27cd70.py`, `tests/backend/live_cw18_independent_p27w.py`, `tests/terminal/test_manual_input_boundary_independent.py`, `tests/terminal/test_manual_input_target.py`, `tests/workflow/test_workflow_unbounded_independent_p27b.py`, 이 결과 파일.

## Residue
probe 임시 디렉터리(`/tmp/p27flaky-*`)는 지웠고 실행 로그는 `/tmp/p27flaky`에 남겼습니다. 내가 띄운 bash/dash/python/burner 프로세스는 테스트 cleanup과 burner의 SIGTERM 처리로 모두 끝냈습니다(pkill 미사용). 출력 파일 6개가 실수로 repo 루트에 생겨 바로 `/tmp/p27flaky`로 옮겼습니다. 사용자 tmux, 다른 소켓, `~/wb-urux-sandbox`, `~/.omp` 등은 건드리지 않았고, credential과 다른 프로세스 environ은 읽지 않았습니다.
