---
name: ask-workflow
description: "Inspect workflow evidence and recommend one next step. Guidance only; no execution or state changes."
---

# Ask workflow — evidence-based navigation

Read `.codex/workflow-contract.md`. This skill is a navigator, not an orchestrator or a
new model role. It may run in any user-selected top-level session. Do not spawn Oracle or
workers, write documents/state, claim approval, start/resume execution, or switch models.

Resolve the target feature from the explicit path and available context. Inspect relevant
BRIEF, SPEC, PLAN, ticket files, operational state, approval references and validation evidence.
If more than one feature plausibly matches and available context cannot resolve it, ask which
feature rather than guessing the most recent filename. Use read-only inspection only.

Determine stage from evidence, not merely file presence or an `approved`/`complete` flag:
- No settled intent: refine-spec in Astra.
- Agreed brief but missing/stale executable plan: to-tickets in Sol XHigh/Max.
- Plan draft: review/approve that actual revision; do not say coding is authorized.
- Approved runnable tickets: implement in GPT-6 Sol (XHigh default).
- Interrupted run: reconcile execution state and resume implement without duplicate writers.
- A user seeking a separate focused bug investigation: recommend explicit
  `$investigate-bug <symptom-or-incident>` with diagnosis/repair intent, preferably in Astra.
  Recommendation is not invocation; do not read and execute it as an internal procedure.
- Hard implementation blocker: resume the Sol owner with the incident context; Root may
  consult Oracle, but ask-workflow must not invoke it.
- Implementation exists but review/tests/integration evidence is absent or stale: resume
  implement for verification, not another requirements interview.
- Actual final evidence is current and sufficient: complete; identify remaining separately
  authorized delivery actions without executing them.

For contradictions, say unknown/stale and explain the specific missing evidence. Do not
run tests merely to answer navigation; report their recorded validity and limitations.

Return: feature, observed stage, brief supporting evidence/uncertainty, unresolved blocker,
recommended model/effort, and exactly one next command/action with a reason. A recommendation
is not permission to run it. Prefer the narrowest continuation over regenerating all artifacts.
