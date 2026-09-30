---
name: change-verification
description: "Root-only independent testing, code review and final candidate verification used inside implement or an explicitly requested investigate-bug repair. Preserve read-only review requests; never turn review-only into test/code writes."
---

# Change verification — internal procedure

Read `.codex/workflow-contract.md`. Root performs this procedure; leaf agents return results
instead of delegating. Establish mode, approved criteria, verification-unit scope, candidate
and environment. Explicit review-only mode permits inspection/findings only: no tests-as-code,
fixes, integration or implementation dispatch. Do not call implement recursively.

For investigate-bug repairs, use the incident's user request, behavior criteria, scope and
required evidence instead of a ticket SPEC. Keep worker_senior for production corrections.
Return results to the invoking Root without starting implement or modifying its ledger.
Diagnosis-only does not authorize production fixes or this repair pipeline.

## Independent tests

In authorized implementation mode, transfer the workspace mutation lease only after the
previous writer and subprocesses stop. Dispatch `test_designer` with acceptance IDs,
requirements and test-only write scope. Expected outcomes come from contracts first.
`test_designer` must add/update meaningful tests for new behavior or uncovered regressions; proven
existing coverage can be reused with named evidence. Documentation-only work can justify
no executable test changes. Audit test delta against its baseline.

Classify failures by implementation/test/environment/contract using evidence. For a valid
failing test Root assigns a production fix to the caller-selected writer (`worker_senior` for investigate-bug), preserving independent acceptance tests.
For a hard diagnosis Root may consult Oracle under the common budget. Do not let fixes
silently alter the desired behavior or turn failed tests into skipped tests.
Root reserves the next correction, independent tests and review together. Internal call
budgets can be adjusted under contract section 8 without a separate user request; explicit
user/host ceilings cannot. Reaching a retry checkpoint requires progress/approach reassessment,
not skipping verification or asking permission for a routine additional correction.

## Frozen review

Early technical review answers one concrete feasibility question; it is optional and consumes
the call budget. It does not approve a ticket or substitute for the final independent review.
If required checks are failing or decisive runtime evidence is missing, return to the bounded
experiment/fix rather than repeatedly final-reviewing partial candidates.

After tests and required production fixes, freeze the actual code+tests candidate. Dispatch
a fresh `reviewer` with SPEC/criteria, complete relevant diff including new files, base,
candidate identity and source context, not a worker's self-evaluation. Read-only reviewers
may inspect separate stable candidates concurrently; no writer mutates the reviewed inputs.

Root adjudicates concrete findings. Route accepted bounded fixes to a writer. If requirements
or accepted design change, pause affected work. Material subsequent deltas need delta/impact
review and rerun affected checks. If impact cannot be bounded, review the unit again. No
endless reviews without new code, evidence or uncovered scope.
Carry forward unaffected original coverage. Give delta reviewers the original findings,
dispositions, changed inputs and affected contracts. Record a concrete impact reason before
expanding to another full review. Optional improvements are non-blocking unless tied to an
approved criterion. Passing required coverage ends this review cycle and returns to integration.
Repeated failures without progress require a different bounded diagnosis/correction approach.
If that yields neither progress nor a concrete next experiment, return the ticket blocker and
preserved findings to Root rather than starting another unchanged cycle.

## Integration and final evidence

Isolated unit verification is provisional until integrated. Root integrates under exclusive
integration ownership, audits the delta, checks cross-ticket contracts and runs relevant
integration tests. New merge/conflict changes require review coverage. Include final feature
acceptance and required whole-repository checks where the approved verification plan requires.
Record candidate before/after each check, command/cwd/environment/exit code/result/log and
acceptance coverage. Stale or missing evidence cannot become a final pass by relabeling.
Link structured gate results in ledger `checks` and compare them with the approved required
items and evidence levels. Reject a fixture-only pass for a runtime requirement; report a
missing project validator as an enforcement gap, not as validated gate execution.
Return evidence and gaps; Root declares the contract's final status. No remote publication.

## What the evidence establishes

`test_designer` should exercise the agreed public behavior with contract-derived expected outcomes,
including a relevant failure or regression. Avoid assertions that recompute the same
implementation or replace the behavior under test with a mock. Use controlled doubles at
external or nondeterministic boundaries and state which integration remains untested.
Reuse the approved test seams without requesting approval again for routine test work.

Label parser and file checks as static, controlled helper executions as fixture tests,
model responses and tool actions as skill behavior evidence, and actual host sessions as
runtime evidence. A result at one level does not establish a result at the next level.
Keep independent testing and frozen review even when a newer prompting guide recommends
less repetitive checking; scale the checks to the change without weakening acceptance.

Use `scripts/verify.py check --input request.json` to record authorized check execution and
`gate` to compare all current evidence with approved required items; read the shared
[formats](../workflow-ledger/references/formats.md) when constructing requests. Exit code 0
is not an acceptance assertion. Preserve execution records and add explicit item observations,
unknowns and evidence in a new record. Record the result path/hash through workflow-ledger.
Read-only review/navigation uses recorded evidence only and never runs the check command.
