# p27-cw16-b3-01 — CW-16 B3: fault 시나리오, VM 재부팅 driver, OMP broker (test_designer)

작성 2026-10-08. W 단계의 작성 결과와 비공식 자기 실행 결과다. 정식 근거는 Root가 동결하는 후보 C16에서 B4가 다시 만든다.
기준은 HEAD `6a2ddb2`에 B1·B2·B3 테스트 변경을 더한 working tree이며 동결 전이다.

지킨 제약:
- 제품 코드(`src/**`, `omp_bridge/**`), `docs/**`, 다른 `.workflow` 파일, harness(`cw16_harness.py`)는 수정하지 않았다.
- 실제 모델·provider 요청은 0회다. 사용한 것은 scripted local provider와 VM의 fake OMP뿐이다.
- 자격 증명 저장소와 다른 프로세스의 `environ`은 읽지 않았다.
- 사용자 tmux/herdr, `~/wb-urux-sandbox`, 사용자 OMP broker에는 접근하지 않았다.
- 신호는 내가 띄운 프로세스에만 pid+start ticks(pidfd)로 보냈다. host는 재부팅하지 않았고, 재부팅은 guest에서만 2회 했다.
- commit과 graphify update는 하지 않았다.
- B2가 같은 시각에 host runtime 실행을 하고 있었다(`wbc16shell*`). 그래도 timing flake로 분류할 실패는 없었다.

## 1. 변경 파일

| 파일 | 내용 |
|---|---|
| `tests/integration/live_cw16_fault.py` (신규) | X1–X4, X6, X7 host 시나리오. X2R(L-CW19-RESTART 재실행 wrapper)와 X5(VM wrapper)도 포함한다. `WB_LIVE_CW16=1`일 때만 실행된다. B1 harness API만 사용하고, harness 파일 자체는 고치지 않았다 |
| `tests/integration/vm_reboot_driver.py` (신규) | vm-02 driver(`/tmp/cw19-vm-02/driver.py` sha `3de6e82e…`)를 그대로 옮겼다. vm-01 `driver.source` `3154e010…`에 문서화된 post2 변경을 더한 것이다. guest 코드에서 바꾼 것은 `ROOT = $CW16_VM_ROOT`(guest 경로)뿐이다. 원본과의 diff는 ROOT 1줄과 `__main__` dispatch뿐임을 확인했다. vm-02의 same-boot 후속 script는 `post2_extra` phase로 옮겼다. 그 외 추가 기능: host orchestration(`host`: start.sh → rsync → manifest 대조 → 2회 guest reboot → log 회수 → stop.sh → 판정), log 재판정(`judge`). |
| `tests/gates/g3_omp/live_tui_extension_fault_probe.py` (probe 정리만) | `_wait_own_brokers`: `_stop_omps` 뒤에 이 probe의 temp root 안 cwd를 가진 `omp __omp_worker_daemon_broker`가 스스로 끝날 때까지 최대 20초 기다린다. 신호는 보내지 않는다. 결과는 `omp_broker_wait`에 기록한다 |
| `tests/gates/g3_omp/test_tui_extension_fault_integration.py` | `assert_clean`이 `[g3-broker-wait]`를 stderr에 기록하고 `omp_broker_wait.remaining == []`를 확인한다. B1의 version-record 변경은 그대로 두었다 |

## 2. 시나리오 표 (자기 실행 `b3-selfrun-01`, OMP 18.8.0, scripted provider, 892초)

보고서 위치는 `/tmp/wb-cw16-b3-reports/b3-selfrun-01/`이다(`fault-*.json`, `fault-matrix.json`, `vm/`, `g3-*`, `selfrun-unittest.log`). Root가 run 디렉터리로 복사하면 된다. 실행된 step은 50개이고 48개 pass, 2개 fail이다. 실패 2개는 모두 아래 제품 finding이다.

| ID | 관측(요지) | 결과 |
|---|---|---|
| X1 | 실험 run 중 UI를 3번 detach했다(각 65초 이상). (a) 자동화 active 상태: 60초 점검이 detach 중 worker에 1회 전달됐다(review_count 0→1). manager 요청 0, identity와 owner/request_id/phase 불변, raw log 증가, detach 중 출력이 재attach 뒤 보였다. (b) 인수 요청 상태(`takeover_requested`, owner=user, epoch 4, held_reasons `takeover_requested·user_owner·outstanding_request`): detach 전후 같았고 자동 전송 0. (c) pause 상태: pause 보존과 표시, 점검 0, provider 0, raw log 증가 지속. resume 뒤 run은 1회만 시작됐고(`runs_started` 1) judgment success. shutdown verified | 7/7 pass |
| X2 | 열린 실험 Task가 실행 중일 때 backend를 SIGKILL했다(정확한 pid). `start` 결과: `same_boot_crash`, run `outcome_unknown`, Task held/`backend_restarted`, 재실행 0(시작 marker 1줄), 이전 outbox 재전송 0. 새 manager 세션에 `backend_restarted`가 1회 주입됐다(journal queued→sent 1건, 15초 추가 대기 뒤에도 1). survivors: `run_target`(verified, stoppable)와 `session_member` 2개(unverified, 표시만). `stop_survivor`: unverified는 `refused`이고 프로세스가 살아 있었다. verified는 `stopped`. status 텍스트에 `backend 재시작 대조: same_boot_crash`가 나왔고 shutdown verified | 9/9 pass |
| X2R | L-CW19-RESTART(`tests/recovery_boot/live_restart_independent_p27cw19.py`)를 현재 tree에서 다시 실행했다. **56/56 checks**, endpoint 요청 0, root 잔존 없음 | pass |
| X3 | worker turn이 HTTP 500을 받았다. OMP 18.8.0은 약 16회 재시도하고 약 40초 뒤 turn error를 냈다. 그 결과 `model_hold:worker`가 걸리고 UI에 `모델 오류(worker)…`가 표시됐다. 이 동안 run과 raw log는 계속됐다. run이 끝난 뒤 analysis 질문은 hold 동안 전달 0이었고 회복 뒤 1회 전달돼 finished/success. **P2-2**: 두 번째 worker 오류 뒤 회복 turn의 첫 도구 `terminal`이 `exited 0`으로 실행됐다(marker 1줄). **P2-1**: manager hold 중 worker `to_manager(progress)`는 `queued`였고 hold 동안 전달 0, 회복 뒤 정확히 1회 전달됐다 | 8/8 pass |
| X4 | `shutdown`(--yes 없음, tty 아님): exit 1. `active work:`에 shell_request/automation/task_run이 나왔고 backend는 그대로였다. `--yes`: verified, exit 0, pane 전부 dead, 잔존 0. `setsid -w` 잔존이 있으면: exit 1, `종료 확인 실패 … (left_session_processes_alive)`, left_running 1. **broker 확인은 fail**(§3) | 3/4 (F1) |
| X5 | VM 실재부팅(§4) | 28/28 pass |
| X6 | 70 MiB 출력: 저장 정확히 64 MiB(`stored_bytes` 67108864, cap_source run), `raw log run 64 MiB 한도 도달, 저장 중지 — 실행 계속`(snapshot과 status). run은 끝까지 갔다(exit_confirmed, exit 0. judgment는 `raw_log_incomplete` 때문에 indeterminate). 재시작 뒤 Task id·summary, worktree `outcome.txt`, raw log 64 MiB, run.json이 유지됐다. 512 MiB project cap은 축소 한도 fixture(`RawLogCaps` 5/5)로 확인했다. raw-logs 0500: `raw log 저장 장애(PermissionError:13): 누락 … — 실행 계속`, run exit 0. data dir 0500과 host shell exit: `metadata_unavailable` hold와 UI 표시, 새 `to_worker`/`terminal`은 `held/metadata_unavailable`(실행 0), pause는 `persistence_error=pause_not_stored`이고 sources에 `automation_pause`가 들어갔다. 복구(0700) 뒤 hold가 풀리고 pause가 durable해졌다. **170열 UI 표시 확인은 fail**(§3 F2) | 8/9 (F2) |
| X7 | worker `terminal`(Task commands 안의 장기 명령)이 host shell에 전달된 뒤 인수 요청: owner=user, held_reasons에 `takeover_requested`. 두 번째 자동 전송은 `terminal_command_running`(실행 0). 인수 확인: `takeover_confirmed` true. backend SIGKILL 뒤 `start`: `same_boot_crash`. `workbench_status.commands_run`에서 옛 명령이 `status: unknown`(journal 재구성 note 포함), 재실행 0(marker 1줄, 두 번째 명령 미실행). 옛 shell 잔존이 있어 shutdown은 unverified(exit 1)였는데, 이는 C-AC-22상 맞는 결과다 | 5/5 pass |

G3 확인(`test_tui_extension_fault_integration.py`, OMP 18.8.0): **3/3 OK**(35초). `[g3-broker-wait]`는 seen 0/1/1, 3.0초 안에 remaining [], 신호 0.

## 3. 제품 finding (src는 고치지 않았고 실패 재현 테스트를 남겼다)

**F1 (P3) `verified` 종료가 Workbench OMP의 daemon broker가 살아 있는데도 보고된다.** 과제 (a) 결과다.

동작:
- OMP 18.8.0은 첫 모델 turn 뒤 `omp __omp_worker_daemon_broker`를 띄운다. 이 프로세스는 자기 session을 갖고, 부모가 init으로 바뀌며, cwd는 project, env는 OMP 것을 상속한다.
- broker.pid는 `<data>/omp-root/run/daemons/global/text-predict/broker.pid`에 있다. Workbench home 범위라서 사용자 `~/.omp`의 broker와 공유되지 않는다.
- 제품은 broker를 알지 못한다.
  - pane 종료는 `killpg`와 pane session 멤버만 대상으로 한다(`src/workbench/backend/panes.py:181-238, 425-452`).
  - 검증은 기록된 pid+ticks만 본다(`service.py:659-695`).
  - 다음 start의 survivor는 기록된 ref와 session 멤버만 본다(`app/recovery.py:565-594`).
  - `src/`에는 cmdline 매칭이 없다. 그래서 **사용자 broker를 죽이는 경로는 없다**(요구 충족).

관측:
- X4에서 2/2 재현됐다. `shutdown --yes` 직후 verified true, exit 0이었는데 broker(pid·ticks 동일, ppid 1)가 살아 있었다. broker는 약 1.1초 뒤 스스로 끝났다.
- OMP 내부의 `OMP_DAEMON_IDLE_GRACE_MS`는 기본 3000ms이고, presence 파일의 pid 생존을 확인한다. 제품은 `OMP_DAEMON_*`를 OMP env에서 제거한다.
- SIGKILL 재시작(X2)에서는 broker가 3초 안에 끝나 survivor 목록에 없었다. 이 시점에는 정상이다.

위험: OMP에 persist daemon이 등록되면 broker가 끝나지 않는데, Workbench는 이를 표시하지 않는다. 이 경우는 관측하지 못했다.

재현: `X4.owned_broker_not_alive_at_verified`.

수정 방향(제안): OMP pane이 끝난 뒤 broker.pid(pid+ticks, data dir 범위 확인)의 종료를 제한 시간 안에서 기다린다. 남으면 `left_running`/unverified로 표시한다.

**F2 (P3, UI) 저장하지 못한 pause의 `저장 오류`가 170열 UI에서 잘린다.** P3-2에서 고친 표시가 실제 화면에서는 사라지는 문제로, CW-19 VM F2와 같은 종류다.

- metadata 장애, raw log 장애, Task 문구가 함께 있는 상태에서 확인했다. 상태 둘째 줄이 `… [보고`에서 잘린다. 첫 줄은 `자동화: paused`만 보여 준다.
- 같은 UI 프로세스를 400열로 넓히면 `… | 일시정지됨 · 저장 오류 | backend: degraded …`가 보인다.
- 재현: `X6.pause_store_failure_visible_at_170`.

**관찰** (결함으로 주장하지 않는다):
- O1. `python -m workbench status/attach`를 포함한 모든 CLI 호출은 `ensure_private_dir`로 data dir을 0700으로 되돌린다. 그래서 0500 주입은 다음 CLI 호출에서 풀린다. 의도된 강화로 본다. X6는 장애 구간 동안 snapshot을 UI socket(`UiClient.snapshot`)으로 읽는다. L-CW19-RESTART D도 이 영향을 받지만, hold를 그 구간 안에서 관측했기 때문에 결과는 유효하다.
- O2. host shell에서 double-fork한 daemon(`setsid` without `-w`)은 shell tree를 벗어난다. 그래서 verified 종료 뒤에도 남고 목록에도 없다. CW-19 test-01 O1과 같다.
- O3. 인수 요청 중에는 60초 점검이 보내지지 않는다. 그런데 review 상태는 `waiting/next_interval_not_due`이고 `next_due_in_seconds: 0.0`이라 이유가 드러나지 않는다(X1 b).
- O4. OMP 18.8.0은 provider 500에 약 16회, 약 40초 재시도한다. 그래서 model hold는 첫 오류보다 약 40초 늦게 걸린다.

## 4. VM 실행 요약 (X5, `vm_reboot_driver.py host`, 15:47:50–15:55:27 KST)

- VM은 `wb-reboot`이고 QEMU pid 1453014는 이 실행에서 시작했다. 2회 guest reboot 동안 pid가 같았다. `stop.sh` rc 0, qemu 종료 확인, disk/base 파일 보존.
- guest: Ubuntu 24.04.5, kernel 6.8.0-142, bash 5.2.21, Python 3.12.3, guest OMP 18.2.10(probe phase만, prompt 없음).
- Manifest(`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`): host 89 = guest 89, aggregate `b12a44c6…941945`로 같다.
  - vm-02의 `a4aebd9c…`(88 files)와는 다르다. 그 뒤 `tests/recovery_boot` 파일이 추가됐기 때문이다.
  - 실행 뒤 host aggregate는 바뀌지 않았다. driver sha256은 `46182ff7…bd5ac`이다.
- boot_id 변화: `03381535…` → `eb9aaa09…` → `4f947416…`.
- 판정은 **28/28 pass**다.
  - R1–R13(C-AC-23 표 전 항목): 기록 복원, `reboot`/`ended_by_reboot`/`interrupted_by_reboot`/`queued_not_sent`, 확인 전 hold, 70초·65초 동안 알림·전달·run 0, 사용자 직접 조작, confirm-boot 1/1/0과 중복 1, 확인 뒤 허용, shutdown verified와 잔존 0, same-boot terminal `exited 0`.
  - F1·F2 fixed 재확인.
  - M(manifest), V(reboot 2회, QEMU 동일, 종료).
- 남은 관찰은 확인 뒤 열린 Task나 cancel 직후의 worker `terminal` 무응답 1건이다. CW-19에서 이미 carried된 항목이라 판정에 쓰지 않았다.
- **재사용 조건**: C16의 같은 범위 aggregate가 `b12a44c6…`와 같으면 이 근거를 다시 쓸 수 있다. 다르면 R 단계에서 아래 명령으로 다시 실행한다(약 8분, 모델 0).

## 5. 정식 실행 명령 (repo root, 실행 셸에서 TMUX*/HERDR_* 제거)

```
# host fault X1-X4, X6, X7 + X2R (약 15분, 실제 모델 0)
env -i PATH=$HOME/.local/bin:/usr/bin:/bin HOME=$HOME LANG=C.UTF-8 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  WB_LIVE_CW16=1 WB_CW16_RUN_ID=<run> WB_CW16_REPORT_DIR=<dir> \
  /tmp/cw02-g1-venv/bin/python -m unittest -v tests/integration/live_cw16_fault.py
# X5 VM (host:reboot-vm 독점, VM이 꺼져 있어야 함, 약 8분): 위 명령에 WB_CW16_VM=1을 추가하거나 단독 실행
env -i PATH=/usr/bin:/bin HOME=$HOME LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 WB_CW16_RUN_ID=<run> \
  /tmp/cw02-g1-venv/bin/python tests/integration/vm_reboot_driver.py host <dir>/vm
# G3 고정 probe의 버전 기록과 broker 대기 (fake HOME, proxy 차단)
env -i PATH=$HOME/.local/bin:/usr/bin:/bin HOME=<fake home> LANG=C.UTF-8 TERM=xterm-256color \
  PYTHONPATH=src:tests/gates/g3_omp HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 \
  NO_PROXY=127.0.0.1 WB_G3_OMP_VERSION_RECORD=<file> /tmp/cw02-g1-venv/bin/python -m unittest -v \
  tests/gates/g3_omp/test_tui_extension_fault_integration.py
```

시나리오 일부만 돌리려면 `WB_CW16_FAULTS=X1,X2,X2R,X3,X4,X6,X7`을 쓴다. `WB_LIVE_CW16`이 없으면 8건 모두 skip되고, pass로 세지 않는다.

F1·F2가 남아 있으면 I-FAULT는 X4와 X6의 해당 step에서 fail한다. Root는 제품 수정이나 판정 중 하나를 정해야 한다. 고친 뒤에는 X4·X6만 다시 실행하면 된다(약 3분).
