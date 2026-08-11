---
name: feature-functionality-review
description: "Use when reviewing a pull request that implements part or all of a planned feature. Trace the PR to its feature definition and proposed-PR sequence, verify the implementation against the applicable scope, and post every actionable finding as an inline comment on the PR."
---

# Feature Functionality Review

Review functionality against the plan that produced it, not only against the local diff. The input is a GitHub PR reference (number or URL) in the current repository. The output is the set of actionable findings posted directly to that PR as inline review comments; do not substitute a prose-only report.

Use the repository's GitHub integration for PR metadata, files, and comments when available. Use `gh` as a fallback for repository data or GraphQL operations that need review-thread context. Before any write, confirm the target repository and PR number.

## Review workflow

### 1. Discover the feature graph

Start with the referenced PR:

- Read its title, body, labels, base/head, changed files, commits, review history, and current state.
- Find feature and proposed-PR identifiers in the PR body, changelog entries, specification paths, commit messages, and changed files. Search `docs/specs/`, especially `docs/specs/<feature>/feature-pr-specification.yaml`, `proposed-pr-specification.yaml`, and implementation specifications.
- Resolve the canonical feature definition and the ordered proposed-PR list. Treat explicit structured metadata as authoritative; use prose references only to locate candidates, then verify each candidate against its specification and GitHub PR.
- Build a small ledger containing: feature id/name, feature-definition files, proposed PR ids in order, each PR's state (`in_progress`, `completed`, open, closed, or merged), and the current PR's position.
- If the relationship cannot be established unambiguously, post an inline comment on the nearest relevant changed line explaining the missing linkage and stop. Do not guess a feature or silently review an unrelated plan.

### 2. Determine review depth

The current PR is the final proposed PR only when the feature's ordered plan identifies it as the last proposed PR and the preceding proposed PRs are complete/merged or otherwise explicitly accounted for. Do not infer finality from the PR title, merge order, or the fact that the PR looks large.

For an intermediate proposed PR:

- Review the current PR against the intent, acceptance criteria, required behavior, dependencies, and validation specified by its matching proposed-PR document.
- Check that the diff delivers the functionality assigned to this step and does not claim or rely on behavior assigned to a later step.
- Check integration points needed by the next planned step when the current specification makes them contractual.
- Do not fail the PR for feature-wide behavior deliberately assigned to a later proposed PR.

For the final proposed PR:

- Read the full feature definition, including goal, user-visible behavior, success criteria, constraints, edge cases, entities/relationships, and validation requirements.
- Read every related previous proposed-PR specification and every related closed/merged PR, including their diffs and relevant review discussions. Reconcile planned behavior with what actually landed; do not assume closed means complete.
- Review the current PR in the context of the complete feature sequence. Trace every feature requirement to implementation, tests, or an explicit justified exception.
- Check that behavior from earlier PRs remains integrated and that the final PR does not leave a planned requirement, edge case, migration, interface, or validation step unimplemented.

### 3. Perform the point-by-point review

Create a checklist from the applicable proposed-PR specification; for a final PR, union the checklist from the entire feature definition and all related proposed PRs. For each item, record:

- requirement or acceptance criterion;
- source document and proposed PR;
- implementation location and relevant test/evidence;
- status: satisfied, partially satisfied, missing, contradicted, or not applicable with rationale.

Inspect code, tests, configuration, schemas, migrations, API contracts, and user-facing documentation as appropriate. Use the PR's complete diff and surrounding code, not just the changed hunk. Consider error paths, empty states, compatibility, persistence, authorization, observability, and test coverage when they are part of the feature contract.

For each unsatisfied item, determine whether it is caused by this PR. Comment only when the PR can fix it or must explicitly explain the deviation. Avoid duplicate comments for one root cause; group tightly related omissions into one finding.

### 4. Post findings as inline comments

Every finding must be posted to the PR as an inline review comment anchored to the smallest relevant changed line. Include:

- what is wrong or missing;
- why it violates the applicable feature/proposed-PR requirement;
- the concrete user-visible or system impact;
- a concise fix direction, when one is reasonably clear.

- If the relevant code is not changed in this PR, anchor the comment to the nearest changed line in the same file or integration area and state that the issue is a feature-completeness gap exposed by this PR. Never post a file-level or general review comment when an inline anchor is possible.
- Use an inline comment rather than a review summary for every actionable finding. A summary may be added only if the platform requires one, and must not contain the sole copy of any finding.
- Preserve severity in the comment (`blocking`, `high`, `medium`, or `low`) only when it helps prioritization; do not invent a severity taxonomy beyond what the repository uses.
- Do not post compliments, speculative concerns without evidence, duplicate findings, or comments about later proposed PRs when reviewing an intermediate PR.
- Before posting, check existing unresolved threads and avoid repeating an already reported issue unless the current diff leaves it unresolved or regresses it.
- After posting, verify that each intended comment exists on the target PR and that its anchor points to the current PR diff. If the platform rejects an anchor, choose a valid nearby changed line and preserve the file/line reference in the comment text.

### 5. Finish with an audit trail

Keep an internal record of the feature graph, review depth selected, checklist items, evidence examined, and comments posted. In the final response, briefly state the PR reviewed, whether it was treated as intermediate or final, and how many inline findings were posted. Do not repeat the findings only in the final response; the PR comments are the authoritative output.

## Guardrails

- Never change code, specifications, or the PR branch as part of this skill. Review and comment only.
- Never approve, merge, close, or label the PR unless the user separately requests that action.
- Do not treat passing CI as proof of feature completeness; compare behavior to the feature plan.
- Do not treat a missing test as a defect when the specification does not require observable test coverage, but do flag an unverified acceptance criterion when the plan requires validation.
- If GitHub write access is unavailable, do not claim completion. Report that the inline comments could not be posted and include the exact findings prepared for posting.
