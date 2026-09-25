"""Compact, compiler-owned context packets for code-editing workers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


def _worker_objective(text: str) -> str:
    """Remove source-level repository workflow instructions from the objective."""
    # Branching, committing, and PR instructions belong to Workrr. They are
    # frequently present in task descriptions, but must not compete with the
    # worker policy rendered by the request compiler.
    objective, separator, _process_instructions = text.partition("\nIMPORTANT:")
    return (objective if separator else text).strip()


@dataclass(frozen=True, slots=True)
class RepositoryContextPacket:
    """Stable repository facts supplied to a coding worker."""

    allowed_paths: tuple[str, ...]
    validation_profiles: tuple[str, ...]
    existing_tests: tuple[Mapping[str, Any], ...] = ()

    def to_data(self) -> dict[str, Any]:
        return {
            "allowed_paths": list(self.allowed_paths),
            "validation_profiles": list(self.validation_profiles),
            "existing_tests": [dict(item) for item in self.existing_tests],
        }


@dataclass(frozen=True, slots=True)
class ImplementationPacket:
    """Minimal semantic handoff; structural identity stays with Powdrr."""

    objective: str
    obligations: tuple[str, ...]
    required_tests: tuple[Mapping[str, Any], ...]
    repository: RepositoryContextPacket

    def for_obligation(self, ordinal: int) -> ImplementationPacket:
        """Return the smallest packet needed for one implementation turn."""
        if ordinal < 1 or ordinal > len(self.obligations):
            raise ValueError(f"obligation ordinal out of range: {ordinal}")
        index = ordinal - 1
        required_tests = (
            (self.required_tests[index],) if index < len(self.required_tests) else ()
        )
        return ImplementationPacket(
            objective=self.objective,
            obligations=(self.obligations[index],),
            required_tests=required_tests,
            repository=self.repository,
        )

    def for_task(
        self,
        *,
        objective: str,
        acceptance_criteria: Sequence[str],
    ) -> ImplementationPacket:
        """Return a worker packet scoped to one compiled code task."""
        task_objective = _worker_objective(objective).strip()
        if not task_objective:
            raise ValueError("task packet objective must not be empty")
        tests = tuple(
            {"description": criterion.strip()}
            for criterion in acceptance_criteria
            if isinstance(criterion, str) and criterion.strip()
        )
        if not tests:
            raise ValueError("task packet requires acceptance criteria")
        return ImplementationPacket(
            objective=task_objective,
            obligations=(task_objective,),
            required_tests=tests,
            repository=self.repository,
        )

    def to_data(self) -> dict[str, Any]:
        return {
            "schema_version": "implementation-packet-v1",
            "objective": self.objective,
            "obligations": [
                {"ordinal": index, "description": description}
                for index, description in enumerate(self.obligations, start=1)
            ],
            "required_tests": [
                {
                    "ordinal": index,
                    "description": str(item.get("description", "")),
                }
                for index, item in enumerate(self.required_tests, start=1)
            ],
            "repository": self.repository.to_data(),
        }

    @classmethod
    def from_data(cls, raw: Mapping[str, Any]) -> ImplementationPacket:
        if raw.get("schema_version") != "implementation-packet-v1":
            raise ValueError("unsupported implementation packet schema")
        raw_obligations = raw.get("obligations")
        raw_tests = raw.get("required_tests")
        repository = raw.get("repository")
        if not isinstance(raw_obligations, list) or not isinstance(raw_tests, list):
            raise ValueError("implementation packet is incomplete")
        if not isinstance(repository, Mapping):
            raise ValueError("implementation packet repository context is missing")
        obligations = tuple(
            str(item["description"])
            for item in raw_obligations
            if isinstance(item, Mapping) and isinstance(item.get("description"), str)
        )
        tests = tuple(
            {"description": item.get("description", "")}
            for item in raw_tests
            if isinstance(item, Mapping)
        )
        existing = repository.get("existing_tests", [])
        if not isinstance(existing, list):
            raise ValueError("implementation packet existing tests are malformed")
        packet = cls(
            objective=_worker_objective(str(raw.get("objective", ""))),
            obligations=obligations,
            required_tests=tests,
            repository=RepositoryContextPacket(
                allowed_paths=tuple(
                    str(item) for item in repository.get("allowed_paths", [])
                ),
                validation_profiles=tuple(
                    str(item) for item in repository.get("validation_profiles", [])
                ),
                existing_tests=tuple(
                    item for item in existing if isinstance(item, Mapping)
                ),
            ),
        )
        if not packet.objective.strip() or not packet.obligations:
            raise ValueError("implementation packet is missing required content")
        return packet

    def render(self) -> str:
        """Render the worker-facing validation contract.

        Obligations, provenance, and structural identity remain compiler-owned
        artifacts. Rendering them here duplicated the same requirements in the
        execution unit, intent packet, and acceptance criteria. The worker only
        needs the executable behavioral contracts that it must make true.
        """
        test_lines = []
        for index, item in enumerate(self.required_tests, start=1):
            description = (
                str(item.get("description", "")).strip().split(" Oracle:", 1)[0]
            )
            test_lines.append(
                f"- T{index:02d} — add a focused test proving {description}"
            )
        test_lines = test_lines or ["- none"]
        return "\n".join(
            (
                "Required behavioral tests:",
                "Make every required behavioral test below pass. These tests "
                "are the executable acceptance contract; do not weaken or "
                "delete them.",
                *test_lines,
                "Run the focused required tests after implementation. If a "
                "test is broken because of an import, fixture, API, assertion, "
                "or other test defect, repair the test and implementation as "
                "needed; never weaken the behavioral assertion.",
            )
        )


def compile_implementation_packet(
    *,
    objective: str,
    obligations: Sequence[str],
    required_tests: Sequence[Mapping[str, Any]],
    allowed_paths: Sequence[str],
    validation_profiles: Sequence[str],
    existing_tests: Sequence[Mapping[str, Any]] = (),
) -> ImplementationPacket:
    """Normalize worker inputs and reject incomplete executable contracts."""
    if not objective.strip():
        raise ValueError("implementation packet objective must not be empty")
    normalized_obligations = tuple(
        item.strip() for item in obligations if isinstance(item, str) and item.strip()
    )
    if not normalized_obligations:
        raise ValueError("implementation packet requires obligations")
    normalized_tests: list[Mapping[str, Any]] = []
    for item in required_tests:
        if not isinstance(item, Mapping):
            raise ValueError("implementation packet test contract is malformed")
        description = item.get("description", "")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("implementation packet test lacks description")
        normalized_tests.append({"description": description.strip()})
    if not normalized_tests:
        raise ValueError("implementation packet requires test contracts")
    return ImplementationPacket(
        objective=_worker_objective(objective),
        obligations=normalized_obligations,
        required_tests=tuple(normalized_tests),
        repository=RepositoryContextPacket(
            allowed_paths=tuple(dict.fromkeys(str(item) for item in allowed_paths)),
            validation_profiles=tuple(
                dict.fromkeys(str(item) for item in validation_profiles)
            ),
            existing_tests=tuple(
                {
                    key: item.get(key)
                    for key in ("provider", "profile", "selector", "fingerprint")
                }
                for item in existing_tests
                if isinstance(item, Mapping) and isinstance(item.get("selector"), str)
            ),
        ),
    )


__all__ = [
    "ImplementationPacket",
    "RepositoryContextPacket",
    "compile_implementation_packet",
]
