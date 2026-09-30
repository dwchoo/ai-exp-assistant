# Helper request formats

Run `python3 <skill>/scripts/workflow_tools.py ACTION --input request.json [--output new.json]`.
Sibling evidence.py and handoff.py expose the same CLI using the single shared module.
Outputs are JSON; ledger exits follow the ledger section. For other actions, exit 2 is invalid
input, exit 1 is a failed PLAN/gate, exit 0 is successful inspection/collection (not permission or proof of readiness). JSON output paths must be new.
Use a private operational directory and explicit project root. No dependencies beyond Python
3.11+, Git and POSIX file locks. No network, scheduler, restore or model calls.

## Ledger: exact journal v1 contract

Implemented contract (2026-09-25); verification levels and limitations are recorded separately.
Only `record_type: "workflow-invocation-journal"`, integer `schema_version: 1`,
`run_id`, `events` are accepted at the top level. No extra keys, coercion, duplicate JSON
keys, NaN or infinity. IDs (run/event/invocation/check/unresolved) match
`^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$`. Integer always excludes boolean. Counts are nonnegative.
Times are timezone-aware ISO 8601 strings. Evidence/authority are nonempty sanitized
references, never credentials or embedded logs. Unknown means JSON null, never inferred zero.

Events have exactly `id, revision, at, kind, data`; revisions are consecutive from 1,
IDs unique. The first and only opening is created by init/convert, never update.
Request events have only `id, kind, data`; the helper supplies revision/time.

### Opening and state primitives

Opening data has exactly `limits, opening_used, calls, checks, provenance, unresolved`.
`limits` is bucket → origin → entry; origin is operating/user/host. Entry has exactly
`value` (count), `authority` (reference), `reason` (nonempty string). An imported bucket may have no known origin (`{}`), provided an unresolved limit
target covers it; effective_limit/available are null and reserve is blocked globally. Buckets are `total`, `role:<id>`, `unit:<id>`, `incident:<id>`.
`opening_used` has exactly the same bucket keys, each count or null. It excludes every
invocation represented in calls, including finished calls. Unknown-origin ceilings stay
in provenance/unresolved until resolved, never in an invented origin.

`calls` maps invocation ID to an object with exactly `invocation, role, unit, incident,
purpose, model, effort, buckets, writer, workspace, state, started, outcome,
observed_model, observed_effort, evidence_refs`. Invocation equals the map key. Model/effort
are configured values. Role follows the ID rule; model/effort/purpose/workspace are nonempty strings; unit and
incident are IDs or empty strings (confirmed absence). Writer is boolean; buckets is a
unique nonempty list containing total and all applicable role/unit/incident buckets.
Imported unknown metadata may be null, including buckets. Every null imported metadata/started/buckets field has an unresolved entry; null outcome
is the normal nonterminal value and does not need one. Observed model/effort are nonempty strings or `unverified`.
Evidence_refs is a list of nonempty references. New reservations have an empty list.
States are reserved/starting/running/unknown/finished/cancelled. Started is boolean/null:
reserved and cancelled require false; starting requires null; running/finished require
true; unknown permits true/null, or false after an evidenced resolution. Finished outcomes
are complete/partial/failed/interrupted; all other outcomes are null. Started true never
reverts. A missing historic state may be represented as unknown without inventing completion.

`checks` maps check ID to exactly `check_id, path, sha256`; IDs match and SHA-256 is
64 lowercase hex digits. Check references are immutable.
`unresolved` is a list of exactly `id, target, buckets, reason, evidence_refs`.
Target is a JSON pointer into replay state (`/opening_used/...`, `/calls/...`, `/limits/...`),
using RFC 6901 escaping. Buckets is a nonempty unique bucket list, or null for run-wide
uncertainty. Reason is one of `usage_unknown`, `started_unknown`, `membership_unknown`,
`metadata_unknown`, `limit_origin_unknown`, `source_relation_unknown`. Evidence_refs is a
nonempty reference list. IDs and targets are unique. Every unknown accounted field must
be covered; a null membership blocks the whole run. Resolved entries disappear from state,
but stay in the opening/event history.

`provenance` is null for new runs, otherwise exactly `sources, converter_version,
schema_version, converted_at, actor, authority, reason, field_mappings, unmapped,
source_relationships, event_mappings, history_level, request_identity, quiescence, accounting, event_metadata`.
Sources entries: `path, sha256, detected_format, preserved_path` (all strings).
converter_version is `1`; schema_version integer 1; converted_at is a timestamp.
Actor/authority/reason are nonempty strings. Field mappings: `source, source_pointer,
target, evidence_refs`. Unmapped entries: `source, source_pointer, reason`.
Source relationships: `sources` (source path list), `relation` (independent/overlap/
aggregate/unknown), `evidence_refs`. Event mappings: `source, old_id, old_revision,
new_id, new_revision`; old IDs are preserved as source facts, never reflected in errors.
History_level is full-events/opening-summary. Request_identity is a SHA-256.
Quiescence has exactly `status, evidence_refs`; status must be quiescent for conversion.
Accounting and event_metadata retain the exact converter mapping objects and their evidence references.
Source hashes and recorded claims do not prove the agent's semantic interpretation.

### Mutation data

`limit`: exactly `bucket, origin, value, authority, reason, decision_owner,
remaining_work_estimate, expected_previous, scope_basis`. Owner is a nonempty reference;
estimate a count. Expected_previous is the exact current entry or null, compared under lock.
Scope_basis is null for existing buckets; new buckets require exactly `status, evidence_refs`,
where status is known-empty/unknown and references are nonempty. Unknown creates null baseline
and an unresolved entry with target `/opening_used/<RFC6901-escaped-bucket>`,
reason usage_unknown and the scope evidence; its ID is `scope:` plus the first 32 lowercase hex digits of SHA-256(event ID UTF-8). Operating replacement requires known used/reserved and value at
least their sum. User/host entries cannot change or disappear. Newly discovered hard ceilings
may be below existing usage: record the fact and block reserve. Effective limit is the
minimum across all origins; binding_origins lists ties in operating/user/host order.

`reserve`: exactly `invocation, role, unit, incident, purpose, model, effort, buckets,
writer, workspace`, all known. Every required bucket must exist, be known and have positive
available capacity, with no relevant unresolved entry. No automatic bucket or zero baseline.
`transition`: exactly `invocation, state, evidence, outcome, observed_model, observed_effort,
not_started_evidence`. Evidence is a nonempty string reference, appended to evidence_refs.
Not_started_evidence is a nonempty reference or null; unknown→cancelled requires a reference
and started!=true, then sets started=false. Graph: reserved → starting/
cancelled; starting → running/unknown; unknown → running/cancelled/finished; running →
finished. Unknown → cancelled needs non-start evidence and started!=true.
`check`: exactly `check_id, path, sha256`.
`resolve-import`: exactly `resolutions, authority, evidence_refs`; nonempty resolutions and
references. Each resolution is exactly `unresolved_id, target, expected_value, value`.
Only registered unknown baseline, imported call fields, and absent known-origin limit entries
may change. Null→false started needs evidence_refs containing an explicit non-start observation reference
prefixed `not-started:`; a generic observation is insufficient. It does not release reservation by
itself. Validate every patch, state combination, bucket membership and accounting invariant
in memory, then commit all or none. No arbitrary patch or change to established facts.

### Requests and views

`ledger-init`: exactly `path, run_id, limits`; initializes all explicit bucket baselines to
zero, empty calls/checks/unresolved and null provenance. It authorizes only an explicit new
run, never bypassing earlier accounting. `ledger-read`: path and optional view summary/full.
`ledger-inspect`: path. `ledger-update`: exactly `path, expected_revision, event`; an existing
valid journal is required. Identical ID/content retry succeeds even with stale expected
revision; ID reuse with changed content fails. `reconcile`: exactly path/observed, where
observed maps invocation IDs to supported states. It never performs host queries or writes.

Summary exact keys: `run_id, revision, buckets, active_calls, unresolved, usage_known,
next_action`. Buckets maps each bucket to exactly `limits_by_origin, effective_limit,
binding_origins, used, reserved, available, usage_known`. Limits_by_origin contains full
entries above. Active_calls maps nonterminal invocation IDs to exactly `state, started,
buckets`. Unresolved summary entries have exactly `id, reason, buckets, next_action`.
Usage_known is boolean. Next_action is `continue`, `resolve_import`, `reconcile_host`, or `review_limits`.
Imported unresolved takes priority, then operational starting/unknown arithmetic uncertainty
requires reconcile_host, then exhausted capacity requires review_limits. Full exact keys: `run_id, revision, limits,
opening_used, calls, checks, provenance, unresolved, usage, usage_known, next_action`;
usage uses the same bucket objects as summary.buckets. Full never embeds event payloads.

Used = opening_used + count of started=true calls in that bucket. Reserved = count of
started!=true calls in reserved/starting/unknown. Available = effective_limit-used-reserved.
Null baseline/started makes used and available null; reserved remains a count. Unknown
membership makes every bucket's usage unknown. Other unresolved entries block their scopes
without pretending that known arithmetic itself became unknown. Exhausted/overdrawn facts
are readable. Summary omits terminal calls and provenance regardless of history length.

### Explicit converter

Request exact keys: `sources, destination, run_id, mode, strategy, mapping,
preservation_directory`. Sources entries: `path, expected_sha256`. Mode check/write;
strategy journal/opening, never chosen by inspect. Mapping exact keys: `actor, authority,
reason, quiescence, field_mappings, unmapped, source_relationships, opening, accounting,
event_metadata`. Opening is opening data with provenance=null (converter supplies it);
for journal strategy it is null. Accounting maps bucket to exactly `reported_used,
baseline_used, materialized_started, evidence_refs`, counts or null. Evidence records
membership/exclusion and source aggregate relationships. Materialized_started is recomputed
from calls, not trusted. Known reported_used must equal baseline_used + computed started;
reservation is never subtracted. Unknown relationships require unknown totals/baseline.
Journal event_metadata maps old event ID to exactly `decision_owner,
remaining_work_estimate, scope_basis, evidence_refs` for every limit event. Missing evidence
returns manual_reconciliation_required; do not infer these fields from authority prose.
Field mappings must uniquely cover every `/opening_used/<bucket>`, `/calls/<id>`, and
`/limits/<bucket>/<origin>` in an opening import; source pointers must exist. Every source
pair needs a declared relationship, not separate singleton claims.
Full-history conversion compares confirmed started usage and reservation counts. Legacy
starting/unknown without start evidence becomes started=null: the confirmed count and held
capacity stay intact while exact used/available becomes unknown.
Full-history conversion prepends empty opening and preserves semantic replay and mappings;
opening conversion does not fabricate earlier events. Known invalid legacy types are rejected.

Check validates and returns a preview without creating directories, locks or files. Write
revalidates sources and candidate under the destination lock, preserves source bytes with
fsync and verifies copies, rechecks source pathname/inode/hash, then atomically publishes a
create-only destination and fsyncs its parent. Existing destinations always fail, including
retry after response loss. Read provenance to determine whether publication succeeded.
Preservation is a dedicated new directory; partial retries require the same request identity
and matching regular copy hashes, never overwrite/delete conflicts. Identity hashes canonical
source paths/hashes, destination, strategy, run_id and mapping (excludes mode/time).
Symlinks, special files, source inode duplicates, source/destination/backup aliases are rejected.
Stop source writers → check → write → compare replay/source/mapping → switch references →
confirm no old writers. The lock cannot enforce external writer quiescence.

### Errors, exits and persistence

Ledger failures return only `error`, whose exact fields are `code, action, message, path,
json_path, expected_record_type, supported_schema_versions, detected_format, usage_known,
mutation, next_action, event_index, reason, phase, revision, event_id, expected_type, missing_keys, unexpected_key_count`. Nullable location/classification fields
use null. Revision is a nonnegative integer or null; event_id is a safe ID or null. Event_index is a zero-based integer or null. Expected type is the journal string;
supported versions is [1]. Mutation is exactly `ledger, artifacts`: ledger none/committed/
unknown, artifacts none/created/unknown. Messages/reasons/actions are fixed sanitized strings;
never echo payload, unknown keys, raw exception or credential-bearing paths.
Codes: file_not_found/file_unreadable/invalid_json/invalid_request/conversion_required/
unsupported_record_type/unsupported_schema_version/invalid_ledger_shape/invalid_event_history/
invalid_event_transition/stale_revision/event_id_conflict/capacity_unknown/capacity_exhausted/
manual_reconciliation_required/source_changed/destination_exists/write_failed/output_failed.
Every successful ledger response also includes mutation. Init/update return summary plus mutation.
Python ledger_read(path) returns (journal, full); ledger_update(path, expected_revision, event)
returns summary plus mutation; the CLI adapts read to the requested view. Inspect separately reports
recognized/supported/valid/usage_known/next_action and detected_format. Candidate detection
is diagnostic only: untyped-journal, legacy-snapshot-candidate, auxiliary-summary-candidate,
unknown, workflow-invocation-journal. It never selects converter mapping/strategy.

Exit 0: every successful read/init/update, including unknown usage, and every completed
inspect diagnosis, including decoded unsupported/invalid records. Exit 1: reconcile
mismatch/unrecorded/unknown and valid conversion with unresolved facts. Exit 2: malformed
request/file/schema/history, unsupported normal operation, rejected mutation or persistence
failure. Inspect I/O/decode failures exit 2. Known conversion/reconciliation exits 0.
Nonledger gate/evidence/handoff exits remain unchanged.
Update validates request and entire existing journal before creating lock, then rereads under
lock. Preflight failures create nothing. Lock inode remains after later failures; flock is
released. Replace/publish is commit point. Failure of parent fsync makes durability unknown.
Output is preflighted as a new regular destination before any action, rejecting aliases and
ancestor/descendant conflicts with ledger/source/lock/preservation paths. It is independently
fsynced and create-only published; never expose partial JSON. An output failure after durable
ledger commit reports output_failed, phase=output, mutation.ledger=committed. Broken stdout
may prevent delivery; recover by event ID or destination provenance, never assume rollback.

## Workspace

`snapshot`: `{"root":"/project","watch":["ignored/generated"],"exclude":[".workflow/run",".workflow/handoffs"],"checkpoint":false}`.
Default observation includes all tracked/non-ignored files, not merely the writer's scope.
Watch adds literal ignored paths; exclude accepts project-relative paths/globs for operational
outputs only. Do not exclude validation inputs. Use the same watch/exclude for the whole run.
Checkpoint=true adds base64 blobs addressed by SHA-256 for worktree and index contents.
It does not restore. Modes, symlink targets, missing files, index stages and Git base are retained.
A separate submodule checkpoint is required for dirty submodules; stability otherwise fails.
`audit`: before/after manifest paths plus allowed path/glob list. Inspect outside_scope and
comparable; policy differences require recollection. Renames appear as removal plus addition.

## Requirements, PLAN and checks

Approved requirements JSON has exactly schema_version:1, revision, authority (actual request
reference), acceptance_ids (nonempty unique list), and gate_items. Each required gate has id,
acceptance_ids and required_evidence_level: static, fixture, skill_behavior or runtime.
The tool checks structure/identity, not whether the authority claim is true.

PLAN adds requirements_identity equal to the helper's requirements digest. `plan` input:
`{"plan":"PLAN.json","requirements":"requirements.json"}`. On mismatch the output includes
the expected identity; record it only after confirming actual approved requirements. Keep
PLAN's existing ticket and gate fields. Missing prior requirements stay unverified.
Optional ticket `routing_hint` must be an object with exactly `reason_codes` and `evidence_refs`,
each a list of nonempty strings (empty lists allowed). Existing PLANs need no hint or rewriting.
This validates shape only, not role choice or evidence truth; `worker` remains a recommendation.

`check`: root, requirements (path), argv (list, no shell expansion), environment (sanitized
nonempty description/map), log (new file), check_id, optional watch/exclude/timeout seconds.
The command executes only when authorized. Choose a log path inside the declared operational
exclusion, never put secrets in argv or environment descriptions. Review logs before sharing.
Timeout kills only the owned process group. Record file output outside candidate inputs.
The result initially has items=[]: exit code alone cannot supply acceptance assertions.

Root adds item observations in a new check record: id, acceptance_ids, result (passed/failed/
not_run), observed_evidence_level, evidence (sanitized supporting reference), unknowns (list).
Preserve the original execution record. `gate`: requirements path, current candidate ID, checks
(list of record paths). Supply all applicable records; incompatible duplicate results are
reported rather than silently selecting a pass. Logs must exist and match their recorded hash.

## Handoff

`handoff` input: root, output (new project-relative directory), optional watch/exclude, draft.
Draft fields: target, purpose, summary, authority, status, active_work, attempts, next_steps
(all prose); references (list of {path, why}); optional previous (project-relative HANDOFF.md),
ledger (path), checks (list of paths), writers ({status, observed_at, evidence}).
Writers status quiescent requires actual observation; active/unknown forces reconciliation.
Each prose section must state its evidence or explicit unknown; missing sections remain marked.
References are project-relative canonical files, never secrets or paths outside the project.
The output directory is excluded to avoid self-reference; predeclare the handoff parent in
all run snapshots/checks to preserve comparable candidate policy.

`handoff-validate`: `{"root":"/project","directory":"/project/.workflow/handoffs/id"}`.
Returns missing, reference_errors, stale, mismatches, unknowns separately, with no score.
A historical writer observation always needs live reconciliation at resume. No checks are run.

## Exact version 1 validation

Records are never coerced. Nonempty strings must contain non-whitespace characters.
schema_version is integer 1 (not boolean); all candidate/requirements/log hashes are 64
lowercase hexadecimal characters. stable/timed_out are booleans; exit_code is an integer,
not boolean (negative signal exits are valid failed executions). argv is a nonempty list:
argv[0] is nonempty, subsequent arguments are strings and may be empty. cwd/log are absolute
paths. environment is a nonempty string or object. Timestamps are timezone-aware ISO strings.
The exact check keys are schema_version, check_id, requirements_identity, argv, cwd,
environment, started_at, finished_at, exit_code, timed_out, log, log_sha256, before, after,
stable, items. Each item has exactly the six fields documented above; evidence is a nonempty
string, acceptance_ids is a nonempty unique string list, unknowns is a string list (even an
empty string member remains an unknown). Empty items supplies no acceptance evidence.
Requirements and gate objects have exactly the documented fields, unique nonempty IDs,
nonempty unique acceptance lists and supported evidence levels.

Gate preflights every decoded record, including records without a matching item. A malformed
record cannot supply a pass. Record issues carry check_index (zero-based), kinds, details,
and check_id when valid; CLI adds check_path. Missing gate evidence can coexist with these
issues. Valid extra items are structurally checked but do not count for required gates.
Unreadable files/invalid JSON or malformed request requirements/candidate/checks list exit 2
without a partial gate result; decoded malformed records exit 1. Input/gate order is retained.
Check requests validate before command execution or log creation, including finite positive
timeout excluding boolean. Logs must be regular files (no symlink, FIFO or directory).
CLI requests must be objects. Existing output files, invalid/dangling output parents, and
an output path identical to the check log are rejected before execution. Concurrent output
changes and later I/O failures still require reconciliation; preflight is not a transaction
over the product command. Handoff documents and manifests also require regular files and
are opened nonblocking, so a special file is reported rather than waited on.

Manifest has exactly schema_version, created_at, target, purpose, candidate, stable,
references, records, writers, unknowns, changed_references, sections, document_sha256.
Candidate is the snapshot without checkpoint blobs: schema_version, candidate, identity,
unknowns, observed_at, workspace, stable. Identity contains files, index, head, watch, exclude;
an unborn Git HEAD may be empty. File variants retain their capture fields: deleted (kind),
file (kind/mode/sha256), symlink (kind/mode/target), submodule (kind/head/status). Index entries
contain path/mode/oid/stage and sha256 except for submodules. References have path/why/sha256
(null hash means missing at collection). Sections is the six-key boolean map; writers has
status/observed_at/evidence. Unknowns and changed_references are string lists. New manifests use integer schema_version 2. Records dispatch first by record_type, then
exact check schema, then an exact one-field unrecognized_record wrapper. Ledger snapshot
exact keys are record_type="workflow-invocation-snapshot", schema_version=1, ledger,
limits_by_origin, usage, active_calls, unresolved. Ledger has exactly path/sha256/run_id/revision;
limits_by_origin maps buckets to origin maps, usage contains the summary bucket objects,
active_calls/unresolved use summary shapes. Hash and snapshot derive from the same journal
bytes. No full-state record is accepted as a snapshot. Unknown record_type remains unknown.
Manifest v1 stays read-only diagnostic: old ledger objects are opaque historical records,
separate from whether their referenced journal now needs conversion. No old runtime replay.

Handoff validation reports missing keys/artifacts as missing; unsupported fields, versions
and types as unknowns with JSON paths. Unsupported versions are not interpreted as v1.
Independent valid subtrees continue after other errors. Manifest JSON damage is unknown;
read failures and invalid/nonregular references or logs are reference_errors. Changed log
or document bytes are mismatches; changed references/workspace or check before/after are
stale. Candidate digest is recomputed from its validated identity. Failed execution alone
is not artifact damage. A categorised damaged artifact exits 0, never meaning readiness;
invalid CLI path arguments exit 2. Original records and older handoffs remain untouched.

A resolve-import null→false start assertion requires an evidence reference prefixed
`not-started:`. The reference is retained on the call. An imported unknown call with
started=false likewise needs this evidence. Normal host transition evidence resolves only
the imported started target it actually confirms. CLI request streams are accepted only
through `/dev/stdin`, `/dev/fd/N` or `/proc/self/fd/N`; ordinary request paths must be regular files.

Successful `--output` results report artifacts=created in both stdout and the published JSON.
Temporary-file cleanup never changes a known commit outcome. If cleanup fails after durable
publication, the successful result still reports committed/created and the internal temporary
artifact may remain. If a preservation directory exists without conversion.json, identity
cannot be established: retain it for inspection and choose a new dedicated preservation path.

Errors use expected_type=null for semantic constraints or a fixed JSON type name for type
failures. Missing_keys lists sorted schema-known names only; unexpected_key_count reports
the number of extra keys without revealing their names or values. Nonobject input has
expected_type=object and empty missing_keys. Request and journal checks share this diagnostic.
