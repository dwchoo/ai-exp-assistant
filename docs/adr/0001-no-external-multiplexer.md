# ADR-0001: Workbench 내부의 외부 terminal multiplexer 의존 금지

상태: 사용자 결정 수용, 2026-09-23. 내부 구현 방식과 전체 BRIEF 승인은 별개다.
근거: [Core 결정 이력](../features/core-workbench/DECISIONS.md)의 C-D25.

## 배경

Workbench는 기존 OMP TUI 두 개와 host terminal을 한 화면에 표시하고, 화면을 닫아도 실행 환경을 유지해야 한다. 초기 조사에서 tmux의 분할·detach 기능 재사용을 제안했다.

사용자는 Workbench 자체를 tmux 또는 herdr 안에서 사용할 예정이며, tmux-in-tmux 등 중첩 multiplexer의 입력·운영 문제를 피하기 위해 이 제안을 명시적으로 거절했다.

## 결정

- Workbench 내부에서 tmux나 다른 외부 terminal multiplexer 프로그램을 시작하거나 필수 런타임으로 요구하지 않는다. 해당 프로그램을 번들하거나 이름을 바꾸는 방식으로 우회하지 않는다.
- 외부 terminal 앱이나 tmux·herdr는 사용자가 선택하는 실행 환경이다. Workbench는 그 안에서 하나의 terminal 앱으로 동작해야 한다.
- PTY·terminal protocol·레이아웃 관련 라이브러리와 기술을 재사용할 수 있다. 구체 라이브러리 선택은 공개 API·라이선스·호환성 검증 후 결정한다.
- 기존 OMP TUI 유지, 세 영역 화면, 자체 detach/재접속이라는 사용자 요구는 유지한다.

## 대안과 결과

- **외부 multiplexer 재사용:** 화면·세션 관리 구현을 줄일 수 있으나 사용자 실행 환경과 중첩되므로 채택하지 않는다.
- **Workbench의 화면·세션 관리:** 사용자 요구에 맞지만 terminal 상태·입력 전달·resize·재접속 관리와 호환성 검증이 필요하다. 이 방향으로 구체화한다.
- **전용 단순 대화 UI:** 기존 OMP TUI 유지 요구를 축소하므로 동의 없이 대체하지 않는다.

PTY별 화면 상태를 조합할지, 공개 TUI 인터페이스를 사용할지 등은 미결정이다. 자체 구현도 terminal protocol 충돌 가능성이 있으므로 무충돌을 선언하지 않고 수락 시험으로 확인한다.

## 검증 요구

- 내부에서 외부 multiplexer 실행 파일을 찾거나 실행하지 않는지 확인한다.
- 일반 terminal, 사용자가 지정한 tmux·herdr 환경에서 입력 대상, 단축키 전달, 한글·다중 행 붙여넣기, resize·화면 복원, detach/재접속을 확인한다.
- 외부 환경의 설정·키 바인딩을 임의 변경하지 않는다. 실제 시험하지 않은 환경은 검증 완료로 표시하지 않는다.
