# p27-cw16-fix-02 — CW-16 fix review-01 지적 수정 (worker_senior)

작성 2026-10-08. 기준 `6a2ddb2` + fix-01 working tree. commit과 graphify update는 하지 않았다.
- 실제 모델·provider 요청은 0회다(live 보고서 `provider.real_model_requests` 모두 0).
- 자격 증명 저장소와 다른 프로세스의 environ은 읽지 않았다. 사용자 tmux/herdr, `~/wb-urux-sandbox`(실행 중인 사용자 backend 포함), VM에는 접근하지 않았다.
- `tests/integration/**`와 `*_independent*`는 고치지 않았다. 임시 파일은 `/tmp/cw16fix02/`와 `/tmp/wb-cw16-fix02-reports/`에만 두었다.
- 정리 뒤 테스트가 남긴 프로세스는 0이다.

## 1. 항목별 수정

### P2-1 pause 중 cancel의 worker 알림 (Root 판정 그대로)
- `flow_tasks.py` `_finish_cancel`: cancel 알림을 `keep_across_pause=True`로 enqueue한다. 아직 submit되지 않은 알림이므로 resume 뒤 전달은 replay가 아니다(R3).
  - 같은 표시가 CW-19 hold 경로에도 적용된다. lane은 hold가 풀릴 때까지 알림을 보류했다가 전달한다.
- 반환값은 이제 알림 상태다: `queued` / `after_resume` / `after_hold:<reason>` / `not_needed` / `not_queued:<status>`.
- `_cancel` 결과는 실제 상태를 말한다.
  - pause 중이면 `worker_notified: false`, `worker_notice: "after_resume"`, `detail`("…the worker is told of the cancel after the resume…")을 준다.
  - hold 중이면 `after_hold:<reason>`이다. 평소처럼 바로 전달되는 경우는 기존과 같이 `worker_notified: true`다.
- `flow.py`의 우회로 주석을 실제 동작에 맞게 고쳤다.
- 테스트(`tests/backend/test_cw16_fix02.py` `CancelNoticeWhilePausedTests`):
  - pause → cancel → 0.3 s 동안 전달 0 → resume → 전달은 정확히 1건, 생성도 1건.
  - pause하지 않은 cancel은 바로 알린다.

### P2-2 pause 중 끝난 run의 재개 뒤 binding 유지
- 원인: pause coordinator(`PauseCoordinator`)는 resume을 거치지 않으면 `_pause_override=True`로 남는다. 그래서 bound를 유지해도 다음 `pause()`가 `already_paused`로 frame을 보내지 않는다.
- `policy/pause_automation/controller.py`에 `resume_finished(run)`을 추가했다.
  - 기존 `_resume` transition을 그대로 쓴다: 두 OMP에 resume(pair·authority 확인), journal `resumed`(status `finished_while_paused`), admission 공개, fence 재개방.
  - 검증만 `_validate_finished_resume`으로 바꾼다. 같은 current·미취소 Task/revision/run/approval인지, 두 OMP가 paused·idle·in-flight 0인지를 본다. 프로세스·cwd·파일 대조는 끝난 run에 성립할 수 없으므로 하지 않는다.
  - `_validate_resume`의 OMP 상태 검사 부분은 `_peers_reconciled`로 떼어 냈다. 동작은 바뀌지 않는다.
- `backend/automation.py` `_resume`의 finished 분기:
  - raw `_resume_peers_after_close` 대신 `pause_policy.resume_finished(bound.view)`를 쓴다.
  - `bound.ended`를 두지 않는다. 대신 새 플래그 `host_finished=True`를 둔다. 따라서 bound·admission active run·lifecycle coordinator는 `run_ended`(report 수락·종료)까지 유지된다(일반 흐름과 같다).
  - `host_finished`인 run에는 60초 review를 하지 않는다(`_review_run`, `_dispatch_review`). `resume-reconcile.jsonl`(`replayed:false`) 기록은 유지된다.
- 테스트(`ResumeAfterFinishBindingTests`, 실제 G3 bridge·ScriptedPeer·bash pane):
  - pause 중 종료 → resume(`run_finished_while_paused`) → bound 유지.
  - 재-pause frame이 manager·worker에 각각 +1이고 두 OMP 모두 paused다.
  - 두 번째 resume도 reconciled되고, review는 0이다.
  - `shutdown_bound()`가 이 run의 결과를 돌려준다(stop fence 유지).
  - `run_ended` 뒤에야 unbind된다.
- fix-01 테스트 `ResumeAfterFinishTests`의 `tick()=="idle"` 단정은 binding 유지 판정과 충돌하므로 "admitted 아님 + review 0"으로 바꿨다. bound된 끝난 run의 tick은 `held/run_ended`이며, 일반 흐름에서 끝난 run과 같다.

### P3-1 env probe: 입력 없는 방식(Root 우선안)
- typed probe(`__b_env`)를 없앴다. 그 대신 managed shell의 기존 prompt hook이 매 prompt마다 export된 **이름만** private 파일에 쓴다.
  - 파일 위치는 init 디렉터리(0700)의 `env-names`이고, `adapter.py`의 `BOUNDARY_ENV_NAMES`가 가리킨다. hook은 READY를 내보내기 전에 파일을 쓴다(`lifecycle.py` `__b_emit`).
  - bash는 builtin `compgen -e`를 쓴다(fork 없음). dash는 자식 `env -0 | cut -z -d= -f1`을 쓴다(절대경로이고, 값은 이 pipe 안에서만 지나간다).
  - 파일 끝에 `\001END` 표식을 붙인다. `>|`를 써서 사용자의 `set -C`에서도 쓸 수 있다.
  - `PROMPT_COMMAND`/`PS1` 문자열은 바꾸지 않았다. 따라서 hook 호환 검사(`__b_hook_compatible`)는 그대로다.
- `panes.py` `ShellPane.exported_names`:
  - 사용자 소유 idle prompt(`manual_prompt`이고 마지막 control event가 `READY`)일 때만 파일을 읽는다(O_NOFOLLOW, 본인 uid, 1 MiB 상한, 표식 확인).
  - 부분 기록이면 0.5 s 안에서 다시 읽는다. 그 밖의 경우는 None(`environment_unverified`)이다.
  - 입력 hold, 타이핑, 재-probe, cache가 모두 없어졌다.
- 실측(bash·dash, `/tmp` HOME):
  - 사용자 `false` 다음 `echo RC=$?`는 `RC=1`이다.
  - backend가 입력한 바이트는 0이다(send_user에 사용자 입력만 지나감). pane 출력과 `history`에 `__b_`/`WBENV1`이 없다.
  - 반복 질의해도 control event가 늘지 않는다. `automation_hold`는 None이다.
  - 나중에 export한 이름은 보이고, local 변수와 unset된 이름은 빠진다. 파일에 값이 들어가지 않는다(`secret-value` 없음).
  - `set -C` 아래에서도 동작한다. 명령 실행 중에는 None이고, prompt로 돌아오면 다시 관측된다.
- 남는 한계(기록):
  - 사용자가 prompt hook 자체를 바꾸면 이름 파일이 마지막 hook 시점에서 멈춘다.
  - 이 경우 run 시작의 `wb-handoff`가 hook 검사로 거절하므로 실험은 시작되지 않는다.

### P3-2 usage 출처
- `usage_snapshot`을 `SerializedReviewAdmission`에서 `WorkerReviewScheduler`로 옮겼다(`observation/worker_review.py:807`).
- 테스트 `UsageSourceTests`는 실제 `AutomationController.review`를 쓴다. 처음에는 `unknown`이고, `UsageValue(1234,"observed")`를 기록한 뒤에는 `tokens_observed 1234`가 된다.
- 보고되지 않은 값은 계속 `미확인`이다.

## 2. 검증 (env -i, HOME=/tmp/…, venv `/tmp/cw02-g1-venv` python)

| 대상 | 결과 |
|---|---|
| 신규 `tests/backend/test_cw16_fix02.py` 7개 + `test_cw16_fix01.py` 12개 | OK |
| backend | 1114 OK (skipped 30, expected failure 1), exit 0 |
| recovery_boot | 170 OK, exit 0 |
| ui | 842 OK, exit 0 |
| terminal | 221 OK (skipped 2), exit 0 |
| gates/g2_shell | 142 OK (skipped 1), exit 0 |
| policy/pause_automation | 129 OK, exit 0 |
| policy/recovery_manager | 26 OK, exit 0 |
| observation | 95 OK, exit 0 |
| workflow | 62 OK, exit 0 |
| live SH1·SH4 bash, dash (`fix02-shell`) | 전 step pass (`SH1_declared_env_exported_after_start`, `SH1_parent_unchanged_no_propagation` 포함) |
| live P2, P3, P8, F45 (`fix02-policy`) | 전 step pass. P2 resume은 `run_finished_while_paused: exit 0 observed, reconciled without replay (manager=resumed, worker=resumed)` |

- 첫 병렬 실행의 일부 import 오류는 시스템 python에 pyte/wcwidth가 없어서 생긴 것이다. venv로 다시 실행해 위 표가 되었다.
- 첫 병렬 실행에서 g2_shell `test_common_run_protocol_independent` 1건이 실패했다.
  - cwd가 `tests/gates/g2_shell`이면 HEAD lifecycle에서도 같은 실패가 재현된다.
  - repo root·discover에서는 통과하고, 재실행한 전체 g2_shell도 OK다. 내 변경과 무관하다고 판단한다.
- 로그: `/tmp/cw16fix02/suites2/*.log`. live 보고서: `/tmp/wb-cw16-fix02-reports/` (Root가 run 디렉터리로 복사).

## 3. Root 확인

- 판정 결과가 새로 생겼다. pause·hold 중 cancel 응답의 `worker_notified`가 false이고 `worker_notice`/`detail`이 추가된다. to-worker skill 문구에 반영할지는 Root가 판단한다.
- P2-2의 `resume_finished`는 끝난 run의 파일 대조를 생략한다(프로세스·cwd와 함께). 이 범위가 수용 가능한지 확인을 바란다.
