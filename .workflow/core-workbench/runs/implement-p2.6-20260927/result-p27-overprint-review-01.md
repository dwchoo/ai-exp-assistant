# p27-overprint-review-01 (reviewer, read-only)

**Verdict: pass (integrate).** P0/P1/P2 없음, P3 3건.

- 신규 2개 테스트 파일 OK(8 / 2, UI 3회 반복 OK), tests/ui/test_product_model*.py 320 OK.
- 확인: shrink 후 커서가 항상 화면 안(drop=cursor.y+1-lines), 위 행은 순서대로 history.top(_CountingDeque pushed 증가, maxlen eviction 정상), alternate는 history 미사용, `_leave_alternate`는 using_alternate=False 설정 뒤 호출돼 primary history에 쌓임, margins 리셋, 확대/열 변경은 pyte 그대로(super().resize는 self.lines 선반영 후 열 처리만), 행 aliasing 없음, stream/DCS/OSC52/SU·SD 코드 미변경.

## Findings
- P3 screen.py:179-190: "tmux와 동일" 주장은 부정확. tmux(screen_resize_y)는 커서 아래 *빈* 행만 먼저 버리고 내용 있는 행은 위를 history로 밀어 보존한다. 여기선 내용 있는 커서 아래 행도 버린다(xterm과 유사). 재현: 6줄 L0..L5 + 커서 홈 → 3줄로 축소 시 L3..L5 소실(history 0). inline TUI 푸터가 잘리지만 SIGWINCH 뒤 앱 재그리기로 복구되므로 영향 작음. 주석/결과 문구를 "xterm식"으로 정정하거나 빈 행만 우선 제거하도록.
- P3 screen.py _resize_screen: DECSC 저장 커서 y를 drop만큼 보정하지 않음(pyte restore가 clamp만). 축소 후 ESC 8 시 위치가 어긋날 수 있음(재현: 6줄 가득 → 3줄, 저장 y=5는 clamp로 우연히 2). 드문 경로.
- P3 model.py:1679-1684: offset 유지는 기존 계약(test_scroll_positions...)과 일치하나, 축소 시 view가 보는 절대 줄이 drop만큼 앞으로 이동(같은 내용을 유지하려면 anchor 유지가 정확). 계약 선택 사항이며 테스트 고정됨, 조치 불필요.
