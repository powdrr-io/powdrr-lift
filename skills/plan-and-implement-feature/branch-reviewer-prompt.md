# Branch Reviewer Prompt Template

Use this template when dispatching a fresh reviewer subagent for the full branch at the end of the run.

```yaml
Subagent (general-purpose):
  description: "Review branch-wide implementation"
  model: [MODEL - REQUIRED]
  prompt: |
    You are reviewing the full branch after all task-level work is complete.

    ## What Was Requested
    Read the feature contract, system map, changelog, and plan diff artifacts that define the run.
    Global constraints that bind the branch: [GLOBAL_CONSTRAINTS]

    ## Diff Under Review
    Base: [MERGE_BASE_SHA]
    Head: [HEAD_SHA]
    Diff file: [DIFF_FILE]

    Read the branch review package once.
    Do not re-run git commands.
    Inspect unchanged code only when you must verify a concrete cross-task risk you can name.

    ## Review Rules
    - Judge the branch as a whole, not one task at a time.
    - Look for integration gaps, duplicated logic, missing coverage, and mismatches between the feature contract, changelog, and plan diff.
    - Verify that the changelog build step, plan-diff reconciliation step, and code-only fix loop all converge on the same final branch state.
    - Cite file:line for every finding.
    - This is the final gate before wrap-up.

    ## Return Format
    Return:
    - branch review verdict
    - strengths
    - issues grouped by severity
    - one short reasoning paragraph
```
