# Dispatch / Oracle evidence packet

These are proposed handoff fields, not Codex tool arguments. Supply them using the installed
host's actual supported messaging/cwd mechanisms. Do not invent spawn parameters.

## Assignment
- feature_id, ticket_id, attempt_id, approved_bundle_digest
- actual_workspace, repository_identity, baseline_snapshot, effective_write_capability
- acceptance_ids, relevant_contracts, allowed_write_paths, prohibited_paths
- generated_outputs, resource_claims, mutation_owner, checkpoint_ref
- exact_checks, reserved_budget, stop_conditions
- feasibility assumption/experiment/result reference when applicable
- configured_model/effort; host_observed_model/effort or unverified, with metadata reference

## Result
- ticket_id, status: candidate_ready / needs_oracle / blocked
- actual_workspace, base_snapshot, candidate_id, attributable_output_bundle
- changed_paths, scope_audit, checks with command/cwd/exit_code/result/evidence
- active_or_stopped_subprocesses, remaining_gaps
- gate_result_refs, original_review_ref, delta_review_scope when applicable

## Run accounting and gate results

Keep these as Root-owned ledger entries, not worker-maintained status copies:

- calls: call_id, role, ticket/unit/incident, purpose, status, model observations, evidence_ref
- budgets: scope, limit, limit_kind (operating_estimate / user_cap / host_cap / unknown),
  origin_ref, used, reserved, available; usage_unknown if history is incomplete
- budget_changes: scope, old_limit, new_limit, remaining_work_estimate, reason, decision_owner,
  authority_ref (Root policy plus development authorization, or explicit user decision)
- reassessments: incident/unit, prior attempts, observable progress, next action or changed
  approach, finite batch allocation including verification, success/stop criteria
- checks: gate_id, candidate_id, requirements_digest, result_ref

Count all started calls, including failures and early reviews; Oracle consumes both Oracle
and total capacity. A reservation becomes usage on dispatch. Remaining capacity cannot be
derived from reservations alone. Separate initial final review from corrective cycles.
Before increasing an operating estimate, account for remaining implementation, independent
tests/reviews, integration and correction reserve. Root records and proceeds under contract
section 8; no new user approval is needed. Do not increase a user/host cap or change concurrency.
On resume resolve legacy limits from their actual origin, retain all usage and record any
policy transition. Do not relabel unknown limits as adjustable or fabricate an approval.

Each gate result resolves to required item IDs from PLAN, observed evidence level,
command/cwd/environment/exit_code, candidate before/after, result, evidence_ref and unknowns.
The project runner validates completeness and candidate identity before accepting `passed`.
An empty checks list, a schema file, or fixture success is not required live runtime evidence.

## Oracle request (always routed by Root)
- incident_id, ticket_id, spec_revision, candidate_id
- expected_behavior, observed_behavior
- reproducer_and_logs (or explicit missing-evidence statement)
- attempted_approaches and observable outcomes
- constraints, unknowns, precise_question

## Oracle response
- supported_observations, candidate_hypotheses, uncertainty
- discriminating_experiments, minimal_fix_direction
- regression_test_ideas, integration_risks, plan_invalidated, remaining_unknowns

Oracle does not mutate the code/state or mark an incident resolved. Root authorizes and
runs/dispatches experiments and fixes, then verifies them independently.
