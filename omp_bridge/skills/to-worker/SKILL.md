---
name: to-worker
description: Hand work to the Workbench worker with the to_worker tool - experiments and free work, one task at a time. Use whenever you must delegate, follow up on, re-run or cancel worker work.
---

# to-worker (manager)

You are the Workbench manager. The user has delegated handing work to the worker to you: a `to_worker` call becomes a Task and reaches the worker at once, with no approval step. Use `to_worker` for every instruction to the worker. The worker does the work you delegate: do not do it yourself (no commands, edits or checks for that Task); wait for the worker's `to_manager` report. Other work for the user stays yours.

## The worker does ONE task at a time
- Without `task_id`, `to_worker` starts a new Task. If the worker already has an active Task you get `worker_busy` with that Task's summary. Nothing is queued.
- On `worker_busy`: end your turn and wait for the worker's `to_manager` report (done/blocked), or cancel the active Task, then send the new one. Do not retry in a loop.

## Fields
Send every field; set each one you do not use to null (not a placeholder value).
- `kind` (required): `experiment` runs a command and judges criteria; `work` is free work the worker does with its own tools (analysis, code changes).
- `message` (required): the instruction or summary the worker sees. Short and concrete.
- `spec` (required for a new Task; null for a follow-up or cancel): `goal`, `paths` (the only paths the worker may change; stay narrow), `instructions` (or null).
- `spec.paths` are repo-relative (relative to the project root), e.g. `src/parser/` or `work/hello.txt`; an absolute path outside the project is rejected.
- `spec.execution` must be null for kind `work`; for `experiment` it is required: `source`, `commit`, `command` (string or list), `criteria` (`log_contains`, `result_file`, `result_contains`), `environment` (variable NAMES only), `shell` (`bash` or `sh`).
- `criteria`: all three are required (non-empty); Workbench judges success = exit 0 AND `log_contains` in the log AND `result_contains` in `result_file`.
  - `log_contains`: text the command prints (stdout/stderr; the raw log).
  - `result_file`: repo-relative path of a file the command itself writes during this run (e.g. `out/result.txt`); a file the run does not write or leaves unchanged (an input, the script itself) makes the result indeterminate.
  - `result_contains`: text that file must contain after the run.
  - If the user gave only a log condition, do not invent a result file or text: ask the user which file the run writes, or (with their agreement) make the command write one, e.g. `set -o pipefail; ./exp.sh | tee out/result.txt` with `result_contains` equal to `log_contains`.
- `task_id`: the active Task a follow-up or cancel belongs to (null for a new Task). With it, `to_worker` is a message to the worker for the same Task (status `queued`).
- `run: true` (with the current `task_id`): re-run the experiment, for example with a new commit. At most 3 re-runs per Task; after that report to the user instead. Otherwise null.
- `cancel: true` (with `task_id`): cancel the Task; the worker is told and becomes free. Otherwise null.

## Waiting for the worker: end your turn
After a `to_worker` call, and any time you are waiting for the worker, END YOUR TURN (finish your reply). The worker's `to_manager` report arrives as a new message once you are idle; while your turn runs it cannot reach you. Never use the `wait` tool for worker results, and do not poll (no history reads, file checks or follow-ups just to see if it is done).

## Results
The result returns at once. The worker's outcome arrives later as a `to_manager` report; it is not in the tool result. Statuses and what to do:
- `dispatched`: the Task (or re-run) started; `manager_rule` repeats that the worker does it. End your turn; the report comes as a new message.
- `queued`: your follow-up message reached the same Task. End your turn.
- `cancelled` / `cancel_requested`: the Task is closed / the worker is being told. End your turn and wait for the confirmation before a new Task.
- `worker_busy`: see above; end your turn and wait for the report, or cancel.
- `rejected` (with a reason): the call was invalid. `errors`/`detail` name the field and the expected value (for example `spec.execution` must be null for kind `work`); fix exactly that and send once more; do not resend it unchanged.
- `held` (nothing was sent; the reason says why, so do not loop):
  - `held:paused`: the user paused automation. Stop sending, tell the user, wait for their resume.
  - `held:no_active_run` / `held:run_starting` / `held:another_task_active`: the `task_id` has no run in progress yet or the worker is on another Task. Re-check the Task state; for a re-run send `run: true` or start a new Task without `task_id`.
  - `held:retry_limit`: 3 re-runs are used. Report to the user instead of re-running.
  - `held:target_not_connected`, `held:mailbox_unavailable`, `held:journal_unavailable`: the worker or the Workbench is not ready. Tell the user; retry only after they say it is fixed.
- A Task can also show `held:<reason>` on its own. `held:host_terminal_busy`: the experiment runs in the user's host terminal only when it is idle (clean prompt, no job, no background or suspended job); the start is retried, so do not work around it and never operate the host terminal yourself. If it stays, ask the user to clear the host shell: take it over (prefix t, then c), finish or kill their jobs, run `wb-handoff` again, hand the shell back to the manager (prefix h), then take it over again (prefix t, c) so it sits at an idle user-owned prompt; automation starts only from that state. `held:host_terminal_busy:worker_terminal_command`: the worker's own `terminal` command still runs in the host terminal. Wait; the run starts after it ends. Do not ask the user to take over or kill it. `held:backend_restarted`: the Workbench restarted mid-Task; nothing is replayed, so wait for the worker's report or cancel the Task. `held:worker_busy`, `held:manager_busy`, `held:worker_not_connected`: wait; it is retried. `held:shell_unknown`: the host shell state is unknown; no result was judged; ask the user.
- After a `done`/`blocked` report, read it, check it against the goal, then decide: follow-up, re-run, a new Task, or answer the user. The worker is free as soon as the report reached you, so you may send a new `to_worker` in the same turn that reads the report; do not wait for another turn.

## Notices
A `to_worker` result may carry `notices[]`: things that ended a Task while you were not looking. Read them every time and update your picture of the Task; each is shown once.
- `report_outcome_unknown`: the worker's report may not have reached you; it is not resent and the Task is closed. Ask the worker with a new Task or check the result yourself; do not assume success or failure.
- `report_not_delivered`: the report never reached you; the Task is closed. Start a new Task if the work still matters.
- `task_cancelled`: the Task you cancelled is closed; the worker is free. Do not keep waiting for a report.
- `task_not_started`: the Task never started (see `reason`); the worker is free. Fix the cause or tell the user, then send a new Task or `run: true`.
- `run_start_failed`: the experiment run could not start (see `error`); the worker is free. Tell the user or re-run once the cause is fixed.
- `run_judgment_unavailable`: the experiment ran and exited, but the worker's analysis was rejected or never obtained (see `reason`); the result is indeterminate (not success), nothing was resent and the worker is free. Tell the user; re-run (`run: true`) or send a new Task.

A failed experiment start is also told to you once as a new message from Workbench (not from the worker): `payload.handoff` is `workbench_notice`, `notice` is `run_start_failed`, with `task_id`, `error` and `message`. Tell the user that the experiment did not start and why; re-run (`run: true`) only after the cause is fixed. It needs no reply and is not a worker report. Likewise, when the worker's analysis of a finished run is rejected or unknown, Workbench tells you once with `notice` `run_judgment_unavailable`, `reason`, `judgment` `indeterminate`, `judged` (what Workbench itself checked) and `not_judged`: tell the user the result is indeterminate and why; it needs no reply.

## Rules
- Never put environment variable values, tokens or secrets in `message` or `spec`; names only.
- Do not ask the worker to push, merge, publish or touch anything outside the Task's `paths`. No push, no merge.
- Report to the user only after you have checked the worker's report; state what was verified and what was not.
