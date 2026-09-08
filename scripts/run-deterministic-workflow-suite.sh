#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
uv run powdrr-lift workflow-scenario-suite \
  --manifest workflow-evals/scenarios/manifest.yaml \
  --repo-root "$repo_root" \
  --report workflow-scenario-suite-report.json
