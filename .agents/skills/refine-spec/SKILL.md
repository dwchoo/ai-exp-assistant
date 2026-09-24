---
name: refine-spec
description: "Interview and document a repository idea into an approved portable brief, domain glossary and selective ADRs. User-selected Astra session; no implementation or ticket execution."
---

# Refine spec — interview plus domain modeling

This is the discovery phase, inspired by grill-with-docs, not a final ticket generator.
Assume the user selected Astra. Do not try to change the model or reroute the conversation
to a planner. Read `.codex/workflow-contract.md`; a delegated leaf must return to Root.
Read existing instructions, the relevant glossary/CONTEXT-MAP, ADRs, feature artifacts and
provided evidence. Preserve prior answers; do not restart a settled interview.

## Interview

Map decisions as a design tree. At each round ask the current frontier: questions whose
prerequisites are settled. Dependent questions wait for later rounds. Number questions,
include alternatives, trade-offs and a recommended answer; wait for user decisions. Keep
questions concrete. Group a large frontier by topic or split it when the user prefers;
do not invent a hard rule that only one question may ever be asked.

Find observable facts through tools or a bounded `code_explorer` assignment instead of
asking the user to recite available code. Distinguish observed behavior from desired behavior.
If an exploration is pending, other independent questions can proceed; do not assert its
result. Surface contradictions rather than choosing silently. Investigate uncertainty;
do not manufacture certainty from the user's proposed answer.

## Document as agreement emerges

Challenge overloaded terminology against existing definitions and use concrete edge cases.
Record resolved domain terms immediately in the applicable CONTEXT.md, glossary only.
Record ADRs sparingly for hard-to-reverse, otherwise surprising choices with genuine
alternatives. Do not create an ADR for every small implementation detail.

Keep `BRIEF.md` using `references/brief-template.md`. Capture agreed goal, users, scope,
non-goals, constraints, acceptance examples/IDs, error behavior, important interfaces,
testing intent, decisions and rationale, known evidence, and open/deferred items. These
are inputs to implementation planning; do not pretend concrete file ownership or scheduling
has already been validated. Link glossary and ADRs rather than copying conflicting versions.
Do not automatically install upstream skills, use a vendor-specific Skill tool that the
host lacks, or require an issue tracker just to save local documentation.

## Handoff

Present the agreed brief and unresolved/deferred decisions. Finish when the agreed scope's
blocking questions are resolved or the user explicitly defers them with consequences.
Record genuine user approval and revision in a separate handoff/state record; never approve
on the user's behalf. An approved brief is not an implementation request.
Return the brief path, decision summary and next command:
`$to-tickets <brief-path>` in the user's fresh Sol XHigh/Max session.
Do not start that session or invoke to-tickets. No product/test changes, commits or publication.

## Evidence notes

For each material statement in the brief, distinguish a repository observation from a
user-agreed decision or a deferred question. Link observations to the inspected file or
command and its revision when available. If code contradicts the agreed behavior, record
the gap explicitly; do not silently turn current behavior into the requirement or reopen
settled decisions that are unaffected by the gap.
