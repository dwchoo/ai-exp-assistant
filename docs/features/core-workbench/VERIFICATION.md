# Core Workbench 최종 검증 기록

아래에서 `run/`은 `.workflow/core-workbench/runs/implement-p2.6-20260927/`을 뜻한다. 이 문서는 기록에 있는 사실만 적는다. 실행하지 않은 항목은 통과로 쓰지 않고, skip은 C18 근거로 세지 않는다. 이전 후보 C16의 기록은 "C16"으로 구분해 남긴다.

## 1. 상태

- 최종 후보 C18: `a2e3daf23dc98d8c98c760d62e227d4425c0b6b8265b6b3d9a696bc7a573482b`(HEAD `c47456a`). snapshot은 `run/p27-snapshot-cw16-C18.json`(sha256 `1d3670ee…1f67`)이다. 후보 snapshot에는 작업 트리의 이 문서 두 개(초안)가 들어 있다(`run/result-p27-cw16-c18-formal-01.md` §4). 이 문서를 고친 뒤 snapshot을 다시 뜨면 candidate id가 달라진다. aggregate의 input_manifest(400 files)에는 이 두 문서가 들어 있지 않다.
- 이전 후보: C16(`f8ad6ec9…`, HEAD `e635723`)에서 시작해 C17(`81bebe6`)을 거쳐 C18(`c47456a`)이 됐다. C18은 C17 review 정정(C18 커밋)을 반영한 후보다. 이 문서의 C16 값은 비교용이다.
- requirements는 `run/requirements-p27-cw16.json`이다(revision `core-workbench-p2.7-CW-16`, sha256 `929967d6…a48a`, check identity `1be5eecb…ee94`. C16과 같다). authority는 사용자 인용 "전부 진행해서 테스트까지 해서 완료하고 보고해"와 승인 bundle이다. requirements 파일의 authority 문구는 amendment u의 bundle `b31396fa…9cf2`를 가리킨다. C18 aggregate expected의 `approval_digest`는 amendment v(`p2.7v`, C-D73)의 bundle `6a942575…5b94`다(`planning-p2.7-20260929/approval-amendment-v.json`: 변경은 PLAN.json·DECISIONS.md, G1-COLOR check 문구 개정, ticket·gate item id·level 불변).
- 최종 gate: `run/gate-p27-cw16-c18-final-result.json` = `{"issues":[],"passed":true}`(2026-10-09 03:02 KST, sha `26261dca…99a7`)
- **CW-16 소유 7개 item(P-C-AC-19, P-C-AC-20, I-FLOW, I-SHELL, I-POLICY, I-FAULT, I-COMPAT)의 gate는 C18에서 통과했다.** 7개 item의 acceptance 합집합은 C-AC-01~34 전부다.
- 71개 전체 집합(§9): C18 기록 기준으로 historical-stale은 0개다. C16에서 stale이던 G1-COLOR와 G3-SESSION, 미관측 부분이 있던 impact-compared 5개는 C18 gap-01/gap-c18과 regression-a의 live 테스트 전환으로 관측됐다. C16 초안 단계에서 남아 있던 **G1-COLOR의 bold/underline/reverse 강조**도 C18에서 저장소 변경 없는 별도 runtime probe로 관측했다(`run/result-p27-cw16-emph-01.md`(`run/cw16-emph-c18/`): 단독 3·색 결합 8 케이스가 pyte와 격리 tmux 바깥 화면 양쪽에서 보존, 모델 요청 0).
- 실제 모델 smoke(§10)는 C16에서 실행한 확신용 기록이며 C18 gate 근거가 아니다. RM2는 실행하지 않았다.
- **Root 완료 판정(2026-10-09): CW-16 `done_verified`.** 71개 전체 집합에 stale·unknown이 없다(§9). 실제 모델 smoke는 계획(§5.6)대로 확신용 기록이며 gate 근거가 아니므로, RM2와 run 진행 중 pause의 turn abort를 실모델로 보지 못한 점은 완료를 막지 않는다. 같은 동작은 C18 scripted runtime(P1 장기 run 점검, P3 turn 중단 요청·확인)으로 관측했다. compat 첫 실행의 Esc 실패와 `test_product_pty` race는 intermittent로 기록하고 후속 관찰 대상으로 둔다(§11).

## 2. 결론

| Item | 판정 | unknowns | 남은 사항 |
|---|---|---|---|
| P-C-AC-19 | passed(runtime) | [] | 자식이 입력을 읽지 않을 때의 queue_full 거절은 compat matrix에서 실행하지 않았다. regression-a의 live 테스트(`test_live_contract_independent`, 실제 pane)가 C18에서 통과로 관측했다(gate 밖) |
| P-C-AC-20 | passed(runtime) | [] | 실제 zsh login 검증은 C-D72 (1)에 따라 범위 밖이다 |
| I-FLOW | passed(runtime) | [] | — |
| I-SHELL | passed(runtime) | [] | — (SH5 사유 문구는 fix-03에서 고쳐졌다. §6 C-AC-26) |
| I-POLICY | passed(runtime) | [] | C-AC-07 근거 보고의 내용은 skill 수준이다. 세션 설정 변경은 revision 변경으로만 관측했다(§6) |
| I-FAULT | passed(runtime) | [] | — |
| I-COMPAT | passed(runtime) | [] | 승인 창은 "해당 없음"이다(근거는 §6 C-AC-14). compat 첫 실행은 intermittent 실패로 exit 1이었다(§4) |

모든 runtime 근거는 실제 OMP 18.8.0 두 개와 scripted local provider로 C18 제품 진입점(`python -m workbench start/attach/status/shutdown/confirm-boot`)과 제품 UI에서 만들었다. 실제 모델 요청은 0회다. 이 0은 관측값이 아니라 구성으로 보장된다. fake HOME, 빈 `agent.db`, `HTTP(S)/ALL_PROXY=127.0.0.1:9`를 쓰고, `cw16_harness.py` `assert_env_isolated`가 fail-closed로 확인한다. 보고서의 `real_model_requests: 0`은 상수다(review P3-4).

## 3. 전제

- **CW-19 통합**: `run/integration-p27-cw19.json`(C-D71, 후보 `93fb4481…`). L-CW19-RESTART와 L-CW19-REBOOT는 passed다(`run/result-p27-cw19-vm-02.md`). CW-16은 둘 다 C18에서 다시 실행했다(X2R, X5).
- **CW-16 중 제품 수정**(C16 이후 포함)
  - B2 결함 D-B2-1/2/3과 B3 finding F1·F2는 fix-01·fix-02에서 고쳤다(`run/result-p27-cw16-fix-0{1,2}.md`). fix-review-01은 block(P2 2건), fix-review-02는 pass(P3 3건)였다.
  - fix-03(`result-p27-cw16-fix-03.md`): shutdown 확인 응답을 close 전에 flush하고 두 OMP에 SIGTERM을 함께 보낸다. CLI는 결과를 backend가 알린 상한(150 s)까지 기다리며 traceback 없이 C-AC-22 방식으로 끝난다. 재시작 상태줄은 120 s(열린 Task나 생존 process가 있을 때) 또는 15 s 뒤 사라진다. `held:retry_limit` 결과에 `detail`을 추가했다. SH5 보류 사유에 `PROMPT_COMMAND`/`PS1` 가능성을 적는다.
  - fix-04: `_color_index`가 6자리 hex를 10진 index로 오인하던 결함(D1)을 고쳤다. 24-bit RGB는 가장 가까운 256색으로 근사한다. 이때 도입한 OMP child umask 077은 C17 review P2-1 때문에 fix-05에서 되돌렸다.
  - fix-05(`result-p27-cw16-fix-05.md`): umask 되돌림(OMP child·launcher helper·tool은 사용자 umask를 유지, 0700은 `prepare_omp_home`이 보장), `brightbrown`(SGR 93/103)→11과 `bfightmagenta`→13 매핑, harness가 `omp-root/agent`를 0755로 만들어 제품의 0700 복구를 관측 가능하게 함, `cmd_shutdown`의 non-tty stdin 처리와 확인 대기 중 Ctrl-C 처리.
  - gap-01(`result-p27-cw16-gap-01.md`): `tests/backend/live_harness.py`의 18.2.10 pin을 버전 기록(`HISTORICAL_OMP_VERSION`)으로 바꾸고 fake HOME sandbox로 실행하게 했다. 새 gap 시나리오 `tests/integration/live_cw16_gap.py`(COLOR, SCROLL, COMPOSER, SESSION)를 추가했다.
- **review**
  - `result-p27-cw16-c17-review-01.md`: **block**(P2-1 umask가 사용자 project 파일 mode까지 바꿈, P2-2 SGR 93/103을 기본색으로 그림, P3-1 harness가 `omp-root/agent`를 먼저 0700으로 만듦).
  - fix-05 뒤 `result-p27-cw16-c18-review-01.md`: **pass**(P1·P2 없음, P3 2건). P3-a(`SHUTDOWN_REQUEST` 도중 Ctrl-C)는 Root가 고쳤다고 assignment에 적혀 있다. 그 수정을 따로 검증한 기록은 읽지 못했다.
- **사용자 검토**(C-D57): UR-UX는 2026-10-03에 closed됐다("UI 적인 것은 괜찮은거 같아"). UR-USABILITY는 2026-10-08에 done이다("좋아 현재 잘 동작해. 이제 이걸 푸쉬해줘."). 근거는 `.workflow/core-workbench/state.json`이다. 사용자 검토와 자동 gate는 서로를 대신하지 않는다.
- **범위 결정**: C-D55에 따라 conda 전용 점검은 하지 않는다. C-D72 (1)에 따라 지원 shell은 Bash와 sh뿐이고 zsh login 검증은 하지 않는다. C-D72 (2)에 따라 최종 OMP 버전은 18.8.0이고, 18.2.10 고정 probe는 historical로 둔다. C-D72 (3)은 worker 직접 요청의 세 경계를 정한다. C-D72 (4)에 따라 승인 창은 근거를 붙여 "해당 없음"으로 둔다. **C-D73**(2026-10-08, 사용자 "256색 근사 수용")에 따라 제품 UI의 256색 근사는 확정 동작이고 G1-COLOR 기준은 "원본 RGB와 같음"이 아니라 index 색·가장 가까운 256색·강조·해석 오류 없음으로 바뀌었다. 원본 색 fidelity는 후속 과제 [FOLLOWUP-TRUECOLOR.md](FOLLOWUP-TRUECOLOR.md)에 있다(승인된 범위가 아니다).

## 4. 정식 check

모든 check는 `workflow_tools.py check`로 실행했다. cwd는 repo root이고, before=after=C18, stable true, timed_out false다. argv는 모두 `env -i`로 시작하므로 `TMUX*`와 `HERDR_*`가 자식에 전달되지 않는다. 정확한 argv는 각 `run/check-p27-cw16-<id>-{request,execution}.json`에 있다. 시간은 2026-10-08 UTC다. 후보 snapshot은 각 check 전후에 모두 C18과 같았다.

| check_id | 내용 | exit | 시간(UTC) | log sha256 |
|---|---|---|---|---|
| p27-cw16-compat-c18 | 비live `test_cw16_runtime_matrix`+`test_cw16_harness` 17건 OK(skip 1) 후 live COMPAT 6회와 SEL1–3 | **1** | 16:09–16:22 | `e93bdddc…349e19` |
| p27-cw16-compat-c18-r2 | 위와 같은 argv로 전체 재실행 | 0 | 16:23–16:36 | `07b32a4e…1226d9` |
| p27-cw16-flow-policy-shell-c18 | `live_cw16_flow_policy.py`, `live_cw16_shell.py` | 0 | 16:37–16:56 | `238d01f0…3a971c` |
| p27-cw16-fault-c18 | `live_cw16_fault.py`(X1–X4, X6, X7, X2R, X5 VM) 후 G3 고정 probe 버전 기록(3 OK) | 0 | 16:56–17:17 | `f4136323…c2b94e` |
| p27-cw16-gap-c18 | `live_cw16_gap.py`(COLOR, SCROLL, COMPOSER, SESSION; 4 OK, 139 s) | 0 | 17:17–17:20 | `92df6040…913963` |
| p27-cw16-regression-a-c18 | fake HOME, proxy 차단. `tests/{backend,ui,ui/status_workbench}` | 0 | 17:20–17:40 | `3153a31b…fa03f5` |
| p27-cw16-regression-b-c18 | 같은 env. `tests/{terminal,recovery_boot,integration,lifecycle,observation,storage,tasks,workflow,contracts,bridge}`, `gates/{g1_vt,g2_shell,g3_omp,g4_evidence,g4_lifetime}`, `policy/{pause_automation,recovery_manager}`, `node --test tests/bridge/*.test.ts`, `run-contracts.sh` | 0 | 17:40–17:59 | `9848f29e…f2e7bc` |

gap-c18은 실행 기록만 있고 observed JSON은 만들지 않았다(gate의 7개 item 밖이다). 요청에 지정한 `WB_LIVE_OMP_VERSION_RECORD` 파일은 이 모듈이 쓰지 않아 생기지 않았다. 버전은 각 보고서 `versions`에 `omp/18.8.0`으로 있다.

**환경**(기록이며 고정값이 아니다)
- OS: Linux 7.0.0-30-generic, Ubuntu 24.04.4
- 도구: OMP `omp/18.8.0`(`~/.local/bin/omp`), Bash 5.2.21, dash 0.5.12-6ubuntu5, tmux 3.4(격리 `-S` socket), Herdr 0.9.3(격리 config/runtime)
- 런타임: Python 3.12.3 venv `/tmp/cw02-g1-venv`(pyte), Node v22.23.2
- VM: 격리 QEMU VM `wb-reboot`(guest Ubuntu 24.04.5, kernel 6.8.0-142)

**compat-c18 첫 실행(exit 1)**: 실패 3건. 첫 기록과 r2 기록을 모두 남겼다.
- 비live 17건은 OK(skip 1)였다. live 6셀 중 plain/bash, plain/dash, tmux/dash가 C2에서 실패했고 herdr 2셀과 tmux/bash는 통과했다. SEL1–3은 통과했다. 보고서는 `cw16-formal-c18/reports/cw16-matrix-5ba4d64c/`다.
- 실패 단정은 manager OMP의 `esc_closed_menu: False`다. 즉 Esc를 보낸 뒤 화면에 slash menu가 닫히지 않은 채로 관측됐다. 같은 셀에서 typed_visible, alt_enter_newline, ctrl_c_cleared, slash_menu, hangul_composer, alive는 모두 True였다. worker는 이 지점까지 가지 않았다.
- 이 실패는 C16에서 알려진 intermittent(`KillScopeTests`, `RestartExitedOmpPtyTests`)에 없던 새 후보다. 같은 argv로 전체를 다시 돌린 r2(`cw16-matrix-984764a2/`)는 6셀과 SEL1–3 모두 통과(exit 0)했다. 같은 코드에서 3/6 셀 실패 뒤 6/6 통과했으므로 결정적 회귀보다 Esc 뒤 화면 timing에 가깝다고 본다. **원인은 조사하지 않았다.** gate의 compat observed는 r2 execution에 items를 붙인 것이고, evidence에 첫 실행 실패를 적었다.

**regression-a·b**: C18에서는 둘 다 첫 실행에서 exit 0이다. C16에서 첫 실행이 실패했던 `KillScopeTests`와 `RestartExitedOmpPtyTests`도 통과했다. 다만 fix-05 자체 실행(정식 check 밖)의 ui suite에서 `RestartExitedOmpPtyTests` 한 건이 spawn 순서(`old["role"]` manager != worker)로 다시 실패했고, 모듈 단독 재실행은 통과했다. 이 race는 원인을 확정하지 않았고 고치지 않았다.

**suite 결과**(regression-a·b, C18)

| suite | 결과 |
|---|---|
| backend | 1127 OK (skipped 3, expected failure 1) — 전환된 live 테스트 포함, 965 s |
| ui | 843 OK |
| ui/status_workbench | 2 OK |
| terminal | 221 OK (skip 0) |
| recovery_boot | 184 OK |
| integration | 24 OK (skipped 2) |
| lifecycle | 38 OK |
| observation | 95 OK |
| storage | 33 OK |
| tasks | 22 OK |
| workflow | 62 OK |
| contracts | 52 OK |
| bridge(py) | 52 OK (skipped 1) |
| g1_vt | 85 OK |
| g2_shell | 142 OK (skipped 1) |
| g3_omp | 117 OK |
| g4_evidence | 10 OK |
| g4_lifetime | 8 OK |
| pause_automation | 129 OK |
| recovery_manager | 26 OK |
| node bridge | 48/48 |
| run-contracts | py 52 OK, node 15/15 |

### 4.1 regression skip 공개

C16에서 OMP 18.2.10 pin 때문에 skip됐던 29건은 **C18에서 모두 실행됐고 통과했다**(skip 0). `tests/backend/live_harness.py`가 `omp/*`이면 받아들이고 버전을 기록하며(`HISTORICAL_OMP_VERSION`), 모든 live 테스트를 fake HOME·빈 `agent.db`·proxy 차단 sandbox에서 돌린다. 버전 기록은 `omp/18.8.0`이다. 테스트 assertion과 관측은 바꾸지 않았다(skip 사유 문자열 10곳, 시작 전 data dir이 비어 있어야 하는 3곳의 `seed_provider=False`, symlink 항목 제외 2줄만 바뀌었다. review가 약해진 단정이 없음을 확인했다).

| module | 건수 | C18 결과(regression-a/b) | 다룬 동작 |
|---|---|---|---|
| `tests/backend/test_live_start.py` | 3 | 3 OK | Bash 우선과 zsh login 무시, **두 번째 start의 backend 재사용**, sh만 있을 때 dash, Bash·sh 부재 안내 |
| `tests/backend/test_live_start_independent.py` | 7 | 7 OK | fake zsh 미실행, **동시 start의 backend 1개와 private mode**, dash 선택, 죽은 backend의 stale socket·lock 재시작, 다른 lock 보유자 비종료(12.2 s), 부재 안내, symlink data dir 거절 |
| `tests/backend/test_live_detach.py` | 1 | 1 OK | 60초 초과 detach 뒤 재attach, identity 동일, 새 run 없음 |
| `tests/backend/test_live_detach_independent.py` | 1 | 1 OK | **attach된 frontend를 SIGKILL**한 뒤 60초 초과 재attach |
| `tests/backend/test_live_input_independent.py` | 4 | 4 OK | 확인된 인수 뒤 텍스트·foreground 프로그램, **커서 키·backspace 편집**, Ctrl-C |
| `tests/backend/test_live_contract_independent.py` | 7 | 7 OK | 한도 아래 multiline paste, connection cap·exclusive attach, framing 오류, mid-stream 단절, **실제 pane queue_full**, 2 MiB 초과 거절, version mismatch |
| `tests/backend/test_live_paste_responsive.py` | 1 | 1 OK | 2 MiB raw paste 중 snapshot 지연 1초 미만 |
| `tests/backend/test_live_review_fixes_independent_p27c.py` | 3 | 3 OK | host sleeper 500개와 2 MiB paste 순서, paste 중 foreground 변경, pipe·XFSZ signal disposition 뒤 정상 종료 |
| `tests/terminal/test_manual_input_backend_independent.py` | 2 | 2 OK | bash·dash 수동 입력(ui_v1) |

gap-01 자체 실행(비공식)에서 `test_concurrent_starts…`가 data dir 아래 OMP 파일의 mode 때문에 실패했다. 이는 fix-04의 umask 변경(이후 fix-05에서 되돌림), test delta, 되돌린 뒤의 0700 기준 단정으로 정리됐고, C18 regression-a에서 통과한다.

**pin과 무관한 skip(C18)**
- backend 3건: `test_cw18_smoke04_fixes` 3개 class의 `test_cancel_during_the_failed_analysis_still_closes_as_cancelled`. 사유는 'covered by AnalysisFailureTests'다.
- integration 2건:
  - `test_product_path_outer_matrix`: opt-in(`WB_LIVE_CW16=1`)이며 compat check에서 live로 실행했다.
  - `FinalRecordsTests`: 'WB_CW16_FINAL_REQUEST not set'이다. 최종 모드에서 따로 실행했다(§8).
- bridge 1건: `test_strict_placeholder_live`(`WB_LIVE_CW18=1` 필요). 이 live 테스트는 C18에서 실행하지 않았다.
- g2_shell 1건: `test_control_probe.ControlWaitExperiment.test_non_delegated_eval_return_is_not_lifetime_done`('delegated scope is available').
- 합계 7건. 이 중 근거로 세지 않는 것은 backend 3, bridge 1, g2_shell 1이다.

**discover 대상 밖의 18.2.10 고정 live probe**: 이 probe들은 C18에서도 실행하지 않았고 historical이다(C-D72 (2)).
- `tests/ui/live_product_omp_independent.py`(gap-01에서 쓰기 범위 밖이라 여전히 18.2.10 pin)
- `tests/gates/g1_vt/live_*_probe.py`
- `tests/bridge/live_mailbox_{probe,independent}.py`
- `tests/observation/live_worker_review_{probe,independent}.py`
- `tests/workflow/live_workflow_{probe,independent}.py`
- `tests/policy/pause_automation/live_pause_runtime_probe.py`

## 5. 최종 integration item

| Item | 시나리오(C18) | evidence(observed JSON, sha256) | 결과 |
|---|---|---|---|
| P-C-AC-19 | COMPAT C1–C7 × {plain, tmux, herdr} × {bash, dash} (r2) | `check-p27-cw16-compat-c18-observed.json` `377fc84a…c3a8` | passed, [] |
| P-C-AC-20 | canonical Bash/dash matrix, SEL1–3, 6회 모두 host Bash와 dash로 실행 | 같음 | passed, [] |
| I-COMPAT | COMPAT 6회와 버전 기록 | 같음 | passed, [] |
| I-FLOW | F1, F2(4사례와 없는 commit), F3, F4, F5(C-D72 (3)), F6, F7, F45 | `check-p27-cw16-flow-policy-shell-c18-observed.json` `a77ce0df…4d28` | passed, [] |
| I-POLICY | P1–P8, CLK(clock fixture 17 모듈) | 같음 | passed, [] |
| I-SHELL | SH1–SH5 × {bash, dash} | 같음 | passed, [] |
| I-FAULT | X1–X4, X6, X7, X2R(L-CW19-RESTART 56/56), X5(VM 28/28), G3 고정 probe 3/3 | `check-p27-cw16-fault-c18-observed.json` `75d8d506…7807` | passed, [] |

observed 파일은 execution JSON에 `items`를 채운 것이다. compat observed의 원본은 r2 execution(check_id `p27-cw16-compat-c18-r2`)이다. item 문안은 `run/cw16-formal-c18/items-*.json`이다. C16 문안에서 후보·보고서 경로·boot id·manifest·compat 시간 범위를 C18 값으로 바꿨고, C18 보고서에서 다시 확인하지 못한 세부 시간 값(186.3 s, 0.4 s, 85 s, 62 s 등)은 문장에서 뺐다. 시나리오 보고서는 `run/cw16-formal-c18/reports/`에 있다. 보고서는 gate log sha로 고정되지 않으므로(review P3-3) 파일별 sha256을 부록 A에 남긴다.

**C18 주요 관측**
- **C5**: r2 matrix에서 detach 65.7–65.9 s 동안 backend, OMP 2개, shell, supervisor의 pid와 ticks가 그대로였다. 출력이 보존됐고 owner와 Task는 바뀌지 않았다. provider 요청 증가는 0, 중복 입력도 없었다.
- **X5**: manifest(`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`)는 host와 guest 모두 C18 aggregate `6eeed2425de99a74c657efa1e09fcf30c5bab1bd4669169ce72067cf16fcd0b2`(93 files)였고 실행 뒤 host 값도 같았다. 이 값은 실행 전에 driver의 manifest 함수로 계산해 request에 기록했고 결과와 일치했다. boot_id는 `3ada806f`→`0e23a1ea`→`812e3a91`로 바뀌었고 QEMU pid는 같았다. 28/28 checks였고 VM은 rc 0으로 종료했다(disk 보존). C16 때 한 번 관찰한 `tool_no_result`(열린 Task 상태에서 worker terminal 45초 무응답)는 C18에서도 재현되지 않았다.
- **X2R**: `tests/recovery_boot/live_restart_independent_p27cw19.py`를 C18에서 56/56 checks로 통과했다. endpoint 요청은 0이다.
- **gap-c18**: COLOR, SCROLL, COMPOSER, SESSION 4건이 모두 통과했다(§9 해당 item). 보고서는 `cw16-formal-c18/reports/c18-gap/`다.

## 6. C-AC-01~34 추적

`local`은 p2.6까지의 formal local gate다. `C18 시나리오`는 이번 정식 check의 runtime 관측이다.

| AC | local supplier | final item | C18 시나리오 | 상태 |
|---|---|---|---|---|
| C-AC-01 | CW-06 | I-FLOW | F1 manager pane 입력 | passed |
| C-AC-02 | CW-08, G3 | I-FLOW | F1, F4(`_dup1` → `worker_busy`, 두 번째 Task 없음). 같은 toolCallId의 멱등성은 CLK fixture로 확인 | passed |
| C-AC-03 | CW-10, G2 | I-FLOW, I-SHELL | F1(exit, output tail, log_path), F2, SH4(exit 3을 확인된 종료로 기록) | passed |
| C-AC-04 | CW-13 | I-FLOW | F2: criteria_met / nonzero_exit / exit_zero_criteria_failed / indeterminate | passed |
| C-AC-05 | CW-10 | I-FLOW | F2, F6(수정 필요 보고 뒤 idle 구간 동안 자동 전달 0, 새 manager 후속 지시 뒤 정확히 1회) | passed |
| C-AC-06 | G1, CW-06 | I-COMPAT | C1(focus를 4회 옮기는 동안 owner와 epoch 불변) | passed |
| C-AC-07 | CW-13 | I-POLICY | P4: 4번째 요청이 `held/retry_limit`(runs_started 4), revision이 바뀐 뒤에도 횟수 유지 | passed. 아래 주 참고 |
| C-AC-08 | CW-07, G2, G4 | I-SHELL | SH2 | passed |
| C-AC-09 | CW-10 | I-FLOW | F2 worktree, 없는 commit은 `WorktreePreparationError`로 보고하고 아무것도 실행하지 않음 | passed |
| C-AC-10 | CW-13, G2 | I-POLICY | P5(실행 중 수정 지시는 `worker_busy`, 실행 중 revision 유지) | passed |
| C-AC-11 | CW-10 | I-FLOW | F3(local commit만, bare remote ref 불변) | passed |
| C-AC-12 | CW-11, G4 | I-POLICY | P1(첫 점검 60.7 s), P2(pause 중 점검 0) | passed |
| C-AC-13 | CW-13 | I-POLICY | P4, F2 manager 2차 판단 | passed |
| C-AC-14 | G1, G3, CW-06 | I-COMPAT | C2: slash, Esc, Ctrl-C, Alt-Enter, 한글 composer, `/copy`. 첫 실행의 Esc 실패는 §4 | passed. 승인 창은 아래 주 참고 |
| C-AC-15 | CW-15, G4 | I-FAULT | X1, C5 | passed |
| C-AC-16 | CW-15, G3 | I-FAULT | X2, X2R, X7 | passed |
| C-AC-17 | CW-15 | I-FAULT | X3(P2-1·P2-2 회귀 포함) | passed |
| C-AC-18 | CW-15, G4 | I-FAULT | X1(b): 인수 요청 상태가 detach 뒤에도 보존되고 자동 전송 0 | passed |
| C-AC-19 | G1, CW-16 | P-C-AC-19, I-COMPAT | C1–C7 ×6(r2) | passed. queue_full은 regression-a live(gate 밖) |
| C-AC-20 | G1/G2, CW-16 | P-C-AC-20, I-COMPAT, I-SHELL | SEL1–3, canonical matrix, SH*, C2 한글·wide | passed. zsh는 범위 밖 |
| C-AC-21 | CW-11 | I-POLICY | P7(status와 상태줄 `사용량: 재시도 0/3 · 점검 N · 모델 미확인`). 121번째 점검과 13번째 wake는 CLK fixture로 확인(SPEC 허용) | passed |
| C-AC-22 | CW-15 | I-FAULT | X4(--yes 없이 exit 1과 활성 작업 목록, --yes verified exit 0과 broker 0, `setsid -w` survivor는 exit 1 `종료 확인 실패`), X7 | passed |
| C-AC-23 | CW-15 | I-FAULT | X5 VM | passed |
| C-AC-24 | CW-14 | I-FAULT | X6(70 MiB 출력이 정확히 64 MiB로 저장되고 실행 계속). 512 MiB는 축소 한도 fixture | passed |
| C-AC-25 | CW-12, G3/G4 | I-POLICY | P3, P8 | passed |
| C-AC-26 | CW-07, G2/G4 | I-SHELL | SH1, SH3, SH5. SH5 보고서 4건의 보류 사유가 `PROMPT_COMMAND (bash) or PS1 (sh)` 가능성을 명시한다(`hook_named_in_reason: true`) | passed |
| C-AC-27 | CW-11, G3 | I-POLICY | P1(busy 병합 coalesced), P2(run 종료 표시) | passed |
| C-AC-28 | CW-14, G4 | I-FAULT | X6(`metadata_unavailable` hold, raw log 장애 표시, 170열에서 `pause_not_stored` 표시) | passed |
| C-AC-29 | CW-09, G4 | I-POLICY | F7(manager 지시 전 idle 구간 동안 모델 요청 0, 실행 0) | passed |
| C-AC-30 | CW-12, G3/G4 | I-POLICY | P2 | passed |
| C-AC-31 | CW-13 | I-POLICY | P4(revision 2를 다음 run에 적용), P5 | passed |
| C-AC-32 | CW-07, G2 | I-SHELL | SH1(시작 뒤 export한 변수도 선언 env로 허용) | passed |
| C-AC-33 | CW-12, G3/G4 | I-POLICY | P3(중단 확인, unknown tool call id, 중단된 turn 재실행 0) | passed |
| C-AC-34 | CW-13, G2 | I-POLICY | P6(`restart_worker` exit 143 확인, `stop_survivor` 거절, host shell 강제 종료) | passed |

**C-AC-07(Root 판정)**
- 재시도 한도에서 실행을 막는 것은 제품이 하고, 이는 runtime으로 관측했다(`held:retry_limit`). fix-03부터 이 결과에 `detail`(영어)이 붙는다. 다시 실행하지 말 것과 사용자에게 보고할 항목 (1) 원인 (2) 근거 (3) 시도 (4) 남은 문제, 사용자 결정 대기를 담는다. 이 문구를 단위 테스트로 확인했다.
- 실제 모델이 이 항목을 빠짐없이 보고하는지는 C18에서 관측하지 않았다. 제품과 테스트 모두 모델의 보고를 막거나 강제하지 못한다. `omp_bridge/skills/to-worker/SKILL.md`는 수정하지 않았다(review P3-1은 이 수준에서 부분 해소).
- 앞서 B2 O5가 적은 대로 retry 한도에서 별도의 자동 요약 보고는 없고 run별 판정 보고만 manager에 전달된다.

**C-AC-14 승인 창: 해당 없음(C-D72 (4))**
- Workbench OMP home의 `config.yml`(`src/workbench/backend/omp_home.py` `home_config`)은 `tools.approvalMode`를 설정하지 않는다. `src/`와 `omp_bridge/`에는 `approvalMode` 설정이 없다.
- worker는 `--tools` 허용 목록을 쓰고, manager는 OMP 기본 구성을 쓴다.
- OMP 18.8.0의 기본값은 `yolo`다. 근거는 Root가 2026-10-08 깨끗한 임시 home에서 `omp config get tools.approvalMode`를 실행한 결과이며, 이 문서 작성자가 직접 실행한 것은 아니다.
- 이번 live 실행(compat, flow-policy-shell, fault) 어디에서도 승인 창은 나타나지 않았다(I-COMPAT evidence 문안).

**I-POLICY의 "세션 설정 변경"**(review P3-6): P4가 관측한 것은 spec revision 변경 뒤 재시도 횟수가 유지되는 것뿐이다. revision 외의 세션 설정 변경은 C18에서 따로 관측하지 않았다.

**SH5 사유 문구**: C16 기록은 PROMPT_COMMAND나 PS1을 덮어쓰면 사유가 `manual_input`으로만 나온다고 적었다(review P3-5). fix-03 뒤 C18 보고서(`c18-shell/shell-SH5-prompt_command-bash.json` 등)는 "host shell reported no prompt after the last entered line and runs no program (mode manual_input): a user PROMPT_COMMAND (bash) or PS1 (sh) change may have replaced Workbench's prompt hook, or a shell builtin or multi-line command is still running or waiting for input"를 보인다. 원인을 단정하지 않는 문구다(builtin `read`, 반복문, PS2 continuation도 같은 상태다). 단, `check-p27-cw16-flow-policy-shell-c18-observed.json`의 I-SHELL 문안은 C16 문구("reason shows manual_input")를 그대로 두고 있다. 보고서 쪽이 현재 관측이다.

## 7. 재사용 근거와 영향 대조

| 근거 | 판정 |
|---|---|
| p2.6 `run/check-cw16-final-observed.json`(6dc51f, sha `cdeca27f…`), `run/gate-cw16-final-result.json`(passed:false) | historical. UI가 feasibility `terminal_g1`이었고 OMP 18.2.10, Herdr 0.9.1로 실행됐다. G4 음성 대조의 stale 입력으로만 쓴다 |
| C16 check 전부(`check-p27-cw16-*-01`, `-r2`), C17 파일 | C18 근거로 쓰지 않았다. 덮어쓰지 않고 남겼다 |
| L-CW19-REBOOT(vm-01/02) | 재사용하지 않았다. C18 manifest로 X5에서 다시 실행했다(aggregate `6eeed242…`가 host·guest·C18에서 같다) |
| L-CW19-RESTART(test-01) | 재사용하지 않았다. X2R로 C18에서 다시 실행했다 |
| L-CW17-*, L-CW18-*, v-cw06 P-C-AC-01/06/14 | L-CW17-* 원 테스트는 C18 regression-a에서 18.8.0으로 직접 실행했다(§4.1). 나머지는 historical이고(`run/integration-p27-v-cw17.json`, `run/integration-p27-cw18.json`, `run/integration-p27-v-cw06.json`), 같은 사용자 동작을 C18 시나리오로 다시 관측했다. 대응은 §9에 있다 |
| G1·G3 18.2.10 고정 live probe | historical(C-D72 (2)). 18.8.0 재관측 대응은 §9에 있다. G3 `test_tui_extension_fault_integration`은 실행 버전 기록 방식으로 바꿔 C18에서 실행했다(`omp/18.8.0`, 3/3) |
| 실제 모델 smoke `result-p27-cw16-rm-01.md` | C16(HEAD `e635723`) 기록이다. C18 gate 근거가 아니다(§10) |
| UR-UX, UR-USABILITY | 전제 충족 기록으로만 쓴다(§3) |

이 문서의 "영향 대조"는 원래 probe를 C18에서 다시 실행하지 않은 대신, 같은 사용자 동작을 C18 시나리오가 다시 관측했음을 시나리오 ID로 연결한 것이다. 파일 단위의 입력 경로 diff는 이 문서에서 계산하지 않았다.

## 8. G4 validator

- workflow gate: `run/gate-p27-cw16-c18-final-request.json`(sha `18c86d7b…4b0c53`)은 observed 3개, C18, requirements로 구성된다. 결과는 `passed:true`, issues []다.
- aggregate expected context: `run/aggregate-p27-cw16-c18-expected.json`(sha `3656ed25…1eb48`)은 Root가 records와 독립적으로 고정했다. items는 7개(owner CW-16, runtime)다. requirements_digest는 `929967d6…`, approval_digest는 `6a942575…`(amendment v)다. input_manifest는 400 files이고 VERIFICATION과 COMPATIBILITY는 포함하지 않는다. evidence는 item별 observed JSON의 path와 sha256이다.
- records: `run/aggregate-p27-cw16-c18-records.json`(sha `aafe29ca…d95c`)
- **최종 모드 실행**: `WB_CW16_FINAL_REQUEST=run/validator-p27-cw16-c18-final-request.json`으로 `tests.integration.test_cw16_final_evidence_validators`를 실행했다. 결과는 **Ran 16, OK**(skip 0)이다. log는 `run/validator-p27-cw16-c18-final.log`(sha `1d096737…1213e`)에 있다.

| 구분 | 테스트 | 결과 |
|---|---|---|
| 양성 | `test_gate_accepts_genuine_set`, `test_aggregate_accepts_genuine_set_at_repo_root`(7개 id 반환), `test_aggregate_accepts_genuine_copy`, `test_recorded_gate_result_passed` | ok |
| 고정 | `test_expected_context_is_pinned_by_root`, `test_input_manifest_scope_is_complete`, `test_records_match_observed_checks` | ok |
| gate 음성 7종 | missing_check→`missing`, p2.6 stale record→`stale`·`failed_or_not_run`·`unknown`, unknowns→`unknown`, log 1바이트 변조→`log_mismatch`, 다른 candidate→`stale`, fixture level→`evidence_level`, acceptance 제거→`acceptance_mismatch` | `test_gate_rejects_each_mutation` ok |
| aggregate 음성 9종 | 누락·중복·소유 밖 record, 입력 변경, 실패·unknown·exit 1, candidate 변경, fixture level·kind, evidence 변경·삭제, `fixture` 키 | `test_aggregate_rejects_each_mutation` ok |
| self-check | 허용적이거나 잘못된 validator·gate를 battery가 잡아내는지, p2.6 stale issue 상수 | 7건 ok |

compat observed의 원본은 실패한 첫 execution이 아니라 r2 execution이다. records와 observed의 일치는 `test_records_match_observed_checks`(ok)가 확인한다.

## 9. 71개 전체 집합 연결

상태 정의
- **current**: C18 정식 check(gate, regression, gap)가 해당 item의 동작을 직접 실행했고, 요구 수준 이상이며 skip이 아니다.
- **impact-compared**: 원래 근거(18.2.10 probe나 이전 후보)는 historical이다. 같은 사용자 동작을 C18 시나리오가 다시 관측했고, 그 시나리오를 연결한다. 원래 probe는 다시 실행하지 않았다(C-D72 (2)).
- **historical-stale**: C18에서 다시 관측하지 않았다.

historical 위치
- G1: `run/gate-cw02-final-result.json`
- G2: `run/gate-cw03-g2-integrated-final-result.json`
- G3: `run/gate-cw04-final-result.json`
- G4: `run/gate-cw05-final-result.json`
- L-CW17: `run/integration-p27-v-cw17.json`
- L-CW18: `run/integration-p27-cw18.json`

| item | 수준 | 상태 | C18 근거와 빈 곳 |
|---|---|---|---|
| M0-CONTRACT | fixture | current | regression-b `tests/contracts` 52 OK, `run-contracts.sh`(py 52, node 15/15) |
| G1-RESIZE | runtime | impact-compared | C3 40×170→34×150→40×170, OMP·host TTY 크기 일치 ×6. 승인 보존 부분은 승인 창이 없어 해당 없음 |
| G1-THREE-PANE | runtime | impact-compared | C1–C3 ×6(세 영역, focus 대상 입력, resize) |
| G1-TEXT-TOOLS | runtime | impact-compared | C2 한글·wide·multiline paste·slash ×6. **scroll**은 gap-c18 SCROLL(pass)이 관측했다: `seq` 400줄 뒤 scroll 모드에서 history 유지, background 출력이 오는 7 s 동안 위치 유지, `G`로 live 복귀, scroll 키가 shell로 가지 않음, Shift+PgUp 직접 진입과 입력 시 live 복귀, 마우스 휠(SGR 64 ×4), manager OMP pane의 scroll 모드. 승인 전후 tool side effect는 해당 없음(§6) |
| G1-PASTE | runtime | impact-compared | C2: 2 MiB 초과는 전체 거절하고 이유 표시(plain·tmux). herdr는 outer가 전달하지 않아 n/a. **실제 pane queue_full**은 regression-a의 `test_live_contract_independent`(ok)가 관측했고, 2 MiB 중 snapshot 응답성도 `test_live_paste_responsive`(ok)가 관측했다 |
| G1-VT-QUERY | fixture | current | regression-b `gates/g1_vt` 85 OK |
| G1-COLOR | runtime | current | **C-D73 기준**으로 gap-c18 COLOR(pass)가 관측했다. TERM=xterm-256color, COLORTERM=truecolor, outer는 pyte. 단계: basic16, SGR 30–37·90–97·40–47·100–107 32건(`brightbrown`·`bfightmagenta` 포함), 256 cube(196/46/214, bg 21), grey ramp(236 `303030`, 244 `808080`, 252 `d0d0d0`, bg 240 `585858`가 모두 원색 그대로), system index 1/4/8/12(`cd0000`, `0000ee`, `7f7f7f`, `5c5cff` 그대로), 24-bit RGB(`123456`→`005f5f`, `ff8000`→`ff8700`, `c81e8c`→`d70087`, bg `fa6432`→`ff5f5f`. 가장 가까운 256색), OMP pane 색 정보(고유 전경 20종). hex를 10진 index로 오인하는 오류는 없다(gap-01에서는 `303030`이 `eeeeee`로 나와 실패였다). bold/underline/reverse 강조는 C18에서 별도 runtime probe로 관측했다(`run/result-p27-cw16-emph-01.md`(`run/cw16-emph-c18/`): 단독 1/4/7, 결합 1;31·4;38;5;208·7;44·1;4;7;32·1;38;5;196;48;5;21·4;91·7;38;5;240;48;5;250·1;4;34;47이 pyte와 격리 tmux `capture-pane -e` 양쪽에서 보존, reverse는 flag 그대로, 전후 snapshot은 문서 두 개만 다름). 실물 GUI terminal의 표시 fidelity는 미검증(COMPATIBILITY §5) |
| G1-OUTER | runtime | impact-compared | 18.2.10 G1 outer probe 대신 제품 경로 matrix(COMPAT 6회, 버전 기록) |
| G2-CONTROL | runtime | current | `gates/g2_shell` 142 OK(skip 1), actual Bash/dash canonical matrix(compat check), SH2 |
| G2-INPUT-JOBS | runtime | current | g2_shell, SH3 |
| G2-HOOK-ENV | runtime | current | g2_shell, SH1, SH5 |
| G2-TAKEOVER | runtime | current | g2_shell, SH2, X7 |
| G2-LIFECYCLE | runtime | current | g2_shell, SH4 |
| G2-LAUNCH | runtime | current | g2_shell, SEL1–3. zsh login은 C-D72 (1)로 제외 |
| G3-TUI-DELIVERY | runtime | impact-compared | F1, F2, F6(to_worker/to_manager 왕복), X3(오류 중 false success나 replay 없음) |
| G3-TUI-CONTENTION | runtime | impact-compared | F4, P1(busy 보류와 병합). **non-empty composer 보류**는 gap-c18 COMPOSER(pass)가 관측했다: manager composer에 입력(미제출) 중 worker 보고는 `queued`, 30.0 s 뒤 `report_wait`(manager_editor_not_empty)와 상태줄 안내가 뜨고 그동안 manager 주입 0·모델 요청 증가 0, 입력을 비우면 0.2 s 뒤 1회 전달. native approval은 해당 없음 |
| G3-SESSION | runtime | current | gap-c18 SESSION(pass): `/new` 뒤 같은 pid·pane process에서 bridge manager가 다시 등록됐다(generation 1→2, session id 변경). 재전송 0, manager 요청은 정확히 1이고 그것은 C-D70 (5) `manager_recovery` notice다(`reports_resent: 0`, journal queued 1·sent 1). Task는 유지됐고 이어진 worker `done`이 새 session에 1회 전달됐다 |
| G3-RECONNECT | runtime | impact-compared | G3 고정 probe(18.8.0 버전 기록, 3/3)의 reconnect 관측(pid·session·generation 동일) |
| G3-PAUSE | runtime | impact-compared | P3 |
| G3-EXTENSION | runtime | current | `test_tui_extension_fault_integration` 3/3(OMP 18.8.0, fault check)와 regression-b g3_omp 117 OK |
| G4-LIFETIME | runtime | current | C5 ×6, X1 ×3, `gates/g4_lifetime` 8 OK |
| G4-WAKE | runtime | current | X1(a): detach 중 60 s 점검 1회 전달. P2: pause 중 점검 0 |
| G4-STORE-PORT | fixture | current | X6 `metadata_unavailable` hold(runtime), storage 33 OK |
| G4-CONTRACT | fixture | current | contracts 52 OK, run-contracts |
| G4-EVIDENCE | fixture | current | `gates/g4_evidence` 10 OK, validator 최종 모드 16 OK(§8) |
| P-C-AC-01 | runtime | current | F1 |
| P-C-AC-02 | runtime | current | F1, F4 |
| P-C-AC-03 | runtime | current | F1, F2, SH4 |
| P-C-AC-04 | fixture | current | F2(runtime) |
| P-C-AC-05 | runtime | current | F2, F6 |
| P-C-AC-06 | runtime | current | C1 ×6 |
| P-C-AC-07 | fixture | current | P4(runtime). 보고 내용은 skill 수준(§6) |
| P-C-AC-08 | runtime | current | SH2. 18.2.10 pin이던 live input 4건은 C18에서 4 OK(§4.1) |
| P-C-AC-09 | runtime | current | F2 |
| P-C-AC-10 | fixture | current | P5 |
| P-C-AC-11 | runtime | current | F3 |
| P-C-AC-12 | runtime | current | P1, P2 |
| P-C-AC-13 | fixture | current | P4, F2 |
| P-C-AC-14 | runtime | current | C2 ×6(r2). 승인 창은 해당 없음(§6). 첫 실행의 Esc 실패는 §4 |
| P-C-AC-15 | runtime | current | X1, C5 |
| P-C-AC-16 | runtime | current | X2, X2R, X7 |
| P-C-AC-17 | runtime | current | X3 |
| P-C-AC-18 | runtime | current | X1(b) |
| P-C-AC-19 | runtime | current | COMPAT 6회(gate) |
| P-C-AC-20 | runtime | current | SEL1–3, canonical matrix(gate) |
| P-C-AC-21 | runtime | current | P7. 121번째와 13번째는 CLK fixture |
| P-C-AC-22 | runtime | current | X4, X7 |
| P-C-AC-23 | runtime | current | X5 |
| P-C-AC-24 | fixture | current | X6 |
| P-C-AC-25 | runtime | current | P3, P8 |
| P-C-AC-26 | runtime | current | SH1, SH3, SH5 |
| P-C-AC-27 | runtime | current | P1, P2 |
| P-C-AC-28 | fixture | current | X6 |
| P-C-AC-29 | fixture | current | F7 |
| P-C-AC-30 | runtime | current | P2 |
| P-C-AC-31 | fixture | current | P4, P5 |
| P-C-AC-32 | runtime | current | SH1 |
| P-C-AC-33 | runtime | current | P3 |
| P-C-AC-34 | fixture | current | P6 |
| L-CW17-DETACH | runtime | current | `test_live_detach`·`test_live_detach_independent`(frontend SIGKILL 뒤 60 s 초과 재attach) 각 1 OK(regression-a). 보조로 C5 ×6, X1 ×3 |
| L-CW17-START | runtime | current | `test_live_start` 3, `test_live_start_independent` 7 모두 OK. **실행 중 backend 재사용(두 번째 start)**, 동시 start의 backend 1개, 외부 lock 보유자 비종료, stale socket·lock 재시작, symlink data dir 거절을 실제 OMP로 관측했다. SEL1–3도 같은 동작을 본다. zsh login은 제외 |
| L-CW17-CONTRACT | fixture | current | regression-a: `test_ui_server` ContractServerTests(version mismatch, framing), queue_full fixture 2건과 live 변형 7건(실제 pane queue_full, connection cap, mid-stream 단절 포함) OK |
| L-CW18-FLOW | runtime | impact-compared | 원래 근거는 OMP 18.2.10이다. C18 F1–F7, F45, SH2, SH3(보류된 wb-handoff, 인수, 재handoff) |
| L-CW18-POLICY | runtime | impact-compared | 원래 근거는 historical이다. C18 P1–P8 |
| L-CW19-RESTART | runtime | current | X2R 56/56 |
| L-CW19-REBOOT | runtime | current | X5 |
| I-FLOW | runtime | current | gate |
| I-SHELL | runtime | current | gate |
| I-POLICY | runtime | current | gate |
| I-FAULT | runtime | current | gate |
| I-COMPAT | runtime | current | gate |

합계
- current 60개
- impact-compared 11개. C16에서 굵게 표시한 미관측 부분은 모두 C18 기록으로 채워졌다.
- historical-stale 0개

aggregate validator는 CW-16 소유 7개에만 실행했다. 71개 전체에 대해서는 진단 실행을 하지 않았다. G1-COLOR의 "current"는 gap-c18(gate 밖 check)에 근거한다. PLAN의 G1-COLOR는 `kind: feasibility`, `evidence_status: pending_current_candidate`로 남아 있다(이 문서는 PLAN을 바꾸지 않는다).

## 10. 실제 모델 smoke

`run/result-p27-cw16-rm-01.md`는 **C16(HEAD `e635723`)에서 실행한 확신용 기록이다. C18 gate 근거가 아니다.** 2026-10-08 21:40–21:47 KST, omp/18.8.0, 요청 32/40(pause 36 미도달), error 0, aborted 1(중단된 첫 시도의 RM2).
- **RM1**(짧은 정상 흐름, 8 요청): 통과. 입력부터 최종 답변까지 21.9 s, done 1회.
- **RM3**(수동 manager turn 중 pause/resume, 1 요청): 통과. pause 31 s 동안 요청 증가 0, resume 뒤 replay와 중복 입력 없음. 진행 중인 자동화 run이 없어 `interruption: not_needed`였고, "turn abort 표시"는 관측하지 못했다.
- **RM4**(열린 Task 중 backend SIGKILL 뒤 재시작, 19 요청): 통과. `same_boot_crash`, `backend_restarted` notice 1회, survivors 3개 열거(`stoppable:false`), 재실행 없음, manager가 continue와 cancel을 거쳐 취소로 끝냈다. 정상 흐름의 2.4배 비용이다(O1).
- **RM2**(210 s 장기 명령): **실행하지 않았다.** RM4 뒤 남은 예산에 들어가지 않았다. ≥200 s 명령의 terminal_check·terminal_done·status_check 억제는 실모델로 확인하지 못했다. run 진행 중 pause의 turn abort 표시도 확인하지 못했다.
- 이 기록이 발견한 O3(재시작 상태줄이 backend 수명 동안 남음)와 O4(활성 작업 중 `shutdown --yes`의 10 s timeout과 traceback)는 fix-03에서 고쳤다. 그 수정은 fake OMP와 단위 테스트로 확인했고(`test_cli_shutdown_wait_cw16`, `test_live_shutdown_slow_turn` 등), 실제 OMP의 turn abort 소요 시간은 측정하지 않았다. C18에서 실모델 smoke를 다시 실행하지 않았다.

## 11. pending, unknown, 잔여

완료 판단에 쓴 Root 결정
1. (해소) §9 G1-COLOR 강조는 `run/result-p27-cw16-emph-01.md`(`run/cw16-emph-c18/`)로 C18에서 관측했다.
2. (Root 수용) §10 실모델 RM2와 run 진행 중 pause의 turn abort는 실모델로 확인하지 못했다. smoke는 C16 확신용 기록이고 gate 근거가 아니다. 같은 동작은 C18 scripted runtime(P1, P3)으로 관측했다.
3. (Root 수용, 후속 관찰) compat 첫 실행의 Esc 실패(원인 미조사)와 `test_product_pty` spawn-order race(fix-05 자체 실행에서 재발). 둘 다 intermittent로 추정하며 확정하지 않았다. Root는 compat를 r2로 판정했다.

C16 대비 해소된 것: 18.2.10 pin skip 29건(§4.1), "실행 중 backend 재사용"과 frontend SIGKILL 재attach·실제 pane queue_full·편집 키·scroll·non-empty composer·`/new`의 미관측(§9), review P3-1(retry_limit 항목)과 P3-5(SH5 사유)의 일부, O3·O4, C17 review P2 2건.

잔여 P3(판정을 바꾸지 않음)
- review-01
  - P3-1(부분): `held:retry_limit`의 `detail`은 있으나 실모델 보고 내용은 확인하지 못했다.
  - P3-3: 보고서 digest가 log로 고정되지 않는다(부록 A로 보완).
  - P3-4: `real_model_requests`는 상수다.
  - P3-5(부분): SH5 사유는 hook 가능성을 적게 됐으나 단정하지 않는다. trap DEBUG는 호환으로 처리된다.
  - P3-6: 세션 설정 변경은 revision 변경으로만 관측했다.
- fix-review-02
  - P3-1: env-names 파일 mode가 0664이고, hook이 못 쓰면 오래된 파일을 읽는다.
  - P3-2: `_cancel_notice_state` 표시 시점.
  - P3-3: 끝난 run의 resume은 파일·프로세스 대조 없이 coordinator 경로로 간다. Root가 이를 수용했다.
- c18-review-01
  - P3-a: `SHUTDOWN_REQUEST` 도중 Ctrl-C. Root가 고쳤다고 하나 검증 기록은 읽지 못했다.
  - P3-b: `bfightmagenta`는 pyte 오타를 이름 그대로 매핑한 것이다. pyte가 고쳐도 무해하다.
- CW-19 carried
  - project 512 MiB cap에 rotation이 없다.
  - hold 해제 직후 0.5 s 안의 순서 역전.
  - to-worker skill 길이 10994/11000.
- B3 관찰
  - 인수 요청 중 점검 보류 사유가 review 상태에 드러나지 않는다.
  - `setsid`(`-w` 없음)로 double-fork한 daemon은 추적되지 않는다.
- 실모델 smoke 관찰(C16): O1 복구 비용, O2 취소 직후 worker의 `to_manager done` 거절(무해), O5.
- 색(C-D73): system 색 0·9·10·11·13·14·15는 pane 화면(pyte)에서 이미 cube 색과 RGB가 같아 구분이 사라진다. 이 색들은 같은 RGB의 cube index로 그린다. 사용자 terminal theme가 system 색을 바꾼 경우 차이가 날 수 있다. 원본 24-bit fidelity는 [FOLLOWUP-TRUECOLOR.md](FOLLOWUP-TRUECOLOR.md)다.
- 환경 특성(COMPATIBILITY §2)
  - herdr 0.9.3은 약 1 MiB를 넘는 paste를 전달하지 않는다.
  - tmux 기본 `set-clipboard external`.

## 부록 A. 시나리오 보고서 sha256(`run/cw16-formal-c18/reports/`)

C18 보고서다. `cw16-matrix-5ba4d64c`는 compat 첫 실행(exit 1), `cw16-matrix-984764a2`는 r2(exit 0)다. C16 보고서(`run/cw16-formal/reports/`)의 sha256은 C16 기록에 있으며 이 부록에서 뺐다.

| 파일 | sha256 |
|---|---|
| `c18-fault/fault-matrix.json` | `19ab252c051dbb3021e33e7c7c3d891c23ae9dde5956db016aa3283b25933a6d` |
| `c18-fault/fault-x1.json` | `a60896aaf773984cc7e7e38cb8fc5bc936e3c356cacffa964bf4ed421ace99ac` |
| `c18-fault/fault-x2.json` | `2d7452cf687b9d09c7a90e7a88737b34dc18be8a9221387cbac3e096976682f2` |
| `c18-fault/fault-x2r-cw19-restart-probe.json` | `d64b60409d9e59f1f334b901f61d635f8eb0867c5df304ce8076e52e9367efd6` |
| `c18-fault/fault-x2r-cw19-restart.json` | `18c5f0fe99f3935fe6f1359cf7a3e4cd001fabded23c3e02f2319b733e5f6400` |
| `c18-fault/fault-x3.json` | `f959a1c20c0a1ce0d6f7ca70277acdbbfffe2d6e95740a1cf9700ee1904e9b92` |
| `c18-fault/fault-x4.json` | `5e2e9b0364eaa7ca5559bc11ca35e94309d392865116bf6ad04e144647c0d873` |
| `c18-fault/fault-x5-vm-reboot.json` | `56f8f634a9c27d879d866bcfc768cb99e64289959deda5be9431516bb9a0fef5` |
| `c18-fault/fault-x6.json` | `d1d9b78f84930b2e2a357d760c8ddc5eb450cd901c9a4bc28367ec496dffb7bd` |
| `c18-fault/fault-x7.json` | `29856356064d5c7ff68e98952ab1da6870a1c73385cd811f5b285523d9757e1a` |
| `c18-fault/g3-version-record.json` | `acc53c73f5ab5d2d572404cb1a62c5c7769edfcff9ba0414ce745a0db78c7e75` |
| `c18-fault/vm/guest-log.jsonl` | `3a1c3cdd9995593874dab78766d2beecb7558161f540d73461777f2c6452754f` |
| `c18-fault/vm/guest-manifest.json` | `38bfb2355845c217b9d82e4b12a16c87bb50ef374f950cb12b09639f9f6712ec` |
| `c18-fault/vm/host-manifest.json` | `38bfb2355845c217b9d82e4b12a16c87bb50ef374f950cb12b09639f9f6712ec` |
| `c18-fault/vm/vm-reboot-report.json` | `926bc5cbf25f0e5573a240d2803a0ac21efb434f7dea5f47307ca7561d81d62c` |
| `c18-flow-policy/flow-F1-F4.json` | `76dc0c5deb7e6d0b956248ab113e9aedd3674a36266aa2aaaf6b8f2a94f3cc22` |
| `c18-flow-policy/flow-F2.json` | `5f159bd8a1ebc9714512f850e7f44192630de23f8fc755715fd0332878f97905` |
| `c18-flow-policy/flow-F3.json` | `2e61573bba0cd36842495edcbb91952247be41676065b38ba85a61c310b76666` |
| `c18-flow-policy/flow-F45.json` | `3ec9bdb7dd4327d79bf5b7a90bb2f0da138c3851ed654e46792607f7279c927b` |
| `c18-flow-policy/flow-F5.json` | `1a6d4cf0366715795214633f236378d4b4af09e8fb3cfe76fdeaac855b6eb0f6` |
| `c18-flow-policy/flow-F6-F7.json` | `7ea094d45a9fed42e8a7a5e03129601ab904817b8a87ebed23b59e3a450182da` |
| `c18-flow-policy/flow-policy-summary.json` | `2fc83714ef7a66f51b843852cf6638b1eee7eaadf0ed9442556755cf168f9745` |
| `c18-flow-policy/policy-CLK-clock-fixture-suite.json` | `b92693450f15239baa48d87d74a4f39c5dc51f442d4e3e6988ab4704a4085b93` |
| `c18-flow-policy/policy-P1-P7.json` | `d88f427342738fcaf5bcc0b2926c948e1cdcf6966850fcb0817712ad1350d728` |
| `c18-flow-policy/policy-P2.json` | `0a2d5910d9c5efa2ee52299d1d397cf67e4e0fbe042fd5a4fbb743b8203eb90f` |
| `c18-flow-policy/policy-P3.json` | `dc2544079de76648988b38d70101df1b3c0b3bac0a3a81c81671d758a9851699` |
| `c18-flow-policy/policy-P4.json` | `5f48e75b39e1801f76bdeef6ae1921b597a5f63202b14c87070a0f822b4bb317` |
| `c18-flow-policy/policy-P5.json` | `6a9e9ac88c1ab720ac670583b71386a6a3c8516ed43550761f199c6a9a83ef30` |
| `c18-flow-policy/policy-P6.json` | `1a350dd973fd3f30be001b06622314ce08fbc3d9fe4c52f32fbb02dda79285cf` |
| `c18-flow-policy/policy-P8.json` | `572b2bf35106ec4b87102b87a42381eb91f2cd7619cf2a4b36e44bf57b7887c9` |
| `c18-gap/gap-G1-COLOR.json` | `f25b82753d51599618900aa898a7f440f76f5216613771c04feeac70768d11ad` |
| `c18-gap/gap-G1-SCROLL.json` | `7657733f81b5d3bd7767ca197c8fb04cc25331c04c6035fea8f397e229cd4da4` |
| `c18-gap/gap-G3-COMPOSER.json` | `0b395698297791235effed8f667239c47734eda9d97c480b50a7f70bf1205792` |
| `c18-gap/gap-G3-SESSION.json` | `1e52d2b02130394c51e5d69d1ece8639d332e6ec674a33d61b5e6f9ecf54a092` |
| `c18-gap/gap-summary.json` | `71cf6f8161da8c873072b560a0b1eb91f571b3834496063868c56e9dbbae82d5` |
| `c18-shell/shell-SH1-SH4-bash.json` | `c81ca185550d493d1aea8214f36768526d37052b3a9cfa330d9a904bb207afa7` |
| `c18-shell/shell-SH1-SH4-dash.json` | `5fdf937d95c7afd63eaa017e3179826d05eb7f77698f8347a4deac37fa95d34c` |
| `c18-shell/shell-SH2-bash.json` | `2bb976be2ca2d933231e601d5cfca9e3bfed734f093b74a43633021bc27bce79` |
| `c18-shell/shell-SH2-dash.json` | `2496cafa9fd9e5c6d06b256897c3fb69fb01157ae526096d786c28f54eda235a` |
| `c18-shell/shell-SH3-bash.json` | `9c80a2a28c4d7050ba7a48fc2524a4220e13ab439db9c65e3a9ba2709b408888` |
| `c18-shell/shell-SH3-dash.json` | `d5795ba2512601f27699a0396bfe546ba6df415e8bd0952238eb81b5a1d64033` |
| `c18-shell/shell-SH5-prompt_command-bash.json` | `cdd10c63b3746062ff1b3cde170b1dd2a6eff5c85a9324aa1c3d741e5a491134` |
| `c18-shell/shell-SH5-ps1-dash.json` | `7c845caff4467eb77f426bb0dc99bf0235e0ec129d64e28c64f1855336b450d1` |
| `c18-shell/shell-SH5-trap_chld-bash.json` | `e46abe3817f7850358fe389289933a2da241ea1df9bd8c4654476da049a546d7` |
| `c18-shell/shell-SH5-trap_chld-dash.json` | `d21029cedc8c2309e71318b49ec040656998cd8786c3fc298d8972c1643c37dd` |
| `c18-shell/shell-SH5-trap_debug-bash.json` | `07e7cbfa2e04512e6c29a6885f0c2815bddf0ddf24070b26e7d71a93fb160cd2` |
| `c18-shell/shell-summary.json` | `7d97ccb18de7e10d5715db039697ee75d5112f69c475884a1fb953e00b5528d9` |
| `cw16-matrix-5ba4d64c/compat-herdr-bash.json` | `ee14b20e109c69d0a8361a6bc6f415b06677343114babb3192aa429530db8d97` |
| `cw16-matrix-5ba4d64c/compat-herdr-dash.json` | `7ca08be5d8569bb45e724bf4dbdb4eeb11b2dcbddc2b2ac5980982afda9e7ecb` |
| `cw16-matrix-5ba4d64c/compat-matrix.json` | `a5282c226123e2a8d0261a49799288cc802489a4ba5ee7dddaf581cc1443c76c` |
| `cw16-matrix-5ba4d64c/compat-plain-bash.json` | `43b99ad4bce5920cb4581aa2b439a7107c44d553e033dd9b4522694ed8c30a41` |
| `cw16-matrix-5ba4d64c/compat-plain-dash.json` | `0a6d177dc93fe0d51ac1c939d76910c324f0ab0a732ed9f0b75a44e27d851b30` |
| `cw16-matrix-5ba4d64c/compat-tmux-bash.json` | `972ebc6b6dfd87899036f90059aec57ed51c7a1a9a8843da8d5c724b9ea82f54` |
| `cw16-matrix-5ba4d64c/compat-tmux-dash.json` | `1b8285c7dd89a0b14d257271e3dc6022bd13f06f970c8f7a2ac0c182c629b6a5` |
| `cw16-matrix-5ba4d64c/sel-1.json` | `b1216b47fdcb960597f6a90995b18b0a7b53784e7a30d88cb9d47f57af629b9d` |
| `cw16-matrix-5ba4d64c/sel-2.json` | `31b6330e0b99f2f43c774a000d967ebf755d1d63576a341285461bc0c78d383d` |
| `cw16-matrix-5ba4d64c/sel-3.json` | `a535906a5d1710f8ab4823441149020b5f9dc00e609287a9b75f8216713f6d40` |
| `cw16-matrix-984764a2/compat-herdr-bash.json` | `54c608e525dc9ea946a4d90061016e1b4136a628be2847947b1ae7482f5d1020` |
| `cw16-matrix-984764a2/compat-herdr-dash.json` | `e7697813f1e95201ca839f978a696c0bceba33c1b82eae39bc8424510ea7ee4c` |
| `cw16-matrix-984764a2/compat-matrix.json` | `cced9d3f6b1dc64c1ace0e252896bc0b4a884536ee484f4bb7a4249072067a40` |
| `cw16-matrix-984764a2/compat-plain-bash.json` | `a354ad13a622e0490689ead41d39faa92a2b3a0fae127c8d1d6e5971eb24a357` |
| `cw16-matrix-984764a2/compat-plain-dash.json` | `693d4229993efe707bf6cb40bbbd375f53eeb966cf1d400fa2a7c2ff14df9479` |
| `cw16-matrix-984764a2/compat-tmux-bash.json` | `2be05919850a683660a9805285e6cd841a6b585cc3fef3d8402c3d85d8f03250` |
| `cw16-matrix-984764a2/compat-tmux-dash.json` | `ac1ecca0b0e35bf45784a38056cecb22fa3d0cc84b88a809a8e77b38cf32c2b9` |
| `cw16-matrix-984764a2/sel-1.json` | `fa23e786443bd5970a65350b81893eeed0d2b63b668242efd5b02937f3740883` |
| `cw16-matrix-984764a2/sel-2.json` | `5245b7a39b56830ef9772a95d7d8c3e63723cf5fed29b1f5f4c72228774d9b0a` |
| `cw16-matrix-984764a2/sel-3.json` | `9387e4ca5a2173094f6c0efb8ea68bc40692e1beadf79d3c76121289e6ae479f` |
