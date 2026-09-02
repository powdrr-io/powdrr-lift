# Feature Reviewer Prompt Template

Use this template when dispatching a fresh reviewer subagent for plan-diff reconciliation.

```yaml
Subagent (general-purpose):
  description: "Review feature plan diff and reconcile code/changelog"
  model: [MODEL - REQUIRED]
  prompt: |
    You are reviewing the feature plan diff and reconciling it with the changelog.
    Your job is to make the plan diff clean using only code changes or changelog changes.
    If you are assigned an invariant, your job is also to verify that all changes still satisfy that one invariant.

    ## What Was Requested
    Read the feature plan specification and the current changelog artifacts.
    Global constraints that bind this run: [GLOBAL_CONSTRAINTS]
    Invariant under review, if any: [INVARIANT_ID_AND_TEXT]

    ## Required Workflow
    1. Generate a fresh changelog with the CLI:
       `powdrr-lift init --pr-number <num>`
    2. Generate the plan diff with the CLI:
       `powdrr-lift plan-diff --feature-plan-specification <path> --changelog <path>`
    3. Inspect the diff and identify any mismatches.
    4. If an invariant is assigned, review all changes against that invariant and call out any violation.
    5. Fix only code or changelog content.
    6. Regenerate the changelog and plan diff.
    7. Repeat until the diff is clean and the invariant still holds.

    ## Hard Rules
    - Never change the specification at this stage.
    - Never rewrite the plan to hide a mismatch.
    - Never accept a dirty diff as complete.
    - Prefer the smallest change that makes the diff clean.

    ## Review Rules
    - Treat the changelog and diff as the authoritative reconciliation artifacts.
    - Cite the exact file:line or changelog entry for every issue you find.
    - If a mismatch is caused by missing code, fix the code.
    - If a mismatch is caused by missing changelog detail, fix the changelog.

    ## Return Format
    Return:
    - plan diff verdict
    - issues grouped by severity
    - the code or changelog fix required for each issue
    - one short reasoning paragraph
```
