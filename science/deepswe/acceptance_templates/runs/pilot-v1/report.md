# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| cattrs-partial-structuring-recovery | direct | 17/17 | 100.0% | 26/26 | 100.0% |
| cattrs-partial-structuring-recovery | templates | 16/17 | 94.1% | 38/38 | 100.0% |
| helm-array-merge-strategies | direct | 29/29 | 100.0% | 31/33 | 93.9% |
| helm-array-merge-strategies | templates | 28/29 | 96.6% | 65/66 | 98.5% |

## cattrs-partial-structuring-recovery: direct

No omissions or unsupported assertions flagged by this automated review.

## cattrs-partial-structuring-recovery: templates

- v016 (partial): Criteria c032 and c033 address detailed_validation=False behavior, but v016 requires that detailed_validation is respected without specifying new exception semantics — c032 and c033 only describe the absence of detailed errors, not that the converter's existing detailed_validation setting is honored in all cases (e.g., when True). The criteria do not fully capture the 'respect' requirement as a general invariant.

## helm-array-merge-strategies: direct

- c031 (unsupported): s002 states 'Null user values delete the key during coalescing', while the criterion requires null to be preserved. This is a direct contradiction. The instruction does not support preserving null values during merging; it explicitly states they are deleted. Therefore, this criterion adds an unsupported obligation.
- c033 (unsupported): The criterion text is incomplete ('When a 'merge' strategy is applied and a user object contains'), making it unverifiable. It lacks a complete predicate or observable outcome. No source supports a partial or truncated requirement. This criterion cannot be validated and is therefore unsupported.

## helm-array-merge-strategies: templates

- v024 (partial): Requirement r024 requires a warning with 'non-array' when a strategy path resolves to a non-array. Criterion c055 captures this message condition. Criterion c056 only states 'validation of the resolved value type' without specifying the 'non-array' message or outcome, so it is incomplete. Only c055 fully satisfies the requirement.
- c039 (unsupported): s007 describes 'ReuseValues' as merging old config with new values using strategy-aware coalescing with 'append: old before new', but it does not specify that 'old config wins' in collisions for append. The instruction defines behavior for array ordering, not collision resolution between conflicting keys. This criterion adds an unmentioned obligation.
