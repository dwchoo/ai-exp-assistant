# p27-cw16-b4b-01 — CW-16 B4b: 동결 후보 C16 정식 check (test_designer, R 단계)

작성 2026-10-08. 후보 C16 `f8ad6ec9…f4e3d`(HEAD `e635723`). 정식 check 5개를 직렬로 실행했다. 최종 gate는 Root 몫이라 실행하지 않았다.

지킨 제약:
- repo 작업 트리 쓰기는 0이다. 쓴 곳은 run dir(`check-p27-cw16-*`, `cw16-formal/**`, 이 파일)과 `/tmp`뿐이다.
- `git status --short -- src omp_bridge tests docs pyproject.toml`는 시작부터 끝까지 비어 있었다.
- 새 snapshot(exclude는 run dir과 `graphify-out/**`, checkpoint false)은 각 check 전후와 마지막에 모두 C16과 같았다. snapshot 파일은 `cw16-formal/snapshots/*.json` 11개다.
- 실제 모델·provider 요청은 0이다. host는 scripted local provider만 썼다(보고서 `real_model_requests`/`model_requests_real` 모두 0). X2R endpoint 요청도 0이고, VM은 fake OMP를 썼다.
- 자격 증명 저장소와 다른 프로세스의 environ은 읽지 않았다.
- 모든 argv는 `env -i`로 시작했다. 따라서 `TMUX*`/`HERDR_*`가 자식에 전달되지 않았다. tmux/herdr는 harness가 sandbox 아래의 격리 socket·config로만 띄웠다.
- 사용자 기본 tmux/herdr, `~/wb-urux-sandbox`, 사용자 OMP에는 접근하지 않았다. pkill은 쓰지 않았다.
- 재부팅은 VM guest에서만 2회 했다. VM은 driver가 `stop.sh`로 종료했다(rc 0, qemu 없음, disk 보존).
- 끝난 뒤 잔존 프로세스와 `/tmp/wbc16*`, `/tmp/wb-cw16-b4b-*`는 없다.
- commit과 graphify update는 하지 않았다.

## 1. 정식 check

공통 사항:
- 실행 방식: `python .agents/skills/workflow-ledger/scripts/workflow_tools.py check --input check-p27-cw16-<id>-request.json --output check-p27-cw16-<id>-execution.json`
- 위치: root는 repo, requirements는 `requirements-p27-cw16.json`(identity `1be5eecb…`)이다.
- 설정: watch `[]`, exclude는 p27 정책과 같고, timeout은 3600이다.
- 모든 check에서 timed_out false, stable true, before = after = C16이다.
- 각 check의 정확한 argv는 해당 request/execution JSON에 있다.

| check_id | 내용 (argv 요지) | exit | 시간 | log sha256 |
|---|---|---|---|---|
| p27-cw16-compat-01 | B1 §6. `env -i PATH=~/.local/bin:/usr/bin:/bin HOME=$HOME …` 아래에서 (1) 비live `test_cw16_runtime_matrix.py`+`test_cw16_harness.py`(17건 OK, opt-in 1 skip), (2) `WB_LIVE_CW16=1 … test_product_path_outer_matrix`(COMPAT 6회 + SEL1–3, 792.7 s OK) | 0 | 808 s | `ee4033a5…f0f4d9` |
| p27-cw16-flow-policy-shell-01 | B2 §7. `live_cw16_flow_policy.py`(14건 OK, 863 s), `live_cw16_shell.py`(11건 OK, 268 s) | 0 | 1137 s | `5576a1c0…a2aa76` |
| p27-cw16-fault-01 | B3 §5. `WB_CW16_VM=1 live_cw16_fault.py`(X1–X4, X6, X7, X2R, X5 VM: 8건 OK, 1222 s). 이어서 fake HOME·proxy 차단 상태에서 G3 `test_tui_extension_fault_integration.py`(3건 OK) | 0 | 1262 s | `f604d47a…eeddda` |
| p27-cw16-regression-a-01 | fake HOME·proxy 차단. `discover -s tests/{backend, ui, ui/status_workbench}` | **1** | 883 s | `2cbc9c0c…adbc8b5` |
| p27-cw16-regression-b-01 | 같은 env. `discover -s tests/{terminal, recovery_boot, integration, lifecycle, observation, storage, tasks, workflow, contracts, bridge, gates/g1_vt, g2_shell, g3_omp, g4_evidence, g4_lifetime, policy/pause_automation, policy/recovery_manager}`, `node --test tests/bridge/*.test.ts`, `sh tests/gates/harness/run-contracts.sh` | 0 | 1068 s | `d1d2729b…07f3d15` |

regression은 3600 s 상한 때문에 a와 b 두 check로 나눴다. `tests/gates/harness`에는 `test_*.py`가 없어 `run-contracts.sh`로 대신했다. `tests/ui/status_workbench`는 상위 discover가 내려가지 않는 하위 디렉터리라 따로 실행했다.

환경(기록이며 pin 아님):
- OS·런타임: Linux 7.0.0-30-generic, Ubuntu 24.04.4, Python 3.12.3 venv `/tmp/cw02-g1-venv`(pyte), Node v22.23.2
- 도구: OMP `omp/18.8.0`(`~/.local/bin/omp`), Bash 5.2.21, dash 0.5.12-6ubuntu5, tmux 3.4, Herdr 0.9.3
- VM guest: Ubuntu 24.04.5, kernel 6.8.0-142, guest OMP 18.2.10(probe 전용)

## 2. Suite 결과 (regression)

| suite | 결과 |
|---|---|
| backend | 1114건 중 **1 FAIL**(skipped 30, expected failure 1) |
| ui | 842건 중 **1 FAIL** |
| ui/status_workbench | 2 OK |
| terminal | 221 OK (skip 2) |
| recovery_boot | 170 OK |
| integration | 24 OK (skip 2: opt-in live matrix, `FinalRecordsTests`는 `WB_CW16_FINAL_REQUEST` 없음. pass로 세지 않음) |
| lifecycle | 38 OK |
| observation | 95 OK |
| storage | 33 OK |
| tasks | 22 OK |
| workflow | 62 OK |
| contracts | 52 OK |
| bridge(py) | 52 OK (skip 1) |
| g1_vt | 85 OK |
| g2_shell | 142 OK (skip 1) |
| g3_omp | 117 OK |
| g4_evidence | 10 OK |
| g4_lifetime | 8 OK |
| pause_automation | 129 OK |
| recovery_manager | 26 OK |
| node bridge | 48/48 pass |
| run-contracts | py 52 OK, node 15/15 pass |

### 실패 2건과 단독 재실행

두 건 모두 계획 §6의 알려진 intermittent 목록에 없다. 그래서 새 intermittent 후보로 기록한다. 테스트는 고치지 않았다.

재실행 방식:
- 정식 check 밖에서, 같은 env(`env -i`, fake HOME, proxy 차단)로 각 테스트를 하나씩 4회 단독 실행했다.
- 4회 모두 OK였다. 로그는 `cw16-formal/reruns/rerun-p27s-kill.log`, `rerun-product-pty-restart.log`에 있다.
- 재실행 뒤에도 snapshot은 C16과 같았다(`snapshots/rega-after-and-reruns.json`).

1. `tests/backend/test_shell_kill_restart_independent_p27s.py` `KillScopeTests.test_kill_ends_every_job_kind_and_group_including_signal_ignoring_and_respawning_members`
   - 실패 내용: `not every session member was signalled: {2092833, 2092834}`
   - 같은 테스트의 앞선 단정(모든 job 종료, session·group 잔존 0, survivors [])은 모두 통과했다. 실패한 것은 마지막 "kill 전 session 집합 ⊆ signalled" 단정뿐이다.
   - 추정: kill 직전 집합에 들어 있던 짧게 사는 프로세스가 신호 전에 스스로 끝나는 시점 race로 보인다. 전체 suite 부하에서만 생기는 것으로 추정한다.
   - 재실행 4/4 OK
2. `tests/ui/test_product_pty.py` `RestartExitedOmpPtyTests.test_exited_manager_shows_the_notice_and_enter_starts_a_new_session_that_takes_input`
   - 실패 내용: `'manager' != 'worker'`
   - 테스트는 "manager가 먼저 등록된다"는 순서를 가정한다. 이번에는 fake OMP 기록 첫 줄이 worker였다.
   - 추정: spawn 순서 race(테스트 가정)로 보인다.
   - 재실행 4/4 OK

Root 판단이 필요하다. 이 결과대로 regression-a를 intermittent로 귀속할지, 아니면 regression-a를 정식으로 다시 실행할지 정해야 한다. 다시 실행해도 후보는 바뀌지 않으며 약 15분 걸린다.

## 3. 7개 gate item 관측 (observed 초안)

observed 파일 형식은 execution JSON 전체에 `items`를 채운 것이다(p2.6 `check-cw16-final-observed.json`과 같다).
- 7개 item은 모두 `result: passed`, `observed_evidence_level: runtime`, `unknowns: []`로 기록했다.
- 실제 OMP 18.8.0, scripted provider, C16 제품 진입점과 UI로 실행한 결과다.
- 관측하지 않은 item은 넣지 않았다.
- regression check는 7개 item의 runtime 관측이 아니라서 observed를 만들지 않았다.

| observed 파일 (sha256) | item |
|---|---|
| `check-p27-cw16-compat-observed.json` (`57da9d17…60f01f`) | P-C-AC-19, P-C-AC-20, I-COMPAT |
| `check-p27-cw16-flow-policy-shell-observed.json` (`5ba6602e…e8322f`) | I-FLOW, I-POLICY, I-SHELL |
| `check-p27-cw16-fault-observed.json` (`c721e4bd…6d54f`) | I-FAULT |

item 문안은 `cw16-formal/items-*.json`에도 있다. 보고서는 `cw16-formal/reports/`에 있다(`cw16-matrix-2e17e11f/`, `b4b-flow-policy/`, `b4b-shell/`, `b4b-fault/`, `b4b-fault/vm/`).

### compat check (P-C-AC-19, P-C-AC-20, I-COMPAT)

outer {plain, tmux, herdr} × host {bash, dash} 6회 모두 pass다. plain의 C6만 n/a로, outer 영속 계층이 없기 때문이다.
- **C5**: UI detach를 65.6–65.8 s 유지한 뒤 attach했다. backend, OMP 2개, shell, supervisor의 identity가 같았고 loop 출력이 3→68줄로 계속 쌓였다. owner와 Task는 그대로였고, provider 요청 증가와 중복 입력은 없었다.
- **C7**: shutdown이 verified였고, `residue_before_fallback` {}, root 제거까지 확인했다.
- **SEL1–3**: 모두 pass다.
  - SEL1: SHELL이 가짜 zsh여도 bash가 선택됐다. SHELL 값은 유지됐고 sentinel은 실행되지 않았다.
  - SEL2: bash가 없으면 dash가 선택됐다.
  - SEL3: bash와 sh가 모두 없으면 exit 2와 안내가 나오고 backend는 0개다.
- 실제 zsh login 검증은 C-D72 (1)에 따라 범위 밖이다.

### flow-policy-shell check (I-FLOW, I-POLICY, I-SHELL)

모든 시나리오가 pass다.
- **flow·policy**: F1/F4, F2(4사례 + 없는 commit), F3, F45, F5(C-D72 (3) 3경계), F6/F7, P1/P7, P2, P3, P4, P5, P6, P8, CLK(17 모듈 fixture)
- **shell**: SH1–SH5, bash·dash 모두
- B2 당시 결함이었던 D-B2-1/2/3은 C16에서 모두 통과했다.
  - SH1 `declared_env_exported_after_start`: bash·dash 모두 pass
  - P2 resume: `run_finished_while_paused … reconciled without replay`
  - P7: status와 UI 상태줄에 `사용량: 재시도 0/3 · 점검 N · 모델 미확인`

### fault check (I-FAULT)

X1 7/7, X2 9/9, X2R 56/56 checks, X3 8/8, X4 4/4, X5 1/1(VM 28/28), X6 9/9, X7 5/5다.
- B3 당시 finding F1·F2는 C16에서 통과했다.
  - F1: verified 시점에 Workbench OMP broker가 0개였다(`gone_after_seconds 0.03`).
  - F2: 170열 화면에 `저장 오류`가 보였다.
- G3 version-record는 `omp/18.8.0`이고, broker wait의 remaining은 []였다(3/3 OK).
- X4와 X7의 `residue_before_fallback`는 의도된 것이다. 시나리오가 일부러 남긴 `setsid -w` survivor와, unverified 종료 뒤 남은 옛 shell이다. 정확한 신원 fallback으로 정리했고 `residue_after_fallback`는 {}다.

**X5 VM 재실행 (재사용 아님)**
- manifest(`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`):
  - host = guest = **C16 aggregate `514dd5c6b357d2f0ddf9b76d23fb76e5ea457801ba7b067c01f69e53860f9166`(91 files)**
  - 이 C16 aggregate는 C16 snapshot의 파일 sha256에서 driver와 같은 식으로 미리 계산해 두었다. 실행 결과가 이 값과 같았고, 실행 뒤 host 값도 바뀌지 않았다.
  - B3 자기 실행 때의 `b12a44c6…`와 다르다. fix-01/02가 src를 바꿨기 때문이다.
- guest reboot은 2회 했다. boot_id는 `4fe4e402`→`44645188`→`45510fb0`으로 바뀌었고, QEMU pid는 같았다.
- phase rc는 모두 0이다. VM은 종료됐다.
- B3 때 남은 관찰 1건(confirm 뒤 열린 Task가 있을 때 worker `terminal`의 45초 무응답)은 이번에 `tool_no_result` 이벤트 없이 재현되지 않았다. after-confirm terminal 2건은 모두 `exited 0`이었다.

## 4. 남은 unknown과 Root 판단 항목

observed에 unknowns로 넣지 않은 것과 그 이유를 적는다. 각 항목을 판정 근거로 받아들일지는 Root가 정한다.
1. **regression-a exit 1**: §2의 새 intermittent 후보 2건이다. 테스트는 고치지 않았다.
2. **C-AC-19의 queue_full paste 거절**(child가 입력을 읽지 않는 경우)
   - live compat matrix에서는 실행하지 않았다. 2 MiB 초과 거절은 plain·tmux에서 runtime으로 관측했다.
   - 근거는 fixture 수준이다(`tests/ui/test_product_pty_independent.py` `test_backend_queue_full_and_owner_refusals_are_visible`, `test_product_paste_independent.py` `test_queue_full_paste_refusal_is_shown`, regression-a에서 통과).
   - PLAN의 composition item은 L-CW17-CONTRACT다.
   - P-C-AC-19와 I-COMPAT의 evidence 문안에 이 사실을 적었다. 이를 unknown으로 올릴지는 Root가 판단한다.
3. **herdr 0.9.3의 약 1 MiB 초과 paste 미전달**(B1 O1): 이 경로에서는 제품의 2 MiB 거절에 도달할 수 없다(n/a). COMPATIBILITY 제약 후보다.
4. **C-AC-07 retry 한도의 "근거 보고"**(B2 O5)
   - 4번째 재실행 요청은 manager에게 `held/retry_limit`(runs_started 4)로 돌아간다.
   - 별도의 자동 요약 보고는 없다. run별 판정 보고는 manager에 전달되어 있다.
   - I-POLICY evidence에 적었다.
5. **SH5 사유 문구**(B2 O1): PROMPT_COMMAND나 PS1을 덮어써도 보류는 정확하다. 다만 사유가 hook이 아니라 `manual_input`으로 나온다. trap DEBUG는 호환으로 처리된다.
6. **OMP 승인 창**(C-D72 (4)): 이번 실행에서는 나타나지 않았다. "해당 없음" 기록용 근거는 Root가 연결한다.
7. **VM guest의 `OMP 격리 failed`**: guest UI 상태줄에 이 문구가 보인다. guest가 fake OMP를 쓰기 때문이며 판정 대상이 아니다.

## 5. Root에 넘길 사항

- gate 입력:
  - checks = observed 3개
  - candidate = `f8ad6ec989ec2b9f1f72a81414bbaeeadc911997a196c0edf1eeed18442f4e3d`
  - requirements = `requirements-p27-cw16.json`
- B4a validator records를 만들 때 P-C-AC-20은 compat에만 있다. 그래서 중복을 고르는 `sources`가 필요 없다. I-SHELL은 C-AC-20을 포함하지만 item id가 다르다.
- 정식 check 산출물:
  - `check-p27-cw16-{compat, flow-policy-shell, fault, regression-a, regression-b}-{request, execution}.json`과 `.log`
  - observed 3개
- 보조 파일: `cw16-formal/{snapshots, reports, reruns, items-*.json, snap.sh, mkobs.py}`

## 6. 추가: regression-a 정식 재실행 (r2, Root 판정 2026-10-08)

첫 실행(`p27-cw16-regression-a-01`, exit 1)과 단독 재실행 4/4 기록은 그대로 두었다. 같은 C16에서 같은 argv와 같은 요청 형식으로 정식 재실행했다. 변경점은 check_id와 log 경로뿐이다.

| check_id | exit | 시간 | log sha256 | before/after |
|---|---|---|---|---|
| p27-cw16-regression-a-r2 | 0 | 885 s | `d0b1dca6d3d919b253f2e65d69a261eaa9fc70eb5cc9510054dd84eb69cefac7` | C16 / C16 (stable, timed_out false) |

- suite 결과:
  - backend: 1114 OK (skipped 30, expected failure 1)
  - ui: 842 OK
  - ui/status_workbench: 2 OK
- 첫 실행에서 실패한 2건은 r2에서 통과했다.
- snapshot `rega-r2-before`/`rega-r2-after`가 모두 C16과 같았다. git status(src/omp_bridge/tests/docs/pyproject)는 비어 있었다. repo 쓰기는 0이다.
- 파일: `check-p27-cw16-regression-a-r2-{request,execution}.json`, `check-p27-cw16-regression-a-r2.log`
- Root 판정에 따라 queue_full은 fixture/L-CW17-CONTRACT 하위 관측으로 기록하고, item unknown으로 올리지 않는다.
