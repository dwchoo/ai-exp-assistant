# p27-cd68-smoke-03: C-D68 (10) 이후 실모델 smoke 결과 (terminal_check 경로)

- 역할: test_designer (Opus). 실행 시각 2026-10-06 01:06:20~01:12:30 KST(약 6.2분/20분). 1회 실행, 재시도 없음.
- 결론: **`terminal_check`가 실모델에서 처음으로 전달됐고, worker는 사양대로 반응했습니다.** 첫 대기는 119.96 s에 `running`으로 끝났고, worker는 짧은 텍스트 한 줄(44자)로 turn을 끝냈습니다. empty stop, `wait` 호출, 재대기는 없었습니다. 그로부터 60 s 뒤(+180 s) `terminal_check: sent`(new output tick 13~19)가 전달됐고, worker는 도구 호출 없이 한 줄(80자)로 응답했습니다. 보고는 보내지 않았습니다. 명령이 끝나자(+240.1 s) `terminal_done`이 전달됐고 `done`이 1회 보고됐습니다. 이어 Task `closed(done)`, worker `idle`, shell은 user에게 반환됐습니다.
- provider request: **10/30**(manager 4 + worker 6). error/aborted 0, cap 미도달.
- 미검증: 명령 없는 `terminal` 호출(C-D68 (10) 즉시 응답)은 worker가 한 번도 하지 않아 실모델에서 발동하지 않았습니다(`terminal_fetch` 0건, packet의 "if any"에 해당).
- 진입점: smoke-01/02와 같습니다. 새 `/tmp/wb-cd68-smoke3-<rand>/{data,project}`(project는 git repo)를 만들고, project dir에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home)를 실행했습니다. 이어 product UI(`workbench attach`)를 PTY(pyte, 170x40)에 붙여 manager pane에 입력했습니다. 관측은 `workbench status --json`, temp `workflow/handoffs.jsonl`(안전 필드만), temp 세션 jsonl(구조만)로 약 1 s 간격으로 했습니다.
- 코드: HEAD 003106c(0c9c774 뒤의 docs commit, `src/`·`omp_bridge/` 차이 0). omp/18.6.1. Python `/tmp/cw02-g1-venv/bin/python`.
- 드라이버: p27w의 `Ui`/`processes_mentioning`/`kill_exact`를 재사용한 scratch 스크립트입니다(28 request에서 pause, 30 또는 19분에서 중단). 실행 후 삭제했습니다.
- 짧은 로그: `result-p27-cd68-smoke-03-timeline.json`(경로는 `<root>`로 줄임, 마지막 부분에 세션 구조, secret 없음)

## Isolation (start 시 check)
| 역할 | state | leaks | warnings | 관측 model |
|---|---|---|---|---|
| manager | ok | 0 | 0 | `gpt-6.1-sol`(assistant 4건 모두) |
| worker | ok | 0 | 0 | `gpt-6-luna`(assistant 6건 모두) |

start 출력은 `omp isolation: ok`이고, `agent.db`는 symlink입니다(basename만 확인, 내용은 읽지 않음).

## Timeline (KST, 명령 시작 = T)
| 시각 | req | 관측 |
|---|---|---|
| 01:06:22 | 0 | start exit 0, phase ready, isolation ok |
| 01:06:24 | 0 | product UI attach `worker: 대기 · 자동화: idle` |
| 01:07:17 | 0 | manager에 지시 입력 완료(free-work, 240 s 명령, "직접 실행하지 말 것"). PTY 한 글자씩 입력이라 53 s 걸림 |
| 01:07:20~30 | 2 | manager `read`(skill) → `to_worker{kind: work, task_id: null}` → Task `work`/`running`, worker `busy` |
| 01:07:35 | 3 | manager text(118자)로 turn 종료. 직접 실행 없음 |
| 01:07:36 | 4 | worker `read`(to-manager skill) |
| **01:07:44 (T)** | 5 | **worker `terminal{command}`**(인자 키는 `command`뿐) → `terminal_request(wait_seconds 120)` → `terminal_started`(cwd `<root>/project`). shell `manager`/`control_wait`, UI는 `host 입력 owner: worker` |
| 01:07:50 | 5 | journal `outbox status: unknown, reason: BridgeTimeout`(Task 전달의 처리 확인 대기 20 s 초과, 아래 O2) |
| 01:07:54 | 5 | host pane `$ wb-handoff` → **`[worker] $ for i in … echo DONE .`** → `tick 1`, 제목 `HOST SHELL … owner=worker` |
| **01:09:44 (T+120.0)** | 5 | **첫 대기 종료: `terminal_result status: running`, `elapsed_seconds: 119.962`**(output_tail tick 1~12) |
| **01:09:49** | 6 | **worker가 thinking 30자 + text 44자("Waiting for the Workbench completion notice.")로 turn 종료(`stop`).** 도구 호출 없음, empty stop 재시도 없음 |
| **01:10:45 (T+180)** | 6 | **journal `terminal_check outcome: sent`, `coalesced_count: 0`, `new_output_bytes: 63`.** worker 세션에 user 메시지(1016자, `terminal_check`/`running` 포함)가 01:10:45.004에 들어감 |
| **01:10:48** | 7 | **worker가 도구 없이 text 80자로 응답: "The command is still running; ticks 13–19 have appeared. Waiting for completion."** 보고 없음 |
| 01:11:45 (T+240.1) | 7 | `terminal_ended`(exited, exit 0, 240.135 s) → `terminal_notice: pending` → `sent`(같은 초). shell `user`/`manual_prompt` 반환. 두 번째 check는 없었음(종료와 같은 시점) |
| 01:11:49 | 8 | worker `to_manager{kind: done}` → `queued` → Task `closed(done)`, worker `idle`, 자동화 `idle` |
| 01:11:53~54 | 10 | worker text 35자로 turn 종료("Reported completion to the manager."). manager가 보고를 받아 사용자에게 완료를 알림(text 145자), outbox `omp_processed` |
| 01:12:24 | 10 | 30 s 동안 추가 request 없음 |
| 01:12:27 | 10 | `shutdown --yes --json` exit 0(코드상 `verified: true`일 때만 0) |
| 01:12:30 | 10 | 소유 프로세스 잔여 0, temp root 삭제 확인, project `git status` 깨끗함 |

## Provider request 수 (세션 jsonl assistant message)
| OMP | request | content 형태 |
|---|---|---|
| manager | 4 | `read` · `to_worker`(dispatched) · text 118자 · text 145자(완료 보고) |
| worker | 6 | thinking+`read` · thinking+`terminal{command}` · thinking+**text 44자**(running 후 종료) · thinking+**text 80자**(check 반응) · `to_manager done`(queued) · thinking+text 35자 |
| 합계 | **10** | error/aborted 0, subagent 0. smoke-02(12)보다 2건 적음(`wait`·재대기·중복 report 없음) |

## Journal의 terminal 기록 (전부)
| 시각 | type | 값 |
|---|---|---|
| 01:07:44.796 | `terminal_request` | worker, wait_seconds 120, task 있음 |
| 01:07:44.835 | `terminal_started` | cwd `<root>/project` |
| 01:09:44.796 | `terminal_result` | running, 119.962 s |
| 01:10:44 | `terminal_check` | **sent**, coalesced 0, new_output_bytes 63 |
| 01:11:44.970 | `terminal_ended` | exited, exit 0, 240.135 s |
| 01:11:44 | `terminal_notice` | pending → **sent** |
| - | `terminal_fetch` | **0건** |

`terminal_check` 기록은 `sent` 1건뿐이고 `merged`/`deferred`/`skipped_paused`는 없습니다. worker가 idle이었으므로 맞습니다. new output 63 bytes는 PTY 줄끝(CRLF) 기준 `tick 13`~`tick 19` 7줄(7×9 bytes)과 맞습니다. worker가 읽은 범위(ticks 13–19)와도 같습니다. 첫 대기의 output_tail(tick 1~12)과 겹치지 않으므로 "지난 확인 이후 새 출력"이 맞게 잘렸습니다.

## 동작한 것 (실모델)
- **C-D68 (8) check 전달:** 대기 중인 호출이 없을 때 첫 대기 반환 +60 s에 check 1건이 전달됐습니다(기록 01:10:44, 세션 도착 01:10:45.004). 내용은 새 출력만 담았습니다.
- **check에 대한 worker 반응:** CHECK_INSTRUCTION대로 도구 호출, 보고, 재대기 없이 한 줄 텍스트로 끝냈습니다(+1 request).
- **C-D68 (10) / RUNNING_DETAIL 수정(smoke-02 P3·M4 대응):** `running`을 받은 worker가 한 줄 텍스트로 turn을 끝냈습니다. empty stop(OMP 자동 재시도)이 없었고 `wait` 도구 호출도 없었습니다(worker에 `wait` 도구가 없음). 반복 `terminal` 호출도 없었습니다.
- **완료 처리:** 대기 중인 호출이 없었으므로 `terminal_notice: pending → sent`(1회, 중복 없음)였고, `terminal_done` 메시지가 worker 세션에 1건 들어갔습니다. 이어 `done` 1회 → `closed(done)` → worker `idle` → shell 반환입니다. smoke-02의 M2'(done 뒤 추가 report)는 재현되지 않았습니다.
- **C-D68 (9) 유지:** 대기는 120 s 고정, host pane에 `[worker] $ <command>` 표시, 실행 중 owner `worker`(상태줄·pane 제목), 반환 뒤 `owner: user`, 진행 보고 0건.
- manager는 직접 실행하지 않고 보고를 기다렸습니다. start/shutdown 정상, isolation ok, residue 0.

## 동작하지 않은 것 / 관찰 / 분류
- **미검증: 명령 없는 `terminal` 즉시 응답(C-D68 (10)).** worker가 `command: null`을 호출하지 않아 `terminal_fetch` 기록이 0건입니다. 결함이 아니라 발동 조건이 생기지 않은 것이며, 결정론 테스트 근거로 판단해야 합니다.
- **O1 (제품 표시, 낮음~중, 새 관찰, C-D68 범위 밖):** manager pane에 `╰─ tmux;]777;notify;warp://cli-agent;{"event":"stop","query":"<사용자 입력 원문>","response":"<manager 응답 원문>"}` 같은 줄이 텍스트로 남았습니다(attach부터 종료까지 계속 보임, screenshot 아래). OMP가 turn 종료 때 보내는 OSC 777 알림이 tmux passthrough(DCS `ESC P tmux; …`)로 감싸여 나왔고, Workbench pane 화면이 이 시퀀스를 삼키지 않고 글자로 그린 것으로 보입니다. 이번 드라이버는 사용자 환경 변수를 그대로 넘겼습니다(`TMUX`, `TERM_PROGRAM=tmux`, `WARP_CLI_AGENT_PROTOCOL_VERSION` 등이 있음, 값은 확인하지 않음). 따라서 사용자가 tmux(+Warp) 안에서 Workbench를 띄우면 같은 일이 생길 수 있습니다. worker pane에서는 같은 줄을 보지 못했습니다. 수정 후보(Root 판단): pane 화면이 DCS/OSC passthrough를 버리게 하기, 또는 OMP 자식 프로세스에 `TMUX`/Warp 관련 변수를 넘기지 않기.
- **O2 (기존 동작, 낮음, 관찰):** Task 전달(`to_worker` → worker)의 outbox가 20 s 뒤 `unknown`/`BridgeTimeout`으로 기록됐습니다. 전달은 `delivery_omp_processed`(수신 OMP의 처리 완료)를 `deliver_timeout` 20 s 동안 기다립니다(`flow.py:651`, `mailbox.py:1026~1035`). 그런데 worker의 첫 turn이 `terminal`의 120 s 대기를 포함해 01:07:30~01:09:49 동안 이어졌습니다. 실제 전달은 1회 정상이었고(01:07:30.010 도착), 재전송이나 상태 이상은 없었습니다. 다만 C-D68 (9)의 120 s 고정 대기 때문에 "첫 turn에 terminal을 쓰는 Task"는 앞으로 항상 이 `unknown` 기록을 남깁니다. 기록의 의미만 Root가 확인하면 됩니다.
- **드라이버 부산물(제품 무관):** 지시 문장 끝의 " ."을 manager가 명령에 포함해 실제 명령은 `… echo DONE .`이었고 출력 끝이 `DONE .`이었습니다. 동작 판단에는 영향이 없습니다.
- 표시(낮음, 기존): host pane의 `[worker] $ …` 앞에 `$ wb-handoff` 줄이 여전히 보입니다(smoke-02와 같음).

## 상태줄·pane text screenshot (product UI)
```
[attach 01:06:24]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 01:06:24 (0s 전) | Ctrl-] ? 도움말

[T+8 s, 01:07:54, host focus]
focus: HOST SHELL | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active
작업: 작업 "host terminal에서 다음 명령을 정확히 그…" [실행 중] | backend: ready | bridge manager=ok worker=ok | …
lq HOST SHELL *FOCUS* alive owner=worker qqq…
x$ wb-handoff
x[worker] $ for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE .
xtick 1

[T+250 s, 01:11:56, host focus]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 작업 "host terminal에서 다음 명령을 정확히 그…" [종료(완료)] | backend: ready | bridge manager=ok worker=ok | …
lq HOST SHELL *FOCUS* alive owner=user qqq…
xtick 23
xtick 24
xDONE .
```

worker pane(T+125 s, `running` 결과 뒤 한 줄로 종료):
```
 • Terminal
  └─ command="for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE ."
 └ • status: "running"
   • log_path: "<root>/data/workflow/terminal/96ed151…"
   • elapsed_seconds: 119.962
   • output_tail: "tick 1 …
 Awaiting completion notice
 Waiting for the Workbench completion notice.
```

worker pane(T+185 s, check 반응):
```
 … (with no Task, tell the user in your reply); then end your turn with one short …
 Checking output
 The command is still running;
  ⎋ Working…
 The command is still running; ticks 13–19 have appeared. Waiting for completion.
```

worker pane(T+250 s, 완료 뒤):
```
 • To manager
  └─ kind="…"
 └ • status: "queued"
   • detail: "Accepted by Workbench; it is delivered to the other OMP onc…"
 Reported completion
 Reported completion to the manager.
```

manager pane(O1, attach 뒤 계속 보임, 왼쪽 열 일부):
```
x╰─ tmux;]777;notify;warp://cli-agent;{"event":"stop","query":"Workbench worker에게
x자유 작업(free-work) Task로 맡겨 주세요: host terminal에서 정확히 다음 명령을 실행
x고 끝나면 결과를 보고하게 해 주세요. 명령: for i in $(seq 1 24); do echo tick $i; s
xleep 10; done; echo DONE . 당신(manager)은 이 명령을 직접 실행하지 ","response":"Wo
xrker에게 free-work Task로 전달했습니다. …
```

## Residue
- 프로세스: `shutdown --yes --json` exit 0(`cli.py:288` 기준 `verified: true`일 때만 0. 드라이버가 JSON의 `shutdown` 하위 키를 읽지 않아 timeline의 `verified` 필드는 null로 남음). 소유 프로세스 잔여 0, 강제 kill 없음.
- 파일: `/tmp/wb-cd68-smoke3-nubdwq81` 삭제 확인(`/tmp`에 `smoke3` 항목 0). scratch의 driver, run log, timeline 원본도 삭제했습니다. run dir에는 이 파일과 `result-p27-cd68-smoke-03-timeline.json`만 남겼습니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. 세션 jsonl에서는 레코드 키 이름과 구조만 봤습니다(`credential_pin` 레코드의 값은 읽거나 기록하지 않음). 환경 변수는 이름만 확인했습니다. commit, graphify update, ledger 수정, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. C-D68 (8)의 1분 check 경로는 실모델에서 의도대로 동작했습니다(전달, 새 출력만, worker 한 줄 반응, 보고 없음). smoke-02의 P3/M4/M2'는 이번 실행에서 재현되지 않았습니다.
2. 명령 없는 `terminal` 즉시 응답은 실모델 근거가 없습니다. 결정론 테스트로 판단하거나, 필요하면 "진행 중 출력을 보여 달라"는 사용자/manager 요청을 넣은 별도 smoke로 유도할 수 있습니다(사용자 승인 필요).
3. O1(tmux passthrough 알림이 manager pane에 글자로 보임)은 tmux 안 실행이 제품 요구사항이므로 별도 확인 대상입니다. O2(`unknown` outbox 기록)는 동작 영향이 없고 기록의 의미만 확인하면 됩니다.
