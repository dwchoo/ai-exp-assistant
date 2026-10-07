# p27-cd70-smoke-03: C-D70 실모델 S3 재smoke (fix-03 이후, D1 확인)

- 역할: test_designer(Sonnet). 2026-10-07 17:23:35~17:26:03 KST(2분 28초/15분). 코드: HEAD c68fb57 작업트리(`src/`·`omp_bridge/`는 fix-03 반영본, 수정 없음), candidate 1dcc538. **omp/18.7.0**. Python `/tmp/cw02-g1-venv/bin/python`.
- **결론: D1 fixed.** manager OMP가 꺼진 동안 worker의 `done` 보고는 `held`가 아니라 `queued`(`waiting_for: manager_session`)로 보존됐고, 새 manager 세션에 **정확히 1회** `kind: report`(commands_run 부록 포함)로 전달됐으며 `manager_recovery` 알림은 `reports_resent: 1`을 냈습니다. manager는 취소·재실행 없이 결과를 사용자에게 설명했습니다. 새 제품 결함 없음.
- 요청 수: **11**(manager 세션1 3 + worker 4 + manager 세션2 4; error 0, aborted 0) / cap 15, pause 13 미도달. fail-closed 카운터 스톨·오류 0회(COUNTER_STALE 없음). 단일 launch(재시도 없음).

## 방법·Isolation
- cd70-smoke-02와 같음: 새 `/tmp/wb-cd70-smoke3-*/{data,project}`(git repo)에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`, ui_v1 client, `workbench attach`를 PTY(pyte 170x40)에 붙여 pane에 입력(prefix+1/2/3). 드라이버는 smoke-02의 `drv70.py`에서 cap/WALL/temp 이름과 기본 시나리오(S3만)만 바꾼 사본입니다. 상태는 `workbench status --json` 스레드(1 s), journal tail, 세션 jsonl로 관측. env에서 `DISPLAY`/`WAYLAND_DISPLAY`/`TMUX*`/`WORKBENCH_*` 제거. `agent.db` 내용은 열지 않았습니다.
- **카운터(fail-closed):** 매 UI pump마다 두 역할의 세션 jsonl 모든 파일(재시작으로 생긴 새 session 포함)을 다시 열거해 assistant message 수를 셉니다. 예외·감소·2 s 이상 갱신 없음이면 자동화 pause+입력 거부, 13에 닿으면 pause+입력 거부. 발동 0회.
- `omp --version`: **omp/18.7.0**. start: `omp isolation: ok (omp/18.7.0 evidence bridge_g3=18.2.10, isolation=18.6.1)`.

| 역할 | state | leaks | warnings | model / thinking | skills |
|---|---|---|---|---|---|
| manager | ok | [] | [] | `openai-codex/gpt-6.1-sol` / high | to-worker, workbench-recovery |
| worker | ok | [] | [] | `openai-codex/gpt-6-luna` / max | to-manager |
- start 직후 세 pane 모두 `alive=True owner=user`, `manual_prompt`. 임시 isolation 프로브 프로세스는 `cleanup state: dead`.

## S3 timeline (KST 17:24:18 입력 = 0 s)
입력(smoke-02와 동일): `worker에게 터미널에서 \`sleep 30; echo cd70-s3-done\` 를 실행하게 하고 결과를 알려줘`

| 시각 | 경과 | 관측 |
|---|---|---|
| 17:24:18 | 0 | manager pane 입력 |
| 17:24:21 | 3 s | manager `read` skill://to-worker |
| 17:24:27.0 | 9 s | manager `to_worker`(work, commands 1개) → `dispatched`, Task 793ec2d7, worker TASK 도착(17:24:27.16) |
| 17:24:30 | | manager 텍스트로 turn 종료("완료 보고가 도착하면 알려드리겠습니다"), worker `read` skill://to-manager |
| 17:24:31.9 | 14 s | worker `terminal` 실행 시작(상태줄 `owner: worker · control_wait`) |
| 17:24:37 | 19 s | **manager pane에 `/exit` 입력**(manager 4 s 이상 쉬는 중, 명령 실행 5 s째) |
| 17:24:39 | +1.8 s | manager pane `exited(0) 종료됨 (Enter: 다시 시작)`, 상태줄 `backend: degraded · bridge manager=down worker=ok` |
| 17:25:02.0 | | 명령 종료(exit 0, 30.09 s), `terminal_notice not_needed` |
| **17:25:03.74** | | worker `to_manager done` → 결과 **`{"status":"queued","waiting_for":"manager_session","handoff_id":"490fbd2f…","detail":"…Workbench keeps this report and delivers it once to the next manager session. Do not send it again; end your turn."}`** (held 아님). journal outbox `pending (waiting_for: manager_session)` → `deferred (target_not_connected)`, 약 2 s 간격 재시도(attempts 1→5). 상태줄 `[실행 중 · done_report_pending]` |
| 17:25:04.8 | | worker 텍스트로 turn 종료(재전송 없음, `to_manager` 호출 1회) |
| 17:25:10 | 약 6 s 후 | 제품 UI에서 manager pane Enter(C-D62 재시작) |
| **17:25:11.25** | +~1.2 s | 새 manager 세션 `…6cfc…` 등록 → 대기 중이던 보고가 새 세션에 created→submitted, 새 세션 입력으로 **`kind: report`** 주입(`in_reply_to` = 원 TASK, payload.`commands_run`: `sleep 30; echo cd70-s3-done`, exit 0, 30.09 s, log_path, `commands_run_note`; message: "The specified command exited with status 0 and printed `cd70-s3-done`…"). Task `closed/done`, 상태줄 `종료(완료)` · `backend: ready · bridge manager=ok` |
| 17:25:12.1 | | `manager_recovery` 알림 queued → deferred(manager가 보고 처리 turn 중) |
| 17:25:16.63 | | manager가 보고에 답함(영어 한 줄: "`sleep 30; echo cd70-s3-done` completed successfully: exit code `0`, stdout `cd70-s3-done`…"). 보고 outbox `delivered / omp_processed` |
| 17:25:16.64 | | `manager_recovery` 알림 sent·주입: `task.status closed / closed_reason done`, `reports_resent: 1`, `reports_unknown: 0`, instruction("…Reports that never reached your old session, or that the worker sent while your OMP was down, arrive as messages (reports_resent); wait for them before you cancel the Task…") |
| 17:25:19.6 | | manager `read` skill://workbench-recovery |
| 17:25:22.8 | | manager `workbench_status`(task_id): task closed/done, `commands_run`(exit 0, 30.09 s, log_path), worker idle/connected, terminal last exited, `reports: []`(이미 전달돼 대기 목록 없음), `watchdog: null` |
| 17:25:26.0 | 총 약 68 s(입력 기준 약 68 s; Enter 기준 약 16 s) | manager 사용자 답(한국어): "세션 복구 후 상태를 확인했습니다. 작업은 이미 완료되어 종료되었으며, 명령을 재실행하지 않았습니다. - stdout: `cd70-s3-done` - stderr: 보고된 내용 없음 - 종료 코드: `0`" |
- **그 밖의 호출:** `to_worker` 1회(S3 위임뿐), cancel 0, 재실행 0, 추가 worker 요청 0. 명령은 한 번만 실행.
- 상태줄(제품 UI 캡처):
```
[17:24:37] focus: MANAGER OMP | host 입력 owner: worker | shell mode: control_wait | worker: 작업중 | 자동화: active
           작업: 작업 "터미널에서 지정된 명령을 그대로 한번 …" [실행중] | backend: degraded | bridge manager=down worker=ok
[17:25:03] … [실행중 · done_report_pending] | backend: degraded | bridge manager=down worker=ok
[17:25:09] pane 제목: `MANAGER OMP *FOCUS* exited(0) 종료됨 (Enter: 다시 시작)`
[17:25:11] focus: MANAGER OMP | … | worker: 대기 | 자동화: idle
           작업: … [종료(완료)] | backend: ready | bridge manager=ok worker=ok
```
- worker pane 화면(17:25:09 캡처)에 `waiting_for: "manager_session"` 도구 결과가 그대로 보였습니다. 새 manager 화면(17:25:51)에는 `kind: report`·`task closed` 표시 후 한국어 답이 있었습니다.
- 후속 host 명령 `echo CD70_S3_FOLLOWUP_$((6*7))` → `CD70_S3_FOLLOWUP_42`, 프롬프트 `$ ` 정상, **overprint·프롬프트 누락 없음**(2 s·5 s 프레임 동일).

## S3 검증 항목
| 항목 | 기대 | 결과 |
|---|---|---|
| manager 종료 중 worker 명령 계속 | 계속 | OK (exit 0, 30.09 s) |
| worker done 결과 | `queued`, `waiting_for: manager_session`, held 아님 | **OK** |
| 큐에 보존 | journal에 outbox 항목, 재시도 | **OK** (`pending/deferred target_not_connected`, 2 s 재시도, attempts 5) |
| worker 재전송 | 없음 | OK (worker `to_manager` 1회) |
| 재시작 후 보고 전달 | 새 세션에 1회 `kind: report` | **OK** (정확히 1건; 중복 없음) |
| 보고 내용 | `cd70-s3-done` 결과 / `commands_run` 부록 | **OK** |
| `manager_recovery` 알림 | `reports_resent: 1` | **OK** (`reports_resent 1`, `reports_unknown 0`) |
| `workbench_status` | 호출 시 상태 반환 | 호출 1회, task closed/done·commands_run 반환 |
| manager의 사용자 설명 | 결과 설명, 취소·재실행 없음 | OK (`cancel` 0, 재실행 0) |
| 후속 host 명령 | 정상 | OK |

## D1 판정: **fixed**
- smoke-02: `held:target_not_connected`, journal에 보고 없음, `reports_resent 0`, 새 세션에 report 없음, manager가 Task를 취소.
- smoke-03: `queued`(waiting_for manager_session) → 새 세션에 report 1회 → `reports_resent 1` → Task `done`으로 정상 종료, 취소 없음. fix-03 설계(`flow.py` `_queue`의 target 없는 큐, `flow_recovery.py` 집계, skill 문구 "Task를 취소하기 전에 기다린다")와 일치합니다.

## 관찰 (결함 아님 / 기존 항목)
- **순서:** 보고가 `manager_recovery` 알림보다 먼저 새 세션에 도착했고(17:25:11.29 vs 17:25:16.64), 알림은 manager의 보고 처리 turn이 끝날 때까지 4.4 s deferred됐습니다(의도된 turn 직렬화). 알림 주입 시점에는 Task가 이미 `closed/done`이어서 `workbench_status.reports`가 `[]`였습니다. 정합.
- **O1(모델 행동, 낮음) 응답 2회·언어:** manager가 보고 도착 직후 영어 한 줄로 한 번, 복구 알림 뒤 한국어로 한 번 답했습니다. 사용자는 두 메시지를 모두 보게 됩니다. 알림이 보고 뒤에 오는 구조의 결과이며 내용 충돌은 없습니다. 첫 답이 영어였다는 점은 모델 행동으로만 기록합니다.
- **O2(기존, cd69 O3 계열, 낮음):** 첫 TASK 전달 outbox가 `unknown / BridgeTimeout`(17:24:47, 20 s)으로 기록됨. worker는 이미 처리 중이었고 알림·재전송은 없었습니다(smoke-02 O3와 동일).
- **O3(표시, 낮음):** manager 종료 중 사용자 화면에는 `done_report_pending`·`backend: degraded`만 보이고 "worker 보고가 manager 재시작을 기다린다"는 별도 안내는 없습니다(C-D70에 표시 요구 없음).
- watchdog 알림(`status_check`/`worker_stalled`)은 발생하지 않았습니다. 보고 대기 구간이 약 7 s뿐이라 60 s 타이머를 넘지 않아 이 실행만으로는 "대기 보고가 watchdog을 막는다"(fix-03 e2e 설계)는 실모델로 검증되지 않았습니다.

## Residue
- `shutdown --yes --json` exit 0, `verified: true`(host_shell·manager_omp·worker_omp·supervisor dead, survivors 0, 강제 kill 없음), 잔여 프로세스 0, project `git status` 깨끗, `/tmp/wb-cd70-smoke3-*` 삭제 확인.
- run dir에는 이 파일과 `-timeline.json`만 추가. scratch(드라이버·세션 사본·화면 캡처)는 Claude scratchpad에만 있습니다. `agent.db` 내용·credential·token·타 프로세스 environ은 읽거나 출력하지 않았습니다. `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex`는 건드리지 않았습니다. commit·graphify update·코드 수정 없음.
