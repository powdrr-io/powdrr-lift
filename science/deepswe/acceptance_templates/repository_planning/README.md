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

## Contract and acceptance experiments

These are standalone science workflows. They do not replace production prompt
generation. The GraphQL comparison in
[`requirement-contract-v3/RESULTS.md`](../runs/requirement-contract-v3/RESULTS.md)
found that these variants still produce incorrect criteria.

- `contracts --setup <setup> --output <fresh-run>` extracts scoped, cited facts
  separately from desired behavior. Each frozen requirement gets behavior
  analysis, criteria proposals, premise checks and outcome checks. Procedrr
  controls the sequence, semantic overlap checks and rendering.
- `dependencies --setup <setup> --output <run>/dependency-evidence.json` captures
  installed GraphQL result declarations and directive definitions. This is
  GraphQL-specific evidence gathering; installed result types may describe a
  newer wire protocol than the task requests. They are not automatically a
  compatibility requirement for the new API.
- `refine --setup <setup> --generation <run> --output <fresh-review>` checks
  every proposed criterion against the original instruction and source,
  without treating generated behavior or previous approvals as authority.
  It permits one declared repair, then checks again.
- `atomic_conditions --setup <setup> --generation <run> --evidence <evidence>
  --output <fresh-run>`
  constructs inputs/operation, observation and counterexample in separate
  decisions. It does not show earlier generated outcomes. The original
  `atomic-contract-v1` run reranked the generation's **batch** excerpts, not the
  full index; the follow-up uses requirement-scoped packets via `--evidence`.
- `requirement_evidence --setup <setup> --generation <run> --output <fresh-run>`
  indexes the repository once, retrieves against the full symbol set separately
  for each routed requirement, then captures bounded source excerpts with
  source hashes and explicit truncation flags. Exact CamelCase receivers pin
  their public methods in declaration order. Candidate rankings remain in each
  packet so retrieval errors can be audited. This recovered DSL declarations
  that batch retrieval missed, but the follow-up prompt run emitted no
  additional acceptance criteria; see the recorded
  [retrieval follow-up](../runs/atomic-contract-v3/RESULTS.md). Pass that
  evidence directory with `atomic_conditions --evidence <evidence>`.
- `consolidate --setup <setup> --output <run>` runs only the consolidation tail
  on saved contract-generation decisions. It makes additional recorded Jev
  calls, so it is not an offline renderer.

Use the same module prefix as above:
`python -m science.deepswe.acceptance_templates.repository_planning.<module>`.
For archived runs, extract `checkpoints.tar.gz` into their run directory to
restore reads and decisions. Model request hashes allow exact completed
checkpoints to be reused. Historical declarations preserve the actual prompts;
published error-handling helpers have subsequently been hardened.

The query checks parse documents and inspect known directive locations. They
do **not** validate the task schema, field types, directive arguments, complete
wire fixtures, or DSL calls. A successful query check is not proof that an
example is valid. Model-approved coverage is also not gold solution coverage.
Unresolved or failed decisions retain the original requirements and continue
headlessly. Local evaluator failures preserve the partial prompt and error.

`archive <run> --all-files` stores raw calls, proposals, reviews, source reads
and declarations together and verifies every archived file hash. The small
top-level summaries and prompts remain reviewable; rejected proposals are
retained in the archive. No model sees solution or validation patches.
