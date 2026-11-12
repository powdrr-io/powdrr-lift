"""One protected, immutable Workrr implementation run for accepted references.

Protected runs use macOS Seatbelt or Linux bubblewrap. Unsupported protection
fails before a model invocation; there is no prompt-only fallback.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from powdrr_contracts.models import ChatCompletionRequest, ChatCompletionResponse
from powdrr_service.decision_obligation import content_fingerprint
from powdrr_service.structrr.reference_catalog import (
    ReferenceCatalog,
    compile_reference_catalog,
)
from powdrr_service.structrr.reference_validation import (
    ReferenceReviewClient,
    ReferenceValidationReport,
    validation_input_identity,
)
from powdrr_service.structrr.source_reader import ReaderLimits
from powdrr_service.structrr.source_reference_handoff import (
    CompiledSourceReferenceHandoff,
    compile_source_reference_handoff,
)

from powdrr_lift.workrr.coding_agent import (
    CodingAgentAttempt,
    CodingAgentAttemptStore,
    CodingAgentOutcome,
    CodingAgentRunner,
    CodingAgentStatus,
    ImplementationRequest,
    MiniSWEAgentProvider,
    _committed_paths,
    _working_paths,
    classify_coding_agent_attempt,
)
from powdrr_lift.workrr.coding_agent_validation import (
    ValidationProfile,
    ValidationReportStatus,
    execute_coding_agent_attempt,
)
from powdrr_lift.workrr.source_reference_validation import (
    SourceReferenceValidationContext,
    _git,
    _paths_output,
)

POLICY = "structrr-source-reference-run-v1"
CHECK_COMMAND = ("python", "-m", "powdrr_agent.execution.source_reader", "check")


def _write_once(path: Path, value: Mapping[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        stream.write(
            json.dumps(dict(value), sort_keys=True, ensure_ascii=False, allow_nan=False)
            + "\n"
        )


def _digest(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Frozen source inputs are missing or replaced.")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def context_delivery_status(
    events: list[dict[str, Any]], refs: set[str], catalog_digest: str
) -> dict[str, Any]:
    """Reconstruct delivered canonical pages, including interleaved continuations."""
    pages: dict[str, list[tuple[int, int]]] = {ref: [] for ref in refs}
    totals: dict[str, int] = {}
    invalid: set[str] = set()
    for event in events:
        if (
            event.get("catalog_digest") != catalog_digest
            or event.get("enrichment_mode") != "verified_reader"
        ):
            continue
        for page in event.get("context_pages", []):
            ref = page.get("ref")
            if ref not in refs:
                continue
            start, end, total = (
                page.get("offset"),
                page.get("end_offset"),
                page.get("total_characters"),
            )
            if (
                any(type(n) is not int for n in (start, end, total))
                or not 0 <= start <= end <= total
            ):
                invalid.add(ref)
                continue
            if ref in totals and totals[ref] != total:
                invalid.add(ref)
            totals[ref] = total
            pages[ref].append((start, end))
    missing = set(invalid)
    for ref in sorted(refs):
        offset = 0
        for start, end in sorted(pages[ref]):
            if start > offset:
                break
            offset = max(offset, end)
        if ref not in totals or offset != totals[ref]:
            missing.add(ref)
    return {
        "complete": not missing,
        "incomplete_refs": sorted(missing),
        "catalog_digest": catalog_digest,
    }


@dataclass(frozen=True, slots=True)
class SourceReferenceRunResult:
    _data_json: str

    def to_data(self) -> dict[str, Any]:
        return json.loads(self._data_json)

    @property
    def accepted(self) -> bool:
        return self.to_data()["outcome"] == "accepted"


class SourceRunFailure(ValueError):
    def __init__(self, outcome: str, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


class SourceReferenceRun:
    """Trusted composition; prepare once, invoke one worker, validate, terminate."""

    workspace_root: Path
    root: Path
    private_root: Path
    public_root: Path
    profile_path: Path
    catalog: ReferenceCatalog
    handoff: CompiledSourceReferenceHandoff
    request: ImplementationRequest
    profiles: dict[str, ValidationProfile]
    model: str
    worker_timeout_seconds: float
    validation_timeout_seconds: float
    reader_limits: ReaderLimits
    run_id: str
    inputs: dict[str, str]
    validation_context: SourceReferenceValidationContext
    _configuration: dict[str, Any]
    _sealed: dict[str, str]
    _invoked: bool
    _executed: bool
    _last_attempt: CodingAgentAttempt | None
    claims: tuple[dict[str, Any], ...]
    completion: dict[str, Any] | None
    delivery: dict[str, Any] | None
    readonly_support_files: dict[str, str]
    _candidate_after_worker: str | None

    @classmethod
    def prepare(
        cls,
        *,
        workspace_root: Path,
        accepted_design: Mapping[str, Any],
        base_commit: str,
        objective: str,
        allowed_paths: tuple[str, ...],
        profiles: Mapping[str, ValidationProfile],
        artifact_root: Path,
        run_id: str,
        model: str,
        worker_timeout_seconds: float = 300,
        validation_timeout_seconds: float = 600,
        private_validation_data: Mapping[str, Any] | None = None,
        authorized_amendment: Mapping[str, Any] | None = None,
        review_client: ReferenceReviewClient | None = None,
        test_collection_evidence: dict[str, Any] | None = None,
        reader_limits: ReaderLimits | None = None,
        readonly_support_files: Mapping[str, str] | None = None,
    ) -> SourceReferenceRun:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("run_id must be a simple stable run identity")
        if not model.strip() or any(
            not math.isfinite(n) or n <= 0
            for n in (worker_timeout_seconds, validation_timeout_seconds)
        ):
            raise ValueError("model and finite positive run budgets are required")
        if (
            not profiles
            or "structrr-references" in profiles
            or any(not p.command or name != p.name for name, p in profiles.items())
        ):
            raise ValueError("Explicit non-reference validation profiles are required")
        self = cls()
        self.workspace_root = workspace_root.resolve(strict=True)
        artifact_root = artifact_root.resolve()
        if artifact_root.is_relative_to(self.workspace_root):
            raise ValueError("Run artifacts must be outside the candidate checkout")
        if _git(
            self.workspace_root, "rev-parse", "HEAD"
        ).decode().strip() != base_commit or _working_paths(
            self.workspace_root, output=_paths_output
        ):
            raise ValueError(
                "Reference runs require a clean checkout exactly at the accepted base"
            )
        if (
            Path(
                _git(self.workspace_root, "rev-parse", "--show-toplevel")
                .decode()
                .strip()
            ).resolve()
            != self.workspace_root
        ):
            raise ValueError("Reference runs require the repository workspace root")
        support_files: dict[str, str] = {}
        for raw_path, content in (readonly_support_files or {}).items():
            relative = Path(raw_path)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or not isinstance(content, str)
            ):
                raise ValueError("Protected support files require safe relative paths")
            target = self.workspace_root / relative
            if any(
                parent.is_symlink()
                for parent in target.parents
                if parent != self.workspace_root
                and parent.is_relative_to(self.workspace_root)
            ):
                raise ValueError("Protected support files cannot traverse symlinks")
            if not target.resolve(strict=False).is_relative_to(self.workspace_root):
                raise ValueError(
                    "Protected support file resolves outside the workspace"
                )
            if target.exists() or target.is_symlink():
                raise ValueError(f"Protected support file already exists: {raw_path}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            support_files[relative.as_posix()] = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
        if support_files:
            _git(self.workspace_root, "add", "-f", "--", *sorted(support_files))
        self.readonly_support_files = dict(sorted(support_files.items()))
        self.catalog = compile_reference_catalog(
            accepted_design,
            base_commit=base_commit,
            authorized_amendment=authorized_amendment,
        )
        self.profiles = dict(sorted(profiles.items()))
        self.handoff = compile_source_reference_handoff(
            accepted_design,
            self.catalog,
            objective=objective,
            allowed_paths=allowed_paths,
            verification_commands=tuple(p.command for p in self.profiles.values()),
            reference_check_command=CHECK_COMMAND,
            private_validation_data=private_validation_data,
            workspace_root=str(self.workspace_root),
        )
        self.request = replace(
            ImplementationRequest.from_source_reference_handoff(
                self.handoff,
                request_id=run_id,
                validation_profiles=tuple(self.profiles),
            ),
            allowed_commands=tuple(
                shlex.join(p.command) for p in self.profiles.values()
            ),
            protected_paths=tuple(support_files),
            allow_existing_changes=bool(support_files),
        )
        self.model = model
        self.worker_timeout_seconds = worker_timeout_seconds
        self.validation_timeout_seconds = validation_timeout_seconds
        self.reader_limits = reader_limits or ReaderLimits()
        self.run_id = run_id
        self.root = artifact_root / run_id
        self.root.mkdir(parents=True, exist_ok=False)
        self.private_root, self.public_root = (
            self.root / "private",
            self.root / "worker",
        )
        self.private_root.mkdir(mode=0o700)
        self.public_root.mkdir(mode=0o700)
        self.inputs = validation_input_identity(self.handoff)
        self.validation_context = SourceReferenceValidationContext(
            self.handoff,
            self.catalog,
            recorded_inputs=self.inputs,
            review_client=review_client,
            test_collection_evidence=test_collection_evidence,
        )
        self._invoked = False
        self._executed = False
        self._last_attempt = None
        self.claims = ()
        self.completion = None
        self.delivery = None
        self._candidate_after_worker = None
        self._configuration = {
            "schema_version": POLICY,
            "mode": "enforce",
            "run_id": run_id,
            "workspace_root": str(self.workspace_root),
            "inputs": self.inputs,
            "request_fingerprint": content_fingerprint(self.request.to_data()),
            "model": model,
            "mini_swe_agent_version": importlib.metadata.version("mini-swe-agent"),
            "worker_timeout_seconds": worker_timeout_seconds,
            "validation_timeout_seconds": validation_timeout_seconds,
            "limits_digest": self.reader_limits.digest,
            "isolation": self._isolation_name(),
            "profiles": {name: p.to_data() for name, p in self.profiles.items()},
            "readonly_support_files": self.readonly_support_files,
        }
        payloads = {
            "catalog.json": self.catalog.to_data(),
            "handoff.json": self.handoff.to_data(),
            "validation-inputs.json": self.inputs,
            "configuration.json": self._configuration,
        }
        for name, data in payloads.items():
            _write_once(self.private_root / name, data)
        self.profile_path = self.private_root / "worker.sb"
        self.profile_path.write_text(self._sandbox_profile(), encoding="utf-8")
        self._sealed = {
            name: _digest(self.private_root / name) for name in (*payloads, "worker.sb")
        }
        for directory in ("tmp", "cache", "config", "data", "state", "pycache"):
            (self.public_root / directory).mkdir()
        try:
            self._probe_protection()
        except Exception as error:
            _write_once(
                self.private_root / "preflight.json",
                {"status": "blocked", "error": str(error), "inputs": self.inputs},
            )
            raise
        return self

    def verify_inputs(self) -> None:
        if any(
            _digest(self.private_root / name) != digest
            for name, digest in self._sealed.items()
        ):
            raise SourceRunFailure(
                "invalid_inputs", "Frozen source validation inputs changed"
            )
        if (
            self.catalog.digest != self.inputs["catalog_digest"]
            or self.request.to_data()["source_reference_mode"] != "enforce"
        ):
            raise SourceRunFailure(
                "invalid_inputs", "Source run authority or mode changed"
            )
        if (
            content_fingerprint(self.request.to_data())
            != self._configuration["request_fingerprint"]
        ):
            raise SourceRunFailure(
                "invalid_inputs", "Source request changed after preparation"
            )

        if (
            self.model != self._configuration["model"]
            or self.worker_timeout_seconds
            != self._configuration["worker_timeout_seconds"]
            or self.validation_timeout_seconds
            != self._configuration["validation_timeout_seconds"]
            or self.reader_limits.digest != self._configuration["limits_digest"]
            or {name: p.to_data() for name, p in self.profiles.items()}
            != self._configuration["profiles"]
        ):
            raise SourceRunFailure(
                "invalid_inputs", "Trusted run configuration changed"
            )
        for relative, expected in self.readonly_support_files.items():
            target = self.workspace_root / relative
            if (
                target.is_symlink()
                or not target.is_file()
                or _digest(target) != expected
            ):
                raise SourceRunFailure(
                    "invalid_inputs", "Read-only support files changed during the run"
                )

    def authorize(
        self,
        request: ImplementationRequest,
        *,
        provider: object,
        worktree_root: Path | None,
    ) -> None:
        self.verify_inputs()
        if (
            type(provider) is not MiniSWEAgentProvider
            or request.to_data() != self.request.to_data()
            or worktree_root is None
            or worktree_root.resolve() != self.workspace_root
        ):
            raise ValueError("Source request does not match its prepared MiniSWE run")
        assert isinstance(provider, MiniSWEAgentProvider)
        if self._invoked or provider.session_trajectory is not None:
            raise ValueError(
                "A source implementation run permits one invocation and no continuation"
            )
        if (
            provider.source_reference_mode != "enforce"
            or provider.source_reference_catalog != self.catalog
            or provider.diagnostics_root != self.private_root
            or provider.source_reader_limits != self.reader_limits
            or provider.model != self.model
            or provider.timeout_seconds != self.worker_timeout_seconds
            or provider.prompt_prefix
            or provider.prompt_suffix
            or provider.write_denied_paths
        ):
            raise ValueError(
                "Provider configuration differs from the sealed source run"
            )
        staged_paths = set(
            _paths_output(
                self.workspace_root, "diff", "--cached", "--name-only"
            ).splitlines()
        )
        unstaged_paths = set(
            _paths_output(self.workspace_root, "diff", "--name-only").splitlines()
        )
        untracked_paths = set(
            _paths_output(
                self.workspace_root,
                "ls-files",
                "--others",
                "--exclude-standard",
            ).splitlines()
        )
        expected_support_paths = set(self.readonly_support_files)
        if (
            _git(self.workspace_root, "rev-parse", "HEAD").decode().strip()
            != self.catalog.base_commit
            or _working_paths(self.workspace_root, output=_paths_output)
            != expected_support_paths
            or staged_paths != expected_support_paths
            or unstaged_paths
            or untracked_paths
        ):
            raise ValueError(
                "Source implementation must start from its clean exact base"
            )

    def claim_invocation(self, request: ImplementationRequest, attempt_id: str) -> None:
        if (
            self._invoked
            or request.to_data() != self.request.to_data()
            or attempt_id != self.run_id
        ):
            raise ValueError(
                "Source invocation identity was already used or does not match"
            )
        self.verify_inputs()
        _write_once(
            self.private_root / "invocation.json",
            {
                "run_id": self.run_id,
                "inputs": self.inputs,
                "prompt_fingerprint": self.handoff.worker_prompt.to_data()[
                    "fingerprint"
                ],
            },
        )
        self._invoked = True

    def _sandbox_profile(self) -> str:
        common = Path(
            _git(self.workspace_root, "rev-parse", "--git-common-dir").decode().strip()
        )
        gitdir = Path(
            _git(self.workspace_root, "rev-parse", "--git-dir").decode().strip()
        )
        denied = {
            str(
                (self.workspace_root / p).resolve()
                if not p.is_absolute()
                else p.resolve()
            )
            for p in (common, gitdir)
        }
        rules = [
            "(version 1)",
            "(allow default)",
            f"(deny file-read* (subpath {json.dumps(str(self.private_root))}))",
            "(deny file-write* (require-all "
            f"(require-not (subpath {json.dumps(str(self.workspace_root))})) "
            f"(require-not (subpath {json.dumps(str(self.public_root))}))))",
            "(deny file-write* (literal "
            + json.dumps(str(self.workspace_root / ".git"))
            + "))",
        ]
        rules.extend(
            f"(deny file-write* (subpath {json.dumps(path)}))"
            for path in sorted(denied)
        )
        rules.extend(
            "(deny file-write* (literal "
            + json.dumps(str(self.workspace_root / path))
            + "))"
            for path in self.readonly_support_files
        )
        return "\n".join(rules) + "\n"

    def protect_command(self, command: tuple[str, ...]) -> tuple[str, ...]:
        self.verify_inputs()
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
            return ("/usr/bin/sandbox-exec", "-f", str(self.profile_path), *command)
        bwrap = Path("/usr/bin/bwrap")
        if sys.platform.startswith("linux") and bwrap.is_file():
            return (
                str(bwrap),
                "--die-with-parent",
                "--new-session",
                "--unshare-user",
                "--unshare-pid",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "--bind",
                str(self.workspace_root),
                str(self.workspace_root),
                *[
                    argument
                    for path in self.readonly_support_files
                    for argument in (
                        "--ro-bind",
                        str(self.workspace_root / path),
                        str(self.workspace_root / path),
                    )
                ],
                "--bind",
                str(self.public_root),
                str(self.public_root),
                "--tmpfs",
                str(self.private_root),
                "--",
                *command,
            )
        raise ValueError(
            "Source execution requires macOS Seatbelt or Linux bubblewrap; "
            "no unprotected fallback is permitted"
        )

    @staticmethod
    def _isolation_name() -> str:
        if sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file():
            return "macos-seatbelt"
        if sys.platform.startswith("linux") and Path("/usr/bin/bwrap").is_file():
            return "linux-bubblewrap"
        return "unsupported"

    def _probe_protection(self) -> None:
        code = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "q=pathlib.Path(sys.argv[2]);\n"
            "for mode in ('rb','r+b'):\n"
            " try:\n  f=p.open(mode); f.close(); sys.exit(11)\n"
            " except (PermissionError, FileNotFoundError): pass\n"
            "q.write_text('probe'); q.unlink()\n"
        )
        result = subprocess.run(
            self.protect_command(
                (
                    sys.executable,
                    "-c",
                    code,
                    str(self.private_root / "catalog.json"),
                    str(self.public_root / "probe"),
                )
            ),
            cwd=self.workspace_root,
            env=os.environ | self.process_environment(),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            raise ValueError(
                "OS source-input protection could not be established: "
                + result.stderr[:500]
            )

    def process_environment(self) -> dict[str, str]:
        return {
            "TMPDIR": str(self.public_root / "tmp"),
            "XDG_CACHE_HOME": str(self.public_root / "cache"),
            "XDG_CONFIG_HOME": str(self.public_root / "config"),
            "XDG_DATA_HOME": str(self.public_root / "data"),
            "XDG_STATE_HOME": str(self.public_root / "state"),
            "PYTHONPYCACHEPREFIX": str(self.public_root / "pycache"),
            "UV_CACHE_DIR": str(self.public_root / "cache" / "uv"),
            "GIT_OPTIONAL_LOCKS": "0",
            "COVERAGE_FILE": str(self.public_root / "tmp" / ".coverage"),
            "COV_CORE_DATAFILE": str(self.public_root / "tmp" / ".coverage"),
        }

    def validation_environment(self) -> dict[str, str]:
        paths = [
            self.workspace_root / "src",
            *sorted((self.workspace_root / "packages").glob("*/src")),
            self.workspace_root,
        ]
        return self.process_environment() | {
            "PYTHONPATH": os.pathsep.join(
                str(p) for p in paths if p.resolve().is_relative_to(self.workspace_root)
            )
        }

    def check(self) -> ReferenceValidationReport:
        self.verify_inputs()
        return self.validation_context.check(self.workspace_root)

    def _candidate_state(self) -> str:
        head = _git(self.workspace_root, "rev-parse", "HEAD").decode().strip()
        paths = _committed_paths(
            self.workspace_root, self.catalog.base_commit, head, output=_paths_output
        ) | _working_paths(self.workspace_root, output=_paths_output)
        values: dict[str, Any] = {}
        for relative in sorted(paths):
            target = self.workspace_root / relative
            if any(
                parent.is_symlink()
                for parent in target.parents
                if parent != self.workspace_root
                and parent.is_relative_to(self.workspace_root)
            ):
                raise SourceRunFailure(
                    "invalid_candidate",
                    "A candidate path traverses a symbolic-link directory",
                )
            values[relative] = (
                {"link": os.readlink(target)}
                if target.is_symlink()
                else {"sha256": _digest(target), "mode": target.stat().st_mode}
                if target.is_file()
                else None
            )
        return content_fingerprint({"head": head, "paths": values})

    def finalize_worker(self, attempt: CodingAgentAttempt) -> None:
        self._last_attempt = attempt
        self.verify_inputs()
        if attempt.status is not CodingAgentStatus.COMPLETED:
            return
        if attempt.worker_committed or attempt.supporting_edit_paths:
            raise SourceRunFailure(
                "policy_violation",
                "Source workers may not commit or expand the accepted path scope",
            )
        if (
            classify_coding_agent_attempt(attempt)
            is not CodingAgentOutcome.COMPLETED_WITH_PROGRESS
        ):
            raise SourceRunFailure(
                "submitted_without_progress",
                "Source worker submitted without attributable durable progress",
            )
        trajectory = json.loads(
            (self.public_root / f"{self.run_id}.traj.json").read_text()
        )
        messages = trajectory.get("messages", [])
        if (
            not messages
            or messages[-1].get("extra", {}).get("exit_status") != "Submitted"
        ):
            raise SourceRunFailure(
                "invalid_submission",
                "Source worker did not produce a submitted trajectory",
            )
        try:
            completion = json.loads(messages[-1]["content"])
        except (ValueError, TypeError) as error:
            raise SourceRunFailure(
                "invalid_submission",
                "Source completion must use the compiled JSON protocol",
            ) from error
        if (
            not isinstance(completion, dict)
            or set(completion) != {"schema_version", "reference_owners", "design_gaps"}
            or completion["schema_version"] != "structrr-reference-completion-v1"
        ):
            raise SourceRunFailure(
                "invalid_submission",
                "Source completion schema does not match the immutable prompt",
            )
        claims, gaps = completion["reference_owners"], completion["design_gaps"]
        if not isinstance(claims, list) or any(
            not isinstance(c, dict)
            or set(c) != {"ref", "path", "kind", "qualified_name"}
            or any(not isinstance(v, str) or not v.strip() for v in c.values())
            for c in claims
        ):
            raise SourceRunFailure(
                "invalid_submission", "Source owner claims are malformed"
            )
        if not isinstance(gaps, list) or any(
            not isinstance(g, dict)
            or set(g) != {"location", "consequence"}
            or any(not isinstance(v, str) or not v.strip() for v in g.values())
            for g in gaps
        ):
            raise SourceRunFailure(
                "invalid_submission", "Source design gaps are malformed"
            )
        self.completion, self.claims = completion, tuple(claims)
        if gaps:
            raise SourceRunFailure(
                "incomplete_design",
                "The worker reported design gaps; a new design revision is required",
            )
        events_path = self.private_root / f"{self.run_id}.source-events.ndjson"
        events = (
            [json.loads(line) for line in events_path.read_text().splitlines()]
            if events_path.exists()
            else []
        )
        refs = {
            o["ref"]
            for o in self.handoff.worker_prompt.to_data()["contract"]["obligations"]
        }
        self.delivery = context_delivery_status(events, refs, self.catalog.digest)
        if not self.delivery["complete"]:
            raise SourceRunFailure(
                "incomplete_context",
                "Required context was not completely delivered by verified reads",
            )
        self._candidate_after_worker = self._candidate_state()

    def verify_candidate(self) -> None:
        """Require the exact post-worker bytes when handing off an accepted patch."""
        self.verify_inputs()
        if (
            self._candidate_after_worker is None
            or self._candidate_after_worker != self._candidate_state()
        ):
            raise SourceRunFailure(
                "invalid_candidate", "Candidate changed after worker validation"
            )

    def execute(
        self,
        inference_handler: Callable[[ChatCompletionRequest], ChatCompletionResponse],
    ) -> SourceReferenceRunResult:
        if self._executed:
            raise ValueError(
                "Source run identities cannot be executed or resumed twice"
            )
        self._executed = True
        result: dict[str, Any] = {
            "schema_version": POLICY,
            "run_id": self.run_id,
            "inputs": self.inputs,
            "mode": "enforce",
            "outcome": "failed",
            "error": None,
            "attempt": None,
            "validation": None,
            "completion": None,
            "context_delivery": None,
        }
        try:
            self.verify_inputs()
            provider = MiniSWEAgentProvider(
                inference_handler=inference_handler,
                model=self.model,
                timeout_seconds=self.worker_timeout_seconds,
                diagnostics_root=self.private_root,
                source_reference_mode="enforce",
                source_reference_catalog=self.catalog,
                source_reader_limits=self.reader_limits,
                source_reference_execution=self,
            )
            execution = execute_coding_agent_attempt(
                CodingAgentRunner(
                    provider, CodingAgentAttemptStore(self.private_root / "agent")
                ),
                self.request,
                worktree_root=self.workspace_root,
                attempt_id=self.run_id,
                profiles=self.profiles,
                validation_timeout_seconds=self.validation_timeout_seconds,
            )
            self.verify_inputs()
            result["attempt"], result["validation"] = (
                execution.attempt.to_data(),
                execution.validation.to_data(),
            )
            if execution.attempt.status is not CodingAgentStatus.COMPLETED:
                result["outcome"] = classify_coding_agent_attempt(
                    execution.attempt
                ).value
            elif (
                execution.validation.status is not ValidationReportStatus.PASSED
                or execution.diff_check.returncode
            ):
                result["outcome"] = "validation_failed"
            elif self._candidate_after_worker != self._candidate_state():
                result["outcome"] = "invalid_candidate"
                result["error"] = (
                    "Validation commands changed the post-worker candidate"
                )
            else:
                result["outcome"] = "accepted"
        except Exception as error:
            result["outcome"] = (
                error.outcome if isinstance(error, SourceRunFailure) else "failed"
            )
            result["error"] = str(error)
        finally:
            result["attempt"] = result["attempt"] or (
                self._last_attempt.to_data() if self._last_attempt else None
            )
            result["completion"], result["context_delivery"] = (
                self.completion,
                self.delivery,
            )
            result["fingerprint"] = content_fingerprint(result)
            _write_once(self.private_root / "terminal.json", result)
        return SourceReferenceRunResult(
            json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False)
        )
