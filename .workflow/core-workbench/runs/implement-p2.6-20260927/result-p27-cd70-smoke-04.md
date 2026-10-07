# p27-cd70-smoke-04: C-D70 UI polish 이후 정상 흐름 실모델 회귀 smoke (R1 하드웨어 스펙 / R2 150 s 장기 명령)

- 역할: test_designer(Sonnet). 2026-10-07 18:07:13~18:13:19 KST(6분 6초 / 20분). 코드: repo HEAD 3377b95(candidate e6a73a1, 수정 없음). **omp/18.7.0**. Python `/tmp/cw02-g1-venv/bin/python`.
- **결론: 정상 흐름 회귀 없음, 새 제품 결함 없음.** R1은 manager가 `to_worker`(commands 1개)로 위임했고 worker가 명령을 문자 그대로 실행했으며, 보고에 `commands_run` 부록이 붙었습니다. R2는 고정 120 s 대기 뒤 `running`이 돌아오고 worker가 turn을 끝냈으며, 종료 시 `terminal_done` 알림이 worker에게 정확히 1회 들어가 worker가 done을 1회만 보고했습니다. `status_check`/`worker_stalled`/`manager_recovery` 알림은 발생하지 않았습니다.
- 요청 수: **16**(R1 manager 4 + worker 4, R2 manager 4 + worker 4; error 0, aborted 0) / cap 20, pause 18 미도달. fail-closed 카운터 스톨·오류 0회(COUNTER_STALE·INPUT_REFUSED 없음). 단일 launch(재시도 없음).

## 방법·Isolation
- smoke-03과 같은 방식: 새 `/tmp/wb-cd70-smoke4-*/{data,project}`(git repo)에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`, ui_v1 client, `workbench attach`를 PTY(pyte 170x40)에 붙여 pane에 입력(prefix+1/3). 드라이버는 smoke-03 `drv70c.py`에서 시나리오(R1/R2)·cap(18/20)·wall(20분)·temp 이름만 바꾼 사본입니다. 상태는 `workbench status --json` 스레드(1 s), journal tail, 세션 jsonl로 관측. env에서 `DISPLAY`/`WAYLAND_DISPLAY`/`TMUX*`/`WORKBENCH_*` 제거. `agent.db` 내용은 열지 않았고 세션 jsonl의 `credential_pin` 레코드는 결과에 옮기지 않았습니다.
- **카운터(fail-closed):** 매 UI pump마다 두 역할 세션 jsonl의 assistant message를 다시 세어, 예외·감소·2 s 이상 갱신 없음이면 자동화 pause+입력 거부, 18에 닿으면 pause+입력 거부. 발동 0회. post-shutdown 재집계도 16으로 일치.
- `omp --version`: **omp/18.7.0**. start: `omp isolation: ok (omp/18.7.0 evidence bridge_g3=18.2.10, isolation=18.6.1)`.

| 역할 | state | leaks | warnings(notes) | extension_errors | model / thinking | skills |
|---|---|---|---|---|---|---|
| manager | ok | [] | [] | [] | `openai-codex/gpt-6.1-sol` / high | to-worker, workbench-recovery |
| worker | ok | [] | [] | [] | `openai-codex/gpt-6-luna` / max | to-manager |
- start 직후 세 pane 모두 `alive=True owner=user`, `manual_prompt`. 전체 isolation `ok=true, leaks=[], notes=[]`. 임시 isolation 프로브는 `cleanup state: dead`. → **OMP 18.7.0 isolation 증거: ok/leaks 0/warnings 0(두 역할).**

## R1 — 하드웨어 스펙 (cd69 smoke-03 비교용 동일 문구)
입력: `현재 하드웨어의 스팩을 확인하는 스크립트를 터미널에서 실행해서 결과를 정리해서 나에게 줘` (UTC 09:07:53.327 = 0 s)

| 시각(UTC) | 경과 | 이벤트 |
|---|---|---|
| 09:07:56.687 | 3.4 s | manager `read skill://to-worker` |
| 09:08:17.342 | 24.0 s | manager `to_worker(kind=work, commands=[스크립트 1665자])` — **직접 실행하지 않고 위임** |
| 09:08:17.469 | | TASK가 worker에 도착(0.13 s) |
| 09:08:20.886 | | worker `read skill://to-manager` |
| 09:08:31.366 | TASK→ **13.9 s** | worker `terminal`(명령 = manager 명령과 문자열 동일, `True`, 1665자) |
| 09:08:31.561 | | terminal_ended exit 0, 0.152 s; `terminal_notice outcome=not_needed`(대기 안에 끝나 알림 불필요) |
| 09:08:40.521 | TASK→done **23.1 s** | worker `to_manager kind=done`(terminal→done 9.0 s) → `queued` |
| 09:08:40.563 | | 보고가 manager에 전달(0.04 s) |
| 09:08:56.923 | 입력→최종 **63.6 s** | manager가 사용자에게 최종 답변(표 포함) |

- **비교(cd69 smoke-03 run B):** TASK→첫 terminal 5.9 s → 13.9 s, TASK→done 11.8 s → 23.1 s, 입력→최종 53.6 s → 63.6 s. 세 값 모두 이번이 느리지만 구조 변화(위임 방식·도구 호출 종류)는 없습니다. worker가 이번에는 `terminal` 직전에 thinking 블록(약 10 s)을 가졌고 보고 작성 9.0 s(cd69 5.9 s)로, 차이는 모델 응답 지연으로 보입니다. 사용자 인지 지연 개선 목표(smoke-02의 123.9 s 대비)는 유지됩니다(63.6 s).
- 명령 enforcement: manager의 `commands` 1건을 worker가 그대로 실행(수정·추가 명령 없음). 하네스 `not_in_task_commands` 거절은 발생하지 않았습니다.
- 보고 형태: `kind: report`, `payload.kind: done`, 한국어 message(약 600자: OS·CPU·RAM·GPU·디스크·virt 요약, NVIDIA 유틸 부재 한계 명시) + Workbench가 붙인 `commands_run`(명령은 표시용으로 축약, exit 0, 0.152 s, log_path) + `commands_run_note`. done 1회, 중복·거절 없음.
- watchdog: `status_check`·`worker_stalled` 알림 0건(세션/journal grep에서 이 낱말은 skill 설명문에만 등장).
- 상태 줄 변화(3 s 폴링 기준): `worker: 대기|자동화: idle` → `작업: "Run the supplied read-only hardware ins…" [실행 중]|worker: 작업 중|자동화: idle→active` (09:08:17) → `[종료(완료)]|worker: 대기|자동화: idle`(09:08:40).

## R2 — 150 s 장기 명령
입력: ``worker에게 터미널에서 `sleep 150; echo cd70-long-done` 를 실행하게 하고 결과를 알려줘`` (UTC 09:09:43.089 = 0 s)

| 시각(UTC) | 경과 | 이벤트 |
|---|---|---|
| 09:09:46.479 | 3.4 s | manager `read skill://to-worker` |
| 09:09:52.757 | 9.7 s | manager `to_worker(commands=["sleep 150; echo cd70-long-done"])` |
| 09:09:52.879 | | TASK가 worker에 도착 |
| 09:09:59.495 | TASK→ **6.6 s** | worker `terminal`(명령 동일, skill read 없음); `terminal_started` 09:09:59.534, `wait_seconds=120` |
| 09:11:59.496 | 명령 시작 +119.96 s | terminal 결과 `status: running`(elapsed 119.962 s, output_tail 빈 문자열) — **고정 120 s 대기** |
| 09:12:01.435 | +1.9 s | worker가 turn 종료(텍스트만, 도구 호출 없음) |
| 09:12:01~09:12:29 | 28 s | 요청·알림 0건(worker 쉼, 명령 실행 중) — **status_check 없음** |
| 09:12:29.649 | 150.115 s | terminal_ended exit 0 |
| 09:12:29.688 | | `terminal_done` 알림이 worker에 전달(journal `terminal_notice` pending→**sent, target worker**, 1회) |
| 09:12:35.917 | 알림 +6.2 s | worker `to_manager kind=done`(1회) → `queued` |
| 09:12:35.961 | | 보고가 manager에 전달(`commands_run`: duration 150.115, exit 0) |
| 09:12:39.463 | 입력→최종 **176.4 s** | manager 최종 답변(출력·실행 시간 150.115초·종료 코드 0) |

- 기대 대비: 120 s 뒤 `running` ✔, worker turn 종료 ✔, 실행 중 status_check 없음 ✔(아래 한계 참고), 종료 알림 1회 ✔(그 외 알림 0), worker done 1회 ✔, manager 최종 답변 ✔. 알림은 R2 전체에서 `terminal_done` 1건뿐이었습니다(R1의 `not_needed` 기록 제외).
- 상태 줄(PTY 캡처 `screens/R2_run000…R2_run165`, 15 s 간격): 실행 중 내내 `focus: MANAGER OMP | host 입력 owner: worker | shell mode: control_wait | worker: 작업 중 | 자동화: active` / `작업: "터미널에서 제공된 명령을 그대로 한 번…" [실행 중] | backend: ready | bridge manager=ok worker=ok | 마지막확인 HH:MM:SS (0s전)`. 시작 직전 0.4 s는 `[시작 중]`. 종료 후 `host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle | [종료(완료)]`. host pane 제목은 `HOST SHELL alive owner=worker`로 바뀌어 주인이 보였고, `[worker] $ sleep 150; echo cd70-long-done` 줄이 host pane에 표시됐습니다. manager pane에는 `Terminal … status: "running"` 도구 결과가 보였습니다. 종료 후 상태 줄은 `owner=user`로 복귀.

## 후속 host 명령 / prompt 표시
- R1 후: `echo CD70_R1_FOLLOWUP_$((6*7))` 입력 → 출력 `42` 확인(`found: true`).
- R2 후: 같은 방식으로 `CD70_R2_FOLLOWUP_42` 확인(`found: true`). host shell은 정상 사용 가능했습니다.
- **O1 재현(3회째, 표시, 낮음):** 후속 명령 뒤 pane 마지막 줄이 `CD70_R1_FOLLOWUP_42 completed.`(앞 줄 `Hardware inspection completed.` 위에 출력이 덮어써져 ` completed.`가 남음) / `$ echo CD70_R1_FOLLOWUP_…` 순서로, 새 프롬프트는 pane 아래로 밀려 보이지 않습니다. R2 후에도 `CD70_R2_FOLLOWUP_42`가 입력 줄(`$ echo CD70_R2_…`) **위**에 나옵니다. 2 s·5 s 프레임이 같아 중간 프레임이 아닙니다. 가득 찬 pane에서 줄 바꿈 직후 스크롤이 한 줄 어긋나는 모양이며, 내 pyte 에뮬레이션과 pane 렌더링의 차이일 가능성은 배제하지 못했습니다(실제 터미널 확인 없음). 조건은 smoke-02/03과 같습니다. 수정 필요 여부는 Root 판단입니다.

## 동작하지 않은 것 / 관찰 / 분류
- **결함 없음(이번 범위).**
- **O2 (모델 행동, 낮음, 신규 관찰):** worker(gpt-6-luna)가 R1의 최종 텍스트(`Хардуерын шалгалтыг…`)와 R2의 turn 종료 텍스트·`to_manager.message`(`Командыг нэг удаа…`)를 **몽골어**로 썼습니다(R1의 `to_manager.message`는 한국어). manager는 정확히 이해해 한국어로 사용자에게 답했고 보고의 `commands_run`·수치는 하네스 값이라 영향은 없습니다. worker가 사용자에게 직접 보이는 경로가 아니라 낮음으로 분류하지만, 이전 smoke 결과(cd68~cd70)에는 몽골어 언급이 없어 새 관찰입니다. 원인(모델 변동 vs worker skill/지시문 언어 지정 부재)은 확인하지 못했습니다.
- **O3 (관측 한계, 정보):** R2의 150 s 명령은 대기 120 s 뒤 worker가 쉬는 구간이 **28 s**뿐입니다(60 s 점검 대기 미만). 따라서 "명령 실행 중 worker가 60 s 이상 쉬어도 status_check가 없음"은 이번에는 시간적으로 검증되지 않았고, "알림이 발생하지 않음"과 `running` 반환 후 정상 종료만 확인했습니다. 그 사례는 더 긴 명령(예: `sleep 200` 이상, 약 +80 s)이 필요합니다.
- **O4 (비용, 정보):** manager의 R1 `to_worker`(스크립트 작성)가 24.0 s(요청 생성 20.7 s)로 cd69의 27.6~29.0 s와 같은 수준입니다. 이전 O4와 동일.
- 확인하지 못한 경로(spill, `not_in_task_commands` 거절, 다중 명령, `start_failed`, 실모델에서의 worker 재시작/`restart_worker`)는 이번 시나리오에 없었습니다.

## 요청 수 상세
| 구간 | manager | worker | 도구 호출 |
|---|---|---|---|
| R1 | 4 (skill read, to_worker, 대기 텍스트, 최종 답변) | 4 (skill read, terminal, to_manager, 종료 텍스트) | read 1 / to_worker 1 / read 1 / terminal 1 / to_manager 1 |
| R2 | 4 (skill read, to_worker, 대기 텍스트, 최종 답변) | 4 (terminal, `running` 후 텍스트, notice 후 to_manager, 종료 텍스트) | read 1 / to_worker 1 / terminal 1 / to_manager 1 |
| 합계 | 8 | 8 | 16 (cap 20, pause 18) |

## Residue
- 프로세스: `workbench shutdown --yes --json` exit 0, `verified: true`(host_shell exit -1, manager·worker 143, survivors 없음, supervisor dead), 소유 프로세스 잔여 0, 강제 kill 없음, `no active work reported`.
- 파일: `/tmp/wb-cd70-smoke4-*` 삭제 확인(`root_removed: true`, `ls /tmp | grep smoke4` 없음), 임시 project `git status` 깨끗. scratch의 driver·세션 사본·화면 캡처·로그는 repo 밖(Claude scratchpad `run4/`)에만 있고 run dir에는 이 파일과 `result-p27-cd70-smoke-04-timeline.json`만 추가했습니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스, `~/.omp`·`~/.agents`·`~/.claude`·`~/.codex`는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. commit, graphify update, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. C-D68~C-D70 정상 경로(manager commands → 정확 실행 → `commands_run` 보고, 고정 120 s 대기 뒤 `running` → worker turn 종료 → `terminal_done` 1회 → done 1회)가 UI polish 이후 실모델에서 회귀 없이 동작했습니다. 사용자 UR 재시도 진행에 걸림돌 없음.
2. O2(worker 몽골어 응답)는 UR에서 재발하는지 보고, 재발하면 worker skill에 응답 언어 지정이 필요한지 검토할 수 있습니다.
3. O1(host pane 표시, 3회 재현)은 실제 터미널에서 재확인하거나 pane 렌더링 단위 테스트 후보입니다(이번에는 결함으로 올리지 않음).
4. 60 s 점검 대기 동안 실행 중 명령이 있는 경우의 status_check 억제를 실모델로 확인하려면 별도 smoke(≥200 s 명령)가 필요합니다.
