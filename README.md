# powdrr-lift

## The software factory for agents that need to ship

Most coding agents are impressive right up until they are not. Give one a large repo,
a long conversation, and permission to act, and familiar failure modes appear quickly:

* Context gets compressed. The agent forgets why it started, loses the dependency chain,
  and edits the first plausible file it finds.
* A small request turns into an unreviewed refactor because “clean this up” becomes the
  agent's new definition of success.
* A failed patch is retried with slightly different wording until the agent declares
  victory, leaving a half-fixed codebase behind.
* An autonomous run reports “done” without proving that the change matches the request,
  the repository's rules, or the decisions made earlier.

**Powdrr Lift is built for the other outcome.**

It is a software factory: a human-facing workflow chat client for directing work, plus
fully autonomous agents that can keep a task moving through planning, implementation,
validation, review, and handoff. The factory is powered by structured, self-validating
workflows instead of an ever-growing prompt. Every task has an explicit shape, every
step has a purpose, and every action is checked against the step that authorized it.

That is the fundamental difference. A generic agent can lose the plot and improvise a
new one. Powdrr Lift cannot silently turn a focused workflow into free-form work: the
agent operates inside a well-formulated plan, receives targeted context, uses declared
actions, works in a dedicated worktree, and stops when it cannot make validated progress.
The result is autonomy with a contract.

## Why engineers use Powdrr Lift

### Direct the work in chat—or let it run

Start with a conversation when the problem is still ambiguous. The workflow chat client
matches the request to a well-formulated plan, asks the questions that matter, and shows the
agent's current step and context. Once the work is specified, autonomous workflows can
continue driving it forward without requiring a human to translate every decision into
the next prompt.

### Keep agents grounded in the repository

Powdrr Lift gathers the relevant specifications, decisions, entities, relationships,
invariants, files, and prior workflow context before an agent acts. It does not ask a
model to reconstruct the whole project from chat history. It gives the model the smallest
useful slice of the repository, then records the resulting decisions and execution events
for the next step and the next review.

### Make the workflow executable and self-validating

Skills are checked-in YAML or JSON definitions with ordered steps, declared tools,
expected outcomes, and composable subskills. The runtime validates skill directories,
resolves dependencies, validates actions against the active step, detects repeated
failures and stalled roundtrips, and persists an execution summary. If an agent cannot
produce a coherent next action, the workflow has a defined stop—not permission to invent
one.

### Isolate changes before they become damage

Every invocation resolves a worktree context. Work happens on a dedicated branch instead
of silently mutating the primary checkout, and the workflow carries its worktree and
pull-request context forward. This makes autonomous execution inspectable, reversible,
and ready for human review.

### Preserve the why, not just the diff

Powdrr Lift's structured change layer captures intent, decisions, affected entities,
relationships, invariants, and guidance alongside the code. Reviews can ask not only
“what changed?” but “why was this the right change, what does it depend on, and what must
remain true?” That context stays useful long after the original conversation is gone.

## How Powdrr Lift is different

Powdrr Lift is intentionally more opinionated than a prompt-and-patch assistant. The
guardrails are the product. They turn an agent from a clever text generator into a
repeatable engineering system:

| Generic agent behavior | Powdrr Lift behavior |
| --- | --- |
| Loses context and guesses what to do next | Gathers typed, relevant context for the active step |
| Expands a small request into unrelated edits | Uses declared actions tied to a checked-in workflow |
| Repeats a failing action until the conversation ends | Detects stalls, records the failure, and stops |
| Claims completion based on its own narrative | Produces validated artifacts and an execution summary |
| Modifies the checkout where it was launched | Works in a dedicated worktree with reviewable state |

This is not a thin memory layer bolted onto a chatbot. It is a structured change system
for software teams that want more output without surrendering direction, traceability, or
the ability to say no.

## What you get

* Human-guided workflow chat for ambiguous or high-leverage work.
* Autonomous skill flows that compose planning, implementation, validation, and review.
* Structured specifications and changelogs that preserve intent across revisions.
* Targeted context, edit, blame, and entity views grounded in repository state.
* Worktree-isolated changes, explicit progress, and durable execution summaries.
* More useful code reviews, fewer wasted tokens, and less cleanup after the agent leaves.

## Design

The next-stage platform design is captured in
[`docs/design/agent-platform-expansion.md`](docs/design/agent-platform-expansion.md).
It describes the specification families, synthesis workflows, review flows,
and context endpoints that will extend the current skill platform.

The current specification documents live under `docs/specs/<work-item-name>/`
and share the `https://powdrr.io/schemas/specification-v1` schema.

## How It Works

1. Install Powdrr Lift in the repository where you want work done
2. Start workflow chat and describe the outcome you want
3. Answer the questions that shape the plan, or let an autonomous workflow continue
4. Review the validated artifacts, execution summary, and proposed code changes

## What You Will Notice

* Code reviews with granular context about why each change was made
* Plans that account for past decisions and explicitly call out where things need to change
* Code generation that stays on task and avoids throwaway work
* Less tokens spent with more output generated

## Get Started

### For Codex Proxy Recording

Use the built-in proxy when you want Codex to send OpenAI requests through a local
recording layer. The proxy forwards requests transparently and writes each request
and response to disk.

1. **Start the proxy**
   ```bash
   powdrr-lift openai-proxy --repo-root . --upstream-base-url https://api.openai.com
   ```

   - The proxy listens on `http://127.0.0.1:8787/v1` by default.
   - Recorded exchanges are written to `.powdrr/openai-proxy/` by default.
   - Use `--log-dir <path>` if you want to store recordings somewhere else.

2. **Point Codex at the proxy**
   - Set `OPENAI_BASE_URL=http://127.0.0.1:8787/v1` before launching Codex.
   - Keep your normal `OPENAI_API_KEY` in place; the proxy forwards the auth
     header to the upstream API.

3. **Use Codex normally**
   - After the one-time setup, Codex talks to the proxy as if it were the
     OpenAI API.
   - You can inspect the recorded request and response bodies later in the log
     directory.

### Workflow chat

Use the terminal workflow chat agent to match a request against the checked-in
workflow templates, ask follow-up questions, and generate a validated task
directory.

```bash
powdrr-lift workflow-chat --repo-root . --templates-dir templates --output-dir docs/workflows/implement-a-feature
```

Durable tasks can be processed with `process-workflow-task`. Its `auto` default
uses the same credential and provider-priority lookup as `workflow-chat`; pass
`--provider` to override it explicitly. For example, when DeepInfra is the
highest-priority configured provider, it reads `DEEPINFRA_API_TOKEN` (or
`DEEPINFRA_API_KEY`):

```bash
powdrr-lift process-workflow-task --workflow-dir docs/workflows/implement-a-feature --repo-root .
```

Human-assigned tasks can be handled through the same durable workflow protocol:

```bash
powdrr-lift process-human-task --workflow-dir docs/workflows/implement-a-feature --repo-root .
```

The command selects the first ready human task, or a specific task with
`--task-id`, shows its task and upstream context, claims it, prompts for an
answer, records the answer as `output_state.answer`, and publishes the updated
workflow state. Use `--answer` or `--answer-file` for non-interactive use and
`--role reviewer|decider` to filter discovery.

If an agent stops partway through and leaves Git state uncertain, inspect the
run by its work-item id (the proposed PR id), then clean only its disposable
task artifacts:

```bash
powdrr-lift workflow-recovery --proposed-pr-id feature-17 --repo-root .
powdrr-lift workflow-recovery --proposed-pr-id feature-17 --repo-root . --cleanup
```

Inspection is read-only. Cleanup closes related open task PRs, removes task
branches, worktrees, claims, and uncommitted workflow-directory artifacts, and
preserves the `powdrr/feature-17` integration branch and worktree as the last
consistent checkpoint. Add `--json` for machine-readable state.

### Command-key clipboard shortcuts

The workflow chat TUI uses Textual's Kitty keyboard protocol support. Textual
enables the protocol on startup (`CSI > ... u`) and restores the terminal on
exit (`CSI < u`), allowing supported terminals to deliver the standard
`Cmd+C`, `Cmd+X`, and `Cmd+V` keys as `super+c`, `super+x`, and `super+v`.

This requires a terminal that supports the Kitty keyboard protocol, such as
recent iTerm2, Kitty, WezTerm, or Alacritty. In iTerm2, enable “Report
modifiers using CSI u” in the profile's Keys preferences if Command keys are
not being delivered. Apple Terminal does not support this protocol and will
continue to handle Command shortcuts itself; use `Ctrl+C`, `Ctrl+X`, and
`Ctrl+V`, or use a Kitty-compatible terminal instead.

- The command uses `OPENAI_API_KEY` by default and also accepts `CODEX_API_KEY`.
- If those are unset, it falls back to the local Codex auth cache in
  `~/.codex/auth.json` or `$CODEX_HOME/auth.json`.
- Use `--provider anthropic` with `ANTHROPIC_API_KEY` for Claude models.
- Use `--provider zai` with `ZAI_API_KEY` for `glm-5.2` and other GLM models.
- Use `--provider deepinfra-cheap` with `DEEPINFRA_API_TOKEN` to use
  `deepseek-ai/DeepSeek-V4-Flash` for every workflow capability. In `auto`
  mode, this provider is selected for non-Claude models whenever DeepInfra
  credentials are available.
- The default mapping combines remote GLM models with local Qwen execution.
  Before starting a workflow that selects Qwen, download the Q5_K_M GGUF
  shards with `powdrr-lift download-qwen-model`. The command caches them from
  `Qwen/Qwen2.5-Coder-14B-Instruct-GGUF` in `<project-root>/.powdrr/models`
  and reuses them across worktrees. The local runtime is optional; on Apple Silicon, install it
  with Metal enabled:
  `CMAKE_ARGS="-DGGML_METAL=on" uv sync --extra local`.
  The local client defaults to a 24,576-token context for reliable GPU
  execution on Apple Silicon. Set `POWDRR_LOCAL_MODEL_CONTEXT` to adjust it
  for a machine with more or less available memory.
- z.ai uses the OpenAI-compatible endpoint `https://api.z.ai/api/paas/v4/`.
- Workflow skill steps and every routing/action response carry an `llm_type`
  descriptor. The default mapping routes `high_reasoning` to `glm-5.2`,
  `standard_reasoning` to `glm-4.7`, `simple_task` and `fast_iteration` to
  `Qwen/Qwen2.5-Coder-14B-Instruct`, `long_context` to `glm-5.2`, and
  `vision` to `glm-4.6v`. Selecting the Qwen model automatically uses the
  local provider, so a workflow can use remote GLM models and local Qwen
  roundtrips together. The descriptor selected in one roundtrip controls the
  model used for the next roundtrip.
- Set `OPENAI_BASE_URL` to point at the local proxy if you want to record the
  requests.
- If you omit `--output-dir`, the generated task set is written to a temporary
  directory and summarized on stdout.

### Skill flows

Skills are reusable workflow definitions. They are checked in as YAML
files under `skill-definitions/` and can be selected by workflow chat, composed
as nested skills by another skill, or used as the definition behind durable
agent-workflow tasks. Every invocation resolves a worktree context first: it
reuses the current dedicated worktree when one exists and creates one when the
invocation starts from the primary checkout.

- Use `when_to_use` to describe the situations where the skill applies.
- Use `steps` to list the ordered actions to follow.
- A step can reference other skills with `uses_skills`; referenced skills run in
  the same worktree before the parent step continues.
- The skill loader can validate a directory of skills and ensure every
  referenced skill exists.

### For Mac
```bash
brew install powdrr-lift
```

### Available Skills

This repository includes 10 reusable skills for planning, implementation, validation,
review, and repository-state management:

1. **bootstrap** - Analyze repository structure and source code to identify taxonomy-compliant entities, relationships, and features. Generate a validated system specification document from the analysis and commit it.

2. **code-edit-context** - Use when you are about to edit code and need index-backed context for a file and line ranges.

3. **implement-pr** - Find a proposed PR by fuzzy search, inspect the full proposal, validate it against the current indexed specs and changelogs, implement the requested changes, review the proposal again, and then optionally generate the matching PR changelog.

4. **prepare-pr-changelog** - Use when preparing a pull request or getting a PR ready. Guides the agent through the PR changelog workflow with powdrr-lift.

5. **review-pr-changelog** - Use during code review when the change includes a changelog. This skill complements general code-review skills; do not replace normal review. Check for the changelog first, validate it, then review each change against the PR intent and report the feedback.

6. **specify-architecture** - Create, fill, and validate architecture specification templates with the repository's architecture-specification CLI or MCP endpoints.

7. **specify-implementation** - Create, fill, and validate implementation specification templates with the repository's implementation-specification CLI or MCP endpoints.

8. **specify-prs** - Create, fill, and validate proposed PR specification templates with the repository's pr-specification CLI or MCP endpoints.

9. **specify-system** - Create, fill, and validate system specification templates with the repository's system-specification CLI or MCP endpoints.

10. **synchronize-code-and-state** - Generate the current codebase-state snapshot, compare it to the source tree and changelog index, and reconcile mismatches by changing code and/or the changelog while preserving the repo's intent.

## Background

All memory systems operate bypointing the agent at the most relevant aspects of an ever-growing
context. The standand approach is to treat context as an ever-growing conversation between 
human and agent. Conversations can be difficult to follow even for participants, necessitating
clarifying questions. Trying to understand a conversation post hoc as an observer is an imperfect
process, leading to semantic loss.

`powdrr-lift` takes a different approach. The human-agent conversation builds a great shared understanding
of intent, decisions, affected entities, and reasoning along with some artifacts like code, documents, images, and models.
`powdrr-lift` provides a way to capture the intent/decisions/entities/reasoning as an additional
structured artifact. This structure removes the ambuiguity of the conversation format. This further enables
a high fidelity way to synthesize changes over hundreds or thousands of revisions into a highly detailed and
accurate semantic graph.

'powdrr-lift' leverages the semantic graph in future operations. The next operation after code changes and
validation is review. The semantic graph information helps inform the review in two key ways:

* Information in the current change helps inform the reviewer on the granular decisions and reasoning
* Information from previous changes helps inform the reviewer on previous decisions, what is ok to change and what should not be changed

'powdrr-lift' leverages the semantic graph for planning. 
