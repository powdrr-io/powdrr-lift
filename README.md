# powdrr-lift

GATHER STRUCTURED CONTEXT -> SYNTHESIZE UPDATED CONTEXT -> LEVERAGE CURATED CONTEXT

`powdrr-lift` is an agent persistent memory system. However it is not Yet Another Memory
System. `powdrr-lift` is an opinionated coding agent memory system that:

* Requires the agent to submit a changelog with every PR
* Synthesizes current state from a set of changelogs
* Curates and provides relevant context during design, implementation, and review

`powdrr-lift` is designed for individuals and teams focused on increasing code quality,
increasing code understandability, decreasing token costs, and decreasing time wasted.

## Why Powdrr-Lift?

AI coding assistants are powerful, but they are hard to trust when the only source of
truth is chat history and a pile of markdown notes. `powdrr-lift` adds a structured
change layer so humans and agents can agree on what changed before, during, and after
the code lands.

* Agree on the change hierarchy before implementation - capture intent, decisions,
  file changes, entities, relationships, invariants, and guidance as structured data.
* Validate at a finer granularity - the more structure the system has, the more
  confidently both agents and humans can check coherency and catch drift early.
* Surface the right context - targeted edit, blame, and entity views keep the current
  change connected to its related code areas and prior decisions.
* Keep the workflow opinionated - the repo has strong guardrails on purpose, because
  opinionated systems make it easier to stay aligned and easier to encode your own
  point of view when the work demands it.

In practice, that means less ambiguity, less wasted work, and more confidence that the
next change is still pointing in the right direction.

## How Are We Different?

`powdrr-lift` is intentionally more opinionated than a generic prompt-and-patch workflow.
That opinionatedness is the point: stronger guardrails, stronger validation, and stronger
coherency statements.

**vs. OpenSpec** - OpenSpec is a useful spec-first pattern for AI coding, but `powdrr-lift`
goes further by making the structure more granular and more machine-checkable. That gives
humans and agents higher-confidence validation, clearer lineage, and stronger context across
the full hierarchy of change.

**vs. Spec Kit** - Spec Kit is thorough, but it can feel heavyweight. `powdrr-lift` keeps the
workflow structured without forcing a rigid phase-gate process. The repo is opinionated, but
the artifacts stay directly tied to the change and are designed for continuous iteration.

**vs. Kiro** - Kiro is powerful, but it is tied to a specific IDE and a narrower model/tooling
environment. `powdrr-lift` is repo-native and assistant-agnostic, so you can use it with the
tools and workflows you already have.

**vs. nothing** - AI coding without a structured spec layer means vague prompts, weak traceability,
and a lot of accidental drift. `powdrr-lift` replaces that with coherent artifacts, validation,
and review surfaces that keep the work directionally aligned.

## Design

The next-stage platform design is captured in
[`docs/design/agent-platform-expansion.md`](docs/design/agent-platform-expansion.md).
It describes the specification families, synthesis workflows, review flows,
and context endpoints that will extend the current skill platform.

## How It Works

1. Install `powdrrlift` skills to your favorite coding agent
2. Prompt and use your agent, the agent will pickup up skills automatically
3. Explicitly use the skills for even better planning, coding output, and code reviews
4. Explore the `powdrrlift` UI to get insights into the reasons and relationships in your code

## What You Will Notice

* Code reviews with granular context about why each change was made
* Plans that account for past decisions and explicitly call out where things need to change
* Code generation that stays on task and avoids throwaway work
* Less tokens spent with more output generated

## Get Started

(Coming Soon)
Mac
```brew install powdrr-lift```


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
