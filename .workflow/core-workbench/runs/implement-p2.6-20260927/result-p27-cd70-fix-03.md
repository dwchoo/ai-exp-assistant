# p27-cd70-fix-03: manager OMP가 없을 때의 worker done/blocked 보고 (smoke-02 D1)

- 역할: worker_senior (Opus). 기준 HEAD bc131d5. 실모델·provider 요청 없음, commit·graphify update 없음.

## Checks
| 항목 | 명령 | exit | 결과 |
|---|---|---|---|
| red (수정 전) | `tests/backend` `python -m unittest test_cd70_fix03` | 1 | 12건 중 8건 실패(`held:target_not_connected`). 회귀 가드 4건은 통과 |
| green (수정 후) | 같은 명령, 5회 반복 | 0 | 12/12 OK, 5회 모두 OK |
| tests/backend | `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/backend -t tests/backend -p 'test_*.py'` | 0 | 1073 OK (skipped 30, expected failure 1) |
| tests/bridge (py) | 같은 형식 | 0 | 52 OK (skipped 1) |
| tests/bridge (node) | `node --test tests/bridge/*.test.ts` | 0 | 43 pass / 0 fail |
| tests/terminal | 같은 형식 | 0 | 208 OK (skipped 2) |
| tests/workflow | 같은 형식 | 0 | 58 OK |
| tests/gates/g2_shell | 같은 형식 | 0 | 142 OK (skipped 1) |
| tests/contracts | 같은 형식 | 0 | 52 OK |
| tests/ui | 같은 형식 | 0 | 819 OK |
| 수정이 필요한 independent 테스트 | — | — | 없음(전 suite 통과) |

## 설계 (file:line)
- `flow.py:205-207` `HandoffDecision.wait_for_target`(기본 False). `flow_tasks.py:1088-1091` `_to_manager`가 work Task의 done/blocked 보고(`keep_across_pause`와 같은 대상)에만 켭니다.
- `flow.py:1060-1097` `_queue`: peer가 없을 때 `wait_for_target`이면서 worker→manager인 경우에만 대상 세션 없이(target None) 큐에 넣습니다. journal outbox 기록에 `waiting_for: manager_session`이 붙고, tool 결과는 `{"status":"queued","handoff_id",…,"waiting_for":"manager_session","detail":QUEUED_WAITING_DETAIL}`입니다(`flow.py:98-102`: "manager OMP is not connected right now; … delivers it once to the next manager session. Do not send it again; end your turn."). 그 밖의 handoff(manager의 to_worker, worker progress/report/answer, backend notice)는 지금처럼 `held:target_not_connected`입니다.
- lane: 대상 세션이 없는 항목은 `_create`에서 `TaskMailbox.create_message`→`bridge.peer(timeout 0)`가 `BridgeDisconnected`를 내므로 `deferred`(`target_not_connected`)로 남고 최대 2 s 간격으로 다시 시도합니다. 아무것도 만들거나 보내지 않습니다. manager가 등록되면 그 세션에 만들어 한 번 전달합니다. 기존 `_create` 세션 불일치 검사는 target이 None이면 건너뜁니다. pause 중에는 `keep_across_pause`로 `kept_paused`(pending)로 남고 resume 뒤 전달됩니다. 철회(Task 취소)되면 `withdrawn`이며, `unknown`·submitted는 다시 보내지 않습니다(기존 불변식 그대로).
- 집계: `flow.py:782-783` `_unbound_uncounted`, `flow.py:913-914`·`flow.py:937-952` `_unbound_for(current)`. `requeue_for_new_session`(watchdog `manager_recovery`)은 대상 세션 없이 큐에 들어간 항목 가운데 아직 만들어지지 않았거나 새 세션에 만들어진 것을 `reports_resent`에 한 번만 더합니다(lane이 알림보다 먼저 전달한 경우도 포함). `unknown`·withdrawn·rejected는 세지 않습니다(unknown은 `reports_unknown`에 나옴). 같은 세션으로 다시 연결된 경우 그 세션에 전달되고, 다음 새 세션에서는 세지 않습니다.
- `workbench_status`: 기존 `report_entries`가 pending·deferred 항목을 내용과 함께 보여 줍니다(`state: deferred`, `reason: target_not_connected`). service.py 변경은 없습니다.
- Watchdog: 대기 중인 보고는 `lane_busy(WORKER)`를 True로 만들므로 `message_pending`이 되어 `status_check`·`worker_stalled`가 나가지 않습니다(e2e 테스트로 400 s 확인).
- 기존 경로: 옛 세션에 이미 만들어진 보고(manager 종료→새 세션)는 지금처럼 `requeue_for_new_session`·`target_session_changed` 재큐로 새 세션에 갑니다(`test_the_old_session_path_still_requeues`).
- 문구: `flow_recovery.py:86-91` `RECOVERY_HINT`, `omp_bridge/skills/workbench-recovery/SKILL.md:15`는 "worker가 manager OMP가 꺼져 있는 동안 보낸 보고도 메시지로 도착하니 Task를 취소하기 전에 기다린다"는 내용입니다(smoke O2 대응). `omp_bridge/skills/to-manager/SKILL.md:56,61`에는 `waiting_for: manager_session` 결과와 `held:target_not_connected` 결과를 안내했습니다.

## 테스트 (tests/backend/test_cd70_fix03.py, 실제 TaskFlow+HandoffService, fake mailbox·peer)
- done/blocked 보고가 대상 세션 없이 큐에 들어가 새 세션에 1회 전달됩니다. 대기 중 상태(`report_entries` 내용, `lane_busy`, `done_report_pending`)를 확인하고, Task가 `closed/done`·`blocked`로 넘어가며 count는 1회만 셉니다.
- lane이 먼저 전달해도 count는 1입니다. 같은 세션 재연결 뒤 새 세션에서는 count 0입니다. unknown은 재전송·집계하지 않고, withdrawn은 전달·집계하지 않습니다. pause 중에는 유지되고 resume 뒤 1회 전달됩니다. pause 중 보고는 `held:paused`입니다.
- progress/report/answer와 worker가 없을 때의 to_worker는 여전히 `held:target_not_connected`입니다. 옛 세션 경로의 재큐도 확인했습니다.
- e2e: 실제 Watchdog(가짜 시계)을 service.py와 같은 포트로 연결했습니다. manager가 사라진 뒤 400 s 동안 worker 알림은 0건이었습니다. 새 세션 등록 시 `manager_recovery`는 1건(`reports_resent 1, reports_unknown 0`)이고, 보고는 새 세션에 1건 전달되어 Task done으로 끝났습니다. 이후 200 s 동안 중복은 없었습니다.

## 변경 경로 + sha256
| path | sha256 |
|---|---|
| src/workbench/backend/flow.py | faa67de2e792ee9d0daacc79bbd0dc1f36c3ce6146debbacc1dab2ba51c3a72d |
| src/workbench/backend/flow_tasks.py | 9b756864f05ecd5fcb145202c542008a64eb4ca4882f63249ab2ce05533f2bf8 |
| src/workbench/backend/flow_recovery.py | 35c8707889d8e08462aa77d7e62caa2dc4ed8174e393beac23d02a48835922f8 |
| omp_bridge/skills/to-manager/SKILL.md | bbeba20792ce460c4ff0bfaf2d1020c1e267d02440cf979e06f4e5b6ecafcd45 |
| omp_bridge/skills/workbench-recovery/SKILL.md | e108ef791f16217b58970b5dc716c948dc3dae9dcb34117760afafbba128153e |
| tests/backend/test_cd70_fix03.py (신규) | b7fdd8c05928087ad9332888c5068df7b68be1955e23a437cc9610cb6860c369 |

## 비고
- `.workflow/.../ledger.json`의 기존 변경은 작업 전부터 있었고 손대지 않았습니다.
- 남은 일: 이 시나리오(S3)를 실모델로 다시 smoke해야 합니다. 대기 중 재시도는 2 s마다 sqlite에서 run을 1회 읽습니다(비용 낮음).
- 임시 파일은 Claude scratchpad(suite 로그)에만 있습니다. 프로세스는 테스트 하네스만 사용했고 따로 띄운 프로세스는 없습니다.
