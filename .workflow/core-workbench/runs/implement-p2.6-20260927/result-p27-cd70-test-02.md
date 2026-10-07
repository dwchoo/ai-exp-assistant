# p27-cd70-test-02 result (test_designer, Opus) — 2026-10-07

Test-side fixes for two pre-existing races. No assertion was weakened. Only the three named test files and this file were written. No src/omp_bridge/docs edits, no commit, no graphify update, no provider request. Temp files only under /tmp. Only PIDs this run started were signalled (the `yes` CPU burners, killed by PID).

## Checks (real exit codes)

| check | result | exit |
|---|---|---|
| cd69 two files, isolated ×5 | 30 OK each time | 0 ×5 |
| cd69 two files, under load (16 `yes` burners on 8 cores, run in parallel with the signal test) | 30 OK | 0 |
| `test_review_fixes_independent_p27c`, isolated ×5 | 2 OK each time | 0 ×5 |
| `DefaultPipeXfszTests`, under the same load ×5 | OK ×5 (before the fix: 2/5 OK on the candidate, 3/5 OK on a HEAD copy) | 0 ×5 |
| tests/backend, full | 1061 run, OK (skip 30, xfail 1) | 0 |
| tests/terminal, full (run in parallel with backend) | 208 run, OK (skip 2) | 0 |

The full backend run also includes `test_recovery_e2e_independent_p27cd70`. My P3-1 test (manager_recovery `reports_resent`) now passes on this candidate, so p27-cd70-fix-01 appears to have fixed it.

## Fixes
1. `tests/backend/test_role_boundary_independent_p27cd69.py` (1 site) and `test_task_message_independent_p27cd69b.py` (2 sites): a new `cancel_settled(test, task_id)` helper replaces "cancel + wait for no active Task". It does three things in order:
   - waits until the TASK counts as sent (`_task_message_state == "sent"`), so a cancel notice is always due (`flow_tasks.py` sends one only for a TASK that was sent);
   - cancels and waits for the Task to close;
   - waits until the cancel QUESTION (`cancel: true`, same task_id) exists in `mailbox.created`.

   Only then does the next subtest clear `mailbox.created`. Each test still asserts the first created message is the TASK and that `created == []` while the worker is not connected. The helper also adds a new check that the cancel notice is created.
2. `tests/terminal/test_review_fixes_independent_p27c.py`:
   - The status probe no longer runs `cat /proc/$$/status`. That `cat` raced the shell's own fork, and bash/dash block every signal for a moment around fork, which gave `SigBlk = 0xFFFFFFFE7FFEFEFF` under load.
   - The shell now reads its own status with a builtin read loop (no fork), `STATUS_SAMPLES = 3` times.
   - SigIgn must be clean in every sample, and each sample must hold both fields. SigBlk must be clean in at least one sample, because a block is transient.
   - The pipe/141, timing and XFSZ/153 checks are unchanged.

## Paths written (sha256, first 12)
test_role_boundary_independent_p27cd69.py ea4a74cd0f64 · test_task_message_independent_p27cd69b.py 7fe66cb843f3 · test_review_fixes_independent_p27c.py 23d5bd8e3e93 · this file.
