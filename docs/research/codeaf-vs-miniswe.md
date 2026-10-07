# CodeAF as a potential Powdrr coding provider

Research date: 2026-10-07.
CodeAF revision observed: `6246d71545f511493d9d1434fe10c133abe8a642`.
This is a source and documentation review, not an independently reproduced benchmark.

## Assessment

CodeAF combines a coding harness with a higher-level orchestration system. Its
built-in `senior-dev` agent is the relevant candidate to compare with MiniSWE.
The full CodeAF factory overlaps with responsibilities Powdrr already owns.

Recommendation: keep MiniSWE as the baseline and evaluate senior-dev as an
optional coding provider before considering a replacement. Published results
justify an experiment, but do not establish that senior-dev would improve
Powdrr's outcomes.

## Architecture and execution

| Component | Responsibility | Comparison for Powdrr |
| --- | --- | --- |
| CodeAF factory | Interpret requests, schedule concurrent tasks, select models, check work, and integrate changes | Overlaps with orchestration |
| Built-in senior-dev | Explore a repository, edit code, execute commands, and submit a change | Alternative coding worker |

CodeAF contains its own agent implementation rather than simply wrapping
MiniSWE or OpenCode. Its factory supports work across projects and separate
worker, planner, and checker model roles. The project describes itself as an
early preview. These capabilities and maturity claims come from the
[CodeAF README](https://github.com/Agent-Field/CodeAF/blob/6246d71545f511493d9d1434fe10c133abe8a642/README.md).

Senior-dev uses one coding agent, without separate planner or reviewer agents
inside its implementation loop. It preserves the brief as a specification,
explores the repository, records a verification command and requirements
checklist, implements, and explicitly submits. Submission freezes the tree;
the harness then runs the project's build and tests without a model. It also
preserves the brief across context compaction.

Its default Git behavior creates a private repository copy and branch.
`--in-place` works without Git commits even inside a repository. Ended runs
cannot resume their conversations; a new run can work from the previous tree.
These details are documented in the
[senior-dev manual](https://github.com/Agent-Field/CodeAF/blob/6246d71545f511493d9d1434fe10c133abe8a642/internal/manual/chat/senior-dev.md).

The command choice matters. `codeaf do` invokes broader planning and delivery
checking; `codeaf senior-dev` invokes the specific coding agent used in the
advertised DeepSWE comparison. `codeaf exec` is another entry point for a worker
without a plan, and should not be assumed equivalent to senior-dev. The
[headless contract](https://github.com/Agent-Field/CodeAF/blob/6246d71545f511493d9d1434fe10c133abe8a642/docs/HEADLESS.md)
describes command behavior, model pins, JSON results, and exit statuses.

MiniSWE emphasizes a minimal agent loop and Bash-based interaction. OpenCode
provides a broader terminal coding-agent interface. Senior-dev belongs in this
coding-harness comparison, while CodeAF's factory adds another layer above it.
See the [MiniSWE repository](https://github.com/SWE-agent/mini-swe-agent) and
[OpenCode documentation](https://opencode.ai/docs/).

## Published DeepSWE evidence

CodeAF reports the following comparison on 113 tasks with one attempt per
harness, using DeepSeek V4 Flash through OpenRouter and a three-hour task budget:

| Harness | Solved | Solve rate | Billed cost per task | Mean agent time |
| --- | ---: | ---: | ---: | ---: |
| senior-dev | 62 | 54.9% | $0.22 | 54 min |
| MiniSWE | 56 | 49.6% | $0.38 | 44 min |
| OpenCode | 30 | 26.6% | $0.50 | 48 min |

The authors explicitly say the senior-dev/MiniSWE difference is not
statistically resolved. There is one seed per harness. Senior-dev omitted
temperature and top-p, whereas the other harnesses supplied 1.0 and 0.95,
respectively. Missing verifier outcomes count as unsolved; OpenCode has one.

The newer senior-dev result of 88/113 (77.9%) uses DeepSeek V4.1 Flash without
corresponding comparison runs. It cannot establish superiority to MiniSWE on
that model. All figures are project-reported, not independently verified here.
Source: [DeepSWE results and limitations](https://github.com/Agent-Field/CodeAF/blob/6246d71545f511493d9d1434fe10c133abe8a642/docs/benchmarks/deepswe/README.md).

## Fit with Powdrr

Powdrr's existing
[coding provider](../../src/powdrr_lift/workrr/coding_agent.py) binds MiniSWE to
the task worktree, supplies the implementation prompt and model, owns the
wall-clock timeout, records trajectories, and checks submission status. Its
[session continuation adapter](../../src/powdrr_lift/minisweagent_session.py)
restores the previous conversation for follow-up attempts.

The following are engineering assessments, rather than measured outcomes:

- Senior-dev's submission protocol, context management, and verification could
  improve completion quality or cost.
- Integrating the full factory would duplicate planning and acceptance work
  and make failures harder to attribute to the worker or orchestrator.
- A senior-dev adapter must reconcile workspace ownership, Git behavior,
  budgets, diagnostics, submission state, and follow-up attempts. Its lack of
  conversation resume differs from the existing MiniSWE integration.
- Passing the harness's checks must not replace Powdrr's independent acceptance
  gates. Project tests can pass without satisfying the requested behavior.

## Proposed evaluation

1. Pin both harness versions and the exact model, provider, reasoning settings,
   sampling settings where supported, and wall-clock and spending budgets.
2. Run identical tasks in fresh isolated containers with identical repository
   states and official external verifiers. Record unavoidable configuration
   differences explicitly.
3. Compare standalone MiniSWE with standalone senior-dev, then compare both
   under Powdrr using equivalent worker briefs and acceptance rules.
4. Record solve rate, billed cost per task and per solved task, wall time,
   timeout and submission failures, diagnostic completeness, and repair
   behavior. Use paired task outcomes and repeated runs where feasible.
5. Verify that changes remain in the assigned workspace and are collectable
   by Powdrr, including on incomplete or interrupted runs.

Adopt senior-dev only if the measured benefit survives Powdrr's orchestration
and the adapter preserves its execution and acceptance boundaries. No provider
implementation or benchmark run is included in this research change.
