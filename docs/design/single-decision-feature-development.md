# Single-Decision End-to-End Feature Development

## Purpose

This document expands the feature-development example in
`llm-execution-language-safety.md` to at least the scope represented by:

- `skill-definitions/specify-a-feature.yaml` and its `design-interview` child;
- `skill-definitions/start-implementing-feature.yaml`;
- the `execute-proposed-pr` changes in
  [pull request #660](https://github.com/powdrr-io/powdrr-lift/pull/660); and
- feature-level validation after all proposed PR workflows finish.

The current definitions are useful migration inputs, not claims that the target
language already provides decision safety. PR #660 separates several
deterministic actions from model judgment and limits a coding-loop response to
one action. The target is stricter: at an LLM activation, only one semantic
decision is legal. The model never chooses which kind of work to do next.

## Normal form used below

Every node is exactly one of:

```yaml
# LLM-owned: answer one question with one typed value.
judge:
  kind: classify_one | construct_one | extract_bounded_set
  question: <one question>
  subject: <one typed value>
  context: [<read-only typed values>]
  output: {name: <name>, type: <schema>}
  validator: <kernel-owned validator>

# Kernel-owned: perform one declared operation and map its result.
operation:
  name: <registered operation>
  with: <typed arguments>
  bind: <typed result>
  outcomes: <exhaustive result mapping>
```

An LLM node has no tools and no selectable action enum. A compatibility
transport may contain `action`, but its schema has one constant value. The
kernel selects branches, enters loops, dispatches operations, commits outputs,
decrements budgets, and advances.

The phrase “one decision” applies to the semantic responsibility, not just the
number of JSON responses. These are invalid even if they return one object:

- “research the repository and implement the feature”;
- “inspect, edit, test, repair, or finish”;
- “review everything and fix anything you find”; and
- “choose the next tool or advance.”

The following are valid:

- classify one candidate as relevant or irrelevant;
- construct one proposed PR entry for one planning subject;
- assign one authoritative effect to one already planned PR;
- construct one patch for one change unit; and
- assess one finding against its cited evidence.

## Whole lifecycle

```text
specify feature
  -> suspend until specification PR is accepted/merged
  -> plan proposed PRs and instantiate their workflows
  -> suspend until planning PR is accepted/merged
  -> execute ready proposed-PR workflows in dependency order
  -> suspend whenever a prerequisite PR is not merged
  -> evaluate feature-wide acceptance, invariants, and security
  -> succeeded | failed | blocked | suspended
```

Waiting for review or merge is a durable suspension, not a polling or LLM loop.
Resumption captures new repository and GitHub snapshots before any branch is
selected.

## Phase 1: specify the feature

### Decomposition of the current flow

| Current responsibility | Target nodes | Why the split is required |
| --- | --- | --- |
| `capture-feature-name` may read, edit, manage files, or ask | One `derive_feature_name` judgment; ambiguity suspends for human input | Naming is separate from effects and control flow |
| `capture-feature-context` asks repeatedly until the model thinks context is sufficient | Extract supplied facts, derive missing-decision candidates, kernel decides whether to suspend | The model cannot own its stopping rule |
| Each `design-interview` category gathers context and proposes edits in one predicated step | One runner gather followed by one category-specific judgment | Observation and judgment have different authority |
| `store-interview-input` asks the model to rewrite a complete JSON file | Deterministic serialization operation | Copying typed values is not judgment |
| Proposal generation and evaluation | Deterministic operations | Existing ownership is retained |
| `repair-proposal` can read, gather, edit, and choose transition | Worklist of evaluator issues; one correction judgment per issue; runner recompiles | Repair target and retry are kernel-owned |
| Stage, prepare, and create specification PR | Typed Git, readiness, and GitHub operations | Publication success must come from receipts |

### Expanded control

```yaml
- id: find-feature-name-candidates
  operation:
    name: extract_feature_name_candidates
    with: {request: "${user_request}", max_items: 4}
    bind: feature_name_candidates

- id: require-unambiguous-feature-name
  match:
    value: "${feature_name_candidates.count}"
    cases:
      - when: 1
        continue: true
    otherwise:
      suspend:
        reason: feature_name_required
        resume_schema: one_feature_name

- id: derive-feature-name
  judge:
    kind: construct_one
    question: What stable kebab-case identifier normalizes this one supplied feature name?
    subject: "${feature_name_candidates.only_item}"
    output: {name: feature_name, type: kebab_case_identifier}
    validator: validate_feature_name

- id: extract-supplied-feature-facts
  judge:
    kind: extract_bounded_set
    question: Which explicit feature facts are present in this request?
    subject: "${user_request}"
    output:
      name: supplied_facts
      type: snapshot<feature_fact>
      key: $.fingerprint
      max_items: 64
    validator: validate_fact_citations

- id: derive-required-human-decisions
  operation:
    name: compare_facts_to_feature_intake_schema
    with: {facts: "${supplied_facts}"}
    bind: missing_decisions

- id: route-missing-decisions
  match:
    value: "${missing_decisions.count}"
    cases:
      - when: 0
        continue: true
    otherwise:
      suspend:
        reason: feature_decisions_required
        resume_schema: answers_for_exact_decision_ids
```

The kernel then evaluates a sealed category list. It corresponds to the
categories currently gathered by `design-interview`: requirements, approach,
entities, entity relationships, invariants, guidance, features, human
decisions, intent, intents, acceptance criteria, expected tests, required test
cases, expected outcomes, non-goals, risks, decisions, proposed PRs, modules,
and tools.

```yaml
- id: specification-categories
  operation:
    name: load_specification_category_catalog
    bind:
      categories:
        type: snapshot<specification_category>
        key: $.name
        exact_items: 20

- id: propose-category-edits
  for_each:
    snapshot: "${categories}"
    item: category
    body:
      - operation:
          name: gather_category_context
          with:
            feature_name: "${feature_name}"
            category: "${category.name}"
          bind: category_context
      - judge:
          kind: extract_bounded_set
          question: Which additions and deletions are required for this one category?
          subject: "${category}"
          context: ["${supplied_facts}", "${category_context}"]
          output:
            name: category_edits
            type: category_edit_set
            max_items: 64
          validator: validate_category_edit_set
    collect: all_category_edits

- id: serialize-interview-input
  operation:
    name: write_design_interview_input
    with:
      feature_name: "${feature_name}"
      facts: "${supplied_facts}"
      category_edits: "${all_category_edits}"
    bind: interview_input_receipt

- id: generate-feature-specification
  operation:
    name: generate_feature_pr_specification
    with: {interview_input: "${interview_input_receipt.artifact_ref}"}
    bind: specification_artifact
```

Evaluation and repair are issue-driven. The model cannot decide to gather more
context, edit arbitrary YAML, or rerun validation.

```yaml
- id: specification-repair-epochs
  repeat:
    budget: 3
    body:
      - operation:
          name: evaluate_feature_specification
          with: {artifact: "${specification_artifact}", max_issues: 256}
          bind: specification_evaluation
      - match:
          value: "${specification_evaluation.issue_count}"
          cases:
            - when: 0
              break: specification_valid
          otherwise:
            for_each:
              snapshot: "${specification_evaluation.issues}"
              item: issue
              body:
                - judge:
                    kind: construct_one
                    question: What corrected typed value resolves this one evaluator issue?
                    subject: "${issue}"
                    context: ["${issue.current_value}", "${issue.allowed_schema}"]
                    output: {name: correction, type: specification_correction}
                    validator: validate_issue_correction
                - operation:
                    name: apply_specification_correction
                    with: {issue: "${issue}", correction: "${correction}"}
      - operation: {name: regenerate_feature_specification}
    on_exhausted: failed.invalid_feature_specification

- id: publish-specification
  sequence:
    - operation: {name: stage_exact_specification_files}
    - operation: {name: run_readiness_checks}
    - operation: {name: create_commit}
    - operation: {name: push_feature_branch}
    - operation: {name: create_or_update_feature_pr}

- id: await-specification-merge
  suspend:
    reason: specification_review_required
    resume_on: exact_pull_request_merged
```

## Phase 2: plan implementation workflows

### Decomposition of `start-implementing-feature`

| Current stage | Target ownership |
| --- | --- |
| Capture feature query | One LLM string judgment |
| Search proposal, current, and workflow directories | Three runner operations, parallel if desired |
| Select canonical feature | Deterministic exact match; zero or multiple matches suspend for human resolution |
| Bootstrap project structure | Deterministic existence match and bounded child workflow |
| Plan proposed PRs | One plan-subject derivation operation plus one PR-entry judgment per subject, followed by graph validation |
| Generate proposed-PR template | Runner operation |
| Load authoritative effects | Runner operation |
| Allocate effects | One target-PR classification per effect, then deterministic total/exact validation |
| Evaluate or repair proposed-PR specification | Runner gate and issue worklist |
| Plan workflow instantiation | Derive instances mechanically from validated PR entries and template inputs |
| Instantiate missing workflows | `for_each` runner operation with idempotency and one exact forced-recovery case |
| Verify dependencies, locations, and bidirectional mapping | Runner evaluators over persisted artifacts |
| Stage, readiness-check, and create planning PR | Typed runner operations |
| Handoff | Render persisted PR URL and plan; no new model decision |

### Expanded control

```yaml
- id: derive-feature-query
  judge:
    kind: construct_one
    question: What feature identifier did the user request for implementation?
    subject: "${user_request}"
    output: {name: feature_query, type: nonempty_string}
    validator: validate_feature_query

- id: discover-feature-artifacts
  parallel:
    branches:
      - operation:
          name: fuzzy_match_proposed_features
          with: {query: "${feature_query}", max_items: 20}
          bind: proposal_candidates
      - operation:
          name: fuzzy_match_current_features
          with: {query: "${feature_query}", max_items: 20}
          bind: current_candidates
      - operation:
          name: fuzzy_match_feature_workflows
          with: {query: "${feature_query}", max_items: 40}
          bind: workflow_candidates

- id: resolve-canonical-feature
  operation:
    name: resolve_exact_canonical_feature
    with:
      query: "${feature_query}"
      proposals: "${proposal_candidates}"
      current: "${current_candidates}"
      workflows: "${workflow_candidates}"
    outcomes:
      one_match: {bind: feature_context}
      no_match: {terminal: blocked}
      tied: {terminal: suspended, resume_on: canonical_feature_selected}

- id: ensure-project-structure
  match:
    value: "${feature_context.project_structure_status}"
    cases:
      - when: present
        continue: true
      - when: missing
        do:
          operation:
            name: run_bounded_project_structure_bootstrap
            bind: project_structure
```

Proposed-PR planning is split so the LLM never both discovers code, defines the
PR graph, and allocates effects.

```yaml
- id: derive-pr-planning-subjects
  operation:
    name: derive_pr_planning_subjects
    with:
      feature_specification: "${feature_context.specification}"
      project_structure: "${project_structure}"
      max_items: 32
    bind: pr_subjects

- id: propose-pr-entries
  for_each:
    snapshot: "${pr_subjects}"
    item: subject
    body:
      judge:
        kind: construct_one
        question: What one proposed PR delivers this subject with an independently valid boundary?
        subject: "${subject}"
        context: ["${feature_context.specification}", "${project_structure}"]
        output: {name: proposed_pr, type: proposed_pr_entry_without_effects}
        validator: validate_proposed_pr_entry
    collect: proposed_prs

- id: validate-pr-graph
  operation:
    name: validate_proposed_pr_graph
    with: {proposed_prs: "${proposed_prs}"}
    outcomes:
      acyclic_and_complete: {continue: true}
      invalid: {terminal: blocked}

- id: load-authoritative-effects
  operation:
    name: load_authoritative_pr_effects
    with: {max_items: 512}
    bind: authoritative_effects

- id: allocate-effects
  for_each:
    snapshot: "${authoritative_effects}"
    item: effect
    body:
      judge:
        kind: classify_one
        question: Which already planned PR owns this one authoritative effect?
        subject: "${effect}"
        context: ["${proposed_prs.ids_and_intents}"]
        output: {name: owner_pr_id, type: enum_from<proposed_prs.ids>}
        validator: owner_is_planned_pr
    collect: effect_assignments

- id: compile-and-evaluate-pr-specification
  operation:
    name: compile_and_evaluate_proposed_pr_specification
    with:
      plan: "${proposed_prs}"
      assignments: "${effect_assignments}"
    outcomes:
      valid: {bind: proposed_pr_specification}
      plan_issue: {goto: repair_one_pr_plan_issue}
      allocation_issue: {goto: reclassify_one_effect}

- id: repair-one-pr-plan-issue
  judge:
    kind: construct_one
    question: What corrected PR entry resolves this one plan diagnostic?
    subject: "${selected_plan_issue}"
    context: ["${proposed_prs}"]
    output: {name: corrected_pr_entry, type: proposed_pr_entry_without_effects}
    validator: validate_proposed_pr_entry
  then:
    operation:
      name: replace_one_proposed_pr_entry
      with: {entry: "${corrected_pr_entry}"}

- id: reclassify-one-effect
  judge:
    kind: classify_one
    question: Which already planned PR owns this one misallocated effect?
    subject: "${selected_allocation_issue.effect}"
    context: ["${proposed_prs.ids_and_intents}"]
    output: {name: corrected_owner_pr_id, type: enum_from<proposed_prs.ids>}
    validator: owner_is_planned_pr
  then:
    operation:
      name: replace_one_effect_assignment
      with:
        effect: "${selected_allocation_issue.effect}"
        owner: "${corrected_owner_pr_id}"
```

The compile/evaluate operation and these two exact repair targets execute inside
a three-epoch `repeat`. The kernel selects the diagnostic and target node, then
reevaluates the whole document. The model never emits `goto_step` or chooses
whether a plan issue should be treated as an allocation issue.

Workflow instances are data, so their creation is a finite runner loop rather
than a model choosing repeated `instantiate-workflow` actions.

```yaml
- id: derive-workflow-instances
  operation:
    name: derive_execution_workflow_instances
    with:
      proposed_prs: "${proposed_prs}"
      template: execute-proposed-pr
      verification_command: "${project_structure.test_command}"
    bind: workflow_instances

- id: instantiate-workflows
  for_each:
    snapshot: "${workflow_instances}"
    item: instance
    body:
      operation:
        name: instantiate_workflow_idempotently
        with: {instance: "${instance}"}
        recovery:
          stale_dedicated_branch:
            operation: refresh_dedicated_branch_with_lease
            max_attempts: 1
    collect: workflow_instantiation_receipts

- id: prove-planning-artifacts
  sequence:
    - operation:
        name: verify_workflow_dependencies
        with: {plan: "${proposed_prs}", receipts: "${workflow_instantiation_receipts}"}
    - operation: {name: verify_artifacts_in_active_worktree}
    - operation: {name: verify_bidirectional_pr_workflow_mapping}
    - operation: {name: evaluate_all_workflow_artifacts}
    - operation: {name: stage_exact_planning_artifacts}
    - operation: {name: run_readiness_checks}
    - operation: {name: create_commit}
    - operation: {name: push_feature_branch}
    - operation: {name: create_or_update_planning_pr}

- id: await-planning-merge
  suspend:
    reason: implementation_workflow_review_required
    resume_on: exact_pull_request_merged
```

## Phase 3: execute every proposed PR

### What PR #660 establishes and what remains

PR #660 is an important intermediate step:

- proposed-PR context gathering is runner-owned;
- execution-plan judgment no longer invokes inspection tools;
- Python structure and symbol inspections are separate tasks;
- formatting, lint, type checking, Git status, promotion, and file-set checks
  are runner-owned; and
- the coding-loop harness permits only one action per model round trip and runs
  verification after edits.

It is not yet the target normal form:

- the coding-loop model still chooses whether its one action is an inspection,
  edit, repair, or completion;
- structure and symbol tasks still ask a model to select and invoke the one tool
  instead of iterating mechanically over the plan;
- completeness and scope repair steps combine “is repair needed?”, mutation,
  and advancement; and
- a `next_step` action remains model-visible after several successful results.

The language should compile the same intent into the expanded nodes below.

### Dependency-aware workflow scheduling

```yaml
- id: execute-proposed-pr-workflows
  worklist:
    source: "${validated_workflow_instances}"
    key: $.workflow_id
    max_admissions: 32
    ready_when: all_dependency_pull_requests_merged
    body: execute-one-proposed-pr-expanded
    outcomes:
      no_ready_item_with_unmerged_dependencies:
        suspend: prerequisite_pull_request_merge_required
      item_failed:
        terminal: failed
      all_disposed:
        continue: true
```

The scheduler uses persisted GitHub merge receipts. PR creation or task
completion does not satisfy a `merged` dependency.

### Expanded execution of one proposed PR

```yaml
- id: gather-proposed-pr-context
  operation:
    name: gather_context
    with:
      types: [proposed_prs, requirements, features, acceptance_criteria,
              expected_tests, required_test_cases, invariants, tools, intent,
              risks, decisions]
    bind: proposed_pr_context

- id: create-execution-plan
  judge:
    kind: construct_one
    question: What ordered plan maps every criterion in this PR to exact code, tests, and commands?
    subject: "${proposed_pr_context.proposed_pr}"
    context: ["${proposed_pr_context}"]
    output: {name: execution_plan, type: detailed_execution_plan}
    validator: validate_criterion_plan_coverage

- id: inspect-planned-python-files
  for_each:
    snapshot: "${execution_plan.python_files}"
    item: python_file
    body:
      operation:
        name: basedpyright_inspect_structure
        with: {path: "${python_file.path}"}
    collect: structure_inspections

- id: resolve-planned-symbols
  for_each:
    snapshot: "${execution_plan.symbols}"
    item: symbol
    body:
      operation:
        name: basedpyright_resolve_symbol
        with: {query: "${symbol.qualified_name}"}
    collect: symbol_inspections
```

The current coding loop becomes a finite set of exact change units. The model
does not choose between inspection, edit, test, and finish.

```yaml
- id: derive-pr-change-units
  operation:
    name: derive_change_units_from_validated_plan
    with: {plan: "${execution_plan}", max_items: 64}
    bind: pr_change_units

- id: implement-pr-change-units
  for_each:
    snapshot: "${pr_change_units}"
    item: unit
    body:
      - judge:
          kind: construct_one
          question: What bounded patch implements this one planned unit?
          subject: "${unit}"
          context: ["${proposed_pr_context}", "${structure_inspections}",
                    "${symbol_inspections}", "${latest_diagnostic?}"]
          output: {name: patch, type: bounded_patch}
          validator: validate_patch_scope_and_shape
      - operation:
          name: apply_validated_patch
          with: {patch: "${patch}"}
      - operation:
          name: run_declared_full_test_suite
          bind: post_edit_test_evidence
    retry:
      budget: 2
      retry_on: [patch_rejected, correctable_test_failure]
    collect: implementation_evidence
    on_item_exhausted: failed.change_unit_not_implemented
```

Specification completeness is evaluated criterion by criterion. Code,
invariant, security, and scope review are likewise evaluated over finite
runner-derived subjects. This is more precise than one model returning a global
`complete` or `secure` boolean and an arbitrary finding list. The following
nodes form the body of a `repeat` with a three-epoch budget.

```yaml
- id: capture-current-pr-evidence
  parallel:
    branches:
      - operation:
          name: capture_git_status_and_diff
          with: {max_changed_paths: 256}
          bind: current_diff
      - operation: {name: run_full_test_suite}
      - operation: {name: run_ruff_format_check}
      - operation: {name: run_ruff_lint}
      - operation: {name: run_mypy}
    collect: validation_evidence

- id: review-pr-criteria
  for_each:
    snapshot: "${proposed_pr_context.acceptance_criteria}"
    item: criterion
    body:
      judge:
        kind: classify_one
        question: Does this one criterion have implementation and fresh passing evidence?
        subject: "${criterion}"
        context: ["${current_diff}", "${implementation_evidence}"]
        output:
          name: criterion_verdict
          type: criterion_verdict  # satisfied | one typed gap
        validator: validate_verdict_citations
    collect: criterion_verdicts

- id: route-completeness
  operation:
    name: derive_gaps_from_criterion_verdicts
    with: {verdicts: "${criterion_verdicts}"}
    bind: completeness_gaps

- id: derive-code-review-subjects
  operation:
    name: derive_changed_symbol_review_subjects
    with: {diff: "${current_diff}", max_items: 128}
    bind: code_review_subjects

- id: review-code-subjects
  for_each:
    snapshot: "${code_review_subjects}"
    item: review_subject
    body:
      judge:
        kind: classify_one
        question: Does this one changed symbol contain a concrete correctness or maintainability defect?
        subject: "${review_subject}"
        context: ["${proposed_pr_context}", "${validation_evidence}"]
        output: {name: code_verdict, type: evidence_verdict}
        validator: validate_verdict_citations
    collect: code_verdicts

- id: review-pr-invariants
  for_each:
    snapshot: "${proposed_pr_context.invariants}"
    item: invariant
    body:
      judge:
        kind: classify_one
        question: Does this one invariant have current supporting evidence after the diff?
        subject: "${invariant}"
        context: ["${current_diff}", "${validation_evidence}"]
        output: {name: invariant_verdict, type: evidence_verdict}
        validator: validate_verdict_citations
    collect: invariant_verdicts

- id: derive-security-review-subjects
  operation:
    name: derive_security_review_subjects
    with: {risks: "${proposed_pr_context.risks}", diff: "${current_diff}", max_items: 64}
    bind: security_review_subjects

- id: review-security-subjects
  for_each:
    snapshot: "${security_review_subjects}"
    item: security_subject
    body:
      judge:
        kind: classify_one
        question: Does this one security obligation have current passing evidence?
        subject: "${security_subject}"
        context: ["${current_diff}", "${validation_evidence}"]
        output: {name: security_verdict, type: evidence_verdict}
        validator: validate_verdict_citations
    collect: security_verdicts

- id: review-each-changed-path
  for_each:
    snapshot: "${current_diff.changed_paths}"
    item: changed_path
    body:
      judge:
        kind: classify_one
        question: Does this one changed path belong to the proposed PR and contain no forbidden artifact?
        subject: "${changed_path}"
        context: ["${proposed_pr_context}", "${current_diff.diff_for_path}"]
        output:
          name: scope_verdict
          type: scope_verdict  # in_scope | one typed scope finding
        validator: validate_scope_verdict_citations
    collect: scope_verdicts

- id: derive-scope-findings
  operation:
    name: derive_findings_from_scope_verdicts
    with: {verdicts: "${scope_verdicts}"}
    bind: scope_findings

- id: normalize-pr-findings
  operation:
    name: normalize_and_seal_findings
    with:
      sources: ["${validation_evidence}", "${completeness_gaps}",
                "${code_verdicts}", "${invariant_verdicts}",
                "${security_verdicts}", "${scope_findings}"]
      max_items: 64
    bind: pr_findings

- id: route-pr-findings
  match:
    value: "${pr_findings.count}"
    cases:
      - when: 0
        break: pr_ready
    otherwise:
      continue: repair-pr-findings

- id: repair-pr-findings
  for_each:
    snapshot: "${pr_findings}"
    item: finding
    body:
      - operation:
          name: resolve_finding_source
          with: {finding: "${finding}", diff: "${current_diff}"}
          bind: current_source_at_finding
      - judge:
          kind: classify_one
          question: Is this one finding supported by its cited current evidence?
          subject: "${finding}"
          context: ["${current_source_at_finding}"]
          output:
            name: finding_disposition
            type: enum[confirmed, unsupported, needs_human]
          validator: validate_finding_disposition
      - match:
          value: "${finding_disposition}"
          cases:
            - when: unsupported
              do: {operation: {name: record_unsupported_finding}}
            - when: needs_human
              do: {suspend: finding_disposition_required}
            - when: confirmed
              do:
                - judge:
                    kind: construct_one
                    question: What one bounded file change resolves this confirmed finding?
                    subject: "${finding}"
                    context: ["${current_source_at_finding}"]
                    output: {name: finding_repair, type: bounded_file_change}
                    validator: validate_finding_repair
                - operation:
                    name: apply_validated_file_change
                    with: {change: "${finding_repair}"}
```

After any repair, the workflow starts a new bounded evidence epoch and reruns
tests, format, lint, type checks, completeness, code, invariant, security, and
scope review. Passing evidence from before the repair cannot be reused. If the
third epoch still has findings, the PR ends as
`failed.validation_not_converged`.

```yaml
- id: prove-pr-readiness
  operation:
    name: evaluate_pr_readiness
    requires:
      plan_criteria: all_implemented
      tests: fresh_and_passing
      format: fresh_and_passing
      lint: fresh_and_passing
      types: fresh_and_passing
      completeness: no_open_gaps
      code_review: no_open_findings
      invariants: all_supported
      security: all_applicable_obligations_supported
      scope: no_open_findings
      effects: within_proposed_pr_authority
    outcomes:
      ready: {continue: true}
      not_ready: {terminal: failed}

- id: publish-proposed-pr
  sequence:
    - operation: {name: promote_feature_documents_if_declared}
    - operation: {name: verify_exact_pull_request_file_set}
    - operation: {name: run_finish_pr_prep}
    - operation: {name: create_commit}
    - operation: {name: push_integration_branch}
    - operation: {name: create_or_update_pull_request}
  outcomes:
    completed: {terminal: succeeded}
    ambiguous: {terminal: suspended, resume_on: publication_reconciled}
    failed: {terminal: failed}
```

## Phase 4: feature-wide acceptance

Per-PR success is necessary but not sufficient. After every proposed PR is
merged, capture a new baseline and evaluate the composed feature.

```yaml
- id: capture-integrated-feature
  operation:
    name: capture_integrated_feature_snapshot
    with: {required_prs: "${proposed_prs.ids}"}
    outcomes:
      all_merged: {bind: integrated_feature}
      merge_pending: {terminal: suspended}

- id: derive-feature-obligations
  operation:
    name: derive_feature_acceptance_obligations
    with:
      specification: "${feature_specification}"
      integrated_tree: "${integrated_feature}"
      max_items: 256
    bind: feature_obligations

- id: verify-feature-obligations
  for_each:
    snapshot: "${feature_obligations}"
    item: obligation
    body:
      operation:
        name: run_feature_verification
        with: {obligation: "${obligation}"}
    collect: feature_evidence

- id: review-feature-invariants
  for_each:
    snapshot: "${feature_specification.invariants}"
    item: invariant
    body:
      judge:
        kind: classify_one
        question: Does this one invariant have current evidence over the integrated feature?
        subject: "${invariant}"
        context: ["${integrated_feature.diff}", "${feature_evidence}"]
        output: {name: invariant_verdict, type: evidence_verdict}
        validator: validate_verdict_citations
    collect: invariant_verdicts

- id: review-feature-security-obligations
  for_each:
    snapshot: "${feature_obligations.security}"
    item: security_obligation
    body:
      judge:
        kind: classify_one
        question: Does this one security obligation have current passing evidence?
        subject: "${security_obligation}"
        context: ["${integrated_feature.diff}", "${feature_evidence}"]
        output: {name: security_verdict, type: evidence_verdict}
        validator: validate_verdict_citations
    collect: security_verdicts

- id: prove-feature-acceptance
  operation:
    name: evaluate_feature_acceptance
    requires:
      proposed_prs: all_merged
      acceptance_criteria: all_fresh_and_passing
      expected_tests: all_fresh_and_passing
      required_test_cases: all_fresh_and_passing
      invariants: all_supported
      security_obligations: all_supported
      non_goals: no_detected_scope_expansion
    outcomes:
      complete: {terminal: succeeded}
      incomplete: {terminal: failed}
```

The final result is relative to the accepted feature specification. The
certificate records semantic assumptions for criteria and review judgments; it
does not misrepresent them as mathematical proofs of unstated intent or absence
of all vulnerabilities.

## Termination and resource derivation

Let:

- `C` be the fixed 20 specification categories;
- `S <= 32` be proposed-PR planning subjects;
- `E` be authoritative effects, bounded by the specification schema;
- `P <= 32` be proposed PRs and workflow instances;
- `U <= 64` be change units in one PR;
- `A <= 64` be acceptance criteria, bounded by the specification schema;
- `Q <= 128` be changed-symbol code-review subjects;
- `I <= 64` be applicable invariant-review subjects;
- `H <= 64` be applicable security-review subjects;
- `F <= 256` be changed files, bounded by the diff operation contract;
- `N <= 64` be normalized findings admitted in one evidence epoch; and
- `R` be the fixed repair-epoch budget.

A conservative LLM activation bound is a sum of finite terms such as:

```text
feature intake
+ C category judgments
+ S proposed-PR judgments
+ E effect-owner classifications
+ P * (
     1 execution-plan judgment
     + U * patch_attempts
     + R * (A criterion verdicts + Q code verdicts
            + I invariant verdicts + H security verdicts
            + F scope verdicts + N finding dispositions
            + N repair judgments)
   )
+ feature-wide invariant and security verdicts
```

Every symbol has a compiler-known maximum before execution begins. If a
repository-derived collection exceeds its declared bound, the producing
operation fails before loop entry. If the calculated maximum exceeds the root
budget, compilation fails. Parallel execution changes scheduling but not this
total activation bound.

## Compiler acceptance checklist

A strict compiler accepts this workflow only if it can answer yes to all of the
following:

- Does every LLM node have one subject, one question, one required output, one
  validator, and a constant transport action?
- Are all tools, mutations, branches, retries, advancement, and completion
  kernel-owned?
- Have `specify-a-feature`, `design-interview`, `start-implementing-feature`,
  `execute-proposed-pr`, `finish-pr-prep`, and `create-pull-request` been
  expanded or proven from transitive compiled summaries?
- Is every generated collection sealed, keyed, typed, and bounded before it is
  iterated?
- Does every repair consume an item attempt and an evidence epoch?
- Does every mutation invalidate affected evidence before readiness can be
  re-established?
- Are proposed-PR dependencies satisfied only by durable merge receipts?
- Does PR-level readiness cover specification completeness, tests, formatting,
  lint, types, code review, invariants, security, scope, and effects?
- Does feature-level readiness cover all merged PRs, acceptance criteria,
  required tests, invariants, security obligations, and non-goals?
- Do all legal paths terminate or suspend, and is every individual activation
  operationally bounded?

If any answer is no, `decision_safe`, `completion_safe`, or one of the
termination guarantees is unknown or disproven; prompt wording cannot upgrade
it to proven.
