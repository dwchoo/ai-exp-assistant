# p27-cd69-smoke-01: C-D69 실모델 smoke 결과 (역할 경계 + OMP `/copy`)

- 역할: test_designer (Opus). 실행 시각 2026-10-06 17:51:05~18:05:36 KST(약 14.5분/20분). 1회 실행, 재시도 없음.
- 결론 요약
  - **역할 경계(C-D69 (2))는 지시·수행 단계에서 지켜졌습니다.** manager는 실행 가능한 절차를 넘겼고(명령 9단계, 허용 대체, 멈출 조건, 돌려받을 값, `analysis: summary`), worker는 그 명령만 스크립트로 묶어 실행하려 했습니다. 범위 확대는 0건입니다.
  - **그러나 Task는 실행되지 못하고 `blocked`로 끝났습니다. 원인은 제품 결함입니다(P1).** worker의 첫 `terminal` 명령(5058자)이 `start_failed: ValueError: bounded request exceeds pipe atomic write`로 시작되지 않았습니다. 그 뒤 host shell이 `owner manager / control_wait`에 묶여 shutdown까지(약 12분) 풀리지 않았습니다. worker의 두 번째 시도(3897자)는 `host_terminal_busy: manager owns the host shell`로 거부됐고, worker는 `blocked`를 보고했습니다. 그래서 하드웨어 결과, 보고 형태(실행 내용·핵심 출력), 보고 지연은 실모델로 비교할 수 없었습니다.
  - **`/copy`(C-D69 (1))는 동작했습니다.** manager pane에서 OMP `/copy`를 실행하자 바깥 terminal(드라이버 PTY)에 `ESC]52;c;<base64 696>BEL` 1건이 깨끗하게 쓰였습니다. 디코드한 520 bytes(249자)는 manager의 마지막 답변 원문과 sha256까지 같습니다. pane에 base64는 그려지지 않았고 알림은 `[MANAGER OMP] 복사됨: 249자`였습니다. UI env에 `TMUX`를 넣은 재attach에서는 plain 1건과 tmux-wrapped 1건(`ESC P tmux; ESC ESC ]52;c;…BEL ESC \`)이 함께 나왔습니다.
- provider request: **9/30**(manager 4 + worker 5). error/aborted 0, cap 미도달. `/copy`는 request를 만들지 않았습니다(전후 9).
- 진입점: smoke-01/03과 같습니다. 새 `/tmp/wb-cd69-smoke1-<rand>/{data,project}`(project는 git repo)를 만들고, project dir에서 `python -m workbench start --data-dir … --omp ~/.local/bin/omp --no-attach`(실제 HOME, Workbench 소유 OMP home)를 실행했습니다. 이어 product UI(`workbench attach`)를 PTY(pyte, 170x40)에 붙여 manager pane에 입력했습니다. 관측은 `workbench status --json`, temp `workflow/handoffs.jsonl`, temp 세션 jsonl로 약 1 s 간격으로 했고, PTY 바깥 출력 bytes는 원본 그대로 모았습니다.
- **env 차이(안전 조치):** OMP `/copy`는 OSC 52를 쓴 뒤 native clipboard도 시도합니다(바이너리 확인: `process.stdout.isTTY`면 OSC 52, 이어서 `DISPLAY`/`WAYLAND_DISPLAY` 기반 복사). 사용자의 실제 클립보드를 건드리지 않도록 Workbench 프로세스 env에서 `DISPLAY`, `WAYLAND_DISPLAY`, `TMUX*`를 뺐습니다(값은 보지 않음). 그래서 첫 attach는 TMUX가 없는 plain 경로입니다.
- 코드: HEAD a35077a(707bcf9 뒤의 docs commit, `src/`·`omp_bridge/` 차이 0). omp/18.6.1. Python `/tmp/cw02-g1-venv/bin/python`.
- 드라이버: p27w의 `Ui`/`processes_mentioning`/`kill_exact`를 재사용한 scratch 스크립트입니다(raw PTY 수집 추가, 28 request에서 pause, 14분에 시나리오 1 대기 종료). 실행 후 삭제했습니다.
- 짧은 로그: `result-p27-cd69-smoke-01-timeline.json`(경로는 `<root>`로 줄임). 세션은 구조만(블록 종류·길이·도구 이름·짧은 인자) 담았고, `/copy` 증거는 길이와 hash만 담았습니다. secret은 없습니다.

## Isolation / 모델
| 역할 | start 결과 | 관측 model | 설정 |
|---|---|---|---|
| manager | `omp isolation: ok` | `gpt-6.1-sol`(assistant 4건 모두) | thinking high |
| worker | 〃 | `gpt-6-luna`(assistant 5건 모두) | thinking max, service tier `priority`(fast) |

`agent.db`는 symlink입니다(존재·symlink 여부만 확인, 내용은 읽지 않음).

## Timeline (KST)
| 시각 | req | 관측 |
|---|---|---|
| 17:51:06 | 0 | start exit 0, phase ready, isolation ok |
| 17:51:07 | 0 | product UI attach, `worker: 대기 · 자동화: idle` |
| 17:51:15 | 0 | manager pane에 사용자 원문 입력 완료: "현재 하드웨어의 스팩을 확인하는 스크립트를 터미널에서 실행해서 결과를 정리해서 나에게 줘" |
| 17:51:18 | 1 | manager `read`(skill://to-worker) |
| 17:51:36.8 | 2 | manager `to_worker{kind: work, analysis: "summary", task_id: null, message 1053자, spec{goal, instructions, paths: []}}` → `dispatched`. Task `work`/`running`, worker `busy` |
| 17:51:36.9 | 2 | worker 세션에 TASK user 메시지(1896자) 도착 |
| 17:51:40 | 3 | manager text 98자로 turn 종료. 직접 실행 없음 |
| 17:51:40.8 | 4 | worker `read`(skill://to-manager) |
| 17:51:56.9 | 4 | journal outbox `unknown / BridgeTimeout`(Task 전달 처리 확인 20 s 초과, smoke-03의 O2와 같은 기록) |
| 17:51:40→17:52:47 | 4 | **worker가 약 66 s 동안 생각한 뒤(thinking 블록 7개) 스크립트를 작성함** |
| **17:52:47.2** | 5 | **worker `terminal{command: 5058자 bash -c 스크립트}` → `terminal_start_failed`, reason `ValueError: bounded request exceeds pipe atomic write`.** 결과 detail은 "The command was not started; the host terminal is the user's again." |
| 17:53:00 | 5 | 상태줄 `host 입력 owner: manager · shell mode: control_wait` (이후 shutdown까지 그대로) |
| **17:53:16.1** | 6 | **worker가 줄인 스크립트(3897자)로 `terminal` 재시도 → `terminal_refused: host_terminal_busy`, "manager owns the host shell"** |
| 17:53:22.6 | 7 | worker `to_manager{kind: blocked, message 162자, reason}` → `queued`. Task `막힘 · worker_blocked`, worker `대기` |
| 17:53:24.7 | 8 | worker text 69자로 turn 종료 |
| 17:53:34 | 9 | manager가 사용자에게 text 249자로 답함: 실행 차단을 알리고 "host terminal을 사용자 소유의 유휴 프롬프트 상태로 전환해 달라"고 요청 |
| 17:53:34~18:05:05 | 9 | 추가 request 없음. Task는 `blocked`, host shell은 `owner manager / control_wait`, host pane은 `$ wb-handoff`만 보임 |
| 18:05:05 | 9 | 드라이버의 14분 대기 상한으로 시나리오 1 관측 종료 |
| 18:05:08~19 | 9 | **`/copy`(1차, TMUX 없음):** manager pane 포커스, `/copy` 입력 → OMP 명령 목록이 뜸(`⧉ copy  Pick text or code…`). Enter 1회로는 복사되지 않음(목록 선택만 됨). 6 s 뒤 Enter 1회 더 → `Copied assistant message to clipboard`, 바깥 출력에 OSC 52 1건, UI 알림 `[MANAGER OMP] 복사됨: 249자` |
| 18:05:23 | 9 | detach 뒤 UI만 `TMUX=<가짜 값>` env로 재attach. 재attach replay에 OSC 52 없음(`replay_has_osc52: false`) |
| 18:05:31~32 | 9 | **`/copy`(2차, TMUX env):** 같은 순서(Enter 2회). plain 1건 + tmux-wrapped 1건, 알림 `복사됨: 249자 (tmux는 set-clipboard on 또는 allow-passthrough on 필요)` |
| 18:05:33 | 9 | `shutdown --yes --json` exit 0(smoke-03 기준 `verified: true`일 때만 0. 드라이버는 이번에도 `verified` 키를 잘못된 위치에서 읽어 timeline 값은 null) |
| 18:05:36 | 9 | 소유 프로세스 잔여 0, temp root 삭제 확인, project `git status` 깨끗함 |

## Provider request 수 (세션 jsonl assistant message)
| OMP | request | 내용 |
|---|---|---|
| manager | 4 | `read` · `to_worker`(dispatched) · text 98자 · text 249자(blocked 안내) |
| worker | 5 | thinking+`read` · thinking 7개+`terminal`(5058자, start_failed) · thinking+`terminal`(3897자, refused) · thinking+`to_manager blocked` · text 69자 |
| 합계 | **9** | error/aborted 0, subagent 0 |

## 시나리오 1: 역할 경계 관측

### manager의 `to_worker` 인자 (secret 없음)
- `kind: "work"`, `analysis: "summary"`, `task_id: null`, `spec: {goal: "현재 터미널에서 읽기 전용 스크립트를 실행하여 하드웨어 사양을 확인하고 실제 출력과 제한사항을 보고", instructions: "읽기 전용. 파일 수정, 설치, sudo, 벤치마크, 식별정보 수집 금지. 지정된 명령과 명시된 대체만 사용.", paths: []}`
- `message`(1053자)는 절차 형태입니다.
  - 명령: (1) `uname -srmo` (2) `lscpu` (3) `free -h` (4) `lsblk -e 7 -o …MOUNTPOINTS` (5) `df -hT` (6) `lspci` (7) `nvidia-smi --query-gpu=…` (8) `systemd-detect-virt` (9) `nproc`, `Cpus_allowed_list`, cgroup `cpu.max`/`memory.max`
  - 허용 대체: `MOUNTPOINTS`가 안 되면 `MOUNTPOINT`로 1회 재시도, `lscpu`/`free`가 없을 때만 `/proc/cpuinfo`·`/proc/meminfo`, 읽을 수 있으면 DMI 4개 필드
  - 멈출 조건: "미설치/권한오류는 기록 후 다음 섹션", "추가 탐색이나 벤치마크는 하지 말고 종료"
  - 돌려받을 값: CPU/메모리/GPU/디스크/가상화 항목, 오류·미확인 항목, "사실만 정리"
- 평가: C-D69 (2)의 "실행 가능한 절차" 요건(명령·대체·멈출 조건·돌려받을 결과)을 모두 담았습니다. 다만 절차 자체가 무겁습니다. 9개 섹션, 섹션별 종료 상태 출력, `lspci` 결과 필터 요약 요구, 자원 할당량과 물리 사양 구분 요구가 있습니다. 사용자의 원 요청보다 범위가 넓은 것은 manager의 판단입니다(C-D69에서 범위 확대는 manager 몫).

### worker의 TASK 메시지
- 첫 TASK user 메시지(1896자)에 manager `message` 원문 1053자가 그대로 들어 있습니다(C-D69 첫 TASK 전체 message, 잘림 없음).
- payload에는 `analysis: "summary"`와 `analysis_rule: "Analysis: summary - run only the given steps and allowed fallbacks; report facts and a short summary; do not investigate or plan beyond the given steps."`가 함께 있습니다. `goal`·`instructions`·`paths`·`revision`·`task_id`도 있습니다.

### worker의 도구 호출
| 시각 | 도구 | 인자 | 결과 |
|---|---|---|---|
| 17:51:40.8 | `read` | `skill://to-manager` | ok |
| 17:52:47.2 | `terminal` | `command`(5058자 `bash -c '…'`) | `start_failed`(pipe atomic write) |
| 17:53:16.1 | `terminal` | `command`(3897자, 같은 절차를 짧게 줄임) | `host_terminal_busy` |
| 17:53:22.6 | `to_manager` | `kind: blocked`, `message` 162자, `reason`, `task_id`, `in_reply_to` | `queued` |

- **범위 확대 0건:** 두 스크립트 모두 manager가 지정한 명령과 허용 대체만 담았습니다(uname, lscpu, free, lsblk(+MOUNTPOINT 재시도), df, lspci 필터, nvidia-smi, systemd-detect-virt, nproc, `Cpus_allowed_list`, cgroup, DMI 4필드). OMP `bash`/`eval`·파일 읽기·subagent 호출은 없었습니다.
- **두 번째 `terminal` 호출:** 같은 절차를 다시 시도한 것이지 범위 확대가 아닙니다. 다만 첫 결과 detail이 "the host terminal is the user's again"이라고 했기 때문에 재시도는 그 안내에 맞는 행동이었습니다.
- worker는 거부를 사실로 보고하고 멈췄습니다(C-D69 (2) "실패는 사실로 보고하고 멈춤"에 맞음). 우회 시도(파일 쓰기, OMP 도구로 직접 읽기)는 없었습니다.
- **시간:** TASK 도착 17:51:36.9 → 첫 `terminal` 17:52:47.2(**70 s**, 대부분 스크립트를 짜는 데 쓴 생각) → `to_manager blocked` 17:53:22.6(**TASK→보고 105.7 s**). 보고는 `done`이 아니라 `blocked`입니다.
- 비교
  - smoke-01(C-D68) 시나리오 1: worker 8 request, 짧은 명령(`lscpu; free -h; nproc; df -h`) 1회, TASK→done 약 19 s. 중복 done 1회(모델 행동).
  - 이번: worker 5 request, 범위 확대 없음, 중복 보고 없음. 하지만 manager가 넘긴 절차가 훨씬 크고 worker가 섹션별 종료 상태·대체 분기를 넣은 5 KB 스크립트를 짜느라 첫 실행까지 70 s가 걸렸습니다. 사용자가 말한 "늦게 전달"이 이번에는 분석 단계가 아니라 **manager의 무거운 절차 + worker의 스크립트 작성 단계**에서 나타났습니다. 실행 뒤 보고 지연은 측정하지 못했습니다.

### 보고 내용 형태 (짧은 발췌)
- worker `to_manager blocked` message(162자): "…지정된 읽기 전용 Bash 스크립트를 실행하려 했지만 호스트 터미널이 `manager owns the host shell` 상태여서 명령이 실행되지 않았습니다. 실행 결과/하드웨어 사양은 확인할 수 없습니다. …" 사실만 담았고 추가 분석이나 추측은 없습니다. 첫 글자는 한자 `指定`으로 시작했습니다(모델의 표기 혼입, 무해).
- 정상 결과(실행 내용·핵심 출력·실패/누락 + 요약 분석)의 형태는 실행되지 않아 **미검증**입니다.

### manager의 최종 답변 (249자, 요지)
"터미널 권한 문제로 스크립트가 실행되지 않았습니다. Workbench가 `manager owns the host shell` 오류로 실행을 차단했습니다. … Workbench에서 호스트 터미널을 사용자 소유의 유휴 프롬프트 상태로 전환해 주세요. 전환 후 알려주시면 다시 실행하여 …" manager는 직접 실행하지 않았고 사실을 그대로 전했습니다. 그러나 사용자가 할 수 있는 조치가 없습니다. shell은 Workbench 내부 상태에 묶였고, 상태줄과 host pane 제목(`owner=manager`) 외에 사용자가 이를 풀 경로가 보이지 않았습니다.

## 시나리오 2: `/copy` 바이트 증거
| 캡처 | 바깥 출력 bytes | OSC 52 | selection | base64 길이 | 디코드 | sha256(앞 16) | 형태 |
|---|---|---|---|---|---|---|---|
| 1차(TMUX 없음) | 15169 | 1건 | `c` | 696 | 520 B / 249자 | `243b3cdd1c30eb1c` | plain `ESC]52;c;…BEL` |
| 2차(UI env에 TMUX) | 15662 | 2건 | `c`, `c` | 696, 696 | 520 B / 249자 | `243b3cdd1c30eb1c` ×2 | plain 1 + `ESC P tmux; ESC ESC ]52;c;…BEL ESC \`(714 B) 1 |

- manager 마지막 assistant 답변(세션 jsonl의 text 블록): 249자 / 520 B / sha256 `243b3cdd1c30eb1c…`. **디코드 결과와 완전히 같습니다.** 앞부분은 `**터미널 권한 문제로 스크립트가 실행되지 않았습니다.** Workbench가 …`(markdown 원문 그대로)입니다.
- 1차 캡처는 OSC 52 1건뿐입니다. 클립보드 읽기 요청(`?`), DCS, 중복 쓰기는 없습니다. 바로 앞 bytes는 커서 이동(`ESC[40;156H`)이라 sequence가 다른 출력과 섞이지 않았습니다.
- **pane에 base64 없음:** 1차 `/copy` 뒤 화면 전체에서 base64 조각(16자 창)을 찾지 못했습니다. manager pane에는 OMP의 `Copied assistant message to clipboard`, 하단 알림에는 `[MANAGER OMP] 복사됨: 249자`가 보였습니다.
- 2차 캡처에서는 화면 맨 아래(모든 pane 상자 밖, 커서 위치)에 base64가 보였습니다. 이것은 **하네스의 바깥 terminal(pyte)이 DCS tmux passthrough를 이해하지 못해 글자로 그린 것**입니다. Workbench pane 화면 문제가 아닙니다. 실제 tmux나 terminal은 DCS를 소비합니다. 이번에는 private `tmux -L` 서버 안에서 반복하지 않았으므로 실제 tmux의 unwrap·`set-clipboard` 경로는 미검증입니다(packet상 선택 항목).
- 재attach replay에는 OSC 52가 다시 나오지 않았습니다(C-D69 (1) "재attach 때 재생하지 않음"과 맞음).
- **UX 관찰(OMP 동작, 낮음):** `/copy`를 입력한 뒤 Enter 1회로는 명령 목록의 선택만 되고, 2회째에 실행됐습니다(두 번 모두 같음). 사용자의 "엔터로 복사했는데도 복사가 안돼"는 707bcf9 이전에는 OSC 52가 버려졌기 때문이고(copy-01 원인 분석), 지금은 두 번째 Enter 뒤 정상 전달됩니다. 이번 답변에 code block이 없어서 OMP picker는 뜨지 않고 답변 전체가 바로 복사됐습니다.

## 동작한 것 (실모델)
- C-D69 (2) manager 쪽: `to_worker`가 절차 형태이고 `analysis: summary`를 명시했습니다. manager는 위임 뒤 직접 실행하지 않았고 보고를 기다렸습니다.
- C-D69 첫 TASK 전체 message: 1053자 원문이 worker에게 그대로 전달됐고, `analysis_rule`이 함께 왔습니다.
- C-D69 (2) worker 쪽: 지정된 명령과 허용된 대체만 실행하려 했고, 실패를 사실로 보고하고 멈췄습니다. 범위 확대, 자체 조사, 중복 보고는 없었습니다. analyst subagent 호출도 없었습니다(C-D69 (3)).
- C-D69 (1) `/copy`: manager pane의 OSC 52가 바깥 terminal로 전달됐습니다. 내용은 hash까지 일치했고, plain과 TMUX일 때의 plain+wrapped 형태, 알림, pane에 base64 없음, 재attach 때 재생 없음, request 0건을 확인했습니다.
- start/shutdown 정상, isolation ok, residue 0.

## 동작하지 않은 것 / 분류
- **P1 (제품 결함, 높음, 새 발견): 긴 `terminal` 명령이 host shell을 manager 소유로 묶어 둠.**
  - 재현: worker `terminal`의 command 5058자 → `shell_g2/lifecycle.py:259~261` `dispatch_managed`가 `RUN:<base64(JSON)>` 요청이 4096 bytes를 넘으면 `self.lifecycle.fail_unknown("request_too_large")` 뒤 `ValueError`를 던짐 → `flow_terminal.py:815~822`가 `terminal_start_failed`를 기록하고 `_give_back(after_failure=True)`를 시도하지만, 실제 상태는 `owner manager / control_wait`로 남음.
  - 결과 detail("the host terminal is the user's again")이 실제 상태와 다릅니다. 다음 `terminal`은 `host_terminal_busy`로 거부됐고, Task는 `blocked`, host shell은 약 12분 뒤 shutdown까지 풀리지 않았습니다.
  - 실제 상한은 base64(4/3)와 JSON escape 때문에 명령 길이로 대략 3 KB 미만으로 추정됩니다(정확한 값은 미확인). 두 번째 3897자 명령도 상한을 넘었을 가능성이 높지만, ownership 거부가 먼저 일어나 확인할 수 없었습니다.
  - 모델은 이 상한을 알 수 없습니다(도구 설명·skill에 없음). C-D69 이후 manager가 상세한 절차를 넘기면서 worker가 긴 스크립트를 쓸 가능성이 커졌습니다.
  - 수정 후보(Root 판단): `terminal` 입력 단계에서 길이를 검사해 shell을 잡기 전에 명확히 거부하기, 또는 긴 명령을 임시 파일/여러 write로 넘기기. 그리고 `request_too_large` 뒤 lifecycle을 복구해 shell을 user에게 반환하기(최소한 결과 detail을 실제 상태와 맞추기).
- **미검증(P1 때문에):** 하드웨어 결과 보고 형태(실행 내용·핵심 출력·실패/누락 + 요약 분석), 실행 후 보고까지의 시간, manager의 최종 정리. 사용자가 비교하려던 "worker가 자세히 조사해 늦게 보고"의 개선 여부는 실행 단계에 닿지 못해 판단할 수 없습니다. 관측된 것은 범위 확대 0건과 첫 실행까지 70 s입니다.
- **M1 (모델 행동/지침, 중, 관찰):** manager가 사용자의 짧은 요청을 9섹션짜리 상세 절차로 바꿨고, worker는 섹션별 종료 상태 출력과 대체 분기를 넣은 5 KB 스크립트를 70 s 동안 작성했습니다. C-D69 위반은 아니지만(범위 결정은 manager 몫), "늦게 전달"의 원인이 manager 절차 크기로 옮겨 갈 수 있습니다. to-worker skill의 Good 예시(`lscpu`, `free -h`, `lspci | grep -i vga`, `df -h`)보다 훨씬 큽니다.
- **O2 (기존 기록, 낮음):** Task 전달 outbox가 20 s 뒤 `unknown / BridgeTimeout`으로 기록됐습니다. worker의 첫 turn이 70 s 넘게 이어졌기 때문이고, 전달은 1회 정상이었습니다(smoke-03 O2와 같음).
- **표시 관찰(낮음):** manager pane에 OMP 명령 목록이 닫힌 뒤 이전 줄 조각(`…프트 상태로 전환해 주세요.`)이 오른쪽에 남았고, 시나리오 1 뒤에는 답변 두 줄이 한 번 중복돼 보였습니다(timeline `after_s1_manager`, `after_copy`). OMP의 부분 다시 그리기와 pane 화면 사이의 잔상으로 보이며, 이번 범위에서는 원인을 확인하지 않았습니다.

## 상태줄·pane text screenshot (product UI)
```
[17:51:07 attach]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle

[17:53:41 이후 shutdown까지 동일]
focus: MANAGER OMP | host 입력 owner: manager | shell mode: control_wait | worker: 대기 | 자동화: active
작업: 작업 "사용자의 현재 머신 하드웨어 사양을 실제…" [막힘 · worker_blocked] | backend: ready | bridge manager=ok worker=ok | …
┌─ HOST SHELL alive owner=manager ─────…
│$ wb-handoff
```

manager pane(`/copy` 입력 직후, OMP 명령 목록):
```
│╰─ /copy
│❯ ⧉  copy        Pick text or code from the conversation to copy
│  📋 dump        Copy session transcript to clipboard (and write LLM request
│  🌐 open        Open the last link from the conversation in your browser (or
```

manager pane(Enter 2회 뒤):
```
│ Copied assistant message to clipboard
…
[MANAGER OMP] 복사됨: 249자
```

## Residue
- 프로세스: `shutdown --yes --json` exit 0, 소유 프로세스 잔여 0, 강제 kill 없음. 사용 중 tmux 서버는 만들지 않았습니다(TMUX는 UI env 값만 가짜로 넣음).
- 파일: `/tmp/wb-cd69-smoke1-ta5w_xpm` 삭제 확인. scratch의 driver, raw 캡처, 세션 사본, run log는 삭제했습니다. run dir에는 이 파일과 `result-p27-cd69-smoke-01-timeline.json`만 남깁니다.
- 사용자 영역: `~/wb-urux-sandbox`, 사용자 Workbench/OMP/tmux 프로세스, 사용자 클립보드는 건드리지 않았습니다(`DISPLAY`/`WAYLAND_DISPLAY` 제거). credential 내용과 다른 프로세스의 environ은 읽지 않았고 token도 출력하지 않았습니다. 세션 jsonl의 `credential_pin` 레코드는 결과에 옮기지 않았습니다. commit, graphify update, ledger 수정, `src/**`·`omp_bridge/**`·`docs/**` 수정은 하지 않았습니다.

## 권고 (Root 판단용)
1. **P1을 먼저 고쳐야 합니다.** 긴 `terminal` 명령 하나로 host shell이 세션 내내 잠깁니다. 결과 detail도 사실과 다릅니다. C-D69 이후 절차가 길어지면 실제 사용에서 다시 일어날 가능성이 높습니다.
2. P1 수정 뒤 같은 원문으로 smoke를 1회 다시 실행해야 C-D69 (2)의 보고 형태와 지연 감소를 판단할 수 있습니다(사용자 승인 필요).
3. M1(manager 절차 크기)은 지침 조정 후보입니다. 예를 들어 "절차는 사용자 요청에 필요한 최소 명령으로" 같은 문구입니다. 결정은 사용자에게 맡깁니다.
4. `/copy`는 plain 경로와 TMUX env 형태까지 확인했습니다. 실제 tmux 안에서의 확인이 필요하면 private `tmux -L` 서버로 별도 확인할 수 있습니다(request 0건).
