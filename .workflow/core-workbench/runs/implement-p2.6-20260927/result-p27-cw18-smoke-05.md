# p27-cw18-smoke-05 — CW-18 실모델 experiment-flow smoke 결과 (G1~G3 수정 후)

- 역할: test_designer (Opus) · 실행: 2026-10-05 14:01:37 ~ 14:04:10 KST (약 2.6분, 1회 실행)
- 결과: **시나리오 1·2 모두 끝까지 동작함.** execute 결정 수용 → host run → 60 s review → 실행 종료·cwd 복원 →
  worker analysis 수용(`judgment: success`) → 결과 REPORT가 manager에 전달되어 `omp_processed` → Task `finished`, worker `idle`, 자동화 `idle`.
  2단계 worker_busy도 확인함. 거부된 worker 응답이 없어 시나리오 3(거부 시 중단)은 발생하지 않음.
- provider request: **12/25**(manager 7, worker 5, error/aborted 0). wall-clock 약 2.6분/20분. cap 미도달.
- 진입점: `python -m workbench start --data-dir /tmp/wb-cw18-smoke-42o8dr5k/data --omp ~/.local/bin/omp --no-attach`
  (project dir에서 실행, 실제 HOME, Workbench-owned OMP home, `agent.db` symlink 확인, store는 열지 않음).
  product UI(`workbench attach`)를 PTY(pyte)에 붙여 manager pane에 타이핑했고, 상태는 `workbench status --json`으로 관측함.
- 코드 상태: HEAD 856fb08 + 미커밋 작업 트리(C-D67, G1 bridge 생성 `response_id`, G2 `run_judgment_unavailable`, G3 거부 사유 기록, f05).
  두 OMP 모두 `openai-codex` / GPT-5.5, omp/18.4.5, isolation `ok`(leaks/warnings 0).
- 드라이버: smoke-02b `smoke2b.py`를 고친 scratch `smoke5.py`(p27w `Ui`/`processes_mentioning`/`kill_exact` 재사용, 0.5 s 간격 cap watcher, 25 req/20분).
  request 수는 temp Workbench home 세션 jsonl의 assistant message 수. 종료 후 temp DB(read-only), run.json, 세션 jsonl에서
  **구조(content type, 길이, 상태, 필드 이름)만** 추출함. 모델 텍스트는 옮기지 않음.
- temp project: git repo(`exp.sh`: `EXP_START` 출력, `sleep 70`, `out/result.txt`에 RESULT=ok, 출력 RESULT=ok; `out/.keep`, `work/.keep`).
- 짧은 로그: `result-p27-cw18-smoke-05-timeline.json`(secret 없음)

## Timeline (KST, DB/세션 시각은 UTC+9로 환산)

| 시각 | req | 관측 |
|---|---|---|
| 14:01:38 | 0 | start exit 0, phase ready, isolation ok, providers manager/worker=`openai-codex`, `agent.db` symlink |
| 14:01:39 | 0 | product UI attach, `worker: 대기 · 자동화: idle` |
| 14:01:41 | 0 | 1단계 지시 입력(experiment, `./exp.sh`, bash, environment none, criteria log_contains/result_file/result_contains) |
| 14:01:44.6 | 1 | manager `read`(skill, thinking 42자 동반) |
| 14:01:49.8 | 2 | manager native `to_worker` → `dispatched`. `scope_approved`/`proceed`(actor `user_standing_delegation`, C-D66), run `started`, execute TASK delivery `attempted`→`api_returned` |
| 14:01:52 | 3 | manager 한 줄 보고(text) |
| 14:01:55 | 3 | 2단계 지시 입력(실행 중 다른 to_worker 한 번만 시도) |
| 14:01:57.33 | 4 | **worker execute 응답 = [`thinking`(42자), `text`(marker, `decision:"execute"`)] → 수용**. delivery `omp_processed`(`provider_request_matched:true`) |
| 14:01:57.41~.51 | — | shell_event `sent` → `accepted` → `started`(cwd = execution worktree). 상태: owner `manager`, `control_wait`, 자동화 `active` |
| 14:01:59.7 | 5 | **2단계: manager 두 번째 `to_worker`(task_id 없음) → `status: worker_busy`**(현재 Task `running` 동반). 14:02:01.8 manager가 사용자에게 알리고 turn 종료(req 6). `work/second.txt` 생성 안 됨 |
| 14:02:37 | 6 | 상태줄 `[실행 중] \| 60s 대조 20s 후` |
| 14:02:58.67 | 6 | **60 s periodic review** QUESTION(stage `periodic_review`) delivery(run 시작 후 약 61 s) |
| 14:03:02.8 | 7 | worker review: `thinking`(42자)+`toolCall:read` |
| 14:03:07.52 | 7 | shell_event `ended`(exit 0, `exit_confirmed`, raw log 22 B, result 10 B 수집). `source_status_before/after` 모두 빈 값 |
| 14:03:07 | 7 | Task `waiting_report`, held `worker_busy`(review turn 진행 중), shell owner `user`, `manual_prompt`. host pane에 `builtin cd -- <project>` 입력됨 → **cwd 복원** |
| 14:03:12.5 | 8 | worker review: `thinking`(88자)+`toolCall:to_manager` → `queued`. ANSWER(worker→manager, review QUESTION에 대한 in_reply_to) 생성 |
| 14:03:14.57 | 8 | review delivery `omp_processed`, backend log `periodic review … -> omp_processed`, review_count 1. 자동화 `held` |
| 14:03:21.7 | 9 | worker review turn 종료(`text` 25자) |
| 14:03:22.08 | 9 | **analysis QUESTION**(stage `analysis`) delivery — worker turn이 끝난 뒤(약 0.3 s 후)에 보냄. held `worker_busy` 해제 |
| 14:03:23.1 | 10 | manager가 review ANSWER에 응답(`thinking`(39자)+`text`), ANSWER delivery `omp_processed` |
| 14:03:27.56 | 11 | **worker analysis 응답 = [`text`(429자, marker `decision:"success"`)] → 수용**. run.json `worker_analysis_response`의 `response_id`는 bridge가 생성(모델 marker에는 `response_id` 없음) |
| 14:03:27.57 | 11 | run_event `completed`(`judgment: success`), **결과 REPORT(worker→manager) delivery**. Task `finished`, worker `idle`, 자동화 `idle` |
| 14:03:30.7 | 12 | manager가 REPORT 처리(`text`), REPORT delivery `omp_processed`. Task `last_result = {judgment: success, outcome: reported, report: omp_processed, run_closed: true}` |
| 14:03:30~14:04:06 | 12 | 30 s 이상 추가 request 없음(안정) |
| 14:04:06 | 12 | `shutdown --yes --json` exit 0, `verified:true`, 세 pane과 supervisor dead, survivors 없음 |
| 14:04:10 | 12 | 소유 프로세스 잔여 0, temp root 삭제, project `git status` 깨끗함 |

### Provider request 수
| OMP | request | content 형태(텍스트 제외) |
|---|---|---|
| manager | 7 | ① `thinking`(42)+`toolCall:read` ② `toolCall:to_worker`(dispatched) ③ `text` ④ `thinking`(47)+`toolCall:to_worker`(worker_busy) ⑤ `text` ⑥ `thinking`(39)+`text`(review ANSWER 응답) ⑦ `text`(REPORT 응답) |
| worker | 5 | ① `thinking`(42)+`text`(execute marker, **수용**) ② `thinking`(42)+`toolCall:read`(review) ③ `thinking`(88)+`toolCall:to_manager`(review) ④ `text`(25, review 종료) ⑤ `text`(analysis marker, **수용**) |
| 합계 | **12** | error/aborted 0, 모든 delivery `provider_request_matched:true` |

## 동작한 것
- 진입점, isolation, 공유 login, 실모델 호출, 확인된 shutdown, residue 0.
- **C-D67:** execute 응답 앞의 보이는 thinking(42자)을 무시하고 marker로 수용함.
- **G1(response_id bridge 생성):** analysis와 execute 모두 run record의 `response_id`가 bridge 생성 값이며, 모델 marker에는 `response_id`가 없음. smoke-04의 거부 원인이 사라짐.
- **f05(busy period):** run 종료 시점에 worker가 review turn 중이어서 Task가 `waiting_report`/held `worker_busy`로 약 15 s 기다렸고,
  worker turn이 끝난 직후 analysis QUESTION을 보냄. 겹친 turn이나 거부 없음.
- **judge success와 manager 보고:** worker analysis 수용 → run `completed`(`judgment: success`, criteria 3개, evidence_errors 0, unknowns 0) →
  REPORT가 manager에 `omp_processed` → Task `finished`(active false), worker `idle`, 자동화 `idle`. 상태줄 `[보고 완료]`.
- **host run:** shell idle 확인 후 Workbench가 execution worktree에서 `./exp.sh`를 실행, 실행 중 owner `manager`/`control_wait`,
  종료 후 exit 0 확인, raw log·result 수집, shell을 user에게 돌려주고 **cwd를 project로 복원**(`phase: control_returned`, host pane에 `builtin cd -- <project>`).
- **60 s worker review** 1회(약 61 s 시점) `omp_processed`, 상태줄 카운트다운 표시.
- **2단계 worker_busy:** task_id 없는 두 번째 `to_worker`가 `worker_busy`로 거절되었고 manager는 재시도 없이 사용자에게 알림(추가 request 2회). 두 번째 Task·파일 생성 없음.
- source repo 변경 없음(`source_status_before/after` 빈 값, 최종 `git status` 깨끗함).

## 동작하지 않은 것 / 관찰 / 분류
- 차단 결함 없음. 거부된 worker 응답 0건이므로 G2(`run_judgment_unavailable` notice)와 G3(거부 사유 기록) 경로는 이번 실행에서 **실모델로는 미검증**(발동 조건 없음).
- 관찰 H1 (모델 행동, 낮음): periodic review에서 worker가 `to_manager`로 ANSWER를 보냄. review 지시는 "manager가 조치해야 할 때만 to_manager"인데
  run은 정상 진행 중이었음. 그 결과 manager request 1회와 worker request 1회가 추가됨(12회 중 2회). 제품은 이를 review QUESTION에 대한 ANSWER로 기록·전달했고 흐름은 깨지지 않음.
- 관찰 H2 (product 표시, 낮음, 결함 여부 미판정): 실행이 끝나고 shell이 user에게 돌아간 뒤에도 `host_shell.shell.held_reasons`가
  `["takeover_requested","user_owner","outstanding_request"]`로 남음(`takeover_confirmed:true`, `automation_hold: null`, 상태줄에는 보류 표시 없음).
  잔여 표시용 필드로 보이며 동작에는 영향이 없었음.
- 관찰 H3 (기존 polish와 같음): worker analysis 프롬프트의 출력 규칙에 `no_thinking:true`가 남아 있음(carried_forward의 "no_thinking contract text now prompt-only"). 이번 모델은 analysis에서 thinking 없이 응답함.
- 관찰: 실행 시작 직후 몇 초간 상태줄은 `[시작 중]`·자동화 `idle`(worker execute 결정 전), 결정 후 `[실행 중]`·`active`로 바뀜. 정상.

## 상태줄 text screenshot (product UI 상단 2줄)

```
[attach 14:01:39]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 14:01:39 (0s 전) | Ctrl-] ? 도움말

[dispatch 직후 14:01:51]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the provided suc…" [시작 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 14:01:51 (0s 전) | Ctrl-] ? 도움말

[실행 중 14:02:37]
focus: MANAGER OMP | host 입력 owner: manager | shell mode: control_wait | worker: 작업 중 | 자동화: active
작업: 실험 "Run ./exp.sh and judge the provided suc…" [실행 중] | 60s 대조 20s 후 | backend: ready | bridge manager=ok worker=ok | 마지막 확인 14:02:37 (0s 전) | Ctrl-] ?

[완료 후 14:04:02, host focus 14:04:03도 같음]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the provided suc…" [보고 완료] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 14:04:02 (0s 전) | Ctrl-] ? 도움말
```

host pane(완료 후, 경로는 `<root>`로 줄임):
```
$ cd <root>/data/workflow/worktrees/<task>-r1-3f5a731d && pwd -P > <root>/data/workflow/runs/<run>/parent-cwd.txt
$ wb-handoff
EXP_START
RESULT=ok
$  builtin cd -- <root>/project
$
```

## Residue
- 프로세스: `shutdown --yes` exit 0, `verified:true`. 소유 프로세스 잔여 0(강제 kill 없음).
- 파일: `/tmp/wb-cw18-smoke-42o8dr5k` 삭제 확인(worktree 포함). scratch의 세션 사본, DB, screen, driver는 삭제함.
  run dir에는 이 파일과 `result-p27-cw18-smoke-05-timeline.json`만 남김.
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 프로세스는 건드리지 않음. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않음.
  commit 없음, graphify update 없음, ledger는 수정하지 않음.

## 권고 (Root 판단용)
1. 남은 시나리오 1·2 항목(execute, host run, 60 s review, judge success, manager 보고, cwd 복원, worker_busy)은 실모델로 확인됨. CW-18 smoke 목표 달성으로 볼 수 있음.
2. G2/G3 거부 경로는 실모델에서 재현되지 않았으므로 기존 결정론 테스트(stub port) 근거로 판단할 것.
3. H1(review 중 불필요한 `to_manager`)은 review 지시 문구 보강 후보, H2(`held_reasons` 잔여)는 polish 후보.
