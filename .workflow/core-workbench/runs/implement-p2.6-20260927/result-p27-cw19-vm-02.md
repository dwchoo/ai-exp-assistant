# p27-cw19-vm-02 — L-CW19-REBOOT 재실행 (수정 후보, 격리 VM 실제 재부팅, test_designer)

판정: **L-CW19-REBOOT pass** (C-AC-23 항목 전부 vm-01과 같은 절차로 재관측) + **VM F1 fixed, VM F2 fixed**.
명령·출력·exit code: `result-p27-cw19-vm-02-log.json`(`guest_events`, driver 정보). 모델·provider 요청 없음(fake OMP, fake HOME의 빈 `agent.db`, proxy 차단). 자격 증명은 읽거나 복사하지 않음. Host는 재부팅하지 않음. repo 파일 변경 없음, commit·graphify update 없음.

## VM·입력 manifest

| 항목 | 값 |
|---|---|
| VM | `wb-reboot`, QEMU pid 728713(두 번의 guest 재부팅 동안 유지), 종료 `stop.sh` rc 0, disk·overlay 파일 보존 |
| Image | `noble-base.img` sha256 `6a81c375…` = SHA256SUMS(vm-01과 동일) |
| Repo 복사 | rsync `--delete` (exclude `.git`, `.workflow`, `graphify-out`, `__pycache__`) |
| Manifest (`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`) | host 88 files aggregate `a4aebd9c…eebcad` = guest 88 files 같은 값(파일별 동일). host 88개 모두 `p27-snapshot-cw19-fix01.json`(candidate `03d6350f…`) sha256과 일치. 실행 후 host aggregate 동일(변경 없음) |
| Driver | vm-01 `driver-final.py`(sha256 `3154e010…`) 기반, 변경은 두 가지만: ROOT를 `~/cw19-reboot-02`로, post2에서 재부팅 뒤 Task를 취소하지 않고 확인 뒤까지 열어 둠(F1 양성 사례). 새 driver sha256 `3de6e82e…280ebe` |

## Boot id·타임라인 (KST)

| 사이클 | 재부팅 전 | 명령 | 재부팅 후 |
|---|---|---|---|
| 1 | `2cb5a178-a3f9-442e-a5ff-da36260d4fd2` | 13:25:15 `ssh.sh 'sync; sudo reboot'` → 13:25:24 ssh 복귀 | `ee166795-ebef-47e0-8078-d3d8815bb7a4` |
| 2 | `ee166795-…` | 13:28:52 같은 명령 → 13:29:03 복귀 | `29b6d314-c603-4efa-a6d3-16ae69f7d521` |

절차: probe(real OMP 18.2.10, fake HOME, `start --no-attach` rc 0, 격리 check는 vm-01처럼 `failed`: native addon 미추출, `shutdown --yes` rc 0 verified) → 사이클 1 `pre`(work Task dispatched, run bound, backend 켠 채로 재부팅) → `post` → 사이클 2 `pre2` → `post2` → 같은 boot 안 추가 확인 1건.

## 관측·판정 (vm-01 표와 같은 항목)

| # | 확인 | 관측 | 판정 |
|---|---|---|---|
| 1 | boot_id 변경 | 1: 2cb5a178→ee166795, 2: ee166795→29b6d314 | pass |
| 2 | 기록 복원 | 재부팅 전 디스크: `boot.json`, `tasks.sqlite3`(runs=1) 존재, 우리 process 0개. 시작 뒤 `startup.classification=reboot`, processes `ended_by_reboot`, `probed=false`, run `interrupted_by_reboot`, outbox TASK 1건 `queued_not_sent`(재전송 없음) | pass |
| 3 | 확인 대기 표시 | `boot.confirmation_required=true reason=reboot`, holds `[boot_confirmation_required]`, CLI `status`에 `부팅 확인 대기 (reboot) …`. 아래 F2 참고 | pass |
| 4 | 확인 전 자동 동작 보류 | manager `to_worker`(follow-up, 새 experiment)·worker `terminal` 모두 `held/boot_confirmation_required`(Nothing was run). 사이클 1에서 70초, 사이클 2에서 65초 대기 동안 manager·worker 알림 0건, deliver 0건, runs 1→1 | pass |
| 5 | 사용자 직접 조작 | host shell 입력 실행됨(`CW19_USER_INPUT`), pause/resume ok, manager `cancel` → `cancelled` | pass |
| 6 | `confirm-boot` 조건 표시 | tty·`--yes` 없이 rc 1, `--json` rc 1, 둘 다 `== 부팅 확인: 환경·실행 조건 ==` 표시(boot marker 기록→현재, data/project dir, host shell, OMP·격리, 자동화, Task, 재시작 대조, 보류 사유, 확인 시 허용 동작). `--yes` rc 0 `부팅 확인됨 (ee166795…)` | pass |
| 7 | 확인 뒤 허용 | `confirmation_required=false`, holds [], UI 문구 사라짐, 새 experiment `dispatched`(runs 1→2). Worker `terminal`: 확인 전 `held`; 확인 뒤는 아래 환경 관측 참고(같은 boot 재시작에서 `exited 0`, 출력 `cw19-after-confirm-closed`) | pass |
| 8 | 중복 확인 | 두 번째 `confirm-boot --yes` rc 1(출력 없음, 필요 없음) | pass |
| 9 | 종료·정리 | `shutdown --yes --json` 3회 모두 rc 0 verified true, left_running [], 우리 process 잔존 0. VM `stop.sh` rc 0 | pass |

## 수정 확인

- **VM F1 fixed** (`backend_restarted`)
  - 사이클 1(확인 전 Task 취소): 취소 직후 `startup.notice.state`가 `queued`→`dropped_task_closed`로 바뀜. 확인 뒤 23초 이상 관찰에서 manager 알림·deliver 0건. 이 base의 `notices.jsonl`이 아예 생성되지 않음(알림 0건). vm-01에서는 `status: running`으로 stale 전달됐다.
  - 사이클 2(Task 열린 채): 확인 전 65초 동안 알림 0건, notice `queued`. 확인 뒤 1회 전달(`backend_restarted`), 내용 `task.status "running"`, `held_reason backend_restarted`(전달 시점 Task 상태). 전달은 1건뿐. 이후 취소는 정상(`cancelled`).
- **VM F2 fixed** (UI 확인 대기 문구)
  - 160열: 상태 줄 둘째 줄이 `재부팅 확인 대기: python -m workbench confirm-boot (그 전에는 자동 동작 보류) | 경고 OMP 격리 failed: …`로 시작 → 격리 경고보다 앞. 사이클 1·2 모두 `has_boot_wait=True`(vm-01은 160열에서 문구가 안 보였다).
  - 400열: 확인 대기 → 격리 경고 → 재부팅 요약(`재부팅 뒤 시작 · 이전 run 중단됨(결과 불명) · 보내지 못한 메시지 1건`) 순. 확인 뒤 두 화면 모두 문구 사라짐.

## 환경 관측 (판정 영향 없음, vm-01과 같은 성격)

- 사이클 1 확인 뒤 worker `terminal`, 사이클 2 확인 뒤 worker `terminal`(Task 취소 직후)은 45초 동안 tool result가 없었다. pre2 baseline(재부팅 전, 수락 안 된 work Task가 열려 있을 때)에서도 똑같이 무응답이었고 Task가 없으면 `exited 0`이었다. 원인(fake worker 턴 진행 중 terminal 무응답)은 이번 범위에서 조사하지 않았다. 대신 같은 boot의 확인된 상태에서 backend를 다시 시작해 닫힌 Task만 있는 조건으로 terminal을 쳤고 `exited 0`을 확인했다(확인 뒤 terminal 허용의 증거). 이 확인은 세 번째 재부팅 없이 같은 boot 안에서 한 것이다.
- 격리 check `failed`와 UI의 `[종료(취소됨) · 취소 요청됨]` 이중 표시는 vm-01과 같고 CW-19 범위 밖이다.
- 이전 backend는 power loss가 아니라 systemd SIGTERM으로 정상 종료됐다(shutdown verified). 갑작스런 전원 차단은 관측하지 않았다.
- Guest에는 `~/cw19-reboot-02/`, `~/cw19-driver-02.py`, `/tmp/cw19-extra.py`가 남는다(VM disk 보존 정책). Host temp는 `/tmp/cw19-vm-02/`.
