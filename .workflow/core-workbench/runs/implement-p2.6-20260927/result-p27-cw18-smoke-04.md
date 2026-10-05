# p27-cw18-smoke-04 — CW-18 실모델 experiment-flow smoke 결과 (C-D67 이후)

- 역할: test_designer (Opus) · 실행: 2026-10-05 11:51:40 ~ 12:00:05 KST (약 8.5분, 1회 실행)
- 결과: **execute 단계 수용, host run, 60 s review, 실행 종료와 cwd 복원, worker_busy까지 동작함.**
  그러나 **analysis(판정) 단계 worker 응답이 거부되어** Task가 `waiting_report` /
  `held_reason=runner_error:WorkerResponseRejected`에 멈춤. judge success와 manager 보고는 일어나지 않음. 시나리오 3에 따라 재시도 없이 중단함.
- provider request: **9/25**(manager 5, worker 4, error/aborted 0). wall-clock 약 8.5분/20분. cap에는 도달하지 않음.
- 진입점: `python -m workbench start --data-dir /tmp/wb-cw18-smoke-v564aeut/data --omp ~/.local/bin/omp --no-attach`
  (project dir에서 실행, 실제 HOME, Workbench-owned OMP home, `agent.db` symlink 확인, store는 열지 않음).
  product UI(`workbench attach`)를 PTY(pyte)에 붙였고 manager pane에 타이핑해 입력함. 상태는 `workbench status --json`으로 관측함.
- 코드 상태: HEAD 856fb08과 C-D67 미커밋 작업 트리(bridge.ts thinking 무시, worker_port `WorkerResponseRejected`).
  두 OMP 모두 `openai-codex` / `gpt-5.5`, omp/18.4.5, isolation `ok`(leaks/warnings 0).
- 드라이버: smoke-02b `smoke.py`를 고친 scratch `smoke4.py`(p27w `Ui`/`processes_mentioning`/`kill_exact` 재사용,
  0.5 s 간격 cap watcher 25 req/20분). request 수는 temp Workbench home 세션 jsonl의 assistant message 수.
  종료 후 temp DB(read-only), run.json, 세션 jsonl에서 **구조(content type, 길이, 필드 이름, 형식 검사 결과)만** 추출함. 모델 텍스트는 옮기지 않음.
- temp project: git repo(`exp.sh`: `EXP_START` 출력, `sleep 70`, `out/result.txt`에 RESULT=ok, 출력 RESULT=ok; `out/.keep`, `work/.keep`).
- 짧은 로그: `result-p27-cw18-smoke-04-timeline.json`(secret 없음)

## Timeline (KST)

| 시각 | req | 관측 |
|---|---|---|
| 11:51:41 | 0 | start exit 0, phase ready, isolation ok, providers manager/worker=`openai-codex`, `agent.db` symlink. 소유 프로세스 backend·bash·omp×2(cwd=project) |
| 11:51:42 | 0 | product UI attach, `worker: 대기 · 자동화: idle` |
| 11:51:44 | 0 | 1단계 지시 입력(experiment, `./exp.sh`, bash, environment none, criteria log_contains/result_file/result_contains) |
| 11:51:47 | 1 | manager `read`(skill, thinking 32자 동반) |
| 11:51:52.012 | 2 | **첫 native `to_worker`가 수용됨**(`status: dispatched`, `task_id=null`, spec dict). scope_approved, proceed 기록, run `started`, execute TASK delivery `api_accepted` |
| 11:51:57 | 3 | manager 한 줄 보고 후 2단계 지시 입력(작업 중 다른 to_worker를 한 번만 시도) |
| 11:51:58.451 | 4 | **worker execute 응답 = [`thinking`(보이는 텍스트 45자), `text`(marker, `decision:"execute"`)] → 수용됨**(C-D67 동작 확인). delivery `omp_processed`(`provider_request_matched:true`) |
| 11:51:58.53~.63 | — | shell_event `sent` → `accepted` → `started`. cwd = execution worktree(`data/workflow/worktrees/<task>-r1-…`). 상태줄 owner `manager`, `control_wait`, 자동화 `active` |
| 11:52:00.866 | 5 | **2단계: manager의 두 번째 `to_worker`(task_id 없음) → `status: worker_busy`**(task_id는 현재 Task). manager가 사용자에게 알리고 turn 종료(11:52:04, req 6) |
| 11:52:29 | 6 | 상태줄 `[실행 중] \| 60s 대조 29s 후` |
| 11:52:59.75 | 6 | **60 s periodic review** QUESTION(stage `review`) delivery(run 시작 후 약 61.8 s) |
| 11:53:03.8 | 7 | worker가 review에서 `read` 도구 호출(thinking 53자) |
| 11:53:08.64 | 7 | shell_event `ended`(exit 0, `exit_confirmed`, raw log 22 B, result 10 B 수집). source status 전후 모두 깨끗함 |
| 11:53:09 | 7 | Task `waiting_report`(일시적으로 held `worker_busy`: review turn이 아직 진행 중), shell owner `user`, `manual_prompt`, **host cwd = project(복원됨)** |
| 11:53:15.28 | 8 | worker review 답(thinking 155자 + text 25자, marker 없음), review delivery `omp_processed`. backend log `periodic review … -> omp_processed`. review_count 1 |
| 11:53:15.70 | 8 | **analysis QUESTION**(stage `analysis`) delivery |
| 11:53:17 | 8 | 자동화 `held`(`control_or_owner_unknown_or_drifted, run_not_ready`) |
| 11:53:21.684 | 9 | worker analysis 응답 = [`text` 481자] 하나(thinking 없음, stop `stop`), marker와 `decision:"success"`. delivery `omp_processed` |
| 11:53:22 | 9 | **Task held `runner_error:WorkerResponseRejected`**. `worker_judgment=null`, Task `last_result=null`, worker `busy` 유지 |
| 11:53:22~11:59:30 | 9 | 추가 request 없음. manager에게 notice가 가지 않음(manager 세션에 새 입력 없음). Task 활성, worker busy, 자동화 held 상태로 7분 넘게 그대로 |
| 12:00:00 | 9 | `shutdown --yes --json` exit 0, `verified:true`, 세 pane과 supervisor dead, survivors 없음 |
| 12:00:05 | 9 | 소유 프로세스 잔여 0, temp root 삭제, project `git status` 깨끗함 |

### Provider request 수
| OMP | request | content 형태(텍스트 제외) |
|---|---|---|
| manager | 5 | ① `thinking`(32자)+`toolCall:read` ② `toolCall:to_worker`(dispatched) ③ `text` ④ `toolCall:to_worker`(worker_busy) ⑤ `text` |
| worker | 4 | ① `thinking`(45자)+`text`(execute marker, **수용**) ② `thinking`(53자)+`toolCall:read`(review) ③ `thinking`(155자)+`text`(25자, review 답) ④ `text`(analysis marker, **거부**) |
| 합계 | **9** | error/aborted 0 |

## 거부된 analysis 응답 (사유 코드와 content type)

- content item types: **`[text]` 한 개**(thinking 없음, toolCall 없음), stop `stop`, 같은 delivery 안의 assistant message 1개, provider round 1회.
- **기록된 사유 코드: 없음.** analysis 단계 예외는 `flow_tasks.py:838`의 `runner_error:{type(exc).__name__}`로만 남음.
  run.json(`worker_judgment: null`), run/shell event, delivery details, Task `last_result`, backend.log 어디에도
  `WorkerResponseRejected.reason`이 없음. F2 수정(`run.py` `preparation_error.reason`)은 **preparation 단계에만** 적용되어 있음.
- **사유 재구성(오프라인, 저장된 세션 응답에 bridge 규칙 적용): `invalid_assistant_response:bad_marker`.**
  - marker 접두, compact flat JSON, 12개 field 순서, 공백 없음, stage `analysis`, kind `question`, revision 1, decision `success`(허용값),
    task/revision/run/message/delivery_attempt/session id는 모두 기대값과 일치함.
  - 실패 항목은 모델이 만드는 **`response_id` 하나**임. 그룹 길이가 `[8,4,4,4,11]`로 마지막 그룹이 12자가 아니라 **11자**여서
    bridge `UUID_PATTERN`(canonical_uuid)에 맞지 않음 → `parseWorkerResponse`가 `bad_marker`를 반환함. 소문자 hex, version 4, variant 8은 정상.
    execute 응답의 `response_id`는 `[8,4,4,4,12]`로 정상이었음.
  - bridge public event는 temp dir와 함께 사라져 실제 emit된 reason은 직접 보지 못함. 위 판정은 코드와 저장된 응답을 대조해 얻은 **추정(높은 확신)**임.

## 동작한 것
- 진입점, isolation, 공유 login, 실모델 호출, 확인된 shutdown, residue 0.
- **C-D67 확인:** worker execute 응답 앞의 보이는 thinking(45자)을 무시하고 marker만으로 판정해 수용함. smoke-03의 차단 요인이 해소됨.
- E1(identity): 세 delivery 모두 `omp_processed`, `provider_request_matched:true`.
- E2: 첫 native `to_worker` 수용.
- **host run:** shell idle 확인 후 Workbench가 execution worktree에서 `./exp.sh`를 입력해 실행함. 실행 중 owner `manager`/`control_wait`,
  종료 후 exit 0 확인, raw log·result 수집, **shell을 user에게 돌려주고 cwd를 project로 복원함**(`phase: control_returned`).
- **60 s worker review** 1회 delivery(약 61.8 s 시점)와 `omp_processed`. 상태줄 카운트다운(`60s 대조 29s 후`) 표시.
- **2단계 worker_busy:** task_id 없는 두 번째 `to_worker`가 `worker_busy`로 거절되었고, manager는 재시도 없이 사용자에게 알림(추가 request 2회).
- source repo 변경 없음(`source_status_before/after` 빈 값, 최종 `git status` 깨끗함).

## 동작하지 않은 것 / 분류
- **G1 (모델 행동 + 계약 설계, 차단): analysis 응답의 모델 생성 `response_id`가 잘못된 UUID(마지막 그룹 11자)여서 거부됨.**
  모델 실수이긴 하지만, 36자 UUID를 모델이 직접 만들게 하는 계약은 한 글자만 빠져도 실험 판정 전체를 잃는 구조임.
  선택지(Root/사용자 결정): (a) `response_id`를 bridge나 backend가 생성하고 모델 marker에서 뺌, (b) 1회 재질문 허용(provider 호출 1회 규칙과 충돌),
  (c) 현행 유지. 분류: 모델 행동이 원인이지만 계약이 취약함.
- **G2 (product defect, 높음): analysis 거부 후 Task가 멈춤.** Task `waiting_report`가 활성 상태로 남고 worker `busy`, 자동화 `held`가
  7분 넘게 유지됨. manager에게 notice가 가지 않아(E3는 start 실패에만 적용됨) manager와 사용자는 판정 실패를 알 수 없음.
  상태줄의 `[보고 대기 · runner_error:WorkerResponseRejected]`가 유일한 신호임. 기대 동작: indeterminate나 실패 REPORT/notice를 manager에 전달하고 worker를 해제함.
- **G3 (product 관측성, 중간): analysis 단계 거부 사유가 기록되지 않음.** `runner_error:WorkerResponseRejected`만 남고 `reason`(예: `invalid_assistant_response:bad_marker`)은
  어디에도 없음. F2를 analysis 단계(`run.py:340-366`과 `flow_tasks.py:838`의 held_reason 또는 run record)로 넓혀야 함.
- 관찰(결함 아님): 자동화 `held` 사유가 `control_or_owner_unknown_or_drifted, run_not_ready`로 표시됨. run 종료 뒤 shell이 user에게 돌아간 상태와 맞는 사유이지만,
  사용자에게는 "판정 대기 중 실패"보다 덜 명확함.
- 미검증(G1 때문): judge success, 결과 REPORT의 manager 전달, Task 종료와 worker idle 복귀.

## 상태줄 text screenshot (product UI 상단 2줄)

```
[attach 11:51:42]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 11:51:42 (0s 전) | Ctrl-] ? 도움말

[dispatch 직후 11:51:52]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the requested su…" [시작 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 11:51:52 (0s 전) | Ctrl-] ? 도움말

[실행 중 11:52:29]
focus: MANAGER OMP | host 입력 owner: manager | shell mode: control_wait | worker: 작업 중 | 자동화: active
작업: 실험 "Run ./exp.sh and judge the requested su…" [실행 중] | 60s 대조 29s 후 | backend: ready | bridge manager=ok worker=ok | 마지막 확인 11:52:29 (0s 전) | Ctrl-] ?

[review 후 / analysis 거부 후 11:53:22~, host focus도 같음]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: held
작업: 실험 "Run ./exp.sh and judge the requested su…" [보고 대기 · runner_error:WorkerResponseRejected] | 보류: automatic work held: control_or_owner_unknown_or_drifted,
```

## Residue
- 프로세스: `shutdown --yes` exit 0, `verified:true`. 소유 프로세스 잔여 0(강제 kill 없음).
- 파일: `/tmp/wb-cw18-smoke-v564aeut` 삭제 확인(worktree도 temp root 안에 있어 함께 삭제됨). scratch의 세션 사본, DB, screen, driver는 삭제함.
  run dir에는 이 파일과 `result-p27-cw18-smoke-04-timeline.json`만 남김.
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 프로세스(그 Workbench backend와 OMP 2개는 계속 실행 중)는 건드리지 않음. credential 내용과 다른 프로세스의 environ은 읽지 않았고
  token도 출력하지 않음. commit 없음, graphify update 없음, ledger는 수정하지 않음.

## 권고 (Root 판단용)
1. **G2를 먼저 수정:** analysis 실패나 거부 시 manager에 notice/REPORT(indeterminate)를 보내고 worker와 Task를 정리할 것.
2. **G3:** analysis 단계에도 `WorkerResponseRejected.reason`을 run record와 held_reason에 남길 것.
3. **G1 결정:** `response_id`를 모델에게 만들게 할지 결정할 것(권장 (a): bridge가 생성). 결정 후 같은 시나리오를 다시 실행하면 약 9~12 request로 예상됨.
