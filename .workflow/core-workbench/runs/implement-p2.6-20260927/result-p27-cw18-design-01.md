# p27-cw18-design-01 결과: CW-18 `to_worker`/`to_manager` 최소 설계

역할: code_explorer(읽기 전용). 이 파일 말고는 수정하지 않았다. 모델 호출이나 OMP 실행도 하지 않았다.
기준: C-D58, C-D60, C-D64, CW-18.md, SPEC s2.7, PLAN `CW-18`, 현재 작업 트리.

## 0. 결론

- 새 전달 경로는 만들지 않는다. 두 도구는 **도구 요청 frame 하나**(extension → backend)만 추가하고, 실제 전달은 이미 검증된 `TaskMailbox.deliver` → bridge `deliver` → `pi.sendUserMessage`가 그대로 맡는다. 이 경로에는 roleAllows, no replay, unknown 처리, pause 시 deferred→unknown이 이미 들어 있다.
- 승인이 필요한지는 backend가 판단한다. 도구 호출은 사용자 승인이 될 수 없다. 첫 `to_worker`, 그리고 승인 범위를 벗어난 `to_worker`는 Task 초안과 UI 승인 요청이 된다. 범위 안의 지시는 backend가 범위를 대조한 뒤 바로 전달한다(C-D60).
- 실행 흐름은 기존 `TaskWorkflow.start` → `WorkflowRun.collect/judge`를 그대로 쓴다. worker의 1차 판단(`execute/hold`, `success/failure/indeterminate`)은 지금 쓰는 marker 응답 계약(no tools, 1 provider round)으로 유지한다. `to_manager`는 그 밖의 보고에만 쓴다. 자유 답변, 코드 수정 필요 보고(C-AC-05), worker의 직접 요청이 여기에 해당한다. marker 계약을 도구 호출로 바꾸면 CW-10의 검증 근거(`tool_activity` → rejected, `providerRequestCount===1`)가 무너지므로 바꾸지 않는다.

## 1. 이미 있는 것과 없는 것

| 구성 | 상태 | 근거 |
|---|---|---|
| G3 전달(deliver/ack/state/event), roleAllows, no replay, pause/resume(`reconciled`) | 있음 | `omp_bridge/g3/bridge.ts:108,324-519`, `ipc/bridge_g3/protocol.py:35-49`, `mailbox.py:86-92,665` |
| worker 단계 응답 계약(marker, no tools) | 있음 | `bridge.ts:30-96,741-756,826-834`, `workflow/worker_port.py:28-` |
| extension 도구 등록 | **없음**. bridge.ts에 `registerTool`이 없다. 설치된 OMP 바이너리에는 `registerTool`/`sendUserMessage` 심볼이 있다(정확한 시그니처는 probe 필요). | `bridge.ts` 전체 |
| extension → backend 요청 frame | **없음**. `_receive`는 `state/api_ack/omp_event`만 처리한다. | `mailbox.py:281-307` |
| Task 생성·revision·범위 승인·proceed·run | 있음 | `tasks/repository.py:271,283,326,355,400` |
| 승인 → run → worker 결정 → host run → collect/judge → manager 보고 | 있음. 다만 `start()`가 **자체 `PersistentShell`을 만든다**. 제품 host pane(`ShellPane.shell`)을 주입할 수 없다. | `workflow/run.py:421-553` (512-513) |
| worker 직접 요청 분류(디스패치 권한 없음) | 있음 | `workflow/run.py:94-116,376-419` |
| Mailbox 메시지는 run에 묶임(`create_message`가 run 존재를 확인) | 있음. run 전의 대화는 mailbox로 보낼 수 없다. | `mailbox.py:576-600` |
| deferred 메시지 재전송 허용(그 밖의 상태는 no replay) | 있음 | `mailbox.py:674`, `bridge.ts:418-438` |
| `LifecycleCoordinator.tick`, `bind_production`, 60초 `WorkerReviewScheduler`, `PauseCoordinator` | 있음. 배선 예는 `tests/lifecycle/test_production_binding.py:553` | `app/lifecycle.py:345,546`, `app/production.py:180`, `observation/worker_review.py:736,911` |
| backend loop와 흐름 연결 | **없음**. `automation`이 `not_configured`이고 `set_automation_status`는 포트만 있다. | `backend/service.py:81-82,287-309,718-720` |
| ui_v1 승인·pause·resume 메시지 | **없음** | `contracts/ui_v1.py:107-123` |
| UI 승인 표시 | **없음**. 상태줄에 자동화 문자열 하나뿐이다. | `ui/product/model.py:1616-1628` |
| Workbench skill | **없음**. README만 있다. role 필터 `ROLE_SKILL_PATTERNS`는 비어 있다. | `omp_bridge/skills/README.md`, `backend/launcher.py:115-116` |
| C-D58 막다른 상태 | 있음(결함). 보류된 `wb-handoff`의 `control_wait`에서 수동 입력이 "request takeover"로 거부된다. `confirm_takeover`도 `sent_id`나 foreground가 없으면 예외다. | `terminal/shell_persistent/adapter.py:344-381,409-421` |

## 2. 도구 스키마와 전달

### 2.1 extension(bridge.ts)

`role==="manager"`일 때 `to_worker`만, `role==="worker"`일 때 `to_manager`만 `pi.registerTool`로 등록한다. `execute(toolCallId, params)`가 하는 일은 다음과 같다.

1. `{kind:"tool_request", requestId, toolCallId, tool, args, sessionId, generation}`를 기존 소켓으로 보낸다.
2. 같은 `requestId`의 `{kind:"tool_result", ...}`를 기다린다. 최대 10초다.
3. 결과 JSON을 도구 결과 텍스트로 돌려준다.
4. 시간을 넘기거나 연결이 끊기면 `{status:"outcome_unknown"}`를 돌려주고, 같은 요청을 다시 보내지 않는다.

도구 결과는 즉시 돌아온다. 전달 완료를 기다리지 않는다. 호출한 OMP가 turn 안에서 상대 OMP의 idle을 기다리면 교착이 생기기 때문이다.

### 2.2 `to_worker` (manager 전용)

입력:
```
{ task_id?: uuid,                 // 없으면 새 Task
  message: string,                // 사람이 읽는 지시·요약 (필수, ≤8 KiB)
  spec?: { goal, paths[], execution:{source, commit, command, criteria{log_contains,result_file,result_contains}, environment:[names], shell} }
  run?: boolean }                 // true면 승인 범위 안의 (재)실행 요청
```

출력 `status`:
- `approval_pending {task_id, revision, approval_id}`: 새 Task이거나 범위 밖이다. UI에 승인 요청이 뜬다.
- `queued {message_id}`: 현재 run에 대한 자유 지시다. 비단계 `question`으로 worker에게 간다.
- `run_scheduled {task_id, revision}`: 범위 안의 재실행이다. backend가 `TaskWorkflow.start`를 실행한다.
- `held {reason}`: pause, 재시도 한도 소진, 다른 run 진행 중, takeover 진행 중.
- `rejected {reason}`: 스키마 위반, 환경 변수 값 포함, 알 수 없는 task 등.

### 2.3 `to_manager` (worker 전용)

입력:
```
{ kind: "answer"|"report",
  message: string,
  in_reply_to?: message_id,
  requires_code_change?: boolean, reason?: string,
  request?: {goal, paths[]} }
```

출력 `status`: `queued {message_id}`, `held {reason}`, `rejected {reason}`.

- 현재 run이 없으면 `rejected: no_active_run`이다. worker의 직접 요청(`request`)은 `route_worker_request`로 분류만 하고 manager에게 보고한다. 디스패치 권한은 주지 않는다.
- 단계 delivery(`response_contract`가 있는 메시지)를 처리하는 동안 호출하면 bridge가 그 응답을 `tool_activity`로 거부한다(`bridge.ts:826-834`). skill에서 이렇게 쓰지 않도록 금지한다.

### 2.4 backend 처리

`flow.py`의 `HandoffService`가 담당한다.

- 인증과 역할: `_receive`에 `tool_request` 분기를 추가한다. role은 hello 토큰으로 인증된 peer의 role을 쓴다. extension이 보낸 값은 쓰지 않는다. `tool` 이름과 role이 맞지 않으면 rejected다.
- 중복 처리: `(role, session_id, generation, toolCallId)` 키로 한 번만 처리한다. 같은 키가 다시 오면 처음 결과를 그대로 돌려준다.
- outbox: 큐에 있는 메시지는 `TaskMailbox.create_message`로 만든다. backend loop 밖의 작업 thread가 `deliver`를 반복한다. 상태가 `DEFERRED`일 때만 다시 보내고, unknown과 rejected는 기록만 하고 다시 보내지 않는다. 결과는 UI 상태로 내보낸다.
- 일시정지: pause 중이면 `held:paused`로 응답한다. 큐에 넣지 않고, resume 뒤에 자동으로 보내지도 않는다. manager가 다시 지시해야 한다.

## 3. 승인 모델

**Task 범위**는 사용자가 승인한 `scope_approved.details` 하나다. 내용은 `{goal, paths[], execution(source·command·criteria·environment 이름·shell), commit_policy:"manager_local_commits", retry_limit:3}`이다.

**첫 `to_worker`가 승인 요청이 되는 과정**은 다음과 같다.
1. `create_task`(또는 `revise_task`)를 실행한다.
2. pending approval `{approval_id, task_id, revision, summary, spec, diff_vs_approved}`를 만든다.
3. ui_v1 state에 `approvals:[...]`를 추가한다.
4. UI에 상단 OMP pane 위로 승인 카드(overlay)를 띄운다. 키는 prefix 명령으로 `a` 승인+진행, `r` 거절, `v` 상세다. IME 중립 prefix 방식을 그대로 쓴다.

**사용자 조작**: ui_v1에 새 메시지 3개를 추가한다.
- `approval_decide {approval_id, decision: approve|reject}`
- `pause {}`
- `resume {reconciled: true}`

승인은 `approve_scope` 다음에 `proceed`를 같은 사용자 조작 하나로 기록한다(C-AC-29: 별도 AUTO ON 조작 없음). 그 뒤 작업 thread에서 `TaskWorkflow.start`를 실행한다. 거절하면 결과 메시지를 manager에게 `to_worker` 결과 알림으로 보낸다. 이 알림은 run이 없으므로 mailbox가 아니라 다음 manager 도구 결과나 UI로 전한다.

**이후 `to_worker`의 범위 판정**(backend 판정, 결정적):
- 같은 task이고 `execution`의 source·command·criteria·environment·shell이 같다. 달라도 되는 것은 `commit`뿐이다.
- `paths`가 승인된 paths 안에 있다. `classify_worker_request`를 재사용한다.
- 위 조건을 만족하면 `revise_task`를 실행하고, **파생 scope_approved**를 기록한다. 기록 내용은 `{…, derived_from: <사용자 decision_id>, actor:"backend_scope_check", tool_call_id}`다. `proceed`도 같은 근거로 기록한다(`details.source="manager_to_worker"`).
- 재시도 횟수는 CW-13 정책의 `retry_count_used`로 센다. 한도를 넘으면 `held:retry_limit`이다.
- 조건을 하나라도 벗어나면 새 승인 요청이다.

**기록되는 것**:
- 모든 도구 요청과 그 결과: `workflow/handoffs.jsonl`(fsync, 0600). `to_worker`의 메시지 본문은 그대로 기록하고, 환경 변수 값은 거부한다.
- 승인과 거절: UI 요청 id, attach 세션, decision_id.
- mailbox delivery history, run event, shell event는 기존 SQLite에 남는다.

## 4. 자동화 구성 (CW-18 수락 기준)

- **필수(L-CW18-FLOW)**
  - 승인 전에는 아무것도 실행하지 않는다.
  - 승인 → `TaskWorkflow.start`를 실행한다. 이때 **제품 host shell을 주입**한다. `run.py`에 `shell=` 인자를 추가하는 CW-10 delta이며, 기존 경로는 기본값으로 그대로 둔다.
  - collect/judge를 거쳐 manager에게 report를 보내고, `complete_run`/`fail_run`을 실행한다.
  - 수정이 필요하다는 보고가 오면 그 지시를 끝내고 worker에게 자동으로 되돌리지 않는다.
- **필수(L-CW18-POLICY)**
  - run이 시작되면 첫 `LifecycleRecord`를 기록하고 `bind_production`을 실행한다.
  - backend loop에서 1초마다 `coordinator.tick()`을 호출한다. 60초 점검과 busy 병합은 기존 `WorkerReviewScheduler`가 맡는다.
  - `PauseCoordinator`가 pause를 처리하는 동안에도 수집은 계속한다. 중단은 요청·확인·불명으로 구분해 표시한다. resume은 `reconciled=true`일 때만 받는다.
  - 재시도 3회, 다음 revision, 강제 종료는 기존 정책을 연결하기만 한다.
- **C-D58**: shell adapter delta(CW-07)다. `control_wait`이고 `request_id`가 없는 보류 상태에서 `request_takeover`는 `release_idle`로 parent prompt를 사용자에게 돌려준다. `confirm_takeover`는 이미 `manual_prompt`면 성공으로 처리한다. 이렇게 하면 사용자는 정리한 뒤 다시 `wb-handoff`를 할 수 있다. 먼저 현재 막다른 상태를 재현 테스트로 고정한 뒤 고친다.
- **나중에 해도 되는 것**: 승인 카드의 diff를 보기 좋게 다듬는 것, outbox 상태 패널(지금은 상태줄 카운트로 충분), 다중 Task. 동시에 활성 Task는 하나로 고정한다.

## 5. Workbench skill

- 위치:
  - `omp_bridge/skills/wb-manager/SKILL.md`, `omp_bridge/skills/wb-worker/SKILL.md`
  - `ROLE_SKILL_PATTERNS = {"manager":("wb-manager",), "worker":("wb-worker",)}`
  - Workbench 홈은 `home_config`의 `customDirectories`로 이미 이 디렉터리를 가리킨다(`omp_home.py:311-322`). C-D60의 `order-worker` 이름은 C-D64에 따라 `wb-manager`로 바꾸는 안을 권장한다(Q3).
- wb-manager 내용(영어, 1쪽 이내):
  1. 사용자와 목표를 논의한다.
  2. 실험을 맡길 때는 `to_worker`에 `spec`을 넣어 보낸다. 승인은 사용자가 UI에서 한다. 결과가 `approval_pending`이면 기다린다.
  3. 승인된 범위 안에서는 commit만 바꿔서 `run:true`로 재실행한다.
  4. worker 보고는 `{"kind":"report",...}` JSON 사용자 메시지로 도착한다. 그 1차 판단을 완료 조건과 대조해 2차로 확인하고 사용자에게 보고한다.
  5. 범위 밖이거나 재시도 3회를 다 썼으면 멈추고 보고한다. 환경 변수 값은 넣지 않는다. push/merge 금지.
- wb-worker 내용:
  1. `response_contract`가 있는 메시지에는 marker 한 줄만 답한다. 도구 호출이나 설명 문장을 붙이지 않는다.
  2. 그 밖의 질문에는 `to_manager(kind:"answer", in_reply_to)`로 답한다.
  3. 코드 수정이 필요하면 `to_manager(kind:"report", requires_code_change:true, reason)`를 보내고 거기서 멈춘다.
  4. host shell을 직접 조작하지 않고 코드를 수정하지 않는다.

## 6. 테스트와 실측 계획

- **fake OMP**(모델 호출 0, `tests/bridge`의 fake peer 방식):
  - 역할별 도구 거부, 토큰/role 위장 거부, toolCallId 중복 처리
  - pause 중 `held`, resume 뒤 자동 전송 없음
  - deferred 재전송, unknown 재전송 금지
  - 승인 전 run 없음, 범위 판정 표(commit만 바뀐 경우는 범위 안, command가 바뀐 경우는 새 승인), 파생 승인 기록
  - 거절 흐름, 재시도 한도, ui_v1 새 메시지 파싱
  - `run.py`에 shell을 주입하는 회귀 테스트와 C-D58 재현·수정 테스트
  - backend `tick`이 60초 점검을 시작하는지(시계 주입)
- **scripted provider + 실제 OMP 두 개**(실제 모델 호출 0, 진입점 `python -m workbench`, 기존 `_worker_provider`/`_semantic_provider` 재사용): L-CW18-FLOW와 L-CW18-POLICY 전 항목. 먼저 `registerTool` 시그니처와 도구 결과 반환을 확인하는 probe를 1회 돌린다.
- **실제 provider E2E**(UR-USABILITY 전 smoke 1회): 시나리오는 목표 논의 → `to_worker`(승인 요청) → 사용자 승인 → worker execute 결정 → host run(약 2분) → 60초 점검 2회 → worker analysis → manager 2차 확인 → worker 자유 질답 1회(`to_worker`/`to_manager`). 예상 provider 호출은 **약 12~16회**다. manager 5~7회, worker 5~7회, 점검 2회이며, 도구 호출 하나마다 provider round가 2회씩 든다.

## 7. 작업 단위

| 단위 | 내용 | 위험 | 모델 |
|---|---|---|---|
| U1 bridge+handoff | bridge.ts 도구 2개, `tool_request` frame, `_receive` 분기, `flow.py` HandoffService(멱등·outbox·기록), probe | 중간: OMP 도구 API와 재연결 시 결과 불명 | **Opus** |
| U2 approval+run 연결 | 범위 판정과 파생 승인, ui_v1 메시지 3개, 작업 thread의 `TaskWorkflow.start`, `run.py` shell 주입(CW-10 delta) | **높음**: 권한 경계, 사용자 shell 공유 | **Opus** |
| U3 loop | `LifecycleRecord`/`bind_production`, tick·pause·resume, 자동화 상태 publish | 높음: 수명 불변식 | **Opus** |
| U4 C-D58 | adapter takeover delta(CW-07) | 높음: PTY·입력 owner | **Opus** |
| U5 UI | 승인 카드·키, pause 표시, outbox/중단 상태 문구 | 낮음 | Sonnet (wb-worker) |
| U6 skill | SKILL.md 2개와 role 패턴, 격리 검사 갱신 | 낮음 | Sonnet |

순서는 U1 → U2 → (U3 ∥ U4) → U5 → U6이고, 그 뒤 독립 test_designer와 fresh reviewer를 거친다.

**PLAN 변경 기록이 필요하다.** 현재 write scope에는 `contracts/ui_v1.py`, `ui/product/**`, `backend/service.py`, `ipc/bridge_g3/mailbox.py`, `workflow/run.py`, `terminal/shell_persistent/adapter.py`, `omp_bridge/skills/**`, `backend/launcher.py`가 없다. 또 `tests/bridge/test_manager_task_tool*`를 `to_worker`/`to_manager` 테스트로 바꿔야 한다.

## 8. 사용자 결정이 필요한 것 (권장 기본값 포함)

1. **Q1. 범위 안의 기준**: 권장안은 같은 Task이고 실행 명령·판정 기준·환경 이름·shell이 같으며 commit만 바뀐 재실행과 승인 paths 안의 지시다. 이 밖은 모두 새 승인을 받는다.
2. **Q2. host shell에 run 보내는 주체**: 권장안은 host shell이 사용자 소유의 깨끗한 prompt이고 job이 없을 때만 Workbench가 `cd`와 `wb-handoff`를 대신 입력하는 것이다. 그렇지 않으면 "host terminal 정리 필요"를 표시하고 보류한다. 대안은 사용자가 직접 UI handoff를 하는 것이다.
3. **Q3. skill 이름**: 권장안은 `wb-manager`/`wb-worker`다. C-D60의 `order-worker` 이름은 쓰지 않는다.
4. **Q4. 실제 provider E2E**: 권장안은 UR-USABILITY 전에 약 12~16회 호출하는 smoke를 1회 돌리는 것이다. 자동 gate는 scripted provider로 한다.
