# p27-cw16-c18-formal-01 — CW-16 정식 check: 동결 후보 C18 (test_designer, R 단계)

작성 2026-10-09. 후보 C18 `a2e3daf2…3482b`(HEAD `c47456a` + 작업 트리의 docs 초안 2개). C16 절차(result-p27-cw16-b4b-01.md)를 -c18 id로 반복했고 gap-c18을 추가했다. 최종 gate는 실행하지 않았다.

## 지킨 제약
- repo 작업 트리 쓰기 0: 쓴 곳은 run dir(`check-p27-cw16-*-c18*`, `cw16-formal-c18/**`, 이 파일)과 `/tmp`뿐이다. `git status`의 src/omp_bridge/tests/pyproject는 비어 있었고 docs 2개 변경은 시작 전부터 있던 C18의 일부다.
- snapshot(`cw16-formal-c18/snapshots/*.json` 13개, exclude는 run dir·graphify-out, checkpoint false)은 각 check 전후와 마지막에 모두 C18과 같았다.
- 실행 방식: 전부 foreground 또는 `run_in_background`로 직렬 실행했다. nohup·`&`는 쓰지 않았다. 러너 셸의 SigIgn은 `0x1000`(SIGPIPE만, SIGINT/SIGQUIT 무시 없음) 또는 0이었다.
- 모델/provider 요청 실제 0(scripted local provider). 자격 증명·타 프로세스 environ 미접근. 모든 argv는 `env -i`(TMUX*/HERDR_* 제거)이며, regression·gap은 fake HOME + proxy 차단이다.
- 사용자 기본 tmux/herdr, 다른 claude-swarm socket, `~/wb-urux-sandbox`는 건드리지 않았다. pkill 미사용. 내가 띄운 PID만 있었고 종료 시 잔존 프로세스·`/tmp/wbc16*`·`/tmp/wb-cw16-c18`은 없다.
- VM(`~/.local/share/wb-vm/wb-reboot`)은 fault 안의 X5 driver가 시작·guest만 reboot 2회·`stop.sh` 종료(rc 0, qemu 없음, disk 보존)했다.
- commit·graphify update·docs 수정 없음. 테스트·src 수정 없음.

## 1. check 결과
공통: `workflow_tools.py check`, requirements identity `1be5eecb…`, watch [], timeout 3600, 모든 check에서 timed_out false, stable true, before = after = C18.

| check_id | exit | 시간(s) | log sha256 |
|---|---|---|---|
| p27-cw16-compat-c18 | **1** | 797 | `e93bdddc66dee880dee7a07c76e4ae8ade53aea7de0c8df15ae2874a70349e19` |
| p27-cw16-compat-c18-r2 (전체 재실행) | 0 | 815 | `07b32a4e293fa10f0483d75601656951847c4df830dc954ed462c409df1226d9` |
| p27-cw16-flow-policy-shell-c18 | 0 | 1129 | `238d01f0fcd5b6ce4bfd6548446c95970b5dd77cfe2f3d560ed702c0e93a971c` |
| p27-cw16-fault-c18 | 0 | 1262 | `f41363231f97b9079b3d7ad5c921396a381bdbab232d4062c7d4f5dfb4c2b94e` |
| p27-cw16-gap-c18 | 0 | 144 | `92df60401af2d8b78105af5ceb473f1d7af364d981cbbce2984ae96cb5913963` |
| p27-cw16-regression-a-c18 | 0 | 1178 | `3153a31bf10af8fda7dfc48e4027e7a7eaab5b0efe171ee71138b54186fa03f5` |
| p27-cw16-regression-b-c18 | 0 | 1125 | `9848f29e0880c6fa55f1864c58ec6ff6f07a21ae9a995a117513c31e32f2e7bc` |

argv는 각 `check-p27-cw16-<id>-c18-request.json`(C17 request를 템플릿으로, check id/log/report dir/후보 문구/X5 aggregate만 변경)에 있다. 산출물: `…-request.json`, `…-execution.json`, `.log`.

### compat-c18 첫 실행 실패와 r2 (Root 판단 필요)
- 첫 실행(report `cw16-formal-c18/reports/cw16-matrix-5ba4d64c/`): 비live 17건 OK(skip 1). live 6셀 중 **plain/bash, plain/dash, tmux/dash가 C2 fail**, herdr 2셀·tmux/bash는 pass, SEL1–3 pass.
- 실패 단정: `('manager_omp', 'esc_closed_menu', {… 'esc_closed_menu': False, 나머지 typed_visible/alt_enter_newline/ctrl_c_cleared/slash_menu/hangul_composer/alive 모두 True})`. 즉 manager OMP slash menu가 Esc로 닫히지 않은 화면이 관측됐다. worker는 이 지점까지 가지 않았다.
- 알려진 intermittent 목록에 없는 새 후보다. 같은 argv로 전체 재실행한 r2(report `cw16-matrix-984764a2/`)는 6셀 + SEL1–3 모두 pass(exit 0)였다. 두 로그와 execution을 모두 남겼다.
- 해석: 3/6 셀만 실패하고 같은 코드의 재실행이 6/6 통과해, 결정적 회귀보다 Esc 후 화면 timing flake에 가깝다. 다만 원인은 조사하지 않았다. 판정은 Root 몫이다.
- observed(compat)는 r2 execution에 items를 붙였고 evidence에 첫 실행 실패를 명시했다.

### regression 상세
- regression-a: backend 1127(전체 exit 0, 이번엔 전환된 live 29건 포함), ui 843, ui/status_workbench 2 — 모두 OK. C16에서 실패했던 KillScope·RestartExitedOmp도 통과.
- regression-b: terminal 221, recovery_boot 184, integration 24, lifecycle 38, observation 95, storage 33, tasks 22, workflow 62, contracts 52, bridge(py) 52, g1_vt 85, g2_shell 142, g3_omp 117, g4_evidence 10, g4_lifetime 8, pause_automation 129, recovery_manager 26 — 모두 OK. node bridge rc 0, run-contracts rc 0(py 52).
- regression 재실행은 필요 없었다(exit 0).

### gap-c18 (`live_cw16_gap.py`, §6-B 명령, fake HOME)
4건 OK(139 s): COLOR pass, SCROLL pass, COMPOSER pass, SESSION pass. 보고서 `cw16-formal-c18/reports/c18-gap/gap-*.json`. gap-G1-COLOR가 이번에는 pass다(gap-01에서는 실패였음). 실행 기록만 만들었고 observed는 만들지 않았다.
참고: request에 지정한 `WB_LIVE_OMP_VERSION_RECORD`(`reports/gap-omp-version.jsonl`)는 이 모듈이 쓰지 않아 파일이 생기지 않았다. 버전은 각 보고서 `versions`에 omp/18.8.0으로 있다.

## 2. fault 세부 (I-FAULT)
- X1 7/7, X2 9/9, X2R 56/56 checks(provider 요청 0), X3 8/8, X4 4/4, X5 1/1, X6, X7 모두 pass. G3 version-record `omp/18.8.0`, broker wait remaining [] 3/3 OK.
- **X5 VM 재실행(재사용 아님)**: manifest(`src/** omp_bridge/** tests/recovery_boot/**`) host = guest = **C18 aggregate `6eeed2425de99a74c657efa1e09fcf30c5bab1bd4669169ce72067cf16fcd0b2`(93 files)**, 실행 뒤 host 값 불변. 이 값은 실행 전에 driver의 manifest 함수로 계산해 request에 기록했고 실제 결과와 일치했다. driver sha256 `46182ff7…`.
- guest reboot 2회: boot_id `3ada806f`→`0e23a1ea`→`812e3a91`, QEMU pid 동일. 28/28 checks, VM 종료 rc 0.
- C16 때 관찰한 `tool_no_result`(열린 Task 상태에서 worker terminal 45초 무응답)는 이번에도 재현되지 않았다(after-confirm terminal 2건 exit 0).
- X4/X7의 `residue_before_fallback`은 시나리오가 의도한 survivor이며 fallback 뒤 `{}`다.

## 3. 7개 item observed 초안
형식은 execution JSON + `items`(mkobs.py). 7개 모두 `passed`, `runtime`, `unknowns []`.

| observed 파일 | sha256 | item |
|---|---|---|
| `check-p27-cw16-compat-c18-observed.json` | `377fc84aa8164e1ce43d8669b563531e1d3a35b4e90295a20a2ec3a99523c3a8` | P-C-AC-19, P-C-AC-20, I-COMPAT (원본 execution = compat-c18-**r2**, check_id `p27-cw16-compat-c18-r2`) |
| `check-p27-cw16-flow-policy-shell-c18-observed.json` | `a77ce0df9272a282aa83f65a15fc8786ec92af7d1e615fdea8a76108c5f04d28` | I-FLOW, I-POLICY, I-SHELL |
| `check-p27-cw16-fault-c18-observed.json` | `75d8d5065c221cbe66305882476f4b18cb1ce0ca0faa524d7f56575487547807` | I-FAULT |

item 문안은 `cw16-formal-c18/items-*.json`. C16 문안에서 후보·보고서 경로·boot id·manifest·compat 시간 범위(65.7–65.9 s)를 C18 값으로 바꿨고, C18 보고서에서 다시 확인하지 못한 세부 시간 값(186.3 s, 0.4 s, 85 s, 62 s 등)은 문장에서 뺐다. flow/policy/shell의 시나리오별 결과·주요 값(60.7 s 첫 review, retry_limit·run_finished_while_paused·'사용량: 재시도' 문구, exit 143 등)은 C18 보고서에서 재확인했다.
- 시나리오 구성은 C16과 동일하다: flow F1/F4, F2, F3, F45, F5, F6/F7, policy P1/P7, P2–P6, P8, CLK(17 모듈), shell SH1–SH5 bash·dash 전부 pass, 각 shutdown verified, residue {}.

## 4. unknown / Root 판단 항목
1. **compat-c18 첫 실행의 C2 esc_closed_menu 실패**(위). 새 intermittent 후보. r2는 pass.
2. C16 B4b와 동일하게 남는 항목: queue_full paste 거절은 live matrix에서 미실행(fixture·regression-a에서 통과, L-CW17-CONTRACT), herdr 0.9.3의 ~1 MiB 초과 paste 미전달(n/a), retry 한도 "근거 보고"의 별도 자동 요약 없음, SH5 사유가 hook 아닌 `manual_input`, OMP 승인 창(C-D72 (4)) 미관측, VM guest UI의 `OMP 격리 failed`는 fake OMP 때문.
3. gap-c18 `WB_LIVE_OMP_VERSION_RECORD` 파일 미생성(위).
4. 이번 run에는 docs 초안이 후보에 포함돼 있어, 이후 docs가 바뀌면 후보 identity도 바뀐다.

## 5. 파일
`check-p27-cw16-{compat,flow-policy-shell,fault,gap,regression-a,regression-b}-c18*` 및 `check-p27-cw16-compat-c18-r2*`, `cw16-formal-c18/{snap.sh, mkobs.py, items-*.json, snapshots/, reports/}`. C17 파일은 덮어쓰지 않았다.
