# p27-cd70-review-03: C-D70 fix-03 (D1) delta review

verdict: **pass (integrate)** — P0/P1/P2 없음, P3 2건.

## 확인
- `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/backend -t tests/backend -p 'test_cd70*.py'` → 91 OK (재실행).
- unbound 큐잉은 `wait_for_target and sender WORKER and target MANAGER`일 때만(flow.py:1070-1076). `wait_for_target`은 `_to_manager`의 work done/blocked(`report`)에서만 켜짐(flow_tasks.py:1091). progress/report/answer/to_worker/notice는 `held:target_not_connected` 유지.
- 전달: `_create`는 target None이라 세션 불일치 검사를 건너뛰고(flow.py `_create`), peer 없음이면 `BridgeDisconnected`→`deferred`(2 s 재시도). 등록된 새 세션에 1회 생성·전달. submitted/unknown 재전송 없음(`_requeue_eligible`·`_TERMINAL_STATES` 기존 불변식).
- pause: `keep_across_pause` 유지. watchdog: pending 항목이라 `lane_busy(WORKER)` True → 알림 없음. cancel: `withdraw`가 pending unbound도 `withdrawn` 처리.
- 등록 race(요청 항목): (a) lane이 먼저 전달 → `_unbound_for`가 message 세션==current로 1회 count, 이후 loop는 state가 pending/delivering이 아니라 중복 없음. (b) lane이 create 중(message None, state delivering) → `_unbound_for`가 count, loop는 `bound is None`이라 건너뜀 → 이중 count 없음. (c) 전달 대기 중 새 manager 세션 재교체 → loop의 requeue 경로가 count, `_unbound_for`는 message 세션≠current라 제외 → 이중 count 없음. 이중 전달 경로 없음(entry는 message 1개, 전달은 lane 1개).
- 구 세션 경로(`target_session_changed`→requeue, count): 변경 없음, `test_the_old_session_path_still_requeues` 통과.
- 문구: 결과 detail("not connected … delivers it once to the next manager session … end your turn"), to-manager SKILL, `RECOVERY_HINT`/workbench-recovery SKILL의 "취소 전에 도착을 기다린다" 모두 실제 동작과 일치.
- 테스트는 실제 TaskFlow+HandoffService+실 Watchdog에 fake mailbox/peer를 쓰는 e2e로 고쳐진 동작(대기→새 세션 등록→1회 전달·count 1·Task closed)을 실제로 검증. mock이 결과를 대신하지 않음.

## Findings
- **P3** flow.py:937-952 `_unbound_for` — manager 세션이 S2→S3으로 연달아 바뀌고 lane이 S2 recovery 직후·S3 등록 전에 이 항목을 "S3용으로" 생성하는 좁은 창에서는 S2 notice가 먼저 count(이미 죽은 세션에 가는 notice), S3 notice는 `reports_resent 0`인데 보고는 S3에 도착. 중복 전달·유실은 아니고 count 표시만 어긋남. 수정 방향: count를 notice 시점이 아니라 전달 대상 세션 기준으로 유지(`_unbound_uncounted`를 message 세션≠current일 때 버리지 말고 보존). 테스트 없음.
- **P3** flow.py `_queue`/lane — manager가 영영 돌아오지 않으면 보고는 deferred로 무기한 대기, `lane_busy(WORKER)`로 watchdog은 계속 침묵, Task는 `done_report_pending` 유지(취소만 해소). 설계 요구("watchdog quiet")와 일치하나 사용자 표시·상한 없음. 필요하면 후속 판단.

## 이전 finding 판정
- D1(manager 종료 중 worker done/blocked 보고 유실): **fixed** — queued+waiting_for, 새 세션 1회 전달, reports_resent 반영, workbench_status 내용 노출, 기존 requeue 경로 회귀 없음. 실모델 S3 재smoke는 별도 필요(worker 비고와 동일).
