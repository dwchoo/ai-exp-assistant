# Bug investigation record

Use `.workflow/diagnostics/<incident-id>/incident.md` unless the project has a convention.
Keep one record per incident; link prior evidence rather than copying a feature's ledger.
Omit inapplicable fields. For explicit read-only work, return the information in the response.

## Scope and identity

- User request/reference and diagnosis or repair scope:
- Symptom, expected behavior and its contract/source:
- Acceptance checks and required evidence levels:
- Related ticket/run/incident and earlier attempts:
- Workspace, base, candidate (including dirty/new files), environment:
- Actual writer/resource ownership and pending processes:

## Evidence and experiments

- Original reproducer or observation command, cwd, candidate, result, sanitized evidence:
- Minimized case and what it does/does not reproduce:
- Hypothesis, supporting/contradicting observations, prediction:
- Discriminating experiment, outcome, next action:
- For nondeterministic/performance cases: conditions, baseline, sample counts and criterion:
- Confirmed causes versus unresolved hypotheses:

## Execution record

- Prior cumulative usage, linked source, new calls and current reservations:
- Internal budget/user ceiling/host limit and its actual origin:
- Root reassessment, progress or approach change:
- Owned temporary artifacts/instrumentation/processes and cleanup:

## Outcome and handoff

- Diagnosis finding or repair delta, final candidate:
- Independent test assessment and code+tests review/delta review evidence:
- Original scenario and integration checks (command/cwd/environment/result/log):
- Missing evidence, blockers and next useful experiment:
- Related feature owner may consume this result on explicit resume; no automatic resume:
