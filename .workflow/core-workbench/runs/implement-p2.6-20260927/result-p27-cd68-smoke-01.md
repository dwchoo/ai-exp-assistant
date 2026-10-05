# p27-cd68-smoke-01: C-D68 실모델 smoke 결과

- 역할: test_designer (Opus). 실행 시각은 2026-10-05 21:42:34~21:51:45 KST(약 9.2분)이며, 시나리오 2는 packet에서 허용한 1회 재시도를 썼습니다.
- 결과: worker는 명령을 Workbench `terminal`로만 실행했고, OMP `bash`/`eval` 호출은 0건이었습니다. 두 Task 모두 `closed(done)`로 끝났고 worker는 `idle`, shell은 user에게 돌아왔습니다.
  120 s 대기 후 `running` 반환, 그 뒤 worker의 turn 종료, 명령 종료 시 `terminal_done` notice `sent`, worker의 manager 보고까지 실모델로 확인했습니다.
  `terminal_check`는 이번 명령 길이(150 s)로는 발동 조건이 생기지 않아 미검증입니다(아래 설명).
- provider request: **31/40**(1차 15 = manager 7 + worker 8, 재시도 16 = manager 7 + worker 9). error/aborted 0건. wall-clock 약 9.2분/25분. cap에 닿지 않았습니다.
- 진입점: project dir에서 `python -m workbench start --data-dir /tmp/wb-cd68-smoke-<rand>/data --omp ~/.local/bin/omp --no-attach`를 실행했습니다(실제 HOME, Workbench 소유 OMP home, `agent.db` symlink 확인, store는 열지 않음).
  product UI(`workbench attach`)를 PTY(pyte)에 붙여 manager pane에 입력했고, 상태는 `workbench status --json`과 temp `workflow/handoffs.jsonl`(구조만)로 1 s 간격으로 관측했습니다.
- 코드: HEAD 396b8e2(fc62bdd 뒤의 docs commit)이며 `src/`·`omp_bridge/` 변경은 없습니다. omp/18.6.1.
- 드라이버: smoke-02b/05 드라이버를 고친 scratch 스크립트입니다(p27w `Ui`/`processes_mentioning`/`kill_exact` 재사용, 0.5 s 간격 cap watcher). 실행 후 삭제했습니다.
  request 수는 temp Workbench home 세션 jsonl의 assistant message 수로 셌습니다. 세션에서 뽑은 것은 구조(content type, 길이, 도구 이름, 인자 키, model 메타데이터)뿐이고, 모델 텍스트는 옮기지 않았습니다.
- 짧은 로그: `result-p27-cd68-smoke-01-timeline.json`(두 실행 합본, 경로는 `<root>`로 줄임, secret 없음)

## Isolation (start 시 check, 두 실행 모두 같음)

| 역할 | state | leaks | warnings | 관측 model / thinking | 관측 도구 | subagent |
|---|---|---|---|---|---|---|
| manager | ok | 0 | 0 | `openai-codex/gpt-6.1-sol` / high | read, bash, edit, eval, glob, grep, task, wait, todo, web_search, write, to_worker | scout, reviewer, security-reviewer, task, sonic (bundled) |
| worker | ok | 0 | 0 | `openai-codex/gpt-6-luna` / max | read, grep, glob, edit, write, web_search, todo, task, wait, to_manager, **terminal** (bash/eval 없음) | analyst, explorer (Workbench 전용) |

- 새 worker leak 규칙(get_state dumpTools 기준): 실제 OMP 18.6.1 도구 목록이 허용 목록과 bridge 도구(`to_manager`, `terminal`)에 정확히 맞아 leak은 0건이었습니다.
- providers는 두 역할 모두 `openai-codex`입니다. `agent.db`는 symlink(basename만 확인)입니다.

## 관측 모델 (세션 jsonl 메타데이터)

- manager: 모든 assistant message가 `openai-codex` / `gpt-6.1-sol`이고 `thinking_level_change: high`입니다. `service_tier_change` 기록은 없습니다(C-D68 (4): manager default는 fast가 아님. 상태줄에도 ⚡ 없음).
- worker: 모든 assistant message가 `openai-codex` / `gpt-6-luna`이고 `thinking_level_change: max`, **`service_tier_change: {openai: "priority"}`**(fast)입니다. 상태줄은 `◉ GPT-6 Luna ⚡`로 표시됩니다.
- api는 `openai-codex-responses`입니다. 메시지별 service tier 필드는 assistant message에 없어서, 실제 응답 tier는 세션 설정 기록으로만 확인했습니다.
- subagent 호출은 0건입니다(slow/smol 모델은 이번 실행에서 쓰이지 않음).

## Timeline (KST, 세션 UTC 시각은 +9로 환산)

### 1차 실행 (`/tmp/wb-cd68-smoke-vjh60qyh`)
| 시각 | req | 관측 |
|---|---|---|
| 21:42:36 | 0 | start exit 0, phase ready, isolation ok (manager/worker 0 leaks), `agent.db` symlink |
| 21:42:37 | 0 | product UI attach `worker: 대기 · 자동화: idle` |
| 21:42:39 | 0 | **시나리오 1** 지시 입력(free-work, 하드웨어 사양 확인) |
| 21:42:42 | 1 | manager `read`(skill) |
| 21:42:51 | 2 | manager `to_worker{kind: work, task_id: null}` → `dispatched`(standing delegation, `manager_rule` 문구 포함). Task `work`/`running`, worker `busy`, 자동화 `active` |
| 21:42:55 | 4 | worker `read`(skill), thinking 25자 |
| 21:43:00~01 | 5 | **worker `terminal{command: "lscpu; …free -h; …nproc; …df -h", timeout_seconds: 120}`** → journal `terminal_request` → `terminal_started`(cwd `<root>/project`, host shell의 현재 위치) → `terminal_ended`(exited, exit 0, 0.09 s) → `terminal_notice: not_needed` → `terminal_result` exited |
| 21:43:03 | 5 | host pane에 `$ wb-handoff`와 lscpu/free/nproc/df 출력이 보임. owner `user`/`manual_prompt`(이미 반환됨) |
| 21:43:10 | 6 | worker `to_manager{kind: done}` → `queued`. Task `closed`(`closed_reason: done`), worker `idle`, 자동화 `idle` |
| 21:43:15 | 7 | worker가 같은 `to_manager{kind: done}`을 한 번 더 보냄 → **`rejected: no_active_task`**(모델 행동, 아래 M2) |
| 21:43:19 | 8 | worker turn 종료 |
| 21:43:24 | 9 | manager가 report 처리, 사용자에게 사양 요약(표) 표시. 상태줄 `[종료(완료)]` |
| 21:43:50 | 9 | **시나리오 2** 지시 입력(150 s tick loop) |
| 21:43:59 | 10 | manager `to_worker` → `dispatched` |
| 21:44:03 | 12 | **worker `terminal{…, timeout_seconds: 180}`**: 모델이 기본값 120보다 긴 대기를 골랐음. `terminal_started`, owner `manager`/`control_wait` |
| 21:44:37 | 12 | host pane `$ wb-handoff`, `tick 1..4`, 상태줄 `[실행 중]` |
| 21:46:33 | 12 | `terminal_ended`(exit 0, 150.1 s). 대기 중이던 호출이 결과를 받음 → `terminal_notice: not_needed`. shell이 user에게 돌아감 |
| 21:46:37~38 | 13 | worker `to_manager{done}` → Task `closed(done)`, worker `idle` |
| 21:46:41 | 14~15 | manager 보고 처리, worker turn 종료. 이후 30 s 넘게 추가 request 없음 |
| 21:47:12 | 15 | `shutdown --yes --json` exit 0, `verified: true`, 세 pane과 supervisor dead, survivors 0. temp root 삭제, project `git status` 깨끗함 |

1차 시나리오 2에서는 worker가 `timeout_seconds: 180`을 넘겼기 때문에 120 s `running` 경로가 발동하지 않았습니다. 그래서 packet 4번 규칙(최대 1회 재시도)에 따라 새 temp dir에서 시나리오 2만 다시 실행했습니다. 이번 지시에는 "timeout_seconds null(기본 120 s)"을 명시했습니다.

### 재시도 실행 (`/tmp/wb-cd68-smoke-9slj6rba`, 시나리오 2만)
| 시각 | req | 관측 |
|---|---|---|
| 21:48:02 | 0 | start exit 0, isolation ok(동일) |
| 21:48:05 | 0 | 지시 입력 |
| 21:48:08~20 | 2 | manager `read` → `to_worker` `dispatched` |
| 21:48:27 | 5 | **worker `terminal{command, timeout_seconds: null}`** → `terminal_started`(cwd `<root>/project`), owner `manager`/`control_wait` |
| 21:49:01 | 5 | 상태줄 `[실행 중]`, host pane `$ wb-handoff` + tick |
| **21:50:27** | 5 | **첫 대기 종료: `terminal_result status: running`, `elapsed_seconds: 119.96`**. detail "End your turn now … check every 60 s … completion notice" |
| 21:50:35~44 | 6~11 | worker가 `to_manager{kind: progress}`를 **3회 연달아** 보냄(모두 `queued`). manager가 각각 text로 응답(+3). worker는 **다시 기다리는 `terminal` 호출을 하지 않았음**(re-wait loop 없음) |
| 21:50:48 | 12 | worker turn 종료(text) |
| 21:50:58 | 12 | `terminal_ended`(exit 0) → 기다리는 호출이 없으므로 **`terminal_notice: pending` → `sent`**, worker에 notice 메시지(user, 816자) 도착. shell이 user에게 돌아감 |
| 21:51:03 | 13 | worker `to_manager{done}` → `queued`. Task `closed(done)`, worker `idle`, 자동화 `idle` |
| 21:51:07 | 14 | worker가 `done`을 한 번 더 보냄 → **`rejected: no_active_task`**(M2 재현) |
| 21:51:08~11 | 16 | manager 보고 처리, worker turn 종료 |
| 21:51:40 | 16 | `shutdown` exit 0, `verified: true`, survivors 0. temp root 삭제, `git status` 깨끗함 |

`terminal_check`가 오지 않은 이유: C-D68 (8)에 따라 check 주기는 "시작 또는 마지막으로 기다리던 호출이 반환된 때"부터 60 s를 셉니다. 첫 대기가 120 s에 끝났으므로 다음 check는 약 180 s 시점에 와야 하는데, 명령은 150 s에 끝났습니다. 그 사이 worker는 바빴다가(progress) 21:50:48에 idle이 되었고, check 대상 구간에서 journal에는 `terminal_check` 기록이 없습니다. 사양대로의 동작이며, check 경로는 **실모델로는 미검증**입니다(재시도는 이미 1회를 썼습니다).

### Provider request 수
| 실행 | OMP | request | content 형태(텍스트 제외) |
|---|---|---|---|
| 1차 | manager | 7 | `toolCall:read` · `to_worker`(dispatched) · text · text(S1 보고) · `to_worker`(dispatched) · text · text(S2 보고) |
| 1차 | worker | 8 | thinking+`read` · thinking+`terminal`(120) · thinking+`to_manager done` · `to_manager done`(**rejected**) · text · thinking+`terminal`(180) · `to_manager done` · text |
| 재시도 | manager | 7 | `read` · `to_worker` · text · text ×3(progress 응답) · text(done 보고) |
| 재시도 | worker | 9 | thinking+`read` · `terminal`(null) · `to_manager progress` ×3 · text(turn 종료) · `to_manager done` · `to_manager done`(**rejected**) · text |
| 합계 | | **31** | error/aborted 0, subagent 0 |

## 동작한 것
- **(1) 명령 실행 경로 강제:** worker의 모든 명령 실행이 `terminal`이었습니다. 세 Task의 worker session jsonl 전체에서 `bash`/`eval` toolCall은 0건입니다. worker 도구 목록에도 bash/eval이 없습니다(isolation 관측).
- **host terminal에서 실행:** shell idle 확인 → `wb-handoff` → child 실행 → 출력이 host pane에 보임 → 종료 후 shell 반환(`input_owner: user`, `manual_prompt`) 순서로 진행됐습니다. cwd는 host shell의 현재 위치(`<root>/project`)이고 journal과 result에 기록됩니다. source repo는 바뀌지 않았습니다.
- **결과 반환:** `exited`(exit_code, duration, log_path, output tail)는 worker에게, 출력 원문은 `workflow/terminal/<id>.log`에만 남고 journal에는 구조만 남습니다.
- **(7)/(8) 대기:** 기본 대기 120 s(119.96 s)에서 `running`이 반환됐고 명령은 계속 돌았습니다. worker는 다시 기다리는 호출을 하지 않았습니다. 종료 후 대기 중인 호출이 없을 때 `terminal_done` notice가 즉시(같은 초에) `sent`되었고, worker는 그 notice로 작업을 이어 `to_manager done`을 보냈습니다. 대기 중인 호출이 결과를 받은 경우(1차 S1·S2)에는 `not_needed`로 notice를 보내지 않았습니다(중복 없음).
- **(3) manager:** OMP 기본 도구와 bundled subagent를 유지합니다. `to_worker` 결과에 `manager_rule`("The worker does this Task; do not do it yourself …")이 보였고, manager는 직접 실행하지 않고 보고를 기다렸습니다.
- **free-work Task 수명:** dispatched → running → worker `done` 보고 → `closed(done)`, worker `idle`, 자동화 `idle`이 세 Task 모두 같았습니다. 늦게 온 두 번째 `done`은 `no_active_task`로 깔끔하게 거절됐고 상태 꼬임도 없었습니다.
- **(4) 모델표:** manager는 gpt-6.1-sol high(fast 아님), worker는 gpt-6-luna max + priority tier로 적용됐습니다.
- start/shutdown, isolation, 공유 login, 확인된 shutdown(`verified: true`), residue 0.

## 동작하지 않은 것 / 관찰 / 분류
- **M1 (모델 행동, 중):** 1차 S2에서 worker가 `timeout_seconds: 180`을 넘겨 120 s `running` 경로를 건너뛰었습니다. 도구 schema가 허용하는 값(1~1800)이라 제품 결함은 아닙니다. 다만 C-D68 (8)의 "2분 대기 후 점검"이라는 의도와 달리, 모델은 예상 시간에 맞춰 대기를 늘리는 경향을 보였습니다. 정책상 120 s를 강제할지는 Root/사용자가 판단할 사항입니다(도구 설명 문구 보강 또는 상한 조정이 후보).
- **M2 (모델 행동, 낮음, 두 번 재현):** worker가 `to_manager{done}`이 `queued`를 받은 직후 같은 `done`을 다시 보내 `rejected: no_active_task`가 되었습니다(1차 S1, 재시도). Task가 이미 닫혀서 제품은 올바르게 거절했고 request만 1회씩 늘었습니다. `queued` 결과의 detail "queued for delivery; not yet processed by the other OMP"가 재전송을 부추겼을 수 있어서, 문구 보강(예: "delivered to Workbench; do not resend") 후보입니다.
- **M3 (모델 행동, 낮음):** 재시도에서 `running`을 받은 뒤 worker가 detail("End your turn now")과 달리 `to_manager progress`를 3회 연달아 보내고 turn을 끝냈습니다(+6 request: worker 3, manager 3). 다시 기다리거나 새 명령을 실행하지는 않았습니다. `RUNNING_DETAIL` 또는 worker skill 문구 보강 후보입니다.
- **P1 (product 표시, 낮음, 결함 여부 미판정):** host pane에는 `$ wb-handoff`와 출력만 보이고 **worker가 실행한 명령 텍스트는 보이지 않습니다**(명령은 child interpreter로 전달되고 echo되지 않음). 명령은 worker pane의 `Terminal` 도구 표시와 journal에만 있습니다. C-D68 (1)의 "사용자에게 보이게 실행"을 출력 기준으로 보면 충족이고, 명령 기준으로 보면 미흡입니다.
- **P2 (product 표시, 낮음):** worker 명령이 실행되는 동안 상태줄과 host pane 제목은 `host 입력 owner: manager`입니다(구현상 managed dispatch를 manager가 claim). 실제 실행 주체는 worker라서 사용자에게 혼동을 줄 수 있습니다.
- **미검증:** `terminal_check`(위 설명), 일시정지 중 동작, busy 거절(`host_terminal_busy`), worker가 바쁠 때 notice 지연(`deferred`). 모두 이번 시나리오에서는 발동 조건이 없었습니다.

## 상태줄 text screenshot (product UI 상단 2줄)

```
[attach 21:42:37]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 21:42:37 (0s 전) | Ctrl-] ? 도움말

[S1 terminal 직후 21:43:04]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
작업: 작업 "Check this machine's hardware specs in …" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 21:43:04 (0s 전) | Ctrl-] ? 도움말

[재시도 S2 실행 중 21:49:01 / 21:50:36 동일]
focus: HOST SHELL | host 입력 owner: manager | shell mode: control_wait | worker: 작업 중 | 자동화: active
작업: 작업 "Run exactly this command in the termina…" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 21:50:36 (0s 전) | Ctrl-] ? 도움말

[재시도 완료 후 21:51:38]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 작업 "Run exactly this command in the termina…" [종료(완료)] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 21:51:38 (0s 전) | Ctrl-] ? 도움말
```

worker pane(1차 S1, 도구 표시 일부):
```
 • Terminal
  └─ command="lscpu; printf '\n--- free -h ---\n'; free -h…", timeout_seconds=120
 └ • status: "exited"
   • exit_code: 0
   • cwd: "<root>/project"
   • log_path: "<root>/data/workflow/terminal/8549c194…"
 ⠹ 11s > ◉ GPT-6 Luna ⚡ > … > ⑂ master
```

host pane(재시도 S2 실행 중 → 완료 후):
```
$ wb-handoff
tick 1
tick 2
…
tick 15
DONE
$
```

worker pane(재시도, `running` 이후):
```
 Waiting for completion
 Awaiting completion notice
 • To manager
  └─ kind="progress", message="Executed the exact requested comman…"
 └ • status: "queued"
   • detail: "queued for delivery; not yet processed by the other OMP"
```

## Residue
- 프로세스: 두 실행 모두 `shutdown --yes` exit 0, `verified: true`. 소유 프로세스 잔여 0(강제 kill 없음). 확인 시 `wb-cd68-smoke` 프로세스 0개.
- 파일: `/tmp/wb-cd68-smoke-vjh60qyh`, `/tmp/wb-cd68-smoke-9slj6rba` 삭제를 확인했습니다. scratch의 세션 사본, DB, terminal log, screen, driver도 삭제했습니다.
  run dir에는 이 파일과 `result-p27-cd68-smoke-01-timeline.json`만 남겼습니다. (`/tmp/wb-cd68-live`(18:19 생성)는 이번 실행이 만든 것이 아니어서 건드리지 않았습니다.)
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 Workbench/OMP/tmux 프로세스는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. commit, graphify update, ledger 수정, `src/**`·`omp_bridge/**`·`docs/**` 수정은 모두 하지 않았습니다.

## 권고 (Root 판단용)
1. C-D68 (1)(3)(4)(7)과 (8)의 running·done 경로는 실모델로 확인됐습니다. `terminal_check`는 실모델 미검증이므로 결정론 테스트 근거로 판단하거나, 필요하면 200 s 넘는 명령으로 별도 smoke를 돌리십시오(사용자 승인 필요).
2. M1: 120 s 첫 대기를 정책으로 강제할지 결정이 필요합니다(도구 설명 보강 또는 `timeout_seconds` 상한·기본값 고정).
3. M2/M3: `to_manager` `queued` detail과 `RUNNING_DETAIL` 문구 보강 후보입니다(중복·불필요 request 약 26% = 31회 중 8회).
4. P1/P2: host pane 명령 표시와 owner 표기는 UX polish 후보입니다.
