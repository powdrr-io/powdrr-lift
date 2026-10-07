# Bootstrap Structure and Validation Discovery Implementation Plan

Status: proposed; implementation is in progress. Validation discovery adapters
and context records are present, but the end-to-end bootstrap contract and
acceptance gate below remain incomplete.

## Objective and delivery contract

Improve bootstrap so a coding workflow can determine how a target Python
repository validates changes locally and on GitHub. Produce two companion
deliverables from the same normalized findings:

1. A validated Structrr YAML document containing the existing source model
   and a versioned, executable validation inventory with provenance.
2. Human-readable Markdown containing a short overview of code structure,
   followed by every validation command, its execution context, evidence,
   confirmation level, and unresolved requirements.

Keep the existing source manifest as a supporting artifact. Probe logs and
GitHub observations are supporting evidence, not additional user-facing
reports. The Markdown must explain why a finding is believed to be true;
it must not merely repeat a list of tools.

Implement every work package below in order. A package is complete only when
its listed acceptance cases pass. Smaller PRs are encouraged, but the overall
work is incomplete until all packages and the final completion gate pass.

## Repository findings that motivate the changes

These observations describe the code inspected at revision `b7ef6594`.
Resolve symbols by name if line numbers or file layout have changed.

| Current boundary | Existing behavior | Required change |
| --- | --- | --- |
| `structrr/validation.py::discover_validation_profiles` | Root manifest detection, substring tool detection, implicit `uv run`, whitespace splitting of CI commands, one result per name | Evidence-based discovery with explicit contexts and distinct variants |
| `structrr/bootstrap.py::_build_document` | Calls discovery and creates `tools` plus `validation_inventory`; tool provenance uses one preferred file | Discover once and serialize precise evidence for each check |
| `structrr/bootstrap.py::_IGNORED_PREFIXES` | Excludes `.github/` from tracked source inventory | Separate validation evidence enumeration from source-model exclusions |
| `structrr/bootstrap.py::BootstrapResult` | Returns YAML and manifest paths | Also return Markdown path and discovery completeness |
| `workrr/feature_endpoint.py::_bootstrap_validation_profiles` | An explicit command replaces discovered checks; empty discovery becomes `true` | Add explicit checks without dropping repository obligations; report unknown validation |
| `workrr/coding_agent_validation.py::ValidationRunner` | Runs argv at repository root and removes `VIRTUAL_ENV` | Execute the recorded directory, environment, shell, and prerequisite graph |
| `workrr/verification_provider.py::PytestVerificationProvider.inventory` | Imports pytest into Powdrr and changes process-wide cwd | Collect in a subprocess using the target environment |
| `workrr/feature_endpoint.py::_ensure_current_baseline` | Checks section versions and optional snapshot digest | Also check validation inputs and companion artifact freshness |
| `skill-definitions/bootstrap-code-structure.yaml` | Reuses an existing artifact immediately; requires successful tool invocations | Check freshness; distinguish invocation confirmation from validation success |

An end-to-end trial on cachetools exposed a concrete evidence integration
failure: tox profiles currently put raw paths such as `tox.ini` and
`.github/workflows/ci.yml` in `provenance.evidence`, while `validation_context`
indexes those same inputs as `validation-input:<path>`. Strict reference
validation therefore rejects the generated snapshot even though both files
are present. Normalize adapter evidence through the shared evidence index
before serializing profiles; do not weaken dangling-reference validation.

Discovery on this repository currently returns Ruff format, Ruff lint, mypy,
and pytest. `.github/workflows/ci.yml` also invokes workflow-definition
validation, `scripts/run-deterministic-workflow-suite.sh`, and conditional
workflow tuning. Those custom validation steps must become acceptance cases.

The existing `docs/plans/polyglot-repository-and-go-support.md` proposes
component-aware profiles. Reuse an implemented component model if present;
otherwise introduce the minimal Python component records described here.
Do not implement a second competing component registry. Preserve current
non-Python discovery through adapters and regression cases.

## Architectural decisions

- Structrr owns declarations, relationships, provenance, and persisted
  findings. Workrr owns tool execution and runtime evidence.
- Bootstrap orchestrates discovery and optionally requests probes through an
  injected Workrr executor interface. Importing Structrr must not import
  Workrr or execute repository code.
- Deterministic parsers produce ordinary findings. Agent-assisted analysis
  may propose additional findings for opaque scripts; all proposals need
  source anchors and the same schema validation. Agent output cannot mark a
  probe as executed or an unknown condition as satisfied.
- Declared, inferred, observed, and required are independent facts. Tool
  installation, a successful help command, and a passing validation run
  establish different facts.
- Every validation obligation must be accounted for. Unknown commands,
  unavailable tools, unsupported configuration, and remote-only checks remain
  explicit records. No fabricated fallback command counts as validation.
- Preserve native wrappers such as `tox -e lint`, `make check`, and
  `pre-commit run --all-files`. Their child commands explain behavior; they
  do not automatically become extra independent executions.
- Preserve exact shell semantics. `shlex.split` is useful for simple POSIX
  argv, but is not a parser for pipelines, loops, PowerShell, or shell state.
- Separate document validity, discovery completeness, and check results.
  A valid Structrr document may describe incomplete discovery or a failing
  baseline. None of those states implies that code changes are ready for PR.

## Proposed production files and interfaces

Paths below are new unless marked existing. Keep modules small and reuse
existing equivalents if they have already been implemented.

| File | Responsibility |
| --- | --- |
| `src/powdrr_lift/structrr/validation_models.py` | Typed records, serialization, validation, stable identities, v1 reader |
| `src/powdrr_lift/structrr/validation_discovery.py` | Discovery orchestration, evidence index, dependency graph, completeness |
| `src/powdrr_lift/structrr/validation_adapters/` | Registered component, environment, runner, and validator adapters |
| `src/powdrr_lift/structrr/github_validation.py` | Workflow parsing, contexts, matrices, actions, reusable-workflow declarations |
| `src/powdrr_lift/structrr/bootstrap_report.py` | Deterministic Markdown rendering from Structrr records |
| `src/powdrr_lift/workrr/validation_execution.py` | Environment-aware executor shared by probes and validation |
| `src/powdrr_lift/workrr/validation_probes.py` | Tool-specific probe construction and evidence interpretation |
| `src/powdrr_lift/workrr/github_validation_evidence.py` | Injected, read-only GitHub observations and remote file retrieval |
| `src/powdrr_lift/structrr/validation.py` (existing) | Compatibility facade and explicit adapter registration |

Public discovery API:

```python
discover_repository_validation(root, *, tracked_paths, options) -> ValidationDiscovery
```

`ValidationDiscovery` contains components, environments, checks, evidence,
input fingerprints, diagnostics, and completeness. Static discovery never
calls subprocesses. The bootstrap orchestrator supplies tracked evidence and
merges optional executor results into these records.

Keep `discover_validation_profiles` callable during migration. Its adapter
must return only executable profiles; never discard unknown findings from
the richer inventory. Read old inventory records explicitly rather than
quietly assigning the new schema version to old data.

## Data contract

Increment `BOOTSTRAP_SECTION_VERSIONS["validation_inventory"]` to 2 and use
`verification-provider-inventory-v2` for new entries. Retain existing
top-level Structrr/changelog identity. Add a versioned `validation_context`
section for shared components, environments, evidence, diagnostics, inputs,
and discovery status. Increment `tools` only if its serialized contract
changes. Update readers, schema validation, exports, and fixtures together.

The inventory remains a list. A representative entry is:

```yaml
id: validation:api:unit:py312
schema_version: verification-provider-inventory-v2
profile: api-unit-py312
provider: pytest
component: component:api
purpose: Run the API unit tests.
roles: [test]
command: [poetry, run, pytest, tests/unit, "-m", "not integration"]
execution:
  kind: argv
  cwd: packages/api
  shell: null
  script: null
  environment: environment:api-test-py312
  depends_on: [setup:api-test-py312]
selectors: []
config_files: [packages/api/pyproject.toml]
settings:
  declared: {testpaths: [tests/unit]}
  effective: {}
settings_status: declared_only
applicability:
  local: true
  events: [pull_request]
  base_branches: [main]
  paths: []
  condition: null
  evaluation: unknown
ci_origins:
  - workflow: .github/workflows/tests.yml
    job: unit
    step: 4
    matrix: {python: "3.12"}
requiredness: {status: unknown, evidence: []}
provenance:
  declaration: declared
  evidence: [evidence:unit-command, evidence:unit-setup]
confirmation:
  level: static
  observations: []
local_reproducibility: unknown
baseline: {status: not_run, observation: null}
unresolved: []
```

Define these rules before writing adapters:

1. `provider` is explicit, not derived from the profile name. Use `custom`
   for opaque native checks and `aggregate` for known wrappers.
2. An executable argv record has a non-empty `command` and no shell script.
   A shell record has `execution.kind: shell`, the exact script and shell,
   and an empty compatibility `command`. An unresolved record uses
   `execution.kind: unresolved` and preserves the original declaration.
3. Cwd is repository-relative; reject traversal and symlink escapes. External
   environment paths are explicit execution-policy inputs, not trusted cwd.
4. Environment records describe interpreter constraints, observed executable,
   OS/architecture, manager, lockfiles, selected extras/groups, installation
   mode, variables, required secret names, services, and native prerequisites.
   Never persist secret values. Distinguish setup declarations from completed
   setup observations.
5. Evidence records include id, kind, path/URL, revision, content hash, YAML
   key or line span, short excerpt, assertion, and interpretation. Evidence
   can be local source, remote source, runtime observation, or human policy.
6. Confirmation levels are `static`, `availability`, `syntax`, `configuration`,
   `collection`, and `execution`. Keep individual probe outcomes; one level
   must not erase a failure or suggest every property has been confirmed.
7. Check outcomes are `not_run`, `passed`, `failed`, `blocked`, `timed_out`,
   and `not_applicable`. A collection with no selected tests is explicit;
   it is not evidence that tests passed.
8. Requiredness is `required`, `optional`, or `unknown`. Applicability is
   `applicable`, `not_applicable`, or `unknown`, with supporting inputs.
9. Deduplicate only equivalent execution, environment, scope, and conditions.
   Merge their evidence and origins. Different matrix jobs remain distinct.
   Derive stable ids from declaration location and variant identity; command
   edits update fingerprints without creating needless identity churn.
10. Discovery is `complete`, `partial`, or `unknown`, relative to explicitly
    recorded coverage: local declarations, GitHub declarations, GitHub rules,
    and runtime confirmation. Missing API access leaves GitHub rules unknown.
    Complete static discovery does not mean complete runtime confirmation.
11. Validate all references, unique ids, prerequisite cycles, enum values,
    execution shape, and source anchors. Malformed config produces an anchored
    diagnostic rather than silently becoming an empty configuration.

Use the existing source model for the Markdown structure overview. Generate
only source-supported package/module descriptions; unresolved purposes use a
plain location description. Do not add speculative architectural claims.

## Work package 1: Records and compatibility

Files: new `validation_models.py`; existing `validation.py`, `bootstrap.py`,
`structrr/__init__.py`, inventory consumers and schema definitions found via
`rg`. Add `tests/test_validation_models.py`.

Steps:

1. Implement the typed records and serializers above with deterministic
   ordering. Keep timestamps and duration in observations, outside semantic
   fingerprints. Keep lists ordered when order affects execution.
2. Implement strict v2 validation and a v1 reader. V1 commands are legacy
   candidates at repository cwd with unknown environment and confirmation.
3. Add `validation_context` and section version checks. Preserve existing
   Structrr entities and source bindings.
4. Give compatibility profiles explicit provider/context fields while keeping
   existing positional construction working during migration.
5. Update inventory consumers to address check ids, not global tool names.

Acceptance: v2 round-trip; v1 reads without overstated confidence; shell and
unresolved records survive serialization; duplicate ids and dangling evidence
fail validation; two pytest variants survive. Every adapter evidence path
resolves to exactly one context evidence ID after normalization, including
tox configuration and workflow inputs. Existing polyglot tests pass.

## Work package 2: Evidence and Python project topology

Files: new discovery orchestrator and component/environment adapters;
existing `_tracked_files`, source manifest integration, bootstrap tests.

Steps:

1. Build a validation evidence enumeration from Git-tracked paths before
   source-model exclusions. Include `.github/workflows`, local action files,
   hooks, manifests, requirements, lockfiles, task definitions, contributor
   instructions, and referenced scripts. Retain ordinary source exclusions.
2. Keep `.github` excluded from source entities if existing policy requires
   it, while recording and fingerprinting it as validation evidence. Update
   `test_bootstrap_ignores_tracked_github_metadata` to assert both behaviors.
3. Discover root and nested Python components from manifests, workspace
   membership, and CI working directories. Avoid interpreting every test
   fixture pyproject as a production package: record fixtures when referenced
   by validation and explain the classification evidence.
4. Parse `pyproject.toml`, `setup.cfg`, requirements and constraints files,
   Pipfile, environment YAML, lockfile metadata, `.python-version`, and manager
   configuration. Read `setup.py` with AST; do not execute it for discovery.
5. Follow requirement includes and dependency-group includes with cycle
   diagnostics. Capture selected groups/extras from installation declarations;
   never assume every available group is installed or required.
6. Detect uv, pip/venv, Poetry, PDM, Pipenv, Hatch, Conda/Mamba, and container
   environments using declarations. A build backend or pyproject alone does
   not select an invocation manager. Preserve mixed environments.

Acceptance: pip project with pyproject gets no invented uv prefix; Poetry,
uv workspace, requirements-only, nested monorepo, namespace, legacy setup,
Conda, native-extension, and shared-environment fixtures have correct scope.
Changing a workflow or included requirements file changes the validation
fingerprint. Generated bootstrap artifacts never become their own evidence.
Bootstrapping cachetools (tox config plus GitHub Actions evidence) produces a
valid snapshot whose tox provenance references IDs present in
`validation_context.evidence`.

## Work package 3: Task runners, hooks, and scripts

Files: adapter modules for tox, Nox, task definitions, hooks, and scripts;
`tests/test_validation_orchestration.py`.

Steps:

1. Implement declarative tox support for tox 3/4 forms: INI, standalone TOML,
   native pyproject tables, and embedded legacy INI. Preserve environment
   selection, factors, inheritance, posargs, chdir, dependencies, installation,
   pass/set environment, commands before/after, and interpreter constraints.
2. Parse Nox source with AST for sessions, parameters, Python variants,
   install/run calls, and configured defaults. Dynamic Python expressions
   stay unresolved until enumeration or execution supplies evidence.
3. Parse Make dependencies/includes, Just/Task definitions, Hatch environment
   scripts, PDM scripts, Pipenv scripts, and Poe tasks. Trace literal Invoke
   and doit declarations; dynamic task construction stays unresolved.
4. Parse pre-commit repositories/revisions, hooks, stages, arguments, file
   filters, language environments, local entries, and `pass_filenames`.
   Preserve hook selection and pinned environments rather than substituting
   a globally installed tool.
5. Follow referenced shell/Python scripts and aggregate tasks with depth and
   cycle limits. Preserve ordered script blocks as executable units. Track
   sourced files and literal subprocess calls; opaque control flow needs a
   diagnostic and retained native entry point.
6. Separate validation from setup, artifact upload, publication, and cleanup.
   Names provide hints, not proof. Classify ambiguous CI steps explicitly.
   Record generated-file checks and whether their commands modify inputs.
7. Add bounded agent-analysis proposals through an injectable interface. Feed
   only relevant source fragments and unresolved questions; validate paths,
   spans, referenced declarations, and record shape before accepting findings.
   Test with a fake analyzer; do not require a live LLM to run bootstrap.

Acceptance: aggregate wrappers run once; hidden lint/test dependencies remain
visible; quoted markers remain one argument; loops/pipelines remain scripts;
dynamic Nox/Invoke tasks stay unresolved; hook formatter mutation is recorded;
analysis proposals without source evidence are rejected.

## Work package 4: GitHub declarations and actual PR obligations

Files: `github_validation.py`, remote evidence adapter, workflow fixture tests.

Steps:

1. Use a dedicated YAML loader whose boolean resolution preserves GitHub's
   `on` key. Retain source locations; do not globally change PyYAML loaders.
2. Parse all workflow files. Capture triggers, branch/path filters, activity
   types, job/step conditions, `needs`, `continue-on-error`, defaults, inherited
   env, shell, cwd, containers, services, setup actions, and installation steps.
3. Expand finite matrices, including include/exclude entries. Record Python,
   OS, dependency and package variants. Bound expansion; dynamic or oversized
   matrices remain symbolic with an explicit unresolved reason.
4. Implement a documented expression subset for literals, known contexts,
   boolean/comparison operations, and common functions. Unknown values use
   three-valued evaluation, not Python `eval` and not guessed substitutions.
   Account for GitHub condition defaults and job dependencies. Mark every
   unsupported expression and retain it unchanged.
5. Follow local composite actions and reusable workflows, including caller
   inputs/defaults and environment differences. Retrieve remote references
   only through the injected read-only adapter, resolving refs to commit SHAs.
   Bound recursion and keep inaccessible/private references unresolved.
6. Model cross-step state such as literal `GITHUB_ENV`, `GITHUB_PATH`, and
   outputs. Dynamic state remains a dependency on the original job. Never
   flatten an unresolved job into supposedly independent local commands.
7. In connected mode read target-branch protection, applicable rulesets,
   required workflows/checks, check runs, commit statuses, job logs, and
   external check providers. Preserve base/head/merge commit identities and
   event context. Sanitize and bound logs before storage.
8. Match declarations to observations using workflow/job/matrix identity and
   revision, not only display names. Observed checks do not imply requiredness.
   Required checks without an accessible implementation become remote-only
   obligations. Record unavailable permissions and stale observations.

Acceptance: multiline custom checks, defaults/overrides, reusable inputs,
static matrices, dynamic matrices, path filters, forks/drafts, PR versus push,
merge groups, optional failures, external required checks, API denial, and
remote-reference cycles are covered. No unresolved expression becomes runnable
argv. CLI bootstrap still works offline without credentials.

## Work package 5: Validator adapters and effective settings

Files: validator adapters and `tests/test_validation_tool_settings.py`.

For each adapter implement declaration detection, version-sensitive config
selection, command recognition, scope/settings extraction, probe recipes,
and unresolved diagnostics. Do not generate a required check solely because
its package or config is present; emit an inferred candidate until stronger
evidence establishes the invocation.

| Family | Required initial coverage | Settings to preserve |
| --- | --- | --- |
| Tests | pytest, unittest, Django runner, doctest, legacy nose/nose2 declarations, custom harnesses | Targets, patterns, markers, plugin flags, addopts/env, warnings, async mode, parallel/sharded execution, settings modules |
| Format/lint | Ruff, Black, isort, Flake8/plugins, Pylint, pycodestyle | Check/fix behavior, config precedence, extends, include/exclude, ignores, rule sets, Python target, scope |
| Types | mypy, Pyright, basedpyright, ty, pytype | Files/modules, exclusions, plugins, stubs, import paths, execution environments, interpreter and platform |
| Coverage | coverage.py, pytest-cov | rcfile/env overrides, source/omit, branch mode, thresholds, subprocess configuration, combine/report dependencies |
| Packaging | Python build, uv build, backend/native checks, twine check, check-manifest, cibuildwheel | Wheel/sdist creation, build isolation, system dependencies, installed-wheel tests, platform matrix |
| Additional checks | Sphinx, MkDocs, Bandit, pip-audit, lock consistency, schema validation, migrations, notebooks, spelling, generated files | Native command, config, prerequisites, applicability, artifacts and mutation |

Explicitly support pytest INI/CFG/TOML formats and pytest 9's native TOML
forms; distinguish older versions. Handle Ruff's nearest configuration and
explicit `extend`, mypy's per-module settings, and Pyright/basedpyright
execution environments. Unknown version means version-dependent interpretation
is unresolved. Never treat pytest rootdir as an import-path configuration.

Additional and unfamiliar tools can remain `custom` providers with verified
native commands. Adapter coverage is not permission to invent a CI check.

Acceptance: Ruff lint alone does not imply Ruff format; Black plus Ruff lint
is represented correctly; mypy configured targets do not become `src tests`;
multiple config files follow actual precedence; command/env overrides survive;
coverage report thresholds and shard combination remain obligations.

## Work package 6: Controlled probes and baseline evidence

Files: shared execution and probe modules; runner/provider integration tests.

CLI policy, also available as typed API options:

- The `bootstrap` task workflow runs baseline checks by default. It attempts
  each applicable discovered local validation command in its recorded context
  and records the outcome. `--validation-timeout-seconds` bounds each command;
  the aggregate budget is 1800 seconds. A future explicit opt-out must leave
  every result visibly `not_run` and must never claim validation was confirmed.
  The lower-level `bootstrap-structrr` command remains a snapshot generator and
  reports `baseline_status: not_run` rather than implying it ran project checks.
  Inspect and collection probes remain available for targeted diagnostics as
  their execution support is implemented.
- `--github offline|connected`, default `offline`.
- `--report-output PATH`, optional; default YAML path with `.md` suffix.
- Bound probe timeout, aggregate time, output size, and graph expansion in a
  policy object. Initial defaults: 30 seconds per inspect probe, 120 seconds
  per collection, 600 seconds per baseline check, 1800 seconds aggregate,
  64 KiB output per stream, 256 matrix variants, recursion depth 16. Record
  truncation and exceeded limits; expose API overrides and CLI time budget.

Steps:

1. Execute argv directly; execute scripts with their declared shell and
   failure semantics. Maintain ordered prerequisite state within an execution
   group. Use injected environment bindings and explicit per-check cwd.
2. Resolve executable/interpreter and package versions inside the selected
   environment. Preserve shared-environment instructions. Do not remove
   `VIRTUAL_ENV` unconditionally or create/sync environments during inspection.
3. Implement availability/version/help probes; configuration/file-scope
   probes; tox environment/config inspection; Nox JSON session listing;
   pytest help/plugin/config/collection probes; coverage configuration debug.
   Detect installed CLI support first, especially for tox 3 versus tox 4.
4. Route collection through a target-environment subprocess. Reuse the pytest
   collector hook in a subprocess helper with a structured result file.
   Do not strip a wrapper and import Powdrr's pytest to simulate its result.
   Preserve plugin-provided options and import behavior. If a wrapper cannot
   support collection without changing semantics, record that limitation.
5. Classify each probe's actual effects. Nox imports, pytest collection,
   pre-commit hooks, Make expansion/dry runs, and environment-manager wrappers
   may execute code or modify files. Inspect them in execution space with
   appropriate isolation. Read-only filesystem and absent host secrets are
   enforced by the executor, not merely claimed by a disposable checkout.
6. Keep original CI commands and adapted local probes as separate records.
   For example, disabling uv synchronization can confirm syntax but does not
   prove the original CI environment has been reproduced.
7. Compare source state before/after probes, bound processes, terminate process
   groups on timeout, and preserve logs/results. Do not run publish/deploy
   steps as discovery. Prepare declared dependencies only inside the isolated
   task worktree; uv commands use that worktree's `.venv`, never the caller's
   environment. Remove caller `VIRTUAL_ENV`, `PYTHONPATH`, uv environment
   overrides, and secret-bearing variables before execution. Report missing
   tools, dependencies, services, or credentials as `blocked`. A command is
   confirmed only when its native validation invocation completes successfully
   in the recorded context.
8. On syntax/path errors, inspect output and make bounded, evidence-supported
   corrections. Keep original declarations and correction history. Missing
   services/secrets/dependencies are blocked prerequisites; do not remove
   flags, markers, plugins, or failing tests just to obtain exit code zero.
9. Run native checks before edits in baseline mode. Record project failures
   separately from invocation failures. A valid command with failing tests
   remains an executable discovered check.

Acceptance: help success never becomes suite success; collection uses the
target pytest version; caller cwd/env never change; missing plugin and missing
database are distinguished; source mutation is detected; timeouts kill child
processes; secrets stay out of artifacts; failed baselines remain visible.
Default bootstrap executes every applicable discovered local validation
command and records `passed`, `failed`, `blocked`, or `timed_out`; it leaves no
applicable executable command at `not_run`. If no checks were discovered, the
task is marked unverified and cannot open a bootstrap PR. Any future explicit
skip mode must also be marked unverified.

## Work package 7: Structrr and Markdown output

Files: bootstrap orchestrator/result, report renderer, CLI, bootstrap skill,
`tests/test_bootstrap_report.py`, `tests/test_cli.py`, bootstrap fixtures.

Steps:

1. Call discovery once. Populate the inventory, context, tool references, and
   report from the same normalized records. Remove `_validation_tool_source`
   as generic provenance; every tool references its own evidence.
2. Extend `BootstrapResult` with `report_path` and discovery status. Write YAML
   and Markdown only after document validation. Stage writes to temporary
   files and publish an artifact generation with a shared semantic digest;
   the manifest is written last as the completion marker. Detect partial or
   mismatched generations on load and regenerate them.
3. Exclude YAML, Markdown, manifests, and evidence outputs from discovery,
   including custom paths and evidence located outside the repository.
4. Add CLI options above and JSON fields for report path, check counts,
   completeness, unresolved count, and probe mode. Preserve existing JSON
   keys. Output no progress/log text to stdout when `--json` is selected.
5. Keep CLI exit code 1 for invalid documents or failed artifact writes, and
   return a distinct nonzero result when default baseline execution has failed
   or blocked checks. Still persist the valid discovery snapshot and its
   results. Exit 0 means artifacts are valid and all applicable local checks
   passed; an empty inventory or explicit probe skip must be labeled
   unverified. PR readiness is
   enforced downstream, not inferred from bootstrap exit code. Explain this
   behavior in CLI help and documentation.
6. Update the skill to inspect freshness before reuse, request supported
   probes, retain failed/unrunnable checks with reasons, and emit both outputs.
   It must not claim help confirmed validation or require a passing baseline
   before acknowledging a project's real command.

Required Markdown layout:

```markdown
# Project bootstrap findings

Source revision: <revision>. Discovery: <status and coverage>.

## Code structure
<Short source-supported overview of packages, applications, tests, scripts.>

## Validation commands

### <Purpose> (`<stable check id>`)
Command: <argv displayed with quoting, or fenced native shell block>
Working directory: <relative path>
Environment and prerequisites: <interpreter, runner, groups, services>
Applies when: <local/CI context, matrix variant, conditions>
Requiredness: <status and basis>

Evidence:
- <linked path and lines/key, declaration excerpt, what it establishes>
- <settings/config evidence, overrides and scope>
- <probe evidence link, what was actually confirmed>

Confirmation: <level, baseline result if present>
Unresolved requirements: <specific reason or none identified>
```

Include every check, including opaque and remote-only obligations. Setup-only
commands belong in prerequisites. Include unresolved potential validation
steps at the end of the validation list with their classification questions.
Keep the overview short; do not render a symbol dump, raw logs, or an essay
about discovery internals. Escape Markdown and quote commands correctly.
Link relative evidence against the report location, not always repo root.

Acceptance: every Markdown check id matches Structrr exactly; provenance is
specific; failed and blocked checks appear; custom output locations link
correctly; malformed YAML writes neither new deliverable; deterministic
static inputs produce identical semantic output; JSON stdout is parseable.

## Work package 8: Freshness and downstream enforcement

Files: source manifest integration, `_ensure_current_baseline`,
`_bootstrap_validation_profiles`, `coding_agent_validation.py`,
`verification_provider.py`, `command_catalog.py`, bootstrap/PR-prep skills.

Steps:

1. Fingerprint every relevant declaration, referenced script, configuration,
   lockfile, component boundary, runner policy, adapter/schema version, and
   remote reference resolution. Include input file additions/deletions so a
   new workflow invalidates a previously empty inventory.
2. Runtime observation identity includes source content, interpreter/tool
   version, environment/dependencies, execution context, and probe recipe.
   Candidate edits invalidate old pass evidence. Offline runs preserve remote
   observations as dated/stale evidence rather than pretending to refresh them.
3. Keep validation freshness alongside the source manifest without redefining
   existing product snapshot exclusions. Baseline reuse requires current
   sections, current validation inputs, and a matching Markdown digest.
   The branch that copies a supplied current document must also create its
   companion report and freshness metadata.
4. Replace profile reconstruction from `tools` with a typed inventory loader.
   Merge explicit feature checks with repository checks. Remove the `true`
   fallback and emit unknown-validation state when no obligation is known.
5. Adapt every profile handoff, including command catalog responses, allowed
   command policy, requests, verification providers, and final reports, to
   preserve cwd/environment/script/conditions. Avoid joining argv into a
   string and splitting it again.
6. Evaluate applicability against the actual change/base/event. Unknown
   applicability is pending, not skipped. Unsupported script execution or
   unavailable remote-only checks are explicit unresolved obligations.
7. Run focused checks during editing and the full applicable validation set
   for PR readiness. Keep native aggregates grouped; do not execute every
   child twice. Never call a partial local matrix equivalent to full CI.
8. Require successful applicable checks or explicit existing waiver policy
   before claiming validated readiness. Pre-existing baseline failures remain
   visible and need disposition; they are not automatic exemptions. Schema
   validity and bootstrap completeness alone never satisfy this gate.

Acceptance: explicit test plus discovered checks both execute; env/cwd/shell
survive all handoffs; no validator produces unknown readiness; changing a
workflow, included script, lockfile, tool version, or package boundary
invalidates the correct evidence; existing non-Python profiles still execute.

## Work package 9: Breadth evaluation and documentation

Files: `tests/fixtures/validation_discovery/`, adapter conformance tests,
integration tests, user documentation, optional corpus evaluation script.

1. Add small fixture repositories with manually reviewed expectations for
   every acceptance case above. Each expectation names commands, scope,
   prerequisites, source anchors, applicability, and unresolved facts.
2. Test adapters through a shared conformance suite. Use fake executors and
   fake GitHub responses for deterministic unit coverage. Add subprocess
   integration cases for environment isolation, target collection, scripts,
   shell failures, and timeouts. Never require credentials/live services in
   the ordinary suite.
3. Create a pinned real-repository corpus covering pytest/unittest/Django,
   pip/Poetry/uv/PDM/Hatch/Conda, tox/Nox/hooks, monorepos, native extensions,
   custom scripts, and reusable Actions. Record licenses and revisions.
   Manually inspect its CI to establish expected obligations. Start with
   this repository; select the remaining corpus as part of this package and
   commit the reviewed expected inventory before using it as an oracle.
4. Report missed validation obligations, invented required checks, exact
   command/context mismatches, configuration mistakes, evidence quality,
   and unresolved prerequisites. Count legitimate unresolved records
   separately from successful executable discovery; do not hide them in
   a single success percentage.
5. Document output examples, supported versions, unknown states, modes,
   execution-policy requirements, custom check extension, and migration.

Final completion gate:

- Both deliverables exist and agree for all valid fixture repositories.
- No reviewed required obligation is missing or falsely confirmed.
- All supported executable fixtures reproduce native invocation context.
- Unsupported/dynamic cases have anchored, actionable unresolved records.
- This repository accounts for Ruff format/lint, mypy, pytest/coverage,
  workflow-definition validation, deterministic workflow scenarios, and
  conditional workflow tuning. RTK/uv installation is setup; artifact uploads
  are reporting. Requiredness remains unknown without repository-rule evidence.
- Full repository verification passes, or independently established baseline
  failures are documented for user review without weakening the checks.

## Implementation verification and PR procedure

Work in a dedicated feature worktree. Prefix shell commands with `rtk`.
Use the existing shared Python environment; do not create a local environment
or run `uv sync` for this repository's checks:

```bash
export VIRTUAL_ENV=/Users/gregory/code/powdrr-lift/.venv
export PATH="$VIRTUAL_ENV/bin:$PATH"
export PYTHONPATH="$PWD/src"
export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"
export UV_NO_SYNC=1

rtk proxy "$VIRTUAL_ENV/bin/python" -m pytest -n auto --cov=powdrr_lift --cov-branch --cov-report=term-missing --cov-report=xml tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff format --check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m ruff check .
rtk proxy "$VIRTUAL_ENV/bin/python" -m mypy src tests
rtk proxy "$VIRTUAL_ENV/bin/python" -m powdrr_lift.cli validate-workflow-definitions skill-definitions templates --liveness --baseline .github/workflow-liveness-baseline.json --warning-budget .github/workflow-liveness-warning-budget.json --warning-report /tmp/bootstrap-workflow-liveness.json
rtk proxy ./scripts/run-deterministic-workflow-suite.sh
```

Run focused package tests while implementing; run the full commands before
pushing. If skill definitions or templates change, also run the conditional
workflow-tuning checks declared in `.github/workflows/ci.yml` against the PR
base, preserving replay/scenario selection. Scope any additional checks to
actual declarations and changes. Inspect `git status` after checks and keep
generated reports out of commits unless they are intended fixtures.

Open a PR for every change set; do not push to main or merge the PR. Describe
implemented packages, fixture coverage, validation results, unresolved limits,
and migration changes. Do not mark this overall plan complete after merely
introducing the records or producing the Markdown renderer.

## Primary research references

Use target tool versions when implementing; current documentation is not
proof that an older project supports a configuration form or CLI option.

- [Python project metadata](https://packaging.python.org/en/latest/specifications/pyproject-toml/)
  and [dependency groups](https://packaging.python.org/en/latest/specifications/dependency-groups/).
- [pytest configuration](https://docs.pytest.org/en/stable/reference/customize.html),
  [unittest](https://docs.python.org/3/library/unittest.html), and
  [Django tests](https://docs.djangoproject.com/en/5.2/topics/testing/overview/).
- [Ruff configuration and introspection](https://docs.astral.sh/ruff/configuration/),
  [mypy settings](https://mypy.readthedocs.io/en/stable/config_file.html),
  [Pyright configuration](https://github.com/microsoft/pyright/blob/main/docs/configuration.md),
  and [coverage configuration](https://coverage.readthedocs.io/en/latest/config.html).
- [tox CLI](https://tox.wiki/en/latest/man/tox.1.html),
  [Nox enumeration](https://nox.thea.codes/en/stable/usage.html),
  [pre-commit](https://pre-commit.com/), and
  [Make execution semantics](https://www.gnu.org/software/make/manual/make.html).
- [uv workspaces](https://docs.astral.sh/uv/concepts/projects/workspaces/),
  [uv synchronization](https://docs.astral.sh/uv/concepts/projects/sync/),
  [Poetry environments](https://python-poetry.org/docs/managing-environments/),
  [PDM tasks](https://pdm-project.org/latest/usage/scripts/),
  [Pipenv scripts](https://pipenv.pypa.io/en/latest/scripts.html), and
  [Hatch environments](https://hatch.pypa.io/latest/environment/).
- [GitHub workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax),
  [reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
  [protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches),
  [check runs](https://docs.github.com/en/rest/checks/runs), and
  [repository rules](https://docs.github.com/en/rest/repos/rules).
