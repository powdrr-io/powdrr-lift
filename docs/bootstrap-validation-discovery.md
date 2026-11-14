# Bootstrap validation discovery

Structrr bootstrap records how a repository says it validates changes. It
produces a versioned YAML snapshot and a companion Markdown report. The source
manifest remains available as supporting evidence.

Static discovery, execution confirmation, and requiredness are separate facts:

- A declaration is a command or task found in a project file or CI workflow.
- An inferred profile is a candidate suggested by configuration. It is not
  proof that CI runs it.
- An observation records a supported probe or baseline execution, including
  the environment, directory, result, and time.
- Requiredness is `unknown` until branch protection, a ruleset, or equivalent
  repository evidence establishes it. A green check run alone does not make a
  check required.

## Generate and read the outputs

Run the snapshot command from the repository root:

```sh
powdrr-lift bootstrap-structrr --json
```

The JSON summary identifies the YAML snapshot and Markdown report paths. The
command validates and writes both files. It is snapshot-only: its baseline
status is `not_run` unless a supported probe or validation command was actually
run and recorded. The higher-level bootstrap workflow collects declarations,
requests supported probes, and retains failed or unsupported checks as findings.

The YAML `validation_context` describes components, environments, evidence,
input fingerprints, diagnostics, and discovery status. Each
`validation_inventory` row has a stable id, provider/profile, native command,
working directory, environment, selectors/settings, CI origins, provenance,
confirmation, prerequisites, and baseline result. For example:

```yaml
validation_context:
  schema_version: validation-context-v1
  discovery_status: partial
  coverage: ["Python manifests", "GitHub Actions workflows"]
validation_inventory:
  - id: validation:pytest:root
    schema_version: verification-provider-inventory-v2
    provider: pytest
    profile: pytest
    command: [uv, run, pytest, -q]
    execution: {kind: argv, cwd: ., shell: null, script: null}
    provenance:
      declaration: declared
      evidence: [validation-input:.github/workflows/ci.yml]
    confirmation: {level: static, observations: []}
    requiredness: {status: unknown, evidence: []}
    baseline: {status: not_run, observation: null}
```

The Markdown report gives a repository structure overview and one section per
check. It shows the command or native task, execution directory, environment,
applicability, requiredness, source links, confirmation level, baseline status,
and unresolved conditions. Read it together with the YAML: the YAML is the
machine-readable record, while the Markdown explains why each record exists.

## Discovery coverage

The current adapters cover Python project topology and selected native task
runners, common validator commands, and GitHub workflow declarations. The
automated tests exercise these paths in fixture repositories and adapter tests.

| Area | Current coverage | Main tests |
| --- | --- | --- |
| Python components and environment declarations | Root and nested Python projects, requirements-only projects, uv/Conda/pip clues, evidence fingerprints | `tests/test_validation_topology.py` |
| Native task runners | Tox, Nox, Make, pre-commit, Just, Task, Hatch, PDM, Pipenv, and Poe declarations; literal wrappers and unresolved dynamic forms | `tests/test_validation_tasks.py` |
| Common checks | Ruff format/lint, mypy, pytest, stestr, plus selected JavaScript, Go, Cargo, Maven, and Gradle checks | `tests/test_validation_discovery.py` |
| Inventory and evidence | v1 compatibility, v2 validation, references, duplicate ids, dependency cycles, and evidence normalization | `tests/test_validation_models.py`, `tests/test_validation_topology.py` |
| Collection and provider execution | Target-directory pytest collection in a subprocess, exact selectors, context handoff, and unsupported shell semantics | `tests/test_verification_provider.py`, `tests/test_verification_evidence.py` |
| Bootstrap outputs | YAML validation, source anchors, freshness, and paired Markdown rendering | `tests/test_structrr_bootstrap.py`, `tests/test_validation_topology.py` |
| Readiness | Discovered and explicit checks must both pass; changed or unsupported context stays pending | `tests/test_feature_endpoint.py` |

The version-specific behavior exercised so far is intentionally narrow: tox INI
configuration and tox 4 TOML/pyproject forms are covered by
`tests/test_validation_tasks.py`; provider inventories query the pytest or
stestr version from the target command. The corpus does not establish a full
historical support matrix for pytest, Ruff, mypy, environment managers, or
GitHub Actions. If a declaration depends on an unknown tool version, preserve
that uncertainty in the record.

Static discovery is intentionally conservative. A task runner's configuration
does not prove that a particular task is selected. A dependency in a manifest
does not prove that a check is required. Unsupported shell syntax, interpolated
matrix values, dynamic tasks, missing environment details, and inaccessible
remote workflows remain unresolved. The native command and evidence are kept
when available; no fallback command is reported as a passing check.

The adapter set is not a guarantee that every version or configuration form of
each tool is understood. A result is only as specific as its recorded evidence
and target version. Unknown version-sensitive configuration must remain
unresolved rather than borrowing behavior from a newer release.

## Confirmation and execution policy

Static declaration is not execution. A successful version or help probe only
confirms that a tool accepts that probe. A baseline pass requires the recorded
validation command to finish successfully in its declared context.

Supported argv checks use their native token sequence and working directory.
The runner does not create or synchronize environments during inspection.
Profile context is bound to the reviewed worker request; changed context blocks
execution. Providers that do not support a declared shell or script profile
leave it unresolved. Local execution policy bounds per-command time, total time,
and captured output; a timeout or unavailable tool is recorded as such.

Connected GitHub observations require an explicitly configured read-only
connection. Offline discovery still records workflow declarations but cannot
establish branch protection, ruleset requirements, external check behavior, or
remote implementation details. These remain unknown until evidence is
available.

## Extend a custom check

Prefer a native command or wrapper already declared by the repository. To add
an adapter, keep parsing deterministic and source-anchored, preserve command
order and execution context, and add a fixture with a manually reviewed
expectation. If static analysis cannot resolve a task, retain the native entry
and attach an actionable unresolved reason. Do not infer requiredness from a
tool name, package dependency, or successful probe.

The shared validation model checks stable ids, execution shape, component and
environment references, evidence references, and prerequisite cycles. New
evidence must be indexed in `validation_context` and referenced by that id.
Adapter conformance should include a fake executor; subprocess tests should
cover directory/environment isolation, script failure behavior, and timeouts.

## Schema migration

New records use `verification-provider-inventory-v2` and the versioned
`validation_context` section. The v1 reader remains for older snapshots. Legacy
rows are imported with unknown environment and confirmation rather than being
upgraded to stronger claims. Consumers should address checks by stable id, not
by a global tool name; one repository may have multiple variants of pytest or
the same task in different components.

## Reviewed corpus

The pinned corpus lives in `tests/fixtures/validation_corpus/corpus.yaml`. It
records repository revisions, GitHub-reported SPDX identifiers, license
evidence, CI/configuration paths, and manually reviewed obligations. It covers
this repository plus pytest, Django, Flask, Poetry, uv, PDM, Hatch, Conda, Nox,
pre-commit, tox, HTTPX, and Pydantic. Release/publication workflows are not
counted as pull-request validation unless the reviewed workflow establishes
that role.

Run the evaluator on local checkouts at the exact pinned revisions:

```sh
uv run python scripts/evaluate-validation-corpus.py \
  --repo-dir powdrr-lift=/path/to/powdrr-lift \
  --repo-dir pytest=/path/to/pytest \
  --format markdown \
  --output docs/reports/validation-discovery-corpus.md
```

Supply one `--repo-dir ID=PATH` for each corpus repository being evaluated.
Alternatively, `--download` clones and checks out the pinned revisions; `--only
ID` limits an evaluation to selected repositories. The report separates exact
executable discoveries, missed obligations, unreviewed candidates, invented
required checks, command/context mismatches, provider mismatches, evidence
gaps, and unresolved obligations/profiles. Unreviewed candidates are not
classified as invented required checks. The ordinary test suite uses only
local fixtures and never clones repositories or needs credentials.

`docs/reports/validation-discovery-corpus-2026-11-14.md` records the baseline
evaluation at the pinned revisions. Its counts are findings to drive follow-up
adapter work, not a success score: resolved executable discoveries and
legitimate unresolved cases are reported separately.
