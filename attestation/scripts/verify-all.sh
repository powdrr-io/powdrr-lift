#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV=/Users/gregory/code/powdrr-lift/.venv
PYTHON="$VENV/bin/python"
export PATH="$VENV/bin:$PATH"
export VIRTUAL_ENV="$VENV"
export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_NO_SYNC=1
export PYTHONPATH="$ROOT/attestation/verifier/src:$VENV/lib/python3.12/site-packages"

"$PYTHON" -m pytest -q "$ROOT/attestation/verifier/tests"
"$VENV/bin/ruff" check "$ROOT/attestation/verifier"
"$VENV/bin/ruff" format --check "$ROOT/attestation/verifier"
"$VENV/bin/mypy" --strict "$ROOT/attestation/verifier/src"

temporary_dir="$(mktemp -d)"
trap 'rm -rf "$temporary_dir"' EXIT
export UV_CACHE_DIR="$temporary_dir/uv-cache"
rtk uv build --wheel --out-dir "$temporary_dir/wheels" "$ROOT/attestation/verifier"
"$PYTHON" -c 'import pathlib, sys, zipfile; wheel = next(pathlib.Path(sys.argv[1]).glob("*.whl")); zipfile.ZipFile(wheel).extractall(sys.argv[2])' \
  "$temporary_dir/wheels" "$temporary_dir/installed"
(cd "$temporary_dir" && "$PYTHON" -I -c \
  'import sys; sys.path.insert(0, sys.argv[1]); import powdrr_attestation; from powdrr_attestation.configuration import parse_validation_policy; from pathlib import Path; sample = Path(sys.argv[2]).read_bytes(); assert parse_validation_policy(sample).steps' \
  "$temporary_dir/installed" "$ROOT/attestation/fixtures/valid/policy.yaml")
