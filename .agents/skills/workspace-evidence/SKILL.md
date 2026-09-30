---
name: workspace-evidence
description: "Root-internal candidate manifests, recovery checkpoints and write-scope audits for authorized workflow work."
---

# Workspace evidence

Read `.codex/workflow-contract.md` sections 5, 6 and 11. Use
`scripts/evidence.py snapshot --input request.json` relative to this skill. The shared
[formats](../workflow-ledger/references/formats.md) describes inputs and audit requests.

Resolve the actual Git workspace root, relevant ignored inputs and operational exclusions.
Observe the whole relevant workspace; allowed write scope is a separate audit input. Default
Git discovery includes tracked and non-ignored new files, including outside the allowed scope.
Add relevant ignored/generated paths to watch. Do not exclude tests, fixtures or configuration.
Reuse the same observation policy across checkpoints, checks and handoff comparisons.

Before a writer use checkpoint=true to retain bytes including index content; for checks use
small manifests. Stop/reconcile writers through the existing owner before claiming a frozen
candidate. A stable double collection does not prove no active writer exists. Dirty submodules
need their own snapshot/checkpoint. Audit additions, removals, content, modes, links and index.

Never restore automatically or reset/clean/stash user work. Checkpoints contain sensitive
project bytes: keep them in local private operational storage. Unknown input or collection
changes invalidate stability. Report scope violations and preserve evidence for safe attribution.
