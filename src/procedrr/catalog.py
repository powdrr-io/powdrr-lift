"""Validated discovery and identity for Procedrr process definitions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from procedrr.parser import parse_and_validate


class ProcessCatalogError(ValueError):
    """A process definition could not be discovered or its inputs are invalid."""


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Stable identity for one exact, validated process revision."""

    process_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ProcessDefinition:
    """A validated Procedrr document and its source identity."""

    identity: ProcessIdentity
    path: Path
    document: Mapping[str, Any]

    @property
    def name(self) -> str:
        return self.identity.process_id

    def validate_inputs(self, bindings: Mapping[str, Any]) -> dict[str, Any]:
        """Validate caller bindings against the process input declarations."""
        declarations = self.document.get("inputs", [])
        if not isinstance(declarations, list):
            raise ProcessCatalogError(f"{self.name}: inputs must be a list")

        schemas: dict[str, dict[str, Any]] = {}
        required: set[str] = set()
        for declaration in declarations:
            if not isinstance(declaration, Mapping):
                raise ProcessCatalogError(f"{self.name}: malformed input declaration")
            name = declaration.get("name")
            if not isinstance(name, str) or not name:
                raise ProcessCatalogError(f"{self.name}: input name must be non-empty")
            schema = declaration.get("schema")
            if not isinstance(schema, Mapping):
                schema = {"type": declaration.get("type", "string")}
                if "enum" in declaration:
                    schema["enum"] = declaration["enum"]
            schemas[name] = dict(schema)
            if declaration.get("required") is True:
                required.add(name)

        supplied = set(bindings)
        unknown = supplied - schemas.keys()
        missing = required - supplied
        if unknown or missing:
            details = []
            if unknown:
                details.append(f"undeclared inputs: {', '.join(sorted(unknown))}")
            if missing:
                details.append(f"missing required inputs: {', '.join(sorted(missing))}")
            raise ProcessCatalogError(f"{self.name}: {'; '.join(details)}")

        validated = dict(bindings)
        for name, value in validated.items():
            errors = sorted(
                Draft202012Validator(schemas[name]).iter_errors(value),
                key=lambda error: list(error.absolute_path),
            )
            if errors:
                raise ProcessCatalogError(
                    f"{self.name}: invalid input {name!r}: {errors[0].message}"
                )
        return validated


class ProcessCatalog:
    """Load only validated Procedrr documents from an explicit directory."""

    def __init__(self, processes: Mapping[str, ProcessDefinition]) -> None:
        self._processes = dict(processes)

    @classmethod
    def from_document(
        cls,
        path: Path,
        document: Mapping[str, Any],
    ) -> ProcessCatalog:
        """Build a catalog from a document already validated by the caller."""
        name = document.get("name")
        if not isinstance(name, str) or not name:
            raise ProcessCatalogError(f"{path}: process name must be non-empty")
        if name != path.stem:
            raise ProcessCatalogError(
                f"{path}: process name {name!r} must match the filename"
            )
        fingerprint = _document_fingerprint(document)
        return cls(
            {
                name: ProcessDefinition(
                    ProcessIdentity(name, fingerprint), path, dict(document)
                )
            }
        )

    @classmethod
    def load(
        cls,
        directory: Path,
        *,
        command_catalog: Any | None = None,
    ) -> ProcessCatalog:
        """Load YAML/JSON processes without consulting the legacy skill tree."""
        root = directory.expanduser().resolve()
        if not root.is_dir():
            raise ProcessCatalogError(
                f"Procedrr process directory does not exist: {root}"
            )

        processes: dict[str, ProcessDefinition] = {}
        paths = sorted(
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() in {".yaml", ".yml", ".json"}
        )
        for path in paths:
            try:
                source = path.read_text(encoding="utf-8")
                document = parse_and_validate(source, command_catalog=command_catalog)
            except (OSError, ValueError) as error:
                raise ProcessCatalogError(f"{path}: {error}") from error
            name = document.get("name")
            if not isinstance(name, str) or not name:
                raise ProcessCatalogError(f"{path}: process name must be non-empty")
            if name != path.stem:
                raise ProcessCatalogError(
                    f"{path}: process name {name!r} must match the filename"
                )
            fingerprint = _document_fingerprint(document)
            if name in processes:
                raise ProcessCatalogError(f"duplicate Procedrr process name: {name}")
            processes[name] = ProcessDefinition(
                ProcessIdentity(name, fingerprint), path, document
            )
        return cls(processes)

    def get(self, process_id: str) -> ProcessDefinition:
        """Resolve a declared process by its stable name."""
        try:
            return self._processes[process_id]
        except KeyError as error:
            raise ProcessCatalogError(
                f"unknown Procedrr process: {process_id!r}"
            ) from error

    def list(self) -> tuple[ProcessDefinition, ...]:
        """Return processes in stable name order."""
        return tuple(self._processes[name] for name in sorted(self._processes))


def _document_fingerprint(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "ProcessCatalog",
    "ProcessCatalogError",
    "ProcessDefinition",
    "ProcessIdentity",
]
