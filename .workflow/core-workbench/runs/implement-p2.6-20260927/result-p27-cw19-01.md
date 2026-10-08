# p27-cw19-01 — CW-19 U1–U3 implementation (worker_senior)

Base: `a12e668` working tree (polish integrated). Inputs: assignment-p27-cw19-01.json, result-p27-cw19-design-01.md,
tickets/CW-19.md, DECISIONS C-D71 (+ C-D55/56/58/62/63/70). No commit, no graphify update, no real model/provider
request (fake OMPs only), no credential read, temp only under /tmp, only own processes signalled.

## Checks (real exit codes)

Command: `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`
(policy run per sub-dir: the top-level discover finds 0 tests, exit 5), node `node --test tests/bridge/*.test.ts`.

| Suite | Exit | Result |
|---|---|---|
| recovery_boot (new; incl. real-process entrypoint SIGKILL/restart/boot-change test) | 0 | 52 OK |
| backend | 0 | 1095 OK (skipped 30, expected failure 1) |
| ui | 0 | 830 OK (re-run after the held-code fix; first run 1 failure, see below) |
| terminal | 0 | 221 OK (skipped 2) |
| workflow | 0 | 58 OK |
| gates/g2_shell | 0 | 142 OK (skipped 1) |
| contracts | 0 | 52 OK |
| lifecycle / storage / tasks / observation | 0 / 0 / 0 / 0 | 38 / 33 / 22 / 95 OK |
| policy/pause_automation, policy/recovery_manager | 0 / 0 | 129 / 26 OK |
| bridge (python) | **1** | 52 run, 1 failure: `test_cw18_schema_independent_p27w.py:63` (independent; expects no `stop_survivor`) |
| node `tests/bridge/*.test.ts` | **1** | 44 pass, 2 fail: `bridge_recovery_independent_p27cd70.test.ts:88`, `bridge_terminal_independent_p27cd68.test.ts:89` (independent; same cause) |

All three red items are independent tests asserting the pre-C-D71 manager tool set; C-D71 (1) requires the new
`stop_survivor` tool. Not edited (prohibited path); they need the one-name update listed below. Everything else green.
First ui run failure `test_cw18_ui_independent_p27w.py:80` (held code `backend_restarted` translated) was fixed by
keeping that code shown as is.

## Status per unit

### U1 — boot marker, start-up reconcile, admission hold, durable pause, survivors (done)
- `src/workbench/backend/boot.py` (new): `read_boot_id`, `classify` (:80; fresh / same_boot_clean_stop /
  same_boot_unverified_stop / same_boot_crash / reboot / boot_unknown), `BootStore` (:102) `<data>/boot.json` 0600
  atomic+fsync: `begin` (:152) persists *pending* before anything is served (reboot, unreadable marker,
  unreadable/unsafe record, pending from an earlier start all fail closed); `confirm` (:198) writes first, then the
  caller lifts the hold; `rewrite` after a failed write.
- `src/workbench/app/recovery.py` (new): `StartupReconciler` (:511) — reads previous `backend.json` (kept as
  `backend.prev.json`), classifies, probes previous refs **only on the same boot** (backend, OMPs, host shell,
  supervisor, experiment main process via new `child_start_ticks`, survivors carried from an earlier start) with
  pid+start ticks+uid; other/unknown boot → `ended_by_reboot` / `not_probed_boot_unknown` without any /proc read (R2);
  lists live members of previous pane sessions; lists outbox messages after the last `backend_start` marker that have
  no terminal state (`queued_not_sent` / `submitted_outcome_unknown`, R9); names the bound run from
  `lifecycle.json` (`outcome_unknown` / `interrupted_by_reboot`, never success); writes `startup.json`. Never
  signals or resends. `AdmissionHold` (:77), `SurvivorRegistry.stop` (:291), `PauseStore` (:471), `lost_outbox` (:437).
- `service.py`: `run()` calls `_reconcile_startup` (:235) right after the instance lock, before `_open` (no pane,
  TaskFlow load, UI socket or record overwrite yet); a reconcile exception fails closed (`reconcile_failed`, boot hold).
  `_hold_reason` (:289) threads into HandoffService, TaskFlow, TerminalService, AutomationController, `_notice`
  (:1222 — held/paused notices answer `paused`, so the watchdog/terminal retry and never send twice).
  `confirm_boot` (:967) exact current boot, durable first (`boot_confirmation_not_saved` keeps the hold).
  `_write_record` (:690) fail-soft (R3) → metadata hold; retried every 5 s from the loop; a later durable write
  reopens admission. Snapshot (additive, documented in `contracts/ui_v1.py`): `boot`, `startup` (:743), `holds`,
  `faults`. `_queue_restart_notice` (:1645): one `backend_restarted` notice (Task, classification, run, survivors,
  outbox count) through the watchdog queue. `_stop_survivor` (:1422) manager tool; `workbench_status` gains
  `backend` (startup, survivors, holds). `_active_work` lists `previous_survivor` and `left_session` (setsid'd
  host-shell descendants); `_close` (:599) sets `shutdown_closing`, reports `left_running`, `previous_survivors`,
  `problems` and `verified=false` while any of them lives (C-AC-22).
- Hold points: `flow.py:1091` (to_worker, cancel allowed), `flow.py:1263` (outbox lane: kept message waits, plain one
  held, never submitted), `flow.py:1102` (task metadata fault → `held:metadata_unavailable`); `flow_tasks.py:1189`
  (`_held_now`) at free-work start, experiment start, typing hold, staged analysis, report retry;
  `flow_terminal.py:991/1007/1036` (worker `terminal` → `held:<reason>`); `automation.py` tick and 60 s review.
- Durable pause (R4): `automation.py:342` `_save_pause`; restored at construction, saved on pause and before the flag
  clears on a reconciled resume.
- CLI `cli.py:334` `confirm-boot [--yes] [--json]`: prints boot ids, data/project dir, shell, OMP + isolation,
  automation/pause, Task, worktrees, reconcile/survivors/holds; refuses without tty/`--yes`, exit≠0 when nothing is
  pending, marker unreadable or refused. `status` prints the same recovery block (`_print_recovery` :167);
  `shutdown` prints `종료 확인 실패` and exits 1 when not verified.
- UI `ui/product/model.py:2024` `backend_recovery_text`: 재부팅 확인 대기(`python -m workbench confirm-boot`),
  재시작 분류·run 결과 불명·보내지 못한 메시지, 남은 이전 process 수, 모델/metadata/raw log 보류 (Korean).
- Bridge/skill: `stop_survivor` tool (`bridge.ts:458`, manager only, strict), `backend_restarted` notice type
  (`bridge.ts:350`), to_worker/workbench_status descriptions; `flow_recovery.py` constants, `BACKEND_RESTARTED_HINT`,
  `validate_stop_survivor_arguments`, `Watchdog.enqueue_notice`; skill `workbench-recovery` two lines.

### U2 — RecoveryPort, termination observation, stop callbacks, full shutdown (done)
- `policy/recovery_manager/port_sqlite.py` (new): `SqliteRecoveryPort.read` (:149) builds `RecoveryFacts` from
  TaskRepository public getters (TaskSpec, last scope approval, current run, revocations, settings revision,
  all runs as initial/normal_improvement) + live `RunFacts`; the run's own recorded `approval_hash` vs now; any gap
  raises (coordinator fails closed). `reserve_recovery` (:205) BEGIN IMMEDIATE CAS in `workflow/recovery.sqlite3`
  (0600), UNIQUE decision id, stale count/authority/settings/limit → False. TaskFlow's re-run limit untouched (R7).
- `automation.py`: `_NoRecoveryFacts`/`_refuse_stop` removed; `bind_production` gets
  `RecoveryCoordinator(SqliteRecoveryPort(...))` (:525), `termination=_observation` (:532, monotonic sequence,
  record + shell lifecycle; free work: worker OMP liveness), `_run_facts` (:567), `_normal_stop` (:607; pidfd +
  start-tick recheck; shell HUP/TERM/CONT, OMP TERM), `_force` (:616), `_drain`, `shutdown_bound` (:626).
- Full shutdown: `_close` calls `shutdown_bound()` (durable at-most-once fence `admission_closed/shutdown_confirmed`)
  for a user-confirmed shutdown before panes close; result `lifecycle` + `user_confirmed`; `verified` stays the exact
  process evidence (plus survivors / left-session processes).

### U3 — model error, metadata failure, raw log (done)
- `bridge.ts:368` `modelTurnOutcome` + `model_turn_result` event (`ok`, `stopReason` class only, never text;
  aborted sends nothing). `mailbox.py:599` `events_after`. `service.py:764` `_poll_model_events`: error →
  `model_hold:<role>` (+ lifecycle `model_failed` for a bound worker run), next error-free assistant message →
  lifted (+ `recheck_model`, passive `model_ok`); no probe request.
- Metadata: HandoffService journal / task metadata (sqlite3.Error, OSError) / flow ledger / backend & boot records →
  `_metadata_event` (:294) → `MetadataAdmissionGate` + `metadata_unavailable` hold; durable success reopens.
  `AutomationState.metadataHealthy` now follows it.
- Raw log (C-D71 (5)): `TaskWorkflow(raw_store=RawLogStore)`; the run's raw log is `<raw-logs>/<run_id>.log`
  (64 MiB run / 512 MiB project, `cap_source`, missing bytes in `raw_log_status`); `WorkflowRun._drain`
  (`run.py:217`) never raises (store or file failure → missing bytes, collection continues); judgment adds an unknown
  for an incomplete log and is `indeterminate`/`raw_log_incomplete` (`run.py:430`) when the marker is not in the
  stored part. Worker terminal logs unchanged (64 MiB/command). UI/CLI text from `faults.raw_log`.

## Runtime observations (real processes, fake OMPs)
- After SIGKILL of the backend the host shell (bash, PersistentShell) **survived** its PTY hangup together with a
  `nohup` job in its session; a SIGHUP-ignoring OMP stand-in survived; the worker stand-in ended. Survivor identities
  were verified and listed; nothing was signalled until the manager's `stop_survivor`.

## Decisions taken where design/C-D71 left room
1. `backend_restarted` is sent for every non-fresh start with an open Task (also after a clean stop and after a
   confirmed reboot), once, never while paused/held/before confirm-boot.
2. Boot/metadata holds apply to `to_worker` (cancel still allowed), outbox deliveries to the held role, worker
   `terminal`, all notices, 60 s review, experiment/free-work starts and staged analysis. A worker's `to_manager`
   call is not refused (it follows a user prompt); its delivery waits in the outbox while the manager is held.
3. Model hold: `stopReason=error` or an error message → hold; `aborted` (user/pause abort) neither holds nor lifts;
   stop/length/toolUse without error lifts.
4. Session members of a previous pane are stoppable only while their recorded session leader still lives with its
   recorded identity; otherwise shown only. A survivor leading its own session is stopped with its pinned members.
5. A live previous survivor or a setsid'd host-shell descendant makes the shutdown `verified=false` (exit 1); they
   are listed, never signalled by the shutdown.
6. No TaskRepository authority revocation on full shutdown (it would permanently invalidate the Task revision);
   the lifecycle fence + exact process evidence are used instead.
7. Any successful durable write in the data dir reopens metadata admission; a persisting fault latches again on the
   next attempt (no half-dispatch).
8. Unreadable `automation.json` → paused (fail closed).

## Independent tests needing updates (not edited: `*_independent*`)
- `tests/bridge/bridge_recovery_independent_p27cd70.test.ts:88` and `tests/bridge/bridge_terminal_independent_p27cd68.test.ts:89`
  assert the manager tool set exactly `[restart_worker, to_worker, workbench_status]`; C-D71 (1) adds `stop_survivor`.
- `tests/bridge/test_cw18_schema_independent_p27w.py:63` (same: manager tool names; needs `stop_survivor`).
  (Non-independent `handoff_tools.test.ts` updated.)
- `tests/backend/test_cd70_restart_worker.py` (non-independent) updated for the new `backend` key of workbench_status.

## Open questions / not done here
- U4 runtime items remain for the independent test_designer: L-CW19-RESTART with real OMP 18.2.10 (does a real OMP
  survive the hangup; is a surviving old OMP's bridge reconnect with the stale token rejected — expected by design,
  not observed here), and L-CW19-REBOOT in the VM (pending; not run).
- The 512 MiB project raw-log cap has no retention: once full, later runs store 0 bytes (`cap_source: project`)
  until logs are removed (CW-14 territory).
- `supervisor` ref was absent in the observed record (pane had none at that time); probed when present.

## Changed paths (this invocation)
src: `app/recovery.py` (new), `backend/boot.py` (new), `policy/recovery_manager/port_sqlite.py` (new),
`backend/{service,automation,cli,flow,flow_tasks,flow_terminal,flow_recovery}.py`, `workflow/run.py`,
`contracts/ui_v1.py`, `ui/product/model.py`, `ipc/bridge_g3/mailbox.py`; `omp_bridge/g3/bridge.ts`,
`omp_bridge/skills/workbench-recovery/SKILL.md`. tests: `tests/recovery_boot/**` (new: support, fake_omp,
test_boot_reconcile, test_survivor_stop, test_backend_restart, test_recovery_port, test_faults_holds,
test_live_restart_entrypoint), `tests/ui/test_product_model_cw19.py`, `tests/bridge/cw19_bridge.test.ts` (new),
`tests/backend/test_cd70_restart_worker.py`, `tests/bridge/handoff_tools.test.ts`. The other modified test files in
the working tree (`*_independent*`, `tests/terminal/test_manual_input_target.py`) are the concurrent agent's, not mine.
No residual process or /tmp dir from these tests (checked after the runs).
