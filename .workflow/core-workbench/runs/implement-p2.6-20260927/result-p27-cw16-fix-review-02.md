# p27-cw16-fix-review-02 — fix-02 delta 검토 (reviewer, read-only)

작성 2026-10-08. 기준 `6a2ddb2` + working tree. 모델 요청 0, 자격 증명·타 프로세스 environ 미접근, 임시 파일은 `/tmp/cw16rev2/`만 사용했다.

## 판정: **pass** (P0/P1/P2 없음, P3 3건)

## 결함

### P3-1 env-names 파일 mode가 0600이 아니고, hook이 못 쓰면 오래된 파일이 그대로 쓰인다
- `lifecycle.py` `__b_emit`의 `>|"$BOUNDARY_ENV_NAMES"`: 파일 mode는 shell의 umask를 따른다. 실측 0664(bash·dash), 디렉터리는 0700이라 접근은 막힌다.
- 사용자가 `unset BOUNDARY_ENV_NAMES`를 하면 hook이 쓰기를 건너뛴다. 이후에도 READY는 나가므로 `panes.py:_read_env_names`는 이전 prompt의 완전한 파일을 읽는다.
  - 실측: unset 뒤 `export WB_AFTER=1` → `exported_names(["WB_AFTER","WB_START"])`가 `{WB_START}`다. 이후 `unset WB_START`도 반영되지 않는다.
  - 사용자가 자기 shell을 스스로 망가뜨릴 때만 생기고 값은 노출되지 않는다.
- 수정 방향: hook에서 `umask 077` 하위 shell로 쓰거나 최초 생성 때 mode 고정, 쓰기 전에 파일을 지우고 `BOUNDARY_ENV_NAMES` 미설정 때는 파일을 지워 None이 되게 한다.

### P3-2 `_cancel_notice_state`의 상태 표시는 enqueue 이후 시점에 계산된다
- `flow_tasks.py:1047`. enqueue 직후 pause/hold가 바뀌면 `after_resume`/`queued` 표시가 실제와 어긋날 수 있다. 알림 자체는 keep으로 정확히 1회 전달되므로 receipt 문구에만 영향이 있다.

### P3-3 (관찰) 끝난 run의 resume은 `bound.view`를 쓰는 coordinator 경로이고, 파일·프로세스 대조는 없다
- Root 답변 ②에 따라 수용. `resume_finished`는 같은 current/approval, 두 OMP paused·idle·in-flight 0만 검증한다.

## 항목별 확인

- **P2-1**: cancel은 `keep_across_pause=True`로 enqueue된다(`flow_tasks.py:1045`). pause 중 `worker_notified:false`, `worker_notice:"after_resume"`다. 테스트가 pause→cancel→0.3 s 전달 0→resume→전달 1건·생성 1건을 확인한다(실제 FlowFixture). pause하지 않은 cancel은 즉시 알린다.
- **P2-2**: `PauseCoordinator.resume_finished`(`controller.py`)는 기존 `_resume` transition과 fence를 그대로 쓴다. 검증만 `_validate_finished_resume`으로 바꿨다. cancelled run·scope 불일치 거절을 유지한다. 반환은 `finished_while_paused` journal이다.
  - `automation.py:_resume`는 `host_finished`만 세우고 bound를 유지한다. 재-pause는 frame이 양쪽에 +1이고, 두 번째 resume도 reconcile된다. review는 0이고 `shutdown_bound`가 run을 돌려준다. `run_ended` 뒤에만 unbind된다(테스트 확인).
  - `run_ended`와 resume이 겹쳐도 `bound.ended`일 때만 unbind하므로 일관된다.
- **P3-1(probe)**: 입력 없이 prompt hook이 이름만 쓴다. 쓰는 시점은 READY 이전이다.
  - 실측(bash·dash): `false` 뒤 `RC=1`이다. 입력은 사용자 것뿐이고 pane·history에 `__b_`/`WBENV1`이 없다. 값 없는 `export X`와 local은 제외된다. `set -C`에서도 쓰인다. 비-ASCII 이름은 걸러진다.
  - reader는 O_NOFOLLOW, uid, 1 MiB 상한, `\001END` 표식을 확인한다. idle(manual_prompt, 마지막 event READY)일 때만 읽고 부분 파일은 0.5초 안에 재시도한다.
  - 디렉터리 0700, 값은 파일에 들어가지 않는다. 우리 shell만 쓴다.
- **P3-2(usage)**: `WorkerReviewScheduler.usage_snapshot`이 실제 `AutomationController.review`에서 호출된다. 테스트는 unknown→1234를 확인하고 fake가 아니다.

## 검증 (env -i, venv python)
- 신규 `test_cw16_fix01/02.py` 19 OK.
- backend 전체 1114 OK (skipped 30, expected failure 1), 668 s.
- recovery_boot 170 OK, ui 842 OK, terminal OK.
- observation 95 OK, policy/pause_automation 129 OK, gates/g2_shell 142 OK (skipped 1).
- 처음 병렬 실행의 일부 실패는 cwd가 `/tmp`라서 생긴 import 오류다. repo root에서 재실행해 통과했다.
