# Scenario-first experiment

Frozen instructions, routed clauses and original-code excerpts from Koota composite traits, GraphQL incremental delivery, and Bandit structured nosec. Generate given/when/observation, resolve with DeepSeek Flash, review with Jev (Typesafe.ai), render without synthesis. Solution/test patches are only post-call audit references.

| Arm | Scenarios | Resolved and accepted | Usable in manual setup/outcome audit |
| --- | ---: | ---: | ---: |
| Original framing | 55 | 7 | 2 |
| Explicit completed-feature framing | Same 55 | 43 | 11 |

The second arm changes only answer/review framing. Original framing often withheld because implementation was absent. Six primary answer calls failed with no streaming events; the adaptive arm had none. Acceptance counts are not correctness, solution coverage, or coding-agent pass rates.

Setup errors dominate: wrong world/entity receivers, wrong Bandit finding IDs, incomplete GraphQL payloads, and invalid paths. Some valid setups also receive wrong answers (callback array shape, region line boundaries, current hasNext). Existing-code retrieval is incomplete, so these results cannot isolate model capability from missing context. Eleven usable criteria still miss most of the same 18 target facets; see `audit.json`.

Jev rejected four obvious contradictions in paired calibration, but accepted flawed premises and subtle errors. One exact GraphQL generation replay captured valid raw JSON unchanged by the parser; it did not reproduce the original truncation, whose raw output was unavailable.

Next experiment: establish actual API/input validity from relevant code before resolving one observation; keep completed-feature framing. Audit premise validity independently of conditional outcome agreement. Do not promote this arm to production.

Artifacts: `audit.json`, `provenance.json`, primary/future prompts and results, calibration and parser diagnostic. Full requests, responses, receipts and raw diagnostic are indexed in `call-checkpoints.json` and archived in `call-checkpoints.tar.gz` (191 provider calls: 133 DeepInfra, 58 Typesafe.ai). No tests or coding agents were run.

Shared Python environment; `PYTHONPATH` must include this worktree's `src` and root. `run.py` runs the primary arm, `run_future.py` the matched adaptive arm, `calibrate.py` the reviewer probes, `capture_generation.py` the single raw-parser replay. These scripts can incur provider calls when checkpoints are absent. `audit.py` is offline and refreshes archive/scores only. Unpack the archive into this directory to reuse captured checkpoints. No production changes.
