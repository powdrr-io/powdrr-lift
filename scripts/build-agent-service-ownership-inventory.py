#!/usr/bin/env python3
"""Write the deterministic PR 1 inventory of source and product resources."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from pathlib import Path
from typing import Any


def _owner(path: str) -> tuple[str, str, str]:
    """Assign a first-pass owner and extraction step for one tracked path."""
    if path.startswith("packages/service/src/powdrr_service/structrr/"):
        existing_prs = {
            f"packages/service/src/powdrr_service/structrr/{module}.py": pr
            for module, pr in {
                "active_intent": "1228",
                "obligation_evidence": "1234",
                "rebase": "1224",
                "snapshot": "1220",
                "validation_models": "1233",
                "verification_health": "1230",
            }.items()
        }
        if path in existing_prs:
            return (
                "service",
                "service-owned Structrr policy and artifact semantics",
                existing_prs[path],
            )
        if path.endswith(
            (
                "/proposal.py",
                "/actual_diff.py",
                "/candidate_comparison.py",
                "/source_manifest.py",
            )
        ):
            return (
                "service",
                "Structrr proposal, source-manifest, and candidate-comparison policy",
                "8",
            )
        if path.endswith(
            (
                "/verification_contract.py",
                "/verification_obligations.py",
                "/gate_compiler.py",
                "/proposal_review.py",
                "/intent_lineage.py",
                "/invariant_review.py",
            )
        ):
            return (
                "service",
                "Structrr verification, proposal-review, and invariant-review policy",
                "8",
            )
        return "service", "service-owned Structrr policy and artifact semantics", "8"
    if path.startswith("src/powdrr_lift/workrr/planning/"):
        if path.endswith(("/source.py", "/evidence.py", "/driver.py")):
            return "split", "service policy; agent evidence collection", "4"
        return (
            "service",
            "bounded planning, question, dependency, or repair policy",
            "4",
        )
    if path.startswith("packages/service/src/powdrr_service/evaluations/"):
        return "service", "service-owned prompt and benchmark evaluation policy", "8"
    if path.startswith("packages/service/src/powdrr_service/planning/"):
        if path.endswith(("/classifier_input.py", "/semantic_contract_compiler.py")):
            return (
                "service",
                "service-owned semantic instruction classification and compilation",
                "8",
            )
        return "service", "service-owned planning policy", "8"
    if path.startswith("packages/service/src/powdrr_service/changelog/"):
        return (
            "service",
            "service-owned changelog parsing, rendering, and validation policy",
            "8",
        )
    if path.startswith("packages/service/src/powdrr_service/specifications/"):
        if path.endswith("/architecture.py"):
            return (
                "service",
                "architecture-specification validation policy over supplied "
                "YAML evidence",
                "8",
            )
        if path.endswith("/common.py"):
            return (
                "service",
                "shared specification-v1 normalization and validation rules",
                "8",
            )
        if path.endswith("/implementation.py"):
            return (
                "service",
                "implementation-specification validation over supplied "
                "architecture evidence",
                "8",
            )
        return (
            "service",
            "service-owned structured specification validation policy",
            "8",
        )
    if path.startswith("packages/service/src/powdrr_service/resources/evaluations/"):
        return "service", "bundled private evaluation rubric", "8"
    if path.startswith(
        "packages/service/src/powdrr_service/resources/processes/"
    ) or path.startswith("src/powdrr_lift/process/"):
        return "service", "private process-language implementation and resources", "8"
    if path.startswith("packages/service/src/powdrr_service/procedrr"):
        return "service", "private process-language runtime", "8"
    if path.startswith("src/powdrr_lift/structrr/"):
        return (
            "split",
            "service semantics; agent supplies pinned repository evidence",
            "3",
        )
    if path.startswith("src/powdrr_lift/core/"):
        if path.endswith("/architecture_specification.py"):
            return (
                "split",
                "agent collects system-specification evidence and handles "
                "template/file I/O; service validates architecture content",
                "8",
            )
        if path.endswith("/specification_v1.py"):
            return (
                "split",
                "agent handles local file I/O; service owns specification-v1 "
                "content normalization",
                "8",
            )
        if path.endswith("/implementation_specification.py"):
            return (
                "split",
                "agent collects architecture-specification evidence and handles "
                "template/file I/O; service validates implementation content",
                "8",
            )
        return (
            "split",
            "classify each symbol as service policy or public exchanged data",
            "3/8",
        )
    if path.startswith("src/powdrr_lift/execution/"):
        return "split", "agent local effects versus service process decisions", "3/7"
    if path.startswith("src/powdrr_lift/workrr/"):
        if path.endswith(("/coding_agent.py", "/minisweagent_monitor.py")):
            return (
                "agent",
                "local MiniSWE process, session, and execution monitoring",
                "6",
            )
        if path.endswith(("/procedrr_chat.py", "/chat_tui.py", "/chat_io.py")):
            return "split", "agent user interaction; service interview policy", "5"
        if path.endswith(
            (
                "/providers.py",
                "/provider_config.py",
                "/provider_runtime.py",
                "/jev_classifier.py",
                "/semantic_classifier.py",
                "/local_model.py",
                "/llm.py",
            )
        ):
            return (
                "service",
                "provider access, model routing, and inference policy",
                "6",
            )
        if path.endswith(
            (
                "/github_issue_queue.py",
                "/github_issue_plan_generation.py",
                "/plan_sizing.py",
                "/multipr_structrr.py",
            )
        ):
            return (
                "split",
                "service planning and GitHub policy; agent local Git effects",
                "4/7",
            )
        if path.endswith(
            (
                "/postrun_probes.py",
                "/probe_repair.py",
                "/execute_pr_tests.py",
                "/differential_verification.py",
                "/verification_evidence.py",
                "/verification_provider.py",
            )
        ):
            return (
                "split",
                "service probe/verification policy; agent runs local commands",
                "7",
            )
        if path.endswith("/external_contract_research.py"):
            return (
                "service",
                "external search, source interpretation, and API credentials",
                "6",
            )
        if path.endswith(("/git.py", "/command_catalog.py", "/paths.py")):
            return "agent", "local repository and command capabilities", "3"
        return (
            "split",
            "separate service decisions from agent I/O and orchestration",
            "5/7",
        )
    if path.startswith("src/powdrr_lift/integrations/harbor/"):
        return "agent", "benchmark harness and local repository adapter", "7"
    if path.startswith("src/powdrr_lift/") and path.endswith(
        (
            "/intrinsic_git_gh.py",
            "/basedpyright_tools.py",
            "/file_management.py",
            "/intrinsic_edit.py",
            "/agent_bootstrap.py",
            "/agent_runtime.py",
            "/minisweagent_monitor.py",
            "/minisweagent_session.py",
            "/opencode_monitor.py",
        )
    ):
        return (
            "agent",
            "local UI, filesystem, process, Git, or analysis capability",
            "3/6",
        )
    if path.startswith("src/powdrr_lift/") and path.endswith(
        ("/providers.py", "/openai_proxy.py")
    ):
        return (
            "service",
            "provider integration; diagnostic proxy is development-only",
            "6",
        )
    if path.startswith("src/powdrr_lift/resources/"):
        return "service", "Powdrr-owned taxonomy and semantic policy resource", "8"
    if path.startswith("src/powdrr_lift/") and path.endswith(
        ("/cli.py", "/mcp_server.py", "/chat_tui.py", "/blame_ui.py")
    ):
        return (
            "split",
            "agent entry point and UI composed by the local application",
            "5/8",
        )
    if path.startswith("src/powdrr_lift/"):
        return "split", "classify public transport types versus private policy", "3/8"
    if path.startswith("scripts/"):
        return "agent", "local development, repository, or verification harness", "8"
    if path.startswith("docs/procedrr/"):
        return "service", "Powdrr-owned process catalog, templates, and policy", "8"
    if path.startswith("docs/"):
        return (
            "review",
            "classify as private policy, public protocol, or user-facing docs",
            "1/8",
        )
    return "review", "assign explicitly during inventory review", "1"


def _internal_imports(source: str, module: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(("powdrr_lift", "powdrr_service", "procedrr")):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(("powdrr_lift", "powdrr_service", "procedrr")):
                found.add(node.module)
            elif node.level:
                package = module.rpartition(".")[0]
                segments = package.split(".")
                keep = max(0, len(segments) - node.level + 1)
                relative = ".".join((*segments[:keep], node.module))
                if relative.startswith(("powdrr_lift", "powdrr_service", "procedrr")):
                    found.add(relative)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    tracked = (
        subprocess.run(
            [
                "git",
                "ls-files",
                "-z",
                "--",
                "src/powdrr_lift",
                "packages/service/src/powdrr_service/evaluations",
                "packages/service/src/powdrr_service/planning",
                "packages/service/src/powdrr_service/changelog",
                "packages/service/src/powdrr_service/specifications",
                "packages/service/src/powdrr_service/procedrr",
                "packages/service/src/powdrr_service/procedrr_evaluator",
                "packages/service/src/powdrr_service/structrr",
                "packages/service/src/powdrr_service/resources/evaluations",
                "packages/service/src/powdrr_service/resources/processes",
                "docs/procedrr",
                "scripts",
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    paths = sorted(
        path
        for path in tracked
        if path
        and (
            path.endswith(".py")
            or path.startswith(
                (
                    "packages/service/src/powdrr_service/resources/evaluations/",
                    "packages/service/src/powdrr_service/resources/processes/",
                    "docs/procedrr/",
                    "src/powdrr_lift/resources/",
                    "scripts/",
                )
            )
        )
    )
    entries: list[dict[str, Any]] = []
    for relative in paths:
        path = root / relative
        owner, rationale, extraction_pr = _owner(relative)
        module = relative.removeprefix("src/").removesuffix(".py").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        imports = (
            _internal_imports(path.read_text(encoding="utf-8"), module)
            if relative.endswith(".py")
            else []
        )
        entries.append(
            {
                "path": relative,
                "kind": "python_module" if relative.endswith(".py") else "resource",
                "owner": owner,
                "rationale": rationale,
                "extraction_pr": extraction_pr,
                "internal_imports": imports,
                "reviewed": False,
            }
        )
    payload = {
        "schema_version": 1,
        "baseline_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        "inventory_scope": [
            "tracked first-party Python source",
            "tracked Powdrr process resources",
            "tracked scripts",
        ],
        "review_rule": (
            "Every entry is a first-pass classification. Review owner, rationale, "
            "and extraction PR before marking reviewed true."
        ),
        "entries": entries,
    }
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {len(entries)} ownership entries to {output.relative_to(root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
