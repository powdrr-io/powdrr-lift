# Initial pilot: four new DeepSWE repository families

The standalone runner completed direct and template generation for cattrs, Helm, dateutil, and Koota, then reviewed all eight outputs. The generator was DeepInfra `deepseek-ai/DeepSeek-V4-Flash-0731`; the reviewer was DeepInfra `Qwen/Qwen3-Next-80B-A3B-Instruct`. Both arms shared each task's requirement inventory. References were authored from the instructions before inspecting generated output. Neither generator received references, solution patches, or validation patches.

The [combined report](runs/summary-v1/report.md), [machine-readable report](runs/summary-v1/report.json), and [agent spot audit](runs/summary-v1/agent-spot-audit.json) are the primary results. Per-task prompt, binding, and review artifacts are in [pilot-v1](runs/pilot-v1/report.md) and [pilot-extra-v1](runs/pilot-extra-v1/report.md). Full request/response checkpoints are bundled with each cohort.

## Raw automated estimates

| Measure | Direct | Templates |
| --- | --- | --- |
| Fully covered reference validations | 90 / 91 | 89 / 91 |
| Strict recall | 98.9% | 97.8% |
| Supported criterion rows | 130 / 133 | 231 / 233 |
| Strict criterion-level precision | 97.7% | 99.1% |
| Tasks with every reference covered | 3 / 4 | 2 / 4 |
| Generation attempts, excluding shared inventory | 24 | 28 |
| Sum of model-call durations, excluding shared inventory | 441.5 seconds | 730.1 seconds |
| Rendered criterion characters | 47,613 | 37,176 |

These are **automated estimates against agent-authored reference labels**. They are not validated precision/recall measurements. The two arms have different criterion granularity and total generation budgets. One attempt per task is not enough to establish stable latency or quality differences. The sum of call durations is not the experiment's wall-clock duration: the cohorts ran concurrently, and explicit resumes reused completed work.

This initial implementation has not established a recall improvement from templates. The catalog produces more, shorter criterion rows and adds binding work. Its apparent precision improvement is also uncertain because spot checks found reviewer errors.

## Concrete findings

1. **Unsupported identity from a read-only property.** The dateutil template arm selects T31 and requires returned tuples to be the identical internal tuple. The instruction requires read-only tuples in insertion order, not identity. The automated support review correctly flags this. It is a candidate/applicability error, not a renderer problem.
2. **Unsupported precedence from an ordering requirement.** The Helm template arm says old config wins collisions for append. The source specifies old-before-new array ordering, not a general collision winner. The reviewer correctly flags this. The binding strengthened the requirement.
3. **An invented cattrs rule escaped the support reviewer.** Direct criterion c022 requires an empty `error_map` when `detailed_validation=False`. The instruction does not establish that policy, and the solution's fallback still passes per-field errors into the result. The reviewer labels it supported and fully covering the reference. Its 100% precision estimate for cattrs therefore cannot be trusted.
4. **Coverage review can confuse a weak extra criterion with a missing requirement.** The Helm template review labels the non-array warning reference partial, while its rationale explicitly identifies c055 as fully satisfying it. c055 names the invalid condition and required `non-array` warning. The additional vague c056 does not erase that valid coverage; it belongs in the precision/verbosity review. The raw template recall is understated on this item.
5. **Operation scope remains hard for the reviewer too.** The Helm direct-arm review says the source does not support preserving nil during merging, although the source explicitly says it does. The emitted criterion also uses the ambiguous phrase “a merge strategy” without naming the merge/coalesce operation, so its scope needs review. The review rationale cannot be accepted as-is.
6. **Comment correction remains an interpretation case.** The dateutil source says a comment references RFC 5445 instead of RFC 5545. The reference treats that as a requested correction; the direct arm treats the typo as a present-state property. An independent reference review should settle this before treating that row as definitive recall evidence.

The spot audit records artifact hashes, exact criterion IDs/text, source spans, and distinguishing patch evidence. It preserves the original model reviews and scores. It is an agent-authored spot review, not a full human adjudication or a complete alternative score.

## Reliability findings

- One Helm direct-generation response repeated criteria until ending in an empty item. A local validator rejected the empty item; the one allowed schema correction completed. Both attempts and their durations are retained.
- Koota binding failed because a streamed response ended before a completion marker. The provider layer classified it as `_ModelUnavailableError`, but the captured detail identifies an interrupted stream rather than an unavailable model. A bounded explicit resume completed the failed batch and remaining batches.
- Reviewers sometimes cited source IDs where requirement IDs were expected. Schema/evidence validation caught those mistakes and recorded the corrections.
- The pilot exposed a resume bug: a failed first schema attempt was repeated instead of reusing its completed correction. The runner now reconstructs the saved correction transcript and reuses it; regression checks cover this. A superseded review attempt remains in the archive for transparency.
- Explicit transport resume preserves the original failed attempt and consumes the next persisted attempt. It cannot repeatedly reset the retry budget. Missing usage receipts are reported as unknown rather than zero tokens.

## Next experiment suggested by this evidence

First calibrate the semantic reviewer against the spot-audit contrasts and review the ambiguous reference labels. Precision estimates that miss invented mandates cannot guide classifier changes reliably. Keep these raw artifacts as the initial baseline.

Then make one offline applicability/binding change aimed at the demonstrated failures: read-only/equal-value language must not select identity, and order must not imply precedence. Compare on the same development cases, then add new repository families with separately drafted source-based references. Hold total prompt length and generation cost constant in the later comparison. Production integration and coding-outcome evaluation need additional evidence.

## Recompute the summary without model calls

```bash
rtk proxy /Users/gregory/code/powdrr-lift/.venv/bin/python \
  -m science.deepswe.acceptance_templates.summary \
  --reports science/deepswe/acceptance_templates/runs/pilot-v1/report.json \
            science/deepswe/acceptance_templates/runs/pilot-extra-v1/report.json \
  --output-dir /tmp/acceptance-summary
```

The combiner rejects repeated task/arm entries, different generation settings, and paired arms with different input or reference fingerprints. It also reports metrics by reference/review evidence level.
