# p27-cw16-fix-03: CW-16 실모델 smoke O3·O4와 review P3 quick win (C17 전 마지막 제품 변경)

- 역할: worker_senior (Opus). 기준 HEAD `e635723`. 모델·provider 요청 0(fake OMP, 빈 agent.db, proxy 차단). commit·graphify update 없음. docs/**, tests/integration/**, `*_independent*`는 수정하지 않았습니다.
- 작업 중 확인: `tests/backend/live_harness.py`, `tests/backend/test_live_*`, `tests/terminal/test_manual_input_backend_independent.py`, docs 2개에는 **다른 작업자가 동시에 만든 변경**이 있습니다(OMP 18.2.10 pin 해제로 보임). 제 변경이 아니며 손대지 않았습니다.

## 1. O4 — 활성 turn 중 `shutdown --yes [--json]`의 10 s timeout과 traceback

**원인(재현함):**
1. backend는 `SHUTDOWN_CONFIRM` 응답 frame을 queue에 넣기만 합니다. 같은 tick에 `_loop`가 끝나고 `_close()`가 시작됩니다. 그래서 응답은 `_close()`가 끝날 때 `ui.close()`에서야 전송됩니다. CLI는 확인 응답을 `UiClient(timeout=10)`로 기다리므로, close가 10 s를 넘으면 `TimeoutError: no frame from backend`(client.py:119/132) traceback으로 끝납니다.
2. close가 길어지는 이유: OMP pane을 하나씩 닫으면서 pane마다 SIGTERM grace 3 s와 KILL 대기 최대 2 s를 씁니다. turn 중인 OMP가 abort하느라 grace 안에 끝나지 않으면 두 OMP가 순차로 각 3 s 이상을 씁니다. 여기에 run fence·thread join 등이 더해집니다.
   - SIGTERM 뒤 8 s 지나서 끝나는 fake OMP와 worker `sleep 100` 조건에서 HEAD 기준 close는 6.3 s였고, 확인 응답도 그 뒤에야 도착했습니다.
   - 실제 OMP의 turn abort 소요 시간은 실모델 요청이 필요해서 측정하지 않았습니다(추정).

**수정:**
- `service.py`: `_close()` 맨 앞에서 `ui.flush(1.0)`를 호출해, queue에 있던 confirm 응답을 close 전에 보냅니다. `UiServer.flush`는 write만 하고 새 요청은 읽지 않습니다.
- `service.py`·`panes.py`: 두 OMP에 SIGTERM을 함께 보냅니다(직전에 detached child scan, B3 F1 유지). grace 3 s는 두 OMP가 공유합니다. `OmpPane.close(grace, *, signalled=)`는 SIGTERM을 다시 보내지 않습니다. KILL·survivor·detached 처리는 기존과 같습니다.
- confirm 응답에 `result_deadline`(=`SHUTDOWN_RESULT_DEADLINE` 150 s)을 넣었습니다. 이 값은 `_close`의 bounded wait 합계를 올림한 상한입니다(근거는 주석). 정상 close는 수 초입니다.
- `cli.py cmd_shutdown`:
  - 결과는 backend가 알려 준 상한까지 기다립니다(구 backend면 150 s).
  - human form은 stderr에 "종료 중: … (최대 N초)"를 먼저 쓰고, 이후 5 s마다 경과 시간을 씁니다. `--json`일 때는 progress를 쓰지 않습니다.
  - 예외는 모두 처리해 traceback이 나지 않습니다.
    - 요청 실패: "nothing was stopped", exit 1.
    - confirm timeout: 결과를 계속 기다립니다.
    - 연결 끊김·상한 초과: 결과 없음으로 처리합니다.
  - 결과가 없으면 shutdown_request의 `processes.backend`(pid+start_ticks)로 backend의 정확한 신원을 최대 10 s 관찰합니다. 그 뒤 "종료 확인 실패: 종료 결과를 받지 못해 … 확인할 수 없습니다 (backend process는 종료됨 / 아직 실행 중 / 상태 불명; 원인)"을 출력하고 exit 1로 끝냅니다(C-AC-22).
  - `--json`은 `{"shutdown": null, "unconfirmed": {"cause", "backend"}}`를 출력합니다. 결과가 있을 때의 출력 형식(`shutdown result: …`, `{"shutdown": …}`, "종료 확인 실패")은 그대로입니다.

## 2. O3 — 재시작 상태줄이 backend 수명 동안 남는 문제
- `ui/product/model.py`: 재시작 줄("backend 비정상 종료 뒤 재시작 · 결과 불명 · 보내지 못한 메시지 N건")은 다음 조건으로만 표시합니다.
  - 열린 Task가 있거나 이전 backend process가 살아 있으면 `STARTUP_SHOWN_SECONDS` = **120 s**(기존 600 s)까지 표시합니다.
  - Task가 닫혔거나 열린 Task가 없고 survivors도 없으면 `STARTUP_SETTLED_SECONDS` = 15 s까지만 표시합니다. 그래서 Task가 닫히면 바로 사라집니다.
- 살아 있는 survivors 줄("이전 backend의 process N개 남음")은 C-D71 (1)대로 계속 표시합니다. `status --json`의 `startup` 기록은 그대로입니다.
- "acknowledged" survivors 상태는 제품에 없습니다(manager가 "그대로 둠"을 기록하지 않음). 그래서 survivors가 남아 있으면 줄 자체는 bounded time(120 s)으로 사라지고, survivors 문구만 남깁니다.

## 3. Review P3 quick win
- **(a) held:retry_limit:** `flow_tasks.py` 결과에 `detail`(`RETRY_LIMIT_DETAIL`)을 추가했습니다. "다시 실행하지 말 것"과 사용자에게 보고할 항목 (1) 원인 (2) 근거(각 run 결과·log/result 경로) (3) 시도(runs, 사이 변경) (4) 남은 문제, 그리고 사용자 결정 대기를 영어로 담았습니다. to-worker skill은 수정하지 않았습니다.
- **(b) SH5 보류 사유:** `panes.py automation_busy`에서 다음 조건이 모두 맞으면 `PROMPT_HOOK_SUSPECT`를 반환합니다.
  - 조건: mode `manual_input`, 입력한 줄이 있고, 입력 중인 줄은 없고, shell 자신이 PTY foreground이고, session member(실행 중 프로그램)가 없음.
  - 반환 문구는 "user PROMPT_COMMAND (bash) or PS1 (sh) change may have replaced Workbench's prompt hook, or a shell builtin or multi-line command is still running or waiting for input"이며 "(mode manual_input)"을 포함합니다.
  - **단정하지 않는 이유:** builtin `read`, 반복문, PS2 continuation도 밖에서 보면 같은 상태입니다. shell 변수는 /proc로 읽을 수 없고, shell에 무언가를 입력해 확인하는 방법은 금지되어 있습니다. 그래서 가능성을 함께 적었습니다.
  - trap DEBUG는 B2 관측대로 호환입니다(보류 없이 정확 실행). 그래서 문구에 넣지 않았습니다.
  - integration `live_cw16_shell` SH5의 `hook_named_in_reason`은 이제 true가 됩니다(assert가 아니라 note라 깨지지 않음). SH3 unsubmitted의 `"manual_input" in reason` 단정은 유지됩니다(입력 중인 줄이 있으면 기존 문구).

## 4. 테스트 (red→green)
| 테스트 | HEAD src(red) | 수정 후 |
|---|---|---|
| 신규 `tests/recovery_boot/test_live_shutdown_slow_turn.py` 3건 (fake OMP `slow-term 8`, worker `sleep 100`, 실제 `python -m workbench`) | confirm 2.5 s 안 응답 → `TimeoutError`; human form progress 없음 → 2 fail (`--json`은 6 s라 통과) | 3 OK: confirm 즉시 응답, 두 OMP SIGTERM 간격 < 1 s, verified true, traceback 없음 |
| 신규 `tests/recovery_boot/test_cli_shutdown_wait_cw16.py` 7건 (fake client) | 8 fail/error | OK |
| `tests/backend/test_panes.py` SH5 1건(bash·dash 실제 shell) | fail 2 | OK |
| `tests/backend/test_task_flow.py` retry_limit detail | error | OK |
| `tests/ui/test_product_model_cw19.py` O3 1건 | fail(상수 없음) | OK. 기존·independent 모델 테스트 OK |
- `tests/recovery_boot/fake_omp.py`에 `slow-term <s>`를 추가했습니다(SIGTERM 시각을 `$FAKE_TERMS`에 기록하고 s초 뒤 종료).

## 5. 전체 suite (fake HOME, 빈 agent.db, proxy 차단, `env -i`, 병렬 실행; 모델 요청 0)
| suite | 결과 |
|---|---|
| backend | 1115건: fail 1, error 1, skipped 3, expected failure 1. 두 건 모두 다른 작업자가 동시에 바꾼 live 테스트(pin 해제로 실제 OMP 18.8.0이 실행됨)입니다. 아래 참고 |
| ui | 843 OK (병렬 1차는 `test_product_pty` 2.18 s > 2.0 s 타이밍 1건 fail, 순차 재실행 OK) |
| recovery_boot | 180 OK (병렬 1차는 `test_survivors_holds_independent_p27cw19` 타이밍 1건 fail, 단독 3회·순차 재실행 OK) |
| terminal | 221 OK |
| integration (unit) | 24 OK (skipped 2) |
| gates g1 / g2 / g3 / g4_evidence / g4_lifetime | 85 / 142 (skip 1) / 117 / 10 / 8 OK |
| policy pause_automation / recovery_manager | 129 / 26 OK |
| workflow / bridge / contracts / lifecycle / observation / storage / tasks / ui/status_workbench | 62 / 52 (skip 1) / 52 / 38 / 95 / 33 / 22 / 2 OK |
| node `tests/bridge/*.test.ts` | 48 pass |
| `tests/gates/harness/run-contracts.sh` | OK (15 pass) |

- g3·workflow의 병렬 1차 error는 제 실행 env에 `NO_PROXY`가 없어 `HTTP_PROXY`가 127.0.0.1 로컬 scripted provider 요청까지 막은 탓이었습니다. `NO_PROXY=127.0.0.1,localhost`를 넣고 순차 재실행해 OK가 되었습니다(g2 포함).
- backend 2건은 제 변경과 무관합니다.
  - `test_live_start_independent…private_modes`: 실제 OMP 18.8.0이 data dir의 `omp-root/bun-transpiler-cache` 등을 0755로 만들어 group/other에 읽기가 열립니다. 이 테스트는 HEAD에서는 18.2.10 pin 때문에 skip이었고, 동시 진행 중인 pin 해제 변경 뒤에 처음 실행된 것입니다. **실제 OMP 18.8.0 + 제품 data dir 권한 문제일 수 있으니 Root 확인이 필요합니다.**
  - `test_live_contract_independent…connection_cap`: 병렬 부하 중 `connect` EAGAIN이었고, 단독 재실행은 OK입니다.
- 로그: `/tmp/cw16fix03/suites/*.log`, 순차 재실행 `/tmp/cw16fix03/suites2/*.log`.

## 6. integration(동결) 영향
- 문구를 단정하는 integration 테스트는 깨지지 않습니다.
  - `live_cw16_fault.py:903-985`의 "refusing…", "active work:", "shutdown result: ", "종료 확인 실패"는 유지됩니다.
  - `cw16_harness.shutdown_result`, `vm_reboot_driver`의 `["shutdown"]` 파싱도 유지됩니다.
  - SH3 `manual_input`도 유지됩니다.
- 이번에 live integration을 실행하지는 않았습니다(unit `tests/integration` discover만 OK). 실모델 O4 재현은 요청이 필요해 하지 않았습니다.
