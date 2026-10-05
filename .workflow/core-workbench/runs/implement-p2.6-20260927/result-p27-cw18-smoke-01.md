# p27-cw18-smoke-01 — CW-18 실모델 end-to-end smoke 결과

- 역할: test_designer (Opus) · 실행: 2026-10-03 23:59:26 ~ 2026-10-04 00:02:47 KST (201 s)
- 결과: **request cap(30)으로 1단계 도중 중단.** 2단계(experiment)와 3단계(worker_busy)는 실행하지 못함.
- 진입점: `python -m workbench start --data-dir /tmp/wb-cw18-smoke-9atfd6gh/data --omp ~/.local/bin/omp --no-attach`
  (실제 HOME 사용 → Workbench-owned OMP home, `agent.db` symlink. store는 열거나 읽지 않음). product UI(`workbench attach`)를
  PTY(pyte 170x40)에 붙여 manager pane에 입력하고, 상태는 `workbench status --json`으로 관측함.
- 실제 provider: 두 OMP 모두 `openai-codex`, 모델 GPT-5.5(OMP status bar 기준. Workbench home에 model 설정이 없어 OMP 기본값이 쓰임).
- 드라이버: scratchpad `smoke.py`(삭제 대상 scratch). p27w의 `Ui`, `processes_mentioning`, `kill_exact`를 재사용함.
  request 수는 Workbench home 세션 jsonl의 assistant message 수로 셌다(우리 temp dir 안의 파일만 읽음).
- 짧은 로그: `result-p27-cw18-smoke-01-timeline.json`(secret 없음, 세션 jsonl 사본은 삭제함)

## Timeline (KST)

| 시각 | t(s) | 관측 |
|---|---|---|
| 23:59:28 | 1.6 | start exit 0, phase ready, isolation `ok`(leaks/warnings 0), providers manager/worker=`openai-codex`, `agent.db`가 symlink임 |
| 23:59:29 | 2.6 | product UI attach, `worker: 대기 · 자동화: idle` |
| 23:59:31 | 4.7 | manager pane에 1단계 지시 입력(영문, hello.txt work Task) |
| 23:59:31~00:02:13 | — | manager가 `to_worker`를 **18회 rejected**받음(아래 D1/D2). OMP loop detector 개입 1회. 이후 `eval` tool로 `tool.to_worker(...)`를 호출함 |
| 00:02:13 | 167 | 20번째 manager request에서 `dispatched`(eval 경유). Task work/running, `worker: 작업 중`, `자동화: active` |
| ~00:02:20 | — | worker: skill read → `write work/hello.txt` → read로 확인 → `to_manager done` (queued). 내용 `hello from worker\n` |
| 00:02:29 | 183 | Task `held_reason=done_report_pending`(manager가 자기 turn을 끝내지 않아 report를 받지 못함, D3) |
| 00:02:13~00:02:39 | — | manager: `wait`("No running background jobs") → `read history://` → 파일 직접 확인 → `eval`로 follow-up(`task_id` 포함, queued) → `wait` 반복. worker는 follow-up QUESTION에 `done`을 다시 보냄 |
| 00:02:39 | 192 | **cap 도달(30)** → UI에서 prefix p,p로 pause. interruption `confirmed`, manager_ack `abort_requested`, worker_ack `paused` |
| 00:02:43 | 196 | 최종 request 33(manager 26, 그중 aborted 1 / worker 7). `shutdown --yes` exit 0, `verified: true`, 세 pane 모두 dead, survivors 없음 |
| 00:02:47 | 201 | 소유 프로세스 잔여 0, temp root 삭제 확인 |

### Provider request 수

| OMP | assistant request | 비고 |
|---|---|---|
| manager | 26 (aborted 1 포함) | rejected `to_worker` 18, `eval` 3, skill/history/file `read` 3, `wait` 2 |
| worker | 7 | skill read, write, read, to_manager, 종료 텍스트, follow-up에 대한 to_manager, 종료 텍스트 |
| 합계 | 33 | cap 30 초과분 3은 cap 감지(약 1 s 폴링)와 pause 사이에 이미 진행 중이던 요청 |

## 동작한 것

- 실제 진입점 시작, isolation `ok`, Workbench home의 `agent.db` symlink를 통한 공유 login으로 두 OMP가 실모델을 호출함.
- `to_worker`(work) → `dispatched` → TASK가 worker에 전달됨 → worker가 자기 write tool로 허용된 경로 안에서 작업함
  → `to_manager done` → `queued`. 파일 `work/hello.txt` = `hello from worker\n`(git status `?? work/hello.txt`, 다른 변경 없음).
- 잘못된 인자는 backend가 거부함(`invalid_arguments`, `unknown_task`, 절대경로는 `invalid_paths`). 아무것도 dispatch되지 않음.
- follow-up(`task_id` 지정) → `queued` → worker에 `question`으로 전달되고 worker가 응답함.
- 상태줄 반영: worker 대기→작업 중, 작업 요약과 `[실행 중 · done_report_pending]`, 자동화 idle→active→(pause 후) paused.
- pause는 실행 중인 manager turn을 abort하고 양쪽 ack 후 `confirmed`가 됨. 확인된 shutdown 후 residue 0.

## 동작하지 않은 것 / 분류

- **D1 (product defect, 높음): `to_worker`/`to_manager`의 optional 필드를 모델이 빠짐없이 채워 보냄.**
  모든 native 호출에 `task_id`(가짜 UUID), `run:false`, `cancel:false`, 그리고 work Task인데도 `spec.execution`
  (`source:"unused"`, `command:"true"` …)이 들어 있었음 → `spec.execution: only an experiment Task has an execution` /
  `unknown_task`. worker의 `to_manager`에도 `requires_code_change`, `reason`, `request:{"goal":"none","paths":[]}`가
  항상 채워졌고 backend는 이를 받아들임(manager가 보는 report에 가짜 scope request와 `requires_code_change:true`가 섞임).
  bridge schema(`omp_bridge/g3/bridge.ts` TO_WORKER/TO_MANAGER_PARAMETERS)에서는 해당 필드가 optional이므로,
  openai-codex 경로가 strict function schema로 모든 property를 required로 바꾸는 것으로 **추정**함(OMP 내부는 확인하지 않음).
  `eval` 안에서 필드를 생략하고 직접 호출하자 바로 `dispatched`됨. 이 provider에서는 manager가 native tool로 Task를 만들 수 없는 셈임.
- **D2 (skill/계약 gap, 중간):** `spec.paths`에 절대경로를 주면 `invalid_paths`가 됨. to-worker skill에는 repo 상대경로라는 말이 없음.
- **D3 (model behaviour + skill 문구, 중간):** manager가 "Wait for the report"를 OMP `wait` tool 호출로 해석해 turn을 끝내지 않았음.
  report 전달은 manager가 idle일 때만 가능하므로 `done_report_pending`이 계속 유지되고, 그동안 요청만 소모됨.
  설계대로 동작한 것(전달 보류)이지만, skill에 "turn을 끝내라, report는 새 메시지로 온다"는 안내가 없음.
- **모델 행동:** 같은 rejected 호출을 반복함(loop detector가 개입하기 전 5회 연속 동일). `eval`로 tool을 우회 호출함.
  worker가 이미 한 일에 follow-up을 보냄.
- **미검증(cap):** experiment 경로(host shell idle 확인, Workbench가 run 입력, 60 s worker review, judge/report,
  cwd 복원), worker_busy, Task close(done) 후 worker idle, manager가 report를 받는 것.
- 관측 한계: pyte가 DEC line-drawing을 `lqqk/x`로 그림(렌더러 문제이지 product 문제가 아님).

## 상태줄 text screenshot (product UI 상단 2줄)

```
[attach 직후 23:59:29]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 23:59:29 (0s 전) | Ctrl-] ? 도움말

[dispatch 직후 00:02:14]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
작업: 작업 "Create work/hello.txt containing exactl…" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 00:02:14 (0s 전) | Ctrl-] ? 도움말

[cap 직전 00:02:39]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
작업: 작업 "Create work/hello.txt containing exactl…" [실행 중 · done_report_pending] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 00:02:39 (0s 전) | Ctrl

[OMP pane footer] manager: ⠼ 3m > ◒ GPT-5.5 > … > S0.54 ▶─6%─┃272K   worker: "⣷ To manager └─ kind="done" …"
```

## Residue

- 프로세스: 이 smoke가 띄운 backend/OMP/host shell 잔여 0(cmdline/cwd 기준 확인, `verified: true` shutdown). 강제 kill 없음.
- 파일: `/tmp/wb-cw18-smoke-9atfd6gh` 삭제됨. run dir에는 이 결과 파일과 timeline json만 남김. 세션 사본은 삭제함.
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 프로세스는 건드리지 않음. credential 내용이나 다른 프로세스의 environ은 읽지 않음.
  token은 출력하지 않음. `~/.omp`에는 C-D64에 문서화된 natives mtime touch와 공유 `agent.db`(SQLite wal/shm, OAuth 사용)
  외에 기대하는 변경이 없음(이번에는 inventory를 비교하지 않음).
- commit 없음, graphify update 없음.

## 권고 (Root 판단용)

1. D1이 최우선: openai-codex(strict) 경로에서 optional 필드가 강제로 채워짐. 선택지는 (a) schema를 nullable/strict 호환으로
   바꾸고 backend가 빈 값(null/"")을 생략으로 취급, (b) 문서에 provider 제약을 명시. 결정은 Root/사용자 몫.
2. D2/D3은 skill 문구 보강(상대경로, "report를 기다릴 때는 turn을 끝낸다", `wait` tool 금지).
3. D1 수정 후 같은 시나리오를 다시 실행할 것(cap 30이면 2단계까지 충분할 것으로 예상).
