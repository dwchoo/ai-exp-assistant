# Dispatch / Oracle evidence packet

Handoff fields, not Codex tool arguments. Use supported host messaging/cwd mechanisms.
Keep immutable packets with existing attempt evidence; no separate routing ledger.

## Assignment
- feature_id, ticket_id, attempt_id, invocation, approved_bundle_digest
- actual_workspace, repository_identity, baseline_snapshot, effective_write_capability
- acceptance_ids, relevant_contracts, allowed_write_paths, prohibited_paths
- generated_outputs, resource_claims, mutation_owner, checkpoint_ref
- exact_checks, reserved_budget, stop_conditions
- feasibility assumption/experiment/result reference when applicable
- configured_model/effort; host_observed_model/effort or unverified, with metadata reference
- routing: planned_role (original recommendation, or null), selected_role (current dispatch ID),
  reason_codes, evidence_refs. Preserve legacy aliases in planned_role.
- prior_assignment_ref on reassignment; reference previous result and stop evidence

Attempt and invocation are distinct identities. Existing bundle/run evidence identifies the
instruction bytes used. Root compares actual role, pins, workspace and budget with the packet,
reserves the same invocation/role, and links the assignment in starting transition evidence.
Failed reserve forbids dispatch. Starting is not proof of a successful start; host observations
support subsequent transitions. Unknown starts retain capacity. Reconcile legacy results
without invocation against assignment/host evidence; never invent a missing connection.

## Result
- invocation, ticket_id, status: candidate_ready / needs_reroute / needs_oracle / blocked
- actual_workspace, base_snapshot, candidate_id, attributable_output_bundle
- changed_paths, scope_audit, checks with command/cwd/exit_code/result/evidence
- active_or_stopped_subprocesses, remaining_gaps
- gate_result_refs, original_review_ref, delta_review_scope when applicable
- for needs_reroute: requested_role, reason_code, remaining_problem, evidence_refs

## Accounting and gates

Use the existing journal v1 [formats](../../workflow-ledger/references/formats.md) and
common contract section 8 for limits, usage, reassessments and gate evidence. No routing
fields or task statuses enter journal schema. Transition evidence links packets and actual
host observations; Root checks packet invocation/selected_role against the journal call.
A result status does not establish process termination or ticket integration.
Required gate results retain PLAN item IDs, candidate identity, evidence level,
command/cwd/environment/exit_code and unknowns. Fixture/schema success is not live evidence.

## Oracle request (Root-routed)
- incident_id, ticket_id, spec_revision, candidate_id
- expected_behavior, observed_behavior
- reproducer_and_logs (or explicit missing-evidence statement)
- attempted_approaches and observable outcomes
- constraints, unknowns, precise_question

## Oracle response
- supported_observations, candidate_hypotheses, uncertainty
- discriminating_experiments, minimal_fix_direction
- regression_test_ideas, integration_risks, plan_invalidated, remaining_unknowns
