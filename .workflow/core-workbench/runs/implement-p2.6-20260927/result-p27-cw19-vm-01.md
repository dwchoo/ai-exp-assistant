# p27-cw19-vm-01 — L-CW19-REBOOT (격리 VM 실제 재부팅, test_designer)

판정: **L-CW19-REBOOT pass** (C-AC-23 항목 전부 관측). 별도 finding 2건(F1 stale `backend_restarted` 알림, F2 UI 확인 대기 문구 잘림)과 환경 관측 2건.
상세 명령·출력·exit code: `result-p27-cw19-vm-01-log.json` (`guest_events`, driver source 포함). 모델·provider 요청 없음(fake OMP, fake HOME의 빈 `agent.db`, proxy 차단). 자격 증명은 읽거나 복사하지 않음. Host는 재부팅하지 않음. repo 파일 변경 없음.

## VM·입력 manifest

| 항목 | 값 |
|---|---|
| VM | `wb-reboot` (`~/.local/share/wb-vm/wb-reboot`), QEMU pid 466967 (11:08:28 KST 시작, 두 번의 guest 재부팅 동안 같은 pid 유지), 종료 `stop.sh` rc 0, overlay·disk 파일 보존 |
| Image | `noble-base.img` sha256 `6a81c375…5bb4d2` = SHA256SUMS `noble-server-cloudimg-amd64.img` |
| Guest | Ubuntu 24.04.5 LTS, kernel 6.8.0-142, bash 5.2.21, Python 3.12.3 `~/wb-venv` (pyte 0.8.2, wcwidth 0.8.4), `omp/18.2.10` |
| Repo 복사 | rsync `--delete` (exclude `.git`, `.workflow`, `graphify-out`, `__pycache__`) → `~/ai-exp-assistant` |
| Manifest (`src/**`, `omp_bridge/**`, `tests/recovery_boot/**`) | host 82 files aggregate `d94bbfc8…cd4e01` = guest 82 files `d94bbfc8…cd4e01` (파일별 동일). host 82개 모두 `p27-snapshot-cw19-candidate.json`(f7091db8…) sha256과 일치 |
| 실행 후 host | src·omp_bridge 변경 없음. 동시 test_designer가 추가한 `tests/recovery_boot/*_independent_p27cw19.py` 3개만 늘어남 |

## Boot id·타임라인 (KST)

| 사이클 | 재부팅 전 boot_id | 명령 | 재부팅 후 boot_id |
|---|---|---|---|
| 1 (주 시나리오) | `7a43f792-6d7e-46fb-8267-1d0c998184cb` | 11:11:42 `ssh.sh 'sync; sudo reboot'` → 11:11:55 ssh 복귀 | `5e453ecf-698c-4310-9215-79abd17a894a` |
| 2 (UI 넓은 화면·terminal 대조) | `5e453ecf-…` | 11:16:49 같은 명령 → 11:17:03 복귀 | `21b40b93-7704-4534-89a7-81b1549f83e2` |

## 절차 (guest, 진입점 `python -m workbench … --data-dir ~/cw19-reboot/<base>/home/wbdata`)

0. Real OMP 확인(probe): `start --no-attach --omp ~/.local/bin/omp`(fake HOME, 빈 agent.db) → rc 0, phase ready, 두 bridge session 등록. 단 OMP isolation check는 `failed`(fake HOME에 native addon 미추출로 `get_state` 전 종료). Prompt 없이 모델 tool call을 만들 수 없으므로 본 시나리오는 구현자 테스트의 fake OMP(`tests/recovery_boot/fake_omp.py`) 경로를 사용. `shutdown --yes` rc 0, verified true.
1. 재부팅 전: git project 생성 → `start --no-attach --omp <fake>` rc 0 → host shell `nohup sleep 3000 &` → manager `to_worker` work → `dispatched` Task `7c4f80a9…`, run `3517d9eb…` bound(runs=1), TASK는 fake worker가 받지 않음. backend를 켜 둔 채로 재부팅.
2. 재부팅 중 systemd SIGTERM으로 이전 backend가 정상 종료(`leaving loop (signal=15, shutdown=False)`, `user_confirmed False`). 재부팅 후 우리 process 0개.
3. 재부팅 뒤 `start` → 아래 확인 → `confirm-boot`(없이/`--json`/`--yes`) → 확인 뒤 동작 → `confirm-boot --yes` 재실행 → `shutdown --yes --json`.

## 관측·판정

| # | 확인 | 관측 | 판정 |
|---|---|---|---|
| 1 | boot_id 변경 | 1: 7a43f792→5e453ecf, 2: 5e453ecf→21b40b93 (`changed: true`) | pass |
| 2 | 기록 복원 | 시작 전 디스크: `boot.json` recorded 7a43f792, `tasks.sqlite3` runs=1, `backend.json`/`lifecycle.json` 존재. 시작 뒤 snapshot: `startup.classification=reboot`, processes 전부 `ended_by_reboot`, `probed=false`, survivors [], Task `7c4f80a9` 복원(`held_reason backend_restarted`), run `3517d9eb` `interrupted_by_reboot`, outbox TASK 1건 `queued_not_sent`(재전송 없음) | pass |
| 3 | 확인 대기 표시 | snapshot `boot.confirmation_required=true reason=reboot`, holds `[boot_confirmation_required]`. `status` 텍스트에 `부팅 확인 대기 … confirm-boot` 표시. Product UI(pyte 캡처) 160열: 문구가 OMP 격리 경고 뒤로 밀려 보이지 않음. 400열: `재부팅 확인 대기: python -m workbench confirm-boot (그 전에는 자동 동작 보류) · 재부팅 뒤 시작 · 이전 run 중단됨(결과 불명) · 보내지 못한 메시지 1건` 표시 → F2 | pass(F2 있음) |
| 4 | 확인 전 자동 동작 보류 | manager `to_worker` follow-up → `held/boot_confirmation_required`; 새 `experiment` → `held/boot_confirmation_required`; worker `terminal` → `held/boot_confirmation_required`(Nothing was run). 70초(60초 점검 주기 초과) 동안 manager·worker 알림 0건, deliver 0건, runs 1→1 | pass |
| 5 | 사용자 직접 조작 허용 | host shell 입력 실행됨(`CW19_USER_INPUT` 파일 생성), pause/resume ok, status ok, manager `cancel` → `cancelled` | pass |
| 6 | `confirm-boot` 조건 표시 | tty·`--yes` 없이 rc 1(거부)·`--json` rc 1. 출력 `== 부팅 확인: 환경·실행 조건 ==`: boot marker 기록→현재(사유 reboot), data dir, project dir, host shell(bash, manual_prompt), OMP 버전·격리 확인 결과, 자동화, Task, 재시작 대조(run interrupted_by_reboot, 재실행·재전송 없음, 보내지 못한 메시지 1건), 보류 사유, 확인 시 허용될 자동 동작 목록. `--yes` rc 0 `부팅 확인됨 (5e453ecf…)` | pass |
| 7 | 확인 뒤 허용 | `confirmation_required=false`, `confirmed_boot_id`=현재, holds [], UI 문구 사라짐. 새 experiment `to_worker` → `dispatched` Task `3bd03903…`, runs 1→2. (사이클 2) worker `terminal`: 확인 전 `held`, 확인 뒤 `exited 0` 출력 `cw19-after-confirm-2`. `backend_restarted` 알림은 확인 뒤에만 manager에 1회 | pass |
| 8 | 중복 확인 | 두 번째 `confirm-boot --yes` rc 1 `부팅 확인이 필요하지 않습니다` | pass |
| 9 | 종료·정리 | `shutdown --yes --json` rc 0, verified true, left_running []. 우리 process 잔존 0. VM `stop.sh` rc 0 | pass |

## Findings

- **F1 (minor, 재현 2회)**: 재부팅 뒤 시작 시점에 `backend_restarted` 알림이 Task snapshot과 함께 큐에 들어가고(`service.py:1645` `_queue_restart_notice`, 시작 시점 `task_view()`), 확인 전 Task가 취소돼도 확인 뒤 그대로 전달된다. 전달된 알림의 내용은 `task.status: "running"`이고, instruction은 "The open Task was not cancelled… continue the Task"이다. 이때 실제 Task는 `closed`(취소)다. C-D71 (2)는 "열린 Task가 있으면"을 조건으로 하므로 전달 시점에 Task가 닫혔으면 보내지 않거나 내용을 갱신해야 한다. 같은 boot에서 pause 중 취소한 경우에도 같을 것으로 보이나 관측하지 않았다.
- **F2 (minor, UI)**: `status_lines` line2는 `isolation_warning` → `recovery_text`(재부팅 확인 대기) 순으로 이어진다. OMP 격리 경고가 있으면 160열에서 확인 대기 문구가 화면 밖으로 잘린다. 이번 VM에서는 fake HOME 때문에 격리 경고가 났지만 real OMP probe에서도 같은 경고가 났다. 안전 관련 대기 표시가 다른 경고에 가려지지 않게 우선순위 조정이 필요하다. CLI `status`·`confirm-boot`에는 항상 표시된다.

## 환경 관측 (재부팅과 무관하거나 판정 영향 없음)

- 같은 boot 기준선(사이클 2 재부팅 전): Task가 없으면 worker `terminal` → `exited 0`. fake worker가 받지 않은 열린 work Task가 있으면 45초 동안 tool result가 없었다(host shell에 `wb-handoff`). 사이클 1의 확인 뒤 `terminal` 무응답도 같은 조건(수락 안 된 experiment Task)이었다. 그래서 확인 뒤 terminal 허용 여부는 사이클 2(Task 취소 뒤)로 판정했다. 수락 안 된 Task가 있을 때 terminal이 응답하지 않는 원인은 이번 범위에서 조사하지 않았다.
- UI task_text가 취소 완료 뒤 `[종료(취소됨) · 취소 요청됨]`처럼 두 상태를 함께 보인다(`cancel_requested` 조건이 closed를 제외하지 않음). CW-19 범위 밖이다.
- 이전 backend는 power loss가 아니라 systemd SIGTERM으로 정상 종료(verified true)됐다. 그래도 boot marker 우선으로 `reboot` 판정이 났다. 갑작스런 전원 차단(QEMU `system_reset`)은 관측하지 않았다.
- Guest에는 `~/cw19-reboot/`(data dir 3개, log.jsonl, state)와 `~/cw19-driver.py`, `/tmp/cw19-manifest.py`가 남아 있다(VM disk 보존 정책에 따름). Host temp는 `/tmp/cw19-vm-01/`.

## CW-16 I-FAULT 재사용 절차

`start.sh` → rsync(위 exclude) 뒤 manifest 대조 → guest `CW19_BASE=<name> ~/wb-venv/bin/python ~/cw19-driver.py pre|pre2` → `ssh.sh 'sync; sudo reboot'` → ssh 복귀 대기 → `… post|post2` → `stop.sh`. Driver 원문은 log JSON의 `driver.source`(sha256 `3154e010…f27c0`)에 있다.
