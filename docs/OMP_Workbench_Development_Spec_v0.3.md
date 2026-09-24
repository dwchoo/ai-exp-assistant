# OMP Workbench — 통합 개발 스펙 v0.3

**기준일:** 2026-09-23  
**상태:** 개발 기준선 채택 / 구현·호환성 검증 전  
**범위:** 대화 재검토 + 1차 개발 사양 + 2차 Deferred 요구  
**주 개발 언어:** CPython 3.13 / Python-first  
**제품:** Manager OMP 1개 + Worker OMP 1개 + host persistent terminal 1개를 포함하는 자체 TUI wrapper

> 익숙한 Python으로 제품을 구현하되, OMP 연결은 작은 TypeScript extension, terminal emulation은 내장 native library에 맡긴다. 사용 경험을 축소하지 않고, 자동화의 범위와 복구 약속을 제한한다.

이 문서는 기존 단일 OMP PRD에 후속 대화의 정정을 반영한 **새 통합 개발 기준 문서**다. 과거 파일을 덮어썼거나, 개발·실행 시험을 완료했다는 뜻이 아니다. 2차 요청 게시판은 요구만 남기고 1차 코드·출시 조건에 포함하지 않는다.

## 0. 근거와 결정의 수준

### 0.1 검토 자료

- **B0 — 첨부 원문:** `붙여넣은 마크다운(1).md`, 2,303줄. 독립 PTY, persistent terminal, shell lifecycle, WATCHED, 공개 Extension API의 최초 요구를 확인했다.
- **C1 — 단순화 정정:** 기존 multiplexer로 wrapper를 대체하거나 terminal을 작업 전용 runner로 축소하는 방안은 사용자가 거절했다.
- **C2 — Host 요구:** Host의 CUDA/Python/환경을 사용하고, 아래 terminal에서 설정한 shell state를 실행에 유지한다.
- **C3 — 이벤트 기반 감시:** 명령 시작·종료는 코드가 통지하고, 필요할 때 OMP가 판단한다.
- **C4 — 2-agent 요구:** Manager는 토의·설계·개발·명세, worker는 실험 운영·정기 로그 분석·보고를 담당하며 각각 사용자와 대화한다.
- **C5 — Mailbox와 2차:** 내부 메시징을 재사용하고, 외부 coding agent 요청 수신·웹 게시판은 1차 완료 후 별도 개발한다.
- **C6 — 언어:** 사용자는 Python에 익숙하고 TypeScript에는 익숙하지 않으며 Python-first 방향에 동의했다.

B0의 원문은 현재 첨부 파일로 확인했다. 이전 응답에서 작성된 v0.2·2차 PRD와 언어 제안은 **대화에 표시된 내용**을 검토했다. 이전 파일을 별도 원문으로 다시 확보했다고 주장하지 않는다. 기술 의존성은 §20의 공식 문서·소스로 추가 확인했으며, 이를 사용자 확정 요구와 구분한다.

### 0.2 표기

| 표기 | 의미 |
|---|---|
| 사용자 확정 | 대화에서 직접 확인된 요구. 편의상 제거하지 않는다. |
| 이번 채택 | 이 개발 스펙에서 선택한 구현·운영 정책. 변경은 ADR로 기록한다. |
| 초기값 | 측정·사용 시험 전의 기본값. 제품 성능이나 비용의 실증치가 아니다. |
| 검증 게이트 | 목표 라이브러리·환경에서 실제 시험을 통과해야 하는 조건. |
| Deferred | 1차에서 구현하지 않는 후속 기능. |

## 1. 대화 재검토 결과

### 1.1 유지해야 할 요구

| ID | 최종 요구 | 근거 | 개발 반영 |
|---|---|---|---|
| U-01 | 제품명은 OMP Workbench | C4 | 문서·패키지·protocol namespace에서 이전 이름을 사용하지 않음 |
| U-02 | 자체 wrapper가 필수 | C1 | 우리가 소유하는 TUI와 terminal emulator state |
| U-03 | 외부 terminal/multiplexer의 한 pane 또는 window에서 실행 | C1 | 내부 multiplexer spawn 및 외부 pane 제어 API 금지 |
| U-04 | Terminal은 실제 host의 persistent bash/zsh | B0, C2 | 로그창·새 작업용 shell·container로 대체하지 않음 |
| U-05 | 같은 shell의 cwd·conda/venv·export를 사용 | C2 | Worker 실행도 해당 shell로 dispatch |
| U-06 | 명령 시작·종료는 명시적 lifecycle | B0, C3 | 출력 정지·prompt regex로 완료 판정 금지 |
| U-07 | Manager·worker OMP를 독립적으로 실행 | C4 | PTY 세 개, 두 OMP 세션, 역할별 모델 |
| U-08 | Manager도 실제 개발 수행 | C4 | 단순 dispatcher 전용 agent로 만들지 않음 |
| U-09 | Worker는 주기적으로 로그를 해석 | C4 | 유료 semantic review와 무료 runtime 관측 구분 |
| U-10 | Manager↔worker 명세·질문·답변·보고 | C4, C5 | 비동기 mailbox, 기존 공개 기능 우선 검토 |
| U-11 | 외부 요청 접수·게시판은 2차 | C5 | 별도 Deferred 절과 접점만 유지 |
| U-12 | Python 중심, 단순한 정책 | C1, C6 | Python runtime + 최소 TS adapter + shell scripts |

### 1.2 이전 설명에서 수정하는 내용

| 이전 제안·표현 | 평가 | 이번 정정 |
|---|---|---|
| Wrapper 대신 tmux를 사용 | 사용자 의도에서 벗어남 | 폐기. 라이브러리 재사용만 허용 |
| 작업마다 별도 runner/shell 실행 | Persistent 환경 요구와 충돌 | 실험은 기존 host shell에서 실행 |
| 핵심 목표가 단일 OMP | 이후 요구를 반영하지 못함 | 고정 manager 1 + worker 1 |
| 모니터링 전체가 토큰 0 | Worker 로그 해석에는 해당하지 않음 | 관측 0회, 해석은 유료·예산 관리 |
| OMP mailbox로 두 CLI가 즉시 연결 | 문서로 확인되지 않음 | 기본 cross-process transport는 wrapper UDS |
| Textual + libvterm이면 곧바로 완성 | Binding·키 입력·query·Unicode·성능 검증 누락 | 기술 후보 채택과 호환성 통과를 구분 |
| Python이 TS보다 terminal 성능에서 불리/우세 | 이 제품의 비교 측정 없음 | 언어 우열 대신 유지보수성과 실제 경계 비용으로 선택 |
| TS는 수백 줄/전체의 10% 정도 | 근거 없는 공수·비율 추정 | 코드량을 약속하지 않음. 경계만 작게 유지 |
| TS에는 판단을 전혀 넣지 않음 | 주입 직전 세션·상태 검증이 필요 | 제품 정책은 Python, OMP별 마지막 안전 확인은 TS |
| 자동 주입 총 3회 제한 | 주기 점검·질문답변까지 끊게 됨 | 정기 분석·peer wake·재실행 제한을 분리 |
| Bridge 장애는 OMP에 절대 영향 없음 | Extension은 OMP와 같은 프로세스 | 예외 containment 시험. 절대 격리 보장 금지 |
| 최신 API이므로 호환성 확인 완료 | 문서 읽기와 실증은 다름 | 고정 버전에서 실제 두 OMP integration test |

**평가:** 제품 방향은 일관적이며 Python-first 개발 착수가 적절하다. 가장 큰 구현 위험은 언어 선택이 아니라 **embedded terminal 호환성**과 **같은 interactive shell에 대한 안전한 자동 실행**이다. 이 둘을 첫 milestone의 통과 조건으로 둔다. 단순화는 기능 삭제가 아니라 고정된 역할·한 개의 실험 terminal·명확한 자동화 중단 규칙으로 달성한다.

## 2. 기술 스택 결정

| 영역 | 이번 채택 | 상태와 제한 |
|---|---|---|
| 본체 | CPython 3.13.x | 기준 minor. 패치 버전은 호환성 lock에 기록; 최신이라는 주장 아님 |
| 비동기 runtime | 표준 `asyncio` | PTY FD·UDS·timer. Textual loop와 하나로 운용 |
| TUI | Textual custom widgets | 채택 후보. M0 입력·화면 게이트 통과 후 production lock |
| Terminal emulator | libvterm C99 + 자체 얇은 CFFI API-mode binding | 채택 후보. 일반 Python wrapper가 검증됐다고 가정하지 않음 |
| PTY/process | `os`, `pty`, `termios`, `fcntl`, `subprocess` | POSIX backend. Controlling TTY/job control 시험 필수 |
| OMP bridge | TypeScript, 공개 Extension API | 같은 extension을 manager/worker에 role별 로드 |
| Shell adapter | zsh / Bash scripts + 필요 시 고정 Python IPC helper | 순수 shell만으로 모든 IPC를 해결한다고 가정하지 않음 |
| IPC | Unix Domain Socket + UTF-8 JSON Lines | 로컬 versioned protocol, payload 상한 |
| 데이터 모델 | Pydantic 2 | IPC·TaskSpec·설정 경계 검증, 공개 JSON Schema 산출 |
| 영속 메타데이터 | Python `sqlite3` | 명세·mailbox·command·review 결과. Durable scheduler 아님 |
| 원문 로그 | 제한된 파일 + 메모리 scrollback | DB에 매 PTY byte를 넣지 않음 |
| 설정 | TOML + `tomllib` | YAML parser 의존성 없이 시작 |
| 개발 도구 | uv, pytest, pytest-asyncio, Ruff, mypy | 버전 lock. OMP 쪽 별도 TS typecheck·계약 시험 |
| 2차 서버 | FastAPI/ASGI + Jinja2 + 공식 MCP Python SDK | Deferred. SPA/React는 최소 범위에서 제외 |

Python은 Unix PTY와 FD 기반 asyncio 처리를 제공한다.[R1][R2] Textual은 custom widget과 입력 처리를 제공하지만 VT terminal을 자동 완성해 주는 선택은 아니다.[R3][R4] libvterm은 렌더링 시스템에 독립적인 terminal emulation 부품이며 CFFI로 C 호출 경계를 만들 수 있다.[R5][R6]

**금지:** 순수 Python으로 VT parser를 새로 작성하기, 제품 전체를 TS로 통일하기 위해 언어를 변경하기, M0 실패를 내부 tmux/웹 terminal/headless OMP로 숨기기. Native parser를 쓴다고 Python rendering·문자 폭·FFI 비용 문제가 사라지는 것은 아니다.

### 2.1 버전·배포 정책

- Python minor, Textual, CFFI, Pydantic, libvterm source hash, OMP release/commit, OMP 실행 runtime, shell, OS를 `compatibility-lock.toml`에 기록한다. Python 지원 상태는 공식 표를 참고하되, 이 문서는 무조건 최신 minor를 선택하지 않는다.[R15]
- 실제 실행 전에는 임의의 최신 버전 번호나 통과한 조합을 만들어 넣지 않는다. M0 성공 artifact와 lock을 함께 보관한다.
- Linux x86_64와 macOS arm64를 초기 시험 대상으로 채택한다. Bash 5.x와 zsh 5.9 계열을 우선 검증한다. 목표 플랫폼·버전 범위이며 통과 선언은 아니다. 지원하지 않는 host shell은 자동 교체하지 않고 기능 제한을 알린다.
- 개발 의존성은 uv로 고정한다. 배포는 독립 app environment의 console entry point를 기본으로 하되 실험 shell에는 그 환경을 활성화하지 않는다.[R16]
- Native binding은 개발기에는 명시적으로 설치한 libvterm로 시험하고, 배포 때 wheel/동적 library 경로·라이선스 notice·소스 hash를 고정한다. 사용자 동의 없이 시스템 package를 설치하지 않는다.
- TypeScript는 host의 검증된 OMP runtime에서 실행한다. 본체용 Node 웹 서버나 별도 TS orchestrator는 추가하지 않는다.

## 3. 구조와 ownership

```text
사용자가 선택한 terminal / 외부 multiplexer의 한 pane
└─ workbench — Python TUI/runtime 한 프로세스
   ├─ Manager TerminalPane + PTY A → 원본 OMP + role=manager TS bridge
   ├─ Worker TerminalPane  + PTY B → 원본 OMP + role=worker TS bridge
   ├─ Host TerminalPane    + PTY C → host persistent bash/zsh + shell adapter
   ├─ local UDS endpoints
   ├─ command/task 상태 + role별 mailbox + review timer
   └─ metadata SQLite + bounded logs
```

| 자원 | owner |
|---|---|
| PTY master FD, byte routing, child PID·resize | Python Workbench |
| PTY별 VT screen/cursor/modes/scrollback | Workbench 안의 native emulator/backend |
| 실제 키 입력과 pane focus | Workbench InputRouter |
| cwd·환경·line editing·job control | Host shell |
| 명령 실행에 대한 실제 관측 | Shell adapter → Python 상태 |
| OMP session/idle/pending/editor/approval 최종 확인 | 해당 TS bridge |
| TaskSpec·review scheduling·mailbox routing | Python domain/runtime |
| 모델 실행·추론·tool 승인 | 각각의 원본 OMP |

Runtime과 TUI는 하나의 프로세스다. 별도 `workbenchd`, 재attach 서버, 네트워크 broker는 1차에 없다. PTY 초기화를 위한 짧은 child bootstrap/helper는 session daemon이 아니며, command별 실험 runner도 아니다.

OMP나 bridge 하나가 중단되면 다른 OMP와 host terminal은 Workbench가 살아 있는 한 유지하도록 설계한다. Workbench 자체가 죽은 뒤 PTY·작업·화면 상태를 살려 재attach하는 것은 1차 보장 밖이다. 정상 종료 시 실행 중 작업이 있으면 확인을 받는다. 바깥 multiplexer가 프로세스를 유지하는 동작과 Workbench 자체 crash recovery를 혼동하지 않는다.

## 4. UI와 terminal backend

### 4.1 고정 화면

넓은 화면은 상단 manager/worker 좌우 + 하단 host terminal + 상태바로 구성한다. 좁은 화면은 상단 agent 전환 + 하단 terminal로 표시한다. 숨겨진 OMP도 PTY 읽기와 VT state 갱신을 계속한다. 동적 pane tree·임의 terminal 생성은 지원하지 않는다.

공통 `TerminalPane` 하나를 세 번 사용한다. Pane 확대·복귀, focus 전환, divider 조절, scrollback, AUTO/OFF 상태와 현재 입력 owner 표시를 제공한다. 키 바인딩은 remap 가능해야 하며 shell의 Tab/Ctrl+C 및 OMP 단축키를 wrapper 동작으로 무조건 가로채지 않는다. Textual의 기본 quit/focus bindings도 명시적으로 재정의·시험한다.[R4]

### 4.2 렌더링 계약

```text
child PTY bytes → libvterm parser → damage/변경된 row → Textual custom widget
user key/paste → InputRouter → active pane 모드에 맞춘 encoding → child PTY
child terminal query → 해당 emulator의 응답 → 같은 child PTY
```

- RichLog나 ANSI 색상 문자열 출력으로 terminal을 대체하지 않는다.
- CFFI 경계는 가능한 한 바뀐 행·style run 단위로 batch 처리한다. Cell마다 Python 객체·callback을 무한 생성하는 구조를 피한다.
- 원래 PTY stream을 바깥 stdout에 직접 뿌리지 않는다. Title/clipboard/image/terminal query 등도 무조건 passthrough하지 않는다.
- `TERM`과 capability는 실제 inner emulator가 지원하는 집합을 광고한다. 바깥 terminal의 Kitty/이미지/확장 keyboard 기능을 그대로 지원한다고 주장하지 않는다.
- 한글·조합 문자·wide character, modifier keys, bracketed paste, application cursor mode, alternate screen, resize, OMP composer/승인 UI를 시험한다. 외부 환경이 전달하지 못하는 키는 복원한다고 약속하지 않는다.
- Parser는 stream 연속성을 유지한다. 중간 ANSI bytes를 버린 뒤 화면이 정상이라고 표시하지 않는다.

### 4.3 성능·backpressure

Frame refresh는 초기 최대 30 Hz로 합친다. Lifecycle/control 처리는 redraw 주기를 기다리지 않는다. PTY별 fair read와 bounded queue를 사용하고 로그 쓰기는 event loop를 막지 않는다.

출력 폭주는 무한 메모리 대신 bounded 처리를 택한다. 로그 저장 중단·화면 redraw 합치기는 허용하지만, 처리량을 초과한 지속 출력에는 OS backpressure가 발생할 수 있다. 어떤 출력량에서도 프로세스가 절대 block하지 않는다는 보장은 하지 않는다. 부하 시 지연·로그 잘림·관측 불가를 표시한다.

Backend 문제는 `terminal/` adapter에서 먼저 해결한다. Python 제품 로직 전체를 재작성하는 것으로 바로 확대하지 않는다. Textual/libvterm 교체가 필요하면 M0 ADR로 근거와 비용을 기록한다.

## 5. Host 환경과 PTY 생성

### 5.1 실험 실행 context

Workbench가 받은 host의 사용자 권한·파일시스템·작업 환경을 기본으로 유지한다. Shell은 persistent interactive 프로세스 하나다. 사용자가 `conda activate`, `source`, `export`, `cd`를 수행하면 이후 worker 실행도 그 context를 사용한다.

실험 명령을 별도 `bash -lc`, `subprocess.run(command)`, OMP built-in bash, container, 숨겨진 복제 workspace로 보내 같은 환경이라고 표현하지 않는다. 외부 agent나 worker의 요구만으로 PATH·CUDA_VISIBLE_DEVICES·패키지를 바꾸지 않는다.

**Workbench 실행용 Python environment와 실험 환경은 다르다.** App의 venv·site-packages를 host shell에 활성화하지 않는다. Shell에 전달하는 환경은 app이 수정하기 전 snapshot을 기준으로 하며, app launcher가 PATH/VIRTUAL_ENV를 변경했다면 이를 명시적으로 시험한다. `python`을 Workbench의 `sys.executable`로 치환하지 않는다. 라이브러리 설치를 위해 실험 conda 환경을 변경하지 않는다.

OMP bridge/helper 실행 파일은 시작 시 absolute path로 해석해 둔다. Terminal에서 Python 환경을 바꿔도 IPC helper가 사라지거나 다른 Python으로 바뀌지 않도록 한다. 이 helper는 환경 조회·IPC용이며 실제 실험 command를 별도 shell로 실행하는 주체가 아니다.

### 5.2 Shell 시작과 보존

기본은 사용자 `$SHELL`의 지원되는 interactive non-login shell이며 login은 명시적 옵션이다. 사용자 dotfile은 영구 덮어쓰지 않는다. Hook 설치 순서가 기존 prompt/conda 초기화와 충돌하지 않는지 시험한다. 현재 shell에서 이미 정의한 부모의 non-export 변수·alias·function까지 새 child가 자동 복제한다고 보장하지 않는다. 사용자의 초기화 파일에서 로드하거나 integrated shell에서 직접 설정한 상태는 유지한다.

OMP 두 프로세스의 environment를 host shell의 이후 변경에 역동기화하지 않는다. 환경 의존 실행은 worker terminal tool로 보낸다. Manager의 코드 개발 도구 실행 context는 별도로 표시한다.

### 5.3 PTY backend 결정

세 PTY 모두 실제 controlling TTY, foreground process group, resize와 job control을 갖도록 생성한다. `asyncio.create_subprocess_exec`의 stdout PIPE는 PTY가 아니다.

직접 UI 프로세스에서 반복 `pty.fork()` + 복잡한 child 초기화를 사용하는 방식을 배포 기본으로 삼지 않는다. Python 공식 문서는 macOS의 `pty.fork()` 혼용 위험과 플랫폼 의존성을 명시하고, `subprocess` 문서는 threads와 `preexec_fn`의 위험을 설명한다.[R1][R7]

이번 구현 경로는 **openpty + 안전한 child bootstrap**이다. `start_new_session`으로 시작한 최소 bootstrap이 slave의 controlling-TTY 설정·stdio·cwd·env를 마친 뒤 shell 또는 OMP를 `exec`한다. UI 프로세스의 `preexec_fn`에서 Python 코드를 실행하지 않는다. 구체적인 OS별 ioctl과 FD close 규칙은 M0 검증 대상으로 둔다. Python bootstrap으로 안전성이 확보되지 않으면 이 경계에만 native helper를 적용하고 사유를 기록한다.

Ctrl+C는 active child terminal의 정상 동작을 우선한다. 자동 interrupt는 command ID·shell generation·foreground job 확인 후 대상에만 적용한다. 무조건 shell PID를 kill하지 않는다. Ctrl+Z 후 prompt가 돌아와도 작업 완료로 단정하지 않는다.

## 6. 같은 shell 자동 실행과 lifecycle

### 6.1 자동 실행 계약

Worker만 `workbench_terminal_run`을 호출해 실험 terminal에 자동 명령을 실행할 수 있다. Manager는 명세와 읽기 도구를 사용한다. 사용자는 terminal에 직접 입력할 수 있다.

한 번에 활성 foreground command 하나, 수락 중 요청 하나만 허용한다. Terminal command queue는 없다. 아래 조건 중 하나면 즉시 구체적인 오류를 반환한다.

| 상황 | 응답 |
|---|---|
| 다른 command/REPL/편집기/suspended job 활성 | `BUSY` |
| 사람이 입력을 소유하거나 미제출 buffer 존재 | `INPUT_PENDING` |
| 요청의 expected_cwd와 현재 cwd 불일치 | `CWD_MISMATCH` |
| Hook·shell identity·입력 경계 불명확 | `UNAVAILABLE` |
| 전송 이후 시작 여부 불명확 | `NEEDS_REVIEW` — 자동 재실행 금지 |

실행 요청은 `task_id`, `spec_revision`, `command`, `expected_cwd`, `watch`를 갖는다. Tool이 빨리 반환해도 command를 `&`로 실행하는 것은 아니다. 명시적인 `command.started` 또는 불명확 결과를 반환하며, OMP가 완료까지 polling할 필요가 없음을 tool 설명에 넣는다.

### 6.2 입력 ownership — 이번 단순화

사람이 host terminal에 입력하면 `input_owner=human`을 유지한다. Focus를 다른 pane으로 옮겼다는 이유만으로 자동 실행에 넘기지 않는다. 사용자가 명시적으로 제어를 반환하고 shell adapter가 입력 buffer가 비었음을 확인해야 `automation`이 된다.

자동 run 수락은 다음 짧은 exclusive 구간으로 직렬화한다.

```text
Python에서 run 후보 예약
 → shell line editor에서 현재 입력 경계·빈 buffer 재확인
 → 사람의 입력과 섞이지 않도록 수락/거절 확정
 → 같은 shell의 command line으로 실행
 → command.started에 요청 ID 연결
```

이 구간 중 새 사람 입력은 요청 취소/거절을 우선하고 기존 입력을 삭제하지 않는다. 수락이 이미 끝났다면 실행 사실을 표시하고 사용자 interrupt로 처리한다. Wrapper와 adapter의 확인 사이 race를 fixed sleep으로 해결하지 않는다.

zsh는 preexec/precmd 및 ZLE widget 경로를, Bash는 DEBUG/PROMPT_COMMAND와 Readline 연동을 시험한다. zsh hook·ZLE 기능의 존재는 공식 문서에 근거하지만, 그것만으로 Workbench의 수락 경쟁 문제가 해결됐다는 뜻은 아니다.[R10][R11] Bash의 단순 DEBUG trap 하나를 top-level 실행과 일대일이라고 가정하지 않는다. **두 shell의 safe dispatch는 별도 필수 게이트**다. 실패하면 새 shell fallback을 쓰지 않고 해당 자동화를 미지원으로 표시한다.

### 6.3 관측 의미

- 지원 단위는 top-level foreground command/list 하나다. 내부 자식 프로세스 전체 그래프를 추적하지 않는다.
- 같은 실행 단위에는 command ID 하나와 확정된 terminal outcome 하나를 기록한다. 중복 이벤트는 같은 ID로 억제한다.
- Pipeline/AND/OR/list는 사용자 shell의 aggregate exit status를 따른다. `pipefail` 등을 임의 변경하지 않는다.
- REPL/편집기는 진입부터 종료까지 하나의 실행 단위다. 내부 입력은 별도 command lifecycle이 아니다.
- Nested shell, background/daemonized job, shell `exec`, hook 유실·job suspend는 사용을 허용하되 자동 완료 보장 범위 밖을 명시한다. Prompt 복귀로 background 전체 완료를 주장하지 않는다.
- `exit_code`와 `signal`은 관측한 값만 기록한다. 128+숫자라는 이유만으로 특정 signal을 확정하지 않는다.
- Shell 프로세스 종료는 실행 중 command의 정상 종료와 다르다. 알 수 없으면 `UNKNOWN`으로 남긴다.

### 6.4 최소 lifecycle 이벤트

`shell.ready`, `command.started`, `command.finished`, `shell.exited`, `cwd.changed`를 사용한다. 실패는 `command.finished.exit_code`로 표현한다. `command.alert`는 협조하는 프로그램/adapter의 명시적 관측을 위한 확장점이며, 외부 임의 prompt API가 아니다.

일반 stdout의 marker나 ERROR 문자열로 lifecycle 상태를 변경하지 않는다. 같은 UID의 악성 process까지 차단하는 sandbox라고 표현하지 않는다.

## 7. Manager·worker 책임과 개발 동시성

| 항목 | Manager | Worker |
|---|---|---|
| 모델 | 사용자가 지정한 고성능 profile | 사용자가 지정한 저비용 profile |
| 사용자 대화 | 요청 명확화, 설계, 개발, 결과 해석 | 실험 상태, 실행 조건 확인, 운영 |
| 코드 | 조사·수정·필요한 추가 개발 | 기본 소스 수정 금지 |
| 명세 | 작성·revision 확정·수정 | 검증 후 실행, 불명확하면 질문 |
| 실험 terminal | 상태·로그 읽기 | 자동 run/interrupt의 담당자 |
| 정기 점검 | 정상 로그를 반복 분석하지 않음 | 명세에 따라 유료 로그 review |
| 예상 밖 문제 | 깊은 원인 분석·수정·사용자 결정 요청 | 근거·의심·요청할 조치를 구분해 보고 |

두 agent는 각각 원본 OMP TUI로 사용자와 직접 대화한다. Worker는 일회성 subagent가 아니고 manager는 코드를 수정하지 못하는 director로 제한하지 않는다. 모델 이름·가격·최신 성능 순위는 이 스펙에서 지정하지 않는다. Role별 실제 model ID를 설정에서 선택하고 세션 시작 시 표시한다. 다른 세션의 모델이나 사용자 전역 OMP 설정을 조용히 바꾸지 않는다.

### 7.1 실행 명세

명세는 다음을 가진 `TaskSpec`이다. JSON을 기준으로 저장하고 사람이 읽는 Markdown view를 함께 제공한다.

- Task ID, revision, 목표, 완료 기준, 작성자·승인 시각.
- Command와 expected_cwd, 필요한 Python/환경/GPU 조건. 전체 secret 환경 dump는 넣지 않는다.
- 실행 코드의 commit·dirty 여부·선택한 변경 기록 참조. Commit만으로 dirty/untracked 내용까지 고정했다고 주장하지 않는다.
- 기대 진행 신호, review interval, semantic review 예산, no-progress 조건.
- 허용된 interrupt·retry와 최대 attempts, 사용자 확인이 필요한 상황.
- 결과/artifact 경로와 보고 대상.

승인 명세는 immutable revision으로 연결한다. Worker가 실행 중인 명세를 새 초안으로 조용히 바꾸지 않는다. 한 번에 active task 하나다. 다음 명세를 작성·보관할 수 있지만 시작 순서를 자동 결정하는 scheduler는 없다.

사용자가 worker에게 직접 간단한 실행을 요청하면 위임된 범위 안에서 작은 TaskSpec으로 기록하거나 기존 명세에 연결한다. 중요한 목표·코드·중단 정책 변경은 manager와 공유하고 새 revision으로 결정한다. 취소를 manager가 이전 명세로 되돌리지 못하도록 runtime에 취소 사실을 남긴다.

### 7.2 실행 중 코드와 환경 보호

동시 개발에는 **명시적인 host 개발 worktree**를 기본 선택으로 채택한다. `experiment_root`와 `development_root`를 화면·명세에 표시하며 사용자가 확인한 뒤 생성·연결한다. 숨겨진 workspace 복제나 container 환경이 아니다. Git worktree는 별도 checkout을 제공하지만 공유 패키지·데이터·권한까지 격리하지 않는다.[R12]

- Worker는 사용자가 설정한 experiment_root의 현재 shell에서 실행한다.
- Manager는 development_root에서 다음 변경을 개발한다.
- 실행 중인 experiment_root의 코드·설정 및 실험 conda 환경의 package install/remove를 자동 변경하지 않는다.
- 다음 실험 전에 manager/사용자가 변경을 검토하고 명시적으로 적용한다. 자동 merge·환경 동기화 엔진은 없다.
- Worktree를 쓰지 않는 `shared_root` 모드에서는 실험 실행 중 manager의 쓰기·범용 실행 도구를 제한하고, 토의·읽기·설계만 허용한다. 다음 개발은 실험 종료 후 수행한다.

이는 역할·운영 정책이다. 같은 UID와 host shell의 임의 코드를 악성 행위로부터 완전히 격리하는 보안 경계는 아니다. 단순 경로 검사만으로 범용 shell 도구의 모든 부작용을 막았다고 주장하지 않는다.

## 8. Mailbox와 공개 OMP bridge

### 8.1 기존 기능 확인과 선택

확인한 OMP `hub` 문서는 peer messaging을 process-global mailbox bus로 설명하고, 별도로 process supervision의 cross-instance broker를 설명한다. 이 두 기능은 다르다.[R9] 따라서 **독립 OMP 두 프로세스 연결은 Python wrapper-local UDS mailbox를 기본으로 채택**한다.

향후 고정 OMP 버전의 공개 cross-process 메시징이 실제 요건을 통과하면 transport adapter만 교체할 수 있다. 이를 위해 OMP private registry/bus를 import하거나 범용 peer broker를 별도로 만들지 않는다. OMP의 공개 message injection·tool surface는 그대로 재사용한다.[R8]

### 8.2 작은 메시지 모델

`task`, `question`, `answer`, `report` 네 가지로 시작한다. 공통 필드는 다음과 같다.

```json
{
  "protocol": "omp-workbench/v1",
  "message_id": "msg-example-001",
  "workbench_run_id": "run-example-001",
  "from_role": "manager",
  "to_role": "worker",
  "target_session_id": "session-worker-001",
  "type": "task",
  "task_id": "task-001",
  "spec_revision": 1,
  "reply_to": null,
  "body": "승인된 명세 task-001/revision-1을 확인하고 실행 조건을 점검하세요."
}
```

위 ID와 tool 이름은 설계 예시다. OMP에 이미 존재하는 전역 식별자와 혼동하지 않는다. Role은 연결된 endpoint에서 확정하고 본문의 role 문자열로 권한을 올리지 못하게 한다.

Manager/worker 대화 history 전체를 복제하지 않는다. 명세와 필요한 질문·결과 참조만 전달한다. 일반 ACK는 코드로 처리하고 모델이 서로 확인 인사를 반복하게 만들지 않는다.

### 8.3 전달 정책

- Mailbox send는 로컬 접수 결과를 반환한다. 답변이 올 때까지 LLM tool call을 장시간 유지하지 않는다.
- 수신 OMP가 busy·approval·미제출 composer 상태면 대기한다. 직접 사용자 대화가 우선이다.
- 수신 bridge가 session identity, idle, pending, 가능한 공개 editor 상태를 **주입 직전 재확인**한다.
- 주입은 공개 API로 수행하고 OMP에 text/Enter를 보내지 않는다. 기본 steer에 의존하지 않고 non-interrupting delivery 옵션을 명시한다. 후보는 `sendMessage(..., {deliverAs: "followUp", triggerTurn: true})`이며 idle 시작·경합 시 queue 의미를 고정 버전에서 시험한다. 불가하면 동등한 공개 경로를 검증하고 기록한다.[R8]
- Idle 확인 후 상태 변경이 발생해도 무조건 interrupt하지 않는다. API 수락은 모델이 실제 처리했다는 보장이 아니다.
- 같은 live session에서 message ID별 중복 시도를 억제한다. 연결·세션 변경 후 미확정 메시지는 확인 필요로 보관하고 자동 replay하지 않는다.
- Peer에서 유발된 자동 wake는 task별 초기 12회 상한을 둔다. 모델 회신을 강제하지 않고, 상한 이후 사용자 개입을 요구한다. 이는 무한 대화 억제용 초기값이다.

### 8.4 TypeScript의 정확한 책임

하나의 bridge package를 두 번 로드한다. Tool 등록·OMP lifecycle 변환·session ID·최종 eligibility·native injection·JSON validation·timeout·오류 containment는 TS 책임이다. Task scheduling·monitor interval·mailbox 저장·로그 parsing은 Python 책임이다.

Extension은 OMP와 같은 프로세스에서 실행되므로 IPC callback, timer, promise의 예외를 모두 다룬다. 공식 managed timer가 제공되는 경로에서는 이를 사용하고, shutdown 시 FD·timer를 정리한다.[R8] 원시 timer나 socket 예외로 OMP를 죽이는 failure test를 포함한다. Adapter가 작다는 이유로 테스트를 줄이지 않는다.

## 9. 관측·정기 분석·자동 조치

### 9.1 세 계층

| 계층 | 내용 | 모델 사용 |
|---|---|---|
| Runtime observation | start/finish, 경과 시간, 최근 출력, lifecycle/연결 상태 | 없음 |
| Worker semantic review | 로그와 기대 진행 비교, 정체 의심, 실행 관련 대화 | 있음 |
| Manager reasoning | 불명확 요구·깊은 원인 분석·코드 수정·중요한 결정 | 있음 |

Python 본체는 provider API를 직접 호출해 세 번째 agent를 만들지 않는다. 모델 호출은 기존 manager/worker OMP를 통해 수행한다. API key는 기존 OMP 설정이 관리하고 Workbench DB에 복사하지 않는다.

### 9.2 이벤트와 timer

종료 이벤트는 60초 점검 timer와 무관하게 처리한다. OMP가 busy이면 로컬 결과를 즉시 표시하고 다음 eligible idle에 전달한다. '즉시'는 정기 점검을 기다리지 않는다는 뜻이지 모델 응답 latency 0이나 즉시 interrupt라는 뜻이 아니다.

Periodic review는 초기 **60초 간격**이며 active watched task에만 적용한다. 이는 모델 review 요청 간격이고 PTY 읽기·실제 lifecycle의 polling 주기가 아니다. 코드 health check도 모델을 호출하지 않는다.

Worker가 바쁘면 review 요청을 무한 queue로 쌓지 않고 최신 한 건으로 합친다. Completion이 오면 오래된 periodic review는 폐기하고 completion을 우선한다. 정상 review 결과는 로컬 기록과 worker 화면에 남기며 manager를 매번 깨우지 않는다.

### 9.3 정체·에러 의미

Non-zero exit는 shell 수준의 실패 결과이며 과업 전체 원인의 판단과 다르다. stderr/ERROR 문자열, 출력 없음, 낮은 GPU utilization은 단독 확정 근거가 아니다. Worker의 `healthy/suspected_stall/needs_attention` 평가는 실제 `RUNNING/FINISHED/UNKNOWN`과 분리한다.

No-progress 기준은 task별로 정한다. 예: 명세상 step 증가가 기대되는데 일정 시간 변화가 없으면 worker가 원인 점검을 요청한다. 로그 변화가 없다는 이유로 이 점검을 생략하지 않는다. 진행 지표를 얻지 못하면 '확인 불가'라고 보고한다.

작업 중 치명적 오류를 즉시 알리려면 프로그램의 명시적 alert 또는 검증된 adapter가 필요하다. 범용 로그 분류·hang 확정·자동 kill은 1차 보장에 넣지 않는다. 기본 worker는 이상을 보고하며, 명세에 승인된 stop/retry만 수행한다.

### 9.4 비용·권한의 분리

| 제어 | 초기값 | 의미 |
|---|---|---|
| Periodic review interval | 60초 | 활성 작업에서 의미 분석 요청 간격 |
| Periodic review activations | command당 120회 | 상한 후 periodic 분석만 중지·표시 |
| Periodic review pending | worker당 최신 1건 | backlog 방지 |
| Peer automatic wakes | task당 12회 | manager↔worker 무한 회신 억제 |
| Autonomous retries | 0회 | 명세에 명시 허용될 때만 증가 |
| Inbox capacity | role당 64건 | 초과 시 명시적 backpressure, 조용한 drop 금지 |

120회는 60초 간격에서 약 2시간 분량의 초기 budget이므로 예상 실행이 더 길면 **시작 전에 표시하고 명세에서 늘리거나 간격을 조정**한다. 사용자의 작업 길이에 적합한 최적값으로 검증된 숫자가 아니다.

Review activation 한 번과 provider model request 한 번은 같지 않을 수 있다. 실제 OMP turn 안의 tool·재시도·추가 호출과 대화 누적 token을 계측한다. 공개 usage가 있으면 token을 기록하고, 없으면 unknown으로 표시한다. Dollar hard cap이나 전체 token의 엄격한 상한을 호출 횟수로 대신 보증하지 않는다.

Periodic budget 소진은 lifecycle 관측·원문 결과 기록을 멈추지 않는다. Completion은 periodic budget과 별도 단발 알림으로 보관·전달한다. 전체 사용자 AUTO OFF, 세션 mismatch, approval, 안전상 PAUSED는 항상 우선한다. Manager의 일반 사용자 대화 비용은 Workbench 자동 wake budget과 별도로 표시한다.

## 10. Tool surface와 승인

| Tool — 제안 이름 | Manager | Worker | 기능 |
|---|---|---|---|
| `workbench_task_get` | 읽기 | 읽기 | 승인 명세·revision 조회 |
| `workbench_task_publish` | 허용 | 금지 | 명세 검증·revision 기록·전달 후보 생성 |
| `workbench_mail_send` | 허용 | 허용 | 상대 role로 task/question/answer/report 접수 |
| `workbench_mail_read` | 자기 inbox | 자기 inbox | backlog·전달 상태 조회 |
| `workbench_terminal_status` | 읽기 | 읽기 | 실제 terminal 상태 snapshot |
| `workbench_terminal_read` | 읽기 | 읽기 | 제한된 로그 조회 |
| `workbench_terminal_run` | 금지 | 허용 | 같은 persistent shell에 run 수락 요청 |
| `workbench_terminal_interrupt` | worker에 요청 | 명세·사용자 승인 범위 | 특정 command 중단 요청 |
| `workbench_review_report` | 읽기 | 작성 | 구조화된 점검 결과 기록 |

이는 Workbench 도구의 권한이며 OMP의 모든 built-in 권한과 동일하지 않다. Worker의 source write와 우회용 범용 bash/exec는 role profile에서 비활성화하거나 명시적 승인 경로로 제한한다. Manager가 공유 root에서 작업할 때의 쓰기·실행 제한도 공개 tool interception을 통해 적용한다. 다른 extension이 이를 우회하는 조합을 호환성 시험에 포함한다.

Tool 등록만 했다고 기존 승인 체계가 자동으로 보존된다고 가정하지 않는다. 공개 API의 tool approval과 cancellation을 integration test한다. 명세 승인과 command 실행 승인·위험 행동 승인은 위임 범위에 따라 구분한다. User 직접 terminal 사용은 가능하되 관측·안전 경고는 계속 표시한다.

## 11. IPC·저장·데이터 구조

### 11.1 Protocol

개인 run directory의 UDS를 사용하며 manager/worker/shell 연결은 별도 endpoint 또는 동일 서버의 서로 다른 고정 인증 경로로 구분한다. 최초 구현은 **세 socket: manager.sock, worker.sock, shell.sock**로 한다. 단일 Workbench 프로세스가 모두 소유한다.

UTF-8 JSON Lines, protocol=`omp-workbench/v1`, 최대 frame 256 KiB를 초기값으로 둔다. LF가 포함된 command는 JSON escaping으로 처리한다. 원문 log stream은 protocol frame에 실어 나르지 않는다. 상대 role·session·connection generation·request ID를 검증한다.

Handshake에는 role, bridge version, OMP version, session ID, 공개 capability snapshot을 포함한다. Event/RPC timeout과 cancel은 동일한 request ID에 연결한다. 재연결은 transport 복구일 뿐 기존 자동 주입·명령의 재시도 허가가 아니다.

### 11.2 저장소

로컬 디스크의 사용자 전용 data directory를 사용한다. SQLite 파일을 공유 NFS 프로젝트 디렉터리에 기본 배치하지 않는다. 주요 테이블은 `tasks`, `messages`, `commands`, `reviews`, `meta`로 제한한다. 원문 PTY bytes는 파일로 보관한다.

DB는 진단·요청·결과 보관의 근거이며 UI/PTY 복원이나 실행 scheduler가 아니다. 한 storage worker가 DB 쓰기를 직렬화한다. DB 작업을 event loop에서 장시간 수행하거나 전부 비동기라고 이름 붙인 동기 쓰기를 섞지 않는다. Schema version과 소규모 SQL migration을 유지하고, 1차에는 ORM·Celery·Redis를 추가하지 않는다.

```text
<private-data>/projects/<project-id>/
  metadata.sqlite3
  specs/<task-id>/<revision>.json
  runs/<run-id>/terminal.log
  runs/<run-id>/diagnostics.jsonl
```

SQLite와 spec 파일 간 일치 상태가 불분명하면 자동 dispatch를 막는다. Approved task는 JSON content hash와 revision으로 확인한다. 파일을 사용자 편집 편의로 제공하더라도 기존 승인 revision을 in-place 변경해서 실행하지 않는다.

### 11.3 출력과 비밀 정보

- PTY는 stdout/stderr가 합쳐질 수 있고 background 출력이 섞일 수 있다. Command별 log view는 시간·offset 연결이며 모든 bytes의 정확한 process 귀속을 보증하지 않는다.
- Lifecycle IPC와 PTY 출력의 도착 순서는 다를 수 있다. Completion 수신 순간 모든 마지막 출력이 저장됐다고 선언하지 않는다. 이후 drain을 계속하고 late output을 조회할 수 있게 한다.
- Run 원문 log 상한 64 MiB, 프로젝트 전체 원문 log 상한 512 MiB를 초기값으로 한다. 상한은 저장만 제한하며 화면·lifecycle 중단 신호가 아니다. 잘림을 명시한다.
- Log OFF·delete·storage usage 조회를 제공한다. Log OFF에서도 제한된 화면 buffer는 남을 수 있음을 알린다.
- Keyboard keylog, 전체 환경변수 dump, OMP 두 화면의 전체 transcript 복제는 기본 기능이 아니다. 관측 log에는 secret이 포함될 수 있으므로 완전 redaction을 약속하지 않는다.

## 12. 상태·고정 정책·장애

### 12.1 최소 상태

| 대상 | 상태 |
|---|---|
| Role별 OMP | OFFLINE, STARTING, IDLE, WORKING, WAITING_APPROVAL, UNKNOWN |
| Host terminal | OFFLINE, STARTING, READY, RUNNING, SUSPENDED, UNKNOWN |
| Task | DRAFT, READY, RUNNING, NEEDS_REVIEW, CLOSED |
| Automation | OFF, ON, PAUSED + reason |
| Message delivery | PENDING, ATTEMPTED, ACCEPTED, NEEDS_REVIEW, CANCELLED |

Command exit result, worker health assessment, request acceptance, model delivery는 하나의 status로 합치지 않는다. Message ACCEPTED도 실제 처리·과업 성공은 아니다.

### 12.2 고정 규칙

1. `command.started`와 정상 heartbeat는 상태만 갱신한다.
2. 사람의 NORMAL command는 기본 새 모델 turn을 만들지 않는다. 현재 실행을 WATCH하면 worker session에 연결한다.
3. WATCHED completion과 선택적 review는 worker에게 전달한다. Manager에게 정상 점검을 모두 broadcast하지 않는다.
4. OMP busy/approval/draft/기존 pending에서는 대기한다. Bridge에서 마지막으로 상태를 확인한다.
5. 사용자 AUTO OFF는 두 role의 새 자동 주입과 새 Workbench 자동 run 수락을 막는다. 기존 작업·이미 시작된 turn은 취소하지 않는다.
6. 이미 돌아가는 OMP의 built-in 행동까지 AUTO OFF가 모두 정지시키는 것은 아니다. 필요하면 별도 OMP abort·tool approval로 제어한다.
7. 사용자 취소는 해당 실행의 자동 재개·재실행을 억제한다. 새 작업은 명시적 결정으로 만든다.
8. 상태·전달·시작 여부 불명확 시 PAUSED/NEEDS_REVIEW로 전환하고 자동 replay하지 않는다.

### 12.3 장애 처리

| 장애 | 처리 |
|---|---|
| Manager crash | Worker·terminal은 유지. manager행 보고 보관. 새 manager 세션에 자동 replay 금지 |
| Worker crash | 실험은 Workbench가 살아 있는 동안 유지. periodic 분석 미가동 표시. 명시적 재연결 필요 |
| Bridge socket disconnect | 역할 자동 주입 중지. 예외를 OMP process로 전파하지 않도록 containment |
| Shell hook 유실/모순 | 일반 terminal은 가능한 한 유지. 자동 run·completion 판단은 중지 |
| Shell exit | 실행 중 command 결과를 shell exit로 대체하지 않음. 확인 필요 |
| Metadata 저장 실패 | 새 자동 실행 수락을 멈춤. 사람의 기존 command는 강제 종료하지 않음 |
| Log 저장 실패/상한 | 저장 경고, 가능한 화면·PTY·lifecycle 처리는 유지 |
| Wrapper crash | 프로세스·화면 복원 비보장. 다음 시작은 AUTO OFF, 이전 RUNNING 기록은 확인 필요 |

새 role session을 기존 active task에 다시 연결하려면 사용자 확인과 현재 상태 점검을 거친다. Role 이름이 같다는 이유로 예전 메시지를 새 세션에 자동 주입하지 않는다.

## 13. 로컬 보안 경계

Run directory 0700, socket·metadata 0600을 기본으로 한다. Endpoint에 연결된 역할과 메시지 type·frame 크기를 검증한다. 프로젝트에서 writable한 임의 경로를 control socket 기본 위치로 사용하지 않는다.

로그·외부 요청·상대 agent의 말은 새로운 사용자 승인이나 system instruction이 아니다. Native message에는 출처·task/revision·observation/reference를 명시하고, 필요한 자료만 읽게 한다.

동일 host 사용자 권한의 악성 프로세스로부터의 강한 격리는 제공하지 않는다. Per-run token이나 inherited FD만으로 이를 해결했다고 표현하지 않는다. Host 환경 공유를 지키면서 container 수준의 isolation을 동시에 약속하지 않는다.

OSC clipboard·임의 링크·host file 열람·escape passthrough는 지원 기능별로 제한한다. 사용자 승인 없이 이미지·클립보드 프로토콜을 outer terminal로 그대로 전달하지 않는다.

## 14. 2차 Task Inbox & Web Board — Deferred

**1차 Workbench 완료 후 별도 착수한다. 지금은 PRD·접점만 유지한다.** 1차 package는 웹 서버·MCP SDK 없이 설치·실행 가능해야 한다.

### 14.1 흐름

```text
외부 coding agent → MCP 접수 → 영속 요청함
                               ↓ request.available
                          Manager mailbox
                               ↓ 검토·질문·개발
                           승인 TaskSpec
                               ↓
                           Worker 실행
                               ↓
                         결과·Manager 검토
                               ↓
                   웹 게시판 / MCP 요청 결과 조회
```

접수와 승인·실행·목표 달성은 별개다. 요청 원문을 worker에 그대로 실행시키지 않는다. Manager가 불명확함을 사용자/요청자와 토의한 후 명세를 만든다.

### 14.2 기술·최소 기능

FastAPI/ASGI + Jinja2 server-rendered UI와 공식 MCP Python SDK를 사용한다. `/mcp`, `/requests`, `/requests/<request_id>`는 동일 Board 서비스의 입력 경로다. MCP는 도구 호출 transport이며 게시판 UI가 자동 생성되는 것은 아니다.[R13][R14][R17]

도구는 `workbench_request_submit/list/get/comment` 네 개로 시작한다. Submit은 저장한 업무 request ID를 즉시 반환하며 실험 종료까지 대기하지 않는다. 외부 client에는 approve·dispatch·terminal run 도구를 열지 않는다.

Board는 선택적 별도 Python 프로세스다. 요청·댓글·검토 기록의 저장소는 하나이며, MCP와 Web을 위한 별도 요청 DB를 각각 만들지 않는다. 1차 metadata와는 ID로 연결하며 원문을 이중 복제하지 않는다. 요청 저장소는 SQLite로 시작하고 구체적 통합/migration은 2차 ADR로 확정한다.

Mailbox는 새 요청·답변 통지에 사용한다. 접수 사실은 영속 store가 기준이며 manager가 offline이어도 Board가 살아 있으면 접수한다. 로컬 재연결이 실험 자동 재실행으로 이어지지 않는다.

상태는 NEW, REVIEWING, NEEDS_INFO, READY, RUNNING, CLOSED로 제한하고 종료 사유를 따로 저장한다. 중복 접수는 requester identity + client_request_id로 억제한다. 같은 키의 다른 내용은 충돌이다.

기본 localhost, 인증·Origin 검증, 안전한 Markdown, 조회 권한, 크기·빈도 제한을 포함한다. 원격 공개는 HTTPS·client 호환 인증 검증 뒤 별도 활성화한다.[R14] 파일 첨부 자동 실행·외부 callback·다중 worker scheduler·웹 terminal은 제외한다.

## 15. 프로젝트 구조와 개발 규칙

다음은 구현할 저장소의 구조이며 현재 생성된 실행 코드가 아니다.

```text
omp-workbench/
  pyproject.toml
  uv.lock
  src/omp_workbench/
    cli.py
    app.py
    config.py
    domain/             # TaskSpec, Message, Command, Review, role rules
    runtime/            # 상태 전이, mailbox, review timer, coordination
    terminal/           # PTY backend, emulator interface, CFFI binding
    shell/              # shell adapter control, same-shell dispatch
    ipc/                # UDS server, framing, request correlation
    storage/            # SQLite writer, log/artifact store
    ui/                 # TerminalPane, layout, status, input ownership
  native/               # 필요한 CFFI shim; optional PTY helper
  omp-extension/
    package.json
    tsconfig.json
    src/index.ts
    src/protocol.ts
    src/lifecycle.ts
    src/tools.ts
    src/delivery.ts
  shell-integration/
    bash.sh
    zsh.zsh
    pty_child.py
    ipc_helper.py
  protocol/
    schema.json
    fixtures/
  tests/
    unit/
    contract/
    shell/
    pty/
    integration/
    performance/
  docs/
    decisions/
    compatibility-lock.toml
    acceptance-results.md
  phase2/               # 지금은 설계 문서만. 서버 코드·의존성 없음
```

Python domain은 Textual·OMP private type에 의존하지 않는다. Terminal backend는 입력·resize·screen update·query reply 경계를 제공한다. TS는 Python에 정기 timer 정책을 복제하지 않는다. 두 언어의 schema drift는 공통 JSON fixtures로 검증하며 처음부터 복잡한 multi-language codegen 체계를 추가하지 않는다.

테스트·lint·typing은 기본 CI 조건이다. Unit/contract test는 모델 호출 없이 실행한다. 실제 OMP integration test만 명시적 자격증명·예산이 있는 환경에서 실행하고 그 결과를 별도 표시한다. 유료 테스트를 일반 unit suite에 숨기지 않는다.

문서·오류·상태 명칭은 command 결과, tool 수락, message 수락, model turn 시작, task 성공을 구별한다. `FAILED` 하나로 모든 장애를 뭉개지 않는다. 실행 전·중·후의 ID와 metadata를 조사할 수 있는 `doctor`/diagnostics 명령을 제공한다.

## 16. 초기 설정과 정책값

동반 파일 `omp-workbench.example.toml`은 이 스펙의 **설계용 설정 예시**다. TOML 구문은 검증하되 실제 프로그램이 이 설정을 구현했다고 주장하지 않는다.

| 항목 | 초기값/결정 | 설명 |
|---|---|---|
| 초기 AUTO | OFF | 사용자가 활성화한 작업 범위에서만 자동 주입 |
| UI redraw 상한 | 30 Hz | Event 처리·PTY read와 별개 |
| Integrated shell | 사용자 shell, non-login interactive | 지원 adapter 없으면 명시적 제한 |
| Manual command watch | OFF | 사용자가 현재 command를 명시 watch 가능 |
| Worker command watch | ON | 정상 lifecycle로 completion 통지 |
| Semantic review | 활성 작업에서 60초 / command당 120 activations | 명세에서 변경, 비용 안내 |
| Peer wake budget | task당 12 | ACK는 모델 호출 없이 처리 |
| 자동 retry | 0 | 명세에서 허용할 때만 실행 |
| Terminal run queue | 없음 | Busy면 거절 |
| Role inbox 상한 | 64 | 초과를 명시적으로 알림 |
| IPC frame 상한 | 256 KiB | 큰 log는 artifact/read 경로 |
| 원문 log | run당 64 MiB / project당 512 MiB | 상한 후 잘림 표시 |
| 2차 Board | disabled | 1차 설치에 서버 package 없음 |

모델 ID는 설정 예시에 비워 둔다. 이는 요구를 미결로 남긴 것이 아니라 사용자가 실제 인증·사용하는 모델에 연결하기 위한 환경값이다. 모델 이름을 임의로 지어 넣지 않는다. Provider 선택·가격 최적화는 이 언어·구조 결정과 분리한다.

## 17. Acceptance criteria와 측정

모든 항목은 **아직 실행하지 않은 검증 조건**이다. 파일 syntax 검증과 실제 runtime 통과를 구분한다.

| ID | 검증 | 통과 조건 |
|---|---|---|
| AC-01 | 고정 3-pane 자체 wrapper | 원본 OMP 두 개와 host shell 표시, 기존 multiplexer 내부 spawn 없음 |
| AC-02 | 원본 OMP fidelity | Composer·model 선택·승인·tool 출력이 지원 capability 범위에서 동작, source patch 없음 |
| AC-03 | Focus와 키 전달 | Tab/Ctrl+C/modifier/붙여넣기/한글이 대상에만 전달, wrapper 단축키 remap |
| AC-04 | Terminal modes/resize | vim/less/REPL, alternate screen, 반복 resize, cursor·wide char가 지원 범위에서 유지 |
| AC-05 | App Python과 host 환경 | App venv가 실험 PATH/Python/CUDA 설정을 대체하지 않음. 작업 환경 before/after 비교 |
| AC-06 | Same-shell execution | integrated shell에서 conda/venv 활성화·export·cd 후 worker command가 같은 값 관측 |
| AC-07 | Safe dispatch 경합 | 미제출 입력·자동 수락 중 typing·REPL 상태에서 덮어쓰기/오입력 없음. Bash/zsh 각 시험 |
| AC-08 | 무출력·실패 lifecycle | `sleep`·non-zero script의 start/finish 정확, 출력 정지로 조기 완료 없음 |
| AC-09 | Shell edge cases | pipeline/list/부정 종료/Ctrl+C/Ctrl+Z/nested/background에서 거짓 완료 대신 지원 경계·UNKNOWN 표시 |
| AC-10 | Display/control 분리 | stdout에 가짜 lifecycle JSON·prompt를 출력해도 상태 변경 없음 |
| AC-11 | Independent OMP mailbox | 별도 두 process에서 task/question/answer/report 전달. 같은 process registry 성공만으로 대체하지 않음 |
| AC-12 | Native wake | eligible idle에서 공개 API 주입 후 실제 turn start 확인. Text/Enter 주입 없음 |
| AC-13 | Busy/draft/approval 경합 | 대기·보류가 동작, 주입 직전 상태변경에 기본 steer·중복 실행 없음 |
| AC-14 | Session binding | manager 또는 worker 세션 교체 후 이전 message/result의 자동 잘못된 주입 없음 |
| AC-15 | No-token observation | 60분 정상 관측-only 실행에서 감시용 model activation과 heartbeat conversation insertion 모두 0 |
| AC-16 | 유료 semantic review | 설정 간격·예산·usage 표기, 중복 review backlog 없음, budget 끝나면 분석 pause 표시 |
| AC-17 | No-progress 판단 | 출력/지표 변화 없음도 기준 도달 시 분석, 근거 없는 확정 hang/kill 없음 |
| AC-18 | Completion 우선순위 | tick 직후 종료해도 다음 tick까지 안 기다림. 오래된 review를 대신 처리하지 않음 |
| AC-19 | Human OFF/cancel | 새 자동 주입/run 거절, 기존 실행과 모델 turn은 별도 제어. 사용자 취소 자동 복구 없음 |
| AC-20 | 업무·코드 소유권 | worker의 소스 수정 제한, manager의 실험 중 보호 경로 쓰기 제한과 명시 worktree 사용 |
| AC-21 | Duplicate/unknown delivery | 같은 ID 재수신·ACK 손실에서 중복 run/wake 없음. 모호한 결과는 확인 필요, crash 후 자동 replay 없음 |
| AC-22 | Failure containment | OMP 한 개/bridge/DB/log 오류에 대해 명시 상태, 다른 live 자원 불필요 종료 없음 |
| AC-23 | Output·memory limits | 상한·순서 지연·섞인 출력 처리, artifact 잘림 표시, 무한 메모리 증가 없음 |
| AC-24 | 배포/단계 경계 | Lock·license·doctor 기록, 2차 서버 없이 1차 설치·실행. CUDA 없는 host에는 CUDA 통과 표시 금지 |

### 17.1 초기 성능 시험 목표

- **Idle 전달:** 유효 `command.finished` 수신부터 bridge 주입 요청까지 p95 1초 이내. OMP가 busy/approval/pending/manual hold인 대기 시간과 provider 응답 시간은 제외한다.
- **상태 반영:** 종료 event 수신부터 UI/state 업데이트까지 p95 250 ms 이내.
- **부하:** 시험용 200×60 외부 화면, 3 PTY 합산 1 MiB/s 출력을 10분 공급하며 input→local redraw p95 150 ms 목표, queue 상한과 메모리 추이를 기록한다. 이는 실증 전 초기 test workload다.
- **장기:** 8시간 soak test에서 fd·task·memory 누수 추이, review 비용과 누적 문맥, 종료 감지 지연을 기록한다. 120회 기본 review 예산을 그대로 쓰면 2시간 후 분석이 멈추므로, 장기 시험 목적에 따라 명시 증액하거나 budget pause를 시험한다.
- **환경:** OS/CPU/메모리/outer terminal·multiplexer/shell/OMP/libvterm/Textual 버전과 조건을 함께 기록한다. 기준 machine이 다르면 수치를 그대로 보증으로 사용하지 않는다.

모든 실험에서 실제 OMP에 쓴 token과 model 없는 harness 결과를 분리한다. Unit test의 mock `sendMessage` 성공만으로 native wake acceptance를 통과시키지 않는다.

## 18. 구현 순서와 종료 조건

| 단계 | 구현·검증 | 종료 조건 |
|---|---|---|
| M0-A | Textual + libvterm + PTY로 실제 OMP/host terminal | 입력·resize·한글·query·배포 binding 검증. 텍스트 mock 금지 |
| M0-B | 같은 shell dispatch + lifecycle | 사용자의 conda/venv·cwd 유지, 입력 경쟁·job control 시험 |
| M0-C | 독립 manager/worker TS bridge·mailbox·native wake | 실제 두 OMP 양방향, session binding·non-interrupting delivery 확인 |
| M1 | 고정 3-pane UI와 core runtime | Role UI·focus·상태·manual 제어·제한 로그 완성 |
| M2 | TaskSpec·역할 도구·mailbox·worker review | 작업 하나 end-to-end, 비용·취소·정기 분석과 종료 통지 분리 |
| M3 | 오류 주입·packaging·호환성 검증 | AC-01~24 결과와 lock·known limitations 기록 |
| Phase 2 | MCP Task Inbox + Web Board | **M3 완료 후 별도 착수. 이번에는 문서만** |

M0-A/B/C는 넓은 기능 개발 전에 가장 위험한 가정을 검증하는 묶음이다. 셋 중 하나가 실패했다고 wrapper·같은 shell·원본 두 OMP를 다른 제품으로 대체하지 않는다. Backend/adapter를 보완하거나 지원 조합을 명확히 줄인 뒤 기록한다.

M0 도중에는 먼저 한 shell에서 짧은 검증을 할 수 있으나, 1차 출시의 Bash/zsh 목표를 통과한 것으로 표시하지 않는다. 구현 시간·코드 줄 수·token 비용을 근거 없이 견적 내지 않는다.

## 19. 미확정 항목의 처리와 최종 판정

### 19.1 코드 작성 전에 결정된 것

제품 형태, manager/worker 역할, Python-first, 공개 TS bridge, host same-shell 실행, UDS mailbox, 고정 자동화 정책, bounded persistence, 2차 Deferred 범위는 이번 기준선으로 채택한다.

### 19.2 M0에서 실증해 고정할 것

정확한 Textual/libvterm/CFFI release 조합, native binding의 distribution, OMP public capability·delivery option의 실제 동작, Bash/zsh safe dispatch 구현, outer keyboard/terminal compatibility는 시험 결과로 고정한다. 이를 문서에서 선택하지 않은 언어 문제로 다시 확대하지 않는다.

### 19.3 이번에 하지 않은 것

실제 OMP 두 프로세스 실행, native binding 구현·컴파일, Textual 화면 렌더링·사용자 키 입력 시험, CUDA 접근 확인, 모델 비용 benchmark, MCP 서버 구현은 수행하지 않았다. 아래 공식 자료 확인과 문서·설정 파일의 구조 검증만 수행한다.

**최종 판정:** 개발 방향은 적절하다. 전체 TS 또는 전체 Rust로 바꾸기보다 사용자가 유지보수할 Python을 중심으로 삼는 선택을 채택한다. 다만 embedded terminal과 safe shell dispatch는 이 제품의 본질적인 기술 과제이므로 '작은 adapter니까 쉽다'고 취급하지 않는다. 핵심 사용 경험을 보존하면서 단순한 고정 정책을 유지한다.

## 20. 공식 참고 자료와 검증 범위

아래 자료는 2026-09-23 문서 재검토 시 확인한 외부 근거다. 웹 문서의 `main`/`latest`는 release lock이 아니다. 개발 시 실제 release/commit으로 고정한다. 각 source는 해당 기술의 공개 기능을 뒷받침하며, 이 제품의 integration이 통과했다는 증거는 아니다.

| ID | 자료 | 참고한 내용 |
|---|---|---|
| R1 | Python 3.13 `pty` | Unix PTY, 플랫폼 의존성, macOS fork 주의 |
| R2 | Python 3.13 asyncio event loop | FD·subprocess·socket·timer 처리 |
| R3 | Textual widgets | Custom widget/line rendering |
| R4 | Textual input | Key·focus·bindings와 terminal 입력 제약 |
| R5 | libvterm 공식 소개 | C99 embedding terminal emulator, renderer 독립 |
| R6 | CFFI 공식 overview | Native API binding의 구조 |
| R7 | Python 3.13 subprocess | Session·FD 제어, preexec_fn thread 위험 |
| R8 | OMP extensions 문서 | TS/JS module, public message·tool·lifecycle·managed timer |
| R9 | OMP hub 문서 | Process-global messaging와 process supervision의 구분 |
| R10 | zsh Functions | preexec/precmd/chpwd hook 의미 |
| R11 | zsh Line Editor | ZLE·widget·입력 buffer 관리 경계 |
| R12 | Git worktree | Host의 별도 checkout 작업 디렉터리 |
| R13 | 공식 MCP Python SDK | Python server/client SDK |
| R14 | MCP 2025-11-25 Transports | Streamable HTTP, local/Origin/security 경계 |
| R15 | Python version status | 지원 minor 선택 참고 |
| R16 | uv tools 문서 | App tool environment와 설치 |
| R17 | FastAPI templates | Server-rendered Jinja2 UI 후보 |

```text
R1  https://docs.python.org/3.13/library/pty.html
R2  https://docs.python.org/3.13/library/asyncio-eventloop.html
R3  https://textual.textualize.io/guide/widgets/
R4  https://textual.textualize.io/guide/input/
R5  https://www.leonerd.org.uk/code/libvterm/
R6  https://cffi.readthedocs.io/en/latest/overview.html
R7  https://docs.python.org/3.13/library/subprocess.html
R8  https://raw.githubusercontent.com/can1357/oh-my-pi/main/docs/extensions.md
R9  https://raw.githubusercontent.com/can1357/oh-my-pi/main/docs/tools/hub.md
R10 https://zsh.sourceforge.io/Doc/Release/Functions.html
R11 https://zsh.sourceforge.io/Doc/Release/Zsh-Line-Editor.html
R12 https://git-scm.com/docs/git-worktree
R13 https://github.com/modelcontextprotocol/python-sdk
R14 https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
R15 https://devguide.python.org/versions/
R16 https://docs.astral.sh/uv/guides/tools/
R17 https://fastapi.tiangolo.com/advanced/templates/
```

**구현의 최상위 원칙:** OMP는 판단과 대화를, host shell은 실제 실행을, Workbench는 화면·관측·연결과 제한된 자동화를 담당한다.
