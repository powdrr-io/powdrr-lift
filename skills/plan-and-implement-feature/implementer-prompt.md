# Implementer Subagent Prompt Template

Use this template when dispatching a fresh implementer subagent for one task.

```yaml
Subagent (general-purpose):
  description: "Implement Task N: [task name]"
  model: [MODEL - REQUIRED]
  prompt: |
    You are implementing Task N: [task name].

    ## Task Brief
    Read this first: [BRIEF_FILE]
    It contains the full task text, exact values, constraints, and acceptance criteria.

    ## Context
    [Scene-setting: where this task fits, what files it owns, and any dependencies from earlier tasks.]

    ## Before You Begin
    If anything about the requirements, acceptance criteria, dependencies, or approach is unclear, ask now.
    Do not guess.

    ## Your Job
    1. Implement exactly what the task specifies.
    2. Write or update tests that verify the behavior.
    3. Run focused tests while iterating, then the full task-relevant suite before reporting.
    4. Commit your work.
    5. Self-review for completeness, quality, and scope.
    6. Write your report to [REPORT_FILE].

    Work from: [WORK_DIR]
    Owned files: [OWNED_FILES]

    While you work:
    - Stay within the owned files unless the task brief says otherwise.
    - Do not revert or overwrite other changes in the branch.
    - If you get stuck, report BLOCKED or NEEDS_CONTEXT with specifics.

    ## Report
    Include:
    - what you changed
    - what you tested
    - test results
    - self-review findings, if any
    - concerns, if any

    Return only:
    - status
    - commits created
    - one-line test summary
    - concerns, if any
    - report file path
```
