# p27-cw16-c17-formal-01 — 중단 보고 (R 단계, C17)

Root 요청(C17 리뷰 block, 후보 변경)으로 중단했다. 정식 근거로 쓸 결과는 없다.
- 시도: 첫 시작은 `&`로 SIGQUIT가 무시돼 시작 직후 내가 띄운 PID만 종료하고 지웠다(`/tmp/wb-cw16-c17/aborted0`). 재시작 후 compat-c17만 약 11분 실행(비live 17건 OK 후 live outer matrix 진행 중) 중에 중단했다.
- 중단 방식: 내가 띄운 PID에만 SIGTERM/SIGINT. harness가 정리해 잔존 프로세스와 `/tmp/wbc16*`는 없다. VM은 시작하지 않았다.
- 남은 파일: `check-p27-cw16-*-c17-request.json` 6개(compat, flow-policy-shell, fault, gap, regression-a/b), 중단된 compat의 execution/log(불완전·무효), `cw16-formal-c17/**`(snap.sh, snapshots, 중단 report). snapshot은 매번 C17(db39f4aa…)과 같았고 repo 쓰기는 0이다. 새 후보에서는 request의 candidate 문구·X5 aggregate(`d95842a4…`, C17용)를 갱신하고 새 check id로 다시 만들어야 한다.
