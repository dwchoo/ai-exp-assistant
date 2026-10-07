# p27-cd70-smoke-02: C-D70 실모델 smoke 결과 (S3 manager recovery + S1 watchdog)

- 역할: test_designer(Sonnet). 2026-10-07 16:32~16:41 KST(약 9분/20분, 본 실행 5분 17초). 코드: HEAD bc131d5(candidate b7e0012 이후 docs만 추가, `src/`·`omp_bridge/` 변경 없음). **omp/18.7.0**. Python `/tmp/cw02-g1-venv/bin/python`.
- **결론 요약**
  - **S3(manager recovery): 부분 성공 + 결함 1건.** manager OMP 종료 → 재시작(제품 UI Enter) → `manager_recovery` 알림이 새 세션에 **자동 주입**되고, manager가 `workbench-recovery` skill → `workbench_status`로 복구했습니다. 그러나 **worker의 `done` 보고가 "manager 재전송"으로 새 세션에 도착하는 경로는 확인되지 않았습니다**(D1: manager가 꺼져 있는 동안 보고가 `held: target_not_connected`로 거절만 되고 Workbench에 남지 않아 `reports_resent: 0`, `reports: []`).
  - **S1(watchdog): 1차 점검까지 확인.** 중단(Escape)한 worker에게 abort 후 **61.0 s에** `status_check`(check 1/2) 알림이 들어갔고, worker가 바로 명령을 실행·보고해 Task가 정상 종료됐습니다. **2차 점검·`worker_stalled` 알림·manager 반응은 발생하지 않아 실모델로 미검증**입니다(worker가 1차 점검에 반응).
  - 요청 수: 본 실행 **22**(manager 12 + worker 10, aborted 1). 앞선 중단 실행 2회(아래) 포함 **≤26 / cap 30**(pause 28 미도달). fail-closed 카운터는 본 실행에서 스톨·오류 0회였습니다.

## 실행 이력 (3회 launch — 투명하게 기록)
| launch | 결과 | 요청 |
|---|---|---|
| 0 | 내 드라이버 결함(타이핑·대기 중 카운터를 갱신하지 않아 "2 s 이상 갱신 없음" 규칙이 오탐 → 자동화 pause/resume 1회가 입력 직전에 발생). 입력 후 약 8 s 시점에 SIGINT로 중단, shutdown 정상 | 1 기록(+진행 중 ≤1) |
| 1 | 드라이버 수정(`Ui.pump`마다 카운터 갱신). **assignment 문구 그대로** `터미널에서 …를 실행하고 결과를 알려줘`를 입력하자 **manager가 worker에 넘기지 않고 자기 `bash` 도구로 직접 실행**(2 req, `to_worker` 0). S3/S1 전제(worker 위임)가 성립하지 않아 중단·shutdown 정상 | 2 |
| 2(본 실행) | 프롬프트를 **"worker에게 터미널에서 `…` 를 실행하게 하고 결과를 알려줘"**로 바꿔 S3→S1 실행 | 22 |
- **프롬프트 변경 사유:** launch 1에서 문구 그대로는 위임되지 않음(모델 판단; manager에 `bash` 도구가 열려 있어 직접 실행이 가능). 명령·sleep 시간·scenario 구조는 그대로 두고 "worker에게 … 실행하게 하고"만 추가했습니다. 사용자 문구를 바꾼 점은 Root 판단 사항입니다.

## 방법·Isolation
- cd69/cd70-01과 같음: 새 `/tmp/wb-cd70-smoke2-*/{data,project}`(git repo)에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`, ui_v1 client, `workbench attach`를 PTY(pyte 170x40)에 붙여 pane에 입력(focus 전환 prefix+1/2/3, Escape=worker pane). 상태는 `workbench status --json` 스레드(1 s), journal(`handoffs.jsonl`) tail, 세션 jsonl로 관측. env에서 `DISPLAY`/`WAYLAND_DISPLAY`/`TMUX*`/`WORKBENCH_*` 제거. `agent.db` 내용은 열지 않았습니다.
- **카운터(fail-closed):** 메인 루프가 매 UI pump마다 두 역할의 세션 jsonl **모든 파일**(재시작으로 생긴 새 session 포함)을 다시 열거해 assistant message 수를 셉니다. 예외·감소·2 s 이상 갱신 없음이면 자동화 pause(prefix p,p)+입력 거부, 28(본 실행 cap 24=28−앞선 실행 4)에 닿으면 pause+입력 거부. 본 실행에서 발동 0회.
- `omp --version`: **omp/18.7.0**. start 결과: `omp isolation: ok (omp/18.7.0 evidence bridge_g3=18.2.10, isolation=18.6.1)`.

| 역할 | state | leaks | warnings | model / thinking | 도구(probe 기록) | skills |
|---|---|---|---|---|---|---|
| manager | ok | [] | [] | `openai-codex/gpt-6.1-sol` / high | read, bash, edit, eval, glob, grep, task, wait (+to_worker, restart_worker, workbench_status는 세션에서 확인) | to-worker, **workbench-recovery** |
| worker | ok | [] | [] | `openai-codex/gpt-6-luna` / max | read, grep, glob, edit, write, web_search, todo, task (+to_manager, terminal) | to-manager |
- start 직후 세 pane 모두 `alive=True owner=user`, `manual_prompt`.

## S3 timeline (KST 16:36:13 입력 = 0 s)
입력: `worker에게 터미널에서 \`sleep 30; echo cd70-s3-done\` 를 실행하게 하고 결과를 알려줘`

| 시각 | 경과 | 관측 |
|---|---|---|
| 16:36:13 | 0 | manager pane 입력 |
| 16:36:16.8 | 3 s | manager `read` skill://to-worker |
| 16:36:22.0 | 8.3 s | manager `to_worker`(work, analysis summary, `commands` 1개) → `dispatched`, worker TASK 도착 |
| 16:36:25 | | manager 텍스트로 turn 종료("완료 보고가 도착하면 알려드리겠습니다") |
| 16:36:28 / 32.1 | | worker `read` skill → `terminal` 실행 시작(상태줄 `owner: worker · control_wait`) |
| 16:36:37 | | **manager pane에 `/exit` 입력**(manager는 4 s 이상 쉬는 중, worker 명령 실행 5 s째) |
| 16:36:38 | +1.8 s | manager pane `exited(0) 종료됨 (Enter: 다시 시작)`, 상태줄 `backend: degraded · bridge manager=down worker=ok` |
| 16:37:02 | | 명령 종료(exit 0, 30.09 s), `terminal_notice not_needed` |
| **16:37:06.3** | | worker `to_manager done` → 결과 **`{"status":"held","reason":"target_not_connected"}`**(큐/journal 기록 없음). 상태줄 `실행 중 · done_report_pending` |
| 16:37:13.7 | | 제품 UI에서 manager pane Enter(C-D62 재시작) |
| **16:37:13.97** | +0.3 s | journal `workbench_notice manager_recovery` queued → **sent**(manager turn 중 아니므로 즉시) |
| 16:37:14 | | 상태줄 `backend: ready · bridge manager=ok`. 새 session `…85bb…` 등록 |
| 16:37:14.0 | | 새 manager 입력으로 notice JSON 주입: task(`held_reason: done_report_pending`), worker(busy), terminal(last: exited 0), `reports_resent: 0`, `reports_unknown: 0`, instruction("workbench-recovery skill, workbench_status…, 사용자에게 간단히 설명") |
| 16:37:17.5 | | manager `read` skill://workbench-recovery |
| 16:37:21.1 | | manager **`workbench_status`**(task_id 지정): task running/held done_report_pending, `commands_run`(exit 0, 30.092 s, log_path), worker busy·idle, terminal last, **`reports: []`**, watchdog `checks_sent 0` |
| 16:37:25 | | manager `read` terminal 로그 파일(`1:cd70-s3-done`) |
| 16:37:29 | | manager `read` skill://to-worker |
| 16:37:33.4 | | manager `to_worker{cancel:true}`("로그로 완료 확인, 재실행·추가 명령 없이 종료하세요") → Task **취소됨**(`종료(취소됨)`), worker에 취소 알림 |
| 16:37:38.6 | **25 s**(Enter 기준) | manager가 사용자에게 설명(156자, 한국어): "세션 복구 후 실행 기록과 로그를 확인했습니다. … 한 번 실행되어 완료됐으며 재실행 없이 남아 있던 작업을 종료했습니다. stdout `cd70-s3-done`, 종료 코드 0, 실행 시간 30.092초" |
| 16:37:42.9 | | worker가 취소 질문에 `to_manager answer` → `rejected: no_active_task`(해로움 없음), 16:37:46 텍스트 종료 |

상태줄(제품 UI):
```
[16:36:38] focus: MANAGER OMP | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active
           작업: 작업 "터미널에서 지정된 명령을 그대로 한 번 …" [실행 중] | backend: degraded | bridge manager=down worker=ok
[16:37:06] … [실행 중 · done_report_pending] | backend: degraded | bridge manager=down worker=ok
[16:37:12] pane 제목: `MANAGER OMP *FOCUS* exited(0) 종료됨 (Enter: 다시 시작)`
[16:37:14] … [실행 중 · done_report_pending] | backend: ready | bridge manager=ok worker=ok
[16:37:33] focus: MANAGER OMP | … | worker: 대기 | 자동화: idle | 작업 … [종료(취소됨) · 취소요청됨]
```
- manager 재시작 알림 자체는 사용자 화면에 별도 표시가 없고(`backend`가 degraded→ready로만 보임) 설명은 manager의 텍스트 답변이 맡았습니다.
- S3 후속 host 명령 `echo CD70_S3_FOLLOWUP_$((6*7))` → `CD70_S3_FOLLOWUP_42`, **프롬프트 `$ ` 정상 표시, overprint 없음**(2 s·5 s 프레임 동일).

### S3 검증 항목
| 항목 | 결과 |
|---|---|
| manager OMP 종료 중 worker 명령 계속 | 계속(exit 0, 30.09 s) |
| manager 종료 중 worker done | **`held: target_not_connected`** — 전달 대기 큐에 남지 않음(D1) |
| 재시작 후 `manager_recovery` 알림 자동 주입 | **동작**(+0.3 s, 같은 turn 아님·즉시 sent) |
| 알림 내용 | task/worker/terminal 요약, `reports_resent`/`reports_unknown`, 지시 포함 |
| 끊긴 보고의 재전송 | **미확인**(`reports_resent 0`, 새 세션에 `kind: report` 메시지 없음) |
| `workbench_status` | 호출 1회, 필요한 상태 반환(commands_run, 로그 경로). 단 worker의 보고 문장은 없음 |
| manager의 사용자 설명 | 있음(정직하게 "세션 복구", "재실행 없이 종료") |
| 사용자 입력 → 재실행 여부 | 명령 1회 실행(재실행 없음) |

## S1 timeline (KST 16:38:32 입력 = 0 s)
입력: `worker에게 터미널에서 \`sleep 20; echo cd70-s1-done\` 를 실행하게 하고 결과를 알려줘` (같은 Workbench·같은 새 manager 세션, S3 Task는 취소 상태)

| 시각 | 경과 | 관측 |
|---|---|---|
| 16:38:32.5 | 0 | 입력 |
| 16:38:37.1 | 4.6 s | manager `to_worker`(work, summary, commands 1개) → `dispatched`, worker TASK 도착(16:38:37.2) |
| **16:38:39.0** | 6.5 s | worker pane에 **Escape**(driver: worker busy 감지 직후, prefix+2 → Esc). worker 첫 turn **aborted**(skill read 전) |
| 16:38:39.0 | | journal outbox `unknown / assistant_message_ended_with_error_or_abort`(TASK 전달 결과 불명. 재전송·알림 없음, 설계 범위 밖) |
| 16:38:43 | | driver가 2차 Escape 전송(worker가 idle인데 Task 상태가 `busy`여서 "놓쳤다"고 오판; 이미 aborted 상태라 효과 없음 — 아래 비고) |
| 16:38:39~16:39:40 | 60 s | worker·terminal 모두 idle, 상태줄 변화 없음(`worker: 작업 중 · 자동화: active`), 요청 0 |
| **16:39:40.04** | **+61.0 s**(abort 기준) | journal `workbench_notice status_check sent`(worker, `idle_seconds 60`, `check 1/max_checks 2`, terminal 요약 포함) |
| 16:39:43.8 | +3.8 s | worker `terminal`(`sleep 20; echo cd70-s1-done`) 실행 → journal `watchdog_reset{checks:1, stalled:false, tool:terminal}` |
| 16:40:03.9 | | 명령 종료(exit 0, 20.09 s) |
| 16:40:05.7 | | worker `to_manager done` → queued → delivered(16:40:08.6 `omp_processed`), 상태줄 `종료(완료)` |
| 16:40:08.6 | 96 s | manager 최종 답변(107자): "worker가 정상 완료했습니다. stdout `cd70-s1-done`, 종료 코드 0, 실행 시간 20.089초" |
- **2차 점검·`worker_stalled`·manager 반응: 미발생**. 1차 점검에 worker가 정상 반응(명령 실행→보고)해 카운터가 리셋됐습니다. 정지 시나리오는 worker가 점검을 한 번 더 무시해야 재현됩니다.
- 사용자 화면에는 watchdog 대기·`status_check` 발송 표시가 **없었습니다**(상태줄은 그대로 `작업 중`). C-D70에 표시 요구는 없어 결함으로 분류하지 않고 관측으로만 남깁니다.
- S1 후속 `echo CD70_S1_FOLLOWUP_$((6*7))` → `CD70_S1_FOLLOWUP_42`, 프롬프트 `$ ` 표시, overprint 없음.
- 비고(driver): Escape는 총 2번 보냈으나 1번째에 이미 abort됨(`stopReason aborted` 1건). 2번째는 이미 쉬는 worker pane에 간 것으로 효과가 보이지 않습니다("재시도 1회 이하" 취지 위반은 아니나 불필요한 입력이었음).

## Provider request (세션 jsonl assistant message, 본 실행)
| 세션 | req | tool calls |
|---|---|---|
| manager #1(S3에서 종료) | 3 | read 1, to_worker 1 (+텍스트) |
| manager #2(재시작 후, S3 복구 + S1) | 9 | read 3(workbench-recovery, 로그, to-worker), workbench_status 1, to_worker 2(cancel, S1 위임) (+텍스트 3) |
| worker(1개 세션, S3+S1) | 10(**aborted 1**) | read 1, terminal 2, to_manager 3 (+텍스트) |
| **본 실행 합계** | **22** | error 0, aborted 1(내가 넣은 Escape) |
- 앞선 중단 launch 0·1: 1(+진행 중 ≤1)·2. **전체 ≤26 / cap 30(pause 28)**.

## 동작한 것
- manager OMP 종료·재시작(C-D62 Enter)이 활성 Task 중에도 정상 동작. 새 manager 세션에 **`manager_recovery` 알림이 자동 주입**되고 `workbench-recovery` skill → `workbench_status` → 판단(취소, 사실 확인 후 재실행 없이 종료) 순서를 따랐으며 사용자에게 설명했습니다.
- worker host 명령은 manager 종료·재시작과 무관하게 끝까지 실행(재실행 없음).
- watchdog 1차 `status_check`가 abort된 worker에게 정확히 60 s 대기 후(61.0 s) 1회 들어가 worker를 재가동했고, terminal 실행으로 카운터가 리셋됐습니다.
- 18.7.0 격리 ok(leaks 0/warn 0), 새 도구·skill 노출은 manager 전용, worker에 `restart_worker`·`workbench_status`·`workbench-recovery` 없음.
- 종료: `shutdown --yes --json` exit 0, `verified: true`(host_shell·manager_omp·worker_omp·supervisor dead, survivors 0, 강제 kill 없음).

## 결함·관찰 (제품 결함 vs 모델 행동)
- **D1 (제품 결함 후보, 중간): manager가 꺼진 동안의 worker `done` 보고가 보존·재전송되지 않음.**
  - 증거: worker `to_manager` 결과 `{"status":"held","reason":"target_not_connected"}`(16:37:06.29, 세션 jsonl). journal에 해당 보고의 outbox 항목 없음(`kind:"report"` 없음, S3 구간 outbox는 task/question만). `manager_recovery` 알림 `reports_resent: 0, reports_unknown: 0`, `workbench_status.reports: []`. 새 세션에 `kind: report` 메시지 주입 없음. 코드: `flow.py` `_queue`가 peer 없음이면 `held("target_not_connected")`를 반환하고 큐에 넣지 않음(`flow.py:1033-1036`). 재전송은 `_manager_new_session`의 `requeue`(큐에 있던 항목) 경로뿐(`flow_recovery.py:~357`).
  - 영향: C-D70 (4)의 "이전 세션에 보내지 못하고 버려진 worker 보고는 새 manager 세션으로 다시 보낸다"가 **manager OMP가 완전히 종료된(bridge 미연결) 경우에는 적용되지 않습니다**. 이 시나리오의 `done` 문장은 사라졌고 manager는 `commands_run`·로그로 복구했습니다. 보고 본문에 해석·요약이 들어 있던 작업이면 정보가 유실됩니다. 상태줄에는 `done_report_pending`이 계속 표시되어 사용자는 보고 대기를 볼 수 있으나 해소 경로가 manager 판단·취소뿐입니다.
  - 해석 여지: 설계가 "터미널에서 종료된 manager는 `held`로 worker가 다시 보내게 한다"일 수 있으나, worker가 다시 보내지 않았고(텍스트로만 종료) 알림 문구에도 재시도 안내가 없습니다. Root 판단 필요.
- **O1 (모델 행동, 낮음): manager가 사용자 문구 그대로면 직접 실행.** launch 1에서 `터미널에서 …를 실행하고` → manager `bash`로 직접 실행(worker 미사용). cd69 smoke-03에서는 같은 형태가 위임됐으므로 모델·턴별 변동일 수 있습니다. 실사용에서도 "worker에게"를 명시하지 않으면 manager가 자체 `bash`로 처리할 수 있습니다(C-D66 위임 규칙과의 관계는 Root 판단).
- **O2 (모델 행동, 낮음):** S3 복구 후 manager가 Task를 **취소**(`cancel: true`)해 종료했습니다. 명령은 이미 완료·로그 확인됐으므로 합리적이나 Task 상태가 `취소됨`으로 남습니다(worker 보고를 받을 수 없어 `done`으로 닫지 못함). D1의 결과입니다.
- **O3 (기존, 낮음, cd69 O3 계열):** 첫 Task 전달 outbox가 `unknown / BridgeTimeout`(20 s)로 기록됨(16:36:42). worker는 이미 TASK를 처리 중이었고 알림은 발생하지 않았습니다. S1은 abort로 인한 `unknown`(알림·재전송 없음).
- **O4 (표시, 낮음):** watchdog 대기·`status_check` 발송이 사용자 화면에 보이지 않습니다. manager 재시작 복구도 상태줄에는 `backend` 변화로만 보입니다.
- **O5 (모델 행동, 낮음):** S3에서 worker가 취소 질문에 `to_manager answer`를 보내 `rejected: no_active_task`(Task가 이미 닫힘). cd68/cd69/cd70-01의 중복 보고와 같은 종류, 영향 없음.
- **host pane 표시(사용자 관찰 항목):** 두 시나리오 후속 명령에서 **overprint·프롬프트 누락 모두 재현되지 않았습니다**(`$ ` 정상, 2 s/5 s 프레임 동일). cd69 O1·cd70-01 O3는 이번엔 보이지 않았지만 pyte 한계 가능성은 그대로입니다.
- **미검증:** watchdog 2차 점검·`worker_stalled`·manager 반응(worker가 1차 점검에 반응해 미발생). 재현하려면 `status_check` 직후 worker pane에 다시 Escape가 필요하며 추가 요청 약 6~8건이 듭니다(이번 cap 잔여 약 2~4건이라 실행하지 않음).

## 도구 결함 (내 쪽)
- launch 0: `Ui.send/pump` 도중 카운터를 갱신하지 않아 2 s 규칙 오탐 → 자동화 pause/resume 1회(요청 전·입력 직후, 영향 요청 0). launch 1에서 `Ui.pump`마다 카운터를 갱신하도록 고쳤습니다. 제품 결함 아님.
- S1 2차 Escape: Task 상태 `busy`를 worker turn 진행 중으로 오인(정정: Task는 열려 있어도 worker OMP는 idle일 수 있음).

## Residue
- 3회 launch 모두 `shutdown --yes --json` exit 0(launch 0·1·본 실행), 본 실행 `verified: true`, 소유 프로세스 잔여 0(`processes_mentioning` 기준, kill 불필요), project `git status` 깨끗. `/tmp/wb-cd70-smoke2-*` 3개 모두 삭제 확인.
- 파일: run dir에는 이 파일과 `-timeline.json`만 추가. scratch(driver·세션 사본·화면 캡처)는 Claude scratchpad에만 있음. `agent.db` 내용·credential·token·타 프로세스 environ은 읽거나 출력하지 않았습니다(세션 jsonl의 `credential_pin` 레코드는 옮기지 않음). `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex` 미접촉. commit·graphify update·코드 수정 없음.

## 권고 (Root 판단용)
1. **D1을 CW 후속 결함으로 판단**: manager 종료 중 worker 보고를 `held`로만 돌려주지 말고 큐에 남겨 새 manager 세션에 `reports_resent`로 보내거나, worker에 재전송 안내를 넣는 방안. 수정 시 단위 테스트(peer 없음 + 새 세션 recovery)와 이 시나리오 재smoke 필요.
2. watchdog 정지 경로(2차 점검·`worker_stalled`·manager 반응)는 단위/독립 테스트 근거에 의존. 필요하면 worker pane Escape를 `status_check` 직후 한 번 더 넣는 smoke를 별도 승인으로 진행 가능.
3. smoke 프롬프트는 "worker에게 … 실행하게 하고" 형태로 고정하는 것을 권장(이번 launch 1 관찰).
