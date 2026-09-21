"""Typed catalogs for deterministic Procedrr tool commands."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as validate_json


class CommandCatalogError(ValueError):
    """Raised when a command or its parameters violate its catalog contract."""


CommandLogic = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """The complete contract for one deterministic Procedrr command."""

    name: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    logic: CommandLogic | None = field(default=None, repr=False, compare=False)

    def validate_input(self, parameters: Mapping[str, Any]) -> None:
        try:
            validate_json(dict(parameters), dict(self.input_schema))
        except JsonSchemaError as exc:
            raise CommandCatalogError(
                f"command {self.name!r} parameters do not match its schema: "
                f"{exc.message}"
            ) from exc

    def static_input_errors(self, parameters: Mapping[str, Any]) -> tuple[str, ...]:
        """Validate command shape while allowing references as typed values."""
        schema = self.input_schema
        properties = schema.get("properties", {})
        required = schema.get("required", ())
        errors: list[str] = []
        missing = [name for name in required if name not in parameters]
        if missing:
            errors.append("missing required parameters: " + ", ".join(missing))
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(parameters) - set(properties))
            if unknown:
                errors.append("unknown parameters: " + ", ".join(unknown))
        for name, value in parameters.items():
            if _is_reference(value):
                continue
            child_schema = properties.get(name)
            if not isinstance(child_schema, Mapping):
                continue
            try:
                validate_json(value, dict(child_schema))
            except JsonSchemaError as exc:
                errors.append(f"parameter {name!r}: {exc.message}")
        return tuple(errors)

    def dispatch(self, parameters: Mapping[str, Any]) -> Any:
        if self.logic is None:
            raise CommandCatalogError(f"command {self.name!r} has no dispatcher")
        self.validate_input(parameters)
        result = self.logic(parameters)
        try:
            validate_json(result, dict(self.output_schema))
        except JsonSchemaError as exc:
            raise CommandCatalogError(
                f"command {self.name!r} returned an invalid result: {exc.message}"
            ) from exc
        return result


class CommandCatalog:
    """Immutable-by-convention registry used by static and runtime Procedrr paths."""

    def __init__(self, specs: tuple[CommandSpec, ...] = ()) -> None:
        names = [spec.name for spec in specs]
        if len(names) != len(set(names)):
            raise CommandCatalogError("command names must be unique")
        self._specs = {spec.name: spec for spec in specs}

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._specs))

    def get(self, name: str) -> CommandSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> CommandSpec:
        spec = self.get(name)
        if spec is None:
            raise CommandCatalogError(f"unknown Procedrr command: {name}")
        return spec

    def dispatch(self, name: str, parameters: Mapping[str, Any]) -> Any:
        return self.require(name).dispatch(parameters)

    def with_logic(self, handlers: Mapping[str, CommandLogic]) -> CommandCatalog:
        """Return a catalog with the registered implementations attached."""
        return CommandCatalog(
            tuple(
                CommandSpec(
                    spec.name,
                    spec.input_schema,
                    spec.output_schema,
                    handlers.get(spec.name),
                )
                for spec in self._specs.values()
            )
        )


def object_schema(
    properties: Mapping[str, Mapping[str, Any]],
    *,
    required: tuple[str, ...] = (),
    additional_properties: bool = False,
) -> dict[str, Any]:
    """Build the deliberately small object schemas used by command specs."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": additional_properties,
    }


def _is_reference(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("type") == "reference"
        and isinstance(value.get("value"), str)
    )


ANY_OBJECT = object_schema({}, additional_properties=True)
STRING = {"type": "string"}
OBJECT = {"type": "object"}
ARRAY = {"type": "array"}


__all__ = [
    "ANY_OBJECT",
    "ARRAY",
    "CommandCatalog",
    "CommandCatalogError",
    "CommandSpec",
    "OBJECT",
    "STRING",
    "object_schema",
]
