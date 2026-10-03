# OMP pane restart (--resume) & drag-to-copy: 사실 조사 (읽기 전용; omp는 --help만 실행)

## A. OMP pane 재시작 (`omp --resume`)

### 1. spawn과 exit 감지
- argv: `omp_command` (src/workbench/backend/launcher.py:206-211) = `[omp, --config <정적 omp-isolation.yml>, --config <root>/omp-isolation-<role>.yml, --no-extensions, --append-system-prompt "", --no-title, *plan.omp_args, --extension <bridge.ts>]` (isolation_args :194-203).
- `--no-session`/`--session-dir`은 pane 명령에 없음 (src/, omp_bridge/ grep). `--no-session`은 격리 검사 RPC 실행에만 붙음 (launcher.py:131 `_RPC_CHECK_ARGS`).
- role별 차이: overlay 경로 (service.py:155-159), env `WORKBENCH_G3_ROLE`/`WORKBENCH_G3_TOKEN` (launcher.py:494-506). 공통 env: `WORKBENCH_G3_BRIDGE_SOCKET`, `_GENERATION=1` (BRIDGE_GENERATION launcher.py:20), `_EXPECTED_OMP_VERSION`, `TERM=xterm-256color`, `COLORTERM`; `PI_AUTO_QA` 제거 (launcher.py:23-24). cwd = `project_dir` (service.py:162). `PI_CODING_AGENT_DIR`/`--profile`은 쓰지 않음 (launcher.py:13).
- spawn은 `Backend._open`에서 1회 (service.py:154-162): `OmpPane.__init__` (panes.py:149-179) `pty.fork` -> chdir -> TIOCSWINSZ -> `execvpe`. token은 service.py:138에서 생성, :166에서 `del tokens` (bridge와 child env에만 존재) -> 재시작하려면 role별 token/command/env를 보관해야 함.
- exit 감지: `OmpPane.poll()` (panes.py:195-208) `waitid(WNOWAIT)` -> `_pin_session` -> `waitpid` -> `returncode` 설정. `info()`가 `alive=False`, `exit_status` 보고 (:323-327). `pump()`가 매 루프 poll 호출 (:283).
- 상태 변화: `_check_ready` (service.py:220-227; starting 중에는 :234-240)가 `phase="degraded"`, `reason="pane_exited:<pane_id,...>"`로 변경 + `_write_record`. `_exited_panes` :242-247, 주석 :243 "restart is CW-19".
- `Pane.session_id`(uuid)와 `generation=1`은 `__init__`에서 한 번만 설정 (panes.py:101-102); 바뀌지 않음.
- 재시작 차단 요인: 별도 가드가 아니라 코드 경로 부재. `panes`는 `_open`에서만 채워지고, "degraded"에서 "ready"로 돌아가는 분기 없음 (`_check_ready`는 phase != starting이면 ready->degraded만). 종료된 pane 입력은 `admit` -> `PANE_UNAVAILABLE` (panes.py:306-307). 종료 후에도 master_fd는 `close()`까지 열려 있음.
- UI는 display frame의 `(session_id, generation)`이 바뀌면 pyte screen reset + scroll view 해제 (model.py:357-372, `_history_cleared`).

### 2. Bridge
- bridge.ts(`--extension`)가 `WORKBENCH_G3_BRIDGE_SOCKET` unix socket으로 접속 (bridge.ts:115, `openConnection` :526-). `hello{protocolVersion:1, token, role, ompSessionId, generation, pid, ompVersion}` (:541-551); `ompSessionId = ctx.sessionManager.getSessionId()` (:616).
- 서버: `G3BridgeServer._register_peer` (src/workbench/ipc/bridge_g3/mailbox.py:236-270): protocolVersion, role, uuid session id, generation>=1 int, pid>=1 int, token(hmac) 검증. 같은 role의 기존 peer는 교체되고 옛 연결은 닫힘 (:255-269). => 같은 token/role이면 새 OMP 프로세스가 재등록 가능.
- ready 조건: `pid_matches_pane` = `peer.pid == pane.pid` (service.py:319) -> 새 OmpPane의 pid 필요.
- OMP 종료 시: 소켓 종료 -> `_G3RequestHandler.handle` 종료 (mailbox.py:132-154) -> `_unregister_peer` (:272-281)가 peer와 pending ack 제거 -> `bridge_state()` connected=False (service.py:307-320). extension은 `session_shutdown`에서 shuttingDown, `socket.end()` (bridge.ts:631-640). extension은 자체적으로 재접속 타이머(50ms..1s backoff)를 가지지만 shutdown 후에는 중단.
- 시작 격리 검사: `_run_isolation_check` 스레드 1개 (service.py:170-194) -> role별 `check_isolation` (launcher.py:749-807): 같은 command + `--mode rpc --no-session --no-title`, `get_state`/`get_available_commands`만 전송(모델 호출 없음), process group 정리. 결과 `self.omp_isolation` (service.py:189). 시작 시 1회, 재시작 시 다시 실행하는 코드 없음.

### 3. 세션 식별
- bridge가 아는 것은 session id뿐: hello `ompSessionId` -> `BridgePeer.session_id`, snapshot `bridge[role]["session_id"]` (service.py:316). state frame(bridge.ts:157-183)에도 경로 필드 없음.
- session 파일 경로: src/, omp_bridge/ 어디에도 없음 (`sessionFile`/`getSessionFile`/`session_file`/`getSessionDir` grep 0건).
- 실제 사용하는 RPC `get_state` 필드는 `systemPrompt`, `dumpTools`, `model` (launcher.py:523-567)뿐; session 필드는 쓰이지 않음. get_state나 extension ctx가 경로를 노출하는지는 이 repo만으로 알 수 없음(미확인; omp 명령은 --help만 실행).
- `omp --help`: `-c/--continue`, `-r/--resume=<ID prefix | path | picker if omitted>`, `--session-dir`, `--no-session`; 환경변수 `PI_CODING_AGENT_DIR` "Session storage directory (default: ~/.omp/agent)"; export 예시 경로 `~/.omp/agent/sessions/--path--/session.jsonl` (help 80행). 기본 저장 위치는 이 help 문구로부터의 추정이며 실제 파일은 확인하지 않음 (~/.omp 접근 안 함).
- 따라서 재시작에 쓸 수 있는 값은 bridge가 준 session id. peer는 종료 시 제거되므로 살아 있는 동안 (`bridge_state()`) 캐시해야 함.

### 4. ui_v1 contract
- `ClientType` (src/workbench/contracts/ui_v1.py:67-81): hello, attach, detach, snapshot, input, paste, resize, focus, takeover_request, takeover_confirm, handoff, shutdown_request, shutdown_confirm, confirm_boot. `Reason` :93-116 (이미 `PANE_UNAVAILABLE`, `PANE...`). `_REQUEST_TYPES = ClientType - {HELLO}` (:119, enum에서 자동).
- 추가 지점:
  - contracts: `ClientType.RESTART_PANE` + `parse_client_frame` 필드 분기 (ui_v1.py:269-317; FOCUS 분기 :296 옆에 `fields["pane"]=_pane(...)`).
  - handler: `UiServer._dispatch` (src/workbench/backend/ui_server.py:309-355, FOCUS :346 다음), `Controller` Protocol (:38-50), 구현은 `Backend` (service.py, `set_focus` :371 근처).
  - tests: tests/contracts/test_ui_v1.py:131-138 (`extras` 표), tests/contracts/test_ui_v1_independent.py:84-103 (payload 없는 타입에 pane 헤더), tests/backend/test_ui_server.py:20- FakeController.
- client 쪽: `UiClient` (backend/client.py:126-153)에 paste/input/attach helper만 있고 focus 등은 `ui_v1.request(...)` 직접 전송 (client.py:234-293); 제품 UI는 `Sender`로 전송.

## B. drag-to-copy (src/workbench/ui/product)

### 5. 마우스 처리
- 파싱: input.py:33 `_MOUSE` = SGR 리포트(`ESC[<b;x;yM|m`)만; `Mouse(button,x,y,release)` (input.py:52-57, `_mouse_event` :100-101). 일반 입력과 prefix 뒤 (:189-195) 모두에서 파싱되어 문자로 전달되지 않음. X10(`ESC[M...`) 리포트는 파싱하지 않음.
- outer 터미널 모드: `MOUSE_ON = ?1000h ?1002h ?1006h` (app.py:32-33), 시작 (app.py:139-141), 리사이즈 시 재적용 (:152), prefix r (:192) 재적용, 종료 시 OFF (:219). 모드는 실행 중 전환하지 않음 (주석 app.py:28-31). `prefix m`은 capture 토글 (model.py:761-763): 끄면 터미널 자체 선택(notice: "텍스트 선택은 Shift+드래그"; capture 꺼짐 시 터미널 선택 가능).
- dispatch: `ProductModel._mouse` (model.py:788-819):
  1. capture off면 무시 (:789).
  2. `_divider_mouse` (:1131-1152): border 위 좌클릭 press -> `_drag` 시작; motion(button&32) -> `_drag_to`(디바운스 resize); release -> `_end_drag`. 이 이벤트는 pane/focus로 전달되지 않음.
  3. `_pane_at` (:779-786): 테두리 안쪽만 (pane, col, row) 1-based pane-local; border/header/footer는 None -> 무시.
  4. wheel (button&64; 64=up, 65=down; 66/67 무시): 스크롤 중이면 `scroll_by(3줄)`, 아니면 pane이 tracking 중이면 `_forward_mouse`, alt-screen이면 방향키 `\x1b[A/B`(DECCKM이면 `\x1bOA/B`) x3, 그 외 `scroll_by`.
  5. 그 외 버튼: pane이 tracking 중이면 `_forward_mouse` (motion은 `_wants_motion` :821-823로 필터: 1003이면 항상, 1002면 버튼 눌린 동안만); tracking 아니면 좌클릭(press, non-motion)만 non-focus pane에 `set_focus` (:816-817). 좌 drag/release는 tracking 없는 pane에서 아무 동작 없음 = 선택 상태 없음.
- pane 앱의 모드 처리: `_track_bracketed` (model.py:540-555)가 수신 시점에 DECSET 1000/1002/1003(하나의 상태: set은 교체, reset은 끔), 1006, 1(DECCKM), 47/1047/1049, 2004를 `PaneView.modes`/`bracketed`에 기록 (`_TRACKED_MODES` :39; session identity 변경 시 초기화 :357-362). `_forward_mouse` (:830-840): pane이 1006이면 SGR, 아니면 X10(<=223칸)으로 pane-local 좌표 재인코딩 후 `INPUT` 전송.
- OMP 18.x의 mouse tracking: repo에 증거 없음. 실제 OMP 출력을 담은 fixture/녹화에 `?1000h/?1002h/?1006h`가 없고, mouse 관련 테스트(tests/ui/test_product_mux_independent_p27n.py 등)는 합성 입력. model.py:99 help 문구는 일반 규칙일 뿐. 확인하려면 실제 OMP pane 출력 캡처 필요(이번 조사에서는 실행하지 않음).

### 6. 렌더링과 텍스트 읽기
- `view.draw` (view.py:27-79): curses. 각 pane은 border(ACS) + 제목, 내부는 `model.pane_lines(pane, inner_rows)` (model.py:958-976) 각 행(pyte Char dict)의 `line.get(x, screen.default_char)`를 `win.addstr(top+1+y, left+1+x, cell.data, _attributes(cell, colors))` (view.py:52-64). `_attributes` (ui/terminal_g1/app.py:117-131)가 fg/bg/bold/underscore/reverse/blink/italics/strikethrough를 적용. `pane_rects`=`pane_boxes` (view.py:15-20).
- `pane_lines`: live면 `screen.buffer.get(y, {})`; 스크롤 중이면 `screen.history.top`(`_CountingDeque`, model.py:195-)을 `len(history)-offset`에서 시작해 rows개 + 이어서 live buffer 행 (:964-976). offset은 `scroll_offset` (:896-909): anchor(`pushed - offset`) 기반이라 새 출력이 와도 보던 위치 유지, 삭제된 history는 clamp. 화면(pyte) 자체는 변형하지 않음.
- 와이드 문자: pyte는 첫 셀에 문자, 이어지는 셀에 `data=""`. view.py:54-58이 빈 셀 skip, `wcswidth`로 폭 계산, 폭 초과/음수는 skip. 선택 텍스트 추출도 같은 규칙(빈 셀 skip, 없는 셀은 default_char 공백, 행 끝 공백 trim 필요)으로 `pane_lines(pane, rows)`를 읽으면 스크롤 offset이 자동 반영됨. history 행은 화면 폭 기준 dict.
- 좌표: Mouse.x/y는 터미널 1-based; `_pane_at`이 border 기준(내부 첫 칸 =1) pane-local (col,row) 반환. `pane_lines` 인덱스는 (row-1, col-1). 하이라이트는 `_attributes(...) | curses.A_REVERSE` 또는 draw 루프에서 선택 범위 셀에 속성 추가로 구현 가능. 렌더 루프는 `_put`/`addstr` 예외를 삼킴.

### 7. OSC 52 / 클립보드
- 없음. src/ 대상 grep(osc, `]52`, clipboard, xclip, wl-copy) 결과 0건. 터미널로 나가는 제어 출력은 app.py의 mouse ON/OFF와 `?2004h/l`(`_safe_write`)뿐.

## 최소 삽입 지점
### A (재시작)
1. contracts/ui_v1.py: `ClientType.RESTART_PANE`(+ pane 필드 parse 분기 :296 부근).
2. backend/ui_server.py: `_dispatch` 분기 (:346 뒤) + `Controller` Protocol (:38-50).
3. backend/service.py: `Backend.restart_pane(pane_id)` — 종료 확인된 `OmpPane`만 `close()` 후, 캐시한 role의 command(+`--resume <session_id>`), env(token 포함; `del tokens` :166 변경 필요), cwd로 새 `OmpPane` 생성 (:162와 동일), `panes[pane_id]` 교체, phase/reason 복구 로직(`_check_ready` :220-227), `_write_record`. session_id 캐시는 peer가 살아 있는 동안 `bridge_state()`(:307-320)에서.
4. 새 pane의 `session_id`/`generation`이 달라야 UI가 화면을 초기화 (model.py:357-372); 새 `OmpPane`은 새 uuid를 받음(panes.py:101).
5. UI 트리거: model.py `MENU_ITEMS` (:56)와 `_command` (:725)에 항목, `_send(ClientType.RESTART_PANE, pane=...)`.
6. tests: tests/contracts/test_ui_v1.py:131-138, test_ui_v1_independent.py:84-103, tests/backend/test_ui_server.py FakeController, tests/backend/test_panes.py.
### B (drag-to-copy)
1. model.py `_mouse` (:788-819): non-tracking, non-divider, non-wheel 좌 press/motion/release 분기(:814-817)에 선택 상태(pane, anchor, current) 추가; tracking pane과의 충돌 정책(예: Shift 또는 tracking 없는 pane만)은 미정.
2. model.py: `pane_lines` 기반 텍스트 추출 함수(빈 셀 skip, trailing trim).
3. view.py draw 루프 (:52-64): 선택 셀에 `A_REVERSE`.
4. app.py: release 후 `_safe_write(stdout_fd, b"\x1b]52;c;<base64>\x07")`; model에 `take_mouse_reassert` (model.py:825) 같은 출력 큐 필요.
5. tests: tests/ui/test_product_model.py, tests/ui/test_product_pty.py.
