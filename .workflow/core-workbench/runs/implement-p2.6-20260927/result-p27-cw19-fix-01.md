# p27-cw19-fix-01 — CW-19 corrections (review-01, vm-01, test-01) (worker_senior)

Base: `a12e668` working tree with the CW-19 candidate. Contract: assignment-p27-cw19-fix-01.json (Root adjudications).
No commit, no graphify update, no model/provider request (fake OMPs only), no credential read, no docs/.workflow edit
except this file, no `*_independent*` edit. Temp only under /tmp. Only my own processes were signalled (one backend of my
own interrupted `test_live_restart_entrypoint` run, pid 621342 + its panes, ended with TERM; its /tmp dir removed).

## Checks (real exit codes)

Command: `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`;
`node --test tests/bridge/*.test.ts`. Full matrix on the final code; backend and bridge re-run after the last skill
wording change (see the last two rows).

| Suite | Exit | Result |
|---|---|---|
| recovery_boot | 0 | 155 OK (incl. new `test_cw19_fix01.py` 22; both previously failing independent tests now pass unchanged) |
| backend (matrix run) | 1 | 1095, 1 failure: `test_task_message_independent_p27cd69b` needed the phrase "in full" that my skill trim had removed → fixed (`sent in full`); see re-run |
| bridge (python) | 0 | 52 OK (skip 1) |
| contracts / lifecycle / observation / storage / tasks | 0 | 52 / 38 / 95 / 33 / 22 OK |
| terminal | 0 | 221 OK (skip 2) |
| ui | 0 | 836 OK |
| workflow | 0 | 62 OK (incl. new `test_cw19_fix01_workflow.py` 4) |
| gates/g2_shell, g1_vt, g4_evidence, g4_lifetime | 0 | 142 (skip 1) / 85 / 10 / 8 OK |
| gates/harness | 5 | 0 tests (no test modules) |
| policy/pause_automation, policy/recovery_manager | 0 / 0 | 129 / 26 OK |
| integration | 1 | 6 run, 3 failures: `test_cw16_runtime_matrix` plain/tmux/herdr `inconclusive_version` (pins omp/18.2.10, installed 18.8.0) — known environment, not changed |
| gates/g3_omp | 1 | 114 run, 1 error: `test_tui_extension_fault_integration` setUpClass "Unexpected OMP version" — same known pin |
| node `tests/bridge/*.test.ts` | 0 | 48 pass |
| backend (re-run, final code) | 0 | 1095 OK (skip 30, xfail 1) |
| bridge (re-run, final code) | 0 | 52 OK (skip 1) |

Red→green: every new test was run against a /tmp copy of `src` with only the corresponding hunk reverted (tests copied
so `support.py` resolves that `src`): 18 of 22 recovery_boot tests and 3 of 4 workflow tests fail there; the four that
stay green on the revert are guard tests of unchanged behaviour (backend message still dropped under a hold, no-hold
report goes at once, overlong journal line skipped, UI shows `저장 오류`). The P2-2 terminal test fails on the revert with
`held:model_hold:worker` exactly as the review predicted.

## Per item

**Review P2-1 — worker→manager message dropped under a hold.**
`flow.py:1277` (`_deliver_once`): under a CW-19 hold of the manager (boot / metadata / `model_hold:manager` /
shutdown) a worker's own `to_manager` entry (`_worker_call_to_manager`, `flow.py:1253`: key role worker, WORKER→MANAGER)
is kept like a report (`_hold_paused`: never submitted, re-checked every retry interval) and delivered once when the hold
lifts — accepted decision 2. Pause semantics are unchanged: the independent CW-18 test
`test_cw18_flow_independent_p27w … dropped_not_resent` requires a worker message hit by a pause to be dropped
(`held_paused`); such a message (and any worker message that ends `rejected`, e.g. requeue limit) is now listed in
`workbench_status.reports` with state `not_sent` (`service.py:1595`) instead of being hidden; the status tool description
(`bridge.ts:452`) and workbench-recovery skill (line 19) say what `not_sent` means. Module docstring `flow.py:23-30`.
Tests: `WorkerToManagerWaits` (model/boot/metadata holds → kept, delivered once; pause → dropped and listed; backend
plain message still dropped), `StatusListsUnsentWorkerMessages`.
Residual (not changed, pause invariant): the worker's `queued` detail still says "delivered once"; for the pause race the
truth is only in `workbench_status`.

**Review P2-2 — tool call of the turn that lifts a model hold.**
`service.py:296` `_hold_reason`: when the answer is a `model_hold:*`, it first applies the `model_turn_result` events
already received (`_sync_model_events` :799, under `_model_lock`), then answers. The bridge appends an `omp_event` under
its lock before it accepts a later `tool_request` from the same socket (`mailbox.py` `_receive`), so the lifting event
is always seen. Side effects (`automation.model_changed` → run record) are queued (`_model_effects`) and run on the loop
in `_poll_model_events` (no lock-order risk from tool threads). Tests: `ModelHoldSyncUnit`;
`ModelHoldLiftingTurnCallsTerminal` (fake OMP sends `model-ok` then `tool terminal {"command":"true"}`, loop not ticked
in between → not held; reverted code → `held:model_hold:worker`).

**Review P3 / test P3-1 — `stop_unconfirmed` never re-observed.**
`app/recovery.py`: `Survivor.pending` (:227) keeps the exact (pid, start ticks) identities still seen alive after a stop;
`refresh` (:261) re-observes them by exact identity (`observe`), drops ended ones and marks the survivor `stopped`
(`stop.outcome`, `confirmed_at`) when none is left, so `alive()`/`carried()`/`_active_work`/`_close` verification use
the refreshed state. A live one stays `stop_unconfirmed`, is `stoppable` in the view and the manager may retry: `stop`
(:324/:337) re-checks the boot, then `_signal_pending` (:370) pins only those exact identities again (pidfd + ticks +
uid; a recycled pid is never signalled) and runs the same TERM/grace/KILL (`_terminate` :387, shared with `_signal`).
Tests: independent `test_stop_unconfirmed_survivor_that_ends_later_is_not_left_running` (now passes) + `UnconfirmedSurvivor`
(ended leaves list and record, live retried exactly, recycled pid not signalled, retry needs same boot).

**Review P3 — experiment report under a manager hold.**
`workflow/run.py:313,359`: `judge(report_hold=…)` creates the report but, when the manager is held (or the hold state is
unreadable), does not deliver it: status `deferred`, nothing submitted, no `sending`. `flow_tasks.py:1763` passes
`report_hold=_held_now(MANAGER)`; the existing deferred loop (:1789) shows the hold reason and sends it with
`retry_report` once the hold lifts (it already checked `_held_now(MANAGER)`). Tests: `JudgeReportHold` (3).

**Review P3 — `TaskWorkflow.start` raw-log store error.** `run.py:744` catches any exception of the artifact/raw-log
setup (OSError, `RuntimeError("RawLogStore is closed")`, `StoreIntegrityError`) → `fail_run(stage artifact_setup, error)`
+ `WorkflowHeld`. Test: `StartRawStoreFailure` (closed store → run not active, history names RuntimeError).

**Review P3 — journal > 64 MiB.** `app/recovery.py:469` `iter_jsonl` streams the whole journal line by line (1 MiB line
cap, O_NOFOLLOW); `lost_outbox` (:525) takes a stream, resets at each `backend_start`, forgets entries at their terminal
state and tracks at most 4096 open entries (bounded memory); the reconcile uses it (:697). `read_jsonl` (:496) now reads
the last 64 MiB (recent records) instead of the first. Tests: `LargeJournal` (70 MiB journal: entry after the last marker
listed; tail read; overlong line skipped).

**Review P3 — texts.** `omp_bridge/skills/to-worker/SKILL.md:51`: `held:metadata_unavailable`,
`held:boot_confirmation_required` (user runs `python -m workbench confirm-boot`), `held:model_hold:worker` (lifts on the
worker's next clean turn), `held:shutdown_closing`; tell the user once, do not loop. `held:backend_restarted` (:52) now
points to the `backend_restarted` notice / workbench-recovery (matches `BACKEND_RESTARTED_HINT`). The skill stays under
the existing size tests (69 lines, 10 994 chars < 11 000) by trimming wording elsewhere without changing meaning; the
hold reasons are named in `flow.py:1093` (skill↔code test). `held()` results were not given a `detail` (an independent
test pins the exact dict). `reconcile_failed`: UI `BOOT_RECONCILE_FAILED_TEXT` (`ui/product/model.py:135`,
`boot_wait_text` :2029) "재시작 대조 실패 확인 대기: python -m workbench confirm-boot …"; CLI `status` (`cli.py:171`)
"재시작 대조 실패: 이전 backend의 process·Task·메시지 상태를 확인하지 못했습니다 …". Tests: `RecoveryTexts`.

**VM F1 — stale `backend_restarted` notice.** `flow_recovery.py:208,553`: a queued notice may carry `build`; right
before each attempt the fields are rebuilt from the current state, and `None` drops it unsent (journal outcome
`dropped_not_applicable`). `service.py:1707,1715` `_restart_notice_fields(task_id)`: current Task view; None when that
Task is closed or another Task is open (`startup.notice.state = dropped_task_closed`). Falls back to the old call when a
watchdog double has no `build` keyword (independent test double). Tests: `RestartNoticeBuiltWhenSent` (3).

**VM F2 — boot wait hidden at 160 columns.** `ui/product/model.py:1983`: `status_lines` puts the boot-confirmation
text first (before the OMP isolation warning); it is not repeated in the recovery part. Test: line 2 starts with
`BOOT_WAIT_TEXT` with a 120-char isolation warning present.

**Test P3-2 — pause store failure only logged.** `backend/automation.py:350` `_save_pause`: a failed save sets
`persistence_error` (`pause_not_stored`/`resume_not_stored`, published at :1074, detail "… not stored durably; retried"),
reports `metadata("automation_pause", OSError)` → metadata hold + `faults.metadata` (UI `저장 오류` /
"metadata 저장 장애", CLI status); the in-memory pause stays. `retry_pause_store` (:384) re-saves the latest wanted
value every 5 s from the loop (:701); success clears the error and reports the metadata success. `service.py:316`: another
durable write does not clear the `automation_pause` latch (the pause is still not stored); wired at `_open`
(`metadata=self._metadata_event`). Tests: independent `test_pause_that_cannot_be_stored_is_shown` (now passes) +
`PauseStoreFault` (3).

## Invariants kept
Kill only exact verified identities (retry pins pid+ticks+uid, same boot); no replay (kept messages and deferred reports
were never submitted); pause semantics unchanged (C-D65 drop on pause, now listed); C-AC-22 shutdown verification reads
re-observed survivors. Independent tests unchanged and green except the two known OMP-version environment failures.

## Changed paths
src: `app/recovery.py`, `backend/{automation,cli,flow,flow_recovery,flow_tasks,service}.py`, `ui/product/model.py`,
`workflow/run.py`; `omp_bridge/g3/bridge.ts` (status description), `omp_bridge/skills/to-worker/SKILL.md`,
`omp_bridge/skills/workbench-recovery/SKILL.md`. Tests (new): `tests/recovery_boot/test_cw19_fix01.py`,
`tests/workflow/test_cw19_fix01_workflow.py`.
