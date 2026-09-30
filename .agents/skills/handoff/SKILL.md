---
name: handoff
description: "Create a codex-swarm model/session handoff with evidence and resume checks, only when the user requests handoff."
---

# Handoff

Start only on a user request to create a handoff, such as `$handoff <target> [next-session purpose]`.
A model change, completed phase, large context or another skill's internal procedure is not a
trigger. Reading a handoff or asking for status does not authorize creation or implementation.
Read `.codex/workflow-contract.md` section 11. Honor valid existing authorization; a new session
alone is not a reason to repeat approval. Preserve explicit pauses and changed scope.

Resolve feature, incident or planning target and the next session's purpose from context.
BRIEF, SPEC, PLAN and ledger are optional; absence is information, not permission to invent them.
Inspect current files and recorded evidence without running tests, stopping processes, switching
models or resuming implementation. Check actual writer observations where available; if unknown,
say so. Do not copy secrets or raw environment/log dumps.

Use the project's handoff location or `.workflow/handoffs/<new-id>/`. Preserve earlier handoffs
and link them as history without requiring the receiver to traverse the chain. Draft Korean
prose with the [request format](../workflow-ledger/references/formats.md):
- Goal, current position, handoff purpose and expected next-session result.
- User decisions, reasons, constraints and request/authorization evidence; distinguish proposals.
- Complete, partial, failed and unverified work tied to candidates and checks. A past pass is
  not necessarily current. Preserve contradictions and exact limits of partial verification.
- Dirty/new files, workspaces, assignments, processes, reservations and observation times.
- Failed approaches, counterexamples, ruled-out hypotheses and conditions for retry.
- First checks, concrete next actions, completion criteria and blockers.
- Canonical paths and when/why to read each, rather than duplicated source documents.

Use `scripts/handoff.py handoff --input request.json` relative to this skill. The helper collects
identities and recorded results; you supply rationale and meaning. Inspect its categorized
validation, complete missing content, and create a new version if a finalized document changes.
Active writers or changed inputs mean reconciliation required. Never turn a document score
into resume permission. Return the document path and the helper's short starter; the user
chooses the model and when to begin.

On reception use this codex-swarm skill and its `handoff-validate` helper; no external
session-handoff skill is needed. For read-only requests pass JSON on stdin with
`--input /dev/stdin` instead of writing a request file. Compare actual authority, files, ledger, processes and
check evidence. Report missing fields, broken references, stale evidence, mismatches and
unknowns separately. A read/status-only request ends with findings. Resume only within the
current request and retained valid authority. Do not silently choose one conflicting record.

New manifests use version 2. Ledger records are compact typed snapshots tied to the exact
journal bytes/hash; they omit completed calls and source bodies. A snapshot is a past
observation, never a replacement for a current ledger read and host reconciliation. Version 1
embedded ledger state remains opaque historical evidence; report its age separately from
whether the referenced live ledger needs explicit conversion. Do not feed full-state responses
or old embedded records to the operational journal reader, or rewrite historical handoffs.
