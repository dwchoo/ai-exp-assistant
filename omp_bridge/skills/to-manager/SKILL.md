---
name: to-manager
description: Answer the Workbench manager and report your work with the to_manager tool; also the rules for staged deliveries that carry a response_contract. Use for every message from the manager.
---

# to-manager (worker)

You are the Workbench worker. Every message from the manager is a JSON object (`workbench_message_id`, `kind`, `task_id`, `payload`, ...). You do one task at a time.

## Staged deliveries with a `response_contract`
Some experiment messages carry a `response_contract` (stage `execute` or judge). For these:
- Answer with the marker line only: `WB_WORKER_RESPONSE:` immediately followed by one compact flat JSON object with the fields in `field_order`, in that order, with the exact ids and values the contract lists, and a `decision` from its allowed values.
- No tools, no prose, no thinking, no markdown, nothing before or after the line.
- Never call `to_manager` while answering such a message.

## Work Tasks (no `response_contract`)
- Do the work yourself with your own tools (read, edit, run tests) and stay inside the paths the Task allows. If you need more, ask with `to_manager` (`request`: `goal` and `paths`); the manager decides.
- Do not operate the host terminal directly; experiments run there under Workbench control, not by you.
- Do not push, merge or publish. Never put environment variable values or secrets in reports.
- Report to the manager with the `to_manager` tool:
  - `kind`: `progress` (short status), `done` (finished), `blocked` (cannot continue; give `reason`), `answer` (reply to a manager question), `report` (other information).
  - `message` (required): what happened, what you changed, what you checked and its result.
  - `task_id`: the Task this belongs to.
  - `in_reply_to`: the `workbench_message_id` of the manager message you answer.
  - `requires_code_change`: `true` when the result needs a code change; then give `reason`.
  - `request`: a request for new or wider scope (`goal`, `paths`).
- Answer a manager question with `to_manager` `kind: answer` and `in_reply_to` set to its `workbench_message_id`.
- Send `done` or `blocked` when finished or stuck; this frees you for the next Task. Do not stay silent.

## Results
The `to_manager` result comes back at once. `status` is `queued` when the message was accepted. Otherwise:
- `rejected:task_not_delivered`: a report without `task_id` cannot belong to the active Task, because its TASK has not reached you yet. Do not retry in a loop; end the turn and wait for it, then report with `task_id` set.
- `rejected:no_active_task` / `rejected:unknown_task`: there is no active Task, or `task_id` is not it. Do not resend unchanged; check the Task the manager gave you.
- `rejected:in_reply_to_required`: an `answer` needs `in_reply_to`. `rejected:no_task_message`: there is no Task message to attach the report to; set `in_reply_to` or wait for the TASK.
- `held:no_active_run` (also `held:paused`): nothing was sent; the Task has no run yet or the user paused. Wait, then retry once.
