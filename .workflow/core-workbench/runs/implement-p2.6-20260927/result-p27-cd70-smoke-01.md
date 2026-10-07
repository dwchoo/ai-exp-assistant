# p27-cd70-smoke-01: C-D70 실모델 smoke 결과 (S2만 실행, 요청 cap 초과)

- 역할: test_designer(Sonnet). 2026-10-07 15:55~16:02 KST(약 7분/20분). 코드: HEAD bc131d5(candidate b7e0012 이후 docs만 추가, `src/`·`omp_bridge/` 변경 없음). **omp/18.7.0**(`omp --version` = `~/.local/bin/omp --version`). Python `/tmp/cw02-g1-venv/bin/python`.
- **결론 요약**
  - **S2(restart_worker)만 실행했고, 거의 이상적으로 동작했습니다.** manager가 `restart_worker`(사유 포함)를 불러 worker만 재시작했고, host terminal 명령은 계속 실행되어 exit 0으로 끝났습니다. `worker_restarted` 알림 1건, 후속 `to_worker` 정확히 1건(Task 전체 재전송 1회), 새 worker는 명령을 다시 실행하지 않고 이어받아 보고했습니다.
  - **S3(manager recovery)와 S1(watchdog)은 실행하지 못했습니다.** 이유: provider request가 cap을 넘었습니다(아래). 추가 요청 없이 멈추고 verified shutdown까지 마쳤습니다.
  - **cap 위반(내 도구 결함):** provider request **34/30**(manager 23 + worker 11). scratch 드라이버의 observer 스레드가 15:58:40 KST(요청 16개 시점)에 예외(재시작 중 `session_id`가 None인 snapshot, `TypeError`)로 죽어 약 1분간 카운트·cap 감시가 멈췄고, 그 사이 manager가 `todo`·`workbench_status` 등으로 요청을 13개 더 썼습니다. cap은 감시 재개(16:00:15) 때 34로 확인했고 그 뒤로는 새 입력을 하지 않았습니다(제 드라이버가 cap에서 입력을 거부함). 초과분 4개는 보고합니다.
- 방법: cd69 smoke와 동일. 새 `/tmp/wb-cd70-smoke/{data,project}`(project는 git repo)에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home, `agent.db`는 열지 않음). 상태·journal은 `workbench status --json`과 data의 `handoffs.jsonl`·`tasks-flow.jsonl`, 세션 jsonl(구조 위주)로 1 s 간격 관측. 제품 UI(`workbench attach`)를 PTY(pyte 170x40)에 붙여 manager pane에 입력. `DISPLAY`/`WAYLAND_DISPLAY`/`TMUX*`/`WORKBENCH_*`는 환경에서 뺐습니다.
- 짧은 로그: `result-p27-cd70-smoke-01-timeline.json`(세션 구조·tool call 수·journal 요약. secret 없음, 경로는 `<root>`).

## Isolation (OMP 18.7.0, start 결과)
`omp isolation: ok (omp/18.7.0 evidence bridge_g3=18.2.10, isolation=18.6.1)`. leaks **[]**.

| 역할 | state | leaks | warnings | model / thinking | 도구 | skills |
|---|---|---|---|---|---|---|
| manager | ok | 0 | 0 | `openai-codex/gpt-6.1-sol` / high | read, bash, edit, eval, glob, grep, task, wait, todo, web_search, write, **to_worker, restart_worker, workbench_status** | to-worker, **workbench-recovery** |
| worker | ok | 0 | 0 | `openai-codex/gpt-6-luna` / max(service tier priority) | read, grep, glob, edit, write, web_search, todo, task, **to_manager, terminal** (bash/eval 없음) | to-manager |

- 새 도구(`restart_worker`, `workbench_status`)와 skill(`workbench-recovery`)이 18.7.0에서도 manager에만 허용되고 worker에는 새지 않았습니다. worker task agent는 `explorer`만 보입니다(cd68의 analyst, explorer와 다름: 관측 사실만 기록).
- start 직후 세 pane 모두 `alive=True owner=user`, shell mode `manual_prompt`.

## S2 timeline (KST, 입력=0 s)
입력 원문: "터미널에서 `sleep 45; echo cd70-s2-done` 를 실행하게 하고, 명령이 실행 중이면 restart_worker로 worker를 재시작한 다음 이어서 마무리해줘"

| 시각 | 경과 | 관측 |
|---|---|---|
| 15:56:42 | 0 | manager pane에 입력 |
| 15:56:45~47 | 3~5 s | manager `read` skill://to-worker, **`read` skill://workbench-recovery**(사전 읽기) |
| 15:57:06 | 24 s | manager `to_worker`(commands 1개, analysis summary, message에 "worker가 실행 중 보고하면…" 서술) → Task#1 `dispatched`, worker TASK 도착 |
| 15:57:15.7 | 34 s | worker `terminal`(120 s 대기 호출) → host pane `[worker] $ sleep 45; …`, 상태줄 `host 입력 owner: worker · control_wait` |
| 15:57:26 | | outbox `unknown / BridgeTimeout`(worker는 15:57:06.15에 이미 TASK 수신·처리 중. 전달은 정상) |
| 15:58:00.9 | 79 s | 명령 종료(exit 0, 45.1 s). `terminal` 호출이 **완료 후에야 반환**(120 s 안) |
| 15:58:04.7 | 82 s | worker `to_manager done`("running-state progress report or restart window was available 없음" 명시) → Task#1 종료 |
| 15:58:27 | 105 s | **manager가 같은 명령으로 Task#2를 새로 보냄**(to_worker, "restart window를 만들기 위해" 재실행; 명령 2회 실행됨) |
| 15:58:31 | 109 s | worker `terminal` → 명령 실행 시작(command_id e6ef0125…) |
| 15:58:35 | 113 s | manager `workbench_status`(Task running, terminal running) |
| **15:58:43.6** | **122 s** | manager `restart_worker`(사유 필수: "사용자 요청: sleep 45; … 명령이 터미널에서 실행 중임을 확인했으므로 worker만 재시작하고 기존 명령은 계속 실행하도록 유지.") — 명령 시작 12.5 s 뒤 |
| 15:58:43.6 | | 이전 worker의 terminal 대기: `terminal_peer_gone`, `terminal_wait_abandoned`(명령은 둠) |
| 15:58:44.7 | +1.1 s | `restart_worker_result: restarted`(이전 pid 종료 exit 143, survivors [], 새 pid 3556490 등록, **새 pane generation 2, 새 session** 01a11528…). 결과 detail: "후속은 바로 보내지 말고 turn 종료, worker_restarted 알림 뒤 정확히 1개" |
| 15:58:45.6 | | `worker_restarted` notice queued → deferred(manager가 turn 중) |
| 15:58:59.7 | +16 s | notice **sent**(manager 입력 형태 JSON: cause=restart_worker, reason, requester=manager, 이전/새 session id, terminal.running=true, instruction) |
| 15:59:04 | | manager `workbench_status` 1회 더 |
| **15:59:13.2** | | manager 후속 `to_worker`(task_id만, analysis summary, "원래 명령 e6ef0125는 실행 중, 재실행·중단 금지, 완료 기록을 기다려 보고") → 결과 `resent_task: true` |
| 15:59:13.24 | | 새 worker에 **Task 전체 재전송**(message kind=question, 3033자): 원 Task(goal/message/commands/paths/revision), `resend_note`, **`commands_already_run`**([sleep 45 … status=running, log_path]), `commands_rule`, `follow_up` |
| 15:59:15.96 | | 새 worker `terminal`(command 없이 fetch, wait 0) → `status: running`(elapsed 44.9 s). **명령을 다시 실행하지 않음** |
| 15:59:16.2 | | 명령 종료(exit 0, 45.09 s). `terminal_result_undelivered` → `terminal_done` notice pending → deferred → 15:59:20 **sent**(새 worker) |
| 15:59:28.0 | 166 s | 새 worker `to_manager done`(exit 0, stdout `cd70-s2-done\n`, command_id, log 경로). Task#2 종료 |
| 15:59:36.2 | **174 s** | manager 최종 답변 224자(한국어): restart_worker 사용, 기존 명령 유지, exit 0, 출력, 약 45.1초, **"첫 실행은 재시작 전에 완료돼 총 두 번 실행했습니다"를 사실대로 밝힘** |

상태줄(제품 UI):
```
[15:57:16] focus: MANAGER OMP | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active
           작업: 작업 "Run the exact command once in the host …" [실행 중] | backend: ready | bridge manager=ok worker=ok
[15:58:44] focus: MANAGER OMP | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active
           worker 재시작됨 (manager 요청: 사용자 요청: sleep 45; echo cd70-s2-don…) | 작업: 작업 "Run the given command exactly once now.…" [실행 중] | …
[15:59:28] focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
           worker 재시작됨 (manager 요청: …) | 작업: 작업 "Run the given command exactly once now.…" [종료(완료)] | …
```
- 재시작 표시는 요청자·사유 앞 40칸에서 잘려 보이고, 최종 상태에도 120 s 안은 남아 있었습니다(`MANAGER_RESTART_SHOWN_SECONDS`).
- host 명령이 `restart_worker` 중에도 안 끊겼음: host pane에 두 번째 `[worker] $ sleep 45; echo cd70-s2-done` 뒤 `cd70-s2-done`이 정상 출력.

### S2 검증 항목 요약
| 항목 | 결과 |
|---|---|
| `restart_worker` 호출·사유 필수 | 호출됨, 사유 기록(journal `restart_worker_request`·결과·상태줄) |
| host 명령 유지 | 유지(exit 0, 45.09 s, log 14 B) |
| `worker_restarted` notice | 1건(queued→deferred→sent, manager 16 s 지연: turn 중이라 대기) |
| 후속 `to_worker` | 정확히 1건(notice 전 보내지 않음. `resent_task: true`) |
| 새 worker TASK 내용 | 전체 Task + commands + `commands_already_run`(running) + follow_up. 재실행 안 함 |
| Task 전체 재전송 횟수 | 1(예상대로) |
| 최종 보고 | worker `done` 1건 → manager 최종 답변(재실행·restart 정직하게 보고) |
| 사용자 승인 없이 사용 | 가능(규약대로) |

## Provider request (세션 jsonl assistant message)
| 세션 | model | req | tool calls |
|---|---|---|---|
| manager(1개 session 유지) | gpt-6.1-sol | **23** | read 2, **todo 7**, to_worker 3, workbench_status 2, restart_worker 1(+ 텍스트 turn들) |
| worker#1(재시작 전) | gpt-6-luna | 6 | read 1, terminal 2, to_manager 2 |
| worker#2(재시작 후) | gpt-6-luna | 5 | read 1, terminal 1, to_manager 1 |
| 합계 | | **34 / cap 30** | error/aborted 0 |

- manager 23건 중 `todo` 7건 등 부수 호출이 많습니다(모델 행동). worker#1의 `to_manager` 2번째(198B)는 `done` 뒤 중복 보고로 보이며(cd68·cd69와 같은 종류, 하네스 거절 여부는 보지 않음) worker#2에는 없었습니다.

## 동작한 것
- 도구·skill 노출(manager 전용), 격리 ok(18.7.0), restart 절차 전체(종료 → 같은 pane 재시작 → 새 session 등록 → notice → 후속 1건 → 재전송 → 이어받기 → 보고).
- 실행 중 terminal 명령 보존과 새 worker로의 terminal 완료 알림(`terminal_done`) 전달.
- 사유가 journal·화면·결과 JSON에 남음. manager가 `workbench-recovery` skill을 먼저 읽고 `workbench_status`→판단 순서를 따랐습니다.

## 동작하지 않은 것 / 관찰 (제품 결함 vs 모델 행동 분류)
- **O1 (설계상 한계, 중간):** `terminal`이 120 s 동안 worker turn을 막는 구조라, 45 s 명령에서는 worker가 "실행 중" 보고를 보낼 수 없고 manager는 turn을 끝내 restart 창이 생기지 않습니다. 명령이 120 s 안에 끝나면 `restart_worker`를 쓸 수 있는 시점이 사실상 없습니다(manager가 worker 대기 중 직접 호출하지 못함). 이번엔 manager가 **명령을 한 번 더 실행하는 Task#2를 만들어** 창을 만들었습니다(모델 판단. 결과적으로 사용자 명령이 두 번 실행됨; 멱등이 아닌 명령이면 부작용이 2배). 정상 사용자 시나리오에서는 120 s를 넘는 명령이나 worker가 막혔을 때 쓰는 도구라 의도와 맞지만, 사용자가 이런 문구를 쓰면 manager가 재실행하는 행동은 관측 사실로 남깁니다. Root 판단.
- **O2 (표시/기록, 낮음, cd69 O3와 동일 계열):** outbox `unknown / BridgeTimeout`이 3건(15:57:26 task, 15:58:24 report, 15:58:47 task) 기록됐지만 모두 실제로는 상대가 이미 처리했습니다(worker는 TASK를 0.1 s 안에 받음). C-D70 (2)의 "전달 결과 불명이면 `도착 확인 불가` 알림"은 이번엔 발생하지 않았습니다(알림 journal 없음). 이 `unknown`이 실제 사용자에게 알림으로 이어질지는 S3 등에서 보지 못했습니다.
- **O3 (표시, 낮음, cd69 O1 계열):** host pane의 마지막 후속 명령 `echo CD70_FOLLOWUP_$((6*7))`(prefix 3으로 포커스, 정상 입력) 결과 `CD70_FOLLOWUP_42` **뒤에 다음 프롬프트 `$ `가 표시되지 않음**(이후 5 s 이상 안정). 덮어쓰기(overprint)는 이번엔 보이지 않았지만 프롬프트 누락은 cd69 O1과 같은 현상일 수 있습니다. pyte 한계 가능성은 배제하지 못했습니다(실제 터미널 미확인). 그 앞 worker 명령(`[worker] $ …`) 뒤에는 `$ ` 프롬프트가 보였습니다.
- **O4 (비용, 낮음):** `worker_restarted` notice가 manager turn 중이라 16 s 지연(deferred → sent). 의도된 동작이지만 이 동안 manager는 `todo` 등으로 요청을 더 썼습니다.
- **미검증(미실행):** S3 manager recovery(manager 종료·재시작, `manager_recovery` notice, 재전송 보고), S1 watchdog(60 s idle `status_check` 2회 + `worker_stalled` notice) — **실모델로 확인하지 못했습니다.** 이번 실행의 두 OMP 격리와 notice/후속 경로(S2)만이 18.7.0에서 확인됐습니다. 재시도는 새 요청 한도 승인이 필요합니다(권장: S3+S1 각 12~15 req, observer가 예외로 죽지 않게 수정한 드라이버).

## 도구 결함 (내 쪽)
- scratch observer 스레드가 재시작 중 snapshot의 `bridge.<role>.session_id=None`을 처리하지 못해 `TypeError`로 종료 → 약 1분 cap 감시 공백 → 34/30. 이후 드라이버는 `try/except`로 고쳤고 데이터는 `handoffs.jsonl`·세션 jsonl에서 복원했습니다(위 표는 그 근거). 제품 결함 아님.

## Residue
- `shutdown --yes --json` exit 0, **`verified: true`**(host_shell·manager_omp·worker_omp·supervisor dead, survivors 0, 강제 kill 없음). 제가 시작한 attach/daemon 프로세스는 `quit`로 정상 종료, 소유 프로세스 잔여 없음(`/tmp/wb-cd70-smoke` 참조 프로세스 0).
- 파일: `/tmp/wb-cd70-smoke` 삭제(아래 확인), run dir에는 이 파일과 `-timeline.json`만 추가. `agent.db` 내용은 읽지 않았고 credential·token·다른 프로세스 environ은 보지 않았습니다. `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex`는 건드리지 않았습니다. commit·graphify update·코드 수정 없음.
