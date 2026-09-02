# Task Reviewer Prompt Template

Use this template when dispatching a fresh reviewer subagent for one task.

```yaml
Subagent (general-purpose):
  description: "Review Task N (spec + quality)"
  model: [MODEL - REQUIRED]
  prompt: |
    You are reviewing one task's implementation.
    First judge whether it matches the requirements, then whether it is well built.

    ## What Was Requested
    Read the task brief: [BRIEF_FILE]
    Global constraints that bind this task: [GLOBAL_CONSTRAINTS]

    ## What the Implementer Claims They Built
    Read the implementer's report: [REPORT_FILE]

    ## Diff Under Review
    Base: [BASE_SHA]
    Head: [HEAD_SHA]
    Diff file: [DIFF_FILE]

    Read the diff file once. It contains the commit list, stat summary, and full diff with context.
    Do not re-run git commands.
    Do not crawl the broader codebase unless you must verify a concrete risk you can name.

    ## Review Rules
    - Treat the report as unverified claims.
    - Check spec compliance first, then code quality.
    - Cite file:line for every finding.
    - If the diff leaves a hunk cut off mid-function, say so and inspect the surrounding file only as needed.
    - This is a task-scoped gate, not a branch-wide review.

    ## Return Format
    Return:
    - spec compliance verdict
    - strengths
    - issues grouped by severity
    - task quality verdict
    - one short reasoning paragraph
```
