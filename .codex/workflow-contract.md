# Repository workflow contract — v4

Repository policy, NOT a Codex API/schema or executable scheduler. Read this document
explicitly from the workflow skills. It never overrides system/developer instructions,
user authorization, managed permissions, or stronger repository rules.

## 1. Three phases and five public entry points

- `refine-spec`: user-selected Astra session; interview and document agreed intent.
- `to-tickets`: fresh user-selected Sol XHigh/Max session; compile the persisted brief
  into an implementation specification plus dependency-aware tickets in one invocation.
- `implement`: Sol-owned development; dispatch ready tickets, integrate, independently
  test and review, repair and run final checks. GPT-5.6 Sol High is the project default;
  an explicitly chosen supported higher effort is not automatically lowered.
- `ask-workflow`: inspect current evidence and recommend the next step; do not execute it.
- `investigate-bug`: explicit user invocation only; investigate a named bug and, when
  requested, repair it with independent verification. Recommended user-selected Astra Root,
  `oracle_senior` advice and `worker_senior` experiments/fixes; no model/config switch.
- `change-verification`: internal Root procedure for implement and authorized bug repair,
  also usable for explicit verification.

Skills do not select/switch the active model. Accept the user's stated model selection;
inspect authoritative host metadata when available. Label unavailable metadata unverified,
not a fabricated mismatch. Never claim a model switch from prompt text or self-report.
Actual known mismatches/capability limitations must be surfaced.

Only the active top-level owner delegates, integrates, updates shared workflow state and
spends the parent budget. All custom agents are leaves: no recursive spawning, no lifecycle
skill re-entry, no hidden CLI/SDK agent processes. A worker requests Oracle help through
Root; Root calls an Oracle sibling and relays the evidence-backed answer. Domain skills
within assigned scope remain allowed. Root may consult Oracle during planning or development
for a specific hard technical question; Oracle is not a mandatory planning stage.

When Root has no independent work and is waiting for agent results, use a 60-second
wait timeout by default. Do not repeatedly request shorter timeouts without a concrete
reason. Longer waits are allowed only within host limits and higher-priority instructions.
Messages, completion and user input may return early; respond to those events promptly.
This is a wait policy, not a mandatory sleep, scheduler, or change to agent/call budgets.
The installed minimum/default settings require a new session and separate runtime evidence;
configuration acceptance alone does not prove effective host behavior. Leaf delegation stays disabled.

## 2. Persistent handoff, not shared conversation memory

Honor existing repository documentation layout. Defaults:
- `CONTEXT.md`: domain glossary only; use `CONTEXT-MAP.md` if the project already has contexts.
- `docs/adr/`: accepted hard-to-reverse, non-obvious trade-off decisions, created sparingly.
- `docs/features/<feature>/BRIEF.md`: approved intent, constraints, decisions, evidence,
  acceptance examples, non-goals, open/deferred questions and revision.
- `docs/features/<feature>/SPEC.md`: executable behavior, interfaces, invariants,
  failure semantics and verification requirements derived from that brief.
- `docs/features/<feature>/PLAN.json`: canonical ticket graph and scheduling constraints.
- `docs/features/<feature>/tickets/<id>.md`: one self-contained narrative per ticket.
- `.workflow/<feature>/state.json` and `runs/<run-id>/`: Root-owned operational records,
  in an authorized writable location. Relocate if repository policy requires it.

Create artifacts lazily. Do not put implementation plans into the glossary. Do not use
ADRs as a replacement for the brief. New sessions must read the full relevant brief,
glossary, ADRs and source references; absence from current chat is not absence of evidence.
Distinguish user decisions, observed facts, recommendations, assumptions, and unresolved items.
Do not publish issues, alter global configs, create PRs, commit/push, deploy, or migrate
production merely because a source skill recommends doing so.

## 3. Approval, revision and source of truth

A file saying approved is not authorization by itself. Record a user approval reference
and tool-computed manifest/digest in a separate state/run record. The approved bundle covers
BRIEF, SPEC, PLAN and ticket contents as relevant; hashes do not include their own record.
Approval of a brief authorizes downstream planning, not product implementation. An explicit
request to implement an unambiguous presented plan may approve it and authorize execution
in one message. Do not ask again at each worker/test/review boundary.

Sol may elaborate technical details inside approved constraints. It must not silently
change user-facing behavior, scope, acceptance, compatibility promises or accepted ADRs.
Missing material decisions produce concrete blockers or a focused decision request, not a
second general interview. A prototype/research question can be recorded as deferred;
unresolved prerequisites block affected tickets rather than being invented away.

PLAN.json is canonical for dependencies, resource claims, ticket IDs and scheduling fields;
ticket Markdown holds intent and criteria, referencing that plan without independently
maintained status/graph copies. The state file is an operational index, not proof. Verify
its revisions, candidates and evidence against the actual repository and host on resume.
Changed inputs make dependent evidence stale. Preserve completed/unaffected work when
regenerating a plan; retain stable IDs and record superseded tickets instead of silently
renumbering active work. A changed approved bundle needs scope-appropriate authorization.
PLAN's `worker` is a recommendation, not the actual dispatch role. Optional ticket
`routing_hint` contains `reason_codes` and `evidence_refs` string lists; validation checks
structure, not routing quality. Preserve existing PLAN bytes and legacy role spellings.
Root records the actual choice in its assignment under implement's worker selection rules.
Explicit user role/model pins take precedence; plan approval alone does not imply a pin.


## 4. Logical dependencies versus execution conflicts

Use dependency edges only for real prerequisite deliverables. Prefer verifiable vertical
slices; shared interfaces can be a bounded prerequisite. A wide refactor may use an approved
expand/migrate/contract sequence instead. Do not manufacture parallelism by claiming a
consumer is independent of an unsettled API. Contract-first parallelism must state the
shared interface, contract tests and subsequent integration verification.

For every required gate item, record its owning ticket, observable check, required evidence
level and the tickets supplying its prerequisites. All suppliers must be the owner itself
or its transitive predecessors. An acyclic graph can still have circular acceptance if an
early gate requires a later ticket's behavior. Split verification ownership without dropping
the requirement: for example, local redraw/state restoration and live detach/reconnect
belong to different checks when process separation is delivered later. Final integration
owns the combined behavior. Correct an existing approved plan with a recorded scope review;
do not silently weaken acceptance or add a reverse edge that creates a cycle.

Separately record write ownership and mutable-resource constraints: generated files,
lockfiles, ports, database/schema, caches, service instances and credentials where relevant.
A resource claim has a canonical resource name and mode `shared` or `exclusive`.
Two live claims conflict if names match and either is exclusive. Shared mode is valid
only for genuinely concurrent-safe access; read-only code may still invoke mutating tools.
File overlap or semantic conflict may require serialization despite separate worktrees.

A runnable ticket needs: authorized current plan; dependencies integrated and sufficiently
validated at the approved integration boundary; a current base snapshot; an available
worker slot; confirmed writable workspace and permissions; a free mutation lease and
compatible resource claims; a recoverable checkpoint; and reserved verification budget.
`parallelizable: true` alone is never sufficient. Recompute readiness after every event;
do not wait for an unrelated slow ticket to finish a whole artificial wave.

## 5. Workspaces and mutation leases

Default to one mutation owner per physical worktree/workspace, NOT one writer globally.
Count Root, workers, test authors, formatters, generators and other mutating subprocesses.
Concurrent writers require distinct verified worktrees/isolated workspaces plus safe
shared-resource access. A new agent thread or different branch name alone is not isolation.
A worktree is filesystem-change isolation, not a security sandbox or database isolation.

Before dispatch, Root provisions or identifies an authorized workspace and verifies actual
cwd/repository identity, current base, writable roots, necessary config/skills, dependencies,
logs and service namespace. Do not invent a spawn `cwd` parameter. Use only capabilities
actually exposed by the installed host. Merely telling an agent a path is an instruction,
not an OS-level boundary. If isolation cannot be established, execute serially and report
the limitation; do not silently start multiple writers in the shared checkout.

Before the first parallel writer dispatch, use a bounded smoke test of the actual agent
workspace binding and effective write boundary. Record observed cwd/roots, disjoint scratch
outputs and process/service namespaces. A Root shell writing two paths does not establish
agent isolation. Reuse evidence while the host, binding and environment remain unchanged;
otherwise recheck. Shared exclusive runtime resources still serialize live probes even in
separate workspaces. Lack of isolation blocks concurrent writes, not independent serial work.

A Git checkout at HEAD does not automatically contain dirty/untracked input. Preserve and
transfer the exact approved input snapshot through an authorized mechanism. Never commit,
stash, reset or clean user changes just to create a baseline. Worktree provisioning and Git
metadata writes may require additional approval; do not bypass it. Use a manifest-backed
patch/output bundle when commits are not authorized. Do not leave essential configuration
behind when workspaces are created before uncommitted configuration is installed.

A blocked worker must finish/stop mutating processes and report its partial delta before
its lease transfers. Do not hold a worker thread indefinitely just to await Oracle if that
starves capacity. Root alone integrates one attributable verified delta at a time into the
integration workspace. Merge conflicts are new work, not permission to choose a side blindly.
Rebase/merge/patch adaptation changes the candidate and invalidates affected check/review
coverage. Reassess remaining ticket assumptions after each integration.

## 6. Checkpoints, scope audits and evidence

Capture enough bytes/provenance to preserve staged, unstaged and untracked pre-existing
user work before each writer. A digest is not a backup. Audit actual changes against the
assignment baseline, not only HEAD: additions, removals, renames, modes, symlinks, relevant
ignored/generated inputs and submodule state. Resolve path boundaries. Scope restrictions
in a prompt are detective unless enforced by effective permissions.

Keep recovery checkpoints (bytes needed to undo attributable writes) separate from candidate
manifests (inputs needed to identify a check). Reuse unchanged content and reference existing
artifacts; do not copy the whole tree for every status update or read-only review. Produce a
new manifest when validation inputs change, and a checkpoint when a new writer needs recovery.

On out-of-scope writes: stop dependent integration, retain evidence, isolate/remove only
the attributable agent delta without harming user work, audit and rerun affected checks.
If safe attribution/recovery is impossible, leave files intact and report blocked. No
blanket reset/clean/restore. Workers never directly edit the central plan/state/approvals.

A candidate ID resolves to a tool-observed immutable snapshot/manifest of validation inputs,
including relevant tests, fixtures, config, locks, new and dirty files, modes and submodules.
Record toolchain/service environment separately. Exclude only true operational logs/state,
not test inputs. Check input identity before and after a frozen review/check. Changes during
execution make the result stale. A bare commit SHA cannot identify an arbitrary dirty tree.

Each check records command, cwd, candidate, environment, exit code when executed, criteria,
result (`passed`, `failed`, `not_run`) and evidence. Empty collection is not useful pass proof.
Final coverage comprises the relevant original reviews plus bounded delta reviews; if impact
is uncertain, review the whole unit again. Do not relabel an older result as a final pass.

For feasibility/runtime gates, link structured results from the run ledger's `checks`.
Each result names gate/item IDs, candidate and approved requirements identity, required and
observed evidence levels (`static`, `fixture`, `skill_behavior`, `runtime`), command/cwd,
environment, exit code, result, evidence reference and unknowns. Required items come from
the approved plan, not from whichever checks happened to run. The project gate runner must
reject `passed` for missing/stale/failed items, required unknowns, or absent required runtime
evidence; fixture success cannot substitute. A schema alone does not enforce this decision.
Without a working validator, record the enforcement gap and do not claim a machine-validated
gate. The shared helper validates required evidence records; project-specific runtime assertions remain external.

## 7. Mandatory tests, review and integration

`implement` includes independent `test_designer` test assessment/code and fresh `reviewer` review for
each coherent verification unit, followed by feature integration verification. Group small
tickets where justified, but make a dependency gate explicit. Worker TDD does not replace
`test_designer`; passing tests do not replace reviewer analysis. Reviewer is not Root self-review.

`test_designer` derives expected outcomes from approved behavior before inspecting implementation.
Add meaningful tests for new behavior/uncovered regressions; reuse proven existing coverage
with named evidence. Documentation-only work can justify no executable test addition.
Do not weaken acceptance tests to fit an implementation. Test failure may arise from code,
test, environment or contract; classify it using evidence. A known bug need not have a
perfect reproducer before Oracle can help design a discriminating experiment.

For a feasibility ticket or a change resting on an unverified critical runtime/API assumption,
first record: required assumption -> smallest real experiment -> passed/failed/inconclusive
with evidence -> next implementation scope. Reuse current decisive evidence. Use bounded
connection, one-turn and tool-control experiments as separate observations when relevant.
Implement only the probe and prerequisites needed to answer the question before expanding
the candidate. A timeout is inconclusive about its cause until the probe itself is sound:
drain child pipes/PTYs while running, bound execution and clean up its process group without
retaining sensitive output bodies. A broken probe needs a bounded repair, not provider blame.
Failed or inconclusive prerequisites block affected expansion; keep unrelated ready work moving.

Within each workspace/unit: decisive experiment when needed -> implement and quick checks -> independent tests -> necessary
production fix -> frozen code+tests review -> bounded corrections and delta review -> checks.
Different units may pipeline concurrently in separate verified workspaces. Never review a
moving target simply because the reviewer itself is read-only. Final integration gets
cross-ticket tests and review coverage of integration deltas before required final checks.
Every approved acceptance ID must map to implementation and current verification evidence.

Early technical review is optional, narrowly scoped to a concrete feasibility uncertainty,
and never final acceptance. Do not submit partial candidates to repeated final review while
known required failures or decisive runtime evidence remain unresolved. Start final review
when the unit's required checks pass and the candidate is stable. After corrections, retain
valid original coverage and review only the delta and affected contracts. Reopen the whole
unit only with an explicit impact reason. Optional cleanup does not block completion unless
it violates an approved criterion. When required coverage passes, integrate and advance the
frontier; another unchanged review is not a next action.

## 8. Oracle and Root-managed execution budgets

`oracle` (High) and `oracle_senior` (Max) are two configured variants of one
read-only advisory role. No Astra Max planner, automatic pre-implementation Oracle pass,
self-dispatch, product modifications or final approval powers. Root owns decisions.

Call for a specific hard root cause, conflicting observations, repeated bounded failure,
cross-module invariant or design contradiction. Do not call for a routine typo, missing
install/permission, or unknown file location that a focused lookup can resolve. Worker,
tester or reviewer may request advice, but Root selects and invokes the sibling Oracle.
High is the default; Max can be selected directly for a documented deep question or once
as escalation. Do not require a wasted High call first. Repeated calls need new evidence or
a genuinely unresolved question. Stronger model output is still a hypothesis until checked.

Consultation input: ticket/incident ID, spec and candidate, constraints, expected/observed
behavior, logs/reproducer if available, attempts and outcomes, known/unknown facts, precise
question. Output: supported observations, hypotheses with discriminating experiments,
smallest fix direction, risks, regression-test ideas, whether an approved decision is
invalidated, and what remains unknown. Root/worker executes mutating experiments/fixes.
Oracle advice never closes a ticket, waives verification or changes acceptance criteria.

Declare finite operating budgets before dispatch, including independent tests, reviews,
corrections and final integration. Root may allocate and increase these internal call budgets
within authorized development without another user approval. Estimate remaining mandatory
work plus a justified correction reserve; do not double a limit without explaining the need.
Resize before capacity is exhausted, preserving capacity for verification. A depleted internal
estimate alone is not a blocker or a reason to ask the user whether development may continue.

Record each limit's origin. Explicit user ceilings on cost, time or calls and host limits are
hard constraints; Root cannot raise or bypass them. A Root-authored number in an approved
PLAN/ledger is an operating estimate unless the user explicitly made it a ceiling. Call counts
are not monetary budgets. Resolve unknown origins from actual instructions before changing
the affected limit; never invent authorization. Scope, acceptance, permissions, model/effort
assignments and publication boundaries remain unchanged. Concurrency defaults remain 4 open
child threads, 2 isolated writer workspaces and 1 integration owner; budget growth does not
increase concurrency. Incident tracking and resource locks remain procedural unless enforced.

Use 2 worker attempts per unresolved issue, 2 corrective review cycles per unit, and 2 Oracle
calls per incident / 4 per run as Root reassessment checkpoints, not user-approval gates.
At a checkpoint compare failed scenarios, causes and observations. A new reproducer, ruled-out
cause or verified affected behavior is progress; a restated hypothesis or new incident ID is
not. If progress supports another attempt, allocate a finite correction-and-verification batch
with a specific next action and success/stop criteria. Further Oracle calls need a concrete
unresolved question and new evidence or a discriminating experiment, not another opinion.
Without progress, change approach through a smaller reproducer, the existing senior worker
or targeted Oracle diagnosis. If that bounded change of approach also yields no progress and
no concrete next experiment, block the affected ticket instead of repeatedly increasing its
budget. Continue independent ready work; report a genuine impasse when no safe work remains.
Ask for user decisions only when needed to resolve scope/requirements/permissions or an
explicit user ceiling, not to approve routine internal allocation.

On an authorized resume under this policy, reconcile prior usage, reservations and limit
origins. Record the policy transition and Root's allocation decision; an old pending request
for internal budget growth need not remain an approval gate. Do not silently reclassify an
explicit user ceiling. Preserve unresolved-issue history across runs, tickets and incidents.
This policy does not authorize starting/resuming work from a guidance-only request.

Maintain an append-only invocation ledger: call ID, role, ticket/unit/incident, purpose,
configured model/effort, host-observed model/effort (or `unverified`), status and evidence.
Count every started child invocation, including failed/interrupted calls and resumed turns
that start a new assignment. Polls/messages are not new calls. Record limit, used, reserved
and available (`limit - used - reserved`) for total calls and the applicable role/incident/
unit budgets; reservations are unspent capacity and are consumed on dispatch. Oracle calls
count toward both Oracle and total budgets. Initial final review and each corrective cycle
are distinct; early reviews consume calls too. Reconcile resumed runs with host/run records.
Missing history means usage unknown, not zero: resolve affected capacity before dispatch.
Record budget changes with old/new limits, remaining-work estimate, reason, decision owner
and authority reference. For internal changes cite this policy and the existing development
authorization; do not claim a new user approval. Never reset usage or erase failed attempts.
Configured models and self-reports cannot establish the actual assignment or explain defects.

## 9. Status and resumption

Suggested phase: discovery / planning / ready / implementing / verifying / blocked / complete.
Ticket states: pending -> ready -> running -> candidate_ready -> verifying -> integrated;
blocked and stale are explicit alternatives. Worker 'done' does not satisfy an integration edge.
Final result:
- done_verified: current approved scope, tests, independent review, scope audit and required
  final checks all satisfied on the delivered integrated candidate; no material unresolved issue.
- implemented_unverified: implementation exists but necessary evidence is missing; do not
  hide an observed required failure here.
- blocked: known required failure, contract/scope/permission/capability/recovery blocker,
  exhausted explicit user/host limit, or a no-progress impasse; not a depleted internal estimate.

On interruption record active assignments, candidates, partial deltas and evidence, then stop.
No promise of future/background execution. On resume inspect the host and processes; do not
assume recorded leases are free or recorded workers remain active. Do not launch duplicate
workers for a ticket with unknown execution state. `ask-workflow` reports uncertainty and
one next step; it never writes state, releases leases, invokes Oracle, or resumes work itself.

## 10. Limits

This bundle is instruction/configuration scaffolding, not a tested worktree provisioner,
lock service, durable job runner, sandbox profile or CI gate. Static parsing cannot prove
runtime model selection, root-only delegation, permissions, actual parallel execution or
workflow compliance. Test those with the supplied smoke cases on the installed Codex build.

## 10. User-invoked bug investigation

Start `investigate-bug` only when the user invokes that skill. A bug report, repeated failure,
or a skill-explanation question is not invocation. Implement must not enter it automatically,
even by reading its instructions as an internal procedure. Ask-workflow may recommend it only.

Resolve diagnosis versus repair from the actual request. Diagnosis permits safe local repros
and isolated temporary probes, not permanent product changes. Explicit read-only restrictions
also prohibit writing diagnostic artifacts. Repair requests authorize bounded fixes and
section 7 verification without another permission question at each phase. Neither mode
permits unrelated refactors, production changes, or publication without authorization.

A brief/ticket bundle is optional. Record the user request, expected-behavior source, scope,
acceptance/checks, candidate and environment in an incident under
`.workflow/diagnostics/<incident-id>/`, respecting project conventions. These are the approved
criteria for independent verification; they do not create a parallel ticket graph. Keep
observations distinct from hypotheses and unresolved product decisions. Default to diagnosis
when an explicit invocation does not request repair. Do not reopen settled requirements.

Reuse existing evidence and link prior incidents, attempts and cumulative budgets. A new
session does not reset user limits. Do not take over another Root's ledger, active assignments
or mutation leases. Resolve ownership before mutating the same workspace. Section 8 governs
internal budgets; use senior personas for this skill's experiments/fixes and hard advice.
An absent decisive repro allows bounded observation experiments, not claims of a proven fix.

After authorized repair, apply change-verification with the incident's criteria and all
required evidence levels. Recheck the original scenario on the final cleaned candidate.
Report diagnosis or verified repair, uncertainty and artifacts; do not mark an unrelated
feature complete or resume implement. A later explicitly resumed owner reconciles the new
candidate and evidence against its full ticket acceptance. Without a productive next experiment,
report the incident blocker; do not expand into other tickets.

## 11. Operational tools and requested handoff

Skill profile 3 supplies workflow-ledger and workspace-evidence as internal Root procedures,
and handoff only on explicit user request. Resolve the shared Python helpers relative to the
installed skills, including global installs. They record evidence, not host scheduling or authority.
Separate requirements revision, candidate, invocation, check and execution state from task outcome.
Worker results candidate_ready/needs_reroute/needs_oracle/blocked are task outcomes, not
journal lifecycle states or proof of stopped processes. Assignment/result packets link via
invocation and existing transition evidence; Root checks the journal ID and actual role.
Rerouting creates a new invocation and retains all prior usage; journal v1 fields stay unchanged.
Use only the typed `workflow-invocation-journal` v1 for live accounting. Initialize explicitly;
updates never create a missing ledger. Read the compact summary directly; use full view for
diagnosis. Legacy records require explicit, source-preserving conversion, never an empty reset.
Imported baselines exclude every individually represented call; unknown counts remain null.
Keep operating/user/host limits separately and bind to their minimum. Resolve unknown scope,
usage and limit origins before the affected dispatch. Conversion retains source hashes, copies,
field mappings and uncertain facts; source interpretation is not proved by a matching hash.
Reconcile retained history and active writers before resuming; unknown starts retain capacity.
Use the common PLAN and gate validators with approved requirements. Project-specific assertions
and independent review remain necessary; zero exit status alone never establishes acceptance.

Handoff may describe a feature, incident or planning task without a PLAN or ledger. Preserve
project conventions, otherwise create a new `.workflow/handoffs/<id>/HANDOFF.md` and manifest.
Never overwrite an earlier handoff. Include purpose, goals, authority and decision evidence,
partial/failed/unverified results, active work, failed attempts, next actions and purposeful links.
Use Korean prose by default. Exclude secrets. Capture candidate and referenced record identities;
mark active writers, changing input, missing records and contradictions explicitly.
Generation does not resume work, change models, stop processes or run checks. At reception,
compare actual files, authority, ledger and evidence before acting. Reading/status requests
remain read-only; retain valid prior authorization without requiring approval merely for a new
session. Report missing fields, broken references, stale evidence, mismatches and unknowns
separately, never a single readiness score. Provide a short next-session starter and artifact path.

Handoff manifests written by the helper use version 2 and compact typed invocation snapshots
bound to the exact journal bytes/hash. They omit completed history. Version 1 manifests remain
read-only historical evidence; embedded old ledger state is opaque and does not enable legacy
operational reads. Re-read the current journal and reconcile host observations at resume.
