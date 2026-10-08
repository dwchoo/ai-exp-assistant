# p27-cw16-fix-review-01 — CW-16 제품 수정(fix-01) 검토 (reviewer, read-only)

작성 2026-10-08. 기준은 `6a2ddb2`다. 대상은 working tree diff(`src`, `tests`(integration 제외), 신규 test 4개)다.
- 실제 모델·provider 요청은 0회다. 자격 증명 저장소와 다른 프로세스의 environ은 읽지 않았다.
- 사용자 tmux/herdr·VM·`~/wb-urux-sandbox`에는 접근하지 않았다. commit과 graphify update는 하지 않았다.
- 임시 재현은 `/tmp/cw16rev/`(HOME도 이곳)에서만 했다. 내가 띄운 shell은 모두 `pane.close()`로 정리했다.

## 판정: **block**

새로 추가된 D-B2-2 경로에 P2 두 건이 있다. 둘 다 pause 의미(C-D71)와 receipt 진실성에 걸린다.
D-B2-1, D-B2-3, F1, F2, fixture는 요구한 동작을 충족한다. 다만 P3 항목이 있다.

## 결함

### P2-1 pause 중 cancel의 worker 알림이 버려지는데 receipt는 `worker_notified: true`다
- 위치:
  - `src/workbench/backend/flow.py:1095-1099`: 새 우회로, cancel은 paused에서도 통과한다.
  - `src/workbench/backend/flow_tasks.py:1017-1020`: `_finish_cancel`이 `keep_across_pause` 없이 enqueue하고, `status == "queued"`이면 `notified=True`로 둔다.
  - `src/workbench/backend/flow.py:1277-1282`: lane은 pause 중 keep 표시가 없는 항목을 `held_paused`로 끝낸다. resume 뒤에도 보내지 않는다.
- 재현(임시 test, `FlowFixture`, 실제 `HandoffService` 사용):
  1. work Task를 dispatch하고 전달한다.
  2. pause한다.
  3. `to_worker{cancel:true}`를 보낸다. 응답은 `{"status":"cancelled","worker_notified":true}`다.
  4. handoffs journal에서 cancel 알림은 `pending → held_paused(reason paused)`다.
  5. resume한 뒤에도 전달은 0건이다.
- 영향:
  - manager와 사용자는 worker가 취소를 통지받았다고 보지만, worker는 Task 취소를 모른다(진실하지 않은 receipt).
  - fix 보고서 §2의 "worker에게 보내는 cancel 알림은 … queue에서 기다린다"는 실제 동작과 다르다.
- 같은 결함이 기존 CW-19 hold 중 cancel 경로(`_hold_reason`)에도 있다.
- 권고 수정: cancel 알림을 `keep_across_pause=True`로 enqueue한다(아직 submit되지 않았으므로 replay가 아니다, R3와 같은 근거). 그렇게 하지 않으려면 pause·hold 중에는 `worker_notified`를 `held`/false로 정직하게 돌려준다. pause 중 cancel 테스트에 알림 상태 확인을 추가한다.

### P2-2 "pause 중 종료된 run" 재개가 아직 current인 run을 unbind해서, 뒤이은 analysis·report 단계에서 pause가 OMP에 닿지 않는다
- 위치:
  - `src/workbench/backend/automation.py:963-976`: 새 분기가 `bound.ended = True`로 둔다.
  - `:995-1003`: resume 처리에서 `self._bound = None`, `admission.set_active_run(None)`이 된다.
- 정상 흐름에서는 bound가 report가 수락될 때(`run_ended`, `flow_tasks.py` `_report_hook`)까지 유지된다. 이 분기 뒤에는 run이 Task의 current로 남은 채(`waiting_report`) worker analysis(모델 turn)와 manager report가 진행되는데 bound가 없다.
- 재현(임시 test, `AutomationFixture`, ResumeAfterFinishTests와 같은 순서):
  1. resume이 `run_finished_while_paused`로 처리된다.
  2. `controller._bound is None`이다. 그러나 `get_current_run`은 여전히 이 run이다.
  3. 다시 `request_pause`를 하면 `paused: True`이지만 manager·worker에 보낸 `pause` frame은 **0/0**이다(`_pause(None)`이 "no bound run"으로 일찍 반환).
- 영향:
  - 재개 직후 진행 중인 worker analysis turn을 사용자가 다시 pause해도 중단 요청이 가지 않는다(C-D71 pause, BRIEF C-AC-12).
  - 이 run의 shutdown stop fence(`shutdown_bound`)도 걸리지 않는다(C-AC-22).
- 권고 수정: host 부분이 끝났다는 표시(review 중지, 대조 생략)만 하고 bound는 `run_ended`까지 유지한다. 또는 `run_ended`의 `keep` 규칙과 같이, 다음 pause가 이 run의 coordinator를 다시 쓰도록 한다. 재개 뒤 재-pause가 OMP에 pause를 보내는지 확인하는 테스트를 추가한다.

### P3-1 env probe가 사용자 shell 상태를 바꾸고, 보류 중에는 사용자 명령마다 반복된다
- 위치:
  - `src/workbench/terminal/shell_g2/lifecycle.py:442-451`(`__b_env`)
  - `src/workbench/backend/panes.py:938-990`(`exported_names`, `:971` 입력)
- 실제 bash pane에서 재현했다(HOME=/tmp).
  - `false` 다음에 probe가 돌면, 이어서 친 `echo RC=$?`가 `RC=0`이다. 사용자의 `$?`가 덮어써진다.
  - `history`에 ` __b_env <token> PATH`가 남는다. managed bash는 `--rcfile <init>`로 뜨고 HISTCONTROL을 설정하지 않으므로, 앞 공백 규칙은 효과가 없다. exit할 때 HISTFILE에도 기록될 수 있다.
  - probe 동안(약 30 ms) 들어온 사용자 키 입력은 `HOST_SHELL_AUTOMATION/environment_check`로 거절된다. 3회 시도 모두 재현되었다.
  - `environment_missing`으로 보류된 동안에는 사용자 명령이 하나 끝날 때마다(control event 증가) 0.5초 안에 probe 줄이 다시 입력된다.
- 안전 요건 가운데 idle prompt에서만 입력하는 것, REPL이나 미제출 입력에 섞이지 않는 것, 값을 출력하지 않는 것, 이름 검증은 충족한다.
- 권고 수정:
  - `__b_env`에서 `$?`를 보존한다(`__b_rc=$?; …; return $__b_rc`).
  - bash에서는 history에서 제외한다(init에 `HISTCONTROL=ignorespace…` 추가 또는 `history -d`).
  - 보류 중 재-probe 간격에 backoff를 둔다.

### P3-2 model usage 출처가 잘못된 class에 붙어 있어 "known" 경로가 죽어 있다
- 위치: `src/workbench/observation/worker_review.py:319-321`. `usage_snapshot`이 `_usage`가 없는 `SerializedReviewAdmission`에 추가되었다.
- `src/workbench/backend/automation.py:1028`은 `self.review`(`WorkerReviewScheduler`, 이 method가 없음)를 호출한다. 확인 결과 `hasattr(WorkerReviewScheduler,'usage_snapshot') == False`다.
- AttributeError가 `except Exception`에 막혀 항상 `unknown`이 된다.
- 지금은 `collect_usage`가 연결되지 않았으므로 표시는 진실하다("미확인"). 하지만 "known일 때 표시" 경로는 테스트가 fake loop로만 덮고 있어 실제로는 성립하지 않는다.
- 권고 수정: method를 `WorkerReviewScheduler`로 옮기고 실제 객체로 테스트한다.

### P3-3 (관찰) F1 대상 범위
- `_scan_detached`(`panes.py:479-509`)는 OMP의 **직계 자식이면서 OMP session 밖**에 있는 프로세스를 모두 대상으로 한다. broker만 대상으로 하지 않는다.
  - 예를 들어 OMP가 tool 명령을 `detached`로 띄우는 경우, 그 명령도 전체 종료 때 5초 대기 뒤 TERM/KILL 대상이 된다.
  - cmdline 매칭을 금지했으므로 이 일반화는 불가피하고 C-AC-22와도 맞는다. Root가 범위를 알고 있도록 기록한다.
- OMP가 스스로 끝나는 경우(pane exit) 최대 2초 scan 간격 안에 생긴 자식은 pin되지 않는다. broker는 첫 모델 turn 뒤에 생기므로(B3) 실제 위험은 낮다.

## 항목별 확인 (통과)

- **D-B2-1**
  - 시작 시점 `_shell_env`를 더 이상 쓰지 않는다(`service.py:408`, `:1704`).
  - 이름은 identifier regex로 검증한 뒤에만 입력한다. 값은 private fd 9로도 보내지 않는다. printenv는 절대경로다.
  - probe는 `automation_busy()`가 None일 때만, io_lock 아래에서 hold를 걸고 입력한다. 끝나면 finally에서 hold를 해제한다.
  - 완료 조건은 `ENV_DONE`과 `READY`(manual_prompt) 둘 다다. cache는 event 수가 같을 때만 쓴다.
  - gate 밖 대기(`flow_tasks.py:1424-1438`)와 `_HOST_START_REASONS`(`:112`, `:549-551`)로 결합을 풀었다. 이 수정은 맞다.
  - 실제 bash에서 `export` 뒤 probe 결과가 바뀌는 것, local 변수는 제외되는 것을 직접 확인했다.
- **D-B2-2**: exit_confirmed인 finished run만 이 분기를 탄다. reconcile 자체는 모델 요청을 보내지 않는다(`resume` frame만 보냄). `resume-reconcile.jsonl`에 `replayed:false`가 기록된다. cancel은 paused에서 통과한다. 단, 위 P2-1과 P2-2가 있다.
- **D-B2-3**: `status --json`과 텍스트(`cli.py:153-162`), UI 상태줄(`model.py:2033-2060`)에 usage가 나온다. 정수가 아니면 `미확인`이고, 고정값은 쓰지 않는다. peer wake는 기존 집계가 없다(grep 0건).
- **F1**
  - pidfd pin 뒤 stat(ppid·start ticks)을 다시 읽는다. leader가 증명된 상태(미회수 자식)에서만 scan한다.
  - cmdline 매칭은 없다. 대기 시간에 상한이 있다(5+2+1 s). 신호는 pidfd로만 보낸다.
  - 살아 있는 자식이 있으면 `verified=false`와 `omp_detached_children_alive`로 표시한다.
  - 전체 종료와 restart는 모두 main loop thread에서 실행된다(`_tick` → `_run_restart_jobs`, `run` → `_close`). 따라서 `_detached` dict 경쟁은 없다.
  - scan 비용은 이 머신(약 650 프로세스)에서 1회 8 ms, OMP마다 2초 간격이다.
- **F2**: 저장 오류가 boot wait 다음, 다른 텍스트보다 앞에 오고 중복 표시가 없다(테스트 3건).
- **Fixture**: string envelope를 `json.loads`로 읽고, dict가 아니면 `{}`로 다룬다.

## 실행한 검증 (env -i, HOME=/tmp/cw16rev/home)

| 대상 | 결과 |
|---|---|
| 신규 test: backend `test_cw16_fix01.py` 12, recovery_boot `test_cw16_fix01_backend.py` 6 / `test_fake_omp_fixture.py` 1, ui `test_cw16_fix01_ui.py` 6 | 모두 OK, exit 0 |
| observation | 95 OK, exit 0 |
| policy/pause_automation | 129 OK, exit 0 |
| backend 전체 | 1107 OK (skipped 30, expected failure 1), exit 0 |
| recovery_boot 전체 | 170 OK, exit 0 |
| ui 전체 | 842 OK, exit 0 |
| terminal 전체 | 221 OK (skipped 2), exit 0 |
| 재현(임시, `/tmp/cw16rev/t`) | P2-1: `worker_notified: True`이고 알림은 `held_paused`. P2-2: 재-pause frame 0/0. P3-1: `RC=0`, history 오염, 키 입력 거절 3/3 |
