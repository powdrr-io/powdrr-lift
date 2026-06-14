# powdrr-lift

GATHER STRUCTURED CONTEXT -> SYNTHESIZE UPDATED CONTEXT -> LEVERAGE CURATED CONTEXT

`powdrr-lift` is an agent persistent memory system. However it is not Yet Another Memory
System. `powdrr-lift` is an opinionated coding agent memory system that:

* Requires the agent to submit a changelog with every PR
* Synthesizes current state from a set of changelogs
* Curates and provides relevant context during design, implementation, and review

`powdrr-lift` is designed for individuals and teams focused on increasing code quality,
increasing code understandability, decreasing token costs, and decreasing time wasted.

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