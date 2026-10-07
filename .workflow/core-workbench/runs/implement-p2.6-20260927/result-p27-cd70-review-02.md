# p27-cd70-review-02 result (reviewer, Opus, delta of fix-01) — 2026-10-07

Verdict: **pass** for the product code. All review-01 and test-01 items are closed as adjudicated, and no new product defect was found. There is one test-only P3: an implementer test now fails deterministically here, so the acceptance run is not green until it is fixed. Read-only; scratchpad temp only; no provider request, no process signalled, no commit, no graphify update. Files the test agent is editing (cd69 independent ×2, terminal p27c) were ignored.

Reviewed files (sha256 first 12): flow.py 44825f395d12 · flow_recovery.py 3162230c6f14 · flow_tasks.py e9e80186e08c · service.py 7d63f14d209e · bridge.ts 835158403d18 · workbench-recovery/SKILL.md 5185a2ae367c (they match result-p27-cd70-fix-01).

## Closure check
- **P2-1 — closed.** `RESTART_DETAIL` and `RESTART_PENDING_DETAIL` (`flow_recovery.py:89-97`) both say "Do not send a follow-up now: end your turn", then "exactly one follow-up" after `worker_restarted`. `RESTARTED_HINT` (:77-81) adds "if you already sent that follow-up … just end your turn". The skill (step 2, third bullet) and the `restart_worker` description (`bridge.ts:423`) agree. `_needs_resend` (`flow_tasks.py:797`) skips a re-send that is pending for the current session. `commands_already_run` is now filled in when the lane creates the message (`OutboundMessage.prepare`, `flow.py:174`, `:1098-1103`; `_commands_run_section`, `flow_tasks.py:845`), and a prepare failure is rejected with nothing created. Re-ran my review-01 reproduction: the first follow-up gets `resent_task=True`, the second gets None, **1 full re-send** (was 2).
- **P3-1 — closed.** A first TASK that ends `unknown` sets `worker_session` (`flow_tasks.py:1321-1324`), and so does an unknown re-send (`:872-873`). The new `RESEND_NOTE` (:148) is neutral and true for both causes.
- **P3-2 — closed.** `on_not_queued` is called after `_queue` returns a non-queued result (`flow.py:1018-1022`), outside the flow lock (RLock in any case). It clears `_resend_pending` and, for a first-TASK re-send, restores the earlier TASK state.
- **P3-3 — closed by the P2-1 wording.** When the new worker is not registered, the result carries `RESTART_PENDING_DETAIL` (`service.py:1118`).
- **P3-4 / O3 — closed.** The `_jobs_closed` check and the job append share `_jobs_lock` (`service.py:1085-1092`), and `_abort_restart_jobs` sets the flag under that lock (:1162-1164). A late call or a timed-out wait during shutdown answers `backend_shutdown`, never `restarting` (:1093-1097). The restart lock is released exactly once on every path.
- **Test P3-1 — closed.** Lane re-queues are counted when they happen (`_count_lane_requeue`, `flow.py:920`). `requeue_for_new_session` (:880-918) counts each re-queue once: entries moved now, plus in-flight entries marked `_requeue_expected`, plus earlier lane re-queues.
- **O1 — closed.** When `manager_recovery` is queued, any `report_delivery_unknown` still queued for a report it lists is dropped and journaled (`flow_recovery.py:359-371`). This runs only on the tick thread.
- **O2 — unchanged, as ruled.**

## Findings
- **P3-T (test race, implementer's own test, not product).** `tests/backend/test_cd70_task_resend.py:166-171` `test_no_resend_while_the_task_message_is_still_on_its_way` sets `_task_message_state[task_id]="queued"` right after `work()` returns. The TASK's own late `_finish("delivered")` event (`flow_tasks.py:1307`, after `submitted`) then writes `"sent"` back before the follow-up is decided, so a re-send is produced.
  - Failed in 2/2 full `test_*cd70*` runs (126 tests, 1 failure) and 5/5 runs alone. A traced run showed the overwrite comes from the TASK lane's `_finish`.
  - With a 1 s settle before setting the state, it passed 3/3, so the product logic (`_needs_resend` checking queued/delivering) is correct.
  - The fix-01 result reported this test green. The timing likely changed with the current fsync speed, the same environment effect fix-01 described for the cd69 tests.
  - Fix: wait for the TASK entry's terminal event (or for `outbox_entries` delivered) before setting the state.
- **P3-W (wording, minor).** `worker_restarted` is queued only when a Task is open or blocked (`flow_recovery.py:313-323`). `RESTART_DETAIL` and `RESTART_PENDING_DETAIL` promise the notice without that condition. If `restart_worker` is called with no open Task, the manager is told to wait for a notice that never comes. There is nothing to follow up in that case, so the impact is low; adding "(when a Task is open)" would make it exact.

## Other checks
- Lock order: `not_queued` and the `_resend_listener` take the flow RLock only.
- `prepare` runs on the lane thread before `create_message` and outside `_outbox_cv`. It reads the terminal journal and never calls back into the outbox.
- `_requeue_expected` can keep an id whose in-flight delivery ends some other way. The set is bounded by the number of such reports and nothing is double-counted.
- Notice dedupe: dropped queue items are never sent, and `_noticed_unknown` still blocks re-notices.

## Runs
- My P2-1 reproduction: OK (1 re-send).
- All `test_*cd70*` in tests/backend: 126 run, **1 failure** (P3-T), in both runs.
- `tests/ui test_*cd70*`: OK.
- node `handoff_tools.test.ts`: pass 18 / fail 0.
- Full suites and a real OMP smoke were not run.
