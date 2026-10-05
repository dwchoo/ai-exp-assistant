# p27-cw18-smoke-03 — CW-18 실모델 experiment-flow smoke 결과

- 역할: test_designer (Opus) · 실행: 2026-10-05 10:22:26 ~ 10:23:30 KST (64 s, 1회 실행)
- 결과: **시나리오 3(정지 조건) 발동.** worker의 execute 응답이 **보이는 reasoning(thinking) 텍스트**를 포함해
  bridge에서 거부되었고, run은 준비 단계에서 `start_failed:ValueError`("no validated public worker assistant response")로 끝남.
  assignment와 Root 판정(`root-adjudication-p27-cw18-empty-thinking`)에 따라 재시도하지 않고 중단함.
  host run, 60 s review, judge, cwd 복원, worker_busy는 관측하지 못함.
- provider request: **5/30**(manager 4, worker 1, error/aborted 0). wall-clock 약 1분/20분. cap에는 도달하지 않음.
- 진입점: `python -m workbench start --data-dir /tmp/wb-cw18-smoke-v62mu56h/data --omp ~/.local/bin/omp --no-attach`
  (project dir에서 실행, 실제 HOME, Workbench-owned OMP home, `agent.db` symlink 확인, store는 열지 않음).
  product UI(`workbench attach`)를 PTY(pyte 170x40)에 붙였고, manager pane에 타이핑해 입력함. 상태는 `workbench status --json`으로 관측함.
  repo는 HEAD 856fb08(smoke-fix-02 포함, c3d50e7)이며 smoke 관련 미커밋 코드 변경은 없음.
- 두 OMP 모두 provider `openai-codex`, 모델 GPT-5.5(OMP footer 기준), omp/18.4.5, isolation `ok`(leaks/warnings 0).
- 드라이버: smoke-02의 `smoke.py`를 고친 scratch `smoke3.py`(p27w의 `Ui`/`processes_mentioning`/`kill_exact` 재사용,
  0.5 s 간격 cap watcher 30 req/20분). request 수는 temp dir 안의 Workbench home 세션 jsonl에 있는 assistant message 수로 셌음.
  종료 후 우리 temp DB(`tasks.sqlite3`, read-only)와 세션 jsonl의 **content type 형태만** 추출함(텍스트는 저장하지 않음).
- temp project: git repo(`exp.sh`: `sleep 70` 후 `out/result.txt`에 RESULT=ok를 쓰고 출력함. `out/.keep`과 `work/.keep` 포함).
- 짧은 로그: `result-p27-cw18-smoke-03-timeline.json`(secret 없음)

## Timeline (KST)

| 시각 | req | 관측 |
|---|---|---|
| 10:22:28 | 0 | start exit 0, phase ready, isolation ok, providers manager/worker=`openai-codex`, `agent.db` symlink. 소유 프로세스: backend, bash, omp×2(모두 cwd=project) |
| 10:22:29 | 0 | product UI attach, `worker: 대기 · 자동화: idle` |
| 10:22:31 | 0 | 1단계 지시 입력(experiment, `./exp.sh`, bash, environment none, criteria 세 항목 모두 지정) |
| 10:22:33 | 1 | manager가 `read`(skill) |
| 10:22:38.956 | 2 | **첫 native `to_worker`가 바로 수용됨**(`task_id/run/cancel=null`, `spec` dict). scope_approved와 proceed 결정 기록, run `started` |
| 10:22:38.984~988 | — | execute TASK가 worker에 delivery됨: `attempted` → `api_returned(api_accepted)` |
| 10:22:40 | 3 | manager가 한 줄 보고 후 turn 종료. 상태줄 `worker: 작업 중 · [시작 중]` |
| 10:22:45.386 | 4 | worker의 assistant message 1개(stop `stop`). content = **[`thinking`(보이는 텍스트 48자), `text`(WB_WORKER_RESPONSE marker + `decision:"execute"`)]** |
| 10:22:45.391 | — | delivery `omp_processed`(`provider_request_matched:true`, `provider_response_observed:true`, `agent_end_observed:true`) → **smoke-02 E1(identity) 수정이 실제 codex WS에서 동작함** |
| 10:22:45.395 | — | shell_event `failed`(`shell_state: preparation_failed`, `preparation_error: ValueError no validated public worker assistant response`, `worker_request.status: omp_processed`), run_event `failed`(stage `preparation`). host shell에는 아무것도 입력되지 않음 |
| 10:22:45.402 | — | **E3 notice:** `run_start_failed` REPORT가 manager에 delivery됨(`api_accepted` → 10:22:48 `omp_processed`) |
| 10:22:46 | 4 | Task `finished`, `held_reason=start_failed:ValueError`, worker idle, 자동화 idle. 상태줄 `[시작 실패 · ValueError]`(E4 라벨 수정 확인) |
| 10:22:48 | 5 | manager가 실패를 받아 사용자에게 요약함("Experiment did not start … worker is free … re-run after the issue is fixed"), turn 종료 |
| 10:22:47~10:23:25 | 5 | 추가 request 없음. host shell: owner user, `manual_prompt`, phase `not_sent`, cwd=project(변경 없음) |
| 10:23:26 | 5 | `shutdown --yes` exit 0, `verified:true`, 세 pane dead, survivors 없음 |
| 10:23:30 | 5 | 소유 프로세스 잔여 0, temp root 삭제 |

### Provider request 수
| OMP | assistant request | content 형태(텍스트 제외) |
|---|---|---|
| manager | 4 | ① `thinking`(보이는 텍스트 40자)+`toolCall:read` ② `toolCall:to_worker` ③ `text` ④ `text`(start 실패 notice 요약) |
| worker | 1 | ① `thinking`(보이는 텍스트 48자)+`text`(marker JSON, `decision:"execute"`) |
| 합계 | **5** | error/aborted 0 |

## 거부된 content type (Root가 설정을 결정할 때 참고)

- worker 응답 assistant message의 content 배열: `[{type:"thinking", thinking:<비어 있지 않은 문자열 48자>}, {type:"text", text:"WB_WORKER_RESPONSE:{...}"}]`.
  세션 jsonl에 저장된 thinking item의 key는 `type`과 `thinking`뿐이며 `thinkingSignature`는 없음
  (bridge의 `message_end` event에 signature가 있는지는 관측하지 않음. 어느 쪽이든 `thinking`이 비어 있지 않으므로 결과는 같음).
- worker pane에도 이 reasoning summary가 한 줄로 보였음(내용은 응답 생성에 대한 짧은 메타 문장이며 여기에는 옮기지 않음).
- bridge 판정(코드 대조): `isOpaqueThinking()`은 `thinking===""`일 때만 무시하므로 이 item은 남고, `rest.length===2`가 되어
  `workerResponseRejected="invalid_assistant_response"` → backend `worker_port.py:75`가 `ValueError`를 냄. marker JSON 자체에는
  `decision:"execute"`와 identity 필드가 모두 있었음. 즉 **거부 원인은 identity나 marker가 아니라 visible reasoning summary뿐임.**
- manager OMP도 reasoning summary를 냄(40자). OpenAI codex 경로에서 GPT-5.5의 reasoning summary가 켜져 있는 것으로 보임.
  smoke-fix-02 capture(빈 thinking + signature)와 다른 형태인데, 요청에 따라 summary 유무가 달라지는 것으로 **추정**함(미확인).

## 동작한 것
- 진입점, isolation, 공유 login, 두 OMP의 실모델 호출, 확인된 shutdown, residue 0.
- **E1 수정 확인:** codex WS에서 worker delivery와 manager notice delivery가 모두 `omp_processed`(`provider_request_matched:true`,
  message_end를 response evidence로 사용). smoke-02처럼 `unknown`으로 떨어지지 않음.
- **E2 확인:** criteria 세 항목을 지정하자 첫 native `to_worker` 호출이 수용됨(rejected 0).
- **E3 확인:** TASK 생성 후 start 실패가 `run_start_failed` notice로 manager에게 능동 전달되었고, manager는 이를 사용자에게 정확히 알림.
- **E4 확인:** 상태줄 `[시작 실패 · ValueError]`.
- 실패 시 안전성: host shell에는 아무것도 입력되지 않음(phase `not_sent`, owner user, manual_prompt, cwd 변화 없음).
  재전송이나 자동 재시도도 없음. worker idle, 자동화 idle로 돌아감.
- 모델 행동: manager는 skill read 1회 후 바로 정확한 호출을 했고, 실패 후 재시도하지 않고 turn을 끝냄(과소비 없음).

## 동작하지 않은 것 / 분류
- **F1 (설정/계약 결정 필요, 차단): openai-codex GPT-5.5의 visible reasoning summary 때문에 staged worker 응답이 항상 거부될 가능성이 높음.**
  Root 판정은 이 경우 "더 완화하지 말고 사용자와 설정 변경(예: worker reasoning summary off)을 결정"하라고 함 → 사용자 결정 필요.
  선택지 예: (a) Workbench-owned worker OMP 설정에서 reasoning summary/effort를 끔(OMP 설정 key는 미확인),
  (b) 계약을 바꿔 leading thinking을 내용과 무관하게 무시함(no_thinking 규칙 완화, Root 판정과 충돌).
  판정상 product defect보다는 **provider 설정과 계약의 불일치**로 분류함.
- **F2 (product 관측성, 낮음):** 거부 사유(`invalid_assistant_response`)가 delivery details, run/shell event, Task `last_result`
  어디에도 남지 않음. 모두 일반 `ValueError`로만 기록되어 원인을 알려면 세션 content를 봐야 함. bridge의 reason 코드를 run failure details까지 전달하는 것을 권장함.
- **미검증(F1 때문):** Workbench의 run 입력, host shell idle 확인 후 실행, 60 s worker review, judge success, report 전달, shell 반환과 cwd 복원,
  worker_busy. 2단계는 시도하지 않음(시도 시점에 이미 Task가 `finished`이고 worker가 idle이어서 busy 구간이 없었음).

## 상태줄 text screenshot (product UI 상단 2줄)

```
[attach 10:22:29]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
backend: ready | bridge manager=ok worker=ok | 마지막 확인 10:22:29 (0s 전) | Ctrl-] ? 도움말

[dispatch 직후 10:22:43]
focus: MANAGER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 작업 중 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the provided suc…" [시작 중] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 10:22:43 (0s 전) | Ctrl-] ? 도움말

[start 실패 후 10:22:45, host focus]
focus: HOST SHELL | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the provided suc…" [시작 실패 · ValueError] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 10:22:45 (0s 전) | Ctrl-] ? 도

[최종 10:23:24, worker focus]
focus: WORKER OMP | host 입력 owner: user | shell mode: manual_prompt | worker: 대기 | 자동화: idle
작업: 실험 "Run ./exp.sh and judge the provided suc…" [시작 실패 · ValueError] | backend: ready | bridge manager=ok worker=ok | 마지막 확인 10:23:24 (0s 전) | Ctrl-] ? 도

[OMP footer] manager: π > ◒ GPT-5.5 > … > S0.12 ▶─4%   worker: π > ◒ GPT-5.5 > … > S0.05 ▶─3%
[HOST SHELL pane] "$" 프롬프트만 있음(Workbench 입력 없음)
```

## Residue
- 프로세스: `shutdown --yes` exit 0, `verified:true`. 소유 프로세스 잔여 0(강제 kill 없음), `pgrep` 결과도 0.
- 파일: `/tmp/wb-cw18-smoke-v62mu56h`는 삭제 확인(worktree `data/workflow/worktrees/...`도 temp root 안에 있었으므로 함께 삭제됨).
  scratch의 screen, DB 형태 추출본, driver는 삭제함. run dir에는 이 파일과 `result-p27-cw18-smoke-03-timeline.json`만 남김.
- 사용자 영역: `~/wb-urux-sandbox`와 사용자 프로세스는 건드리지 않음. credential 내용과 다른 프로세스의 environ은 읽지 않았고
  token도 출력하지 않음. commit 없음, graphify update 없음, ledger는 수정하지 않음.

## 권고 (Root 판단용)
1. **F1 결정이 선행 조건:** worker OMP(Workbench-owned home)의 reasoning summary를 끌 수 있는지 확인(OMP 설정 key와 openai-codex의
   `reasoning.summary` 전달 여부)하고, 사용자에게 설정 변경을 확인받은 뒤 같은 시나리오를 다시 실행할 것(예상 약 10~15 request).
2. F2: bridge 거부 reason을 run failure details와 notice에 남길 것(작은 수정).
