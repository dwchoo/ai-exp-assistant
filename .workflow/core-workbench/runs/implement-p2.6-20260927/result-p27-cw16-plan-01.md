# p27-cw16-plan-01 — CW-16 최종 검증 실행 계획 (code_explorer, read-only)

작성 2026-10-08. 이 파일 외에는 아무것도 쓰지 않았다. 모델·provider 요청 0, 자격 증명 미열람, 사용자 tmux/herdr·VM·sandbox 무접촉, 프로세스 신호 없음, commit 없음. 실행한 것은 읽기 명령과 `omp --version`·`tmux -V`·`herdr --help`(서버 무접촉)뿐이다.

## 0. 결론

- CW-16은 **CW-19 수정 통합 뒤의 단일 동결 후보 C16**에서 다시 실행해야 한다. p2.6 근거(candidate `6dc51f…afa88`)는 제품 UI 경로가 아닌 `terminal_g1`·OMP 18.2.10·Herdr 0.9.1 기준이라 **재사용할 수 없고 historical로만 남긴다**.
- 재사용 가능한 것은 세 가지다. (a) L-CW19-REBOOT VM 근거: VM manifest(`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`)의 aggregate가 C16과 같을 때만 쓴다. (b) UR-UX·UR-USABILITY 사용자 검토 완료 기록. 이는 사용자 검토 전제이며 gate 근거가 아니다. (c) 기존 live 시나리오 코드(p27w `Ui`/scripted provider, L-CW19-RESTART probe, VM driver). 코드만 재사용하고, 실행은 C16에서 다시 한다.
- 모든 gate 근거는 **실제 OMP 18.8.0과 scripted provider(모델 요청 0)**로 만든다(PLAN.gate_evidence_contract: actual OMP + scripted provider = runtime). 실제 모델 smoke는 확신용으로 1 batch만 둔다. 요청 상한은 40, 예상은 약 32다.
- 작업 묶음은 B1~B4 test_designer 4회, 그 뒤 fresh reviewer와 Root 마감이다. **작성 단계(W)와 정식 실행 단계(R)를 나눈다.** 이유는 candidate snapshot이 `tests/**`·`docs/**`·HEAD를 포함하기 때문이다. 테스트 파일을 하나라도 고치면 그 전에 실행한 check가 모두 `stale`이 된다.
- 사용자 결정이 필요한 것은 네 가지다(§9). zsh 실제 login shell, OMP 18.2.10 pin 처리, "CW-16 완료=71개 전체 집합" 해석, worker 직접 요청 3경계의 기대 동작.

## 1. 관측한 현재 상태

| 항목 | 관측 |
|---|---|
| p2.6 최종 gate | `gate-cw16-final-result.json`: `passed:false`. issue 6개: P-C-AC-19·I-COMPAT `failed`, I-FLOW/I-SHELL/I-POLICY/I-FAULT `not_run`, 모두 unknown 포함. P-C-AC-20만 passed(6dc51f, Bash 5.2.21/dash/OMP 18.2.10/tmux 3.4/Herdr 0.9.1) |
| 형식 | `requirements-cw16.json`(p2.6 authority, 7 gate_items, 34 acceptance). `workflow_tools.py check`는 `items:[]`를 남기고, Root가 `check-…-observed.json`에 items를 추가하며, `workflow_tools.py gate`가 판정한다 |
| CW-19 | candidate `f7091db8…`(base a12e668). review-01 **block**(P2-1 worker→manager 조용한 drop, P2-2 model hold 해제 race, P3×5). test-01은 P3-1·P3-2 실패 2건. vm-01은 L-CW19-REBOOT pass와 F1·F2. **p27-cw19-fix-01 진행 중**(service/flow/flow_tasks/automation/recovery/run/model/bridge.ts/skills 변경 예정) → CW-19 delta test·VM 재실행·delta review·Root 통합이 끝나야 CW-16 R 단계를 시작할 수 있다 |
| 사용자 검토 | state.json: UR-UX closed 2026-10-03, **UR-USABILITY done 2026-10-08**("좋아 현재 잘 동작해…"). C-D57 전제 충족 |
| 환경 | OMP **18.8.0**(`~/.local/bin/omp`, 18.2.10 없음), Bash 5.2.21, dash 0.5.12(`/bin/sh`), tmux 3.4, **Herdr 0.9.3**, **zsh 미설치**, Python 3.12.3 venv `/tmp/cw02-g1-venv`(pyte), Node 22.23.2. VM guest: Ubuntu 24.04, bash 5.2.21, OMP 18.2.10 |
| 실행 맥락 | 이 세션은 **사용자 tmux와 herdr 안에서** 돈다(`TMUX*`, `HERDR_*` 설정됨). 모든 probe는 자식 env에서 `TMUX*`/`HERDR_*`/`WORKBENCH_*`/`DISPLAY`/`WAYLAND_DISPLAY`를 지우고, tmux는 항상 `-S <own sock>`, herdr는 항상 격리 `HERDR_CONFIG_PATH`/`XDG_RUNTIME_DIR`/`XDG_CONFIG_HOME`로만 호출해야 한다 |
| pin | 18.2.10 hard pin: `tests/integration/test_cw16_runtime_matrix.py` → `live_outer_compat_probe.py:67,71`(OMP·herdr 0.9.1)·`live_outer_tmux_probe.py:144`. `tests/gates/g3_omp/test_tui_extension_fault_integration.py:24`. G1·G3 live probe 다수. 제품 launcher `EVIDENCE_OMP_VERSIONS={"bridge_g3":"18.2.10","isolation":"18.6.1"}`(시작 때 isolation 재검사로 18.7.0 ok 기록, 18.8.0은 L-CW19-RESTART에서 사용) |
| candidate 정책 | p27 snapshot exclude = `[".workflow/core-workbench/runs/implement-p2.6-20260927/**", "graphify-out/**"]`, watch `[]`. `capture()`는 tracked+untracked(gitignore 제외)+HEAD를 포함한다 |

## 2. B0 — Root 전제(test_designer dispatch 전)

1. CW-19 체인 완료: fix-01 → 독립 delta test(실패 2건 green, L-CW19-RESTART 재실행) → VM L-CW19-REBOOT 재실행(fix가 src를 바꾸므로) → fresh delta review → Root 통합. **CW-19 VM 재실행 때 manifest aggregate를 기록하게 해야** CW-16이 그대로 재사용할 수 있다.
2. `requirements-p27-cw16.json`을 새로 만든다. revision `core-workbench-p2.7-CW-16`. authority는 현재 승인 bundle(amendment t, `cd3e3c9d…`)과 사용자 인용 "전부 진행해서 테스트까지 해서 완료하고 보고해"다. gate_items는 PLAN의 CW-16 7개를 그대로, acceptance_ids는 34개를 쓴다. p2.6 파일은 보존한다.
3. ledger 예약(§10), mutation lease와 resource claim(PLAN: host:interactive-terminal, host:omp-runtime, host:shell-fixture, repo:integration-wiring, host:reboot-vm 모두 exclusive), namespace `pty/uds/omp-session/data-dir:cw-16`, `vm:wb-reboot`.
4. R 단계 동안 repo writer는 0이어야 한다. graphify-out은 exclude지만 `graphify update` 금지를 공지한다.

## 3. 재사용 판정

원칙(PLAN.gate_evidence_contract.reuse): 과거 digest를 새 digest로 덮어쓰지 않는다. 같은 입력이라는 영향 대조 기록을 붙이거나 다시 실행한다.

| 근거 | 판정 | 이유 |
|---|---|---|
| p2.6 `check-cw16-final-observed.json`(6dc51f) 전체 | **재사용 불가 → historical**. G4 음성 대조의 stale 입력으로 쓴다 | UI가 feasibility `terminal_g1`(제품 경로 아님)이고, OMP 18.2.10·Herdr 0.9.1이며, 이후 shell adapter(C-D58, 1ea7c2f 등)가 바뀌었다 |
| L-CW19-REBOOT(vm-01, f7091db8) | CW-19 fix 뒤 **CW-19가 C16 manifest로 재실행하면 재사용**한다. 아니면 B3에서 재실행한다(약 15분, 모델 0) | VM manifest 범위가 CW-16 산출물(tests/integration, docs)과 겹치지 않는다. 조건은 aggregate 일치다 |
| L-CW19-RESTART(test-01) | C16에서 재실행한다(약 3분, 모델 0) | fix가 service/recovery를 바꾼다 |
| L-CW17-*, L-CW18-*, P-C-AC-01/06/14(v-cw06), cd68~cd70 smoke | 코드·시나리오만 재사용하고 **C16에서 재실행**한다 | 이후 C-D66~C-D71로 흐름이 크게 바뀌었다(terminal 도구, commands, watchdog, restart_worker, boot hold) |
| UR-UX / UR-USABILITY | 전제 충족 기록으로만 인용한다 | 자동 gate와 사용자 검토는 서로 대체하지 않는다(C-D57) |
| 실제 모델 smoke(cd68~cd70) | 확신용 historical(OMP 18.6.1~18.7.0) | gate 근거 등급이 아니다 |
| G1/G3 18.2.10 live probe 근거 | 버전 고정 historical. 71개 전체 집합에서는 §8의 처리가 필요하다 | 로컬에 18.2.10이 없다 |

## 4. 항목별 계획

### 4.1 CW-16 소유 7개 item

| Item | 근거 | 실행(시나리오 §5) | 재사용 |
|---|---|---|---|
| P-C-AC-19 | runtime | COMPAT matrix: plain PTY/격리 tmux/격리 herdr × host Bash/dash(6회). 각 회차 C1–C7. 이 중 **backend 유지 UI detach≥60s → reattach**(C5)와 outer client detach/reattach(C6) 포함 | 없음 |
| P-C-AC-20 | runtime | SEL1–4(제품 진입점의 실제 PATH 조작) + G2 canonical matrix(Bash/dash) 재실행 + COMPAT의 dash 회차 + SH1–SH5의 bash·dash 양쪽 | 없음(p2.6 pass는 historical) |
| I-FLOW | runtime | F1–F7(scripted, 진입점+제품 UI, host Bash. F1·F2는 dash로 1회 더) | 없음 |
| I-SHELL | runtime | SH1–SH5 × {bash, dash}. C-D50..52 정상 handoff·control 대기·환경 전환·전송 중 인수·foreground 수동 입력 | 없음 |
| I-POLICY | runtime | P1–P8. 121번째 점검·13번째 wake는 clock fixture suite(tests/policy, observation) 재실행을 함께 붙인다(SPEC 허용) | 없음 |
| I-FAULT | runtime | X1–X7. X5는 VM | X5만 조건부 재사용 |
| I-COMPAT | runtime | COMPAT 6회 + 버전 기록(OMP 18.8.0, tmux 3.4, herdr 0.9.3, bash 5.2.21, dash 0.5.12) + C-AC-06/14의 UI 조작을 각 outer에서 수행 | 없음 |

### 4.2 C-AC-01..34

| AC | final | 확인할 동작(요지) | 시나리오 | Batch |
|---|---|---|---|---|
| 01 | FLOW | manager pane에서 목표 대화(입력이 원본 OMP로 감) | F1 | B2 |
| 02 | FLOW | Task 연계 task/question/answer/report 왕복과 중복 방지(같은 toolCallId 재전송, busy 중 두 번째 `to_worker`→`worker_busy`) | F1, F4 | B2 |
| 03 | FLOW, SHELL | host shell 실행·출력, worker `terminal`/실험 의뢰, exit status·log 위치 기록, 종료/unknown | F1, F2, SH4 | B2 |
| 04 | FLOW | worker 1차 판단(출처·내용·미확인)과 manager의 완료 조건 대조 2차 확인·추가 조사 | F2(성공/실패/exit0인데 실패/로그 부족 4사례) | B2 |
| 05 | FLOW | 준비→실행/관측→1차 판단 보고. 수정 필요 보고 시 지시 종료, 새 지시 전 자동 반환 없음 | F2, F6 | B2 |
| 06 | COMPAT | 세 영역, focus와 입력 owner를 따로 표시 | C1, C5(owner=worker 중 focus 이동) | B1 |
| 07 | POLICY | 실제 종료 뒤 승인 범위 내 수정·재지시·재실행, 3회 한도 뒤 자동 중단과 근거 보고, 설정 변경 뒤 횟수 유지, 시간 상한 없음 | P4 | B2 |
| 08 | SHELL | 인수 요청 즉시 새 전송 보류, 요청/확인 분리, 전달된 명령 상태, foreground 수동 입력, `wb-handoff`/control 대기, no replay | SH2 | B2 |
| 09 | FLOW | commit 기준 실행 worktree 가이드, 준비 실패(없는 commit) 처리와 무실행 | F2 | B2 |
| 10 | POLICY | 실행 종료 확인 뒤에만 소스·설정 수정 | P5 | B2 |
| 11 | FLOW | 위임 변경만 local commit, push/merge 없음(로컬 bare remote로 push 부재 확인) | F3 | B2 |
| 12 | POLICY | 활성 중 60초 점검, 일시정지 중 점검 중단, 무출력 조사 | P1, P2 | B2 |
| 13 | POLICY | manager의 근거·범위·재시도 대조, 추가 조사/해결 불가 보고 | P4 | B2 |
| 14 | COMPAT | 두 원본 OMP TUI와 명령(slash, Esc, Ctrl-C, Alt-Enter) 유지. approval prompt는 §9-D5 | C2 | B1 |
| 15 | FAULT | detach 중 두 OMP·실험 지속, 활성 때만 점검, pause 보존, 재접속 | X1(+C5) | B3 |
| 16 | FAULT | 재시작 뒤 기록/프로세스 대조, 불명 replay 금지 | X2 + L-CW19-RESTART 재실행 | B3 |
| 17 | FAULT | 모델 오류 중 실행·관측 유지와 재확인(오류 응답→hold→다음 정상 turn 해제, P2-2 race 포함) | X3 | B3 |
| 18 | FAULT | 인수 요청/확인·user owner·전달된 명령 상태가 detach/재접속 뒤 보존, 명시적 handoff 전 자동 전송 없음 | X1 | B3 |
| 19 | COMPAT | plain/tmux/herdr 조작·재접속 matrix | C1–C7 ×6 | B1 |
| 20 | COMPAT, SHELL | Linux 텍스트(한글·wide·bracketed paste), Bash 우선, sh fallback, zsh 기본 shell 무시, 둘 다 없음 안내 | SEL1–4, C2, SH* | B1 |
| 21 | POLICY | 활성 중 121번째 점검·13번째 peer wake(clock fixture), usage 표시(runtime status/UI) | P7 | B2 |
| 22 | FAULT | 전체 종료 확인·중단/정리·미종료 표시(verified/unverified 둘 다) | X4 | B3 |
| 23 | FAULT | 재부팅 뒤 `confirm-boot` 전 새 실험·자동 모델 작업 없음 | X5(VM) | B3 |
| 24 | FAULT | 명세/요약/worktree 보존, 64 MiB 실험 raw log cap 실측과 실행 지속, 512 MiB project cap(축소 한도 fixture) | X6 | B3 |
| 25 | POLICY | 승인 범위·Task/revision/run 연결, 취소, 중단 요청/확인/불명 연결 | P3, P8 | B2 |
| 26 | SHELL | 관리 부모 shell의 cwd/exported env 보존, 실험 내부 상태 비전파, 미제출 입력/REPL/job/수동 잔존/비호환 hook·trap 보류 | SH1, SH3, SH5 | B2 |
| 27 | POLICY | busy 점검은 최신 한 건 병합·지연, pause 중 자동 점검 보류, 즉시 종료 표시 | P1, P2 | B2 |
| 28 | FAULT | metadata 장애 시 새 자동 실행 차단, raw log 장애 관측, pause 저장 실패 표시(P3-2) | X6 | B3 |
| 29 | POLICY | 범위 승인+진행 지시(C-D66 standing delegation 아래 manager `to_worker`)로만 자동화 시작, 그 전에는 무실행 | F7 | B2 |
| 30 | POLICY | pause 중 새 자동 모델 작업 보류, 로그/프로세스 수집과 기존 실험 유지, 수동 조작 독립 | P2 | B2 |
| 31 | POLICY | 새 revision은 다음 run에 적용, 현재 실행 적용 지시는 종료 확인 뒤 재실행, 즉시 제한 우선 | P4 | B2 |
| 32 | SHELL | 사용자가 준비한 cwd·PATH·exported env를 실험 자식에 상속, app 환경 분리(conda 제외) | SH1 | B2 |
| 33 | POLICY | manager turn 중단 요청/확인, 불명 도구 결과 표시, 명시적 재개 대조와 replay 금지 | P3 | B2 |
| 34 | POLICY | 허용된 확인 대상만 강제 종료하고 실제 종료를 검증(`restart_worker`, `stop_survivor` 거절, host shell 강제 종료 확인) | P6 | B2 |

## 5. 실행 절차

### 5.1 공통 harness(B1이 작성하고 B2·B3가 재사용)

`tests/integration/cw16_harness.py`는 unittest discover 대상이 아닌 module이다.
- Sandbox: `/tmp/wb-cw16-<rand>/`(fake HOME, 빈 `~/.omp/agent/agent.db`, git project, data dir, `<data>/omp-root/agent/models.yml`에 scripted provider 하나). p27w setUp(`tests/backend/live_cw18_independent_p27w.py:339-380`)을 옮긴다. `HTTP(S)/ALL_PROXY=http://127.0.0.1:9`를 둔다.
- ScriptedProvider: OpenAI 호환 SSE이며 역할별 script를 쓴다. 지연 stream(turn 중단 시험), HTTP 오류/overload 응답(모델 장애), 요청 계수, 주입된 사용자 메시지 기록을 지원한다. p27w `Script`/`make_server`를 확장한다.
- 진입점: `python -m workbench start --data-dir D --omp ~/.local/bin/omp --omp-arg=--model --omp-arg=<p>/scripted --no-attach`, `attach`, `status --json`, `shutdown [--yes] --json`, `confirm-boot`.
- UI driver: p27w `Ui`(`setsid --ctty`, PTY 170x40, `tests/ui/support.py:208 rep_screen_classes()`)와 prefix `\x1d`(focus/q detach/p pause)를 쓴다.
- 정리: 소유 pid는 pid+start ticks로만 신호한다. PTY/pipe를 계속 drain한다. 전체 deadline을 둔다. 잔존 process·socket·tmp를 검사한다(`processes_mentioning(root)`=∅).
- 보고: 시나리오마다 JSON(명령, exit, pid/ticks, boot_id, OMP/herdr/tmux/shell 버전, 관측, unknown)을 쓴다. 출력은 run 디렉터리 밖 /tmp에 쓰고 Root가 run 디렉터리로 복사한다.

### 5.2 Outer 환경(COMPAT C1–C7)

- **plain PTY**: `Ui` 자체가 outer다. C6은 해당 없음으로 표시하고 pending으로 두지 않는다. plain에는 outer 영속 계층이 없다.
- **격리 tmux**: `tmux -S $ROOT/tmux.sock -f /dev/null new-session -d -x 170 -y 42 'exec python -m workbench attach --data-dir D'`. 렌더링은 우리 PTY 안의 `tmux -S sock attach`가 맡는다. 조작은 PTY 입력(+`send-keys`)으로 한다. resize는 PTY winsize와 `resize-window`로 한다. C6은 `detach-client` 뒤 재attach다. 정리는 `kill-server`와 socket 제거 확인이다. 사용자 socket(`$TMUX`)은 env에서 제거한다. 확인 사항: OSC 52가 tmux로 포장되는지, OMP DCS passthrough 알림이 pane 글자로 보이지 않는지(018bde4).
- **격리 herdr 0.9.3**: 기존 `live_outer_compat_probe.py:105-180` 방식을 쓴다. 격리 `herdr.toml`(onboarding false, default_shell /bin/bash), `HERDR_CONFIG_PATH`/`XDG_RUNTIME_DIR`/`XDG_CONFIG_HOME`를 tmp 아래에 두고, `session list --json`의 socket_path/session_dir가 tmp 아래인지 preflight로 확인한다. 우리 PTY에서 `herdr --session <name>`를 띄우고 `pane list`→`pane run <id> 'exec python -m workbench attach …'`를 실행한다. 화면은 우리 PTY 렌더와 `pane read --source visible`로 본다. C6은 herdr client 종료 뒤 `herdr session attach <name>`이다. 정리는 `session stop`/`delete`다. **바뀐 점**: (1) 버전 pin(0.9.1)을 기록으로 바꾼다. (2) 기존 `default_state_unchanged`는 사용자 기본 herdr server에 질의하므로 빼고, 격리 경로 확인으로 대체한다. (3) 이 세션의 조상에 사용자 herdr가 있으므로 ancestry 기반 `non_nested_attach`는 정보 기록으로만 두고, 판정은 env 제거와 별도 socket으로 한다.
- 회차 내용(각 outer × host {bash, dash}): C1 시작·세 영역·focus/owner 표시. C2 세 pane 입력(marker), OMP slash/Esc/Ctrl-C/Alt-Enter, 한글·wide 문자, 다중 행 bracketed paste, 2 MiB 초과 paste 거절 이유 표시. C3 resize와 자식 TTY 크기 변화. C4 OMP pane OSC 52 전달(host pane 복사는 전달 안 됨). C5 host에서 사용자 loop 실행 → UI detach(prefix q) → 65초 → `attach` → backend/OMP×2/shell/supervisor의 pid+ticks 동일, detach 중 출력 보존, owner/control 불변, 새 run·중복 전송 없음. C6 outer client 재접속. C7 `shutdown --yes` verified와 잔존 0.
- 예상 시간: 회차당 약 3분, 6회 약 20분, 모델 0.

### 5.3 Shell 선택(SEL)

SEL1–3은 `tests/backend/test_live_start_independent.py`의 실제 PATH 조작 3건(bash 우선+fake zsh sentinel 미실행, bash 없음→`/usr/bin/dash`, 둘 다 없음→`Workbench needs Bash or sh` 안내·exit≠0·무시작)을 C16에서 재실행하고 tests/integration에서 묶어 기록한다. SEL4 실제 zsh login은 §9-D1에 따른다. 권장 방식은 VM guest에 zsh를 설치하고 `chsh`한 뒤 zsh login에서 `start`를 실행하는 것이다. fake OMP(`tests/recovery_boot/fake_omp.py`)로 충분하다(shell 선택은 OMP와 무관).

### 5.4 Flow/Policy/Shell 시나리오(scripted, 진입점+UI)

- F1 자유 작업: manager pane 입력 → `to_worker(kind=work, commands=[...])` → worker `terminal` → exit/log_path → `to_manager done`+`commands_run` → manager 수신 → Task closed. F4: 같은 toolCallId 재전송은 멱등이고, busy 중이면 `worker_busy`.
- F2 실험: `spec.execution`(commit, command, criteria, shell)으로 worktree를 준비하고 host run → collect/judge → worker 1차 판단 → manager 2차. 판정 4사례(exit0+성공 로그, exit≠0+실패 로그, exit0인데 실패 marker, 로그 부족→판단 보류). 없는 commit이면 준비 실패를 보고하고 아무것도 실행하지 않는다.
- F3: worker가 승인 경로 안에서 수정하면 worktree branch에 local commit만 남는다. 로컬 bare remote의 ref 불변으로 push 부재를 확인한다.
- F5 worker 직접 요청 3경계(§9-D4 확인 뒤 기대값을 고정한다). F6 `requires_code_change`/`blocked` 보고 → 지시 종료 → 새 manager 지시 전까지 worker에 자동 전달 0. F7 manager 지시 전 automation idle, 실행 0.
- P1 140초 이상 무출력 구간이 있는 실험에서 약 60초·120초에 점검이 간다. worker busy면 최신 한 건으로 병합하고 지연한다. P2 pause 중에는 `to_worker`→`held:paused`, 점검·watchdog 0, raw log 증가 지속, 수동 입력 가능, pause 중 종료는 즉시 표시. P3 manager 지연 turn → pause → turn 중단 요청/확인(aborted) 표시. 도구 결과 불명 변형도 본다. resume은 대조 뒤에만 되고 replay는 0. P4 실패 run → `run:true` 재실행 3회 → 4번째는 자동 중단과 근거 보고. 진행 중 revision 변경은 다음 run에 적용하고 횟수는 유지. P5 run 진행 중 수정 지시는 보류. P6 `restart_worker`(Workbench가 띄운 worker만 종료하고 실제 종료 검증), `stop_survivor` 신원 불명은 거절, host shell 강제 종료 확인(C-D63). P7 status/UI usage 표시 + clock fixture suite. P8 취소: withdraw, run 관측 유지, ID 연결.
- SH1 host에서 `cd sub; export WB_CW16=1; PATH=$PWD/bin:$PATH` → 실험 자식이 cwd·PATH·값을 받고 부모 PID는 동일. 실험 안의 `cd`/`export`는 부모에 전파되지 않음. `WORKBENCH_*`는 자식에 없음. SH2 worker 명령 실행 중 UI 인수 요청 → 새 전송 즉시 보류 → 요청/확인 분리 → 전달된 명령 unknown/indeterminate 표시 → 현재 foreground에 수동 입력 → `wb-handoff` → control 대기 복귀, replay 0. SH3 미제출 입력/REPL(`python3`)/background job 상태에서 `terminal`·실험은 이유와 함께 보류. SH4 `sh -c 'sleep 5 & exit 3'`: 주 프로그램 반환·후손 종료·입력 반환을 분리하고 exit 3을 확인된 종료로 기록. SH5(bash) `trap … DEBUG`/`PROMPT_COMMAND` 비호환 → 이유와 보류.
- 예상 시간: B2 전체 약 30~40분(실제 60초 점검, 140초 run 포함), 모델 0.

### 5.5 Fault 시나리오

- X1 실험 실행 중 + 자동화 active + 인수 요청 상태에서 UI detach 65초 → 재attach. 점검은 active일 때만, pause 보존, 인수/owner/전달 명령 상태 보존, 자동 전송 0.
- X2 열린 Task와 실험 실행 중 backend를 정확한 pid로 SIGKILL → `start`. `same_boot_crash` 대조, run `interrupted`/unknown, outbox 재전송 0, 대조 뒤 새 manager 세션에 `backend_restarted` 1회(scripted 주입 기록으로 계수), survivor 목록, `stop_survivor`. 이어서 `tests/recovery_boot/live_restart_independent_p27cw19.py`(무Task 경로, 56 checks)를 C16에서 재실행한다.
- X3 scripted가 worker turn에 오류를 돌려줌 → `model_hold:worker` 동안 host 출력·raw log 지속 → 사용자가 worker에 입력 → 정상 turn 첫 도구 `terminal`이 허용됨(P2-2 회귀) → hold 해제. manager 오류 hold 중 worker `to_manager(answer)`는 보존되고 해제 뒤 1회 전달(P2-1 회귀). 실험 judge 보고도 대기(review P3).
- X4 실행 중 `shutdown`: `--yes` 없으면 exit 1과 활성 작업 목록. `--yes`면 정리 verified exit 0. survivor가 남으면 unverified, exit 1, "종료 확인 실패".
- X5 VM: `start.sh` → rsync(exclude `.git .workflow graphify-out __pycache__`) → manifest 대조 → guest `CW19_BASE=<n> ~/wb-venv/bin/python ~/cw19-driver.py pre|pre2` → `ssh.sh 'sync; sudo reboot'` → `post|post2` → `stop.sh`. driver 원문은 `result-p27-cw19-vm-01-log.json` `driver.source`(sha256 `3154e010…f27c0`)에 있다. CW-16은 이를 `tests/integration/vm_reboot_driver.py`로 고정해 둔다(guest 경로만 다름). 재사용 조건은 §3.
- X6 실험 run이 70 MiB를 출력 → 저장 64 MiB, run 지속, cap 표시. project 512 MiB cap은 축소 한도 fixture. 재시작 뒤 Task 명세·요약·worktree 보존. data dir 0500(또는 write 실패 주입) → `metadata_unavailable` hold, 새 자동 실행 차단, raw log 장애 표시. pause 저장 실패 표시(P3-2).
- X7 C-D52: 전달 중 인수 → backend 재시작 → old delivery attempt는 durable unknown이고 replay 0.
- 예상 시간: B3 약 25분 + VM 약 15분, 모델 0.

### 5.6 실제 모델 smoke(B4, 확신용이며 gate 근거 아님)

C16, OMP 18.8.0, 사용자 OMP 인증(Workbench OMP 홈의 symlink를 그대로 쓰고 내용은 열지 않음). RM1 짧은 정상 흐름(commands 1~3개) 약 8회. RM2 210초 명령(terminal_check 1회 이상 + `terminal_done` 1회) 약 10회. RM3 manager turn 중 pause → 중단 확인 → resume 약 6회. RM4 열린 Task 중 backend SIGKILL → 재시작 → `backend_restarted` → manager 판단 약 8회. **합계 약 32회, cap 40, 36에서 pause와 입력 거부.** counter는 UI pump 루프 안에서 센다(smoke-01에서 counter thread가 죽어 34/30을 넘긴 사례가 있다). 증가 정지 2초 이상이나 감소면 fail-closed다.

### 5.7 G4 validator(B4)

1. 각 batch의 정식 실행은 `python .agents/skills/workflow-ledger/scripts/workflow_tools.py check --input <req> --output check-p27-cw16-<b>-execution.json`으로 한다(root repo, requirements-p27-cw16.json, argv, exclude는 p27 정책과 동일, log는 run 디렉터리, timeout 3600). 그 뒤 Root(또는 B4)가 `-observed.json`에 7개 item을 기록한다.
2. `workflow_tools.py gate --input {requirements, candidate:C16, checks:[…observed]}` → `gate-p27-cw16-final-result.json`. 기대값은 `passed:true`, exit 0.
3. `tests/integration/test_cw16_final_evidence_validators.py`(env `WB_CW16_FINAL_REQUEST`가 없으면 skip하고 pass로 세지 않음)는 실제 최종 records를 tmp에 복사해 변형한다.
   - workflow gate 음성 대조: (a) I-FAULT를 담은 check를 빼면 `missing`. (b) p2.6 `check-cw16-final-observed.json`(6dc51f)을 넣으면 `stale`·`failed_or_not_run`. (c) 한 item의 unknowns가 `["x"]`면 `unknown`. (d) log 1바이트 변조면 `log_mismatch`. (e) 다른 candidate 인자면 전 항목 `stale`. (f) `observed_evidence_level=fixture`면 `evidence_level`. (g) acceptance 불일치면 `acceptance_mismatch`. 모두 exit 1이고 issue kind가 정확해야 한다.
   - `tests/gates/harness/aggregate.py validate()`(G4-EVIDENCE): Root가 records와 **독립적으로** expected context를 고정한다. items는 PLAN gate_items의 CW-16 7개(owner CW-16, runtime), candidate_id C16, requirements_digest, approval_digest(bundle), input_manifest는 `src/** omp_bridge/** tests/** pyproject.toml docs/features/core-workbench/{PLAN.json,SPEC.md,BRIEF.md,DECISIONS.md}`의 sha256이다(산출물인 VERIFICATION/COMPATIBILITY는 제외). evidence는 item별 observed JSON의 {path, sha256}이다. records는 observed items에서 변환한다(`evidence_kind:"actual_runtime"`). 양성은 7개 id를 반환해야 한다. 음성 대조: record 누락이면 "missing, duplicate or unowned", tmp root의 입력 1개를 변경하면 "current integrated input changed", unknown이면 "failed check or required unknown", candidate 변경이면 "changed candidate_id", runtime item에 fixture면 "insufficient evidence level"/"fixture-only runtime evidence", evidence sha 불일치면 "evidence content changed/missing", pin된 JSON에 `fixture` 키가 있으면 거절된다.
4. 문서 쓰기는 후보를 바꾼다. 순서는 gate 판정 → VERIFICATION/COMPATIBILITY 작성 → `workflow_tools.py audit(before=C16 snapshot, after, allowed=[두 문서])`로 문서만 바뀌었음을 기록한다. aggregate input_manifest는 두 문서를 포함하지 않으므로 그대로 유효하다.

## 6. Batch(dispatch 단위)

**단계 규칙**: W 단계(B1~B3 작성과 비공식 자기 실행) → Root가 C16 동결 → R 단계(B4가 모든 정식 check를 C16에서 실행, 파일 변경 0) → reviewer → Root 문서·gate. R 단계에서 테스트를 고쳐야 하면 새 후보가 되고 R 전체를 다시 실행한다(모델 0이므로 비용은 시간뿐).

| Batch | 역할 | 산출물(쓰기 범위 `tests/integration/**`) | 덮는 item |
|---|---|---|---|
| B1 harness+compat (Opus test_designer) | 공통 harness, outer adapter 3종, COMPAT 6회, SEL1–3 묶음, 기존 `test_plain_tmux_and_herdr_outer_matrix`를 제품 경로 version-record 방식으로 교체(구 probe는 historical 주석), canonical matrix 유지 | `cw16_harness.py`, `test_cw16_runtime_matrix.py`(갱신), `live_cw16_compat.py` | P-C-AC-19, P-C-AC-20, I-COMPAT |
| B2 flow+policy+shell (Opus test_designer) | F1–F7, P1–P8, SH1–SH5(bash·dash), clock fixture suite 연결 | `live_cw16_flow_policy.py`, `live_cw16_shell.py` | I-FLOW, I-POLICY, I-SHELL |
| B3 fault+VM (Opus test_designer, host:reboot-vm) | X1–X7, L-CW19-RESTART 재실행 wrapper, VM driver 고정, manifest 대조, (D1 승인 시) SEL4 zsh | `live_cw16_fault.py`, `vm_reboot_driver.py`, `vm_zsh_select.py` | I-FAULT(+P-C-AC-20 zsh) |
| B4 formal run+validator+real model (test_designer) | C16에서 정식 check 3~4개(compat / flow-policy-shell / fault / regression 전 suite + node + `run-contracts.sh`), validator 시험, RM1–4 | `test_cw16_final_evidence_validators.py`, check execution/observed 초안, smoke 보고 | 7개 전부의 정식 근거, G4 |
| Review (fresh Opus reviewer) | 동결 C16의 tests/integration·records·판정 대조. 필요하면 delta 1회 | — | — |
| Root 마감 | gate, audit, VERIFICATION/COMPATIBILITY, 71개 전체 연결(§8), PLAN 상태 | 문서 2개, `gate-p27-cw16-final-*.json` | — |

B1~B3의 W 단계 작성은 서로 다른 파일이라 병렬로 할 수 있다. 다만 harness는 B1이 먼저 확정한다. runtime 실행은 exclusive resource 때문에 **직렬**이다(VM은 host:reboot-vm라 B2와 겹쳐도 되지만 timing flake 위험 때문에 권장하지 않는다). 알려진 flake(p27b 3초 pwd 대기, UI `test_last_fed_chunk_is_drawn_promptly`, p27c XFSZ, connection_cap EAGAIN, product_pty send_failure race, Ctrl-C flood 1회)는 1회 단독 재실행으로 귀속하고 그대로 기록한다.

## 7. 문서 구조

**VERIFICATION.md**(교체; 상태 줄은 근거가 모두 갖춰질 때만 완료로 쓴다)
1. 상태: candidate C16, gate 결과, 날짜.
2. 결론: 7개 item 판정, 남은 pending/unknown.
3. 전제: CW-15/CW-19 통합 기록, UR-UX·UR-USABILITY(인용, 날짜), C-D55 범위 제외.
4. 정식 check 표: check_id, argv, cwd, candidate before/after, exit, timed_out, log sha, 환경.
5. 최종 integration item 표: item, 시나리오, evidence 파일과 sha, 결과, unknowns.
6. C-AC-01~34 추적표: local supplier/owner, final item, 시나리오 ID, evidence, 상태.
7. 재사용 근거와 영향 대조: VM manifest 일치 여부, historical 목록(p2.6 6dc51f, 18.2.10 probe).
8. G4 validator 결과: workflow gate와 aggregate 양성 1건, 음성 대조 표.
9. 71개 전체 집합 연결: item별 current/impact-compared/stale(§8).
10. 실제 모델 smoke: 확신용, 요청 수/cap, 관찰.
11. pending·unknown과 완료를 막는 항목.

**COMPATIBILITY.md**(교체)
1. 검증 환경 표: OS/kernel, Python, OMP 18.8.0(host), OMP 18.2.10(VM guest, 재부팅만), Bash 5.2.21, dash 0.5.12, tmux 3.4, Herdr 0.9.3, TERM/COLORTERM, PTY 크기.
2. Outer matrix(행: plain/tmux/herdr × host Bash/dash. 열: 입력·focus/owner, OMP 키, 한글·wide, paste/거절, resize, OSC 52, UI detach≥60s 재attach, outer client 재접속, 정리). 셀에 통과/해당 없음/미실행을 쓴다.
3. Shell matrix: Bash 우선, Bash 없음→dash, zsh 기본/login(실제 VM 또는 fake sentinel만, 구분해 표기), 둘 다 없음 안내, SH1–SH5 bash/dash.
4. OMP 버전별 판정: 18.8.0 제품 경로 검증, 18.2.10은 G1/G3 feasibility historical, 18.4~18.7은 중간 smoke historical.
5. 미검증: 실물 GUI terminal의 font/RGB/IME fidelity(pyte 에뮬레이션만), 갑작스런 전원 차단(system_reset), macOS/Windows, 다른 tmux/herdr 버전, OMP approval prompt(D5).
6. 지원 판정: 실제 실행한 조합만 지원으로 쓴다. 내부 multiplexer는 쓰지 않는다.

## 8. "CW-16 완료 = 전체 집합"(71 gate_items)

PLAN.gate_evidence_contract.evaluation_scope는 "CW-16 완료는 전체 집합"이다. 7개 소유 item이 통과해도 나머지 64개(M0, G1×7, G2×6, G3×6, G4×5, P-C-AC×32, L×7)를 C16에 연결해야 한다. 제안은 다음과 같다. B4가 C16에서 전 suite(fixture 수준 item)를 실행한다. Root는 runtime 수준 item마다 (i) C16 최종 시나리오가 같은 행동을 runtime으로 재관측했는지(대부분의 P-C-AC·L-*), (ii) 입력 경로 diff로 영향 대조가 가능한지를 표로 만든다. 그 뒤 aggregate를 71개 전체에 진단 실행해 거절 목록을 공개한다. **G1·G3의 18.2.10 고정 live probe는 로컬에서 재실행할 수 없다**(18.2.10 미설치이고 probe는 CW-16 쓰기 범위 밖). 이 항목들은 §9-D2·D3의 결정 없이 통과로 기재하지 않는다.

## 9. 사용자/Root 결정 필요

- **D1 zsh 실제 기본 shell**: (a, 권장) VM guest에 zsh를 설치하고 login shell로 바꿔 SEL4 실행. (b) host에 `sudo apt install zsh`. (c) fake sentinel 근거만 두고 SEL4를 pending으로 둔다. 이 경우 P-C-AC-20에 unknown이 남아 gate는 실패한다.
- **D2 OMP 버전**: (a, 권장) 18.8.0을 최종 검증 버전으로 하고, 18.2.10 pin 근거는 historical, g3_omp 고정 테스트 1건은 "환경"으로 기록한다. (b) 18.2.10을 격리 prefix에 설치하거나 VM guest(18.2.10)에서 G1/G3 고정 probe를 재실행한다. 이 경우 VM 용도가 확장된다. (c) Root가 고정 probe를 version-record 방식으로 바꾸는 test delta를 승인한다(CW-16 쓰기 범위 밖이라 PLAN 변경이 필요하다).
- **D3 전체 집합 해석**: 71개 모두 C16 current 근거를 요구할지, 아니면 7개 소유 gate와 영향 대조 연결표로 완료할지 정해야 한다. 전자이면서 D2 (a)를 택하면 완료할 수 없다.
- **D4 worker 직접 요청 3경계**(기존 범위/새 작은 명세/범위 확대): SPEC 6항은 "기존 명세나 범위 안의 작은 새 명세에 연결"이라고 한다. 반면 C-D69(6)(b)는 "Task 없이 사용자와 직접 대화하면 제한하지 않음"이라고 한다. 범위 확대 때 제품이 무엇을 해야 하는지(manager 보고 / 거절 / 무제한) 기대값이 문서로 고정돼 있지 않을 수 있다. B2 착수 전에 Root가 대조하고, 공백이면 사용자에게 묻는다.
- **D5 OMP approval prompt**(v-cw06 carried, P-C-AC-14): 제품 구성(worker `--tools`, manager 기본)에서 approval prompt가 나타나지 않으면 "해당 없음(근거 첨부)"으로 기록할지 정해야 한다.
- 실제 모델 cap 40(pause 36)은 사용자 승인("with caps") 범위 안에서 Root가 정할 기본값이다.

## 10. 추정

- 실제 모델 요청: B4 RM1–4 예상 약 32, **cap 40**. 나머지는 scripted라 0이다.
- child 호출: B1·B2·B3·B4 test_designer 4회, fresh reviewer 1회, correction·delta 예비 2~3회로 **합계 7~8회**.
- wall time(R 단계): compat 약 20분, flow/policy/shell 약 40분, fault 약 25분, VM 약 15분(재사용하면 0), 전 suite 약 15분, 실제 모델 약 20분. 총 약 2~2.5시간이다.
