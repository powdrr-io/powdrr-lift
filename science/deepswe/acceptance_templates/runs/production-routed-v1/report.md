# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| cattrs-partial-structuring-recovery | templates | 15/17 | 88.2% | 25/25 | 100.0% |
| helm-array-merge-strategies | templates | 27/29 | 93.1% | 50/50 | 100.0% |
| koota-entity-snapshot-rollback | templates | 20/20 | 100.0% | 68/74 | 91.9% |
| dateutil-rfc5545-timezone-interop | templates | 23/25 | 92.0% | 60/65 | 92.3% |

## cattrs-partial-structuring-recovery: templates

- v006 (missing): No criterion explicitly states that failed fields with defaults use those defaults as fallback values in the resulting partial object. While r009 is covered by requirement text, no criterion describes the fallback behavior in the value. This is a missing outcome.
- v016 (partial): v016 requires that partial_structure honors the converter's detailed_validation setting without introducing new exception semantics. While the requirement is stated in r022, none of the criteria explicitly mention respect for detailed_validation in error aggregation, exception propagation, or validation depth. Criteria like c007 and c008 describe the existence of errors and error_map but do not tie them to the detailed_validation flag. Thus, coverage is partial due to missing explicit linkage to the setting.

## helm-array-merge-strategies: templates

- v012 (missing): No criterion explicitly states that the global. prefix is stripped before applying the strategy to the globals map. While c026 and c027 address scoping, they do not mention global. prefix handling. This behavior is missing.
- v024 (partial): Criterion c036 mentions 'same warning conditions' but does not explicitly state that 'non-array' appears in the message. No criterion explicitly contains the phrase 'non-array' in the warning message. Therefore, the requirement for the exact message content is not fully captured.

## koota-entity-snapshot-rollback: templates

- c018 (unsupported): The phrase 'exposes the ECS framework' is vague and not defined in the source. The instruction details specific functions and behaviors but never uses or defines 'exposes' as a criterion. This is an interpretive addition without source support.
- c030 (unsupported): The phrase 'For items equivalent under keys that are equal, retain reject the duplicate key by throwing Error' is ambiguous and not present in the source. The source only requires throwing on duplicate keys (s002), but this criterion adds unclear phrasing ('retain reject'), implies a semantic equivalence rule not mentioned, and lacks source support. It appears to be an attempted paraphrase that introduces confusion and unsupported interpretation.
- c032 (unsupported): The instruction mandates throwing an Error on duplicate traits but does not specify that 'no trait is retained' as a behavioral consequence. This adds an implementation detail (retention policy) not present in the source, making it unsupported.
- c061 (partial): The instruction specifies that arrays in `diffEntitySnapshots` and `diffWorldSnapshots` are sorted ascending (s007, s008), but 'All arrays in the returned object' is overly broad — it implies every array in every return value (e.g., relation target arrays) must be sorted, which is not mandated. Only the top-level arrays (`addedTraits`, `removedTraits`, etc.) are required to be sorted. This criterion overgeneralizes and adds unsupported obligations.
- c062 (unsupported): The criterion attempts to combine multiple conditions into a single equivalence claim about 'inputs producing the same equality result', but the source does not define or require equivalence of inputs — only how equality is computed (shallow comparison, ignoring ordering, {} vs absent). The phrasing 'produce the same equality result' is vague and not source-supported; it implies a meta-property not stated or testable from the spec.
- c066 (partial): The source mandates ascending order only for the top-level arrays in `diffEntitySnapshots` and `diffWorldSnapshots` results (s007, s008). This criterion says 'the returned arrays' without qualification, implying all arrays (e.g., relation target arrays) must be sorted — which is not required. Thus, it overgeneralizes and adds unsupported obligations.

## dateutil-rfc5545-timezone-interop: templates

- v015 (missing): The reference requires rruleset.copy() to create a shallow copy with identical components. No criterion explicitly states this behavior. Criterion c052 and c053 discuss union and subtract, not copy. No criterion mentions copy at all. Therefore, this requirement is missing.
- v017 (partial): The reference requires rruleset.subtract to add other's rrules as exrules and rdates as exdates, and raise TypeError for non-rruleset. Criterion c054 explicitly states that the resulting exrules include other's rrules. Criterion c055 explicitly states that subtract raises TypeError for non-rruleset. Although c054 does not mention rdates → exdates, the requirement for rdates → exdates is not captured by any criterion. Therefore, this is partial coverage.
- c004 (partial): s002 confirms RDATE accepts TZID, VALUE=DATE, and VALUE=DATE-TIME, but does not specify rejection of unsupported forms. The criterion adds an obligation (rejection) not present in the source.
- c014 (unsupported): s019 states rrulestr auto-detects BEGIN:VCALENDAR and extracts VTIMEZONE/VEVENT, but does not mandate rejection of non-VCALENDAR input. It describes behavior for VCALENDAR input but does not specify error handling or rejection for non-compliant input, so the 'rejects' obligation is unsupported.
- c015 (unsupported): s019 says rrulestr extracts VTIMEZONE from VCALENDAR, but does not state that it rejects input lacking VTIMEZONE or containing non-VTIMEZONE content. The criterion demands rejection of non-VTIMEZONE content, which is not mandated in the source.
- c037 (partial): s009 states that rrule.count() 'otherwise iterates (inherited from rrulebase)' when count is not set, which implies equivalence to iteration. However, the criterion phrasing 'behavior is equivalent to iterating over the recurrence set' is not explicitly stated — it is inferred. The source supports the mechanism but not the exact equivalence claim.
- c060 (unsupported): s018 describes rruleset.from_str(s) as wrapping rrulestr with forceset=True, but says nothing about the behavior when forceset is omitted. The criterion invents a default behavior (equivalent to forceset=True) that is not stated, implied, or suggested in the source. This is an unsupported extension.
