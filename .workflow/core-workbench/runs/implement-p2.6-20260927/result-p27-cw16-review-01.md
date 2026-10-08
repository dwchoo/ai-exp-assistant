# p27-cw16-review-01 — CW-16 동결 후보 C16 최종 검증 review (fresh reviewer, read-only)

작성 2026-10-08. 대상 C16 `f8ad6ec9…f4e3d` (HEAD `e635723`). 이 파일 외 쓰기 0, 모델·provider 요청 0, 자격 증명·타 프로세스 environ 미열람, 사용자 tmux/herdr·VM·sandbox·실행 중 real-model smoke(`/tmp/wb-cw16-rm-*`) 무접촉, 신호 0, commit·graphify update 없음. 최종 gate는 실행하지 않았다. 재실행한 것은 `/tmp` 출력 snapshot 1회뿐이다.

## 판정: **pass** (CW-16 소유 7 item 기준, 조건 3개는 Root 마감에서 처리)

7개 item(P-C-AC-19/20, I-FLOW/SHELL/POLICY/FAULT/COMPAT)의 runtime 근거는 실제 OMP 18.8.0 두 개와 scripted provider로 C16 제품 진입점·UI에서 관측됐고, 형식 기록은 요구 형식과 맞는다. 7개 item의 acceptance 합집합은 C-AC-01..34 전부다. 아래 P2는 7-item gate 판정을 뒤집지 않지만, Root의 71개 전체 집합 연결(§8)과 VERIFICATION 기록에서 반드시 반영해야 한다.

## 직접 확인한 것

| 항목 | 결과 |
|---|---|
| 후보 고정 | 현재 workspace snapshot(exclude 동일, `/tmp`) = `f8ad6ec9…` = `p27-snapshot-cw16-C16-final.json`, 파일 차이 0. `git status -- src omp_bridge tests docs pyproject.toml` 빈 상태 |
| 형식 check 6개 | compat/fps/fault/regression-a/-a-r2/-b 모두 before=after=C16, stable, timed_out false, `log_sha256` = 실제 log sha. exit: 0/0/0/**1**/0/0 |
| observed 3개 | execution과 `items`를 뺀 모든 필드가 동일(사후 변조 없음). 7 item 모두 runtime·passed·unknowns [] |
| argv 격리 | 전 check `env -i`(TMUX*/HERDR_* 제거). harness `assert_env_isolated`(cw16_harness.py:155)가 fake HOME·빈 agent.db(:797)·proxy 차단을 fail-closed로 강제 |
| 보고서 | `cw16-formal/reports/**` 시나리오 JSON의 시작·종료 시각이 각 check 창 안이며 모든 step pass. n/a는 plain C6 2건뿐이고, matrix 테스트가 plain C6 외 n/a를 거절(test_cw16_runtime_matrix.py:298-301) |
| vacuous 방지 | StepRunner는 AssertionError/예외를 fail로 기록(cw16_harness.py:1163-1186), `requires` 미충족은 not_run → 테스트 실패. live 로그에 skip 0 (fps 14+11, fault 8+3, compat 1 live) |
| X5 VM | manifest host=guest=`514dd5c6…`(91 파일 = `git ls-files src omp_bridge tests/recovery_boot` 91), driver sha 기록, boot_id 2회 변경 |
| C-AC 링크 | requirements 7 item의 acceptance 합집합 = C-AC-01..34 |

## Root 판정 대조

1. **C-AC-07 retry 한도 보고 (held:retry_limit + skill + run별 보고)**: 수용. 제품 측 강제(4번째 재실행 `held/retry_limit`, runs_started 4, revision 변경 뒤 횟수 유지, live_cw16_flow_policy.py:1071-1083)는 runtime 관측이다. "원인·근거·시도·남은 문제 보고"는 manager 모델의 행동이라 scripted로는 관측할 수 없고, 근거는 skill 지시(omp_bridge/skills/to-worker/SKILL.md:50)와 run별 판정 보고 전달이다. 다만 아래 P3-1을 참고한다.
2. **queue_full paste 거절 (fixture + L-CW17-CONTRACT)**: 수용. PLAN이 queue 부족 거절을 fixture 수준 L-CW17-CONTRACT에 배정했고, P-C-AC-19 check 문안은 queue_full을 요구하지 않는다. C16 regression-a-r2에서 fixture(`test_ui_server` fake controller, UI ScriptedServer)는 통과했다. 단, 실제 pane 버전은 P2-1처럼 skip됐다.
3. **regression-a 첫 실행 exit 1 → r2 clean**: 수용. 첫 기록이 보존됐고, r2는 같은 argv·C16에서 exit 0이다. 실패 2건 모두 사후 조건은 통과했다(kill 테스트에서 survivors []·session/group 잔존 0. 실패한 것은 "kill 전 집합 ⊆ signalled" 단정뿐). 근본 원인은 확정되지 않았고 추정(race)으로 남아 있다는 점은 기록에 그대로 둔다.

## Findings

### P2-1 regression의 skip 29건은 OMP 18.2.10 pin 때문이며, 결과 보고가 이를 드러내지 않는다
- 위치: `tests/backend/live_harness.py:28,46`(`find_omp()`는 `omp/18.2.10`이 아니면 None 반환 → `skipUnless`). 해당 module은 test_live_start(3)·test_live_start_independent(7, "L-CW17-START")·test_live_detach(1)·test_live_detach_independent(1, "L-CW17-DETACH")·test_live_input_independent(4)·test_live_contract_independent(7, 실제 pane queue_full `:289` 포함)·test_live_paste_responsive(1)·test_live_review_fixes_independent_p27c(3)·tests/terminal/test_manual_input_backend_independent(2, `:71`)이다. 근거: `check-p27-cw16-regression-a-r2.log`, `-b.log`의 skip 사유 집계.
- 기록: `result-p27-cw16-b4b-01.md:45,184`는 "backend 1114 OK (skipped 30)"로만 적었다. 계획 §3("L-CW17-* … C16에서 재실행")과 C-D72 (2)("고정 probe는 실행 버전 기록 방식으로")에 비춰 보면, 이 live 테스트들은 C16에서 실행되지 않았다.
- 영향: Root가 §8 71개 연결표에서 regression-a/b 통과를 L-CW17-START/DETACH(runtime)의 C16 current 근거로 세면, 실행하지 않은 runtime 항목을 통과로 기재하게 된다. 대부분은 CW-16 live 시나리오가 다시 관측했다(C5 detach≥60s, SEL1–3, C2 paste). 그러나 **L-CW17-START의 "실행 중 backend 재사용(중복 시작 없음)"은 실제 OMP로 C16에서 관측한 기록이 없다.** stub OMP 진입점 테스트 `test_omp_isolation_independent_p27n.py:318-320`만 있다. 7-item gate에는 영향이 없다.
- 조치: VERIFICATION에 skip 29건과 사유를 명시한다. L-CW17-* 항목은 CW-16 시나리오 대응표(impact-compared)로 연결하고, 중복 시작은 fixture/미관측으로 표기한다. pin 해제는 PLAN 범위 결정에 따른다.

### P3-1 C-AC-07 "근거 보고" 내용이 skill에 열거되지 않음
`omp_bridge/skills/to-worker/SKILL.md:50`은 "Report to the user instead of re-running"만 말한다. BRIEF C-AC-07(원인·로그/결과 근거·시도·남은 문제)의 항목은 지시되지 않는다. 실제 모델이 그 항목을 빠뜨려도 제품·테스트 모두 막지 못한다. 수용 판정은 유지하되, VERIFICATION에는 "제품 hold는 runtime, 보고 내용은 skill_behavior 수준"으로 구분해 적는다.

### P3-2 C-D72 (4) 승인 창 "해당 없음"에 근거가 아직 붙지 않음
`check-p27-cw16-compat-observed.json`의 I-COMPAT evidence는 "was not observed to appear in these runs"뿐이다. C-D72 (4)는 근거 첨부를 요구한다(worker `--tools` 허용 목록, manager 기본 구성). Root가 VERIFICATION에 구성 근거를 연결해야 한다.

### P3-3 시나리오 보고서와 버전 기록이 digest로 고정되지 않음
compat log(`check-p27-cw16-compat.log`, 33줄)에는 cell별 결과와 버전이 없다. 버전(herdr 0.9.3 등)과 수치(65.6 s 등)는 run dir(candidate exclude)의 `cw16-formal/reports/**`에만 있다. gate는 log sha만, aggregate는 observed JSON sha만 고정한다. 따라서 보고서가 사후에 바뀌어도 검출되지 않는다. VERIFICATION에 보고서 파일별 sha256 목록을 남길 것을 권한다.

### P3-4 "real_model_requests: 0"이 관측값이 아니라 상수
`tests/integration/cw16_flow_rig.py:275`에서 이 값이 하드코딩돼 있다. 실제 차단은 구성으로 보장된다(fake HOME·빈 agent.db·proxy 차단, cw16_harness.py:155-166, :797). 따라서 결론은 맞지만 "관측"이라고 쓰지 않는다.

### P3-5 SH5 보류 사유 오표시 (제품, B2 O1)
PROMPT_COMMAND(bash)나 PS1(dash)을 덮어쓰면 보류 자체는 정확하다. 그러나 사유는 `not at a clean prompt (mode manual_input)`로 나와 hook 원인을 가리키지 않는다. C-AC-26 판정에는 영향이 없다. 사용자가 잘못된 원인을 보고 대처할 수 있으므로 COMPATIBILITY 제약이나 후속 항목으로 남긴다.

### P3-6 "세션 설정 변경 뒤 재시도 유지"는 revision 변경으로만 관측됨
ticket I-POLICY 문안은 "승인·revision·세션 설정 변경"이다. P4가 관측한 것은 spec revision 변경 뒤 횟수 유지뿐이다. SPEC :104의 "세션 설정"을 revision과 같은 것으로 해석했다면, VERIFICATION에 그 해석을 명시한다.

## Root 마감 조건 (pass의 전제)
1. `WB_CW16_FINAL_REQUEST`로 `test_cw16_final_evidence_validators` FinalRecordsTests를 최종 모드로 실행해 기록한다. regression에서는 skip이었으므로 pass로 세지 않는다.
2. P2-1의 skip 공개와 L-CW17-* 대응표를 작성한다.
3. P3-2 승인 창 근거를 연결한다.
