---
name: workbench-recovery
description: Recover a Workbench Task after a Workbench notice (worker_stalled, worker_restarted, report_delivery_unknown, manager_recovery) - read workbench_status, then follow up, cancel or restart_worker. Agent-only; not for the user.
---

# workbench-recovery (manager)

Workbench watches the worker for you; never poll it yourself. It tells you with a Workbench notice: a user message whose JSON has `workbench_notice` (no `workbench_message_id`; it is not a worker report and needs no reply to the worker).

## Notices
- `worker_stalled`: the worker's Task is open, but after 2 automatic status checks it sent no report and ran no command.
- `worker_restarted`: the worker OMP is a new session (user restart, your `restart_worker`, or a crash). It has no memory of the Task; the Task is still open. This notice is the one cue to continue: one follow-up on the same `task_id` (Workbench re-sends the full Task), or cancel.
- `report_delivery_unknown`: a worker report may not have reached you; it is not re-sent. Its text is in `workbench_status`.
- `worker_terminal_done` (information): a terminal command the previous worker session started has ended (`command_id`, exit, `log_path`); the new worker has not got the Task yet. It is in `commands_run` and in the Task re-sent with your follow-up; never run it again yourself.
- `manager_recovery`: your own OMP session is new. Reports that never reached your old session, or that the worker sent while your OMP was down, arrive as messages (`reports_resent` counts them; wait for them before you cancel the Task); reports with an unknown delivery are only listed.

## Procedure
1. Call `workbench_status` (no arguments, or the notice's `task_id`). It is read-only and answers at once: the Task (message, `commands`, `commands_run` with exit code, duration and `log_path`), the worker (idle/busy, session, restarts with reasons), the host terminal (running command or idle) and reports to you that are pending, deferred or of unknown delivery, with their text.
2. Decide, once:
   - The work is done or the facts are enough (for example `commands_run` shows every command finished): read the report text there and answer the user. If the Task is still open, cancel it (`to_worker` with `task_id`, `cancel: true`).
   - The worker is idle but has not finished, or it is a new session: send a follow-up `to_worker` on the same `task_id` that says what to do next (continue with the remaining commands, or clarify). A new worker session gets the full Task and the commands already run from Workbench.
   - The worker is stuck mid-turn or does not respond (busy for a long time with no terminal command running, or your follow-up got no answer): call `restart_worker` with a short `reason` and end your turn; when the `worker_restarted` notice arrives, send exactly one follow-up on the same `task_id` (or cancel). Never send a second follow-up for the same restart.
   - The Task no longer matters: cancel it.
3. Tell the user briefly what happened and what you did, then end your turn and wait for the worker's report.

## Rules
- Never run the worker's commands yourself and never operate the host terminal; a command the worker started keeps running through a restart.
- `restart_worker` is refused while another restart runs (`restart_in_progress`); do not retry in a loop.
- Do not re-send a report's content to the worker; do not invent results the status does not show.
