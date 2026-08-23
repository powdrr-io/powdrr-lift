# Intrinsic Git and GitHub tools

Workflow LLMs always have two repository tools available: `git` and `gh`.
They execute in the active worktree and return structured command, exit status,
stdout, and stderr data.

## Supported operations

Use structured parameters whenever possible:

```json
{"action":"invoke_tool","tool":"git","parameters":{"operation":"status"}}
```

```json
{"action":"invoke_tool","tool":"git","parameters":{"operation":"add","paths":["docs/a.yaml","docs/b.yaml"]}}
```

```json
{"action":"invoke_tool","tool":"git","parameters":{"operation":"move","source":"old.yaml","destination":"new.yaml"}}
```

```json
{"action":"invoke_tool","tool":"gh","parameters":{"operation":"pr_view","pr_reference":"394"}}
```

The git tool supports `status`, `add`, and `move`/`rename`. The gh tool supports
pull-request `view`, `diff`, `checks`, `create`, `edit`, and `comments`.
Relative paths are required for git operations; absolute paths and `..` path
components are rejected.

## Existing skill uses

The following declarative uses have been migrated from shell commands:

| Existing use | Intrinsic operation |
| --- | --- |
| Stage exact files before PR preparation | `git` / `add` |
| Inspect short worktree status | `git` / `status` |
| Rename a generated workflow file | `git` / `move` |
| Inspect PR metadata, diffs, and checks in review skills | `gh` / `pr_view`, `pr_diff`, `pr_checks` |
| Create or update a draft PR | `gh` / `pr_create`, `pr_edit` |

Other commands remain shell actions when they are not part of this intrinsic
surface, such as `git commit`, `git push`, `git diff --cached`, `git log`, and
GitHub API calls. They retain their existing declared command and result
handling.

## Empty commits and dirty worktrees

A commit action that receives Git's “nothing to commit” response succeeds as a
no-op when `git status --short` is empty. This means an earlier action already
committed the desired state and prevents the workflow from retrying an
impossible empty commit.

If status reports unstaged or untracked paths, the action remains a failure and
returns corrective guidance. Intended files should be staged with the intrinsic
`git`/`add` action; unintended files should be removed with the file-management
action. The commit must be retried only after the reported state is corrected.
