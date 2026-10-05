# p27-cd68-smoke-02: C-D68 (8)(9) 실모델 smoke 결과 (terminal_check 경로)

- 역할: test_designer (Opus). 실행 시각 2026-10-05 23:38:15~23:43:29 KST(약 5.2분/20분). 재시도 없음(packet에 재시도 허용이 없어 1회 실행).
- 결론: **`terminal_check`는 이번에도 발동하지 않았습니다(journal에 `terminal_check` 기록 0건). 실모델 미검증 상태가 그대로입니다.**
  원인은 제품 결함이 아닙니다. worker가 `running`을 받은 뒤 텍스트 없이 turn을 끝냈고(thinking만 있는 empty stop), **OMP 18.6.1의 empty-stop 자동 재시도**가 worker를 다시 깨웠습니다. 다시 깨어난 worker가 `terminal{command: null}`로 재대기했고, 그 대기가 check 예정 시각(시작+180 s)을 덮었습니다. 대기 중인 호출이 있으면 check를 보내지 않는 것은 사양(C-D68 (8))대로입니다.
- 그 밖의 C-D68 (9) 수정은 모두 실모델로 확인했습니다. 대기는 120 s 고정이고(worker 인자에 `timeout_seconds` 없음, 119.96 s에 `running`), host pane에 `[worker] $ <command>`가 표시되며, 실행 중 owner는 `worker`로 표시됩니다. 진행 보고(progress)는 0건, `done`은 1회였고 Task `closed(done)`, worker `idle`, shell은 user에게 반환됐습니다.
- provider request: **12/30**(manager 4 + worker 8). error/aborted 0, cap 미도달.
- 진입점: smoke-01과 같습니다. project dir에서 `python -m workbench start --data-dir /tmp/wb-cd68-smoke2-<rand>/data --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home)를 실행하고, product UI(`workbench attach`)를 PTY(pyte, 170x40)에 붙여 manager pane에 입력했습니다. 관측은 `workbench status --json`, temp `workflow/handoffs.jsonl`(안전 필드만), temp 세션 jsonl(구조만)로 약 1 s 간격으로 했습니다.
- 코드: HEAD cdbbd23(74fc955 뒤의 docs commit, `src/`·`omp_bridge/` 차이 0). omp/18.6.1.
- 드라이버: p27w의 `Ui`/`processes_mentioning`/`kill_exact`를 재사용한 scratch 스크립트입니다(cap watcher는 28에서 pause하도록 설정). 실행 후 삭제했습니다.
- 짧은 로그: `result-p27-cd68-smoke-02-timeline.json`(경로는 `<root>`로 줄임, 마지막 항목에 세션 구조, secret 없음)

## Isolation (start 시 check)
| 역할 | state | leaks | warnings | 관측 model |
|---|---|---|---|---|
| manager | ok | 0 | 0 | `gpt-6.1-sol`(assistant 4건 모두) |
| worker | ok | 0 | 0 | `gpt-6-luna`(assistant 8건 모두), 상태줄 `◉ GPT-6 Luna ⚡` |

`agent.db`는 symlink입니다(basename만 확인, 내용은 읽지 않음). start 출력은 `omp isolation: ok (omp/18.6.1 evidence bridge_g3=18.2.10)`입니다.

## Timeline (KST)
| 시각 | req | 관측 |
|---|---|---|
| 23:38:17 | 0 | start exit 0, phase ready, isolation ok |
| 23:38:18 | 0 | product UI attach `worker: 대기 · 자동화: idle` |
| 23:38:20 | 0 | manager에 지시 입력(free-work, `for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE`, "직접 실행하지 말 것") |
| 23:38:23~31 | 2 | manager `read`(skill) → `to_worker{kind: work, task_id: null}` → `dispatched`. Task `work`/`running`, worker `busy` |
| 23:38:34 | 3 | manager text 후 turn 종료(직접 실행 없음) |
| 23:38:38 | 4 | worker `read`(to-manager skill) |
| **23:38:44** | 5 | **worker `terminal{command}`(인자 키는 `command`뿐, 대기 시간 입력 없음)** → `terminal_request` → `terminal_started`(cwd `<root>/project`). shell owner `manager`/`control_wait`, **UI 표시는 `host 입력 owner: worker`**, pane 제목 `HOST SHELL … owner=worker` |
| 23:38:53 | 5 | host pane `$ wb-handoff` → **`[worker] $ for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE`** → `tick 1` |
| **23:40:44** | 5 | **첫 대기 종료: `terminal_result status: running`, `elapsed_seconds: 119.959`** |
| 23:41:00 | 6 | worker가 OMP `wait` 도구를 호출(0.1 s 만에 반환, 결과 129자) — 모델 행동 M4 |
| 23:41:14 | 7 | worker가 **텍스트 없이 turn 종료**(thinking 2블록, text 길이 0, `stopReason: stop`) |
| 23:41:20 | 8 | 그 사이 user·Workbench 입력이 없는데 worker가 다시 응답(OMP empty-stop 자동 재시도, 아래 P3) → **`terminal{command: null}`** → journal `terminal_request(command null)` + `terminal_wait` |
| 23:41:44 | 8 | check 예정 시각(첫 대기 반환 +60 s). 대기 중인 호출이 있어 **check를 보내지 않음**(사양). journal 기록 없음 |
| 23:42:44 | 8 | `terminal_ended`(exited, exit 0, 240.11 s) → 대기 중인 호출이 결과를 받음(`terminal_result exited`) → **`terminal_notice: not_needed`**. shell `user`/`manual_prompt` |
| 23:42:48 | 9 | worker `to_manager{kind: done}` → `queued`. Task `closed(done)`, worker `idle`, 자동화 `idle` |
| 23:42:51 | 10 | manager가 report를 처리해 사용자에게 완료를 알림(text) |
| 23:42:52 | 11 | worker `to_manager{kind: report, message: "No further action."}` → **`rejected: no_active_task`**(M2 변형) |
| 23:42:56 | 12 | worker text로 turn 종료. 이후 30 s 넘게 추가 request 없음 |
| 23:43:26 | 12 | `shutdown --yes --json` exit 0, `verified: true`, 세 pane과 supervisor dead, survivors 0 |
| 23:43:29 | 12 | 소유 프로세스 잔여 0, temp root 삭제 확인, project `git status` 깨끗함 |

## Provider request 수 (세션 jsonl assistant message)
| OMP | request | content 형태(텍스트 제외) |
|---|---|---|
| manager | 4 | `read` · `to_worker`(dispatched) · text(100자) · text(완료 보고 155자) |
| worker | 8 | thinking+`read` · thinking+`terminal{command}` · thinking×2+`wait` · thinking×2+빈 text(**empty stop**) · thinking+`terminal{command: null}` · `to_manager done`(queued) · `to_manager report`(**rejected**) · text(88자) |
| 합계 | **12** | error/aborted 0, subagent 0 |

## 동작한 것 (실모델)
- **C-D68 (9) 대기 고정:** worker의 `terminal` 인자는 `command`뿐이었습니다. 첫 대기는 119.959 s에 `running`으로 끝났고 명령은 계속 실행됐습니다. smoke-01 M1(180 s로 대기 연장)은 재현되지 않았습니다(재현할 수단이 없음).
- **명령 표시(smoke-01 P1 수정):** host pane에 `[worker] $ <command>`가 출력보다 먼저 보였고, log와 output_tail에는 그 줄이 들어가지 않았습니다(log는 `tick 1…tick 24, DONE`).
- **owner 표시(smoke-01 P2 수정):** 명령 시작부터 shell 반환 전까지 상태줄 `host 입력 owner: worker`, pane 제목 `owner=worker`. 반환 뒤에는 `owner: user`.
- **M3 수정:** `running`을 받은 뒤 `to_manager progress`는 0건이었습니다.
- **M2 수정(부분):** `done`은 한 번만 보냈고 중복 `done`은 없었습니다. 다만 아래 M2'처럼 다른 kind로 한 번 더 보냈습니다.
- **완료 처리:** 대기 중인 호출이 결과를 받아 `terminal_notice: not_needed`가 기록됐고(notice 중복 없음), `done` → `closed(done)` → worker `idle` → shell 반환이 이어졌습니다. manager는 직접 실행하지 않고 보고를 기다렸습니다.
- start/shutdown(`verified: true`), isolation ok, residue 0.

## 동작하지 않은 것 / 관찰 / 분류
- **미검증: `terminal_check` 전달과 그에 대한 worker 반응.** journal에 `terminal_check` 기록이 0건입니다. 23:41:20부터 23:42:44까지 대기 중인 `terminal` 호출이 있었고, check 예정 시각 23:41:44가 그 구간 안에 있었습니다. 대기가 반환된 시각이 곧 명령 종료 시각이라 이후 check도 생기지 않았습니다. 제품은 사양대로 동작했습니다.
- **P3 (OMP 동작 × 제품 지시 문구, 중) — 이번 미검증의 직접 원인:** `running` detail("End your turn now …")을 따른 worker가 **텍스트 없이** turn을 끝냈습니다. OMP 18.6.1은 이런 응답(thinking만 있는 `stop`)을 empty stop으로 보고 developer 메시지(`prompts/system/empty-stop-retry.md`, 최대 3회)를 붙여 다시 요청합니다. 다시 요청받은 worker는 할 일을 찾다가 `terminal{command: null}`로 재대기했습니다. 바이너리 문자열로 확인한 근거는 `Kke`(stop + 표시 내용 없음 → empty) → `#I`에서 `acceptTerminalEmptyStop`이 아니면 retry, 그리고 `appendMessage({role: "developer", … empty-stop-retry})`입니다. 세션 구조에서도 23:41:14 빈 stop과 23:41:20 응답 사이에 user/toolResult 메시지가 없습니다(developer 메시지는 세션 기록에 보이지 않음).
  사용자 지시가 아니라 OMP 내부 재시도라는 점이 smoke-01 M1/M3과 다릅니다. 이 경로가 있는 한 "turn 종료 → 60 s check" 흐름은 모델이 빈 응답을 낼 때마다 재대기로 바뀔 수 있습니다.
  수정 후보(Root 판단): `RUNNING_DETAIL`/to-manager skill에 "turn을 끝낼 때 한 줄 텍스트(예: 'Waiting for the terminal notice.')를 남긴다"를 넣기. 또는 `command: null` 재대기를 `running` 직후 일정 시간 막거나 짧게 반환하기. 또는 OMP `acceptTerminalEmptyStop` 경로를 쓸 수 있는지 확인하기.
- **M4 (모델 행동, 낮음):** `running` 직후 worker가 OMP `wait` 도구를 호출했습니다(즉시 반환, +1 request). 현재 skill은 manager 응답을 기다릴 때만 `wait`를 금지합니다. terminal 명령에도 `wait`를 쓰지 말라는 문구가 후보입니다.
- **M2' (모델 행동, 낮음):** `done`이 `queued`된 뒤 worker가 `to_manager{kind: report, "No further action."}`를 한 번 더 보내 `no_active_task`로 거절됐습니다(+1 request). 제품은 올바르게 거절했고 상태 꼬임은 없었습니다. queued detail의 "Do not send it again"은 같은 보고의 재전송만 막고, 추가 report는 막지 못했습니다.
- **표시(낮음, 결함 아님):** host pane에 `[worker] $ …` 앞에 `$ wb-handoff` 줄이 여전히 보입니다. worker pane에서는 `To manager` 도구 표시가 같은 내용으로 3번 렌더링되어 보였지만, 세션의 실제 호출은 1건입니다(OMP TUI 표시).

## 상태줄·pane text screenshot (product UI)
```
[attach 23:38:18]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 23:38:18 (0s 전) | Ctrl-] ? 도움말

[명령 +8 s, 23:38:53, host focus]
focus: HOST SHELL | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active
작업: 작업 "Run exactly this command in the Workben…" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 23:38:53 (0s 전) | Ctrl-] ? 도움말
lq HOST SHELL *FOCUS* alive owner=worker qqq…
x$ wb-handoff
x[worker] $ for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE
xtick 1

[완료 후 23:43:23]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 작업 "Run exactly this command in the Workben…" [종료(완료)] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 23:43:23 (0s 전) | Ctrl-] ? 도움말
lq HOST SHELL *FOCUS* alive owner=user qqq…
xtick 23
xtick 24
xDONE
x$
```

worker pane(+125 s, `running` 결과 → +190 s, 재대기):
```
 • Terminal
  └─ command="for i in $(seq 1 24); do echo tick $i; sleep 10; done; echo DONE"
 └ • status: "running"
   • command_id: "61f94342-b8f8-42b8-90d3-a65670b9bd7d"
   • elapsed_seconds: 119.959
…
 Waiting on command completion
 Command remains running
 Checking completion status
 Checking terminal status
 ⣻ Terminal
  └─ command=null
 ⠴ 3m > ◉ GPT-6 Luna ⚡ > 🗑 …smoke2-xb678z3v/project > ⑂ master
```

worker pane(완료 후):
```
 • To manager
  └─ kind="report", message="No further action.", task_id="20a05f38-3b93-4027-a…"
 └ • status: "rejected"
   • reason: "no_active_task"
 The command exited with status 0. Output included tick 1 through tick 24 and DONE.
```

## Residue
- 프로세스: `shutdown --yes --json` exit 0, `verified: true`. 소유 프로세스 잔여 0(강제 kill 없음). 확인 시 `wb-cd68-smoke2` 프로세스 0개.
- 파일: `/tmp/wb-cd68-smoke2-xb678z3v` 삭제 확인. scratch의 driver, 세션 구조 사본, screen 사본, OMP 바이너리 문자열 추출본도 삭제했습니다. run dir에는 이 파일과 `result-p27-cd68-smoke-02-timeline.json`만 남겼습니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스, `/tmp/wb-cd68-live`는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. commit, graphify update, ledger 수정, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. `terminal_check`는 실모델에서 두 번 연속 발동 조건이 생기지 않았습니다(smoke-01은 명령 길이, smoke-02는 OMP empty-stop 재시도 → 재대기). check 자체의 전달과 merge/deferred 동작은 결정론 테스트 근거로 판단하는 것이 현실적입니다.
2. P3는 C-D68 (8)의 흐름을 실제로 깨는 경로입니다. `running` 결과와 skill에 "한 줄 텍스트로 turn 종료"를 명시하는 것이 가장 작은 수정 후보입니다. `command: null` 재대기를 허용할지 여부(C-D68 (8) "결과를 다시 받는 호출은 유지")는 사용자 결정 범위입니다.
3. 수정 후 다시 실모델 smoke를 돌린다면 이번 설정(240 s, 같은 지시)으로 약 12~15 request, 6분이면 충분합니다(사용자 승인 필요).
