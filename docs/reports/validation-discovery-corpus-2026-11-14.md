# Validation discovery corpus evaluation

Corpus revision: `2026-11-14`.

This is static discovery evidence. It does not establish that a command was executed or that a remote check is required.

| Repository | Pinned revision | License | Exact argv matches | Missed obligations | Unreviewed discoveries | Invented required checks | Command/context mismatches | Provider mismatches | Evidence gaps | Unresolved profiles |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| [powdrr-lift](https://github.com/powdrr-io/powdrr-lift) | `7e1d8c2c7a0d` | `NOASSERTION` | 4 | 2 | 7 | 0 | 0 | 0 | 0 | 5 |
| [pytest](https://github.com/pytest-dev/pytest) | `fc9a7ef678f6` | `MIT` | 0 | 0 | 5 | 0 | 0 | 0 | 0 | 4 |
| [flask](https://github.com/pallets/flask) | `49943b5a9441` | `BSD-3-Clause` | 2 | 0 | 10 | 0 | 0 | 0 | 0 | 1 |
| [django](https://github.com/django/django) | `f24f38c6b282` | `BSD-3-Clause` | 1 | 1 | 7 | 0 | 0 | 1 | 0 | 4 |
| [poetry](https://github.com/python-poetry/poetry) | `4acc75e19f12` | `MIT` | 2 | 0 | 2 | 0 | 0 | 0 | 4 | 0 |
| [uv](https://github.com/astral-sh/uv) | `d5c3f1744174` | `Apache-2.0` | 2 | 1 | 107 | 0 | 0 | 0 | 2 | 0 |
| [pdm](https://github.com/pdm-project/pdm) | `9af22ebcd76e` | `MIT` | 0 | 0 | 11 | 0 | 0 | 0 | 0 | 7 |
| [hatch](https://github.com/pypa/hatch) | `893fa13ccd61` | `MIT` | 0 | 0 | 10 | 0 | 0 | 0 | 2 | 10 |
| [conda](https://github.com/conda/conda) | `742ee7878560` | `NOASSERTION` | 0 | 0 | 4 | 0 | 1 | 0 | 0 | 0 |
| [nox](https://github.com/wntrblm/nox) | `00e921b3c00a` | `Apache-2.0` | 1 | 0 | 10 | 0 | 0 | 0 | 0 | 8 |
| [pre-commit](https://github.com/pre-commit/pre-commit) | `ba5c4209a6d5` | `MIT` | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| [tox](https://github.com/tox-dev/tox) | `a72221e1a618` | `MIT` | 0 | 1 | 4 | 0 | 1 | 0 | 0 | 0 |
| [httpx](https://github.com/encode/httpx) | `b5addb64f016` | `BSD-3-Clause` | 0 | 2 | 4 | 0 | 0 | 0 | 0 | 0 |
| [pydantic](https://github.com/pydantic/pydantic) | `560f0f96a37d` | `MIT` | 0 | 0 | 50 | 0 | 0 | 0 | 0 | 23 |

## Totals

- Expected obligations: 32
- Exact executable discoveries: 12
- Missed obligations: 7
- Unreviewed discovery candidates: 231
- Invented required checks: 0
- Command/context mismatches: 3
- Evidence gaps: 9
- Expected unresolved obligations: 18
- Observed unresolved profiles: 62

## powdrr-lift

### Missed obligations

- `{"obligation": "procedural-catalog", "profile": "validate-procedrr-processes", "requiredness": "unknown"}`
- `{"obligation": "deterministic-workflow-scenarios", "profile": "workflow-scenario-suite", "requiredness": "unknown"}`
### Sample unresolved profiles

- `{"profile": "make-format", "reasons": ["Shell or variable expansion in Makefile target 'format' was preserved without expansion."]}`
- `{"profile": "make-format-check", "reasons": ["Shell or variable expansion in Makefile target 'format-check' was preserved without expansion."]}`
- `{"profile": "make-lint", "reasons": ["Shell or variable expansion in Makefile target 'lint' was preserved without expansion."]}`
- `{"profile": "make-test", "reasons": ["Shell or variable expansion in Makefile target 'test' was preserved without expansion."]}`
- `{"profile": "make-typecheck", "reasons": ["Shell or variable expansion in Makefile target 'typecheck' was preserved without expansion."]}`

## django

### Missed obligations

- `{"obligation": "migrations", "profile": "django-migration-check", "requiredness": "unknown"}`
### Provider mismatches

- `{"actual_provider": "pytest", "expected_provider": "unittest", "obligation": "django-unittest", "profile": "pytest"}`
### Sample unresolved profiles

- `{"profile": "make-lint", "reasons": ["No tracked Makefile was found to explain this native command."]}`
- `{"profile": "make-black", "reasons": ["No tracked Makefile was found to explain this native command.", "Make target names alone do not establish that the target validates code; inspect the recorded recipe and workflow context."]}`
- `{"profile": "make-spelling", "reasons": ["No tracked Makefile was found to explain this native command.", "Make target names alone do not establish that the target validates code; inspect the recorded recipe and workflow context."]}`
- `{"profile": "make-epubcheck", "reasons": ["No tracked Makefile was found to explain this native command.", "Make target names alone do not establish that the target validates code; inspect the recorded recipe and workflow context."]}`

## poetry

### Evidence gaps

- `{"file_exists": true, "obligation": "type-check", "path": ".github/workflows/tests.yaml", "profile": "mypy"}`
- `{"file_exists": true, "obligation": "type-check", "path": "pyproject.toml", "profile": "mypy"}`
- `{"file_exists": true, "obligation": "integration-tests", "path": ".github/workflows/tests.yaml", "profile": "pytest"}`
- `{"file_exists": true, "obligation": "integration-tests", "path": "pyproject.toml", "profile": "pytest"}`

## uv

### Missed obligations

- `{"obligation": "reusable-check-workflow", "profile": "reusable-check-workflow", "requiredness": "unknown"}`
### Evidence gaps

- `{"file_exists": true, "obligation": "formatting", "path": "pyproject.toml", "profile": "ruff-format-check"}`
- `{"file_exists": true, "obligation": "lint", "path": "pyproject.toml", "profile": "ruff-check"}`

## hatch

### Evidence gaps

- `{"file_exists": true, "obligation": "documentation-build", "path": "pyproject.toml", "profile": "hatch-1-docs-build-check"}`
- `{"file_exists": true, "obligation": "type-check", "path": "pyproject.toml", "profile": "hatch-5-types-check"}`
### Sample unresolved profiles

- `{"profile": "hatch-1-docs-build-check", "reasons": ["Could not statically parse Hatch settings in pyproject.toml.", "Hatch script docs:build uses context or shell expansion that remains unresolved.", "Hatch script docs:serve uses context or shell expansion that remains unresolved.", "Hatch script docs:ci-build uses context or shell expansion that remains unresolved."]}`
- `{"profile": "hatch-2-docs-ci-build", "reasons": ["Could not statically parse Hatch settings in pyproject.toml.", "Hatch script docs:build uses context or shell expansion that remains unresolved.", "Hatch script docs:serve uses context or shell expansion that remains unresolved.", "Hatch script docs:ci-build uses context or shell expansion that remains unresolved.", "Arguments after the Hatch script selector may supply script context and remain unresolved."]}`
- `{"profile": "hatch-3-docs-build-check", "reasons": ["Could not statically parse Hatch settings in pyproject.toml.", "Hatch script docs:build uses context or shell expansion that remains unresolved.", "Hatch script docs:serve uses context or shell expansion that remains unresolved.", "Hatch script docs:ci-build uses context or shell expansion that remains unresolved."]}`
- `{"profile": "hatch-4-docs-ci-build", "reasons": ["Could not statically parse Hatch settings in pyproject.toml.", "Hatch script docs:build uses context or shell expansion that remains unresolved.", "Hatch script docs:serve uses context or shell expansion that remains unresolved.", "Hatch script docs:ci-build uses context or shell expansion that remains unresolved.", "Arguments after the Hatch script selector may supply script context and remain unresolved."]}`
- `{"profile": "hatch-5-types-check", "reasons": ["Could not statically parse Hatch settings in pyproject.toml.", "Hatch script types:check uses context or shell expansion that remains unresolved."]}`
- … and 5 more.

## conda

### Command/context mismatches

- `{"actual_command": ["python", "-m", "pytest", "`"], "actual_cwd": ".", "expected_command": ["python", "-m", "pytest", "-k", "memray", "--memray"], "expected_cwd": ".", "obligation": "unit-test-workflow", "profile": "pytest"}`

## pre-commit

### Command/context mismatches

- `{"actual_command": ["tox", "-e", "py,pypy3,pre-commit"], "actual_cwd": ".", "expected_command": ["tox", "-e", "py", "pypy3", "pre-commit"], "expected_cwd": ".", "obligation": "tox-checks", "profile": "tox-py,pypy3,pre-commit"}`
### Evidence gaps

- `{"file_exists": true, "obligation": "tox-checks", "path": ".github/workflows/main.yml", "profile": "tox-py,pypy3,pre-commit"}`

## tox

### Missed obligations

- `{"obligation": "reusable-workflow", "profile": "tox-shared-check-workflow", "requiredness": "unknown"}`
### Command/context mismatches

- `{"actual_state": "executable", "expected_state": "unresolved", "obligation": "dynamic-test-matrix", "profile": "tox-1-${{"}`

## httpx

### Missed obligations

- `{"obligation": "native-check-script", "profile": "httpx-check-script", "requiredness": "unknown"}`
- `{"obligation": "native-test-script", "profile": "httpx-test-script", "requiredness": "unknown"}`
