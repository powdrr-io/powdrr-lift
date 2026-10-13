# Acceptance criteria pilot

Scores are automated semantic estimates against agent-authored reference labels, unless a review is explicitly marked human_reviewed. They do not measure coding outcomes or certify correctness.

| Task | Arm | Full / reference | Recall | Supported / criteria | Precision |
| --- | --- | --- | --- | --- | --- |
| helm-array-merge-strategies | templates | 29/29 | 100.0% | 63/66 | 95.5% |
| dateutil-rfc5545-timezone-interop | templates | 25/25 | 100.0% | 56/62 | 90.3% |

## helm-array-merge-strategies: templates

- c039 (unsupported): s007 describes 'append: old before new' for array concatenation, but does not state that 'old config wins' in collisions during upgrade. The criterion incorrectly implies conflict resolution semantics for 'append' strategy in upgrade results, which is not specified. 'Wins' implies override logic, but append only defines order, not precedence. No source supports 'old config wins' as a collision rule.
- c042 (unsupported): s007 describes merging old config on top of new defaults, but s002 states 'user fields win' during merge strategy application. The criterion conflates 'old config wins' with 'user fields win' — but 'old config' is not always 'user values' (e.g., in ResetThenReuseValues, old config is merged on top of new defaults, but user values may be different). No source supports 'old config wins over new chart defaults' as a universal key-level rule. This overgeneralizes and misrepresents the semantics.
- c043 (unsupported): s007 describes merging old config on top of new defaults, but does not state that 'collisions resolve to old config wins'. The term 'wins' implies override precedence, but the instruction only describes order of merging. No source defines collision resolution semantics for non-array values or key-level conflicts. This adds an unsupported obligation.

## dateutil-rfc5545-timezone-interop: templates

- c003 (unsupported): The instruction specifies supported parameters but does not mandate rejection of unsupported combinations. This is an additional constraint not present in the source, making it unsupported.
- c032 (unsupported): The instruction does not specify that identical inputs must produce identical VTIMEZONE blocks. While s010 describes how the block is derived, it does not mandate determinism or equivalence for identical inputs — this is an added semantic obligation.
- c035 (unsupported): The instruction says the attributes are read-only tuples in insertion order, but does not state they are the exact same internal objects. The criterion demands identity (same object), which is not mandated — only content and order are specified.
- c037 (unsupported): The instruction requires that dates in component groups be compared in a sorted order for equality (s012), but it does not mandate that the stored tuples themselves be ordered by sorted(). The tuples are specified to be in insertion order (s011), and sorting is only mentioned as a comparison strategy, not a storage requirement. Thus, 'dates in each component group is ordered by sorted' is an added obligation not supported by the source.
- c049 (unsupported): The instruction specifies that rruleset.to_ical() emits one VTIMEZONE block per unique non-UTC timezone (s017), but does not address behavior for rrulesets differing in order or duplication of components. No source supports the claim that such differences produce identical serialization — this adds an implicit requirement about normalization that is not mandated.
- c053 (unsupported): s019 says rrulestr auto-detects BEGIN:VCALENDAR and extracts VTIMEZONE/VEVENT, but does not state that input not beginning with BEGIN:VCALENDAR is rejected. The criterion demands rejection, which is a stronger obligation than implied by 'auto-detects' — this is an unsupported extension.
