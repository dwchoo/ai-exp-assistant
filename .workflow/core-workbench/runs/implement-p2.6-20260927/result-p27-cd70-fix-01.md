# p27-cd70-fix-01 result (worker_senior, Opus) — 2026-10-07

All 8 Root rulings applied (O2 unchanged as ruled). No commit, no graphify update, no provider request, temp only under /tmp, no `*_independent*` file edited.

## Checks (final code; `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`, node `--test tests/bridge/*.test.ts`)

| suite | result | exit | failures |
|---|---|---|---|
| tests/backend | 1058 run (skip 30, xfail 1) | 1 | 2, pre-existing race (below), not C-D70 |
| tests/bridge (py) | 52 OK (skip 1) | 0 | — |
| node tests/bridge/*.test.ts | pass 43 / fail 0 | 0 | — |
| tests/terminal | 208 OK (skip 2) | 0 | — |
| tests/workflow / gates/g2_shell / contracts / ui | 58 / 142 / 52 / 819 OK | 0 | — |
| new test_cd70_fix01.py (13) | OK | 0 | — |
| all `test_*cd70*` incl. independent e2e/watchdog/skill (113) | OK (2 reruns) | 0 | — |

`test_recovery_e2e_independent_p27cd70` passes unchanged, including `test_the_recovery_notice_counts_a_report_the_outbox_already_re_sent_to_the_new_session` (test P3-1). One transient error was seen in a batch run: `IndexError` in `test_restart_mid_command…`, at the test's own `find_in_session(shell.pid, b"2.7")[0]`, which re-reads /proc after its wait. 5 further runs (module ×3, batch ×2) and the full run passed.

**Two backend failures, pre-existing and not caused by this work:** `test_role_boundary_independent_p27cd69.AnalysisTaskMessageTests.test_default_is_summary…` and `test_task_message_independent_p27cd69b` (subtests). The subtest loop cancels a Task and then at once clears `mailbox.created`. The cancel's worker notice is created by the lane after that clear, so the next subtest finds `created[0]` = the cancel QUESTION (`kind question`, `cancel: true`). A `git archive HEAD` copy (no C-D70 code) fails the same way right now: 5/5 runs with 4–10 failures. Earlier today they passed in every full run. This is a timing race in those tests that depends on how fast the journal fsync is, and it belongs to their owner.

Red→green: test_cd70_fix01 was run against a /tmp copy with each fix neutralised. 7 behavioural failures (resend once, held-at-queue ×2, unknown first TASK, shutdown race, requeue count, O1 drop). The wording tests and the prepare-time test are red only through missing symbols (`RESTART_PENDING_DETAIL`). Green on the final code.

## Item status (Root ruling → change)
- P2-1/P3-3 — done. `RESTART_DETAIL`/`RESTART_PENDING_DETAIL` (flow_recovery.py:91) say "Do not send a follow-up now: end your turn … after the worker_restarted notice send exactly one follow-up … or cancel". The pending text is also used when the new worker is not registered yet (service.py result). `RESTARTED_HINT` (:77) is the single cue, with "if you already sent that follow-up after this restart, just end your turn". The restart_worker tool description and the recovery skill now say the same. `_needs_resend` (flow_tasks.py:797) treats a re-send queued or delivering for the current session as pending, so a further follow-up is plain. `commands_already_run` is computed by `OutboundMessage.prepare` (flow.py:174) when the lane creates the message (`_commands_run_section`, flow_tasks.py:845), including after a re-queue.
- P3-1 — done. A first TASK that ended `unknown` sets `worker_session` to its session (:1323). An unknown re-send does too (:873). Neutral `RESEND_NOTE` (:148): "re-sent in full because the current worker session may not have it".
- P3-2 — done. `HandoffDecision.on_not_queued` (flow.py:194) is called when the queue step held the message. The re-send clears its pending state and, for a first-TASK re-send, restores the earlier TASK state (journaled `task_resend_not_queued`). The next follow-up re-sends again.
- P3-4/O3 — done. The job is appended under `_jobs_lock` only if `_jobs_closed` is not set (service.py:1086). `_abort_restart_jobs` sets that flag under the same lock (:1164). A late call answers `refused: backend_shutdown` at once and releases the restart lock. A timed-out wait during shutdown also answers `backend_shutdown`, never `restarting`.
- test P3-1 — done. Lane re-queues are counted when they happen (`_count_lane_requeue`, flow.py:920). `requeue_for_new_session` (:880) returns: lane re-queues not counted yet, plus entries moved now, plus in-flight old-session entries (whose later lane re-queue is not counted again). A report queued after the new session registered is not counted.
- test O1 — done. When `manager_recovery` is queued, waiting `report_delivery_unknown` notices for reports it lists are dropped (journal outcome `dropped_listed_in_manager_recovery`, flow_recovery.py:371).
- test O2 — unchanged (as ruled).

## Changed paths (sha256, first 12)
bridge.ts 835158403d18 · skills/workbench-recovery/SKILL.md 5185a2ae367c · flow.py 44825f395d12 · flow_recovery.py 3162230c6f14 · flow_tasks.py e9e80186e08c · service.py 7d63f14d209e · tests/backend/test_cd70_fix01.py 54d6369eef0f (new)

## Independent tests
None needs an update for this fix; all C-D70 independent tests pass unchanged. The two cd69 tests above fail at HEAD too, from a timing race in the tests themselves. Their owner should make them wait for the cancel notice, or clear `created` only after it.

## Open questions
None.
