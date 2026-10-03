# UR-UX 사용자 검토 안내 (CW-06 제품 UI)

- 대상 후보: CW-06 통합 후보 `671b05d3…` ([통합 기록](../integration-p27-v-cw06.json)) → UR-UX 반영 후보 `0877c6df…` ([UR-UX delta 통합](../integration-p27-v-cw06-urux.json))
- 1차 검토 결과: [C-D58](../../../../../docs/features/core-workbench/DECISIONS.md#C-D58) — 배치 변경·terminal 스크롤 반영, Q1 (a)는 CW-18, Q2는 CLI 유지.
- 근거: PLAN `user_review_checkpoints` UR-UX, [C-D57](../../../../../docs/features/core-workbench/DECISIONS.md#C-D57)
- UR-UX가 끝나기 전에는 CW-18을 시작하지 않는다.

## 1. 실행 방법

설치 없이 sandbox 폴더에서 실행한다. 일반 terminal, tmux, herdr 안에서 모두 실행할 수 있다.

```bash
cd ~/wb-urux-sandbox
./wb            # 시작 (이미 실행 중이면 다시 붙기)
./wb status     # 상태 보기
./wb down       # 전체 종료 (확인 질문에 y)
```

- backend가 따로 뜨고 제품 UI가 붙는다. 두 OMP는 사용자 주변 설정에서 격리되고(C-D59) 인증·provider·모델 설정만 쓴다. OMP에 말을 걸면 실제 모델 호출이 일어난다.
- UI만 닫기(detach): `Ctrl-] q`. backend, 두 OMP, host shell은 계속 실행된다. 다시 붙기는 `./wb`.
- 새 코드는 backend를 새로 시작해야 적용된다(`./wb down` 뒤 `./wb`).

## 2. 키

prefix는 `Ctrl-]`다. 한글 입력 상태에서도 되도록 Ctrl 조합(Ctrl을 누른 채 `]` 다음 글자), `Ctrl-] Space` 메뉴, 자모+Space를 함께 쓸 수 있다.

| 동작 | 키 |
|---|---|
| detach | `Ctrl-] q` / Ctrl 누른 채 `]`+`q` / `Ctrl-] ㅂ Space` / 메뉴 0 |
| 명령 메뉴 | `Ctrl-] Space` 뒤 번호·화살표·Enter |
| pane focus | `Ctrl-]` 뒤 `1` / `2` / `3`, `Tab`, 또는 pane 클릭 |
| 확대(zoom) | `Ctrl-] z` / Ctrl 누른 채 `]`+`z` / `Ctrl-] ㅋ Space` |
| host shell 인수 요청 / 확인 | `Ctrl-] t` / `Ctrl-] c` (Ctrl 조합 `Ctrl-t` / `Ctrl-y`) |
| handoff | `Ctrl-] h` (Ctrl 조합 `Ctrl-o`). 먼저 shell에서 `wb-handoff` |
| 다시 그리기 / 마우스 캡처 | `Ctrl-] r` / `Ctrl-] m` (Ctrl 조합 `Ctrl-r` / `Ctrl-e`) |
| 창 크기 조절 | 경계선 드래그, `Ctrl-]` 뒤 화살표(이어서 화살표 반복), `Ctrl-] =` 기본값 |
| 스크롤 | 마우스 휠, Shift+PgUp/PgDn, `Ctrl-] [` 스크롤 모드(q/Esc 종료) |
| **복사** | pane 안에서 드래그 → 놓으면 clipboard로 복사("복사됨" 표시). 한 pane 안만 선택되고, 위·아래 끝을 넘으면 자동 스크롤 |
| **종료된 OMP 다시 시작** | 그 pane에서 Enter → 새 OMP 세션. 이전 대화는 새 OMP에서 `/resume` |
| **종료된 host terminal 다시 시작** | 그 pane에서 Enter → 새 shell (처음 시작과 같은 설정, 입력 권한은 사용자) |
| **host terminal 강제 종료** | `Ctrl-] k`(Ctrl 조합 `Ctrl-k`, 한글 `ㅏ Space`) → 확인 창에서 `k` 한 번만. 다른 키·여러 키·붙여넣기·창 크기 변경은 취소. manager가 쓰는 중이면 경고 표시 |
| OMP에 Ctrl-d 보내기 | 2초 안에 두 번 (한 번은 경고만). host shell은 바로 전달 |
| 도움말 | `Ctrl-] ?` |
| `Ctrl-]` 문자 자체 | `Ctrl-]` 두 번 |

- 복사는 바깥 terminal의 clipboard로 OSC 52를 보낸다. herdr는 그대로 된다. tmux는 `set -s set-clipboard on` 또는 `set -g allow-passthrough on`이 필요하다(현재 사용자 설정은 둘 다 켜져 있음).
- mouse를 직접 쓰는 pane 프로그램(예: mouse를 켠 vim)에는 드래그를 그대로 전달한다. 그때 terminal 자체 선택은 Shift+드래그다.
- wb가 띄우는 OMP는 wb 전용 OMP 홈(`<data dir>/omp-root`)을 쓴다(C-D64). 사용자 전역 설정·skill·agent·세션·memory를 보지 않고 사용자 `~/.omp`에 기록하지 않는다. 인증만 사용자 OMP 인증 파일(`agent.db`)을 symlink로 공유하므로 login/logout은 일반 omp와 함께 적용된다. 인증 파일이 없고 provider API key 환경 변수도 없으면 wb는 시작하지 않고 안내만 보인다(밖에서 `omp` 실행 후 `/login`).
- wb OMP에서는 OMP 브라우저 도구가 꺼져 있다(사용자 결정). worktree·cache도 wb 홈 안에 생긴다.
- 예외: OMP 실행 부품(`~/.omp/natives/<버전>`)은 OMP가 항상 그 위치에 푼다. OMP 업그레이드 직후 일반 omp보다 wb를 먼저 켜면 wb OMP가 그 버전의 부품을 풀 수 있다(일반 omp와 같은 파일).
- 종료된 OMP나 host terminal을 다시 시작하면 이전 세션에 남아 있던 process(예: 그 OMP가 백그라운드로 띄운 명령, 종료된 shell에 남은 `nohup` job)는 정리된다. 다시 시작하기 전에 필요한 process인지 확인한다.
- host terminal 강제 종료는 shell과 그 job을 모두 종료한다. 스스로 session을 분리한 daemon(`setsid`, double-fork)은 종료하지 않는다. 권한이 없어 종료할 수 없는 process(예: `sudo`로 실행 중인 명령)는 남은 process로 보고된다.

## 3. 확인 목록 (UR-UX 범위, 1차 답변 완료)

항목마다 **좋음 / 바꾸고 싶음 / 모르겠음** 중 하나와 한 줄 의견을 남겨 주면 된다.

1. **세 영역 배치와 크기**: 3열 배치가 읽기 좋은가. OMP 화면이 너무 좁지 않은가(스냅샷 01, 15, 16).
2. **focus와 host 입력 owner 구분**: 상단 두 줄만 보고 "지금 어디에 입력되는가"와 "host shell 입력권이 누구에게 있는가"를 구분할 수 있는가(03, 05–08).
3. **pane 전환과 단축키**: `Ctrl-]` prefix가 불편하거나 OMP·shell 키와 충돌하는가(02).
4. **붙여넣기**: 한글·여러 줄 붙여넣기와, 2 MiB를 넘는 붙여넣기의 거절 표시가 이해되는가(09, 10, 10b).
5. **detach/재attach**: 닫았다가 다시 붙었을 때 화면 복원이 자연스러운가(13, 14).
6. **상태 문구**: 자동화 상태, shell mode, 마지막 확인 시각, 거절·보류 사유 문구가 이해되는가(05b, 07a, 08, 11).

## 4. 결정이 필요한 질문

아래는 배치·키·문구가 아니라 제품 동작에 관한 질문이다. 답에 따라 새 결정 기록과 계획 변경이 필요할 수 있다.

- **Q1. background job 때문에 handoff가 막히는 경우**
  - 상황: host shell에서 사용자가 background/suspended job을 남긴 채 `wb-handoff`를 하면 handoff가 보류된다.
  - 문제: 이때 shell이 수동 입력과 인수 확인을 모두 거부해서, UI 안에서는 빠져나올 방법이 없다.
  - 배경: 보류 자체는 명세된 fail-safe이고, G2 문서에 미확인 항목으로 남아 있다.
  - 선택지:
    - (a) 보류 중에도 사용자 인수와 수동 입력을 허용한다.
    - (b) 보류 사유와 해결 방법(job 정리 후 재시도)만 안내한다.
    - (c) 지금처럼 두고 CW-18에서 다룬다.
- **Q2. UI 안의 전체 종료·boot 확인 조작**: 지금은 CLI(`shutdown`)로만 할 수 있다. UI에도 이 조작이 있어야 하는가? 재부팅 뒤 `confirm_boot` 화면은 CW-19에서 만든다.

## 5. 알려진 제한

검토 중 아래 현상을 만나도 결함으로 보고하지 않아도 된다. 의견은 환영한다.

- **여러 줄 입력 뒤 handoff 보류**: host shell에 여러 줄을 직접 입력하거나 붙여넣은 뒤, 또는 대량 출력 후 Ctrl-C 뒤에는 `wb-handoff`가 보류될 수 있다. 안전 경계를 확인할 수 없을 때 보류하는 설계이고, 화면에 `held:` 사유가 표시된다(07a).
- **대량 출력 중 화면**: 출력이 매우 많으면 pane이 "출력 따라잡음"을 표시하고 오래된 화면 출력을 건너뛴다. backend 기록은 그대로 남는다. 출력이 이어지는 동안에는 화면이 반복해서 새로 그려질 수 있다(11, 12).
- **자동화 연결 전**: Task 흐름, 60초 점검, 일시정지는 CW-18 전이라 연결되지 않았다. 상태는 `not_configured`로 보이고, 승인·진행·일시정지 조작도 아직 없다.
- **재attach 뒤 인수 요청**: 재attach하면 대기 중인 인수 요청은 따로 표시되지 않는다. owner와 mode는 표시된다.
- **스크롤 기록**: host terminal 5000줄, OMP pane 1000줄을 보관한다. 대량 출력 따라잡기나 세션 교체 때는 그 pane의 기록이 지워진다. vim 같은 전체화면 프로그램(alternate screen)이 떠 있는 동안에는 스크롤 모드가 꺼진다.
- **렌더링 한계**:
  - 색은 256색 근사다.
  - scrollback(위로 스크롤) UI는 없다.
  - pane 안 프로그램의 DEC 선그리기 문자(`ESC(0`)는 문자로 보일 수 있다.
- **승인 prompt**: 현재 OMP 설정에서는 shell tool을 실행할 때 승인 prompt가 뜨지 않아 승인 조작은 검증하지 못했다.

## 6. 화면 스냅샷

### 새 배치(C-D58 반영, [snapshots-v2/](snapshots-v2/))

위에 manager·worker OMP 두 pane, 아래에 전체 폭 host terminal이다.

| 파일 | 상태 |
|---|---|
| 01-start-attach | 시작 직후 새 배치 |
| 02-help-overlay | 도움말(스크롤 키 포함) |
| 03-host-shell-seq-300 | `seq 1 300` 뒤 live 화면 |
| 04-scroll-mode | `Ctrl-] [` + PgUp 3회: 제목에 `[SCROLL live보다 N줄 위/총 M]` |
| 05-scroll-new-output | 스크롤 중 새 출력이 와도 보던 줄 유지 |
| 06-scroll-exit-live | q 뒤 live 화면 |
| 07-narrow-100x30 / 08-wide-200x50 | 좁은/넓은 화면 |
| 09-flood-catchup | 대량 출력 중 따라잡음 표시 |

### 이전 3열 배치(참고, [snapshots/](snapshots/))

[snapshots/](snapshots/)에 실제 OMP 18.2.10으로 모델 호출 없이 찍은 text 화면 19장이 있다. 크기는 160x45이고, 15·16만 다르다.

| 파일 | 상태 |
|---|---|
| 01-start-attach | 시작 직후: 세 pane, 상단 상태 두 줄, footer |
| 02-help-overlay | `Ctrl-] ?` 도움말 |
| 03-focus-worker-omp | worker로 focus, owner는 그대로 |
| 04-focus-host-shell | host shell 명령 실행 |
| 05b-takeover-request-already-user | 이미 user owner일 때 인수 요청 알림 |
| 05-takeover-request / 06-takeover-confirm | manager owner에서 인수 요청과 확인 |
| 07a-handoff-refused-before-wb-handoff | `wb-handoff` 없이 handoff: 보류 사유 표시 |
| 07-handoff-owner-manager | handoff 후 owner=manager |
| 08-input-refused-owner-manager | manager owner일 때 입력 거절 알림 |
| 09-paste-over-2mib-refused | 2 MiB 초과 붙여넣기 거절 |
| 10 / 10b-korean-multiline-paste | 한글 3줄 붙여넣기, Enter 전과 후 |
| 11-flood-catchup / 12-flood-after-ctrl-c | 대량 출력 중 "따라잡음" 표시와 Ctrl-C 후 |
| 13-after-detach / 14-after-reattach | detach 메시지와 재attach 후 복원 화면 |
| 15-narrow-100x30 / 16-wide-200x50 | 좁은 화면과 넓은 화면 |

## 7. 피드백 처리

- 배치·키·문구 수준의 피드백은 CW-06 수정으로 반영하고, 독립 test와 review를 다시 거친다.
- 4절의 결정이나 제품 동작·수락 기준 변경은 새 결정 기록과 계획 변경을 거친다.
- 검토 결과는 사용자 인용과 함께 이 run의 UR-UX 기록에 남긴다.
