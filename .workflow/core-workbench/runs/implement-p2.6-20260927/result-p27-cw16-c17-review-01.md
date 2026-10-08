# p27-cw16-c17-review-01 — CW-16 C17 delta review (fresh reviewer, read-only)

작성 2026-10-09. 대상은 `git diff e635723 81bebe6 -- src omp_bridge tests`(26 files)이고, 근거로 fix-03·fix-04·gap-01·rerun-01 assignment/result, C-D71/72/73, C-AC-22를 대조했다.
- 이 파일 외에는 쓰지 않았다. 모델·provider 요청 0, 자격 증명과 다른 프로세스 environ은 열지 않았다. 사용자 tmux/herdr·VM·sandbox는 건드리지 않았고, commit·graphify update도 하지 않았다.
- 실행은 `/tmp/c17rev`(81bebe6 `git archive` 사본)에서 `env -i`, fake HOME, proxy 차단으로 했다. 결과는 `test_cw16_fix04` 8 OK, `test_cli_shutdown_wait_cw16` 7 OK, `test_product_model_cw19` 5 OK, `test_live_shutdown_slow_turn` 3 OK(fake OMP, 12.9 s)였다. 그 밖에 비live 색 매핑 probe 2개를 돌렸다.

## 판정: **block** (P2 2건. 둘 다 작은 수정이거나 Root/사용자 판정으로 해소된다)

## 확인 결과 (문제 없음)

| 항목 | 결과 |
|---|---|
| O4 confirm 응답 flush | `service.py:635-637` `_close` 첫머리의 `ui.flush(1.0)`는 write만 한다(`ui_server.py:393-406`). slow-term 8 s fake OMP에서 confirm이 즉시 왔다 |
| SIGTERM grace 공유 | `service.py:668-685`: 두 OMP에 신호를 먼저 보내고 grace 3 s를 공유한다. `terminate()`가 False면 `close(signalled=False)`가 다시 보낸다(`panes.py:596-627`). B3 F1 detached scan은 신호 직전에 유지된다 |
| CLI deadline wait | `cli.py:440-495`: `result_deadline` 150 s, stderr progress(human만). 결과가 없으면 `observe` 기반 `_backend_state`가 `ended/alive/unknown` 셋만 돌려주므로 dict KeyError가 날 수 없다. `shutdown: null`이면 "종료 확인 실패"를 쓰고 exit 1이다. 결과가 있을 때의 출력 형식은 유지된다(C-AC-22 충족) |
| O3 상태줄 만료 | `model.py:2085-2101`: settled면 15 s, 그 밖에는 120 s다. survivors 문구는 별도 분기(`:2101`)라 C-D71 (1)이 유지된다. `status --json` 기록은 그대로다 |
| retry_limit detail | `flow_tasks.py:136-151`. skill은 바뀌지 않았다 |
| SH5 사유 | `panes.py:908-916, 946-947`: `submitted_lines`는 prompt marker에서 감소하므로(`prototype.py:285-293`) 조건이 맞다. 단정하지 않는 문구도 타당하다. SH3 `manual_input` 단정과도 호환된다 |
| 색 hex/10진 오인(D1) | 6자리 hex를 `isdigit`보다 먼저 판정한다(`app.py:97-101`). pyte `FG_BG_256` 256개를 왕복 검증했다. 불일치는 문서화된 system 7개(0,9,10,11,13,14,15 → 같은 RGB의 cube)뿐이다. `_SYSTEM16`은 pyte 값과 같다 |
| live_harness sandbox | `sandbox_env`: 처음부터 env를 만들고, fake HOME, 빈 `agent.db`, proxy 6종 차단, `assert_sandboxed`는 fail-closed다. `PI_CODING_AGENT_DIR`를 제거했고, `--version` probe도 sandbox에서 돈다. passwd home은 binary 경로에만 쓴다 |
| independent test 변경 | skip 사유 문자열 10곳, `seed_provider=False` 3곳, symlink `S_ISLNK` 제외 2줄(`test_live_start_independent.py:176-177`)뿐이다. 약해진 단정은 없다. gap COLOR는 `truecolor` 기대값만 `nearest_256`(테스트 안에서 독립 계산)으로 바꿨다 |
| gap 시나리오 | SCROLL·COMPOSER·SESSION의 단정은 유효하다. SESSION은 C-D70 (5) notice 1회를 정확히 단정하므로 더 엄격하다 |

## Findings

### P2-1 OMP umask 077이 OMP의 도구 실행에도 상속되어 사용자 project 파일이 0600/0700으로 만들어진다 (문서·결정 없음)
- 위치: `src/workbench/backend/panes.py:440-442`(`os.umask(OMP_UMASK)`), `launcher.py:486,1060`
- 문제: umask는 OMP의 모든 후손에게 상속된다. worker는 `edit`·`write` 도구를 가진다(`launcher.py:161`). manager는 기본 도구(bash 포함)다. 그래서 data dir 밖, 즉 사용자 project에서 OMP가 만드는 파일과 디렉터리도 모두 owner-only가 된다.
- fix-04 결과는 "host shell(과 실험 자식)은 사용자 umask"까지만 적었다. COMPATIBILITY/VERIFICATION에는 umask 언급이 없다.
- 실패 시나리오: group 공유 실험 디렉터리(setgid lab share, 다른 uid의 container나 batch job이 읽는 경로)에서 일어난다.
  - worker가 `write`로 `configs/exp.yaml`을 만들면 0600이 되어 공동 작업자나 다른 uid job이 읽지 못한다(Permission denied).
  - manager bash의 `mkdir outputs`는 0700이 된다.
  - C16에서는 사용자 umask(예: 002)를 따랐다.
- 보안상 이득은 작다. gap-01 §5 D3에서 data dir, `omp-root`, `agent`는 이미 0700이어서 실제 노출이 없었다.
- 조치: Root/사용자 판정이 필요하다.
  - (a) 수용하고 COMPATIBILITY에 "OMP가 만드는 파일은 owner-only"로 기록한다.
  - (b) umask를 되돌리고, 테스트 단정을 "group/other가 도달 가능한 경로 없음(0700 parent)"으로 바꾼다.

### P2-2 SGR 93/103(bright yellow)을 기본색으로 그린다. C-D73 "index 색은 같은 index, 해석 오류 없음" 위반이다 (기존 결함, gap 미검출)
- 위치: `src/workbench/ui/terminal_g1/app.py:75-93`
- 문제: pyte는 SGR 93/103을 `"brightbrown"`으로 기록한다(`pyte.graphics.FG_AIXTERM[93]`, 실측 `cell.fg == cell.bg == "brightbrown"`). named dict에는 `"brightyellow"`만 있다. 그래서 `_color_index("brightbrown")`는 -1이 되고, `_ColorPairs.pair`는 terminal 기본색으로 그린다. 제품 view·app도 이 경로를 쓴다(`ui/product/view.py:11`, `ui/product/app.py:26`).
- gap COLOR `basic16`은 31/32/34/91/43만 쓴다(`tests/integration/live_cw16_gap.py:248-252`). 그래서 이 오인을 잡지 못했고, G1-COLOR pass가 C-D73 기준을 과대 주장한다.
- 실패 시나리오: host pane에서 `printf '\033[93mWARN\033[0m'`를 실행하면 노란색이 아닌 기본색으로 보인다. `\033[103m` 배경도 같다.
- 조치: named에 `"brightbrown": 11`을 추가한다. gap/unit에 93·103 케이스를 넣는다.

### P3-1 harness가 data dir 아래 `omp-root`/`agent`를 먼저 0700으로 만들어, private-mode 단정이 제품의 생성 mode를 관측하지 못한다
- 위치: `tests/backend/live_harness.py` `wire_provider`(약 :287-304)
- 문제: `test_concurrent_starts…private_modes`(`test_live_start_independent.py:152-180`)의 data dir 0755→0700 단정은 유효하다. 그러나 `omp-root`와 `agent`는 harness가 미리 만들고 chmod 0700 한다. 그래서 제품 `omp_home`이 이 디렉터리를 넓은 mode로 만드는 회귀가 생겨도 이 테스트는 통과한다.
- 조치: wiring 뒤 mode를 일부러 넓혀 두거나, 제품 생성 뒤 seed하는 방식을 권한다. 정식 결과에는 이 한계를 적는다.

### P3-2 `shutdown`(--yes 없음)에서 stdin이 닫혀 있으면 `input()` EOFError traceback이 난다. Ctrl-C 대기 중단은 아무 말 없이 exit 1이다
- 위치: `cli.py:437`(기존 코드), `cli.py:553`
- 문제: O4 범위(`--yes`)는 아니지만 "no traceback" 계약의 가장자리다.
- 실패 시나리오:
  - `python -m workbench shutdown < /dev/null`을 실행하면 traceback이 난다.
  - 150 s 대기 중에 Ctrl-C를 누르면 "종료 확인 실패" 안내 없이 exit 1로 끝난다(C-AC-22상 거짓 성공은 아니다).

## 참고 (비결함)
- system 16색 7개는 같은 RGB의 cube로 그려진다(fix-04가 문서화). COMPATIBILITY에 기록해야 한다. 현재 docs에는 C-D73 한계 문구가 아직 없다(Root/docs 담당).
- rerun-01의 backend intermittent 2건(EAGAIN connection cap, concurrent kill/restart)은 이번 delta가 건드린 코드 밖이다. 재현 근거는 rerun-01 분류와 일치한다.
