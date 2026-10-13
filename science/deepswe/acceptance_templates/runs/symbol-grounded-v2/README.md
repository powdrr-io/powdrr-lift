# Symbol-grounded GraphQL planning

Validated `pipeline.yaml` ran through Procedrr: deterministic Python symbol extraction and candidate retrieval, Flash selection, deterministic code reads, Flash scenarios, Jev premise checks, Flash outcomes, Jev outcome checks, deterministic six-section rendering. Base SHA and clean checkout verified; no coding agent or tests.

- 1,589 symbols, zero parse errors. Six requirement groups; lexical matching plus syntactic neighbors; actual source reads preserved.
- 24 proposals; eight dropped for malformed prose citations, not proven semantic errors. Sixteen premise checks all accepted; ten outcomes withheld; six criteria emitted.
- Manual post-call audit: three usable emitted criteria, one invalid query example, one wrong HTTP error-wrapper outcome, one ambiguous HTTP progress fixture. This is not full solution coverage or evidence of quality improvement.
- Missing implementation still triggers incorrect abstention. Retrieval sometimes conflates result decoding with incremental merging. All five previous target facets remain without complete correct added criteria; original routed requirements remain in the final prompt.

The first symbol pass (`../symbol-grounded-v1`) had 16 LOCAL classifier-adapter failures before any Typesafe.ai requests. This corrected pass reuses all twelve completed Flash selection/proposal calls, with request hashes and cache provenance. Thirty-eight new provider calls completed (16 Flash outcomes, 22 Typesafe.ai decisions); zero provider failures. No repeated generation or solution access during calls.

See `prompt.md`, `audit.json`, `retrieval/`, `procedrr-events.jsonl`, `setup/`, `checkpoints.json` and `checkpoints.tar.gz`. Raw model captures included. Setup/source hashes identify both container code and host harness. The earlier free-form pilot is preserved at `../code-grounded-v1`; it is not the new architecture or a successful quality result.
