# Solution coverage matrix

| ID | Target | Earlier hand | Previous generated | New focused candidate | Same-analysis control |
| --- | --- | --- | --- | --- | --- |
| D01 | public API | full | full | partial | full |
| D02 | createAspect | partial | full | partial | full |
| D03 | createAspect | full | full | full | full |
| D04 | createAspect | full | full | partial | full |
| D05 | createAspect | partial | full | partial | full |
| D06 | metadata | missing | wrong | partial | wrong |
| D07 | configuration | full | wrong | partial | partial |
| D08 | has/get | full | full | full | full |
| D09 | get/readEach | missing | partial | missing | missing |
| D10 | set | missing | full | missing | full |
| D11 | set/add | partial | full | partial | full |
| D12 | set | missing | missing | missing | missing |
| D13 | add | full | partial | partial | partial |
| D14 | remove | partial | full | partial | full |
| D15 | direct query | full | partial | partial | partial |
| D16 | callback slots | partial | wrong | partial | wrong |
| D17 | updateEach | partial | partial | partial | partial |
| D18 | updateEach options | missing | full | missing | full |
| D19 | updateEach options | missing | full | missing | partial |
| D20 | updateEach lifecycle | missing | full | missing | full |
| D21 | updateEach notifications | missing | full | missing | full |
| D22 | Not | full | partial | partial | partial |
| D23 | Changed | partial | full | partial | full |
| D24 | Added/Removed | partial | partial | partial | partial |
| D25 | Or | missing | full | missing | full |
| D26 | subscriptions | full | partial | partial | partial |
| D27 | subscriptions | full | full | missing | full |
| D28 | onChange multiplicity | missing | full | missing | missing |
| D29 | query cache | missing | missing | missing | missing |
| D30 | multiple Not groups | missing | missing | missing | missing |
| D31 | tracking generations | missing | missing | missing | missing |
| D32 | factory schemas | missing | missing | missing | missing |
| D33 | reset | full | full | missing | missing |
| D34 | set notification equality | wrong | full | full | partial |

All 34 ideal-reference targets are explicit by construction. Exact quotations, line locations, source/patch references and reasons are in comparison.json.
