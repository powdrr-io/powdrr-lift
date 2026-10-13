# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| helm-array-merge-strategies | templates | 29/29 | 100.0% | 42/42 | 100.0% |
| dateutil-rfc5545-timezone-interop | templates | 24/25 | 96.0% | 53/65 | 81.5% |

## helm-array-merge-strategies: templates

No omissions or unsupported assertions flagged by this automated review.

## dateutil-rfc5545-timezone-interop: templates

- v024 (missing): The reference requires correcting a comment that references 'RFC 5445' to 'RFC 5545'. Criterion c063 only states that such a comment exists — it does not require or verify the correction. No criterion confirms the comment has been fixed. Therefore, coverage is missing.
- c002 (unsupported): The instruction specifies that RDATE 'supports' these parameters but does not mandate rejection of RDATE without them. Parsing behavior and rejection rules are not specified, so this adds an obligation not present in the source.
- c009 (unsupported): The instruction describes output behavior (what is emitted) but does not specify input validation or rejection rules for DTSTART without TZID. This criterion adds a parsing/acceptance constraint not present in the source.
- c010 (unsupported): The instruction describes output format for UTC dtstart but does not mandate input validation or rejection of DTSTART without Z. This adds a new constraint not supported by the source.
- c017 (unsupported): s005 describes the expected output format for RDATE/EXDATE but does not mandate rejection of malformed inputs (e.g., missing timezone indicator). The criterion adds a validation obligation not present in the source.
- c019 (unsupported): s005 specifies the correct format for EXRULE lines but does not state that other prefixes or missing prefixes should be rejected. The criterion adds input validation behavior not mandated by the source.
- c024 (unsupported): s007 says eval(repr(r)) yields an equivalent rrule, implying reconstructability, but does not mandate rejection of non-symbolic expressions. c024 adds a validation constraint not present in the source.
- c033 (unsupported): s010 specifies the output format (VCALENDAR/VEVENT) but does not state that other formats are rejected. The criterion adds a new obligation (rejection of other formats) not present in the source. This is an extension.
- c035 (unsupported): s010 describes what must be included for non-UTC dtstart but does not state that a VTIMEZONE without STANDARD is rejected. The criterion adds a new validation rule (rejection) not mandated by the source.
- c041 (unsupported): The source mandates that equality ignores order by sorting dates internally, but does not state or imply that the stored dates themselves are sorted. The criterion incorrectly assumes internal sorted order, which is not required by the instruction.
- c043 (unsupported): Source s013 describes the expected format but does not forbid or reject other formats. The criterion imposes a hard restriction ('rejects any other representation format') not present in the source, which only specifies the required format, not exclusivity.
- c049 (unsupported): The instruction specifies that rruleset.subtract(other) adds other's rrules as exrules and rdates as exdates, but does not state that the resulting rruleset's exrules contain 'exactly' other's rrules or that exdates contain 'exactly' other's rdates. The word 'exactly' introduces a cardinality and exclusivity constraint not present in the source. Also, the criterion implies bidirectional equivalence between input and output sets, which is not mandated. No source span supports this precise 'exactly' formulation.
- c055 (unsupported): Source s019 says rrulestr auto-detects BEGIN:VCALENDAR and extracts VTIMEZONE and VEVENT, but does not state that it 'rejects non-VCALENDAR input'. The source implies VCALENDAR detection is optional (auto-detection), not mandatory. Rejecting non-VCALENDAR is an added obligation not present in the source.
