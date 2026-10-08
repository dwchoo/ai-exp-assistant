# p27-cw16-gap-01 — CW-16 남은 검증 공백 (test_designer)

작성 2026-10-08. 이 결과는 W 단계 작성과 비공식 자기 실행이다. 정식 근거는 Root가 최종 후보에서 아래 §6 명령으로 다시 실행해야 한다.
- 쓰기: 허용 경로만 썼다. `src/**`, `omp_bridge/**`, `docs/**`, 다른 `.workflow` 파일은 바꾸지 않았다. commit과 graphify update는 하지 않았다.
- 실제 모델 요청: 0회(구성으로 보장). 모든 실행은 fake HOME, 빈 `agent.db` placeholder, `HTTP(S)/ALL_PROXY=127.0.0.1:9`, 처음부터 만든 env(TMUX*/HERDR_* 없음)로 돌렸다. 자격 증명 저장소는 열지 않았다.
- 신호: 내가 띄운 프로세스에만 harness의 exact pidfd 경로로 보냈다. pkill은 쓰지 않았다. 사용자 tmux/herdr, VM, `~/wb-urux-sandbox`는 건드리지 않았다.
- `/proc/<pid>/environ`: probe 1회에서 내가 띄운 manager OMP만 읽었다. HOME과 proxy 값만 출력했다.
- 동시 작업: product fixer가 `src`와 일부 테스트(`test_panes`, `test_task_flow`, `recovery_boot/*`, `test_product_model_cw19`)를 수정 중이었다. 내 자기 실행 중 `git diff src`의 sha256 앞 16자는 `22d3212c5a8015db`로 실행 전후가 같았다. 모든 shutdown은 `verified: true`, 잔존 0이었으므로 shutdown/banner 영역으로 돌릴 실패는 없다.

## 1. 판정 요약 (OMP `omp/18.8.0`, 기록이며 pin 아님)

| 공백(VERIFICATION 초안 §4.1/§9/§11) | 결과 | 근거 |
|---|---|---|
| 18.2.10 pin skip 29건 | **실행 29건: 28 ok, 1 FAIL** | §3. 실패 1건은 제품/테스트 기대 충돌(D3) |
| L-CW17-START 실행 중 두 번째 start의 backend 재사용 | **pass** | `test_live_start` 1번: 동시 start 2개 → backend 1개, `attaching instead of starting`, log `starting in` 1회, `another backend holds this data dir` 1회. `test_live_start_independent` 동시 start 3개 → backend 1개(실패 단정보다 앞에서 통과) |
| L-CW17-START 외부 lock, stale socket, symlink data dir | **pass** | `test_live_start_independent`: lock 보유자 비종료(12.2 s에 반환), 죽은 backend 뒤 재시작, symlink 거절 |
| L-CW17-DETACH frontend SIGKILL 후 >60 s 재attach | **pass** | `test_live_detach_independent` ok(68.9 s). `test_live_detach` ok(66.9 s) |
| 실제 pane queue_full | **pass** | `test_live_contract_independent.test_queue_full_on_a_stalled_real_pane_rejects_whole_frame_with_reason` ok. connection cap, mid-stream 단절, framing, version mismatch, 2 MiB 초과 거절도 ok(7/7) |
| paste 응답성 | **pass** | `test_live_paste_responsive` ok(2 MiB raw paste 중 snapshot < 1 s). `p27c`: sleeper 500개와 2 MiB 순서, paste 중 foreground 변경, pipe·XFSZ 뒤 정상 종료 3/3 ok |
| 편집 키 변형(커서·backspace·Ctrl-C) | **pass** | `test_live_input_independent` 4/4 ok |
| terminal 2건(bash·dash 수동 입력 ui_v1) | **pass** | `test_manual_input_backend_independent` 2/2 ok |
| G1-TEXT-TOOLS scroll | **pass** | gap `SCROLL` 4단계(§4) |
| G3-TUI-CONTENTION 비어 있지 않은 composer 보류 | **pass** | gap `COMPOSER`(§4): 30.0 s 뒤 `report_wait` 표시, 비운 뒤 0.2 s에 1회 전달 |
| G3-SESSION(`/new`) | **pass** | gap `SESSION`(§4): 같은 pid, generation 1→2, 재전송 0, `manager_recovery` notice 1회 |
| G1-COLOR | **FAIL (제품 결함 D1, 한계 D2)** | gap `COLOR`: basic16·256 cube pass. grey ramp, 256 system index, 24-bit RGB fail |

## 2. 변경 파일 (모두 허용 경로)

| 파일 | 변경 |
|---|---|
| `tests/backend/live_harness.py` | **pin → 버전 기록**: `OMP_VERSION` 비교를 지우고 `HISTORICAL_OMP_VERSION="omp/18.2.10"`을 기록용으로만 둔다. `find_omp()`는 `omp/*`이면 받아들이고, 버전을 stderr와 `$WB_LIVE_OMP_VERSION_RECORD`(JSON lines)에 기록한다. `--version` probe도 sandbox env에서 돈다. 후보 탐색 순서는 `$WB_LIVE_OMP`, PATH, passwd home의 `~/.local/bin/omp`다. **sandbox**: `LiveBackend` env를 `os.environ` 복사 대신 `sandbox_env()`로 처음부터 만든다. root 안의 fake HOME과 빈 `~/.omp/agent/agent.db` placeholder, XDG_*/TMPDIR/XDG_RUNTIME_DIR, proxy 차단, `NO_PROXY=127.0.0.1`를 넣는다. `assert_sandboxed()`가 fail-closed로 확인한다. `PI_CODING_AGENT_DIR`는 뺐다: 제품(C-D64)이 그 값으로 사용자 auth store를 찾으므로, 이제 store는 fake home의 빈 placeholder이고 제품은 그 링크만 만든다. **provider 연결**: `wire_provider()`는 `start_args()` 때 counting provider의 `models.yml`을 제품 OMP home(`<data>/omp-root/agent/`, 0700/0600)에 둔다. C-D64 이후 profile의 `models.yml`은 제품 OMP가 보지 못해 `--model cw17-probe/scripted`가 resolve되지 않았다. 따라서 `provider.requests == 0` 단정이 공허하지 않다(probe: manager composer 입력 1회 → counting provider 요청 1). symlink data dir에는 쓰지 않는다. `seed_provider=False`로 끌 수 있다 |
| `tests/backend/test_live_{start,start_independent,detach,detach_independent,input_independent,contract_independent,paste_responsive,review_fixes_independent_p27c}.py`, `tests/terminal/test_manual_input_backend_independent.py` | **wiring만**: `skipUnless` 사유 문자열 10곳 `"real OMP 18.2.10 is required…"` → `"real OMP is required… (version recorded, not pinned: C-D72 (2))"`. 그리고 data dir이 start 전에 비어 있어야 하는 3곳만 `self.live(seed_provider=False)`: `test_live_start.py` `test_path_without_bash_or_sh…`, `test_live_start_independent.py` `test_no_bash_no_sh_on_plain_pty…`와 `test_symlinked_data_dir…`. assertion·관측은 하나도 바꾸지 않았다. 모듈 docstring의 "OMP 18.2.10" 문구도 그대로 뒀다 |
| `tests/integration/live_cw16_gap.py` (신규) | W 단계 gap 시나리오 4개(COLOR, SCROLL, COMPOSER, SESSION). opt-in `WB_LIVE_CW16=1`, B1 `cw16_harness`와 B2 `cw16_flow_rig`만 쓴다. `test*.py`가 아니므로 discover 대상이 아니다. 보고서는 `$WB_CW16_REPORT_DIR/<run>/gap-*.json`과 `gap-summary.json` |

`cw16_harness.py`, `tests/gates/g1_vt/**`, `tests/gates/g3_omp/**`는 바꾸지 않았다. G1 live probe는 historical(C-D72 (2))로 두고, G1-COLOR는 제품 UI 경로 시나리오로 다시 관측했다. `tests/ui/live_product_omp_independent.py`는 쓰기 범위 밖이라 여전히 18.2.10 pin이다.

## 3. 전환한 18.2.10 고정 테스트 자기 실행 (29건)

명령은 §6-A다. 모듈별 순차 실행이며 HEAD `e635723`, src diff는 위와 같다.

| module | 결과 | 시간 |
|---|---|---|
| `test_live_start` | 3/3 ok | 9.7 s |
| `test_live_start_independent` | 6/7 ok, **1 FAIL** `test_concurrent_starts_yield_one_backend_and_private_modes` | 36 s |
| `test_live_detach` | 1/1 ok | 66.9 s |
| `test_live_detach_independent` | 1/1 ok | 68.9 s |
| `test_live_input_independent` | 4/4 ok | 55.8 s |
| `test_live_contract_independent` | 7/7 ok | 45.2 s |
| `test_live_paste_responsive` | 1/1 ok | 5.6 s |
| `test_live_review_fixes_independent_p27c` | 3/3 ok | 29.3 s |
| `tests/terminal/test_manual_input_backend_independent` | 2/2 ok | 50.7 s |

skip은 0건이다. 버전 기록은 모든 모듈에서 `{"version": "omp/18.8.0", "historical": "omp/18.2.10", "pinned": false}`였다.

**실패 원인(D3)**: 단일 backend, log 1회, data dir 0700, socket 0600 단정은 모두 통과했다. 실패한 것은 마지막 단정 "data dir 아래 어떤 항목도 group/other 비트가 없다"이고, 항목 25개가 걸렸다. 재현 probe 결과는 다음과 같다.
- runner umask 002에서 OMP가 만든 항목이 umask를 따른다.
  - `omp-root/agent/{history.db,models.db,skill-descriptions.db}`와 그 `-wal`/`-shm`은 0644다.
  - `last-changelog-version`은 0664다.
  - `omp-root/{cache,logs}`는 0775다.
  - `omp-root/bun-transpiler-cache`는 0755/0644다. 이 경로는 제품이 `BUN_RUNTIME_TRANSPILER_CACHE_PATH`로 지정한다(`omp_home.py:395`).
- umask 077에서 다시 하면 이 항목들은 사라진다. 남는 것은 `omp-root/agent/agent.db` 하나다. 이 항목은 C-D64 설계상 symlink이고 lstat mode가 항상 0777이다.
- harness가 만드는 `omp-root`, `agent`, `models.yml`은 0700/0700/0600이라 원인이 아니다.

## 4. gap 시나리오 자기 실행 (`live_cw16_gap.py`)

최종 코드 전체 실행은 run `gap-self-3`(124.5 s)이다. 보고서 sha256 앞 16자는 다음과 같다. 위치는 `/tmp/wbgap-runner/reports/gap-self-3/`이다.
- COLOR `c63bb5327567c9d4`
- SCROLL `b4ad151fee639700`
- COMPOSER `a3329399226509d9`
- SESSION `38417fd66cf8d0cf`
- summary `db9dfa0f5b31a085`

scripted provider 요청은 COLOR 0, SCROLL 0, COMPOSER m3/w3, SESSION m5/w5다. cleanup은 모두 `residue_before_fallback {}`, root 제거였다.

- **SCROLL**(pass, 4/4)
  - host pane에 `seq` 400줄을 찍고 prefix `[`로 들어가면 title이 `[SCROLL live 끝/391]`이 된다.
  - `g`로 `GQS0001`이 보이고 `GQS0400`은 사라진다.
  - background job 출력이 오는 7 s 동안 위치가 그대로다(앞 3줄 동일).
  - PgDn으로 이동하고, `G`로 live에 돌아와 `GQLATE`가 보인다. `q`로 나간다.
  - scroll 키는 shell에 가지 않는다(`command not found` 없음, 다음 echo 정상).
  - Shift+PgUp은 `[SCROLL live보다 15줄 위/395]`가 되고, 입력하면 live로 돌아온다.
  - 마우스 휠은 SGR 64를 4회 보내면 `[SCROLL live보다 12줄 위/397]`가 된다(UI가 mouse reporting을 켠 상태).
  - manager OMP pane의 scroll 모드 진입과 종료도 확인했다.
- **COMPOSER**(pass, 3/3)
  - 열린 work Task를 두고 manager composer에 `GQDRAFT42 keep me`를 입력한다(제출 안 함).
  - 이어서 worker의 `to_manager(progress)`는 `queued`가 된다.
  - 30.0 s 뒤 status `recovery.report_wait={count 1, reason manager_editor_not_empty}`가 되고, UI 상태줄에 "worker 보고 대기 중: manager 입력창을 비우면 전달됩니다"가 뜬다.
  - 그동안 manager 주입 0, manager 모델 요청 증가 0, draft는 화면에 그대로였다.
  - backspace로 비우면 0.2 s 뒤에 1회 전달되고 `report_wait`는 null, UI 문구도 사라진다.
  - draft는 provider 요청에도, 전달된 report에도 나타나지 않았다.
- **SESSION**(pass, 4/4)
  - `/new` 앞에서 worker progress가 manager에 전달된다.
  - `/new`와 Enter 1회 뒤 bridge manager가 다시 등록된다. pid 2269049는 그대로이고 pane process(pid+ticks)도 같다. `pid_matches_pane` true이고 generation은 1→2, session id가 바뀐다.
  - 이후 12 s 동안 주입 메시지 재전송 0, worker 요청 0이다. manager 요청은 정확히 1이며, 그것은 C-D70 (5) `manager_recovery` notice다. notice에는 새 session id와 `reports_resent: 0`이 있고, journal의 `workbench_notice` manager_recovery는 queued 1, sent 1이다. Task(id, revision 1, active, running)는 그대로다.
  - 이어진 worker `done`은 새 session에 1회 전달된다(envelope session id = 새 id, `session_generation 2`). Task는 closed done이 되고, 전환 전 report의 message id는 1회만 전달됐다.
- **COLOR**(FAIL, 3/5 클래스 실패. 실패한 단정은 정확값 그대로 유지)
  - outer는 pyte다. host pane 프로그램이 쓴 색을 outer 셀의 RGB와 비교했다.

| 클래스 | 결과 | 관측(쓴 값 → outer) |
|---|---|---|
| basic 16(SGR 31/32/34/91/43) | pass | 정확 |
| 256 cube(196/46/214, bg 21) | pass | 정확 |
| 256 grey ramp | **fail** | 236 `303030`→`eeeeee`, 244 `808080`→`eeeeee`, bg 240 `585858`→`eeeeee`, 252 `d0d0d0`→`d7d7d7` |
| 256 system index(38;5;1/4/8/12) | **fail** | `cd0000`→`d70000`, `0000ee`→`0000ff`, `7f7f7f`→`878787`, `5c5cff`→`8787ff` |
| 24-bit | **fail** | `123456`→`eeeeee`, `ff8000`→`ffaf00`, `c81e8c`→`d75faf`, bg `fa6432`→`ff875f` |

참고: OMP pane의 outer 전경색은 18종이었고, 모두 cube 근사값이었다.

첫 실행(`gap-self-1`)에서는 SCROLL과 SESSION이 실패했다. 둘 다 내가 새로 쓴 테스트 쪽 문제여서 고쳤다.
- (a) title을 찾을 때 상태줄 "focus: HOST SHELL"을 먼저 잡았다.
- (b) SESSION에서 "전환이 모델 요청 0"이라고 단정했는데, 이는 합의된 C-D70 (5) recovery notice와 어긋났다. 단정을 "요청은 정확히 1, 그 요청이 `manager_recovery`이고 `reports_resent` 0, journal queued/sent 각 1, 주입 재전송 0"으로 바꿨다. 기존 테스트의 단정이 아니며, 원래보다 더 엄격하다.

## 5. 제품 결함·관찰 (src는 고치지 않음)

- **D1 (P2, G1-COLOR)**: `src/workbench/ui/terminal_g1/app.py:68` `_color_index`가 hex 판정(`:70`)보다 먼저 `lowered.isdigit()`를 본다. 그래서 숫자로만 된 hex 색(`303030`, `808080`, `585858`, `123456`, `111111` 등)을 10진 index로 읽고 `min(255, …)`로 잘라 **index 255(`eeeeee`, 거의 흰색)**로 그린다. 제품 view(`ui/product/view.py:11`)도 이 함수를 쓴다. 어두운 회색이 밝은 흰색으로 반전되어 보인다.
  - 재현(비live): `PYTHONPATH=src python -c "from workbench.ui.terminal_g1.app import _color_index; print(_color_index('303030',256))"` → `255`
  - live 재현: host pane에서 `printf '\033[38;5;236mX\033[0m\n'`
- **D2 (G1-COLOR 한계, 기존 G1 기록 "curses 256색 근사")**: 제품 UI는 curses color pair로 그리므로 24-bit RGB는 6×6×6 cube로 근사된다. `38;5;0–15`와 grey ramp도 pane 화면(pyte)이 hex로 바꾼 뒤 cube로 다시 근사되어 원래 index와 RGB를 잃는다. G1-COLOR "RGB/256 fidelity"는 C16 후보에서 충족되지 않는다. 판정(결함 수정 또는 한계 수용)은 Root/사용자 몫이다.
- **D3 (P3, data dir mode / 테스트 기대 충돌)**: 첫째, 제품 OMP home 안에서 OMP가 만드는 파일과 디렉터리가 umask를 따른다(§3). `history.db` 0644, cache/logs 0775, bun-transpiler-cache 0755. data dir와 `omp-root`, `agent`는 0700이라 실제 노출은 없다. 둘째, `test_concurrent_starts…`의 "group/other 비트 0" 단정은 C-D64의 `agent.db` symlink(lstat 0777)와도 충돌한다. umask 077로도 통과하지 않는다. Root 선택지는 셋이다.
  - (a) 제품이 OMP child umask를 077로 둔다. 다만 symlink 문제는 남는다.
  - (b) symlink를 제외하도록 테스트를 바꾼다(test delta 승인 필요. 이번 쓰기 범위 밖이라 하지 않았다).
  - (c) 둘 다.
- **O1**: `/new`는 18.8.0에서 `session_switch`로 처리된다(generation +1). recovery notice 1회는 설계대로다.
- **O2**: OMP 18.8.0은 첫 OMP 시작 때 fake HOME의 `.omp/natives/18.8.0`에 addon을 푼다. 제품 start 줄의 note가 이를 미리 알린다. 사용자 `~/.omp`는 쓰지 않았다. HOME이 fake이기 때문이다.
- **영향**: regression-a(`tests/backend` discover)는 이제 전환된 live 테스트를 실제로 실행한다. 약 +6분이 걸리고 detach 2건은 각 60 s를 넘는다. D3가 해결되기 전까지 **`test_concurrent_starts…` 1건이 실패한다.** 정식 regression env에는 `PATH`에 `~/.local/bin`이 있어야 한다. 없으면 harness가 passwd home의 `~/.local/bin/omp`를 찾는다.

## 6. 정식 실행 명령 (repo root, 바깥 셸에서 TMUX*/HERDR_* 제거)

공통 env:
```
E="env -i HOME=<fake runner home under /tmp> PATH=<user home>/.local/bin:/usr/bin:/bin LANG=C.UTF-8 TERM=xterm-256color \
 PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 HTTP_PROXY=http://127.0.0.1:9 HTTPS_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 \
 http_proxy=http://127.0.0.1:9 https_proxy=http://127.0.0.1:9 all_proxy=http://127.0.0.1:9 NO_PROXY=127.0.0.1 no_proxy=127.0.0.1"
```
- A. 전환한 29건(약 6분):
  ```
  $E WB_LIVE_OMP_VERSION_RECORD=<file> /tmp/cw02-g1-venv/bin/python -m unittest discover -v -s tests/backend -t tests/backend -p 'test_live_*.py'
  $E WB_LIVE_OMP_VERSION_RECORD=<file> /tmp/cw02-g1-venv/bin/python -m unittest discover -v -s tests/terminal -t tests/terminal -p test_manual_input_backend_independent.py
  ```
  (regression-a/b의 기존 `tests/backend`·`tests/terminal` discover에도 그대로 포함된다.)
- B. gap 시나리오(약 2분 30초, 모델 0):
  ```
  $E WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=<dir> WB_CW16_RUN_ID=<id> /tmp/cw02-g1-venv/bin/python -m unittest -v tests/integration/live_cw16_gap.py
  ```
  필터는 `WB_CW16_SCENARIOS=COLOR,SCROLL,COMPOSER,SESSION`이다. 판정은 `gap-summary.json`의 scenario별 `result`로 한다.

자기 실행 기록: log는 `/tmp/wbgap-runner/logs/`(`conv-*.log`, `converted-summary.txt`, `gap-{1,2,3}.log`, `omp-version.jsonl`)에 있고, 보고서는 `/tmp/wbgap-runner/reports/gap-self-{1,2,3}/`에 있다.
