# Design proposal overlay

## Objective

Keep current specification YAML as a read-only canonical product graph and let
LLMs create and revise separate semantic proposal operations. Validate the
canonical graph plus proposal and expose a derived resolved preview.

## Progress

- [ ] Canonical graph projection
- [ ] Proposal overlay and validation
- [ ] LLM-facing context and CLI integration
- [ ] Tests and verification

## Decisions

- Canonical documents remain YAML and are never edited by proposal operations.
- Proposal operations use stable graph IDs instead of YAML paths or line edits.
- The resolved graph is derived and shown as a preview, never stored as the
  canonical source.
