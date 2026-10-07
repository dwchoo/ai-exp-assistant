# p27-cd70-ui-01: pre-UR UI polish (거부 알림 한국어화, manager 부재 보고 대기 표시)

- 역할: worker (wb-worker). 기준 b51ae1f. 실모델·provider 요청·commit·graphify update 없음. 별도 프로세스·temp 없음(suite 로그는 scratchpad).

## Checks
| 항목 | 명령 | exit | 결과 |
|---|---|---|---|
| red (ui) | `-s tests/ui -p test_product_model_cd70.py` | 1 | ImportError(REPORT_WAIT_NO_MANAGER_TEXT) |
| red (backend) | `-s tests/backend -p test_cd70_ui_wait.py` | 1 | 3건 중 2건 실패(reason/waiting_for 없음) |
| green (backend 신규) | 같은 명령 5회 | 0 | 3/3 OK ×5 |
| tests/ui | `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/ui -t tests/ui -p 'test_*.py'` | 0 | 824 OK, 202 s |
| tests/backend | 같은 형식 | 0 | 1076 OK (skipped 30, expected failure 1), 642 s |
| tests/contracts | 같은 형식 | 0 | 52 OK, 0.3 s |

## 변경 (file:line)
- `src/workbench/ui/product/model.py:113-116` `REPORT_WAIT_NO_MANAGER_TEXT`("worker 보고 대기 중: manager OMP가 꺼져 있음 (다시 시작하면 전달)"), `REPORT_WAIT_REASON_TEXT`. `:129-135` `HELD_REASON_TEXT`(= `TASK_HELD_TEXT` + worker 명령 실행 중/manual_jobs/unknown_or_manual_residue/paused/target_not_connected 한국어). 거부 알림은 `(보류: …)`로 한국어 표시하고 모르는 code는 raw 그대로, JOB_HELD hint는 raw code 기준으로 유지(`~:810`). `recovery_text`(~`:2007`)는 `report_wait.reason`으로 문구를 고르고, reason이 없거나 모르면 기존 editor 문구(하위 호환).
- `src/workbench/backend/flow.py` `_OutboxEntry.queued_at`(`:719`), `report_entries`에 `waiting_for`("manager_session") / `waiting_since` 추가, `_waits_for_manager`(`:896`): pending·메시지 미생성·target 없음·미제출·미철회·reason `target_not_connected`일 때만. 전달되면 None.
- `src/workbench/backend/flow_recovery.py` `report_wait()`: 대기 항목이 있으면 30 s 지연 없이 `reason: manager_session_not_connected`(editor 대기와 섞이면 이 reason이 우선, count는 합계). 전달·제출 후 null. docstring 갱신.
- `src/workbench/contracts/ui_v1.py` Recovery docstring에 새 reason과 하위 호환 규칙을 추가했습니다(필드 추가 없음, `reason`은 이미 존재).
- service.py 변경 없음(기존 `reports=handoffs.report_entries` 포트를 그대로 사용).
- tests: `tests/ui/test_product_model_cd70.py`(+5건: 새 문구, 알 수 없는 reason, 거부 알림 한국어·raw fallback·job hint), `tests/backend/test_cd70_ui_wait.py`(신규 3건: 실제 HandoffService+Watchdog, manager peer 부재→표시→전달 후 해제, 연결된 manager는 표시 없음, 혼합 우선순위).
- 기존 테스트 수정 1건: `tests/ui/test_product_model.py:324` (`test_job_held_refusal_shows_how_to_clear_it`)가 raw `manual_jobs`/`unknown_or_manual_residue`를 기대했으나 한국어 문구로 바꿨습니다. independent 테스트는 `unsubmitted_or_unconsumed_input`·`multiline_residue`(미매핑 → raw)만 확인하므로 영향 없습니다.

## 비고·gap
- 거부 알림 접두는 `(held: …)` → `(보류: …)`로 바뀌었습니다. independent/live 테스트는 `held` 문자열을 `handoff_held` reason 쪽에서 만족합니다(suite 통과).
- `waiting_since`는 watchdog과 같은 monotonic clock 기준입니다(서비스 기본 `time.monotonic`). 테스트의 fake clock에서는 절대값을 검증하지 않았습니다.
- 실제 터미널 렌더(긴 알림 줄 clip)는 확인하지 않았습니다. 화면 폭에 따른 clip은 기존 동작입니다.
