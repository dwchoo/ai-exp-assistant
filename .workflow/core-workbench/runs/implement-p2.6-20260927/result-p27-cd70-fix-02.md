# p27-cd70-fix-02 result (worker_senior, Opus) — 2026-10-07

Review-02 P3-T and P3-W fixed. No commit, no graphify update, no provider request, temp only under /tmp. The files the test agent is editing (cd69 independent ×2, terminal p27c) were not touched.

## Checks (real exit codes)
| run | result | exit |
|---|---|---|
| `test_*cd70*` (tests/backend), alone, 5× | 129 OK ×5 | 0 ×5 |
| tests/backend full (final code) | 1061 OK (skip 30, xfail 1) | 0 |
| tests/bridge py | 52 OK (skip 1) | 0 |
| node tests/bridge/*.test.ts | pass 43 / fail 0 | 0 |

The first full backend run had one error: `test_cw18_smoke_fixes.NullableArgumentsTests.test_follow_up_with_placeholder_spec_and_flags_is_a_question` (IndexError). It read `mailbox.created` right after the call returned, before the outbox lane had created the message. This is a non-independent test, so I made it wait for the QUESTION (`wait_until`, as the same file does at :79 and :198). After that, 3 isolated runs and the full run above passed.

## Items
- P3-T — done. `test_cd70_task_resend.py` `test_no_resend_while_the_task_message_is_still_on_its_way` wraps `_task_message_listener` and waits for the TASK's last outbox event (`delivered`, recorded after the flow's own handling) before it sets the state by hand. No sleep; the assertion is unchanged.
- P3-W — done. `restart_detail(open_task, ready)` (flow_recovery.py:111) returns the text for each case:
  - Open Task (busy, or blocked and open, the same rule that sends `worker_restarted`; `Backend._worker_task_open`, service.py:1121): `RESTART_DETAIL` / `RESTART_PENDING_DETAIL` as before.
  - No open Task: `RESTART_NO_TASK_DETAIL` / `RESTART_NO_TASK_PENDING_DETAIL` (:101): "no worker_restarted notice follows and nothing is re-sent", with no follow-up instruction.
  - Tests in `test_cd70_fix01.py`:
    - `WordingTests`: covers all 4 texts.
    - `RestartDetailTests`, on a real backend with fake OMPs: an open Task gives `RESTART_DETAIL` and the notice arrives; a blocked Task gives `RESTART_DETAIL` and the notice arrives; with no Task the result is `RESTART_NO_TASK_DETAIL` and no `worker_restarted` notice arrives within 3 s.
  - The registered-false test now opens a Task. All of these are red without the change (missing `restart_detail` / no-task texts).

## Changed paths (sha256, first 12)
flow_recovery.py 21a0d5e55ecd · service.py b9533955be42 · tests/backend/test_cd70_fix01.py ef98eca4ac38 · tests/backend/test_cd70_task_resend.py eb21e1e8460e · tests/backend/test_cw18_smoke_fixes.py 6aaad963a2d5

## Open questions
None.
