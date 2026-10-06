# p27-cd69-smoke-03: C-D69 (6) 실모델 smoke 결과 (manager가 명령어 작성)

- 역할: test_designer(Sonnet). 2026-10-07 01:49~01:53 KST(약 5분/20분). 코드: candidate 24d994e(현재 HEAD d52df9d는 docs만 추가, `src/`·`omp_bridge/` 차이 0). omp/18.6.1. Python `/tmp/cw02-g1-venv/bin/python`.
- **실행은 2회입니다.**
  - **run A(smoke-03a):** 제품 흐름은 끝까지 갔지만, 내 scratch 드라이버가 Task 종료 뒤 pyte 화면 렌더링에서 예외(`wcwidth` IndexError)로 죽었습니다. 제품 결함이 아닙니다. 그 시점에 manager의 최종 답변 turn이 진행 중이어서 최종 답변·후속 명령·shutdown 검증을 얻지 못했습니다.
  - **run B(smoke-03b):** 드라이버의 화면 렌더링만 고쳐 같은 시나리오를 다시 돌렸습니다(코드·절차 동일, 새 `/tmp` data dir·project). 최종 답변, 후속 명령, verified shutdown까지 완료했습니다. 아래 "비교 기준"은 run B입니다.
- 결론 요약
  - **manager가 명령어를 만들고 worker가 그대로 실행했습니다.** 두 run 모두 `to_worker`에 `commands`(1개, run A 1930자 / run B 1604자 heredoc 스크립트)가 실렸고, worker는 `terminal`을 1회 호출했으며 명령 문자열이 `commands[0]`과 **정확히 같았습니다**(양쪽 모두). `not_in_task_commands` 거절 0건, 임시 스크립트 spill 0건(길이 한도 미만), `command_too_long`·`start_failed` 0건.
  - **worker 보고가 짧아졌고 명령 원문을 다시 쓰지 않았습니다.** 보고 메시지 874자(A) / 1110자(B)이고, 스크립트·명령 원문은 없습니다. 실행 명령 목록은 하네스가 `commands_run`으로 붙였습니다(명령 첫 줄 + `…`, 종료 코드 0, 0.12 s, 로그 경로).
  - **시간이 크게 줄었습니다.** smoke-02 대비 TASK→첫 `terminal` 53.8 s → **5.9 s**, TASK→done 89.5 s → **11.8 s**, 사용자 입력→manager 최종 답변 123.9 s → **53.6 s**(run B).
  - 후속 명령 `echo WB_FOLLOWUP_$((6*7))`이 동작했습니다(`WB_FOLLOWUP_42`). 다만 host pane 표시 이상이 smoke-02에 이어 재현됐습니다(아래 O1).
- provider request(세션 jsonl의 assistant message): run A **7**(manager 3 + worker 4, manager 최종 turn은 shutdown 때 진행 중이라 기록 안 됨, 청구됐다면 +1), run B **9**(manager 4 + worker 5). **합계 16(+최대 1)/30**, error/aborted 0, cap 미도달.
- 방법은 smoke-02와 같습니다: 새 `/tmp/wb-cd69-smoke3[b]-<rand>/{data,project}`(project는 git repo)에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home), product UI(`workbench attach`)를 PTY(pyte 170x40)에 붙여 manager pane에 입력, `workbench status --json`과 temp 세션/handoff jsonl을 약 1 s 간격으로 관측. request 수는 Workbench home의 manager·worker 세션 jsonl assistant message 수입니다. Workbench 프로세스 env에서 `DISPLAY`, `WAYLAND_DISPLAY`, `TMUX*`, `WORKBENCH_*`를 뺐습니다(값은 보지 않음). 드라이버는 p27w(`tests/backend/live_cw18_independent_p27w.py`)의 `Ui`/`processes_mentioning`/`kill_exact`를 import한 scratch 스크립트이며 실행 후 repo 밖에 두었습니다.
- 짧은 로그: `result-p27-cd69-smoke-03-timeline.json`(경로는 `<root>`로 줄임, 세션은 구조만: 블록 종류·길이·도구 이름·인자 크기·출력 토큰). secret 없음.

## Isolation / 모델 (두 run 동일)
| 역할 | start 결과 | 관측 model | 설정 |
|---|---|---|---|
| manager | `omp isolation: ok`(`omp/18.6.1`) | `gpt-6.1-sol` | thinking high |
| worker | 〃 | `gpt-6-luna` | thinking max, service tier `priority` |

`agent.db`는 내용을 읽지 않았습니다. start 직후 세 pane(host_shell, manager_omp, worker_omp) 모두 `alive=True owner=user`, shell mode `manual_prompt`.

## Timeline (run B, KST / 입력 = 0 s)
| 시각 | 경과 | req | 관측 |
|---|---|---|---|
| 01:51:38 | – | 0 | start exit 0, phase ready, isolation ok |
| 01:52:16.05 | 0 | 0 | manager pane에 사용자 원문 입력 |
| 01:52:18.85 | 2.8 s | 1 | manager `read`(skill://to-worker, 10.7 KB) |
| **01:52:45.02** | **29.0 s** | 2 | manager 텍스트 85자 + `to_worker{kind: work, analysis: summary, commands[1], message 309자, spec{goal, instructions, paths: []}}` → `dispatched`. Task `실행 중`, worker `작업 중` |
| 01:52:45.14 | 29.1 s | 2 | worker에 TASK 메시지(3293자) 도착 |
| 01:52:47.40 | 31.3 s | 3 | manager 텍스트 64자로 turn 종료(직접 실행 없음) |
| 01:52:47.94 | 31.9 s | 4 | worker `read`(skill://to-manager) |
| **01:52:51.07** | **35.0 s** | 5 | worker `terminal`(1604자, `commands[0]`과 동일) → 0.12 s, exit 0 |
| **01:52:56.99** | **40.9 s** | 6 | worker `to_manager{kind: done, 1110자}` → Task 종료(완료), worker `대기`, 자동화 idle |
| 01:52:58.69 | 42.6 s | 7 | worker 두 번째 `to_manager done`(42자) → `rejected: no_active_task`(전달 안 됨) |
| 01:53:00.29 | 44.2 s | 8 | worker 빈 text turn 종료 |
| **01:53:09.68** | **53.6 s** | 9 | manager가 사용자에게 최종 답변(943자) |
| 01:53:32 | – | 9 | host pane 포커스 후 `echo WB_FOLLOWUP_$((6*7))` → `WB_FOLLOWUP_42`, 상태줄 `host 입력 owner: user · manual_prompt` |
| 01:53:36 | – | 9 | `shutdown --yes --json` exit 0, **`verified: true`**(host_shell·manager_omp·worker_omp·supervisor 모두 dead, survivors 0) |
| 01:53:38~39 | – | 9 | 소유 프로세스 잔여 0, project `git status` 깨끗함, temp root 삭제 확인 |

run A(참고, UTC 16:49:43.8 입력): `to_worker` 27.6 s, TASK→`terminal` 14.0 s, TASK→done 24.4 s, 보고 manager 수신 52.2 s. 최종 답변은 shutdown 때문에 얻지 못했습니다.

## Provider request 수 (세션 jsonl assistant message)
| run | manager | worker | 합계 | 비고 |
|---|---|---|---|---|
| A | 3: `read` · `to_worker`(+텍스트) · 텍스트 | 4: `read` · `terminal` · `to_manager done` · 텍스트 306자 | 7 | manager 최종 turn 진행 중 shutdown |
| B | 4: `read` · `to_worker`(+텍스트) · 텍스트 · 최종 텍스트(943자) | 5: `read` · `terminal` · `to_manager done` · 중복 `done`(rejected) · 빈 텍스트 | 9 | error/aborted 0 |

smoke-02는 8(manager 4 + worker 4)이었습니다. worker request는 4~5로 비슷하고, 줄어든 것은 요청 수가 아니라 요청당 생성량입니다(아래).

## 시나리오 1 관측

### manager의 `to_worker` (secret 없음)
| | smoke-02 | run A | run B |
|---|---|---|---|
| `commands` | 없음(절차를 message에 서술) | 1개, 1930자 | 1개, 1604자 |
| `message` | 1006자(명령 목록·대체·멈출 조건·돌려받을 값) | 510자(영문, 제약·돌려받을 값) | 309자(한국어, 제약·돌려받을 값) |
| `analysis` | summary | summary | summary |
| manager 입력→`to_worker` | 20.5 s | 27.6 s | 29.0 s |
| manager 출력 토큰(그 request) | – | 1015 | 1009 |

- `commands[0]`은 `bash <<'HARDWARE_CHECK' … HARDWARE_CHECK` heredoc 스크립트 하나입니다. 구간 제목 출력 + `command -v` 확인 + 종료 코드 기록 + 요청 항목(uname/os-release/DMI 일부/lscpu/nproc/free/lsblk/df/lspci/nvidia-smi 확인/systemd-detect-virt/cgroup 파일)을 담았습니다. 대체 명령 목록은 없고, 없는 명령은 스크립트가 "Unavailable"로 출력하게 했습니다. 시리얼·UUID·MAC 수집은 제외했습니다.
- message는 "그대로 실행·추가 명령 금지·불가 항목 보고·돌려받을 값"만 담았고 스크립트 설명을 되풀이하지 않았습니다. smoke-02의 "절차 서술 1006자 + worker가 스크립트 작성"이 "manager가 스크립트 작성 + 짧은 지시"로 옮겨졌습니다. 그 비용(manager 20.5 → 27.6~29.0 s, 약 +7~9 s)이 worker 쪽 절감(TASK→done 89.5 → 11.8~24.4 s)보다 훨씬 작습니다.
- 범위: 스크립트가 원 요청(하드웨어 스펙)보다 넓은 항목(swap, cgroup 한도, EFI 파티션 등)을 포함했습니다. manager 판단 범위이고 worker는 넓히지 않았습니다.

### worker의 TASK 메시지 (run B 3293자 / run A 3987자)
- payload 키: `analysis`, `analysis_rule`, `commands`(`[{command, number}]`), `commands_rule`, `goal`, `handoff`, `instructions`, `kind`, `message`, `paths`, `revision`, `task_id`. 명령은 잘림 없이 그대로 들어갔습니다.
- `commands_rule`: "Run these commands exactly as given with the terminal tool, one per call, in order (number 1 first); use an alternative entry only as the message says. Do not write, change, split or combine commands: while this Task is active Workbench refuses any other command. Then report a short summary of the results (key values, failures); Workbench attaches the list of commands run, so do not paste commands or scripts back."
- `analysis_rule`: smoke-02와 같음("run only the given steps and allowed fallbacks; report facts and a short summary; …").

### worker의 도구 호출
| run | 시각(UTC) | 도구 | 결과 |
|---|---|---|---|
| B | 16:52:47.9 | `read` skill://to-manager | ok(8 KB) |
| B | 16:52:51.07 | `terminal` 1604자(`commands[0]`과 동일) | `exited`, exit 0, 0.123 s, `terminal_notice: not_needed` |
| B | 16:52:56.99 | `to_manager done` 1110자 | `queued` → 보고 manager 수신, Task 종료(완료) |
| B | 16:52:58.69 | `to_manager done` 42자("Hardware findings reported to the manager.") | `rejected: no_active_task` |
| A | 16:50:25.55 | `terminal` 1930자(동일) | `exited`, exit 0, 0.122 s |
| A | 16:50:35.98 | `to_manager done` 874자 | `queued` → Task 종료 |

- **정확 일치:** 양 run의 `terminal.command == commands[0]`(문자열 동일). 거절·재시도·분할·결합 0건. `write` 도구, `bash`/`eval`, subagent 사용 0건.
- **spill:** 두 명령 모두 1.9 KB 이하라 임시 스크립트 spill 경로는 실모델로 **미검증**입니다(이 시나리오는 한도를 넘지 않는 크기). `command_too_long`·`start_failed` 경로도 발생하지 않아 미검증입니다.
- **시간 분해(B):** TASK 도착 → skill 읽기 2.8 s → `terminal` 5.9 s(thinking 0, 출력 605 토큰) → 실행 0.12 s → `done` 작성 5.9 s(출력 652 토큰). smoke-02는 스크립트 작성 45.6 s, 보고 작성 35.5 s였습니다. 보고에서 스크립트 원문(2.7 KB)을 다시 쓰던 부분이 사라졌고, 명령 작성은 manager로 이동했습니다.

### worker 완료 보고
- B(1110자, 영어): 실행 사실("Read-only hardware script completed (exit 0)")에 이어 CPU/메모리/GPU/저장장치/파일시스템/OS·kernel/가상화/cgroup 값과 제한(DMI `Default string`, VRAM·드라이버 미확인, `nvidia-smi` 없음, cgroup 파일 없음)을 한 문단으로 정리했습니다. 해석·권고·추가 조사 없음. "documented script label notes exit 1 for none", "indicating no detected virtualization" 같은 짧은 해석이 한두 군데 있습니다.
- A(874자, 한국어): bullet 8개 + 한 줄 요약. GPU bullet에 "`lspci` 이용 가능" 같은 군더더기 한 줄이 있습니다. worker는 `done` 뒤 사용자에게 보이는 일반 텍스트 306자도 썼습니다(manager에게 전달되지 않는 pane 출력).
- 명령·스크립트 원문 반복: **없음**(두 run). 하네스가 붙인 `commands_run`(manager가 받은 payload): `[{command: "bash <<'HARDWARE_CHECK' …", duration_seconds: 0.123, exit_code: 0, log_path: "<root>/data/workflow/terminal/<id>.log", signal: null, status: "exited"}]` + `commands_run_note`("Recorded by Workbench from the terminal journal: …"). 보고 수신 payload 전체 2101자(B) / 1873자(A)로, smoke-02의 worker 보고 3837자보다 작습니다.

### manager의 최종 답변 (run B, 943자)
- "터미널에서 읽기 전용 하드웨어 확인 스크립트를 실행했습니다. **Intel Core i7-1165G7 / RAM 약 30 GiB / Intel Iris Xe / 500GB급 NVMe SSD** 구성입니다." 뒤에 표(CPU, 메모리, GPU, 저장장치, 루트 파일시스템, 스왑, OS, 커널/아키텍처, 가상화, 실행환경 CPU) + `확인 한계` 3개(시스템 모델 `Default string`, GPU 메모리·드라이버·`nvidia-smi` 없음, cgroup 한도 파일 없음) + 변경 없음 안내.
- worker 보고 값과 일치하고 추정값 없음. 스크립트 원문은 사용자 답변에 다시 넣지 않았습니다. 사용자용 정리는 manager가 했습니다(C-D69 (2) 분담과 일치).
- run A는 위 이유로 얻지 못했습니다.

## 시나리오 2: host shell
- start failure가 없어 "실패 뒤 shell 반환"은 이번에도 실모델로 미검증입니다(단위·독립 테스트 범위).
- Task 종료 뒤 상태줄 `host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle`, host pane 포커스 후 후속 명령이 동작했습니다(`WB_FOLLOWUP_42`).

## 상태줄 (product UI, run B)
```
[01:52:16 입력 전]   focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
[01:52:45 dispatched] focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
                      작업: 작업 "아래 읽기 전용 스크립트를 터미널에서 그…" [실행 중] | backend: ready | bridge manager=ok worker=ok
[01:52:57 종료]      focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
                      작업: 작업 "아래 읽기 전용 스크립트를 터미널에서 그…" [종료(완료)] | backend: ready | bridge manager=ok worker=ok
[01:53:32 후속 명령]  focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
```
- Task 이름이 사용자 원문이 아니라 manager message 첫머리("아래 읽기 전용 스크립트를 터미널에서 그…")로 표시됩니다. smoke-02는 "사용자 요청: 현재 하드웨어 사양 확인 스…"였습니다. 실행 중 화면에는 manager가 쓴 지시문 첫 줄이 보입니다.

## 동작한 것 (실모델)
- C-D69 (6)(a): manager가 `to_worker`에 `commands`를 만들어 넘겼고(두 run) 자유 작업에서 `analysis: summary`와 함께 전달됐습니다.
- (b): TASK 메시지가 명령 그대로·순서대로·분할/결합 금지 규칙을 담았고, worker가 `terminal`에 명령을 문자 그대로 전달했습니다. 거절 필요 상황은 발생하지 않아 **거절 경로는 실모델로 미검증**입니다(단위 테스트 범위).
- (d): 완료 보고에 하네스가 `commands_run`(명령 첫 줄, 종료 코드, 걸린 시간, 로그 경로)을 붙였고 worker는 스크립트 원문을 다시 쓰지 않았습니다.
- 지연: TASK→done 11.8 s(B)/24.4 s(A), 입력→최종 답변 53.6 s(B). 사용자가 문제 삼은 "너무 많은 분석으로 늦게 전달"이 크게 줄었습니다.
- start/shutdown 정상(B verified), isolation ok, residue 0, request 합계 16(+1)/30.

## 동작하지 않은 것 / 관찰 / 분류
- **O1 (표시, 낮음, 원인 미확정, smoke-02에서 재현):** 후속 명령 뒤 host pane 마지막 줄들이 `WB_FOLLOWUP_42/memory.max: unavailable`(앞 출력 줄 위에 덮어씀) / `$ echo WB_FOLLOWUP_$((6*7))` 순서로 보입니다. smoke-02는 직후 한 장만 봐서 중간 프레임일 수 있다고 했는데, 이번에는 **3 s 뒤 프레임도 같습니다**(안정된 상태). 출력 줄이 프롬프트 줄 위로 그려지고 다음 프롬프트는 pane 아래로 밀려 보이지 않습니다. pyte 에뮬레이션과 pane 렌더링의 차이일 가능성은 배제하지 못했습니다(실제 터미널 확인 없음). 직전 출력이 줄바꿈 없이 끝났고 pane이 가득 찬 상태에서 발생합니다. 수정 필요 여부는 Root 판단입니다.
- **O2 (모델 행동, 낮음):** worker가 `done` 성공 뒤 두 번째 `done`(42자)을 보내 `rejected: no_active_task`가 났습니다(run B). 하네스가 거절해 영향은 없고, run A에는 없었습니다. cd68 smoke-01의 중복 done과 같은 종류입니다. worker는 `to_manager` 뒤 빈 텍스트(B) 또는 306자 텍스트(A)로 turn을 끝내기도 했습니다.
- **O3 (기존, 낮음):** run A에서 Task 전달 outbox가 20 s 뒤 `unknown / BridgeTimeout`으로 한 번 기록됐습니다(run B는 `omp_processed`). 전달은 1회 정상이었습니다. smoke-02와 같은 기록입니다.
- **O4 (비용 이동, 정보):** 명령 작성이 manager로 옮겨가서 manager의 `to_worker` request가 20.5 → 27.6~29.0 s, 출력 약 1000 토큰입니다. 전체 지연은 줄었지만 manager 쪽에 스크립트 작성 부담이 생겼습니다. 스크립트가 원 요청보다 넓은 항목을 포함하는 경향은 남아 있습니다.
- **O5 (관측 한계):** spill, 거절(`not_in_task_commands`), 여러 명령/대체 명령 순서(`number` 2 이상), `command_too_long`/`start_failed` 이후 shell 반환은 이번 시나리오에서 발생하지 않아 실모델로 확인하지 못했습니다.
- **run A 중단 원인(내 도구 측):** scratch 드라이버가 pyte `Screen.display`에서 `wcwidth` 예외를 냈습니다(한글 와이드 문자 셀 처리). 드라이버를 셀 단위 렌더링으로 바꿔 run B에서 해소했습니다. run A의 `shutdown`은 `active work: manager omp_turn`을 보고하며 exit 1이었지만(stderr 미기록), 이후 소유 프로세스 잔여 0, project git status 깨끗함, temp root 삭제를 확인했습니다. shutdown 검증은 run B의 `verified: true`가 근거입니다.

## Residue
- 프로세스: run B `shutdown --yes --json` exit 0, `verified: true`, 소유 프로세스 잔여 0, 강제 kill 없음. run A도 잔여 0(`processes_mentioning` 기준, kill 불필요).
- 파일: `/tmp/wb-cd69-smoke3-nhl3jj0g`, `/tmp/wb-cd69-smoke3b-ai_7nlbp` 삭제 확인(`ls /tmp/wb-cd69-smoke3*` 없음). worker는 `write`를 쓰지 않았습니다. scratch의 driver·세션 사본·화면 캡처·로그는 repo 밖(Claude scratchpad)에만 있고 run dir에는 이 파일과 `result-p27-cd69-smoke-03-timeline.json`만 추가했습니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스, 클립보드는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. 세션 jsonl의 `credential_pin` 레코드는 결과에 옮기지 않았습니다. commit, graphify update, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. C-D69 (6)의 정상 경로(manager 작성 `commands` → worker 문자 그대로 실행 → 요약 보고 + 하네스 `commands_run`)는 실모델에서 두 번 동작했고, 지연이 smoke-02 대비 크게 줄었습니다. 지연 기준의 만족 여부는 사용자 판단이 필요합니다.
2. 실모델로 확인되지 않은 경로(spill, 거절, 다중 명령, 시작 실패 뒤 shell 반환)는 단위·독립 테스트 근거에 의존합니다. 필요하면 명령이 길거나 여러 개인 시나리오로 추가 smoke를 하나 더 설계할 수 있습니다(추가 request는 승인 범위 확인 필요).
3. O1(host pane 표시, 2회 재현)은 실제 터미널에서 재확인하거나 tmux/pane 렌더링 테스트로 원인을 가르는 것을 권합니다.
