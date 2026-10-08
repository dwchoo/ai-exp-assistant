# p27-cw16-b2-01 — CW-16 B2: flow·policy·shell 시나리오 (test_designer)

작성 2026-10-08. W 단계 작성과 비공식 자기 실행 결과다. 정식 근거는 Root가 동결한 C16에서 B4가 다시 실행한다.
`src/**`, `omp_bridge/**`, `docs/**`는 고치지 않았고 `cw16_harness.py`도 바꾸지 않았다(추가 helper는 새 파일 `cw16_flow_rig.py`에만 있다). 실제 모델·provider 요청은 0회다(scripted local provider만 사용, 보고서의 `provider.real_model_requests: 0`). 자격 증명은 열지 않았고, 모든 sandbox는 fake HOME(빈 `agent.db`)과 proxy 차단 상태에서 돌렸다. 사용자 tmux/herdr·VM·`~/wb-urux-sandbox`에는 접근하지 않았다. 신호는 내가 띄운 프로세스에만 pid+start ticks로 보냈다. commit과 graphify update는 하지 않았다.

## 1. 변경 파일

| 파일 | 내용 |
|---|---|
| `tests/integration/cw16_flow_rig.py` (신규) | B2 공통 rig(B1 harness 위에 얹음). 주입 메시지 전체를 역할별로 기록하는 recording rule, tool 결과와 도착 시각, bridge tool 전체 인자 builder(strict schema null 채움), staged contract 응답(`live_workflow_probe._frame_from_contract`), UI 입력(prefix 1/2/3), pause/resume, journal 읽기, 시나리오 보고·정리 |
| `tests/integration/live_cw16_flow_policy.py` (신규) | F1–F7, F45, P1–P8(P7은 P1과 함께), CLK(clock fixture suite). opt-in `WB_LIVE_CW16=1`. CLK만 live 조건 없이 실행된다 |
| `tests/integration/live_cw16_shell.py` (신규) | SH1–SH5 × {bash, dash}(SH5는 bash PROMPT_COMMAND / trap DEBUG / trap CHLD, dash PS1 / trap CHLD) |

18.2.10 고정 테스트는 고치지 않았다(§5).

## 2. 시나리오 결과 (자기 실행 run `cw16-b2-selfrun-164526`)

보고서: `/tmp/wb-cw16-b2-reports/cw16-b2-selfrun-164526/` (시나리오별 JSON, `flow-policy-summary.json`, `shell-summary.json`, `evidence/`). Root가 run 디렉터리로 복사하면 된다. 버전: OMP `omp/18.8.0`(격리 check `ok`), GNU bash 5.2.21, dash 0.5.12, Python 3.12.3, kernel 7.0.0-30. 모든 회차에서 `shutdown --yes` verified, `residue_before_fallback` = {}, root 제거를 확인했다.

| ID | 결과 | 핵심 관측 | 보고서 |
|---|---|---|---|
| F1 | pass | manager 입력 → `to_worker(work, commands 2개)` dispatched. worker `terminal`: `exited 0`, `output_tail` `F1_ONE_42`, log_path 존재. 목록 밖 명령은 `not_in_task_commands`로 거절되고 실행되지 않음(파일 없음). done 보고 payload `commands_run` 2건(command·exit 0·log_path). Task `closed/done`, worker idle. host pane에 `[worker] $ …`와 출력 표시 | flow-F1-F4.json |
| F4 | pass | 모델이 같은 call id를 다시 내면 OMP 18.8.0이 `<id>_dup1`로 이름을 바꿔 보낸다. 백엔드는 이를 새 요청으로 보고 `worker_busy`로 답하며 두 번째 Task는 생기지 않음(worker Task 주입 1건). busy 중 새 Task는 `worker_busy`이고 같은 task_id, UI `worker: 작업 중`. 정확히 같은 toolCallId의 멱등성은 OMP를 거쳐서는 만들 수 없어 CLK의 `test_handoff_service.py`·`test_flow_terminal.py`로 보완했다 | flow-F1-F4.json |
| F2 | pass | 네 사례 모두 worktree(`d/workflow/worktrees/<task>-r1-…`), execute→analysis 순서, manager 보고 payload의 `judgment`/`reasons`/`worker_response`를 확인했다. a 성공 `criteria_met` / b exit 3 `nonzero_exit` / c exit 0 + 실패 marker `exit_zero_criteria_failed` / d 결과 파일 없음 `indeterminate`(`missing_or_empty_result_file`). 없는 commit: `start_failed:WorktreePreparationError`(`git cat-file` 실패), run.json `shell_state=preparation_failed`, manager에 `stage: preparation, judgment: indeterminate, requires_manager_resolution` 보고, `touch` 대상 파일은 어디에도 없음 | flow-F2.json |
| F3 | pass | worker가 OMP `write`로 `src/f3.txt`를 쓰고 Task 명령으로 local commit을 만들었다(변경 파일 = `src/f3.txt`만). 그 commit의 실험은 detached worktree에서 success. 로컬 bare remote `show-ref` 불변, branch는 `master`뿐, terminal journal에 push 0 | flow-F3.json |
| F5 | pass | C-D72 (3). Task 없이 작은 요청 → 제한 없이 `exited 0`(journal `task_id: null`), Task·manager 관여 0. 열린 Task 범위 안 → run의 `task_id`와 progress 보고의 `task_id`가 열린 Task와 같고 이후 `done`. 범위 밖 → 목록 밖 명령 `not_in_task_commands`(파일 없음), worker의 `request{paths:[src/]}`가 manager에 `classification: scope_expansion_manager_confirmation`, `dispatch_authorized: false`로 도착. Task id·revision·active 불변, 새 Task 0. 모델 선택은 scripted이고, 확인한 것은 제품의 응답이다 | flow-F5.json |
| F6 | pass | `done + requires_code_change` → Task `closed/done`. `blocked + requires_code_change` → `blocked`(held_reason `worker_blocked`, worker idle). 각각 62초 동안 worker 요청·주입 0. 새 manager follow-up → `queued` → worker에 정확히 1건 전달, Task `running` | flow-F6-F7.json |
| F7 | pass | 시작 뒤 65초: automation `idle`만 관측, manager·worker 요청 0, task/run 0, host shell `user/manual_prompt` | flow-F6-F7.json |
| F45 | pass | §4. 실제 OMP에서 dispatch되었지만 아직 전달되지 않은 Task가 있을 때의 `terminal`은 0.16초, 취소 직후의 `terminal`은 0.20초 만에 `exited 0` | flow-F45.json |
| P1 | pass | 200초 무출력 run. 첫 점검 60.7초. worker OMP turn(94.9→약 185초) 동안 status에 `delayed/worker_busy_or_unknown, pending`. turn이 끝난 직후 186.2초에 `coalesced_count: 1` 점검 1건(120·180초 몫 병합). review facts `hang_investigation.classification: unknown`(무출력만으로 hang 판정 없음). 최종 success | policy-P1-P7.json |
| P7 | **fail (결함 D-B2-3)** | review facts의 usage는 `tokens_* : "unknown"`(숫자로 확정하지 않음)이지만, status snapshot과 Workbench 상태줄에 usage 표시가 없다 | policy-P1-P7.json |
| P2 | **fail (결함 D-B2-2)** | pause 중: `to_worker` → 정확히 `{"status":"held","reason":"paused"}`, worker `terminal` → `paused`, raw log 44→77 B 증가, 사용자 입력은 두 OMP 모두 turn 실행, 85초 pause 동안 점검 0, analysis 질문 0. pause 중 run 종료 0.4초 뒤 Task `waiting_report/paused`, UI `[보고 대기 · paused]`. **재개가 두 번 모두 거부되었다**(아래 D-B2-2) | policy-P2.json |
| P3 | pass | bound run 중 manager가 streaming turn일 때 pause → `interruption confirmed`, `manager_ack abort_requested`, provider stream abort 기록. resume `resumed`, 이후 manager 요청 증가 0(중단된 turn 재실행 없음). 실행 중인 manager OMP `bash` tool의 call id가 `unknown_tool_call_ids`에 기록되고 resume `resumed` | policy-P3.json |
| P4 | pass | 실패 run(exit 1) → `run:true` 재실행 3회 모두 dispatched(`retry` 1·2·3, 두 번째에 spec 변경 → revision 2). 4번째 요청은 `held/retry_limit, runs_started 4`. run 기록의 revision `[1,1,2,2]`, 마지막 raw log에 `P4_REV2`, 그 뒤 run 증가 없음 | policy-P4.json |
| P5 | pass | run 진행 중 spec 변경 + `run:true` → `worker_busy`. 진행 중 run은 revision 1을 유지하고 raw log에 바뀐 명령이 없으며 runs_started 1, success | policy-P5.json |
| P6 | pass | `restart_worker` → `restarted`. 이전 worker pid 종료를 확인했고(exit 143) 새 pid, manager·host 동일, Task 유지. `stop_survivor` 모르는 id → `refused/unknown_survivor`. UI prefix k, k → host shell 종료 확인 후 Enter로 새 shell(generation 1→2), 새 shell 입력 정상 | policy-P6.json |
| P8 | pass | 실행 중 실험 취소 → `cancel_requested`, Task `cancelling/cancel_waiting_for_host_exit`. 명령은 죽지 않고 끝까지 실행(`P8_DONE`), 그 뒤 `closed/cancelled`(run_id 유지). run.json의 task_id·run_id 연결과 raw log가 남고 judgment 질문 0. work Task 취소 시 queue에 있던 follow-up은 전달 0, 취소 알림 1건(`worker_notified: true`) | policy-P8.json |
| CLK | pass | fixture(근거 수준 fixture): 17개 모듈 431건 OK. terminal 60초 점검·병합·pause, watchdog, automation loop, review scheduler(121번째 점검 포함), pause policy, handoff/terminal 멱등성 | policy-CLK-clock-fixture-suite.json |
| SH1 bash/dash | pass(아래 1단계만 fail) | 사용자가 `cd sub; export WB_CW16=…; PATH=$PWD/bin:$PATH`를 실행했다. worker `terminal`은 cwd=`p/sub`, 값, PATH 선두, `WORKBENCH_*` 0개를 받았다. 실험(environment `[PATH]`)은 값과 PATH를 받고 cwd는 worktree였다. 자식 안의 `cd /`·`export`는 부모에 전파되지 않고(`WB_CHILD` unset), 부모 `$$`·cwd·generation은 그대로였다 | shell-SH1-SH4-*.json |
| SH1 선언 env | **fail (결함 D-B2-1)** | 실험 `environment`에 사용자가 시작 뒤 export한 `WB_CW16`을 넣으면 `environment_missing:WB_CW16`으로 계속 보류된다 | shell-SH1-SH4-*.json |
| SH2 bash/dash | pass | worker 명령(`read`)이 foreground를 기다리는 중 prefix t → `takeover_requested`, 새 실험은 `host_terminal_busy:worker_terminal_command`(runs 0)로 보류 후 취소. 확인 전 입력은 UI가 `입력 거부 … confirm takeover`로 거절하고 foreground에 도달하지 않음. prefix c → `takeover_confirmed`, 입력 `hello`가 foreground에 전달(`SH2_GOT:hello` 1줄). 전달된 명령의 결과는 `exited 0`(실제 종료와 일치). `wb-handoff` → `control_wait`, 재전송 0, 취소된 실험 미실행. prefix t, c 뒤 다음 terminal 정상, 부모 pid 동일 | shell-SH2-*.json |
| SH3 bash/dash | pass | 미제출 입력 → `host_terminal_busy`(`mode manual_input`), python3 REPL → `manual_foreground`, background job → `the host shell has jobs`. 세 경우 모두 명령이 실행되지 않았다. job 중 실험은 `dispatched/host_terminal_busy/runs 0`이었고 사용자 job은 건드리지 않았다. job이 끝나자 실험이 시작되어 success | shell-SH3-*.json |
| SH4 bash/dash | pass | `sh -c 'sleep 5 & exit 3'`: terminal `exited 3`, duration 5.1초(후손 종료까지 대기). 실험은 exit_status 3, `exit_confirmed: true`, `nonzero_exit`. shell event `started→ended` 4.9초(주 프로그램 반환 뒤 후손 종료와 입력 반환까지 기다린 뒤 확정) | shell-SH1-SH4-*.json |
| SH5 | pass(관측 O1 포함) | trap CHLD(bash·dash): 첫 호출 `start_failed: UnsafeShellState`, 다음 호출 `host shell hook is unsupported`, held_reasons `unsupported_hook_or_trap`, 실험 보류. PROMPT_COMMAND(bash)·PS1(dash) 덮어쓰기: 명령은 실행되지 않고 보류되지만 사유가 `not at a clean prompt (mode manual_input)`로 나온다(O1). trap DEBUG(bash): 제품이 호환으로 취급하고 명령이 정확히 실행됨 | shell-SH5-*.json |

## 3. 결함 (제품, src 미수정, 실패 재현 포함)

**D-B2-1 (I-SHELL, C-AC-26/32) 실험 `environment`가 host shell의 시작 시점 환경으로만 검사된다.**
- 재현: `live_cw16_shell.py` `SH1_declared_env_exported_after_start`(bash·dash 모두 fail).
  1. host shell에서 `export WB_CW16=sh1-value`를 실행한다.
  2. manager가 `to_worker(experiment, environment:["PATH","WB_CW16"])`를 보낸다.
  3. Task가 `dispatched/environment_missing:WB_CW16/runs 0`에 머문다(UI `[전달됨 · environment_missing:WB_CW16]`).
- 같은 변수를 실험은 실제로 상속한다(SH1_experiment_inherits에서 값 확인). 그런데도 검사 때문에 실험이 시작되지 않는다.
- 보류된 Task 때문에 worker `terminal`도 `host_terminal_busy: an experiment run uses the host terminal`로 거절된다. 취소해야 풀린다.
- 위치: `src/workbench/backend/service.py:404` `environment_names=lambda: set(self._shell_env or {})`(start-up env), `flow_tasks.py:1374-1381`.

**D-B2-2 (I-POLICY, C-AC-12/명시적 재개) pause 중 run이 끝나면 재개할 수 없다.**
- 재현: `live_cw16_flow_policy.py` `P2_resume_reconciled_no_replay`.
  1. 실행 중 pause한다.
  2. pause 중 run이 끝난다(Task `waiting_report/paused`).
  3. prefix p, p를 누르면 `resume.outcome: refused, reason: PausePolicyError: explicit resume and complete file/tool/process/task/approval reconciliation are required`가 나온다. 두 번째 시도도 같다.
  4. manager `cancel`은 `held/paused`다. 사용자가 automation을 되살릴 경로가 없다(shutdown 말고는 없음).
- 대조 실험(`evidence/diag-p2.py`):
  - 결과 파일을 pause 전에 쓴 run과 pause 중에 쓴 run 모두 refused.
  - run이 아직 실행 중이면 `resumed`.
  - 따라서 원인은 파일이 아니라 run 종료 자체로 보인다.
- 경로: `automation._resume`에서 `_bound_run_current`가 True를 돌려준다(run이 아직 Task의 current이므로 F3 "run_closed_while_paused" 분기를 타지 않음). 이어서 `pause_policy.resume_observed` → `controller._validate_resume`(controller.py:1227)로 간다. `_observed_resume_evidence`의 processes/files 대조가 끝난 run에서 성립하지 않는 것으로 추정한다. 어느 match가 false인지는 내부 값이라 관측하지 못했다(unknown).

**D-B2-3 (I-POLICY, C-AC-21) usage 표시가 없다.**
- 재현: `P7_usage_in_review_facts_and_display`.
- status snapshot JSON 어디에도 usage가 없고, Workbench 상태줄 두 줄(`focus: …`, `작업: … | 60s 대조 …`)에도 없다.
- 주기 점검 facts에는 `usage: {tokens_observed/estimated/unknown: "unknown"}`가 있다. 이는 worker 모델에게만 보이고, `collect_usage`가 연결되어 있지 않아 값이 항상 unknown이다(`automation.py:332-337`).
- "Usage 미확인은 확정값으로 표시하지 않는다"는 지켜졌지만, "사용량 표시"가 사용자 화면에 없다.

## 4. CW-19 VM "45초 terminal 무응답" 판정: **fixture 결함(제품 결함 아님)**

- 호스트 재현(`evidence/fake45d.py`, 결과 `evidence/fake45-out3.json`):
  - `tests/recovery_boot/fake_omp.py`로 시작하면 Task가 없을 때 `terminal`은 0.1초 만에 결과가 온다.
  - work Task를 dispatch한 뒤 `terminal`(40초 대기)과 취소 직후 `terminal`(40초)은 둘 다 결과가 없다. VM과 같은 증상이다.
- 백엔드 journal(`handoffs.jsonl`)에서는 두 호출 모두 `terminal_request → terminal_started → exited → terminal_result`가 0.13초 안에 기록되었다. 명령도 실제로 host shell에서 실행되었다.
- 원인은 fake worker의 frame reader 스레드가 첫 `deliver` 프레임에서 죽는 것이다.
  - 근거: `/proc/<fake worker>/status` `Threads: 2 → 1`(Task dispatch 3초 뒤).
  - 백엔드는 `envelope`를 JSON **문자열**로 보낸다(`ipc/bridge_g3/mailbox.py:954` `candidate.to_json()`, `bridge.ts:824`도 string을 기대).
  - `fake_omp.py`의 deliver 분기는 `deferred` ack를 보낸 뒤 `envelope.get(...)`을 문자열에 호출하다 `AttributeError`가 난다.
  - 이후 그 fake는 tool_result 프레임을 읽지 못한다. VM 화면의 `Exception in thread Thread-1 (serve)`와 같은 현상이다.
  - VM에서 "같은 boot 재시작 뒤 exited 0"이 나온 이유는 새 fake 프로세스였기 때문이다.
- 실제 OMP 대조(F45, pass): 같은 조건에서 응답 지연은 0.16초와 0.20초였다.
  - 같은 조건: worker OMP turn 중이라 Task가 아직 전달되지 않음(확인함). 취소는 그 뒤에 함.
- 권고(Root, 쓰기 범위 밖): `fake_omp.py`의 deliver 분기에서 `envelope`가 str이면 `json.loads` 뒤에 읽는다. VM 근거의 판정 영향은 없다(해당 단계는 vm-02에서도 "판정 영향 없음"으로 분리되어 있음).

## 5. OMP 18.2.10 고정 live 테스트: 전환하지 않음(목록)

최소 변경 지점은 `tests/backend/live_harness.py` `find_omp()`와 `tests/ui/live_product_omp_independent.py` `omp_available()` 두 곳이다. 하지만 두 harness 모두 **사용자의 실제 HOME과 환경을 물려받는다.**
- `LiveBackend`는 `os.environ`을 복사하고 HOME을 그대로 두며, proxy도 막지 않는다.
- live_product_omp_independent는 "the user's existing OMP configuration"을 쓴다.
- 18.8.0에서 이 pin을 풀면 제품이 사용자 OMP 자격 증명 저장소가 있는 홈으로 OMP를 띄우게 되어 B2 규칙(자격 증명 미접촉)과 충돌한다.

그래서 pin 줄만 바꾸는 것은 "관측을 약화시키지 않는 최소 변경"이 아니라고 판단해 그대로 두었다. 대상 목록은 다음과 같다.
- **`find_omp` 경유(skip)**: `tests/backend/test_live_start.py`, `test_live_start_independent.py`, `test_live_detach.py`, `test_live_detach_independent.py`, `test_live_paste_responsive.py`, `test_live_contract_independent.py`, `test_live_input_independent.py`, `test_live_review_fixes_independent_p27c.py`(independent_support 경유).
  - 18.8.0 재관측: B1 SEL1–3·C1–C7(start·shell 선택·detach·paste·input), B2 F/P/SH.
- **`OMP_VERSION` 비교(skip)**: `tests/ui/live_product_omp_independent.py`. 18.8.0 재관측은 B1 C1–C4.
- **쓰기 범위 밖(`live_omp_probe.OMP_VERSION`)**: `tests/bridge/live_mailbox_probe.py`·`live_mailbox_independent.py`, `tests/observation/live_worker_review_probe.py`·`live_worker_review_independent.py`, `tests/workflow/live_workflow_probe.py`·`live_workflow_independent.py`, `tests/policy/pause_automation/live_pause_runtime_probe.py`.
- **다른 pin(18.6.1 isolation evidence)**: `tests/backend/live_omp_isolation_independent_p27m.py`·`_p27n.py`.
- **pin 아님(전환 불필요)**: fake OMP가 `omp/18.2.10`을 출력하는 `tests/ui/test_product_*`, `live_product_flood_independent_p27f.py`, `live_ime_independent_p27o.py`, 단위 기대값 `tests/backend/test_paths_launcher.py`·`test_omp_isolation.py`.

Root 선택지는 둘이다. (a) 영향 대조표에서 "18.8.0 재관측 = B1/B2 시나리오"로 연결한다. (b) 두 harness를 fake HOME으로 바꾸는 별도 test delta를 승인한다.

## 6. 관찰 (결함 아님, 판정 참고)

- **O1**: PROMPT_COMMAND(bash)나 PS1(dash)을 덮어쓰면 제품이 prompt 복귀를 못 보아 `manual_input`으로 판단한다. 보류는 정확하지만 사유가 hook을 가리키지 않는다. hook 검사는 `wb-handoff` 시점에만 하기 때문이다. trap CHLD는 hook 사유로 보류된다. trap DEBUG는 호환으로 처리된다(계획 SH5의 "DEBUG 보류" 기대와 다름).
- **O2**: 사용자가 `wb-handoff`로 control 대기에 두면 worker `terminal`은 `not at a clean prompt (control_wait)`로 보류된다. prefix h 뒤에는 `manager owns the host shell`로 보류된다. UI 도움말대로 prefix t, c로 재인수하면 정상이다.
- **O3**: OMP 18.8.0은 모델이 반복한 tool call id를 `_dupN`으로 바꾼다(F4 참고).
- **O4**: OMP 18.8.0은 같은 문자열을 반복하는 streaming turn을 약 30초 만에 스스로 끊는다("…final answer, not more reasoning"). P1 fixture는 반복 없는 텍스트로 바꿨다.
- **O5**: C-AC-07 "한도 후 자동 중단·근거 보고"에서 제품이 하는 일은 4번째 재실행 요청을 `held/retry_limit(runs_started 4)`로 manager에 돌려주는 것뿐이다. 별도 자동 보고는 없고, 앞선 각 run의 판정 보고는 manager에 전달되어 있다. 이를 충족으로 볼지는 Root가 대조할 항목이다.
- **O6**: SH2에서 인수 뒤 전달된 명령의 결과는 실제로 끝났으므로 `exited 0`이었다(C-D52 "실행 중·종료·미확인" 중 종료). 미확인 표시 경로는 이번에 관측하지 않았다(명령이 끝나지 않는 인수 사례는 B3 X1/X7 범위).
- 타이밍: B3와 같은 시간대에 실행했지만 B2 실패 중 타이밍 귀속 사례는 없다. 모든 실패는 원인을 위에서 특정했다.

## 7. 정식 실행(B4/R) 명령

실행 셸에서 `TMUX*`/`HERDR_*`를 지우고 돌린다(아래 `env -i`).

```
E="env -i PATH=$HOME/.local/bin:/usr/bin:/bin HOME=$HOME LANG=C.UTF-8 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
   WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=<dir> WB_CW16_RUN_ID=<run>"
# I-FLOW/I-POLICY + CLK (약 15분, 모델 0)
$E /tmp/cw02-g1-venv/bin/python -m unittest -v tests/integration/live_cw16_flow_policy.py
# I-SHELL bash+dash (약 6분, 모델 0)
$E /tmp/cw02-g1-venv/bin/python -m unittest -v tests/integration/live_cw16_shell.py
```

필터는 `WB_CW16_SCENARIOS=F1,F2,F3,F5,F7,F45,P1,P2,P3,P4,P5,P6,P8,CLK,SH1,SH2,SH3,SH5`(test 이름의 `test_` 다음 토큰), `WB_CW16_SHELLS=bash,dash`. 두 파일을 병렬로 돌려도 이번 자기 실행에서는 문제가 없었다. 결함 D-B2-1/2/3이 고쳐지기 전에는 `SH1_declared_env_exported_after_start`(bash·dash), `P2_resume_reconciled_no_replay`, `P7_usage_in_review_facts_and_display`가 fail로 남는 것이 기대값이다.
