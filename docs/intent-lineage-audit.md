# Intent lineage audit

`powdrr-lift audit-intent-lineage` replays an immutable JSON history manifest
and writes a deterministic report. It checks proposal and review identity,
source and clause provenance, operation-to-execution-unit and packet links,
actualization findings, validation freshness, accepted Structrr state identity,
supersession, and current invariant evidence.

The audit does not treat historical acceptance as proof of current health. A
current invariant failure or evidence tied to an older repository tree remains
a finding even when every prior proposal was accepted.

## Run

```sh
powdrr-lift audit-intent-lineage --input intent-history.json
powdrr-lift audit-intent-lineage --input intent-history.json --output audit.json
```

The command prints the report and returns status 1 when findings remain. The
report fingerprint is calculated from canonical JSON content, excluding only
its own fingerprint field. Replaying unchanged input produces the same report.

## Input contract

The top level uses `schema_version: intent-lineage-history-v1` and contains:

- `sources`: immutable source records with `update_id`, `source_ref`,
  `exact_text`, and a matching SHA-256 `content_fingerprint`;
- `intent_clause_history`: all clause versions, including inactive versions;
- `active_intent_clauses`: the current active clause set and each clause's
  `source_ref`, `version`, `active`, and optional `supersedes_clause_id`;
- `revisions`: ordered records containing a positive `revision`,
  `parent_proposal_fingerprint` (null for the first revision), a serialized and
  fingerprint-valid `proposal_revision`, a matching accepted `proposal_review`,
  and the following subrecords:

  - `lineage`: `source_update_ids`, `intent_clause_ids`,
    `execution_unit_ids`, `intent_packet_fingerprints`,
    `validation_evidence_fingerprints`, `accepted_structrr_fingerprint`, and
    `repository_tree`;
  - `execution_units`: unit IDs, the proposal fingerprint, and operation IDs;
  - `intent_packets`: packet fingerprints, execution unit IDs, the proposal
    fingerprint, and operation IDs;
  - `actualization_report`: the report emitted by final implementation review;
  - `validation_evidence`: records with fingerprint, candidate tree, status,
    and contract identity;
  - `accepted_structrr_state`: the accepted state fingerprint, repository
    tree, and complete snapshot whose normalized digest matches the fingerprint.

- `current_invariant_evidence`: exactly one current result per active clause,
  with clause ID, outcome, evidence fingerprint, and candidate tree.

Artifact references in findings use stable manifest locations such as
`revision:3`, `proposal_review`, and `actualization_report`; subject IDs and
messages identify the specific failed edge or evidence record.

The audit reports concrete findings with severity counts, identifies the first
revision with operation or semantic drift, and lists the latest repository tree.
Missing source data, receipts, units, packets, validation, or accepted state is
reported as an unresolved lineage edge instead of being inferred.
