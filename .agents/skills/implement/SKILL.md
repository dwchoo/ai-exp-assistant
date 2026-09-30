---
name: implement
description: "Execute an approved ticket graph: schedule safe independent work in parallel, integrate dependencies, write independent tests, review, fix and verify. Explicit implementation requests only; not discovery, planning, or review-only."
---

# Implement — dependency-aware verified delivery

Read `.codex/workflow-contract.md`, the supplied feature's BRIEF, SPEC, PLAN and relevant
tickets/ADRs. Leaves return to Root. Sol owns the complete execution; the project default is
High, but honor an explicitly selected supported higher effort. Do not switch models.

## Entry and resume

Establish explicit authorization for the current plan/scope, recording its real digest and
approval reference. Read state, but reconcile it with files, current candidates, evidence
and host execution status. Do not duplicate an active/unknown assignment. A plan-only or
review-only request is not permission to implement. Do not silently generate a new plan:
if no valid ticket bundle exists, return the precise need for to-tickets. Limited dispatch
scope/base refresh inside an unchanged approved plan is normal, not a new design interview.

Check availability of configured workers, `test_designer`, reviewer and workspace capabilities.
Do not declare supported isolation/model/effort merely because the config names it. Reserve
finite implementation, Oracle, verification/fix and final integration capacity in the ledger.
Read `references/dispatch-and-oracle.md` for assignments and journal evidence. Record every
started call, including early reviews and Oracle. Reconcile unknown usage before spending.
On resume preserve cumulative usage and user ceilings; resolve legacy limit origins, record
policy transitions and clear obsolete internal-budget approval waits through Root decisions.
A PLAN estimate alone is not a user cap. Reuse already-read instructions; reread needed sections
after context loss or changed bytes. Missing instructions block affected dispatch; never
silently substitute another installation scope.

## Select an implementation worker

Use observed implementation constraints, preserving explicit user role/model pins:
- `bounded_pattern`: clear contracts and an established bounded pattern → `worker`.
- `coupled_invariants`: interacting invariants, concurrency or complex compatibility →
  direct `worker_senior`; no prior failure is required.
- `stalled_implementation`: known cause/fix direction but a concrete implementation obstacle →
  consider `worker_senior` using the returned delta and failed checks.
- `unresolved_diagnosis`: unresolved cause/design question → precise Oracle question or
  discriminating experiment; use the common contract's Oracle tier/read-only rules.
- `external_blocker`: environment, permissions, requirements or prerequisites → repair within
  authority or block affected work, not escalation to a stronger implementer.

File count, description length, keywords, confidence and failure count alone never select
senior. Senior preserves scope, permissions and independent verification. PLAN is advice;
record actual selection and evidence in the assignment, not a rewritten PLAN. Use `user_pin`
and its request reference when a pin determines selection. Surface unavailable roles.

## Reassess results and retries

Classify worker results: candidate_ready needs independent verification; needs_reroute needs
implementation reassessment; needs_oracle needs a concrete diagnostic/design question;
blocked needs its authority/environment/contract prerequisite resolved. Counts in contract
section 8 remain reassessment checkpoints, not automatic escalation or approval gates.
Compare progress, choose a bounded correction or discriminating experiment, and reserve
verification/integration capacity. Resize internal estimates with recorded reasons; retain
cumulative usage and causes. Preserve user/host ceilings and unknown-limit reconciliation.
Do not repeat an unchanged failing approach. If a bounded alternative yields neither progress
nor a concrete next experiment, block affected work and continue independent ready tickets.

Before reassignment, confirm the previous writer and child processes stopped, inspect partial
delta/checkpoint and release safe leases. Unknown state forbids another writer in that workspace.
Link the prior assignment and create a new invocation without resetting usage.

## Resolve feasibility before expanding implementation

For tickets with a critical unverified runtime/API assumption, follow contract section 7:
record the assumption, run the smallest real bounded experiment, classify its result and
choose the next implementation scope. Reuse current evidence; routine changes need no new
feasibility ritual. A failed or inconclusive probe calls for a focused repair/experiment or
an explicit blocker, not a larger candidate and another final review. Check probe drainage
and process cleanup before attributing timeouts to the system under test.

## Schedule the ready frontier

Select tickets with integrated, validated dependencies, current base/contracts and available
resources/leases/capacity. Recompute after every result/integration; no global wave barrier.

Root provisions/identifies authorized isolated workspaces and verifies actual cwd, writable
roots, input snapshot, config and environment before allowing concurrent mutation. Default
pilot: two writer workspaces and at most four open child threads of all roles combined.
One mutation owner per workspace, including Root, tests and mutating subprocesses.
If binding/isolation is unsupported, use serial execution and say why. Do not invent tool
parameters or launch hidden alternate agents to fake support. Separate branches alone do
not isolate files or shared services.
Use the contract's actual agent-binding smoke evidence before the first concurrent writers;
reuse it while the environment is unchanged. Shared live runtime claims remain exclusive.

Delegate each bounded ticket using the selection criteria and assignment packet.
Workers use small relevant red/green/refactor checks where practical. Root audits each
assignment against its own recoverable checkpoint and collects attributable output only.

## Verification is automatic, not another user command

For each coherent verification unit, read and apply `../change-verification/SKILL.md`.
Root dispatches `test_designer` and a fresh `reviewer`; no request to the user to start these stages.
`test_designer` writes meaningful tests for new behavior/regressions or identifies sufficient existing
coverage. Production fixes go to a bounded worker, not `test_designer`. Frozen final code+tests go to
the independent reviewer. Fix, rerun affected checks and review changed impact as necessary.
Different units can pipeline in separate safe workspaces; never final-review a moving tree.
A worker's tests or Root self-review cannot substitute for the independent roles.
Do not dispatch final review with known required failures or missing decisive gate evidence.
Use narrow early technical review only when it will resolve a named uncertainty. Once final
coverage passes, integrate and dispatch the next ready work without another unchanged review.

Root integrates one verified attributable output at a time into the integration workspace.
Recheck the new integrated candidate; integration/conflict deltas receive affected tests and
review. Only then satisfy dependency gates. Dependent work starts from that integrated base,
not an earlier parent commit that lacks the prerequisite. Final feature verification must
cover cross-ticket behavior, not only isolated successes.

## Handle deep blockers through Oracle

Workers/testers/reviewers return `needs_oracle` evidence to Root; they do not spawn.
Root may identify the question itself. After process/lease reconciliation, consult an Oracle
sibling under the common contract. Relay guidance and delegate the resulting experiment/fix;
repeat required tests/review. Advice alone never resolves an issue. Do not resend an unchanged
packet without new evidence or a specific unresolved question. Keep unaffected work moving.

## Close or pause

Record current candidate, acceptance coverage, tests, review/delta review, integration and
scope-audit evidence. Declare only contract-supported status. Preserve its failure, authority, recovery,
capability and user/host-limit blockers. Depleted internal estimates require reassessment,
not automatic approval. Missing verification is not a pass.
On interruption persist resumable state and actual pending assignment information; do not
promise future execution. No commit/push/PR/deploy/production migration without authorization.

## Existing ticket identifiers

When reading an existing PLAN or ticket, interpret `luna_worker` as `worker` and
`luna_worker_max` and `complex_worker` as `worker_senior`. Interpret `deep_oracle` as
`oracle_senior`. Dispatch only the current identifier without
rewriting the user's stored ticket or its history. If another worker identifier is
unknown, report the mismatch to Root instead of guessing a replacement.

Use `../workflow-ledger/SKILL.md` for durable invocation/reservation records and
`../workspace-evidence/SKILL.md` for checkpoints, candidates and scope audits. On resume compare
host assignments/processes and retained checks; unknown starts hold capacity. Read a supplied
handoff as evidence, preserving prior authority, never as a new authorization by itself.
