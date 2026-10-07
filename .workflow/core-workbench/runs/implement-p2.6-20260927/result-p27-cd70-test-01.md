# p27-cd70-test-01 result (test_designer, Opus) — 2026-10-07

Independent C-D70 tests, derived from DECISIONS.md C-D70, the p27-cd70-01 assignment and the p27-cd70-02 root rulings. They were not copied from the implementer's tests. No real model or provider request was made. No commit, no graphify update, no edits under src/omp_bridge/docs. All temp files were under /tmp. Only PIDs this run started were signalled (exact pidfd + start time; the decoy and CPU burners were killed by PID).

## Checks (final code; `PYTHONPATH=src /tmp/cw02-g1-venv/bin/python -m unittest discover -s tests/<suite> -t tests/<suite> -p 'test_*.py'`; `node --test tests/bridge/*.test.ts`)

| suite | result | exit | failures |
|---|---|---|---|
| tests/backend | 1045 run (skip 30, xfail 1) | **1** | 1 = finding P3-1 (deterministic, intended red) |
| tests/bridge (py) | 52 OK (skip 1) | 0 | — |
| node tests/bridge/*.test.ts | pass 43 / fail 0 | 0 | — |
| tests/terminal | 208 OK (skip 2) | 0 | — |
| tests/workflow | 58 OK | 0 | — |
| tests/gates/g2_shell | 142 OK (skip 1) | 0 | — |
| tests/contracts | 52 OK | 0 | — |
| tests/ui | 819 OK | 0 | — |
| new test_watchdog_independent_p27cd70 (30) | OK | 0 | — |
| new test_recovery_e2e_independent_p27cd70 (15) | 14 OK, 1 FAIL ×3 runs | 1 | P3-1 |
| new test_recovery_skill_independent_p27cd70 (5) | OK | 0 | — |
| new bridge_recovery_independent_p27cd70.test.ts (4) | OK | 0 | — |
| flaky sh signal-mask (`test_review_fixes_independent_p27c.DefaultPipeXfszTests`) | isolated 5/5 OK; under load (16 CPU burners, 8 cores) 2/5 OK; **HEAD copy under load 3/5 OK** | — | pre-existing, not C-D70 |

The first full backend run (exit 1, errors=1) showed a harness bug in my own test: `bridge.peer()` raised while the manager was reconnecting. I fixed it with a `peer_pid` helper and re-ran the suite, which gives the row above.

## Findings

### P3-1 `manager_recovery.reports_resent` under-counts a report the outbox already re-sent
- Decision: C-D70 (4)/(5) and assignment (5) say the notice carries the counts of re-sent and unknown reports.
- Observed: when the manager OMP becomes a new session, the outbox lane re-addresses the never-submitted worker report and delivers it to the new session (`_requeue` on BridgeBoundMismatch, or at `_create`). If that happens before the watchdog's next tick sees the new session, `HandoffService.requeue_for_new_session` (flow.py:871) counts only entries that are still pending or delivering. The notice then says `reports_resent: 0` while the manager does receive the report. In production this is a race between the 1 s watchdog tick and the outbox retry. In the e2e "held report" test the report did reach the new session but the count was 0 there too.
- Repro (deterministic: the watchdog thread is closed and ticked by hand after the lane re-sent): `tests/backend/test_recovery_e2e_independent_p27cd70.py::ManagerRecoveryTests::test_the_recovery_notice_counts_a_report_the_outbox_already_re_sent_to_the_new_session` gives `0 != 1`.
- Impact: the manager is told "0 re-sent" but still gets the report. No content is lost or duplicated, so this is wording/accounting only.
- Hint: count entries whose `requeued` rose for this session change (or that the lane already moved to the current manager session), not only those still pending.

### Observations (not findings; root to judge)
- O1: a `report_delivery_unknown` notice deferred by the old manager session stays in the watchdog queue. The new session gets it after `manager_recovery`, which already lists the same report, so the report is mentioned twice in different notices. This is not a duplicate notice_id, and the order is not decided.
- O2: `report_delivery_unknown` is sent only for never-submitted reports (`submitted` false). A report the API accepted whose processing outcome is unknown gets no notice. This fits the decision's "arrival unconfirmed" reading, but the wording could be read more broadly.
- O3 (not reproduced): `_restart_worker` checks `_shutting_down()` before it queues the job. A shutdown landing between that check and `_restart_jobs.append` (after `_abort_restart_jobs` ran) would make the call wait 8 s and answer `restarting`. The window is microseconds and the bridge is closed by then.
- Flaky terminal test: the failures are `SigBlk = 0xFFFFFFFE7FFEFEFF` (almost all signals blocked) for the shell, user_child, bg, dfork or experiment probes. The probe reads `/proc/$$/status` of a live bash/dash, and those shells block every signal for a moment around fork, so under CPU load the sample can catch that moment. `SigIgn` (the real disposition) is always correct. The code under test (`src/workbench/terminal/**`) is unchanged by C-D70: `git diff HEAD` is empty there, and the test imports only shell_g2 and shell_persistent. A `git archive HEAD` copy failed 2/5 under the same load, so this is a pre-existing test-design race, not a product defect. Remedy belongs to that test's owner (sample several times, or require `SigBlk` clean in at least one sample).

## What the new independent tests cover (decision → test)
- Watchdog (`test_watchdog_independent_p27cd70.py`, real `Watchdog`, fake clock and ports):
  - Each blocking condition alone keeps it silent for 600 s: worker busy, pending, approval, in-flight tool, unknown probe, worker terminal running, terminal notice pending, outbox busy, paused, no Task, held Task, experiment Task, TASK not in the current session, worker disconnected. A raising port also counts as not idle.
  - Timing: no check before 60 s of continuous idleness; a busy/terminal/outbox/pause blip restarts the 60 s; a turn event restarts it.
  - Limits: exactly 2 checks, then exactly 1 `worker_stalled` 60 s after the 2nd check, never repeated over 20 min. Probes run at most every 5 s.
  - Reset: terminal, to_manager and manager_follow_up (Q2) each re-arm the checks and the stalled notice; a text turn alone does not.
  - Races: a `worker_acted` that lands during an in-flight notify does not count that check; worker_acted is hammered concurrently with ticks.
  - Delivery: deferred/paused/not_connected retries reuse the same notice_id and count once; unknown/rejected are never resent; a deferred manager notice keeps one id.
  - Reports: an unknown report is told once without content; the editor wait is null before 30 s, set after, cleared on delivery.
  - Sessions: the first registration is not a restart; a reconnect of the same session is not a restart; a new worker session gives 1 notice per restart; `manager_recovery` comes first after the switch and lists unknown reports without content.
  - Thread lifetime: no thread is left and nothing is sent after close; a flaky port never kills the thread.
- Real backend + fake OMPs on real PTYs and the real bridge and host bash (`test_recovery_e2e_independent_p27cd70.py`):
  - `restart_worker` ends the worker OMP and its in-session child only. The manager OMP and its child, the host shell, a running host command and a same-executable decoy are untouched. Generation +1, exactly one new worker. The reason/requester/time appear in the restarts, the backend record and the product UI status line.
  - A worker that ignores TERM is KILLed after the grace, within 10 s. The worker cannot call `restart_worker` or `workbench_status`. Invalid reasons restart nothing.
  - 3 concurrent calls give exactly 1 restart and the rest `restart_in_progress`. A user restart during the job is refused. `restart_worker` during the isolation re-check of a user restart is refused, then allowed. A shutdown during the grace spawns no worker and releases the lock.
  - Full flow: TASK reaches the worker → the worker runs a listed command → `restart_worker` mid-command → the command keeps running → `worker_restarted` ×1 and `worker_terminal_done` ×1 to the manager, with no output → nothing goes to the new worker until the follow-up → the follow-up re-sends the full TASK (message, goal, analysis, commands) plus `commands_already_run` (exit, duration, log path) and the follow-up text → `not_in_task_commands` still enforced → no duplicate completion.
  - Same-session follow-up is not a re-send. A user restart gives one `worker_restarted` with cause `user_restart`.
  - Editor wait: the report is held, shown in the backend state and the Korean UI line, re-sent once to the new manager session after a manager restart, then the line is cleared.
  - Unknown report: notice ×1 without content; readable in `workbench_status`; listed in `manager_recovery` (`reports_unknown` 1) and never re-sent.
  - `workbench_status`: shape (task message/analysis/commands/commands_run, worker restarts with reasons, terminal, reports); answers in under 4 s while both delivery locks are held and the worker never answers probes; stays under 200 KB with 60 reports × 200 KB.
- Skill (`test_recovery_skill_independent_p27cd70.py`): front matter, English, agent-only, every notice covered, status before the decision, never re-running worker commands, installed for the manager only.
- Bridge (`bridge_recovery_independent_p27cd70.test.ts`): manager-only tools and schema; the restart_worker tool_request round trip; per-role notice types; a manager notice deferred on composer/busy/pending, then injected once.

## Edited independent tests (minimal; all reflect decided C-D70 behaviour)
| test | edit | reason |
|---|---|---|
| tests/bridge/bridge_terminal_independent_p27cd68.test.ts:89 | manager tools = to_worker + restart_worker + workbench_status | C-D70 (3)/(5) manager-only tools |
| tests/bridge/test_cw18_schema_independent_p27w.py:61 test_bridge_tools_per_role | same manager tool set (TOOL_ROLES unchanged: the backend answers the new tools itself) | C-D70 (3)/(5) |
| …p27w.py:121 test_front_matter_and_directories | skill dirs + workbench-recovery | C-D70 (5) skill |
| tests/backend/test_omp_isolation_independent_p27m.py:208 test_workbench_skills_directory_holds_exactly_the_two_role_skills | + workbench-recovery | C-D70 (5) |
| …p27m.py:368 test_skills_directory_and_role_filters | manager includeSkills + workbench-recovery | manager-only install |
| …p27m.py:392 test_role_skill_allowlist_reads_names_from_the_skills_directory | manager allowlist + workbench-recovery (crowded-dir check unchanged) | same |
| …p27m.py:678 test_the_real_role_allowlists_accept_only_the_roles_own_workbench_skill | per-role allowlist; added a case: workbench-recovery is a leak for the worker | strengthens the manager-only rule |
| tests/backend/test_omp_isolation_independent_p27n.py:201 | manager default includeSkills + workbench-recovery | same |

## Paths written (sha256, first 12)
test_watchdog_independent_p27cd70.py d23e535eb68d (new) · test_recovery_e2e_independent_p27cd70.py 56a108bf1845 (new) · test_recovery_skill_independent_p27cd70.py a1456fc86f33 (new) · bridge_recovery_independent_p27cd70.test.ts ef2c53e56804 (new) · test_omp_isolation_independent_p27m.py 985675ba6102 · test_omp_isolation_independent_p27n.py 8370bccc606f · test_cw18_schema_independent_p27w.py 447ca7fe50e5 · bridge_terminal_independent_p27cd68.test.ts aa2d33835ed0 · this file.
