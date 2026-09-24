# Core 저장소·외부 자료 조사 근거

조사일: 2026-09-23. 문서·소스·도구 결과의 관측 기록이며 제품 구현·호환성 통과 증거가 아니다.
현재 요구사항은 [BRIEF](BRIEF.md)를 따른다. 과거 환경 관측을 현재 설치 상태로 재사용하지 않는다.

- 조사일: 2026-09-23. HEAD: `fea46fd561f1da1cef32be191775b56c1cff0a13`.
- `git ls-tree -r --name-only HEAD`: 추적 파일은 README.md뿐이었다. 작업 디렉터리에는 workflow 설정과 PRD가 있으나 제품 구현은 찾지 못했다.
- [Phase 2 PRD v0.1](../../OMP_Workbench_Phase2_Task_Inbox_PRD_v0.1.md) §0.3·§15가 선행 core의 입력 근거다. SHA-256: `7b612cad6913317eae33c09f5ec654dd691cb257fe9a617d1bf29a9e999be4e3`.
- PRD가 언급하는 1차 PRD v0.2 및 이전 대화 원문은 이 저장소에서 찾지 못했다. 내용을 추정하지 않는다.
- [사용자 확인] 초기 답변 `1-b`에서 1차 Workbench 구현이 없음을 확인했다.
- [로컬 관측, 2026-09-23] `command -v omp`, `readlink -f /home/dwchoo/.bun/bin/omp`, `omp --version` 결과: 전역 설치 `@oh-my-pi/pi-coding-agent`, 버전 `13.5.7`. 소스 루트는 `/home/dwchoo/.bun/install/global/node_modules/@oh-my-pi/pi-coding-agent/`다. 해당 설치의 `src/index.ts`, `src/sdk.ts`, `src/modes/rpc/rpc-types.ts`, `examples/sdk/README.md`에서 공개 export·세션 생성·RPC 연결 후보를 확인했다. 모델 호출 및 통합 실행은 하지 않았다.
- [외부 문서 관측, 2026-09-23] PRD가 인용한 snapshot `ef6d8b2d0c2af26417c633619d8dcce1cc61a226`의 [hub 문서](https://github.com/can1357/oh-my-pi/blob/ef6d8b2d0c2af26417c633619d8dcce1cc61a226/docs/tools/hub.md)는 peer messaging을 process-global bus로 설명한다. Process supervision의 cross-instance 동작과 구분해야 한다.
- [호환성 간극] 로컬 13.5.7의 `src/tools` 파일 목록에는 위 문서의 `hub` 디렉터리가 없었다. PRD 인용 snapshot의 기능이 로컬 설치본에서 동작한다고 가정할 수 없다. 재사용 여부·wrapper bridge 필요성·지원 버전은 보류한다.
- [환경 변경 관측, 2026-09-23] 후속 조사에서 기존 Bun 설치 소스 경로를 읽으면 `No such file or directory`가 반환되었다. `command -v omp`는 `/home/dwchoo/.local/bin/omp`, `omp --version`은 `omp/18.2.10`, `file`은 Linux x86-64 ELF 실행 파일을 반환했다. 이 인터뷰 작업에서 설치·업데이트를 실행하지 않았다. 이전 13.5.7 근거는 과거 관측으로 보존한다.
- [공개 문서, 18.2.10] [Extensions](https://github.com/can1357/oh-my-pi/blob/v18.2.10/docs/extensions.md), [RPC](https://github.com/can1357/oh-my-pi/blob/v18.2.10/docs/rpc.md), [hub](https://github.com/can1357/oh-my-pi/blob/v18.2.10/docs/tools/hub.md)를 조회했다. Extension 문서의 Message delivery semantics와 managed timer가 연결 후보의 근거다. 문서 조회만으로 임베딩·독립 두 프로세스의 mailbox 연결·terminal 통합 성공을 주장하지 않는다.
- [과거 대안 조사, 2026-09-23] `command -v tmux` → `/usr/bin/tmux`, `tmux -V` → `tmux 3.4`, `uname -s` → `Linux`. [tmux 공식 문서](https://github.com/tmux/tmux/wiki)의 detach 기능은 확인했으나 내부 재사용 제안은 C-D25로 철회했다. 설치되어 있다는 사실을 제품 의존성의 근거로 삼지 않는다.
- [로컬 CLI 관측] `omp --help`에서 `--extension`, `--no-extensions`, `--append-system-prompt`, `--session-dir`, `--resume`, `--config`, `--tools`, `--model`과 승인 관련 옵션을 확인했다. 실제 wrapper 연결·역할 분리·사용자 제어권 구현에 충분한지는 미검증이다.
- [공개 소스 관측, v18.2.10] [pi-tui exports](https://github.com/can1357/oh-my-pi/blob/v18.2.10/packages/tui/src/index.ts)와 [README](https://github.com/can1357/oh-my-pi/blob/v18.2.10/packages/tui/README.md)에서 `SplitPane`·`Row`·`Stack`·`Terminal` 관련 공개 export를 확인했다. 두 OMP 전체 TUI의 임베딩 또는 terminal emulation 지원을 검증한 것은 아니다.
- [추가 사용자 제공 문서] [개발 스펙 v0.3](../../OMP_Workbench_Development_Spec_v0.3.md), 713줄, SHA-256 `d9db3315a48b25c831f71f46c438947c6e781dbd424db50e0e2ab2a2ba8193f0`. 문서 내용·상충 정책·공식 자료 확인 범위는 [대조표](SOURCE-RECONCILIATION.md)에 기록한다. 원문은 수정하지 않았다.
- [로컬 관측, 2026-09-23] `python3 --version` → `Python 3.12.3`, `command -v uv` → `/home/dwchoo/.local/bin/uv`. CPython 3.13 및 Textual/native 조합은 아직 설치·검증하지 않았다.
- [로컬 shell 관측, 2026-09-23] `command -v bash` → `/usr/bin/bash`, `bash --version` → `5.2.21`. `command -v zsh`는 현재 PATH에서 찾지 못했다. 이는 zsh 제품 지원 여부 결정이나 호환성 검증 결과가 아니다.

- [로컬 shell 추가 관측, 2026-09-23] `command -v bash` → `/usr/bin/bash`, `command -v sh` → `/usr/bin/sh`, `readlink -f /bin/sh` → `/usr/bin/dash`. 현재 host에서 sh가 가리키는 구현을 확인했으며 sh adapter·same-shell dispatch·OMP 통합은 시험하지 않았다.
