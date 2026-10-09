# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| dateutil-rfc5545-timezone-interop | direct | 24/25 | 96.0% | 31/32 | 96.9% |
| dateutil-rfc5545-timezone-interop | templates | 25/25 | 100.0% | 61/62 | 98.4% |
| koota-entity-snapshot-rollback | direct | 20/20 | 100.0% | 42/42 | 100.0% |
| koota-entity-snapshot-rollback | templates | 20/20 | 100.0% | 67/67 | 100.0% |

## dateutil-rfc5545-timezone-interop: direct

- v024 (missing): The requirement r031 states a comment references 'RFC 5445' instead of 'RFC 5545', but the criteria do not include any validation or correction of source code comments. The criteria focus on behavior, not comment text correction. Thus, v024 is not covered by any criterion.
- c031 (unsupported): s020 states 'A comment references "RFC 5445" instead of "RFC 5545"' — this describes a typo, but does not assert the absence of 'RFC 5545' in any comment, nor does it confirm the presence of 'RFC 5445' in the source code. The criterion demands exact string matches and absence of 'RFC 5545', which goes beyond the source's observation of a single error. Source is ambiguous about code content.

## dateutil-rfc5545-timezone-interop: templates

- c035 (unsupported): The instruction says the attributes are 'read-only tuples' but does not specify whether they are the same object as the internal tuple (identity) or a copy. The criterion demands identity ('identical object'), which is an unmandated implementation detail not supported by the source.

## koota-entity-snapshot-rollback: direct

No omissions or unsupported assertions flagged by this automated review.

## koota-entity-snapshot-rollback: templates

No omissions or unsupported assertions flagged by this automated review.
