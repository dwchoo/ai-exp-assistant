# Core 요구사항과 개발 스펙 v0.3 대조

작성일: 2026-09-23. 참고 원문은 [통합 개발 스펙 v0.3](../../OMP_Workbench_Development_Spec_v0.3.md).
SHA-256: `d9db3315a48b25c831f71f46c438947c6e781dbd424db50e0e2ab2a2ba8193f0`.
사용자는 이 문서를 참고하도록 요청했고 Python 중심 개발을 명시했다. 원문의 “채택” 표시는 이번 대화의 모든 결정을 대체하는 승인이 아니다.
현재 사용자 결정은 [BRIEF](BRIEF.md), 제품 동작은 [동작 규약](OPERATING-CONTRACT.md), 용어는 [CONTEXT.md](../../../CONTEXT.md)를 참조한다.

## 이미 정한 방향과의 관계

| 항목 | v0.3 내용 | 현재 반영 |
|---|---|---|
| 제품 형태 | 자체 wrapper, 원본 OMP 두 개, host terminal 하나 | 기존 합의와 일치. 외부 multiplexer 내부 실행 금지 유지 |
| 주 언어 | CPython 3.13, Python-first | Python 중심은 C-D28로 확정. 구체 minor·의존성 lock은 기술 기준 후보 |
| 지원 OS | Linux x86_64 + macOS arm64 초기 시험 | C-D26에 따라 Linux만 첫 지원. macOS는 OS 경계 분리로 향후 고려. Windows 제외 |
| TUI fidelity | 원본 OMP·host interactive terminal 유지 | C-D27의 텍스트 조작 우선 범위로 검증. Inline image는 1차 필수 아님 |
| 프로세스 수명 | §3 단일 TUI/runtime, 자체 daemon·재attach 없음 | C-D21/C-D25와 충돌. 자체 detach와 실행 지속을 유지하며 Python frontend/backend 수명 분리를 설계해야 함 |
| 재시작 | §12.3 wrapper crash 복원 비보장, 새 role 연결은 사용자 확인 | C-D22의 기록·실제 프로세스 대조에 의한 확인 가능한 복구를 유지. 불명확한 실행·메시지를 자동 replay하지 않음 |
| 실패 복구 재시도 | §9.4·§16 기본 0회, 명세에서 허용 | C-D19의 작업당 기본 3회 유지. 사용자 위임 범위는 별도로 필요 |
| 로그 점검 | 활성 작업 60초, worker 모델 사용 | C-D17과 일치. C-D36: 대화 우선·최신 한 건 유지·지연 표시, 종료 이벤트 즉시 기록·표시 |
| 분석 횟수 | Command당 120회 후 분석만 정지 | C-D29로 채택하지 않음. 고정 호출 횟수 제한 없이 활성 실험 동안 계속하고 사용량 표시·사용자 중지를 제공 |
| Peer wake | Task당 12회 제한 | C-D30으로 채택하지 않음. 정상 업무 대화에 고정 횟수 제한 없음. 위임 범위·복구 재시도 한도·중복 처리 방지는 유지 |
| 실행 중 별도 개발 | §7.2 development worktree에서 동시 개발 기본 | 실행용/개발용 분리 가이드는 유지. 실행 중 다음 실험의 병렬 개발을 1차 필수 기능으로 추가한 것은 아님 |
| 전체 종료·재부팅 | 자체 실행 수명 보완 필요 | C-D31/C-D32: 전체 종료는 확인 후 중단·정리, 재부팅 후 새 실험은 사용자 확인 |
| 로그 보존 | run당 64 MiB·프로젝트당 512 MiB | C-D33으로 채택. 상한 후 추가 저장 중단·잘림 표시, 실행·관측 유지. 작업·요약·실행용 worktree는 자동 삭제하지 않음 |
| Shell | Bash 5.x·zsh 5.9 | C-D41/C-D42: Bash 우선·sh 최소 지원. 기본/login shell이 zsh여도 전용 persistent Bash 또는 sh 시작, 사용자 기본 shell 설정 유지. zsh adapter는 후속 |
| Worktree 준비 실패 | 실행 경로·기준 commit 확인 | C-D40: manager/worker가 허용 범위 내 자동 해결, 해결 불가·추가 권한 필요 시 사용자 확인. 전용 관리 기능은 추가하지 않음 |
| 실험 환경 | App/host 환경 분리 | C-D43: 기존 conda/venv 재사용, 새 환경·패키지 변경은 위임 범위에서만 |
| 일시정지 | 새 자동 주입·run 중지 | C-D49: 즉시 새 자동 지시 보류, manager 진행 중 turn 중단 요청·확인/불명 결과 구분, 새 자동 모델 작업 중단; 로그·프로세스 수집과 기존 host 실험 유지 |
| 강제 종료 | 대상 확인 후 interrupt | C-D45: 일반 중단 실패 시 허용 범위·대상 확인 조건으로 manager 판단, 미충족 시 사용자 확인 |
| 저장 장애 | Metadata/raw log 분리 | C-D46: metadata 실패 시 새 자동 실행 보류, 기존 실행·관측 유지. Raw log 실패만으로 실행 중단하지 않음 |
| Phase 2 | 웹/MCP 별도 후속 개발 | 기존 합의 유지. 제시된 서버 스택을 1차 의존성에 넣지 않음 |

## 상세화에 활용할 요구사항 입력

다음은 참고 명세에서 가져온 입력이다. 아직 구현·호환성 통과 사실이 아니며 최종 BRIEF 검토에 포함한다.

- Host에서 Workbench 전용 persistent Bash 또는 sh를 시작하고 같은 shell을 실제 terminal로 유지한다(C-D41/C-D42). 사용자가 그 안에서 설정한 cwd·conda/venv·export를 worker 실행이 사용한다. Worktree 실행 경로 전환도 같은 shell과 승인 범위에서 다룬다.
- App Python 환경과 실험 환경을 분리한다. Workbench의 Python executable·venv를 실험용 `python`이나 CUDA 환경으로 치환하지 않는다.
- 명령 시작·종료는 shell adapter의 lifecycle로 관측한다. Prompt 모양·로그 무출력·stdout의 가짜 marker로 성공이나 종료를 판정하지 않는다.
- 사용자 미제출 입력·REPL·suspended job과 자동 실행의 경합을 다룬다. 불명확한 입력 경계에서는 새 shell로 우회하지 않고 자동 실행을 보류한다.
- Python이 작업·상태·관측·저장·전달 정책을 소유한다. 작은 TS extension은 공개 OMP API 연결과 주입 직전 session/idle/pending/approval 확인을 담당한다.
- Model은 기존 manager/worker OMP가 호출한다. Python 본체에 별도 판단 agent나 provider credential 저장소를 만들지 않는다.
- 본문·로그 출력과 제어 IPC를 분리한다. 명세 revision, command ID, message ID, session binding 및 접수/실제 시작/업무 완료를 구분한다.
- C-D34에 따라 범위 승인과 진행 지시로 자동화를 시작한다. C-D49의 자동화 일시정지는 manager OMP 진행 중 turn 중단을 즉시 요청하고 manager·worker의 새 자동 모델 작업을 보류한다. 모델 점검은 멈추지만 로그·프로세스 수집과 이미 실행 중인 host 실험은 유지한다. 요청/확인/불명 결과, 사용자 취소, terminal 입력 제어권을 구분한다. 사용자 취소를 실패 복구 대상으로 자동 재실행하지 않는 규칙을 상세화한다.
- SQLite metadata, 원문 로그 파일, TOML 설정, Pydantic 경계 검증, uv·pytest·Ruff·mypy는 기술 후보다. 제시된 용량·성능·비용 수치를 검증 완료값으로 취급하지 않는다.

## 기술 검증 우선순위 후보

1. **원본 TUI와 PTY:** Python + Textual custom widget + libvterm/CFFI 후보로 OMP composer·승인·키·한글·resize·query reply를 확인한다. 실패를 tmux나 headless 대화 UI로 대체하지 않는다.
2. **같은 shell 실행:** 지원 shell의 입력 경합, 환경 보존, lifecycle, job control을 실제로 확인한다. C-D42에 따라 G2에서 Bash 5.x와 실제 sh 구현을 검증하며 zsh는 후속으로 둔다. 최초 shell 선택과 실행 중 다른 shell로 우회하는 동작을 구분한다.
3. **독립 OMP 연결:** 공개 TS extension + Python UDS bridge로 명세·질문·답변·보고 및 non-interrupting wake를 확인한다.
4. **자체 detach/재접속:** 원문 M0에 없는 필수 보완이다. Python 화면을 분리해도 backend와 세 PTY가 유지되고, 자동화 활성 중에는 1분 모델 점검이 계속되며 일시정지 중에는 새 모델 점검이 시작되지 않는지 확인한다. 재접속이 중복 실행 없이 화면과 제어권을 복원하는지도 확인한다.

이는 BRIEF의 설계 방향이며 SPEC·ticket graph·일정 확정이 아니다. 넓은 기능 개발 전에 이 기술 가정을 검증하는 실행 계획으로 인계한다.

## 근거와 미확인 사항

- 원문의 B0 및 일부 이전 대화·동반 설정 예시는 현재 저장소에서 별도 확보하지 못했다. v0.3에 기술된 내용 이상의 원문 검증을 주장하지 않는다.
- 2026-09-23 `python3 --version`은 `Python 3.12.3`, `command -v uv`는 `/home/dwchoo/.local/bin/uv`였다. CPython 3.13 개발 환경이나 native dependency를 설치하지 않았다.
- [Python PTY](https://docs.python.org/3.13/library/pty.html), [Textual custom widgets](https://textual.textualize.io/guide/widgets/), [libvterm](https://www.leonerd.org.uk/code/libvterm/), [CFFI](https://cffi.readthedocs.io/en/latest/overview.html)를 확인했다. 공개 기능 확인이며 Workbench 통합·성능·배포 검증은 아니다.
