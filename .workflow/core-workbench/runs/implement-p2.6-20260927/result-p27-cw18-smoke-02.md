# p27-cw18-smoke-02 — CW-18 실모델 end-to-end 재smoke 결과 (D1-D3 수정 후)

- 역할: test_designer (Opus) · 실행: 2026-10-05 09:04:57 ~ 09:13:54 KST (두 번 실행, 합계 약 9분)
  - 02a 09:04:57~09:08:28: 시나리오 1~3
  - 02b 09:12:23~09:13:54: experiment 시작 실패가 재현되는지만 확인
- 결과: **work 경로와 D1/D3 수정은 실모델에서 동작함. experiment Task는 두 번 모두 run 시작 단계에서
  `start_failed:WorkflowHeld`로 실패함(새 product defect E1).** 60 s review, judge, cwd 복원, worker_busy는 관측하지 못함.
- provider request: **25/40**(02a 21, 02b 4). wall-clock 약 9분/25분. cap에는 도달하지 않음.
- 진입점: `python -m workbench start --data-dir /tmp/wb-cw18-smoke-*/data --omp ~/.local/bin/omp --no-attach`
  (실제 HOME, Workbench-owned OMP home, `agent.db` symlink 확인, store는 열지 않음). product UI(`workbench attach`)는
  PTY(pyte)에 붙였고, 입력은 manager pane에 타이핑함. 상태는 `workbench status --json`으로 확인함.
  repo는 commit 801671d에 smoke-fix 미커밋 working tree를 더한 상태임.
- 두 OMP 모두 provider `openai-codex`(isolation `ok`, leaks/warnings 0, omp/18.4.5).
- 드라이버: smoke-01의 `smoke.py`를 재사용함(p27w `Ui`/`processes_mentioning`/`kill_exact`). 변경점은
  cap 40/25분, 0.5 s 간격 background cap watcher(cap에 닿으면 즉시 prefix p,p로 pause), 프롬프트 문구.
  request 수는 temp dir 안의 Workbench home 세션 jsonl에 있는 assistant message 수로 셌음.
- 짧은 로그: `result-p27-cw18-smoke-02-timeline.json`(02a/02b, secret 없음)

## Timeline (KST)

### 02a
| 시각 | req | 관측 |
|---|---|---|
| 09:04:58 | 0 | start exit 0, phase ready, isolation ok, `agent.db` symlink |
| 09:04:59 | 0 | product UI attach, `worker: 대기 · 자동화: idle` |
| 09:05:01 | 0 | 1단계 지시 입력(영문, `work/hello.txt`, repo 상대경로) |
| 09:05:05 | 1 | manager가 `skill://to-worker`를 read |
| 09:05:09 | 2 | **첫 native `to_worker` 호출이 바로 `dispatched`됨**(미사용 필드 null, `spec.execution:null`, eval 없음). Task work/running, worker busy, 자동화 active |
| 09:05:11 | 3 | manager가 "dispatched" 한 줄로 **turn 종료** |
| 09:05:14~22 | — | worker: skill read → write → raw read로 확인 → `to_manager done`(null 필드, `queued`) |
| 09:05:22 | 7 | Task closed(`done`), worker idle, 자동화 idle. report가 manager에 도착(새 user message) |
| 09:05:24 | — | manager가 report를 요약하고 turn 종료. `done_report_pending` 없음 |
| 09:05:55 | 9 | 2단계 지시 입력(experiment: `./exp.sh`, log_contains RESULT=ok, bash, env 없음) |
| 09:06:00 | 10 | `to_worker` experiment → **rejected** `spec.execution.criteria: needs non-empty log_contains, result_file, result_contains`(E2) |
| 09:06:12 | 11 | manager가 `exp.sh`를 read함 |
| 09:06:19 | 12 | 두 번째 호출(`result_file:"exp.sh"`, `result_contains:"RESULT=ok"`를 임의로 넣음) → `dispatched` |
| 09:06:19.789 | — | run 시작: execute TASK가 worker에 `api_accepted`됨. **34 ms 뒤 receipt `unknown`(`provider_request_identity_not_observed`)** → `WorkflowHeld("worker instruction was not confirmed processed")` → Task `finished`, `held_reason=start_failed:WorkflowHeld`, worker idle. host shell에는 아무것도 입력되지 않음(phase `not_sent`) |
| 09:06:21 | — | manager는 "Experiment task dispatched"로 turn 종료. 이 시점에는 실패를 모름 |
| 09:06:25 | — | worker가 contract대로 `WB_WORKER_RESPONSE:{... "decision":"execute"}`로 응답함. backend는 이미 포기한 뒤였음 |
| 09:06:25 | 13 | 3단계 지시 입력(실행 중이라면 second.txt를 시도해 보라는 내용). 실험이 이미 끝나 worker가 idle이었으므로 `to_worker` → `dispatched`. 결과의 `notices[]`에 `run_start_failed`가 있었고 manager가 이를 사용자에게 알림 |
| 09:06:36 | 19 | second.txt work Task closed(done). report 도착, manager가 turn 종료 |
| 09:08:24 | 21 | 시나리오가 더 진행될 수 없어 driver를 SIGINT로 중단(finally에서 정리). `shutdown --yes` exit 0, `verified:true` |
| 09:08:28 | 21 | 소유 프로세스 잔여 0, temp root 삭제 |

### 02b (재현 확인)
| 시각 | req | 관측 |
|---|---|---|
| 09:12:24~27 | 0 | 새 temp root에서 start, ready, attach. 지시 입력(criteria는 log + `out/result.txt` 세 항목 모두 자연스럽게 지정) |
| 09:12:34 | 2 | skill read 후 첫 native `to_worker` 호출이 `dispatched`됨 |
| 09:12:34.345 | — | **같은 실패: execute TASK가 `api_accepted` 후 8 ms 만에 `unknown`(`provider_request_identity_not_observed`)** → `start_failed:WorkflowHeld` |
| 09:12:36 | 3 | manager turn 종료. **이후 manager에게 실패 알림이 오지 않음**(E3) |
| 09:12:41 | 4 | worker가 `WB_WORKER_RESPONSE ... "decision":"execute"`로 응답함(사용되지 않음) |
| 09:13:49 | 4 | `shutdown --yes` exit 0, `verified:true`. 09:13:54 잔여 0, temp root 삭제 |

### Provider request 수
| 실행 | manager | worker | 합계 |
|---|---|---|---|
| 02a | 11 (skill read 1, to_worker 4[rejected 1], read 1, 종료 텍스트 5) | 10 (skill read, write/read/to_manager ×2, 종료 텍스트 2, execute 응답 1) | 21 |
| 02b | 3 (skill read, to_worker, 종료 텍스트) | 1 (execute 응답) | 4 |
| **합계** | 14 | 11 | **25** (aborted/error 0) |

## smoke-01과 비교 (4단계)

| 항목 | smoke-01 | smoke-02 |
|---|---|---|
| 첫 native `to_worker` 수용 | rejected 18회, `eval`로 우회 | **3번 중 3번 첫 native 호출에서 dispatched**. rejected 1회는 criteria 계약 때문(E2), 수정 후 1회에 통과 |
| optional 필드 | 가짜 UUID와 placeholder를 채움 | **모두 `null`**(manager의 `task_id/run/cancel/execution`, worker의 `in_reply_to/requires_code_change/reason/request`) |
| dispatch 후 turn 종료 | `wait` tool과 polling 반복 | **매번 한 줄 보고 후 turn 종료**. `wait`, history read, polling 없음 |
| report 수신 | `done_report_pending` 상태 유지 | **두 work Task 모두 report 도착 → closed(done) → worker idle**. 수신 후 manager가 요약함 |
| 상대경로 | 절대경로로 `invalid_paths` | `work/hello.txt` 상대경로로 바로 통과 |

→ D1, D2, D3의 수정은 실모델(openai-codex)에서 의도대로 동작함.

## 동작한 것
- 진입점, isolation, 공유 login, 두 OMP의 실모델 호출, 확인된 shutdown, residue 0(두 번 모두).
- work Task 2건의 전체 흐름: dispatch → worker 자기 tool로 허용 경로 안에서 작업 → `to_manager done` → manager 수신 → closed → worker idle.
  결과 파일: `work/hello.txt`=`hello from worker\n`, `work/second.txt`=`second from worker\n`. git status에는 두 파일만 `??`로 나타남.
- backend 계약 거부(`criteria` 불완전)에 manager가 한 번만 수정해서 재시도함(같은 호출을 반복하지 않음).
- `notices[]`의 `run_start_failed`가 다음 `to_worker` 결과로 전달되었고 manager가 사용자에게 알림.
- 상태줄: 대기→작업 중→대기, 자동화 idle→active→idle, Task 요약과 `[실행 중]`/`[종료(완료)]`.
- start 실패 때 host shell에는 아무것도 입력되지 않음(phase `not_sent`, owner user, manual_prompt 유지). no replay도 지켜짐.

## 동작하지 않은 것 / 분류

- **E1 (product defect, 높음 — experiment 경로 전체 차단): bridge의 provider-request identity 판정이 실 provider에서 항상 실패함.**
  `delivery_status_events`를 보면 이번 smoke의 **모든 delivery**(worker TASK 4건, manager report 2건, 02b execute 1건)가
  `api_accepted` 후 20~40 ms 안에 `unknown`(`provider_request_identity_not_observed`, `provider_request_matched:false`,
  `agent_end_observed:false`)으로 끝났음. work/report 경로는 `api_accepted`만으로 진행되어 영향이 드러나지 않지만,
  experiment run 시작은 `MailboxStatus.OMP_PROCESSED`를 요구하므로(`src/workbench/workflow/run.py:612-613`) 항상 `WorkflowHeld`가 됨.
  worker는 실제로 메시지를 처리하고 contract대로 `decision:"execute"`로 응답했음(02a 6 s, 02b 7 s 뒤).
  판정 코드는 `omp_bridge/g3/bridge.ts` `before_provider_request` → `providerRequestContainsActiveDelivery()`(l.393-423)로,
  `event.payload.messages`의 마지막 `role:"user"` text block만 확인함. **추정 원인:** openai-codex(Responses API) 요청
  payload에는 `messages`가 없고 `input` 등 다른 형태여서 첫 provider request에서 일치하지 않음 → 즉시 `markDeliveryUnknown`.
  (OMP 바이너리 내부와 실제 payload key는 확인하지 않음. scripted provider를 쓰는 live probe는 `messages` 형태라서 통과했던 것으로 보임.)
  이 판정은 smoke-fix 이전부터 있던 코드임(9b3ea72). judge 같은 다른 staged worker 응답도 같은 경로를 쓸 가능성이 높음(미확인).
  02a와 02b에서 두 번 결정적으로 재현됨(race가 아님).
- **E2 (계약/skill gap, 중간):** `criteria`는 `log_contains`, `result_file`, `result_contains`가 모두 비어 있지 않아야 함.
  to-worker skill은 세 필드를 나열만 하고 "모두 필수"라고 하지 않음. 사용자가 log 조건만 원했는데도 manager가
  `result_file:"exp.sh"`, `result_contains:"RESULT=ok"`라는 의미 없는 보조 조건을 만들어 통과시킴. 사용자 의도와 실제
  판정 조건이 달라지는 문제임. 계약을 log만으로도 허용할지, skill에 "세 항목 모두 필수"라고 쓸지는 Root/사용자가 결정할 일임.
- **E3 (product gap, 중간):** run 시작 실패(`start_failed`)는 manager에게 능동적으로 전달되지 않음. 다음 `to_worker`
  결과의 `notices[]`로만 보임. 02b에서 manager는 "Started experiment Task"라고 말한 뒤 실패를 알지 못한 채 멈춤.
  사용자가 상태줄을 보지 않으면 실험이 시작되지 않았다는 사실을 모름.
- **E4 (UI 문구, 낮음):** run이 한 번도 실행되지 않은 Task의 상태줄이 `[보고 완료 · start_failed:WorkflowHeld]`로 표시됨.
  "보고 완료"는 오해를 부름(실제로는 `finished` + start 실패).
- **모델 행동:** 큰 문제 없음. 3단계 지시("실행 중이라면")에서 worker가 idle이었으므로 새 Task를 보낸 것은 정상임.
- **미검증(E1 때문):** Workbench의 run 입력, host shell idle 확인 후 실행, 60 s worker review, judge/report,
  shell 반환과 cwd 복원, worker_busy(experiment가 실행되지 않아 busy 구간이 없었고, work Task는 약 10 s 만에 끝나 시도하지 않음).

## 상태줄 text screenshot (product UI 상단 2줄)

```
[02a attach 09:04:59]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 09:04:59 (0s 전) | Ctrl-] ? 도움말

[02a work dispatch 09:05:10]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
작업: 작업 "Create work/hello.txt containing exactl…" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 09:05:09 (0s 전) | Ctrl-] ? 도움말

[02a work 완료 후 09:05:53]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 작업 "Create work/hello.txt containing exactl…" [종료(완료)] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 09:05:52 (0s 전) | Ctrl-] ? 도움말

[02a experiment 시작 실패 09:06:22, host focus]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge success by log c…" [보고 완료 · start_failed:WorkflowHeld] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 09:06:22 (0s 전

[02b 최종 09:13:45]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge success criteria." [보고 완료 · start_failed:WorkflowHeld] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 09:13:45 (0s 전
```

## Residue
- 프로세스: 두 실행 모두 `shutdown --yes` exit 0, `verified:true`(세 pane dead, survivors 없음). 소유 프로세스 잔여 0이고 강제 kill 없음.
  02a driver는 내가 띄운 PID에 SIGINT를 보내 중단함(finally 정리 정상 수행).
- 파일: `/tmp/wb-cw18-smoke-5oofcqm2`, `/tmp/wb-cw18-smoke-du3q3pny` 삭제 확인. scratch의 세션/DB 사본은 삭제함.
  run dir에는 이 파일과 `result-p27-cw18-smoke-02-timeline.json`만 남김.
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 프로세스는 건드리지 않음. credential 내용과 다른 프로세스의 environ은 읽지 않았고
  token도 출력하지 않음. commit 없음, graphify update 없음, ledger는 수정하지 않음.

## 권고 (Root 판단용)
1. **E1 최우선:** provider-request identity 판정을 provider 형태와 무관하게 바꾸거나(예: Responses `input` 형태 지원,
   또는 session에 들어간 user message의 envelope와 그 뒤 assistant 응답으로 처리 여부 판정), OMP가 실제로 보내는
   openai-codex payload 형태를 먼저 관측할 것(event surface probe, key 이름만 기록). 이것이 고쳐지기 전에는 실모델에서 experiment가 하나도 시작되지 않음.
2. E2: criteria 필수 범위를 결정하고 skill/schema 설명에 반영할 것.
3. E3: `start_failed`(그리고 `run_start_failed` 계열)를 manager에게 능동적으로 알리는 경로가 필요한지 결정할 것. E4는 상태 라벨 문구.
4. E1 수정 후 experiment 경로(60 s review, judge, cwd 복원, worker_busy)만 다시 smoke할 것. 예상 비용은 약 15 request.
