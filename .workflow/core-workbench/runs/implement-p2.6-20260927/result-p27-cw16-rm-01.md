# p27-cw16-rm-01: CW-16 실모델 smoke RM1·RM3·RM4 (RM2 예산으로 미실행) — C16(e635723) 신뢰도 증거(게이트 증거 아님)

- 역할: test_designer(Sonnet). 2026-10-08 21:40~21:47 KST(실행 약 7분 / 45분). 코드: repo HEAD e635723(수정 없음). **omp/18.8.0**. Python `/tmp/cw02-g1-venv/bin/python`. 임시 데이터/프로젝트는 `/tmp/wb-cw16-rm-*`(삭제 확인).
- **결론: 제품 결함(blocker) 없음.** RM1 정상 흐름, RM4 backend 재시작 복구(same_boot_crash, notice 1회, survivors 목록, 재실행 없음), RM3 pause/resume(replay 없음)은 기대대로 동작했습니다. **RM2(210 s 장기 명령)는 실행하지 못했습니다** — RM4가 19 요청을 써 남은 예산(36 이하)에 들어가지 않았습니다(아래 "한계"). 관찰 O1~O5는 낮음/정보.
- 요청 수(세션 jsonl assistant message, fail-closed 카운터): **총 32 / 40 cap, pause 36 미도달**. 1차 실행 12(RM1 8 + 중단된 RM2 시도 4, aborted 1) + 2차 실행 20(RM4 19 + RM3 1). error 0(aborted 1). COUNTER_STALE·INPUT_REFUSED 0회.

## 방법·Isolation
- smoke-04와 같은 방식: 새 `/tmp/wb-cw16-rm-*/{data,project}`(git), `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`, `workbench attach`를 PTY(pyte 170x40, repo `Ui`의 rep_screen_classes)에 붙여 prefix+1/3으로 pane에 입력. env에서 DISPLAY/WAYLAND/TMUX*/HERDR_*/WORKBENCH_* 제거. 상태는 `workbench status --json` 1 s 폴링 + journal tail + 세션 jsonl. `agent.db` 내용·credential·타 프로세스 environ은 읽지 않았고 `credential_pin` 레코드는 결과에 옮기지 않았습니다.
- `omp --version`: **omp/18.8.0**. start: `omp isolation: ok (omp/18.8.0 evidence bridge_g3=18.2.10, isolation=18.6.1)` (1차·2차·restart 모두). 세 pane `alive=True owner=user`, `manual_prompt`.
- **카운터(fail-closed):** 매 UI pump마다 data dir의 모든 세션 jsonl(재시작으로 생긴 새 session 포함)을 다시 열거해 assistant message를 셉니다. 예외·감소·2 s 이상 갱신 없음이면 자동화 pause+입력 거부, 상한 도달 시 pause+입력 거부. 2차 실행은 1차에서 쓴 12를 빼 상한 24(=36−12)로 걸었습니다. 발동 0회. post-shutdown 재집계가 실시간 합과 일치(12, 20).
- **실행 경위(투명성):** 1차 드라이버에서 RM1 후 RM2 종료판정 버그(직전 Task가 closed라 입력 직후 "closed"로 오판)로 RM2 직후 RM3 입력이 겹칠 뻔해 **SIGINT로 즉시 중단**했습니다(RM3 입력 전, RM2 후속 host 명령 입력 중). 판정에 task_id 변경 조건을 넣어 2차 실행(새 data dir)에서 RM4→(RM2 예산 skip)→RM3을 수행했습니다. RM2의 1차 시도(요청 4개: manager to_worker 위임 → worker `terminal` 호출)는 shutdown 중 `held:shutdown_closing`·worker aborted 1로 끝나 **RM2 증거로 쓰지 않습니다**.

## RM1 — 짧은 정상 흐름 (`uname -a; nproc`) 8 요청 ✔
입력 12:40:45.9Z (=21:40:46 KST).
| UTC | 경과 | 이벤트 |
|---|---|---|
| 12:40:48.7 | 2.8 s | manager `read skill://to-worker` |
| 12:40:53.56 | 7.7 s | manager `to_worker(kind=work, commands 1개)` → `dispatched` (task b72aa2cd) |
| 12:40:53.70 | | TASK가 worker에 도착(0.12 s) |
| 12:40:57.4 | | worker `read skill://to-manager` |
| 12:40:59.18 | TASK→terminal **5.5 s** | worker `terminal`(`uname -a; nproc`, manager 명령과 동일) → 0.1 s 뒤 `exited` exit 0, `output_tail` 포함 |
| 12:41:01.94 | TASK→done **8.2 s** | worker `to_manager kind=done` → `queued` |
| 12:41:01.98 | | 보고가 manager에 전달: `kind: report`, `payload.commands_run`(exit 0, 0.092 s, log_path) + `commands_run_note` |
| 12:41:07.79 | 입력→최종 **21.9 s** | manager 최종 답변(한국어; uname 출력과 nproc=8) |
- 도구 호출: manager read 1/to_worker 1, worker read 1/terminal 1/to_manager 1. 알림: `status_check`·`worker_stalled`·`terminal_check`·`terminal_done`·`manager_recovery` 0건(`terminal_notice outcome=not_needed`). done 1회. 후속 host 명령 `echo CW16_RM1_FOLLOWUP_$((6*7))` → `42` 확인.

## RM4 — 열린 Task 중 backend SIGKILL + 재시작 19 요청 ✔(복구 동작은 기대대로, 비용이 큼)
명령 `sleep 90; echo cw16-rm4-done`. 입력 12:43:01.9Z.
| UTC | 이벤트 |
|---|---|
| 12:43:11.4 | manager `to_worker(commands=[…])` → dispatched (task 2d4c2680, run 0bfd2d36) |
| 12:43:15.97 | worker `terminal`(`sleep 90; …`) 시작 |
| 12:43:28.2 | **내가 시작한 backend pid(snapshot의 pid와 start_ticks 일치 확인 뒤 exact pid)에 SIGKILL**(명령 시작 +12 s). 같은 순간 worker의 대기 중 terminal 도구 결과: `{"status":"outcome_unknown","reason":"session_shutdown"}` |
| 12:43:28~32 | 열려 있던 attach UI 프로세스는 4 s 안에 종료. 이전 incarnation의 python/bash/**sleep** 3개 프로세스가 남음(host shell 세션 구성원) |
| 12:43:36~38 | `workbench start` 재실행 2.6 s, exit 0, backend pid 새로 발급, 상태줄/출력에 `Task … run 1` 표시, isolation ok |
| 12:43:37.7 | 새 manager 세션에 **`backend_restarted` notice 정확히 1회**(journal: notice 3f968a92 `queued` 12:43:36.6 → `sent` 12:43:37.6 1건씩, held_reason 기록 2줄 포함 4줄 일치; manager 세션 user message는 1건): `classification: same_boot_crash`, `task.held_reason: backend_restarted`, `run.state: outcome_unknown`, `survivors` 3개(s1 python / s2 bash / s3 sleep, 모두 `stoppable:false`, `identity: unverified`, why_not "its session leader (host_shell) is gone…"), `outbox_not_sent: 1`, 지침("Task는 취소·재전송·재실행되지 않았다…") |
| 12:43:40~47 | manager `read skill://workbench-recovery`, `workbench_status`(task_id), `read skill://to-worker` |
| 12:43:58.7 | manager의 결정 = **계속**: `to_worker`(task_id 지정, follow-up "재실행하지 말고 로그만 회수"; `resent_task: true`) → queued. worker 새 세션이 TASK 재전송 수신(`commands_already_run: status unknown`) |
| 12:44:05~13 | worker는 **명령을 재실행하지 않고**(terminal 호출 합계 1) 로그 파일을 read(빈 파일)한 뒤 `to_manager kind=answer`("상태 unknown, 회수 가능한 출력 없음, s1/s2/s3는 건드리지 않음") |
| 12:44:18 | manager `workbench_status`로 재확인 |
| 12:44:24.6 | manager 결정 = **취소**: `to_worker(cancel:true)` → `cancelled`, worker_notified |
| 12:44:29 | worker가 `to_manager done` 시도 → `{"status":"rejected","reason":"no_active_task"}`(무해), manager가 사용자에게 최종 답변: "종료 결과는 확인 불가, 재실행 안 함, 남은 프로세스는 신원 미검증이라 종료하지 않음" 표 포함 |
- 상태줄(재시작 직후, 제품 UI): `backend 비정상 종료 뒤 재시작 · 이전 run 결과 불명(재실행 없음) · 보내지 못한 메시지 1건 · 이전 backend의 process 3개 남음 (status·manager 확인)`. Task 종료 후 survivors 문구는 사라졌고 앞 3 항목은 남아 있음(O3).
- 도구 호출 합계: manager read 3·workbench_status 2·to_worker 3(위임·follow-up·cancel), worker read 3·terminal 1·to_manager 2. 요청: manager 11 + worker 8 = **19**. error 0.
- 알림: `status_check`/`worker_stalled`/`terminal_check`/`terminal_done`/`manager_recovery` 0건. `backend_restarted` 1건. 후속 host 명령 → `CW16_RM4_FOLLOWUP_42` 확인.
- 판정: 기대(same_boot_crash reconcile, manager에 notice 1회, manager 결정 continue/cancel, survivors 열거·무단 종료 없음, 재실행 없음)를 모두 충족. 남은 `sleep 90`은 약 12:44:46에 스스로 끝남.

## RM3 — 수동 manager turn 중 pause/resume 1 요청 (설계상 abort는 해당 없음)
프롬프트(도구 없는 긴 답): "도구는 쓰지 말고, 1부터 25까지 각 수의 약수를 전부 나열한 아주 긴 표를 설명과 함께 작성해줘" 입력 12:45:11.6Z. 입력 후 ~3.8 s에 pause.
| UTC(KST) | 이벤트 |
|---|---|
| 12:45:14 | pause 직전: 이 prompt에 대한 assistant message 아직 없음(요청 진행 중) |
| 12:45:15 | prefix p → 확인창("자동화 일시정지 확인 / 자동화를 일시정지합니다 (p: 확인)", 키는 pane으로 전달되지 않음) → p. 상태줄 `자동화: paused`, snapshot `automation.paused: true`, detail "paused by the user; collection continues, new automatic work is held", **`interruption.state: not_needed`**(진행 중인 run이 없음) |
| 12:45:15~12:45:46 | 일시정지 31 s 동안 요청 증가 0, 자동 작업 0 |
| 12:45:47 | prefix p → 확인창("자동화 재개 확인 / 대조 후 재개합니다 (p: 확인) · 일시정지 중 바뀐 상태를 확인(대조)한 뒤에만 확인하세요") → p. snapshot `resume.outcome: resumed`, 상태줄 `자동화: idle` |
| 12:46:17.9 | manager의 긴 표 답변 도착(사용자 prompt 입력 후 66 s, resume 후 30 s) — 이후 새 요청 0, **replay·중복 입력 없음**(user message 1건) |
- **기대 중 "turn abort requested/confirmed 표시"는 관측하지 못했습니다.** 이유: 이 pause 시점에 진행 중인 자동화 run(Task)이 없어서 코드가 `interruption: not_needed`로 처리합니다(`automation.py` L1118: run bind가 없으면 not_needed). 사용자가 직접 입력한 manager turn은 pause의 abort 대상이 아니라 pause 중에도 끝까지 진행됩니다(설계 해석; 사양 문구와 대조는 Root 판단). **run이 있는 상태에서의 pause(worker/manager turn abort)는 이번 실모델로 확인하지 못했습니다**(예산, 아래).
- 비용: 1 요청(+ 후속 host 명령 `CW16_RM3_FOLLOWUP_42` 확인).

## RM2 — 210 s 장기 명령: **미실행(예산)**
- 2차 실행의 RM2 게이트(요청 9 필요)가 RM4 19 요청 뒤 `tot+9 > 24`(=남은 36 이하 예산)로 skip. 우선순위(RM1, RM4, RM2, RM3)상 RM2가 RM3보다 앞이지만 RM3(1 요청 수준)은 남은 예산에 들어가 RM3을 수행했습니다. RM2를 하려면 약 +8~10 요청이 필요해 pause 36을 넘고 hard 40에 근접합니다.
- 따라서 **"running 반환(120 s) 후 worker turn 종료 → terminal_check ≥1 → terminal_done 1회 → status_check 없음 → done 1회"의 실모델 확인은 이번 run에 없습니다.** 가장 가까운 이전 증거는 cd70-smoke-04의 150 s 명령(terminal_done 1회, 28 s 쉼 구간, status_check 없음)이고 ≥200 s 사례는 여전히 실모델 미확인입니다(RM2 요청 시 추가 예산 필요).

## 관찰 / 분류 (결함 아님 또는 낮음)
- **O1 (정보, 비용):** RM4 복구가 19 요청(manager 11)으로 정상 흐름(8)의 2.4배입니다. manager가 복구 skill/status/follow-up/재확인/취소를 거치며 cancel로 끝냈고, worker는 재전송된 TASK를 읽은 뒤 빈 로그만 보고했습니다. 동작은 정확하지만 사용자 인지 지연·비용은 큽니다.
- **O2 (낮음, 표시/경합):** 취소 직후 worker가 `to_manager kind=done`을 시도해 `rejected/no_active_task`를 받았습니다. 무해하나 worker 쪽에는 이미 `task_cancelled` notice가 갔는데도 보고를 시도한 모델 행동입니다.
- **O3 (낮음, 표시):** 재시작 상태줄의 "backend 비정상 종료 뒤 재시작 · 이전 run 결과 불명(재실행 없음) · 보내지 못한 메시지 1건"은 Task가 `종료(취소됨)`가 된 뒤와 RM3 전체(재시작 후 약 3분)에도 계속 표시됩니다. 사용자가 지울 방법 없이 backend 수명 동안 남는지(의도?)는 확인하지 못했습니다(Root 판단).
- **O4 (낮음, 1차 실행 중 발견, 재현 안 함):** 활성 작업이 있는 상태(manager/worker turn, worker `sleep 210` Task run)에서 `workbench shutdown --yes --json`이 **exit 1**, stdout에 active work 목록, stderr에 `TimeoutError: no frame from backend`(client.py:119/132) traceback으로 끝났습니다. `cmd_shutdown`의 UiClient timeout=10 s이고 호출~실패가 약 11 s라 확인 응답이 10 s를 넘긴 것으로 보입니다. 같은 시도에서 15 s 안에 소유 프로세스는 모두 사라졌고(residue 0) shutdown 결과 JSON(`verified`)은 받지 못했습니다. 활성 작업이 없거나 수동 foreground 프로그램(`sleep 100`)만 있을 때는 0.4 s, `verified:true`(별도 확인 실행, 요청 0). 활성 turn이 있을 때만 느려지는 것으로 보이며 원인은 확인하지 못했습니다. **traceback과 미확인 종료 보고(C-AC-22)가 사용자에게 보일 수 있는 경로**라 Root가 판단하도록 올립니다(blocker 아님, 실모델 재현은 요청이 필요).
- **O5 (정보):** RM4에서 기존 attach UI는 backend SIGKILL 4 s 안에 종료했고(`alive=False`) 재시작 후 새 attach가 정상 연결됐습니다. worker의 대기 중 terminal 호출은 `outcome_unknown/session_shutdown`로 즉시 풀렸습니다.
- 언어: worker(gpt-6-luna)의 일부 텍스트가 영어, manager는 RM1/RM4/RM3 모두 한국어로 사용자에게 답했습니다(몽골어는 이번에 없음).

## Residue / 규칙 준수
- 두 실행 모두 종료: 1차는 shutdown이 exit 1(O4)이었지만 소유 프로세스 잔여 0·root 삭제 확인(`root_removed: true`). 2차는 `shutdown --yes --json` exit 0, `verified: true`(host_shell -1, manager·worker 143, survivors 없음, previous_survivors 없음), 잔여 0, 강제 kill 없음(`kill_exact`는 RM4의 exact backend pid SIGKILL 1회만, 시작한 pid·start_ticks 일치 확인 후). 별도 shutdown 확인 실행(`sd.py`)도 root 삭제 확인. `/tmp/wb-cw16-rm-*` 없음. 임시 project `git status` 깨끗.
- repo 작업트리 쓰기 없음(run dir에 이 파일과 `-timeline.json`만 추가). 드라이버·세션 사본·화면 캡처·로그는 Claude scratchpad(`rm/`)에만 있습니다. `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux/herdr, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex`는 건드리지 않았습니다. credential 내용·타 프로세스 environ 미접근, token 미출력. commit·graphify update·`src/**` 수정 없음.

## 권고 (Root 판단용)
1. RM1·RM3(비-run)·RM4 복구 경로는 C16에서 실모델 회귀 없음. RM4 notice 1회·survivors 열거·재실행 없음 확인.
2. 미확인으로 남은 것: ① ≥200 s 장기 명령(terminal_check/terminal_done/status_check 억제) ② run 진행 중 pause의 turn abort 표시. 필요하면 추가 요청(각 약 8~10)을 승인받아 별도 실행.
3. O3(재시작 배너 지속)와 O4(활성 작업 중 shutdown 10 s 타임아웃·traceback)는 의도 여부/수정 필요 판단.
