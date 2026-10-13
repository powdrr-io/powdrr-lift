# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| helm-array-merge-strategies | templates | 29/29 | 100.0% | 40/40 | 100.0% |
| dateutil-rfc5545-timezone-interop | templates | 25/25 | 100.0% | 56/65 | 86.2% |

## helm-array-merge-strategies: templates

No omissions or unsupported assertions flagged by this automated review.

## dateutil-rfc5545-timezone-interop: templates

- c002 (unsupported): The instruction specifies that RDATE 'gains support' for these parameters but does not mandate rejection of RDATE without them. Rejection is an added constraint not present in the source.
- c009 (unsupported): The instruction describes output behavior but does not mandate input validation or rejection of malformed DTSTART. Rejection is an added constraint not in the source.
- c010 (unsupported): The instruction describes output format for UTC, but does not require input validation or rejection of missing Z suffix. This is an added enforcement not in the source.
- c017 (unsupported): s005 describes what is emitted (TZID for non-UTC, Z for UTC) but does not mandate rejection of malformed inputs (e.g., missing timezone indicators). The criterion adds a validation obligation not present in the source.
- c019 (unsupported): s005 describes output format but does not specify input validation or rejection of malformed EXRULE lines. The criterion adds a parsing enforcement obligation not found in the source.
- c033 (unsupported): s010 specifies the output format (VCALENDAR/VEVENT) but does not state that other formats are rejected. The criterion imposes a new restriction (rejection of other formats) not present in the source. No source supports this exclusivity claim.
- c035 (unsupported): s010 describes what is produced for non-UTC dtstart but does not state that a VTIMEZONE without STANDARD is rejected. The criterion adds a new validation rule (rejection) not mandated by the source. Unsupported.
- c043 (unsupported): Source s013 describes the expected format but does not prohibit or reject other formats. The criterion imposes a hard restriction ('rejects any other representation format') not mandated by the source, which only specifies the required format without exclusivity.
- c049 (unsupported): The instruction specifies that rruleset.subtract(other) adds other's rrules as exrules and rdates as exdates, but does not state that the resulting rruleset's exrules contain 'exactly' other's rrules or that exdates contain 'exactly' other's rdates. The word 'exactly' introduces a cardinality and exclusivity constraint not present in the source. Also, no mention is made of the resulting rruleset retaining its own original exrules/exdates — the criterion implies a replacement, while the source implies an addition. This is an over-specification not supported by the source.
