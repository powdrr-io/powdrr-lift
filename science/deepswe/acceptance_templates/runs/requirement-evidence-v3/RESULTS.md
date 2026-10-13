# Requirement-scoped retrieval packets

This is a post-routing source retrieval experiment for the frozen GraphQL
instruction in `requirement-contract-v3`. It uses the clean task base
`f07c89f8f065010a36b4263eded209b2b1d37063`; the solution and validation patches
were not mounted in the analysis container.

The index contains 1,589 Python symbols with no parse errors. Retrieval ran
against that full index independently for all 23 `include` and
`include_prohibition` requirements. Each packet stores the query terms, ranked
candidates, selected excerpts, source SHA-256 values and explicit line/character
truncation flags. The full packet set and compressed symbol index are in
`checkpoints.tar.gz`.

The targeted miss was recovered: r022 selects `DSLFragment` and
`DSLFragmentSpread`, and r023 selects `DSLField` plus its public methods,
including `.directives`. In the earlier batch-reranked packet, WebSocket
constructors displaced these DSL declarations. The new retrieval is not yet
selective enough: r023 also includes an unrelated HTTP upload test, and r015
finds only a multipart test helper. This establishes source availability for the
DSL case, not general retrieval quality.

All 23 packets were built from the clean base, with no oracle, tests or coding
agents. Source excerpts may be truncated; the flags and original symbol ranges
must be checked before relying on a packet.
