---
name: investigate-bug
description: "Investigate a specific bug through reproduction and discriminating experiments; repair and independently verify when requested. Explicit user invocation only, not automatic implement recovery."
---

# Investigate bug

Read `.codex/workflow-contract.md`, especially sections 8 and 10. Start only when the user
invokes this skill, not on an ordinary error report, a question about skills, or an implement
failure. Another skill must not bypass this boundary by loading these instructions internally.

Recommend a user-selected Astra Root with `oracle_senior` for hard read-only advice and
`worker_senior` for experiments and repairs. Keep `test_designer` and `reviewer` independent.
These are usage recommendations, not model/config changes: honor the current user-selected
session, leave Root effort to the user, and do not stop merely to request a model switch.
Check actual role availability; never invent support or silently relabel a fallback.
Only Root delegates; all personas remain leaves.

## Establish the incident and scope

Resolve the symptom, target and expected behavior from the request and repository evidence.
Read existing attempts, logs, tests and relevant contracts/ADRs before asking for available facts.
A ticket or BRIEF is not required. Use [the incident template](references/incident-template.md)
when recording a new investigation; reuse project conventions and existing incident links.
Do not duplicate a ticket graph or take over another Root's ledger or assignments.

- Diagnosis is the default if repair was not requested. It permits safe local reproduction,
  existing tests and isolated temporary probes, not permanent product changes.
- A repair request authorizes the bounded fix and required independent verification. Do not
  ask again between diagnosis, repair, testing and delta review.
- An explicit read-only request also prohibits diagnostic file writes. Report in the response
  and use only observations that satisfy the restriction.

Preserve user edits. Establish workspace/base/candidate and writer ownership before experiments;
do not overlap an active writer or its mutating subprocesses. Existing implement evidence is
input, not permission to resume that run. Record the real request and behavior/check criteria,
not a fabricated approval flag. Ask only for material behavior decisions or missing authority.

## Find a signal for this bug

Choose the smallest useful loop: an existing failing test, real CLI/API invocation, replay,
or a temporary harness exercising the relevant production path. Verify that the observed
failure matches the reported symptom. A mock replacing that path is not a reproducer.
Record command, cwd, environment, candidate, result and sanitized evidence. Never include
credentials in commands or retained captures; keep only the signal needed for diagnosis.

Minimize inputs while retaining the failure. Reuse adequate evidence instead of rebuilding it.
If no reproducer exists, bounded code investigation and Oracle advice may help design an
observation experiment; do not call an untested theory a confirmed cause. Missing access or
an exhausted productive approach is a concrete blocker, not proof that the bug does not exist.

For intermittent failures, record seed, conditions, attempts and failure counts before/after.
Use controlled stress or scheduling changes only when they preserve the relevant behavior.
Decide the comparison criterion before inspecting the result; a few passing runs are not
proof of absence. For performance regressions, measure representative inputs against a
baseline repeatedly and separate noise from a material change.

## Distinguish causes through experiments

Separate observations, hypotheses and unknowns. Rank plausible alternatives without inventing
a fixed number. Each hypothesis needs a prediction and an experiment whose outcomes distinguish
it from alternatives. Classify code, test, environment and contract failures using evidence.

Root may consult `oracle_senior` with the symptom, candidate, expected behavior, attempts,
results and a precise unanswered question. Request predictions, discriminating experiments
and next actions for each outcome. Root assigns experiments to `worker_senior`; Oracle never
writes product code or approves a fix. New consultations need new observations or a genuinely
unanswered question, not the same failed packet sent again.

Change one explanatory variable at a time. Prefer targeted inspection/probes to broad logging.
Tag temporary instrumentation and track its baseline so cleanup removes only this incident's
changes. Bound execution, drain subprocess output, and clean up owned processes; a stuck probe
can be the fault in the experiment rather than the product.

Keep cumulative attempts and budget origins linked to prior work. Under contract section 8,
Root adjusts finite internal capacity and reserves independent verification without routine
approval waits. Report hypothesis/experiment choices and proceed within scope. Reassess at
retry checkpoints; change approach when no useful evidence appears. If the bounded alternative
produces neither progress nor a concrete next experiment, report the incident blocker and stop.
Do not reset counts, exceed user/host ceilings, or start unrelated tickets.

## Diagnose or repair, then close

For diagnosis-only, clean up temporary changes/resources and report supported causes, unresolved
hypotheses, evidence, the smallest repair direction and remaining experiments. Diagnosis complete
does not mean the product bug is fixed.

For repair, assign the smallest supported fix to `worker_senior`. Where possible, demonstrate
the real regression failing before the fix and passing afterward. Do not weaken expected
behavior, skip failing tests, or include unrelated refactors. A missing test seam is a coverage
gap; it does not waive required evidence. A wider design decision needs the user's decision.

Apply `../change-verification/SKILL.md` with the incident's behavior criteria, scope and evidence
requirements in place of a ticket SPEC. Keep `worker_senior` for production corrections.
Independent tests assess whether the real defect is detectable; review a stable code+tests
candidate only after required checks pass. Rerun affected checks and delta review as needed.
Clean up temporary instrumentation before final verification, rerun the original unminimized
scenario and relevant integration behavior, and identify the final candidate including dirty files.

Return the investigation outcome, actual changes, checks/review evidence, uncertainty and incident
path. Missing runtime evidence stays missing. Do not mark an entire feature complete, change its
ledger, or start/resume implement. A later explicitly resumed owner can reconcile this evidence.
No commit, push, deployment, installation or production mutation without authorization.
