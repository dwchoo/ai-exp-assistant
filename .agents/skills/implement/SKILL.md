---
name: implement
description: "Execute an approved ticket graph: schedule safe independent work in parallel, integrate dependencies, write independent tests, review, fix and verify. Explicit implementation requests only; not discovery, planning, or review-only."
---

# Implement — dependency-aware verified delivery

Read `.codex/workflow-contract.md`, the supplied feature's BRIEF, SPEC, PLAN and relevant
tickets/ADRs. Leaves return to Root. Sol owns the complete execution; the project default is
XHigh, but honor an explicitly selected supported higher effort. Do not switch models.

## Entry and resume

Establish explicit authorization for the current plan/scope, recording its real digest and
approval reference. Read state, but reconcile it with files, current candidates, evidence
and host execution status. Do not duplicate an active/unknown assignment. A plan-only or
review-only request is not permission to implement. Do not silently generate a new plan:
if no valid ticket bundle exists, return the precise need for to-tickets. Limited dispatch
scope/base refresh inside an unchanged approved plan is normal, not a new design interview.

Check actual availability of configured workers, `test_designer`, reviewer and workspace capabilities.
Do not declare supported isolation/model/effort merely because the config names it. Reserve
finite implementation, Oracle, verification/fix and final integration capacity in the ledger.
Under contract section 8, Root adjusts internal call budgets to remaining work without asking
the user again. Distinguish these estimates from explicit user ceilings and host limits.
Read `references/dispatch-and-oracle.md` when preparing assignments and the ledger. Record
actual usage as calls start, including early reviews and Oracle, not just initial reservations.
Unknown historical usage needs reconciliation before spending the affected budget.
On resume, identify the origin of old limits and record the policy transition without resetting
usage. Clear obsolete internal-budget approval waits through a recorded Root decision; preserve
actual user ceilings. A plan's numeric estimate alone is not evidence of a user-imposed cap.

## Reassess retries without routine approval waits

The worker/review/Oracle counts in contract section 8 are reassessment checkpoints. Before
the next batch, compare concrete progress and choose a bounded correction or discriminating
experiment with independent verification reserved. Resize internal budgets from remaining
mandatory work and justified correction capacity; report the decision instead of requesting
permission merely to exceed 2 attempts or an internal total. Keep cumulative usage and causes.
If attempts make no progress, change approach through reproduction, the existing senior role
or targeted Oracle diagnosis. If that bounded alternative yields neither progress nor a
concrete next experiment, block only the affected ticket and continue independent ready work.
Do not keep increasing budgets to repeat an unchanged failing approach. User/host limits,
approved requirements, permissions and independent testing/review remain binding.

## Resolve feasibility before expanding implementation

For tickets with a critical unverified runtime/API assumption, follow contract section 7:
record the assumption, run the smallest real bounded experiment, classify its result and
choose the next implementation scope. Reuse current evidence; routine changes need no new
feasibility ritual. A failed or inconclusive probe calls for a focused repair/experiment or
an explicit blocker, not a larger candidate and another final review. Check probe drainage
and process cleanup before attributing timeouts to the system under test.

## Schedule the ready frontier

Select tickets whose declared dependencies are integrated and validated, base/contract
assumptions remain current, and resource claims/workspace leases/capacity are available.
Recompute after any ticket result or integration. No artificial global wave barrier.

Root provisions/identifies authorized isolated workspaces and verifies actual cwd, writable
roots, input snapshot, config and environment before allowing concurrent mutation. Default
pilot: two writer workspaces and at most four open child threads of all roles combined.
One mutation owner per workspace, including Root, tests and mutating subprocesses.
If binding/isolation is unsupported, use serial execution and say why. Do not invent tool
parameters or launch hidden alternate agents to fake support. Separate branches alone do
not isolate files or shared services.
Use the contract's actual agent-binding smoke evidence before the first concurrent writers;
reuse it while the environment is unchanged. Shared live runtime claims remain exclusive.

Delegate each bounded ticket to `worker` or a justified `worker_senior`. Include
actual workspace/base, acceptance IDs, write scope, resource claims, checks and stop rules.
Workers use small relevant red/green/refactor checks where practical. Root audits each
assignment against its own recoverable checkpoint and collects attributable output only.
A worker's candidate_ready response does not mark its ticket integrated.

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

Workers/testers/reviewers return a concise `needs_oracle` evidence packet; they do not spawn.
Root can make the same request itself. Stop mutating processes, release safe leases, and call
`oracle` or `oracle_senior` as a sibling when justified. Use the common contract.
Relay diagnostic guidance, delegate experiments/fix, then repeat required tests/review.
Keep unaffected ready work moving safely. Oracle advice alone never closes the issue.

## Close or pause

Record current candidate, acceptance coverage, tests, review/delta review, integration and
scope-audit evidence. Declare only the contract's supported final status. Known failures,
permission/scope/plan changes, unsafe recovery, unavailable required roles, a no-progress
impasse or exhausted explicit user/host limits are blockers. A depleted internal estimate
calls for Root reassessment under section 8, not automatic user approval. Missing verification
is not a pass.
On interruption persist resumable state and actual pending assignment information; do not
promise future execution. No commit/push/PR/deploy/production migration without authorization.

## Existing ticket identifiers

When reading an existing PLAN or ticket, interpret `luna_worker` as `worker` and
`luna_worker_max` and `complex_worker` as `worker_senior`. Interpret `deep_oracle` as
`oracle_senior`. Dispatch only the current identifier without
rewriting the user's stored ticket or its history. If another worker identifier is
unknown, report the mismatch to Root instead of guessing a replacement.
