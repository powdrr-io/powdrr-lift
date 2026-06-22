# Making Coding Agents Operate Like Professionals

Coding agents are already good at producing code that looks right. That is not the hard part.

The hard part is producing change that behaves like professional software work.

In a real team, code is never just code. It is tied to intent, decisions, invariants, guidance, provenance, and review. If an agent cannot preserve that structure, it creates output that is locally plausible and globally expensive. Humans then have to reconstruct context, rediscover decisions, and repair the coherence the agent lost.

The answer is not to ask agents to be smarter in the abstract. The answer is to give them the same kind of structure we expect from a professional engineering process.

This repository is a reference implementation of that idea.

## 1. Stop treating the agent like autocomplete

Autocomplete predicts text.

A professional contributor works inside a system:

- the goal is explicit
- the decisions are documented
- the constraints are visible
- exceptions are stated
- review checks the result against the rules

That is the difference between a coding toy and a coding workflow.

If you want an agent to behave well, do not give it only a prompt and a codebase. Give it a hierarchy of context that describes what the change means, not just what files it touches.

## 2. Model change as a hierarchy, not a flat patch

Software change is hierarchical.

At the top is the outcome:

- What problem are we solving?
- What is the intended result?
- What does success look like?

Below that are decisions:

- What did we choose?
- What did we reject?
- What tradeoff did we accept?

Below that are invariants:

- What must remain true?
- What cannot be broken?
- What is the rule the change must respect?

Below that are guidance:

- How should people use this?
- What should reviewers check?
- What operational note matters here?

And below all of that is implementation:

- Which files changed?
- Which spans changed?
- What code moved, and why?

The mistake most agent workflows make is to collapse all of that into a single diff. The result is information loss. The change still exists, but the meaning is gone.

## 3. Use a changelog as the contract for change

This repo treats the changelog as a first-class artifact.

The changelog v2 format is not a decorative summary. It is a structured contract that captures:

- intent
- decisions
- file changes
- entities
- entity relationships
- invariants
- guidance

That matters because it makes the change machine-readable at every level of the hierarchy.

The agent can author it.
Validation can check it.
The index can ingest it.
Review can reason over it.

That is the key move. The system is no longer asking a reviewer to reconstruct the change from scattered clues. The change is represented as structure from the start.

## 4. Validate coherency, and allow explicit exceptions

Validation is where structure becomes force.

Without validation, a changelog is just narrative. With validation, it becomes a contract.

This repo validates more than syntax. It checks that the changelog matches the branch diff, that IDs are unique, that references are valid, and that the proposed change is coherent with the repository rules.

That matters because agents are very good at producing something that looks complete while quietly drifting away from the actual change.

Validation is the brake pedal.

It keeps the agent directional. It prevents the workflow from accepting a result that is internally consistent but externally wrong.

It also needs to allow explicit exceptions. Sometimes a new decision supersedes an older one. Sometimes an invariant changes. Sometimes the right thing is to break the old rule and replace it with a better one.

The important part is not pretending the exception does not exist. The important part is recording it clearly enough that the system and the reviewer can see it.

## 5. Surface targeted context at every level

A professional workflow does not dump the entire repository into the reviewer’s lap.

It gives the right context for the task.

This repo does that with tools like:

- `get_edit_context`
- `get_invariants`
- `get_current_decisions`
- `get_entity_references`
- `get_entity_relationships`
- `get_blame_view`

That is the right shape of leverage.

If an agent is reviewing a changed span, it should not just see the lines in that file. It should also see:

- the provenance of the change
- the entities affected by the span
- the other code areas edited in the same changelog
- the current decisions that still apply
- the invariants that must still hold

That is what turns a code review from archaeology into inspection.

It also matters for humans. If the system can point directly at the relevant slice of context, the team spends less time rediscovering what the change was supposed to do.

## 6. Review as structured questions, not a vibes check

The best review process is not a single yes or no.

It is a sequence of small, specific questions.

For each change, ask:

- Does this support the stated intent?
- Does it honor the current decisions?
- Does it preserve the invariants?
- If it introduces a new decision, does it explicitly supersede the old one?
- What sibling code areas were edited in the same changelog?
- Are those areas being reviewed too?

This is where the hierarchy pays off.

The review is not guessing at meaning. It is checking meaning at each level of the structure.

That is also why the review skill in this repo is explicit about using separate subagents for current decisions and invariants. Different questions deserve different checks. The more structured the review, the less likely it is to miss a mismatch hiding under a plausible-looking patch.

## 7. Why this is basically the V-model for agents

The V-model works because it connects requirements to verification.

On one side, you define what the system should do. On the other side, you verify that the implementation actually does it.

That is exactly what a professional agent workflow should do.

On the left side:

- intent
- decisions
- invariants
- guidance

On the right side:

- file changes
- indexed provenance
- validation
- review

This is why the model matters so much in regulated industries and other high-consequence environments. You do not just want code that compiles. You want traceability, reviewability, and a defensible line from requirement to implementation.

The agent does not replace that process.

It becomes part of it.

## 8. What changes for the team

The common fear is that more structure will slow the team down.

In practice, good structure does the opposite.

The agent can generate the artifact.
The validator can check the artifact.
The index can synthesize the current state.
The review tools can surface the right context.

That means the team spends less time on clerical work and more time on judgment.

It also means less wasted work:

- fewer context resets
- fewer fuzzy reviews
- fewer changes that drift away from the goal
- fewer handoffs where nobody can tell what is still true

In this model, the agent is not extra overhead. It is a force multiplier that handles the documentation, the indexing, and the repetitive consistency checks.

That is liberating because it keeps the team focused on decisions instead of rediscovery.

## 9. Practical takeaways

If you want coding agents to operate more like professionals, start here:

1. Make change hierarchical.
   - Capture intent, decisions, invariants, guidance, and implementation together.

2. Make validation meaningful.
   - Check coherence, not just format.
   - Allow explicit exceptions when the change really needs them.

3. Make context targeted.
   - Surface the exact spans, sibling code areas, and semantic references the task needs.

4. Make review structured.
   - Ask small questions at each level of the hierarchy.
   - Do not rely on a single vague approval step.

5. Make the system directionally useful.
   - The agent should always be able to point back to the goal and show how the change stays aligned with it.

This repo is one concrete way to build that workflow.

## 10. Close: the real promise of coding agents

The real promise of coding agents is not just faster code.

It is a workflow that produces better software with less wasted motion.

If the system can describe the outcome hierarchically, validate coherency, surface targeted context, and structure review around bite-sized questions, then agents stop feeling like a source of churn.

They start acting like disciplined contributors.

That is what it means to make coding agents operate like professionals.
