---
name: to-tickets
description: "In one planning command, convert a persisted approved brief into an implementation SPEC and dependency/resource-aware tickets. Fresh user-selected Sol XHigh/Max session. Plan only; do not implement."
---

# To tickets — specification plus executable work graph

One entry point performs both specification synthesis and ticket decomposition. It does
not invoke separately installed upstream to-spec/to-tickets skills. Assume the user chose
Sol XHigh/Max; model changes are host/user operations, not skill actions.
Read `.codex/workflow-contract.md`. Leaves return to Root. Locate the explicit BRIEF.md;
read it fully with relevant glossary, ADRs and linked source decisions. New session context
is not a license to repeat the whole interview. If approval is absent, draft transparently;
do not claim the brief or plan has been approved.

## Establish source and technical reality

Inventory the current repository, existing tests, public interfaces and baseline including
dirty/new files. Use `code_explorer` only for independent factual work. Compare assumptions
with actual code. Resolve available facts yourself. Mark material missing decisions and
request only those decisions or route them to refine-spec; never invent product requirements.
An optional Root-mediated Oracle consultation is for one hard technical blocker, not default
planning. Do not dispatch implementation workers or write product/tests.

## Synthesize SPEC.md

Use `references/spec-template.md`: problem, solution, relevant user stories, scope/non-goals,
public contracts and invariants, module/interface decisions, failure behavior, compatibility,
acceptance IDs and test seams/commands. Preserve decisions from the brief. Prefer existing
behavior-level test seams; explain new ones. Scale detail to the feature instead of forcing
a long story list. Keep stable design intent separate from volatile dispatch file scopes.
Cite the input brief revision and any genuinely new technical elaboration.

## Create tickets and PLAN.json

Create one independently understandable Markdown file per ticket, each with delivered
behavior, acceptance IDs, contracts, testing requirement and stop conditions. Prefer small
verifiable vertical slices, not arbitrary layers or files. Shared contract work is a real
prerequisite when necessary. Preserve compatible refactors through expand/migrate/contract
where appropriate. Include explicit final cross-ticket integration/verification work.

PLAN.json is the canonical graph. Use `references/plan-template.json`. Record real dependency
edges separately from mutual-exclusion resources and write ownership. State why each edge
exists and what verified/integrated artifact satisfies it. Include base/contract requirements,
proposed write scope, recommended worker, workspace requirement and relevant service namespaces.
For worker recommendations read only [Select an implementation worker](../implement/SKILL.md#select-an-implementation-worker).
This reference is not implement invocation or execution authority. Keep `worker`; optionally add
`routing_hint` with `reason_codes` and `evidence_refs` string lists. Preserve older PLANs without hints.
Do not mark parallel-safe just because file paths differ. Path scopes are planning estimates
until implement revalidates them at dispatch.

Check ID uniqueness, dependency existence, no cycles, no unowned acceptance IDs, no phantom
independence of unstable APIs, and final integration coverage. Show the dependency graph and
initial independent frontier. Resource conflicts restrict runtime concurrency even for ready
tickets. Waves are illustrations, not barriers; implement schedules dynamically.
Treat planned call counts as Root-adjustable operating estimates. Record actual user-imposed
ceilings with their request references separately; do not invent fixed total-call approval gates.

Also check acceptance dependencies: each required gate item must be verifiable using only
its owner and transitive predecessors. Record item ID, observable check, evidence level and
supplier tickets in PLAN; a later-ticket supplier is a planning error even in an acyclic DAG.
Separate local behavior from later live integration without dropping either acceptance item.
For feasibility work, name the critical assumption and smallest decisive runtime experiment;
make its result determine the next implementation scope, not merely a final review checklist.

## Publish locally and hand off

Persist SPEC, PLAN and tickets; no remote issues without authorization. Repeated execution
must preserve stable ticket IDs, completed work and old revision references; record changes.
Present one combined approval summary: important decisions, tickets, dependencies, parallel
candidates, conflicts, test plan and unresolved blockers. No general new interview. A request
to implement this exact presented bundle can both approve it and start development later.
Record bundle manifest/digest separately from files being hashed. Return:
`$implement <feature-directory>`.
Stop at planning. Do not start code changes, tests-as-code, worktree provisioning or agent
execution merely because tickets are ready. The planning command is one entry point, not
a promise that material ambiguity never requires a user decision.

## Acceptance trace

Before presenting the combined plan, map each acceptance ID to its responsible ticket,
the behavior-level interface that will demonstrate it, and any final integration check.
Mark checks that require unavailable services or runtime capability as pending evidence.
Do not count a ticket status or an implementation detail assertion as acceptance coverage.
Keep this trace in SPEC/PLAN and reference it from tickets instead of maintaining separate
competing graphs. This check stays within the single to-tickets planning command.

Validate the graph and gate coverage with `scripts/validate_plan.py --input request.json`
and the agreed requirements revision using the shared
[formats](../workflow-ledger/references/formats.md). Requirements include the complete approved
acceptance/gate list and real authority reference, not a list inferred from passing checks.
Unapproved drafts remain drafts even when structurally valid. Preserve older nonstandard
plans and report missing requirements instead of silently inventing approval or coverage.
