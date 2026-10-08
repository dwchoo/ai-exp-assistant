# p27-cw16-fix-01 — CW-16 B2/B3 제품 결함 수정 (worker_senior)

작성 2026-10-08. 기준 `6a2ddb2` + B1–B3 test 변경이 있는 working tree. commit과 graphify update는 하지 않았다.
지킨 제약은 다음과 같다.
- 실제 모델·provider 요청은 0회다. live 보고서의 `model_requests_real`/`real_model_requests`가 모두 0이다.
- 자격 증명 저장소와 다른 프로세스의 environ은 읽지 않았다.
- 사용자 tmux/herdr, `~/wb-urux-sandbox`, VM에는 접근하지 않았다.
- 신호는 내가 띄운 프로세스에만 pid+start ticks/pidfd로 보냈고, 정리 뒤 잔존 프로세스 0을 확인했다.
- `tests/integration/**`와 `*_independent*`는 고치지 않았다.

## 1. 확인 표

| 항목 | red→green 테스트 | 결과 |
|---|---|---|
| Fixture `fake_omp.py` | `tests/recovery_boot/test_fake_omp_fixture.py` | HEAD fixture에서는 FAIL(10 s timeout), 수정 후 OK |
| D-B2-1 live env | `tests/backend/test_cw16_fix01.py` `LiveEnvironmentAdmissionTests`(4), `LiveShellProbeTests`(bash·dash)<br>`tests/recovery_boot/test_cw16_fix01_backend.py` `LiveShellEnvironmentTests` | HEAD src에서 FAIL/ERROR, 수정 후 OK |
| D-B2-2 pause 중 종료·취소 | `test_cw16_fix01.py` `ResumeAfterFinishTests`, `CancelWhilePausedTests` | HEAD에서 FAIL/ERROR, 수정 후 OK |
| D-B2-3 usage | `test_cw16_fix01.py` `UsageViewTests`(3)<br>`tests/ui/test_cw16_fix01_ui.py` `UsageLineTests`(3) | HEAD에서 FAIL/ERROR, 수정 후 OK |
| B3 F1 broker | `test_cw16_fix01_backend.py` `BrokerShutdownTests`(5: 스스로 종료 대기 / 남으면 정확한 신원만 종료 / 종료 불가면 verified false / stop fence 전 pin / 남의 프로세스 미접촉) | HEAD에서 FAIL/ERROR, 수정 후 OK |
| B3 F2 170열 | `test_cw16_fix01_ui.py` `PauseStoreFailureTests`(3) | HEAD에서 FAIL, 수정 후 OK |
| 전체 unit | 각 `tests/<suite>`를 `discover -s tests/<suite> -t tests/<suite>`로 실행(env -i) | 아래 표 |
| node | `node --test tests/bridge/*.test.ts` | pass 48, fail 0, exit 0 |

| suite | 결과(exit) |
|---|---|
| backend | 1107 OK (skipped 30, expected failure 1), exit 0. 마지막 수정 뒤 재실행 결과는 §4 |
| bridge | 52 OK, exit 0 |
| contracts | 52 OK, exit 0 |
| integration | 17 OK (skip 1), exit 0 |
| lifecycle | 38 OK, exit 0 |
| observation | 95 OK, exit 0 |
| policy | `discover -s tests/policy`는 **exit 5(0 tests)**. 하위 폴더에 package 파일이 없어서이며 HEAD도 같다. 하위 폴더별 실행: pause_automation 129 OK, recovery_manager 26 OK, 각각 exit 0 |
| recovery_boot | 169 OK, exit 0. 마지막 수정 뒤 재실행 결과는 §4 |
| storage | 33 OK |
| tasks | 22 OK |
| terminal | 221 OK (skip 2) |
| ui | 842 OK |
| workflow | 62 OK |
| gates | `discover -s tests/gates`는 **exit 5(0 tests)**. 구조 원인은 policy와 같다. 하위 폴더별 실행: g1_vt 85, g2_shell 142 (skip 1), g3_omp 117, g4_evidence 10, g4_lifetime 8, 모두 OK·exit 0 |

로그 위치: `/tmp/cw16fix/suites/*.log`

## 2. 항목별 수정

**Fixture.** `tests/recovery_boot/fake_omp.py:66-75`
- `envelope`가 JSON 문자열이면 `json.loads`로 읽는다. dict가 아니면 빈 dict로 다룬다. 이전에는 reader thread가 죽었다.
- F1 테스트용 pane 명령 `broker <seconds>`를 추가했다(`:103`). 자기 session을 가진 자식 프로세스를 띄운다.

**D-B2-1 (C-D55) live 환경 확인과 terminal 결합 해소.**
- `src/workbench/terminal/shell_g2/lifecycle.py:438-455`: managed shell init에 `__b_env <id> <names>` 함수를 넣었다.
  - 자식 프로세스 `printenv`로 현재 export 여부를 보고, 있는 이름만 private control channel(fd 9)로 보낸다.
  - 값은 보내지 않는다. prompt hook 문자열은 바꾸지 않았다.
- `src/workbench/backend/panes.py:938` `ShellPane.exported_names`:
  - 사용자 소유의 깨끗한 prompt일 때만 입력을 잡고 ` __b_env …`를 입력한다(앞 공백은 history 제외용, restore_cwd와 같은 규칙).
  - `ENV_DONE`과 prompt 복귀를 기다린 뒤 결과를 돌려준다.
  - 이름은 identifier만 허용한다. 그 밖의 이름은 입력하지 않고 None을 돌려준다.
  - control event 수가 그대로면 cache를 다시 쓴다. 보류 중 같은 줄이 반복 입력되지 않는다.
  - `src/workbench/backend/service.py:1704`에서 연결했다. 시작 시점 `_shell_env`는 더 이상 쓰지 않는다.
- `src/workbench/backend/flow_tasks.py:1386-1399`: 이름 확인을 admission의 마지막 단계로 옮겼다. paused·hold → worker idle → host idle 다음이다. 관측할 수 없으면 `environment_unverified`로 보류한다.
- **결합 원인**(설명 요청 사항): `_run_experiment`는 보류될 때 HostGate("experiment")를 쥔 채 `poll_interval` 동안 기다렸다. 또 `experiment_host_activity`는 `pending_start`인 실험을 무조건 "starting"으로 보고했다. 그래서 `environment_missing`으로 보류된 Task만 있어도 worker `terminal`이 `host_terminal_busy`가 되었다.
  - 수정 1: 대기를 gate 밖으로 옮겼다(`flow_tasks.py:1422-1437`).
  - 수정 2: host 사유(미점검, `host_terminal_busy`, worker terminal)로 보류된 시작만 activity로 본다(`:112`, `:547-551`).

**D-B2-2 pause 중 종료된 run의 재개와 pause 중 취소.**
- `src/workbench/backend/automation.py:943-1022`:
  - pause된 run의 host 명령이 `exited`이고 `exit_confirmed`이면, 끝난 run에는 live-run 대조(프로세스·cwd)가 성립할 수 없다.
  - 그래서 F3처럼 두 OMP의 pause만 해제(`resume reconciled`)하고 `run_finished_while_paused: exit N observed, reconciled without replay`를 기록한다.
  - `<run>/resume-reconcile.jsonl`에 `resumed_after_finish`와 `replayed: false`를 남긴다. 해당 run의 review는 끝낸다.
  - reconcile 자체는 모델 요청을 보내지 않는다. 이후 analysis·report는 일반 흐름이 진행한다.
- `src/workbench/backend/flow.py:1095-1101`: `to_worker` cancel은 paused 상태에서도 통과한다(C-D71 (3), C-D65). 다른 요청은 계속 `held/paused`다. worker에게 보내는 cancel 알림은 다른 보류 전달과 같이 queue에서 기다린다.
- 막힘 경로 테스트: pause 중 종료 → `waiting_report/paused` → cancel → `closed/cancelled`, judge 0회.

**D-B2-3 (C-AC-21) usage.**
- snapshot에 `usage`를 추가했다. 필드: `{task_id, runs_started, retries_used, retry_limit, review_count, model{tokens_observed,tokens_estimated}, model_known}`.
  - 구현: `service.py:778`, `:1222`, `automation.py:1025`, `observation/worker_review.py:319`. contract 문서는 `contracts/ui_v1.py:107`.
  - `status --json`에 그대로 나온다. 텍스트 status는 `cli.py:154`에서 `사용량: Task … run N (재시도 a/b) · 60s 점검 n · 모델 미확인`으로 보여 준다.
  - UI 상태줄에는 Task가 있을 때만 `사용량: 재시도 1/3 · 점검 2 · 모델 미확인`을 짧게 보여 준다(`ui/product/model.py:2033`).
  - 모델 token 값은 정수가 보고될 때만 숫자로 보이고, 아니면 `미확인`이다.
- **Root 확인 필요:** peer wake는 제품에 기존 집계가 없다. 그래서 새 값을 만들지 않았고 표시하지 않았다.

**B3 F1 (C-AC-22) OMP daemon broker.**
- `panes.py:249` `_pin_child`, `:266` `DetachedChildren`, `:473-515` `OmpPane.scan_detached/_scan_detached/take_detached`.
  - OMP가 살아 있고 backend의 미회수 자식으로 증명될 때만 대상을 찾는다. 대상은 OMP의 **직계 자식이면서 OMP session 밖**에 있는 프로세스다. pidfd로 pin하고, 열고 난 뒤 ppid와 start ticks를 다시 확인한다.
  - cmdline으로 매칭하지 않는다. 사용자 broker는 Workbench OMP의 자식이 아니므로 후보가 될 수 없다.
  - 2초마다 다시 scan하고, `close()` 직전에도 scan한다.
- `service.py:628-635`: 전체 종료의 맨 앞, 즉 bound run stop fence가 OMP를 끝내기 전에 모든 OMP pane을 scan한다.
  - 1차 live X4 재실행에서 broker가 종료 2초 전에 생겨 놓친 것을 이 수정으로 고쳤다.
- `service.py:653-667`: pane을 닫은 뒤 `DetachedChildren.finish()`를 실행한다.
  - 5초 동안 스스로 끝나기를 기다린다. 남으면 그 pidfd에만 TERM(2초)을 보내고, 그래도 남으면 KILL(1초)을 보낸다.
  - 그래도 살아 있으면 `verified=false`와 `omp_detached_children_alive`로 표시한다.
  - 결과의 `omp_children{observed,alive}`에 신원과 종료 방식(`by_itself`/`terminated`/`killed`)을 기록한다.
  - pane restart 때는 즉시 끝내지 않는다. 다른 OMP가 broker를 공유할 수 있기 때문이다. 대신 registry에 넘겨 전체 종료 때 계산한다(`service.py:1090`).

**B3 F2 170열 표시.**
- `ui/product/model.py:1984`, `:2137` `store_error_text`: 저장 오류를 상태 둘째 줄 앞쪽에 둔다. 순서는 부팅 대기 다음, 다른 텍스트보다 앞이다.
- 표시 문구는 `일시정지 저장 오류(재시작 시 유지 안 될 수 있음)`이고, pause가 아니면 `자동화 상태 저장 오류`다.
- 중복 표시는 없앴다(`automation_text(include_store_error=False)`).

## 3. 자기 실행 재실행 (scripted provider, OMP 18.8.0, 모델 0)

보고서 위치: `/tmp/wb-cw16-fix01-reports/`. Root가 run 디렉터리로 복사하면 된다.

| 시나리오 | 결과 |
|---|---|
| SH1 bash·dash (`fix01-shell`) | 전 step pass. `SH1_declared_env_exported_after_start`: 시작 뒤 export한 `WB_CW16`으로 run 시작(`runs_started 1`, held 없음). `SH1_parent_unchanged_no_propagation`도 pass(probe 입력이 부모 상태를 바꾸지 않음) |
| P1+P7, P2 (`fix01-policy`) | 전 step pass. P7: status JSON·UI에 `사용량: 재시도 0/3 · 점검 2 · 모델 미확인`, review facts usage는 `unknown`. P2: resume `resumed`(`run_finished_while_paused: exit 0 observed … manager=resumed, worker=resumed`), held to_worker 재전송 0, 이후 judgment success·finished |
| X6 (`fix01-fault`) | 9/9 pass. 170열 둘째 줄이 `일시정지 저장 오류(재시작 시 유지 안 될 수 있음) | metadata 저장 장애 …`로 시작 |
| X4 1차 (`fix01-fault`) | broker step fail. `observed []`: broker가 종료 직전에 생겼고 stop fence가 OMP를 먼저 끝냄 → §2 F1에서 수정 |
| X4 2차 (`fix01-fault-x4b`) | 4/4 pass. 실제 broker를 worker OMP 자식으로 pin했고 `ended: by_itself`. verified 시점 broker 0, `gone_after_seconds 0.03` |

## 4. 남은 점·Root 확인

- peer wake 사용량은 기존 집계가 없어 표시하지 않았다(D-B2-3).
- live 환경 probe는 host pane에 ` __b_env <id> NAME…` 한 줄이 보인다.
  - run 시작 때 `cd …`·`wb-handoff`를 입력하는 것과 같은 방식이다.
  - 보류 중에는 사용자가 무언가를 실행했을 때만 다시 입력된다.
- `discover -s tests/policy`와 `discover -s tests/gates`는 구조상 0 tests(exit 5)다. 하위 폴더별로 실행하는 것을 정식 명령으로 쓰기를 권한다.
- F1 마지막 수정(전체 종료 시작 시 scan) 뒤 재실행: backend 1107 OK (skipped 30, expected failure 1, exit 0), recovery_boot 170 OK (exit 0). 정리 후 테스트가 남긴 프로세스 0.
