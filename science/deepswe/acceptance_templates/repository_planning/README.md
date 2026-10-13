# Repository-grounded planning experiment

`pipeline.yaml` is a validated Procedrr declaration, executed by the existing evaluator. It owns the ordering and branching:

1. Extract Python symbols once from the prepared base repository (AST names, signatures, docstrings, source ranges and hashes).
2. Retrieve candidates per frozen routed requirement group using lexical scores, then expand to parent classes and syntactic references. Keep ten production matches, three existing-test usages, and up to 22 total candidates. These are recall limits, not correctness guarantees or resolved type edges.
3. Flash selects relevant declarations. Deterministic operations gather their source excerpts and preserve lexical alternatives even when selection misses them.
4. Flash proposes concrete scenarios. Jev checks premises; Flash resolves outcomes; Jev reviews outcomes. Separate decisions; no rewriting step.
5. Render the six-section prompt. Unresolved decisions or recorded provider failures skip added criteria while preserving original requirements. No human wait, coding agent or test execution.

Setup uses the task's `environment/Dockerfile`, with no task-directory/solution/validation-patch mount. Analysis container has no network, a read-only filesystem and writable artifact/tmp directories. Providers run on the host; Typesafe.ai credentials do not enter the container. Structrr bootstrap inspects metadata without running checks. Verify exact base SHA and clean checkout at setup and completion.

Use the shared Python interpreter with `PYTHONPATH=<worktree>/src:<worktree>` for these modules:

- Build the task environment: `docker build -t <base-image> <task>/environment`.
- `python -m science.deepswe.acceptance_templates.repository_planning.prepare context --task <task> --output <fresh-setup> --base-image <base-image>`.
- `docker build -t <analysis-image> <fresh-setup>/build-context`.
- `python -m science.deepswe.acceptance_templates.repository_planning.prepare start --output <fresh-setup> --image <analysis-image> --container <fresh-container> --base-sha <task-base-sha>`.
- `python -m science.deepswe.acceptance_templates.repository_planning.run --setup <fresh-setup> --output <fresh-results>`.

The initial runner is scoped to GraphQL and reuses the frozen production routing/splitting input bundled in `baseline/`. Symbol extraction/retrieval and setup operations are generic Python machinery. No embeddings yet; dependencies are installed but external-library symbols are not indexed. Long method excerpts and class headers are explicitly marked truncated. Missing symbols, irrelevant candidates, invalid citations and reviewer mistakes remain evaluation targets.

Full request/response checkpoints and raw Flash captures, Procedrr events, ranked candidates, source reads, environment fingerprints and final prompt are preserved. Solution patches are used only for post-call manual audit. Tests remain disabled by user request.
