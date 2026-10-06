# p27-cd69-smoke-02: C-D69 실모델 재-smoke 결과 (역할 경계 + stuck-shell 수정)

- 역할: test_designer (Opus). 실행 시각 2026-10-06 19:57:34~20:00:29 KST(약 3분/20분). 1회 실행, 재시도 없음.
- 결론 요약
  - **Task가 끝까지 실행되고 `done`으로 닫혔습니다.** smoke-01의 P1 증상(긴 명령 → `start_failed` → shell이 `owner manager / control_wait`에 묶임 → `blocked`)은 재현되지 않았습니다. worker는 `terminal` 1회(2704자 heredoc, exit 0, 0.122 s)로 실행했고, `command_too_long`·`start_failed`·`host_terminal_busy`는 0건입니다.
  - **역할 경계(C-D69 (2))는 지켜졌습니다.** manager는 절차(명령 목록·허용 대체·멈출 조건·돌려받을 값, `analysis: summary`)를 넘겼고, worker는 그 명령과 허용 대체만 실행해 사실과 짧은 요약을 보고했습니다. 범위 확대·추가 조사·중복 보고는 0건입니다.
  - **시간:** TASK→첫 `terminal` 53.8 s, TASK→`to_manager done` **89.5 s**, 사용자 입력→manager 최종 답변 123.9 s. 실행 자체는 0.1 s이고, 나머지는 worker의 스크립트 작성(약 45 s)과 보고 작성(35.5 s)입니다.
  - **시나리오 2:** start failure가 일어나지 않아 "실패 뒤 shell 반환"은 실모델로 다시 확인할 수 없었습니다. 대신 Task 종료 뒤 host shell이 `owner user / manual_prompt`였고, 사용자 후속 명령(`echo WB_FOLLOWUP_$((6*7))` → `WB_FOLLOWUP_42`)이 바로 동작했습니다.
- provider request: **8/30**(manager 4 + worker 4). error/aborted 0, cap 미도달, pause 없음.
- 진입점: smoke-01과 같습니다. 새 `/tmp/wb-cd69-smoke2-<rand>/{data,project}`(project는 git repo)를 만들고 project dir에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home)를 실행했습니다. product UI(`workbench attach`)를 PTY(pyte, 170x40)에 붙여 manager pane에 입력했습니다. 관측은 `workbench status --json`, temp `workflow/handoffs.jsonl`, temp 세션 jsonl로 약 1 s 간격입니다.
- env 차이(안전 조치): Workbench 프로세스 env에서 `DISPLAY`, `WAYLAND_DISPLAY`, `TMUX*`(smoke-01과 같음)와 `WORKBENCH_*`(사용자 Workbench 설정이 섞이지 않도록 추가)를 뺐습니다. 값은 보지 않았습니다.
- 코드: HEAD df7ecd7(1ea7c2f 뒤의 docs commit, `src/`·`omp_bridge/` 차이 0). omp/18.6.1. Python `/tmp/cw02-g1-venv/bin/python`.
- 드라이버: p27w(`tests/backend/live_cw18_independent_p27w.py`)의 `Ui`/`processes_mentioning`/`kill_exact`를 import한 scratch 스크립트입니다(28 request pause, 15분 시나리오 상한, Task 종료 후 25 s·request 정지 15 s에서 종료). 실행 후 삭제했습니다.
- 짧은 로그: `result-p27-cd69-smoke-02-timeline.json`(경로는 `<root>`로 줄임, 상태줄은 변화분만). 세션은 구조만(블록 종류·길이·도구 이름·짧은 인자) 담았습니다. secret은 없습니다.

## Isolation / 모델
| 역할 | start 결과 | 관측 model | 설정 |
|---|---|---|---|
| manager | `omp isolation: ok`(`leaks: []`) | `gpt-6.1-sol`(assistant 4건 모두) | thinking high |
| worker | 〃 | `gpt-6-luna`(assistant 4건 모두) | thinking max, service tier `priority` |

`agent.db`는 symlink입니다(존재·symlink 여부만 확인, 내용은 읽지 않음).

## Timeline (KST)
| 시각 | req | 관측 |
|---|---|---|
| 19:57:36 | 0 | start exit 0, phase ready, isolation ok |
| 19:57:39 | 0 | product UI attach, `host 입력 owner: user · shell mode: manual_prompt · worker: 대기` |
| 19:57:59.97 | 0 | manager pane에 사용자 원문 입력: "현재 하드웨어의 스팩을 확인하는 스크립트를 터미널에서 실행해서 결과를 정리해서 나에게 줘" |
| 19:58:02.5 | 1 | manager `read`(skill://to-worker) |
| 19:58:20.50 | 2 | manager `to_worker{kind: work, analysis: "summary", task_id: null, message 1006자, spec{goal, instructions, paths: [], execution: null}}` → `dispatched`. Task `work/running`, worker `작업 중` |
| 19:58:20.63 | 2 | worker 세션에 TASK user 메시지(1844자) 도착 |
| 19:58:24.3 | 3 | manager text 95자("…수집하겠습니다")로 turn 종료. 직접 실행 없음 |
| 19:58:28.8 | 4 | worker `read`(skill://to-manager) |
| 19:58:40.6 | 4 | journal outbox `unknown / BridgeTimeout`(Task 전달 처리 확인 20 s 초과. smoke-01·smoke-03 O2와 같은 기록, 전달은 1회 정상) |
| **19:59:14.39** | 5 | **worker `terminal{command: 2704자 bash heredoc}` → `terminal_started` → `exited`, exit 0, 0.122 s.** `terminal_notice: not_needed` |
| 19:59:50.08 | 6 | worker `to_manager{kind: done, message 3837자}` → `queued`. Task `종료(완료)`, worker `대기`, 자동화 `idle` |
| 19:59:53.6 | 7 | worker 빈 text(0자)로 turn 종료 |
| 20:00:03.9 | 8 | manager가 사용자에게 text 1030자(표 2개 + 미확인 항목)로 답함. outbox `delivered / omp_processed` |
| 20:00:19 | 8 | 시나리오 1 관측 종료(Task 종료 뒤 request 정지) |
| 20:00:19~24 | 8 | host shell 상태 `owner user / manual_prompt`. host pane 포커스(prefix 3) 뒤 `echo WB_FOLLOWUP_$((6*7))` → 화면에 `WB_FOLLOWUP_42` |
| 20:00:25 | 8 | `shutdown --yes --json` exit 0(드라이버가 stdout JSON을 해석하지 못해 timeline `verified`는 null. smoke-03 기준 exit 0은 verified일 때만) |
| 20:00:28~29 | 8 | 소유 프로세스 잔여 0, project `git status` 깨끗함, temp root 삭제 확인 |

## Provider request 수 (세션 jsonl assistant message)
| OMP | request | 내용 |
|---|---|---|
| manager | 4 | `read` · `to_worker`(dispatched) · text 95자 · text 1030자(최종 답변) |
| worker | 4 | thinking+`read` · thinking 6개+`terminal`(2704자, exit 0) · thinking 3개+`to_manager done` · 빈 text |
| 합계 | **8** | error/aborted 0, subagent 0 |

## 시나리오 1 관측

### manager의 `to_worker` 인자 (secret 없음)
- `kind: "work"`, `analysis: "summary"`, `task_id: null`, `spec: {goal: "터미널에서 읽기 전용 하드웨어 확인 스크립트를 실행하고 실제 측정한 현재 시스템 사양과 확인 제한을 보고한다.", instructions: "읽기 전용 수집만 수행. 파일 생성/수정, 설치, sudo, 네트워크, 부하 테스트 금지.", paths: [], execution: null}`
- `message`(1006자, 한 문단) 형태
  - 실행 방식: "호스트 터미널에서 **파일을 만들지 않는** 읽기 전용 Bash 스크립트(**bash heredoc 또는 bash -c**)를 실행하라."
  - 명령: `uname -srm`, `lscpu`, `free -h`, `lsblk -d -o NAME,SIZE,MODEL,ROTA,TYPE`, `df -hT /`, `lspci -nn`(GPU/Display/VGA만), `nvidia-smi --query-gpu=…`, `systemd-detect-virt`, `/etc/os-release` PRETTY_NAME, DMI 4필드, `dmidecode --type 17`(sudo 없이)
  - 허용 대체: lscpu/free/lsblk/lspci가 없을 때만 `/proc/cpuinfo`·`/proc/meminfo`·`/sys/block`·`/sys/class/drm`
  - 멈출 조건: 없는 명령·권한 오류는 기록하고 다음 섹션. 설치·sudo·벤치마크·파일 수정·네트워크 금지, 식별정보 수집 금지
  - 돌려받을 값: 실행한 스크립트, 종료 상태, CPU/RAM/GPU/디스크/OS 항목, 실패한 명령, "없는 값은 추정하지 말라"
- 평가: C-D69 (2)의 절차 요건을 모두 담았습니다. smoke-01(1053자, 9섹션 + 섹션별 종료 상태·자원 할당량 구분 요구)보다 조금 짧지만 거의 같은 크기입니다. 1ea7c2f의 to-worker skill 문구("Keep it as concise as the task needs")가 들어간 뒤에도 원 요청보다 범위가 넓은 절차(dmidecode, DMI, 가상화)를 냈습니다. 또한 **manager가 "파일을 만들지 않는 heredoc/bash -c"를 지정**해서, to-manager skill의 새 안내(긴 스크립트는 `write`로 파일을 만든 뒤 `bash <file>`)를 worker가 쓸 수 없게 했습니다.

### worker의 TASK 메시지
- 첫 TASK user 메시지(1844자)에 manager `message` 1006자가 그대로 들어 있습니다(잘림 없음). payload에 `analysis: "summary"`와 `analysis_rule`("run only the given steps and allowed fallbacks; report facts and a short summary; do not investigate or plan beyond the given steps."), `goal`·`instructions`·`paths`·`revision`·`task_id`가 있습니다.

### worker의 도구 호출
| 시각 | 도구 | 인자 | 결과 |
|---|---|---|---|
| 19:58:28.8 | `read` | `skill://to-manager` | ok(7510자, 1ea7c2f의 짧은 명령·`command_too_long` 안내 포함) |
| 19:59:14.4 | `terminal` | `command` 2704자 `bash <<'EOF' … EOF`(한 번에 실행, script 파일 없음) | `exited`, exit 0, 0.122 s, 출력 tail 잘림 없음 |
| 19:59:50.1 | `to_manager` | `kind: done`, `message` 3837자, `task_id`, `in_reply_to` | `queued` → manager 처리 |

- **짧은 명령/스크립트 파일 여부:** worker는 `write` + `bash <file>`를 쓰지 않았습니다. manager가 "파일을 만들지 않는 heredoc"을 지정했기 때문에 지시를 따른 것입니다. 명령은 2704자로 skill이 안내한 상한(약 2,800 ASCII자)보다 **96자 짧았습니다**. 통과는 했지만 여유가 작습니다.
- **command_too_long / start_failed: 0건.** host shell은 전 과정에서 `owner user`로 보였고(명령이 0.1 s라 1 s 샘플에서는 manager 소유 구간이 잡히지 않음), Task 종료 뒤 `manual_prompt`였습니다.
- **범위 확대 0건:** 스크립트는 지정 명령과 허용 대체만 담았습니다. 덧붙인 것은 `LC_ALL=C`, 명령 존재 확인·종료 코드 출력, dmidecode 출력의 `Serial Number` 행 제거(식별정보 금지 지시를 지키기 위한 필터)입니다. OMP `bash`/`eval`, 파일 읽기, subagent는 없었습니다.
- **시간 분해:** TASK 도착 19:58:20.6 → skill 읽기 19:58:28.8(8 s) → 스크립트 작성·첫 `terminal` 19:59:14.4(**45.6 s**, thinking 6블록) → 실행 0.1 s → 보고 작성·`to_manager done` 19:59:50.1(**35.5 s**). TASK→done **89.5 s**.
- 비교
  | 실행 | worker req | 실행 형태 | TASK→첫 실행 | TASK→보고 | 결과 |
  |---|---|---|---|---|---|
  | cd68 smoke-01 | 8 | 짧은 명령 1회(`lscpu; free -h; nproc; df -h`) | – | 약 19 s | done(중복 done 1회) |
  | cd69 smoke-01 | 5 | 5058자 → 3897자 inline 스크립트 | 70 s | 105.7 s | **blocked**(P1) |
  | **cd69 smoke-02** | **4** | 2704자 heredoc 1회 | **53.8 s** | **89.5 s** | **done** |
  - request 수는 가장 적고 범위 확대·중복 보고도 없습니다. 하지만 cd68 smoke-01(약 19 s)보다 약 4.7배 느립니다. 지연은 분석 단계가 아니라 **manager의 큰 절차 → worker의 긴 스크립트 작성(45.6 s) → 스크립트 원문을 포함한 긴 보고 작성(35.5 s)**에서 생깁니다.

### 보고 내용 형태 (짧은 발췌)
- worker `to_manager done`(3837자)의 구성: ① 실행 사실("읽기 전용 스크립트를 호스트 터미널에서 실제 실행했습니다. Bash heredoc 실행 종료 상태 0 …", 로그 경로) ② `결과:` 8개 bullet(CPU `i7-1165G7`·1소켓 4코어 8스레드, 메모리 30 GiB(모듈 미확인), GPU Intel Iris Xe(VRAM 미확인, nvidia-smi 없음), 디스크 nvme 465.8G SSD·루트 34%, OS/kernel, 가상화 `none`(exit 1), DMI `Default string`, `실패/제한:` dmidecode 권한 거부·nvidia-smi 없음·fallback 미실행) ③ `실행한 스크립트:` 코드블록(약 2.7 KB, terminal 명령 원문).
- 평가: "실행 내용 / 핵심 출력 / 실패·누락"이 모두 있고, 분석은 요약 수준입니다(해석·권고·추가 조사 없음). 보고의 약 70%가 스크립트 원문인데, manager가 "실제 실행한 스크립트"를 돌려받을 값으로 지정했기 때문입니다.

### manager의 최종 답변 (1030자, 요지)
- "터미널에서 **읽기 전용 Bash 스크립트를 실제 실행**한 결과입니다." 뒤에 `하드웨어 사양` 표(CPU, 코어/스레드, 아키텍처, 메모리, GPU, 저장장치, 루트 파일시스템), `시스템 환경` 표(OS, 커널, 가상화), `확인되지 않은 항목` 3개(RAM 모듈 – dmidecode 권한, GPU 메모리·드라이버, 제조사/보드 – DMI `Default string`), 마지막 줄에 사용한 주요 명령과 종료 코드 0.
- worker 보고와 값이 일치하고 추정값을 넣지 않았습니다. 정리·해석은 manager가 했습니다(C-D69 (2)의 분담과 맞음). 스크립트 원문은 사용자 답변에 다시 넣지 않았습니다.

## 시나리오 2: start failure 뒤 shell 반환
- 이번 실행에서는 start failure가 없어 **해당 경로는 실모델로 미검증**입니다(1ea7c2f의 단위·독립 테스트 범위).
- 관측한 것: worker 명령 종료 뒤와 Task 종료 뒤 상태줄은 `host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle`이었습니다. host pane에 포커스하고 후속 명령을 입력하자 `WB_FOLLOWUP_42`가 나왔고 상태도 `owner user`로 유지됐습니다.

## 동작한 것 (실모델)
- smoke-01의 P1 증상 없음: `terminal`이 시작·종료됐고 Task가 `done`으로 닫혔고 host shell이 사용자에게 남았습니다. 후속 사용자 명령이 동작했습니다.
- C-D69 (2): manager는 절차와 `analysis: summary`를 넘기고 직접 실행하지 않았습니다. worker는 지정 명령과 허용 대체만 실행하고, 사실·핵심 값·실패/누락과 짧은 요약으로 보고했습니다. 범위 확대·자체 조사·중복 보고·analyst subagent는 없었습니다.
- C-D69 첫 TASK 전체 message: 1006자 원문과 `analysis_rule`이 worker에게 그대로 전달됐습니다.
- start/shutdown 정상, isolation ok, residue 0, request 8/30.

## 동작하지 않은 것 / 관찰 / 분류
- **M2 (모델 행동/지침, 중):** manager가 "파일을 만들지 않는 heredoc 또는 bash -c"를 지정해, 긴 스크립트는 파일로 쓰라는 to-manager skill의 새 안내와 충돌했습니다. worker는 2704자 inline으로 상한(약 2,800자)에 96자 차이로 통과했습니다. 절차가 조금만 더 컸다면 `command_too_long`이 났을 것입니다. to-worker skill(manager 쪽)에는 명령 길이 상한이나 "실행 방식을 파일 금지로 묶지 말 것" 같은 안내가 없습니다. 수정 여부는 Root/사용자 판단입니다.
- **M1 (지속, 중):** to-worker skill에 "Keep it as concise as the task needs"가 추가된 뒤에도 manager 절차는 1006자로 smoke-01(1053자)과 비슷했습니다. 그 결과 worker의 스크립트 작성 45.6 s, 보고 작성 35.5 s(보고의 약 70%가 스크립트 원문)가 걸렸습니다. "늦게 전달"의 남은 원인은 worker의 과잉 분석이 아니라 절차 크기와 "실행한 스크립트 반환" 요구입니다.
- **미검증:** `command_too_long`·`start_failed` 뒤 shell 반환과 결과 detail의 실모델 경로(이번에 발생하지 않음).
- **O2 (기존, 낮음):** Task 전달 outbox가 20 s 뒤 `unknown / BridgeTimeout`으로 기록됐습니다. worker의 첫 turn이 길어서 생기는 기록이며 전달은 1회 정상이었습니다.
- **O3 (모델 행동, 낮음):** worker가 `to_manager` 뒤 빈 text(0자, output 15 token)로 turn을 끝냈습니다. 기능 영향은 없습니다.
- **표시 관찰(낮음, 미확인):** 후속 명령 직후 캡처 1장에서 host pane 마지막 두 줄이 `WB_FOLLOWUP_42t=1]`(앞 줄 `[dmidecode exit=1]` 위에 덮어씀) / `$ echo WB_FOLLOWUP_$((6*7))` 순서로 보였습니다. 드라이버가 문자열을 찾은 즉시 캡처해서 다시 그리기 중간 프레임일 수 있습니다. 이후 프레임은 남기지 않아 원인은 확인하지 않았습니다.

## 상태줄·pane text screenshot (product UI)
```
[19:57:39 attach]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle

[19:58:20 Task dispatched]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: active
작업: 작업 "사용자 요청: 현재 하드웨어 사양 확인 스…" [실행 중] | backend: ready | bridge manager=ok worker=ok | …

[19:59:50 보고 직후]
… worker: 작업 중 | 자동화: active
작업: 작업 "사용자 요청: 현재 하드웨어 사양 확인 스…" [실행 중 · done_report_pending] | …

[19:59:51 이후]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 작업 "사용자 요청: 현재 하드웨어 사양 확인 스…" [종료(완료)] | backend: ready | …
┌─ HOST SHELL alive owner=user ───…
│[dmidecode exit=1]
│$

[20:00:24 후속 명령]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
```

## Residue
- 프로세스: `shutdown --yes --json` exit 0, 소유 프로세스 잔여 0, 강제 kill 없음.
- 파일: `/tmp/wb-cd69-smoke2-w01pcbt9` 삭제 확인. worker는 `write`를 쓰지 않아 temp root 밖에 만든 파일이 없습니다. scratch의 driver, 세션·journal 사본, 화면 캡처, run log는 삭제했습니다. run dir에는 이 파일과 `result-p27-cd69-smoke-02-timeline.json`만 남깁니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스, 사용자 클립보드는 건드리지 않았습니다. credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. 세션 jsonl의 `credential_pin` 레코드는 결과에 옮기지 않았습니다. commit, graphify update, ledger 수정, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. C-D69 (5)의 stuck-shell 증상은 실모델 정상 경로에서 재발하지 않았습니다. 실패 경로(`command_too_long`/`start_failed` 뒤 반환)는 실모델로 재현되지 않았으므로 단위·독립 테스트 근거로 판단해야 합니다.
2. M2: manager가 "파일 금지 inline 스크립트"를 지정하면 worker가 상한 근처의 긴 명령을 만들게 됩니다. to-worker skill에 명령 길이 상한과 "긴 스크립트는 worker가 파일로 실행할 수 있게 둘 것" 같은 문구를 넣을지 검토가 필요합니다(지침 변경은 사용자 결정).
3. M1: 절차 크기와 "실행한 스크립트 원문 반환" 요구가 지연(약 90 s)의 주원인입니다. 사용자의 "늦게 전달" 기준에 맞는지는 사용자 판단이 필요합니다.
