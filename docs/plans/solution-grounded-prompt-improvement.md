# Solution-grounded instruction-to-prompt improvement plan

Status: proposed implementation plan. This document defines work to implement;
it does not report a completed experiment or authorize a production rollout.

## Objective

Improve the instruction-to-prompt pipeline by working backward from demonstrated
task behavior to a reference worker prompt, then backward again to the source
interpretation needed to produce that prompt. Run the production pipeline forward
on the original inputs and locate the differences that matter to implementation.

The reference solution provides evidence about what a successful implementation
does. Our target is a prompt that reliably produces those required behaviors,
including valid alternative implementations. Reproducing the reference patch's
private architecture or exact bytes is not the objective. A reference prompt is
an experimentally tested candidate, not an assertion that we have found a unique
ideal prompt.

The central artifact is a connected mapping:

```text
solution behavior and verifier evidence
                    |
         justified task requirements
                    |
       target packet and worker prompt
                    |
      required instruction interpretation
                    |
original instruction + base repository + bootstrap
```

Every interpretation decision must have a reason in the target behavior or prompt.
Classifier accuracy and prose similarity are supporting diagnostics. Success is
closing meaningful gaps between achievable reference prompts and production
prompts, with corresponding improvement in worker outcomes and measured cost.

## Existing foundation and boundaries

Reuse these components rather than introduce a parallel compiler:

- `src/powdrr_lift/core/implementation_packet.py`: `ImplementationPacket` and its
  renderer, including required tests, behavior scenarios, and scoped contracts.
- `src/powdrr_lift/workrr/coding_agent.py`: `ImplementationRequest`,
  `from_execution_unit`, provider prompt customization, and exact prompt storage.
- `src/powdrr_lift/workrr/feature_endpoint.py`: production orchestration and
  worker-prompt capture mode.
- `src/powdrr_lift/workrr/deepswe_design_evaluation.py`: task identity, artifact
  integrity, source anchors, and evidence-based behavior judging.
- `docs/evaluations/deepswe-python-statemachine-state-data.yaml`: an existing
  task-grounded rubric, usable as the first worked example.
- `docs/harbor/python-instruction-prompt-audit-2026-09-29.md`: six additional
  families exposing ambiguity, decomposition, metadata, and extraction failures.

The existing rubric covers one task; extending corpus coverage is new work. The
six audited runs stopped before final design completion and are failure examples,
not completed reference-prompt evaluations. Preserve that distinction in reports.

Use two isolated jobs. The backward job may inspect solutions and hidden tests.
The forward compiler and coding worker receive only the original instruction,
base repository, bootstrap, and context allowed in normal operation. They cannot
read reference artifacts, patch paths, hidden tests, or backward-job transcripts.
Evaluator artifacts live outside their worktrees and tool access. Freeze and hash
forward outputs before comparison. A forward repair is a new experiment revision,
not an edit to a previously captured result.

## 1. Select and freeze a small pilot

Start with eight tasks: state-data scoping; the six families in the September 29
audit; and one existing simple task from the local corpus that compiles cleanly.
Check availability and usable reference/verifier execution before final selection.
If a task is unavailable, record its exclusion and choose a replacement with the
same language feature. Do not silently select only tasks the compiler can finish.

For each task, retain instruction bytes, repository URL and base commit, task
metadata, bootstrap inputs/output, reference patch and verifier hashes, environment
manifest, compiler revision, and worker/provider settings. Validate the benchmark
harness against the base repository and reference solution in evaluator sandboxes.
Record expected baseline failures; do not assume the unchanged base must fail
every test. Quarantine broken harnesses and distinguish them from product failures.

Reserve two task families as held-out evaluation before tuning begins. The other
six support reference construction and development. Keep paraphrases and related
tasks from the same repository/feature family in one split. Pilot results will be
descriptive; eight tasks cannot establish broad model quality or rare-error rates.

Deliverable: immutable task manifest and an eligibility report. The pilot is ready
when each included task has reproducible inputs and an evaluator that can assess
the reference solution. Compiler failures remain eligible for forward comparison.

## 2. Recover the behavior the task actually requires

An automated analyst reads the original instruction, base repository, solution
diff, and verifier. It creates one evidence record per meaningful behavior:

- observable action, condition, population, result, and preservation requirement;
- exact instruction spans, including contextual sentences and cross-references;
- base-repository evidence or an explicitly permitted external authority;
- solution locations demonstrating the behavior and verifier cases exercising it;
- boundary or contrast cases distinguishing plausible misinterpretations;
- importance, uncertainty, and whether the behavior is currently tested.

Assess solution and verifier evidence independently. A solution can omit a stated
requirement, and a test can demand unstated behavior. Inspect untouched instruction
requirements as well as changed code so the reference patch does not define the
entire requirement set by accident. One behavior may need several source spans;
several behaviors may derive from the same sentence.

Assign each record an evidence disposition:

| Disposition | Treatment |
| --- | --- |
| Explicit instruction requirement | Include in the reference contract with exact spans. |
| Inference justified by instruction and base context | Include with a written derivation and supporting context. |
| Ambiguous source with several valid readings | Retain alternatives and mark unresolved; do not silently choose the solution's reading. |
| Detail available only in the solution/verifier | Retain for benchmark diagnosis; exclude from recoverable prompt obligations. |
| Implementation choice | Retain as explanatory evidence, without requiring that architecture. |

A second automated reviewer challenges every proposed inference and searches for
lost exceptions, scope changes, contradictions, and private implementation details.
Require quoted evidence for conclusions. Exact spans and file anchors are checked
deterministically. Agreement is provisional evidence, not human-certified truth.
Review disagreements, consequential ambiguity, and a random sample with a human.

Deliverable: behavior evidence set and uncertainty register. No accepted prompt
obligation may rely solely on hidden evidence. Report the fraction of benchmark
expectations that cannot be recovered from normal generator inputs.

## 3. Produce reference prompts within the real target format

First inventory the current packet/request fields, validation rules, rendered
sections, context references, and provider wrappers. Pin their revisions. Specify
a prompt-size budget based on observed production prompts and the worker context
limit; apply the same budget to all experimental arms and log truncation failures.

Construct reference `ImplementationPacket` and execution-unit inputs from the
accepted evidence. Render through `ImplementationRequest.from_execution_unit`
and the same provider customization used in production. Store both the request
and exact provider-ready prompt. Do not hand-author arbitrary extra sections or
append reference tests to compensate for a missing field.

Allowed paths, planned changes, commands, and repository context must be derivable
from the base repository and normal bootstrap capabilities. In particular, do not
copy the reference patch's changed-file list into scope unless base evidence
supports that choice. Preserve instruction-mandated implementation constraints;
avoid inferring internal classes, algorithms, or helper names merely from a patch.
Behavior examples should be original expressions of supported requirements,
without hidden fixture values, test names, or reference code fragments.

If a required distinction cannot survive the current schema or renderer, record
a format gap with the requirement, attempted encoding, and resulting loss. Such a
task has a partially representable reference until a separately reviewed format
change exists. Track this separately from instruction interpretation failures.

Allow at most two reference candidates initially: a complete contract and a more
compact version preserving the same behavior. Review them against the evidence
set and validate schema/rendering integrity before spending on coding runs.

Deliverable: source-supported reference packets, rendered prompts, and format-gap
report. “Reference” means supported and representable; mark worker validation as
pending until the next step succeeds.

## 4. Test whether the reference prompt helps produce the solution behavior

Run three arms in fresh, identically prepared worker environments:

1. Original instruction inside the normal worker wrapper, as a simple baseline.
2. Production-generated prompt/request from the original instruction/bootstrap.
3. Reference-generated prompt/request constrained to the production format.

All arms use the same worker model/version, tool permissions, base repository,
environment, context budget, execution budget, and repair policy. Log differences
in request scope and context as potential confounders. Run a prompt-content-only
comparison under a common envelope when a difference could explain the result.
Randomize execution order and run three attempts per available arm per task for
the pilot. Record seeds where supported and repeat identity everywhere. The
initial ceiling is 72 attempts; a compiler stop is a recorded failure to produce
a prompt, without spending on a nonexistent worker run.

Evaluate frozen patches with benchmark verification and independent assessment
of supported instruction requirements the verifier misses. Keep verifier scoring,
behavior assessment, and environment failures distinct. Do not use patch similarity
as correctness. Preserve all attempts and repair histories, including failures.

Use failures to improve a reference candidate only on development tasks, with at
most two revision rounds. Every addition must pass the source-support check again.
Verifier-only demands remain separately reported, even when they affect benchmark
success. Hold-out failures are results; they cannot trigger tuning in that split.

If reference prompts consistently fail, inspect representation, worker ability,
environment, and evaluation before assigning blame to instruction interpretation.
If the original instruction performs as well as the reference, investigate whether
the compilation machinery contributes value. Worker outcomes qualify our reference
target; semantic comparison alone cannot establish that it is better.

Deliverable: paired outcomes and a reference-prompt status of validated, promising
but inconclusive, failed, or unrepresentable. Report counts, variation, and paired
task differences. Expand the corpus/attempt count before claiming general gains.

## 5. Work backward from the prompt to instruction interpretation

For every accepted reference obligation, specify the minimum interpretation needed
to produce its prompt content. Include source spans; atomic propositions; subject
and behavior bindings; disposition; condition; scope; polarity; quantifier;
ordering; identity; precedence; lifecycle; exceptions; and links between clauses
where relevant. Use only dimensions that explain a concrete requirement.

Validate the proposed interpretation against the compiler's current schemas. Mark
missing dimensions and unrepresentable relations explicitly. Record both what the
language means and how current compiler fields would encode it; do not mistake a
schema limitation for ambiguity in the English.

Examples of distinctions to recover from the pilot include:

- “On entry ... on exit ... re-entering ...”: related lifecycle obligations with
  separate conditions, preserving the timing of cleanup and callbacks.
- “Returns the same instance”: identity, which an equality-only oracle misses.
- “Self's errors before the other's”: ordered accumulation, with two populations.
- “Ignores interior empty records but rejects final ones”: one predicate with
  positional conditions and different outcomes.
- “Data is not persisted; add persistence”: an existing product gap followed by
  requested behavior, rather than a non-goal inferred from the word “not.”
- An absent temporal modifier: no stated timing constraint, rather than an
  unresolved requirement that prevents prompt generation.

Avoid gold labels for irrelevant local decisions. An obligation should explain
which source distinction is necessary and where its loss would change the prompt.
The backward reconstruction can reveal that the current split/classifier design
needs fewer decisions, a different representation, or more context across clauses.

Deliverable: instruction-to-reference mapping. Prefer a graph over a one-to-one
table: source spans, interpretations, packet fields, and prompt ranges have
many-to-many relationships.

## 6. Run forward and identify the earliest consequential divergence

Capture the production pipeline from original inputs, including ledger,
decomposition, classifications, semantic contracts, repository bindings,
bootstrap output, execution units, packets, requests, final prompts, and failures.
Record node identities and upstream inputs so each finding can be replayed without
reconstructing a full transcript. Missing prompts are explicit outcomes, not zero
coverage hidden inside an aggregate prompt score.

Match by meaning and source lineage, allowing equivalent wording and alternative
valid decompositions. Use deterministic checks for spans, schemas, and identity;
automated reviewers for behavioral equivalence and scope. Assign differences as
missing, weakened, broadened, contradicted, invented, unresolved, or equivalent.
Record exact reference and candidate evidence and permit reviewer abstention.

Follow a difference backward until the required distinction first disappears.
Inspect bootstrap when it omits discoverable API/context or narrows planned scope.
Inspect instruction interpretation when the source meaning is lost. Inspect
rendering when the contract is intact but absent from the worker prompt. Preserve
secondary contributing causes; some losses arise from several interacting stages.

On development tasks, replay with the smallest supported correction to that stage.
Test whether the correction reaches the packet/prompt and then changes worker
behavior. These counterfactual repairs are diagnostic experiments with explicit
oracle access; they are never reported as clean production results. A production
fix must subsequently succeed from original inputs without oracle corrections.

Deliverable: prioritized findings linking English distinction -> pipeline stage
-> prompt difference -> observed or hypothesized worker failure. Distinguish
observed causality, correlated evidence, and untested hypotheses.

## 7. Turn findings into focused improvements

Rank findings by supported behaviors affected, recurrence across task families,
strength of worker evidence, and runtime/cost. Fix one coherent failure class per
PR. Candidate interventions include deterministic defaults, contextual splitting,
schema changes, classifier examples, bounded response correction, repository
binding changes, and rendering changes. Select the intervention from evidence.

Add source-grounded fixtures around the discovered English distinction, including
contrasts where the proposed rule must not apply. Paraphrases can enlarge training
and development data but stay with their source family. They do not increase the
independent held-out task count. Preserve excluded/ambiguous records separately.

Validate corrections at three levels: faithful interpretation; preservation in the
production-format prompt; and worker outcomes on fresh runs. Use selective
expensive reruns for affected development cases, then all held-out tasks for a
release candidate. A faster path must preserve required behavior and avoid new
unsupported obligations. An accuracy improvement must report its latency/cost.

Do not start classifier training until recurring findings yield a stable target,
reviewed examples, and independent held-out data. Reference-prompt construction
can generate candidate labels automatically; it does not certify those labels.

## Automation and human workload

Implement resumable stages: manifest -> behavior extraction -> evidence review
-> reference construction -> reference validation -> backward mapping -> forward
capture -> alignment -> diagnosis -> report. Cache immutable inputs and successful
artifacts by input, schema, renderer, evaluator, and model revisions. A change to
one stage invalidates its dependents. Keep retries bounded and failed responses.

Automated reviewers perform distinct jobs: behavior extraction, support challenge,
and reference/production comparison. Avoid presenting agreement between models
as independent ground truth. Pin reviewer prompts and revisions, and monitor
mistakes found by human sampling.

Humans review consequential source ambiguity, proposed new meaning/format, reviewer
disagreements, and a stratified random sample of agreed records. Begin the pilot
with a budget of two 60-minute review sessions per week and a 10% random sample.
If the budget cannot resolve a case, keep it provisional and outside trusted
promotion criteria. Review requests show the source, relevant base context,
behavior evidence, target prompt excerpt, competing interpretations, and the
specific decision needed. Capture adjudications for later replay.

Use a machine-readable experiment budget for tasks, provider calls, worker attempts,
retries, tokens, and estimated dollars. Estimate from the first two tasks before
scheduling the remaining pilot. Stop scheduling at the cap, retain partial
results, and report incomplete coverage. No always-on expensive benchmark is
needed until the pilot demonstrates useful signal.

## Artifact contract

Proposed schemas and locations below are new work, not existing commands/files.
Store sanitized manifests, schemas, mappings, and summaries under
`prompt-quality/`; keep licensed task code, patches, transcripts, and worker
artifacts in a separately managed artifact store with content-addressed references.

| Artifact | Required contents |
| --- | --- |
| Task manifest | Inputs/hashes, family/split, environment, eligibility, revisions. |
| Behavior evidence | Requirement ID, source spans, derivation, solution/verifier anchors, disposition, alternatives, review status. |
| Reference package | Validated packet/unit/request, exact rendered prompt, renderer/provider revisions, format gaps, worker validation status. |
| Backward mapping | Requirement -> source interpretation -> compiler fields -> packet fields -> exact prompt ranges. |
| Forward capture | Original-input artifacts, identities, complete status/failure, timings and cost. |
| Alignment finding | Difference, evidence, earliest divergence, contributing causes, confidence, review/adjudication. |
| Intervention record | Changed stage/inputs, diagnostic-only marker, downstream propagation and worker effect. |
| Experiment report | Every arm/attempt, verifier and behavior outcomes, missing prompts, exclusions, costs, unresolved findings. |

For example, a lifecycle requirement should link the source's “On exit” span to
the cleanup condition, an accepted packet behavior, a prompt byte range, and
verifier evidence. If classification loses the exit condition, the finding points
to that decision and demonstrates how the prompt permits premature cleanup.
Stable IDs are local to versioned artifacts; hashes identify content revisions.

## Delivery sequence and acceptance criteria

| Change set | Work | Completion evidence |
| --- | --- | --- |
| 1. Worked example and target contract | Inventory actual renderer/schema; manually inspect state-data through the backward process; define artifact schemas and source-support rules. | One complete, reviewed source-to-prompt example rendered through current code; documented representation gaps; reproducible inputs. |
| 2. Automated backward construction | Manifest runner, evidence extractor/reviewer, reference packet construction, integrity checks, review queue, budgets/caching. | Six development tasks processed with no hidden-only obligation accepted; failures and provisional records preserved. |
| 3. Reference worker experiment | Three-arm harness, environment isolation, patch freezing, verifier/behavior evaluation, paired reporting. | All eligible development attempts accounted for; reference benefit or limitation demonstrated; broken harnesses explicit. |
| 4. Backward mapping and forward diagnosis | Interpretation maps, production captures, semantic alignment, failure identity, counterfactual replay. | Each actionable finding connects source meaning to final prompt and implementation evidence or an explicit untested hypothesis. |
| 5. First production improvements | Address the strongest recurring cause; add contrast fixtures; rerun from original inputs. | Clean forward improvement on affected tasks and held-out evaluation; quality/cost tradeoff reported. |
| 6. Broader corpus and recurring operation | Add unrelated task families; calibrated reviewers; selective CI and scheduled worker runs. | Results remain useful beyond tuned families and fit the agreed automated/human budgets. |

Treat these as independently reviewable PRs. Begin with change set 1 so we can
inspect a concrete mapping before investing in automatic generation. Schedule the
following work based on what that example reveals; avoid automating a representation
that cannot express the desired prompt.

For implementation changes, run the repository's required tests, formatting,
linting, type checks, and workflow validation using the shared environment. Add
meaningful checks for stale spans, hidden-only claims, cross-clause mapping,
schema/rendering loss, unsupported reference fields, reviewer abstention,
environment failures, and downstream invalidation. Live experiments belong to a
separate bounded job and publish complete manifests and reports.

The pilot is successful if it yields credible, reproducible reference prompts,
identifies source distinctions the production pipeline loses, and demonstrates
at least one correction propagating from original instruction to improved worker
behavior. If it cannot establish that chain, report which link failed and revise
the approach before expanding or training models.
