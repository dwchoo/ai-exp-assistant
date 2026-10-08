# p27-cw16-b1-01 — CW-16 B1: 공통 harness, outer compat matrix, shell 선택 (test_designer)

작성 2026-10-08. W 단계 작성과 비공식 자기 실행 결과다. 정식 근거는 Root가 동결한 후보 C16에서 B4가 다시 실행한다.
제품 코드(`src/**`, `omp_bridge/**`)와 `docs/**`는 바꾸지 않았다. 실제 모델·provider 요청은 0회다(scripted local provider만 사용). 자격 증명은 열지 않았고, 사용자 tmux/herdr 서버·VM·sandbox에는 접근하지 않았다. 신호는 내가 띄운 프로세스에만 pid+start ticks(pidfd)로 보냈다. commit과 graphify update는 하지 않았다.

## 1. 변경 파일

| 파일 | 내용 |
|---|---|
| `tests/integration/cw16_harness.py` (신규) | B1~B3 공통 harness(§2). unittest discover 대상이 아니다 |
| `tests/integration/live_cw16_compat.py` (신규) | outer adapter 3종, C1–C7 × {plain, tmux, herdr} × {bash, dash}, SEL1–SEL3. opt-in `WB_LIVE_CW16=1` |
| `tests/integration/test_cw16_harness.py` (신규) | harness 비live 점검 10건(provider 규칙·오류·stream·abort·주입 기록, shutdown 파싱, env 격리, StepRunner, kill_exact) |
| `tests/integration/test_cw16_runtime_matrix.py` (갱신) | C-D72 (2): 18.2.10/0.9.1 고정 G1 probe outer matrix를 제거하고 제품 경로 matrix(`test_product_path_outer_matrix`, opt-in)와 버전 기록(`test_runtime_versions_are_recorded_not_pinned`, 항상 실행)으로 바꿨다. 구 probe는 모듈 docstring에 historical로 적었다. canonical matrix는 그대로 뒀다. 추가로 `_owned_processes`가 남의 프로세스 `environ`을 읽던 것을 자기 자손으로 한정했고, `_environment()`에서 `TMUX*`/`HERDR_*`/`WORKBENCH_*`를 지운다 |
| `tests/gates/g3_omp/test_tui_extension_fault_integration.py` (갱신) | setUpClass의 `omp/18.2.10` 거부를 실행 버전 기록으로 바꿨다. bridge에 넘기는 `WORKBENCH_G3_EXPECTED_OMP_VERSION`도 실제 버전이 되도록 class 범위에서 patch한다. 기록은 stderr와 `WB_G3_OMP_VERSION_RECORD` 파일 |

`tests/integration/**`의 다른 18.2.10 pin은 없었다(구 outer matrix가 호출하던 G1 probe의 pin은 `tests/gates/g1_vt/**`에 있고 쓰기 범위 밖이라 그대로 두고 호출만 끊었다).

## 2. Harness API (B2·B3용)

import: `sys.path.insert(0, "tests/integration"); import cw16_harness as h` (PYTHONPATH=src, pyte 있는 `/tmp/cw02-g1-venv/bin/python`).

**환경·버전**
- `h.find_omp()`, `h.find_tool(name)`: `$WB_CW16_<NAME>` → PATH → 사용자 `~/.local/bin`(passwd home 기준). 버전을 강제하지 않는다.
- `h.tool_versions(omp=None) -> dict`: omp/tmux/herdr/bash/dash/sh/python/kernel/os. `--version`류만 실행하고 서버에는 접속하지 않는다.
- `h.clean_base_env(**extra)`: 처음부터 만드는 env. `TMUX*`/`HERDR_*`/`WORKBENCH_*`/`DISPLAY`/`WAYLAND_DISPLAY`는 들어가지 않는다. `h.assert_env_isolated(env, root)`: HOME이 root 안인지, proxy 차단 여부, outer context 누출을 확인한다(fail closed).

**ScriptedProvider** (`openai-completions` SSE, 127.0.0.1:0, 모델 없음)
- `p = h.ScriptedProvider(name="wbcw16", default=h.Turn(text="ok"))`. `p.model`(= `wbcw16/scripted`), `p.models_yml()`.
- 규칙: `p.on(predicate, responder, role=None|"manager"|"worker"|"unknown", once=False, name="")`. `p.on_text(suffix, responder, role=..)`은 사용자가 composer에 친 마지막 텍스트를 기준으로 한다. responder는 `Turn`·`str`·`None`(다음 규칙으로 넘어감)을 돌려주는 callable이거나 고정값이다. 역할은 tool 목록으로 정한다(`to_worker`→manager, `to_manager`→worker).
- `h.Request`: `index, role, body, tool_names, last_role, last_text, injected(Workbench 주입 JSON|None), tool_results{call_id: 값}`.
- 응답: `h.text(s, delay=, chunks=, chunk_gap=)`(지연·분할 stream: turn 중단 시험), `h.tools((name, args, call_id), ...)`, `h.error(status, body)`(HTTP 오류: 모델 장애), `h.Turn(...)`. `p.call(role, name, args)`는 고유 id를 가진 tool call tuple을 만든다.
- 관측: `p.count(role)`, `p.wait_count(role, n, timeout)`, `p.snapshot()`(requests/injected/aborted/errors/rule hits), `p.injected[role]`(kind·message_id·handoff·payload_kind·stage·contract), `p.tool_results`, `p.log`(최대 2000, 텍스트 꼬리 120자), `p.aborted`(stream 도중 client가 끊은 요청: interrupt 증거). script 예외는 `script-error` 응답으로 바뀌고 `errors`에 남는다(OMP를 매달지 않음).

**Sandbox** (`with h.Sandbox(label, provider=None, host_shell="bash"|"dash"|"none"|"custom", shell_env="/usr/bin/bash", report=None) as sb:`)
- `/tmp/wbc16<label>-XXXX/`: `h/`(가짜 HOME, 빈 `~/.omp/agent/agent.db`), `p/`(git project, `sb.commit`, `sb.git(...)`), `d/`(data dir, `omp-root/agent/models.yml`에 scripted provider), `t/`(TMPDIR), `r/`(XDG_RUNTIME_DIR). proxy는 `127.0.0.1:9`로 막는다.
- host shell: `bash`=PATH `/usr/bin:/bin:<omp dir>`. `dash`=`/usr/bin` symlink farm에서 bash/rbash/zsh를 뺀 PATH(제품이 sh→dash를 고른다). `none`=omp만 있는 PATH. `custom`=`sb.use_path()`로 지정한다. `h.make_bindir(dir, exclude=, links=, farm=)`, `h.fake_zsh(bindir, sentinel)`.
- 진입점: `sb.start(*extra)`(`start --no-attach --omp <omp> --omp-arg=--model --omp-arg=<p.model>`), `sb.start_args(...)`, `sb.cli(*args)`, `sb.status()`/`sb.status_full()`, `sb.wait_status(pred, timeout, what, pump=[uis])`, `sb.wait_ready()`(phase ready + bridge 둘 다, identity 기억), `sb.shutdown(yes=True)`(`--json`, 결과는 `h.shutdown_result(stdout)`→`{"verified":..}`), `sb.confirm_boot(*extra)`, `sb.ui()`(plain PTY 위의 제품 UI), `sb.attach_argv()`.
- 소유·잔존: `sb.own(pid)`, `sb.remember(snapshot)`, `sb.backend_processes()`, `sb.attach_processes()`, `sb.residue()`(identities/session members/backend/root를 언급하는 프로세스/socket), `sb.wait_no_residue(t)`, `sb.cleanups.append(fn)`. `sb.close()`(멱등): 등록된 cleanup → UI 닫기 → 확인된 shutdown → 잔존 대기, 이때 남은 것을 `residue_before_fallback`으로 기록 → exact SIGKILL fallback → rmtree.
- 명령은 `report`가 있으면 argv·exit·stdout/stderr 꼬리를 기록한다.

**UI driver** `h.Ui(argv, env, cwd, rows=40, cols=170, label=)`: `setsid --ctty` PTY, pyte+REP+SU/SD(`tests/ui/support.rep_screen_classes`). `pump(s)`, `send(bytes, chunk=, settle=)`(쓰는 동안 drain), `type(text)`, `keys(*parts, gap=)`, `paste(body)`(bracketed), `text()/lines()/excerpt(*markers)`, `wait_text(re)`, `wait(pred)`, `resize(r,c)`, `wait_exit(t)`, `close()`(exact kill). `ui.osc52`: 가장 바깥 터미널에 도착한 OSC 52 `(selection, decoded)` 목록. 상수 `h.PREFIX`(Ctrl-]), `h.FOCUS_KEYS`, `h.TITLES`.

**Snapshot helper**: `h.identity(snap)`(backend/pane/bridge/shell parent/supervisor), `h.owner_state(snap)`, `h.pane_sizes_for(rows, cols)`, `h.pty_size_of(pid)`, `h.exe_of(pid)`, `h.owned_environ(pid, ticks)`(내가 띄운 프로세스만), `h.wait_file(path, contains=, endswith=, pump=)`, `h.ticks/alive/kill_exact/processes_mentioning/session_members`.

**보고**: `h.ScenarioReport(scenario, run_id=, **context)` → `$WB_CW16_REPORT_DIR/<run_id>/<scenario>.json`(기본 `/tmp/wb-cw16-reports`, run id는 `$WB_CW16_RUN_ID`). `command()/step()/unknown()/write()`. `h.StepRunner(report).run(name, fn, requires=[...])`: dict 반환은 pass, `AssertionError`는 fail, `h.NotApplicable`은 n/a(이유 포함), 기타 예외는 fail(trace 포함), 선행 실패는 not_run(이유 포함). `failures()`.

**Outer adapter**(`live_cw16_compat.py`; B2·B3가 outer 안에서 돌릴 때 import): `PlainOuter/TmuxOuter/HerdrOuter(sb, rows, cols)`. `open()`(outer 시작 + outer 셸에서 제품 UI 실행), `client`(가장 바깥 PTY 렌더러), `detach_ui()`(prefix q, outer가 보여 준 UI exit status), `reattach_ui()`, `detach_client()/reattach_client()`(plain은 `NotApplicable`), `resize()`, `enable_clipboard()`(tmux `set-clipboard on`), `screen_text()`(client 화면 + tmux capture-pane / herdr pane read), `facts`, `close()`(Sandbox cleanup에 자동 등록). 격리 방식: tmux는 `-S <root>/x.sock -f /dev/null`과 처음부터 만든 env, herdr는 `HERDR_CONFIG_PATH`·`XDG_*`·HOME을 root 아래에 두고 `session list --json` preflight로 socket/session_dir가 root 안인지 확인한다. 사용자 기본 herdr server에는 질의하지 않는다. 조상에 herdr가 있는지는 정보로만 기록한다(`nested_ancestry_info`).

## 3. 자기 실행 결과(비공식, W 단계)

HEAD 6a2ddb2 + 이 batch의 테스트 변경(동결 전). 정식 형태 그대로 실행했다: `WB_LIVE_CW16=1 ... unittest tests.integration.test_cw16_runtime_matrix.Cw16RuntimeMatrixTests.test_product_path_outer_matrix`(`_run_bounded` 안에서 `live_cw16_compat.py` 전체 실행). **exit 0, 790초, 실제 모델 요청 0**(scripted 요청은 회차마다 manager 1회, C4 copy용). 보고서는 `/tmp/wb-cw16-b1-reports/cw16-matrix-66dca4ae/`(`compat-<outer>-<shell>.json` 6개, `sel-1..3.json`, `compat-matrix.json`)에 있다. Root가 run 디렉터리로 복사하면 된다.

| outer / host | C1 | C2 | C3 | C4 | C5 | C6 | C7 |
|---|---|---|---|---|---|---|---|
| plain / bash | pass | pass | pass | pass | pass | n/a¹ | pass |
| plain / dash | pass | pass | pass | pass | pass | n/a¹ | pass |
| tmux / bash | pass | pass | pass | pass² | pass | pass | pass |
| tmux / dash | pass | pass | pass | pass² | pass | pass | pass |
| herdr / bash | pass | pass³ | pass | pass | pass | pass | pass |
| herdr / dash | pass | pass³ | pass | pass | pass | pass | pass |

1. plain PTY에는 outer 영속 계층이 없다. UI 자체의 detach/reattach는 C5에서 확인했다.
2. tmux 기본값(`set-clipboard external`, `-f /dev/null`)에서는 앱이 보낸 OSC 52를 tmux가 버린다. 제품은 "복사됨 … (tmux는 set-clipboard on 또는 allow-passthrough on 필요)" 안내를 띄웠다. 격리 서버에 안내대로 `set-clipboard on`을 적용한 뒤 다시 복사하니 바깥 터미널이 OSC 52를 받았다. 두 관측을 모두 기록했다.
3. herdr 0.9.3은 약 1 MiB를 넘는 bracketed paste를 앱에 전달하지 않는다. raw probe 결과 1 MiB는 도착했고 1.9 MiB와 2 MiB+64는 도착하지 않았다. 그래서 2 MiB 초과 거절은 herdr 경로에서 **제품까지 도달할 수 없다**(`big_paste_product_refusal: "n/a: the outer did not deliver the paste"`). shell 줄이 깨끗하고 `#`가 들어가지 않은 것만 확인했다. 제품 거절은 plain과 tmux에서 확인했다.

회차 공통 관측: C1 시작 줄 `shell bash (/usr/bin/bash)`/`shell sh (/usr/bin/dash)`, host parent exe 일치, isolation ok, focus 4회 이동 동안 owner(user, epoch) 불변. C2 세 pane 입력, 한글·wide·emoji 입력과 multi-line paste가 원본 그대로, `cat -v`에 `^]^[x`, Ctrl-C 뒤 prompt 복귀, 두 OMP composer에서 Alt-Enter 줄바꿈·Ctrl-C 지움·slash 메뉴와 Esc 닫힘·한글 composer. C3 40×170 → 34×150 → 40×170에서 OMP 두 개의 tty 크기와 host `stty size`가 `pane_inner_sizes`와 일치(tmux/herdr는 chrome 1행). C4 OMP `/copy`(18.8.0은 picker가 열리고 Enter로 복사) → 바깥 터미널 OSC 52 수신, host pane의 OSC 52는 전달 안 됨, `Ptmux` 글자 그림 없음. C5 host 사용자 loop 실행 중 UI detach 65.7초: loop 줄 3→68, backend/OMP×2/shell/supervisor identity 동일, owner/mode 불변, Task 불변, provider 요청 증가 0, 재attach 뒤 detach 중 출력이 화면에 보임, `count.txt`는 `once`(중복 입력 없음), Ctrl-C로 prompt 복귀. C6 tmux `C-b d` / herdr `C-b q` → client 종료, 제품 UI 프로세스(같은 pid+ticks)와 attached 상태 유지 → 재attach 뒤 입력 가능, identity 동일. C7 `shutdown --yes --json` verified true, UI client 종료, tmux server 종료와 socket 정리(tmux 3.4는 `-S` socket 파일을 남기므로 harness가 server 종료 확인 뒤 지운다), herdr session stop/delete 확인, `residue_before_fallback` = {}, root 제거.

SEL(같은 run): SEL1 PATH {bash, sh→dash, omp} + SHELL=가짜 zsh → `shell bash (/usr/bin/bash)`, `$BASH_VERSION` 5.2.21이고 `$$`가 parent pid와 일치, host shell env의 SHELL 보존, zsh sentinel 미실행, shutdown verified. SEL2 PATH {sh→dash, omp} → `shell sh (/usr/bin/dash)`, `$$:none`. SEL3 PATH {omp} → attach 모드 PTY에서 exit 2, "OMP Workbench needs Bash or a POSIX sh on PATH…", "No backend was started.", data dir 변화 없음, backend 0, `status` exit 3. (C-D72 (1): SEL4 zsh login 검증은 없다.)

비live: `test_cw16_runtime_matrix.py` 7건 중 6건 OK, 1건 skip(opt-in live). canonical matrix, timeout/pipe/output bound 점검, 버전 기록 포함. `test_cw16_harness.py` 10건 OK.

G3 버전 기록 실행(`tests/gates/g3_omp/test_tui_extension_fault_integration.py`, OMP 18.8.0, fake HOME, proxy 차단, 기록 `/tmp/wb-cw16-b1-reports/g3-version-record.json`): **3건 중 1건 pass, 2건 fail**(§4 F1). 로그는 `/tmp/wb-cw16-b1-reports/g3-fault-integration.log`.

자기 실행 중 고친 것(같은 W 단계 안): (a) `/copy` picker 대응, (b) shutdown JSON 파싱(앞에 사람용 줄이 있고 값은 `shutdown.verified`), (c) matrix 요약 시점을 `tearDownModule`로 옮김, (d) `_run_bounded`의 RLIMIT_FSIZE 8 MiB가 모든 자손에 상속돼 OMP 18.8.0이 가짜 HOME에 native addon을 풀지 못해 전 회차가 `degraded`가 되던 문제. 이 문제는 prlimit 재현으로 확인했고, live matrix 호출만 `limit_child_files=False`로 바꿨다(출력 크기 상한 검사는 유지).

## 4. 결함·관찰

- **제품 결함: 없음**(B1 범위 C1–C7, SEL1–3).
- **F1 (G3 고정 probe, 버전 영향, 제품 아님)**: `test_tui_extension_fault_integration` 3건 중 2건(`outage…`, `opt_in_fault…`)이 `assert_clean`의 `cwd_processes_remaining`에서 실패한다. 관측 내용 자체(outage 미성공/no replay, reconnect 동일 pid·session·generation, provider 계수)는 기대와 같다(outage 결과 `passed_pair_tui_outage_delivery`). 원인: OMP 18.8.0이 `omp __omp_worker_daemon_broker`를 띄운다(ppid 1, 자체 session, cwd는 probe worker cwd). 이 프로세스가 probe의 `killpg` 정리 범위 밖이라 측정 시점에 남아 있다가 잠시 뒤 스스로 끝난다(재실행 중 cmdline·ppid로 확인, 내가 신호를 보내지 않았다). 고치려면 `live_tui_extension_fault_probe.py`의 정리/대기를 바꿔야 하는데, 이는 version-record 범위를 넘으므로 손대지 않았다. Root 판단이 필요하다: (a) probe가 OMP broker의 종료를 기다리거나 정리하도록 test delta를 승인, (b) 18.2.10 historical + 18.8.0 영향 대조표에서 "알려진 차이"로 연결. 참고로 제품 경로(C7·SEL)에서는 shutdown 뒤 잔존이 0이었다.
- **O1 herdr 0.9.3 대용량 paste 폐기**(outer 특성): §3 각주 3. COMPATIBILITY 미검증/제약 항목 후보.
- **O2 tmux 기본 `set-clipboard external`**: 앱 OSC 52를 버린다. 제품 안내 문구가 이를 정확히 짚고, 설정 적용 뒤 동작한다.
- **O3 tmux 3.4 `-S` socket 파일 잔존**: kill-server 뒤에도 남는다. tmux 동작이며, harness가 server 종료를 확인한 뒤 지운다.
- **O4 `_run_bounded` FSIZE 상속**: §3 (d). 기존 canonical matrix에는 영향이 없다.
- 기존 `tests/backend/live_harness.py`·`test_live_start_independent.py`·`tests/ui/live_product_omp_independent.py`는 `omp/18.2.10` pin 때문에 18.8.0에서 **skip**된다(쓰기 범위 밖이라 그대로 뒀다). SEL1–3은 이번 harness로 다시 구현해 18.8.0에서 실행했다.
- 제약: `test_cw16_runtime_matrix`의 class skip 조건은 `shutil.which("omp")`이므로 실행 PATH에 `~/.local/bin`이 있어야 한다.

## 5. 버전(기록, pin 아님)

OMP `omp/18.8.0`(`~/.local/bin/omp`), tmux 3.4, herdr 0.9.3, GNU bash 5.2.21(1)-release, dash 0.5.12-6ubuntu5(`/bin/sh`→dash), Python 3.12.3(`/tmp/cw02-g1-venv`, pyte), kernel 7.0.0-30-generic, Ubuntu 24.04.4 LTS. tmux 격리 서버 기본값: default-terminal `tmux-256color`, set-clipboard `external`, escape-time 500. herdr: 격리 session의 socket/session_dir가 sandbox root 안에 있음(preflight true), 조상 herdr 여부는 정보 기록 false(`_run_bounded` 아래). 제품 start 줄의 evidence 표기: `omp/18.8.0 evidence bridge_g3=18.2.10, isolation=18.6.1`(제품 launcher 값이며 변경하지 않았다).

## 6. 정식 실행(B4/R 단계) 명령

```
# 비live(항상): 버전 기록, canonical matrix, bounded runner 점검, harness 점검
PATH=$HOME/.local/bin:/usr/bin:/bin PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 /tmp/cw02-g1-venv/bin/python -m unittest -v \
  tests/integration/test_cw16_runtime_matrix.py tests/integration/test_cw16_harness.py
# live COMPAT 6회 + SEL1-3 (약 25분, 모델 0): _run_bounded 안에서 live_cw16_compat.py 전체 실행 후 compat-matrix.json 판정
WB_LIVE_CW16=1 WB_CW16_REPORT_DIR=<dir> PATH=$HOME/.local/bin:/usr/bin:/bin PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 \
  /tmp/cw02-g1-venv/bin/python -m unittest -v tests.integration.test_cw16_runtime_matrix.Cw16RuntimeMatrixTests.test_product_path_outer_matrix
# G3 고정 probe의 버전 기록 실행
PYTHONPATH=src:tests/gates/g3_omp WB_G3_OMP_VERSION_RECORD=<file> /tmp/cw02-g1-venv/bin/python -m unittest -v tests/gates/g3_omp/test_tui_extension_fault_integration.py
```
실행 셸에서 `TMUX*`/`HERDR_*`를 지우고 돌려야 한다(harness는 자식 env를 처음부터 만들지만, 바깥 runner의 env 위생은 실행자 몫이다).
