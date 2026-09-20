"""Parsing and structural validation for declarative procedrr documents."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from jsonschema import Draft7Validator
from jsonschema.exceptions import SchemaError

KNOWN_TOOLS = frozenset(
    {
        "gather_context",
        "internal",
        "edit",
        "yaml_edit",
        "read_document",
        "invoke_tool",
        "list_files",
        "procedrr_fragment_start",
        "procedrr_fragment_apply_edit",
    }
)
KNOWN_VALIDATORS = frozenset({"json_schema"})
KNOWN_LIMITS = frozenset(
    {
        "llm_activations",
        "tool_calls",
        "max_epochs",
        "context_chars",
        "context_value_chars",
    }
)
_BINDING_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_BINDING = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.-]*)\}")
_BINDING_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")
_MODEL_OWNED_METADATA = re.compile(
    r"(?:^|_)(?:id|uuid|identifier|hash|digest|checksum|fingerprint|version|ref|refs)$"
)


@dataclass(frozen=True, slots=True)
class DocumentDiagnostic:
    path: str
    message: str
    code: str = "invalid_document"
    line: int | None = None
    column: int | None = None

    @property
    def json_pointer(self) -> str:
        """Return the diagnostic path as an RFC 6901-style JSON Pointer."""
        return _path_to_json_pointer(self.path)

    def to_data(self) -> dict[str, Any]:
        """Return a stable machine-readable diagnostic for model correction."""
        result: dict[str, Any] = {
            "code": self.code,
            "path": self.path,
            "json_pointer": self.json_pointer,
            "message": self.message,
        }
        if self.line is not None:
            result["line"] = self.line
        if self.column is not None:
            result["column"] = self.column
        return result


class ParseError(ValueError):
    """The source is not a valid procedrr document."""


def parse_document(source: str, *, source_format: str = "auto") -> dict[str, Any]:
    """Parse a JSON or YAML Procedrr document and reject non-mappings."""
    if source_format not in {"auto", "json", "yaml"}:
        raise ValueError("source_format must be auto, json, or yaml")
    selected_format = source_format
    if selected_format == "auto":
        selected_format = "json" if source.lstrip().startswith(("{", "[")) else "yaml"
    if selected_format == "json":
        try:
            document = json.loads(source)
        except json.JSONDecodeError as exc:
            raise ParseError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        if not isinstance(document, Mapping):
            raise ParseError("a procedrr JSON document must be an object")
        return dict(document)
    try:
        document = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        raise ParseError(str(exc)) from exc
    if not isinstance(document, Mapping):
        raise ParseError("a procedrr document must be a mapping")
    return dict(document)


def validate_document(document: Mapping[str, Any]) -> tuple[DocumentDiagnostic, ...]:
    diagnostics: list[DocumentDiagnostic] = []
    if not isinstance(document.get("name"), str) or not document["name"].strip():
        diagnostics.append(
            DocumentDiagnostic("name", "name must be a non-empty string")
        )
    recoveries = document.get("recoveries", {})
    recovery_names = set(recoveries) if isinstance(recoveries, Mapping) else set()
    limits = document.get("limits")
    if limits is not None:
        if not isinstance(limits, Mapping):
            diagnostics.append(DocumentDiagnostic("limits", "limits must be a mapping"))
        else:
            for name, value in limits.items():
                if not isinstance(name, str):
                    diagnostics.append(
                        DocumentDiagnostic("limits", "limit names must be strings")
                    )
                elif (
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"limits.{name}", "limit values must be positive integers"
                        )
                    )
                elif name not in KNOWN_LIMITS:
                    diagnostics.append(
                        DocumentDiagnostic(f"limits.{name}", f"unknown limit: {name}")
                    )
    outputs = document.get("outputs")
    if outputs is not None:
        if not isinstance(outputs, Mapping) or not outputs:
            diagnostics.append(
                DocumentDiagnostic("outputs", "outputs must be a non-empty mapping")
            )
        else:
            for name, schema in outputs.items():
                if not isinstance(name, str) or not _BINDING_NAME.fullmatch(name):
                    diagnostics.append(
                        DocumentDiagnostic(
                            "outputs", "output names must be valid binding names"
                        )
                    )
                if not isinstance(schema, Mapping):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"outputs.{name}",
                            "output declarations must be JSON Schema objects",
                        )
                    )
                else:
                    try:
                        Draft7Validator.check_schema(dict(schema))
                    except SchemaError as error:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"outputs.{name}",
                                f"invalid output schema: {error.message}",
                            )
                        )
    inputs = document.get("inputs", [])
    if not isinstance(inputs, list):
        diagnostics.append(DocumentDiagnostic("inputs", "inputs must be a list"))
    else:
        input_names: set[str] = set()
        for index, item in enumerate(inputs):
            path = f"inputs[{index}]"
            if not isinstance(item, Mapping):
                diagnostics.append(DocumentDiagnostic(path, "input must be a mapping"))
                continue
            name = item.get("name")
            if not isinstance(name, str) or not _BINDING_NAME.fullmatch(name):
                diagnostics.append(
                    DocumentDiagnostic(f"{path}.name", "input name is invalid")
                )
            elif name in input_names:
                diagnostics.append(
                    DocumentDiagnostic(f"{path}.name", "input name is duplicated")
                )
            else:
                input_names.add(name)
    steps = document.get("steps")
    bindings: set[str] = set()
    if not isinstance(steps, list) or not steps:
        diagnostics.append(
            DocumentDiagnostic("steps", "steps must be a non-empty list")
        )
    else:
        bindings = {
            item["name"]
            for item in document.get("inputs", [])
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        }
        _validate_steps(steps, "steps", diagnostics, bindings, recovery_names)
    if recoveries is not None and not isinstance(recoveries, Mapping):
        diagnostics.append(
            DocumentDiagnostic("recoveries", "recoveries must be a mapping")
        )
    elif isinstance(recoveries, Mapping):
        for name, recovery in recoveries.items():
            if not isinstance(name, str) or not isinstance(recovery, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(
                        "recoveries", "each recovery must be a named mapping"
                    )
                )
            elif not isinstance(recovery.get("steps"), list):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"recoveries.{name}.steps", "recovery steps must be a list"
                    )
                )
            else:
                recovery_input_values = recovery.get("inputs", [])
                if not isinstance(recovery_input_values, list) or not all(
                    isinstance(item, str) and _BINDING_PATH.fullmatch(item)
                    for item in recovery_input_values
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"recoveries.{name}.inputs",
                            "recovery inputs must be a list of binding names",
                        )
                    )
                recovery_inputs = {
                    item for item in recovery_input_values if isinstance(item, str)
                }
                _validate_steps(
                    recovery["steps"],
                    f"recoveries.{name}.steps",
                    diagnostics,
                    set(bindings) | {"failure"} | recovery_inputs,
                    recovery_names,
                )
    return tuple(diagnostics)


def parse_and_validate(source: str, *, source_format: str = "auto") -> dict[str, Any]:
    document = parse_document(source, source_format=source_format)
    diagnostics = validate_document(document)
    if diagnostics:
        detail = "; ".join(f"{item.path}: {item.message}" for item in diagnostics)
        raise ParseError(detail)
    return document


def render_document(document: Mapping[str, Any], *, source_format: str = "json") -> str:
    """Render a Procedrr document in its canonical JSON or YAML source form."""
    if source_format == "json":
        return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    if source_format == "yaml":
        return yaml.safe_dump(dict(document), sort_keys=False)
    raise ValueError("source_format must be json or yaml")


def validate_single_decision(
    document: Mapping[str, Any],
) -> tuple[DocumentDiagnostic, ...]:
    """Report judge outputs that encode multiple model decisions at once."""
    diagnostics: list[DocumentDiagnostic] = []

    def walk(steps: Any, path: str) -> None:
        if not isinstance(steps, list):
            return
        for index, step in enumerate(steps):
            if not isinstance(step, Mapping):
                continue
            step_path = f"{path}[{index}]"
            judge = step.get("judge")
            if isinstance(judge, Mapping):
                output = judge.get("output")
                schema = output.get("schema") if isinstance(output, Mapping) else None
                reasons = _decision_complexity(schema)
                for reason in reasons:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.output.schema",
                            f"not single-decision normal form: {reason}",
                        )
                    )
            for control in (
                "for_each",
                "worklist",
                "call",
                "attempt",
                "repeat",
                "specialize",
            ):
                nested = step.get(control)
                if isinstance(nested, Mapping):
                    walk(
                        nested.get("body", nested.get("steps")),
                        f"{step_path}.{control}",
                    )
            if "branch" in step and isinstance(step["branch"], Mapping):
                branch = step["branch"]
                cases = branch.get("cases", {})
                if isinstance(cases, Mapping):
                    for case, case_steps in cases.items():
                        walk(case_steps, f"{step_path}.branch.cases.{case}")
                walk(branch.get("default"), f"{step_path}.branch.default")

    walk(document.get("steps"), "steps")
    recoveries = document.get("recoveries")
    if isinstance(recoveries, Mapping):
        for name, recovery in recoveries.items():
            if isinstance(recovery, Mapping):
                walk(recovery.get("steps"), f"recoveries.{name}.steps")
    return tuple(diagnostics)


def _validate_steps(
    steps: list[Any],
    path: str,
    diagnostics: list[DocumentDiagnostic],
    bindings: set[str],
    recovery_names: set[Any] | None = None,
) -> None:
    for index, step in enumerate(steps):
        step_path = f"{path}[{index}]"
        if not isinstance(step, Mapping):
            diagnostics.append(DocumentDiagnostic(step_path, "step must be a mapping"))
            continue
        controls = {
            key
            for key in (
                "operation",
                "judge",
                "for_each",
                "worklist",
                "call",
                "terminal",
                "gate",
                "attempt",
                "repeat",
                "branch",
                "specialize",
            )
            if key in step
        }
        if len(controls) != 1:
            diagnostics.append(
                DocumentDiagnostic(step_path, "step must contain exactly one control")
            )
            continue
        control = next(iter(controls))
        _validate_unknown_fields(
            step,
            {control, "id"},
            step_path,
            diagnostics,
        )
        if control == "specialize":
            value = step[control]
            if not isinstance(value, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize", "specialize must be a mapping"
                    )
                )
                continue
            _validate_unknown_fields(
                value,
                {
                    "question",
                    "context",
                    "bind",
                    "process",
                    "max_steps",
                    "allowed_tools",
                },
                f"{step_path}.specialize",
                diagnostics,
            )
            for key in ("question", "context", "bind"):
                if key not in value:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.specialize.{key}",
                            "field is required",
                        )
                    )
            question = value.get("question")
            if not isinstance(question, str) or not question.strip():
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.question",
                        "question must be a non-empty string",
                    )
                )
            context = value.get("context")
            if not isinstance(context, list) or not all(
                isinstance(item, str) for item in context
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.context",
                        "context must be a list of binding names",
                    )
                )
            elif any(item.split(".", 1)[0] not in bindings for item in context):
                for item in context:
                    if item.split(".", 1)[0] not in bindings:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.specialize.context",
                                f"unknown binding: {item}",
                            )
                        )
            if not isinstance(value.get("bind"), str) or not value.get("bind"):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.bind",
                        "bind must be a non-empty string",
                    )
                )
            process = value.get("process", "generate-fragment")
            if not isinstance(process, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]*", process
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.process",
                        "process must be a lowercase Procedrr process name",
                    )
                )
            if "max_steps" in value and (
                not isinstance(value["max_steps"], int) or value["max_steps"] <= 0
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.max_steps",
                        "max_steps must be positive",
                    )
                )
            allowed_tools = value.get("allowed_tools")
            if allowed_tools is not None and (
                not isinstance(allowed_tools, list)
                or not all(
                    isinstance(tool, str) and tool in KNOWN_TOOLS
                    for tool in allowed_tools
                )
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.specialize.allowed_tools",
                        "allowed_tools must contain only known tools",
                    )
                )
            if isinstance(value.get("bind"), str):
                bindings.add(value["bind"])
        elif (
            control == "call"
            and isinstance(step[control], Mapping)
            and "fragment" in step[control]
        ):
            value = step[control]
            _validate_unknown_fields(
                value,
                {"fragment", "max_steps"},
                f"{step_path}.call",
                diagnostics,
            )
            fragment = value.get("fragment")
            if not isinstance(fragment, str):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.call.fragment",
                        "dynamic call fragment must be a binding reference",
                    )
                )
            else:
                root = fragment.removeprefix("${").removesuffix("}").split(".", 1)[0]
                if root not in bindings:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.call.fragment",
                            f"unknown binding: {fragment}",
                        )
                    )
            if "max_steps" in value and (
                not isinstance(value["max_steps"], int) or value["max_steps"] <= 0
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.call.max_steps",
                        "max_steps must be positive",
                    )
                )
        elif (
            control == "call"
            and isinstance(step[control], Mapping)
            and "process" in step[control]
        ):
            value = step[control]
            _validate_unknown_fields(
                value,
                {"process", "inputs", "outputs"},
                f"{step_path}.call",
                diagnostics,
            )
            process = value.get("process")
            if not isinstance(process, str) or not re.fullmatch(
                r"[a-z][a-z0-9-]*", process
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.call.process",
                        "process must be a lowercase Procedrr process name",
                    )
                )
            inputs = value.get("inputs", {})
            if not isinstance(inputs, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.call.inputs", "inputs must be a mapping"
                    )
                )
            else:
                for reference in _template_references(inputs):
                    root = reference.split(".", 1)[0]
                    if root not in bindings:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.call.inputs",
                                f"unknown binding: {reference}",
                            )
                        )
            outputs = value.get("outputs")
            if not isinstance(outputs, Mapping) or not outputs:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.call.outputs",
                        "outputs must be a non-empty mapping",
                    )
                )
            else:
                for name, reference in outputs.items():
                    if not isinstance(name, str) or not isinstance(reference, str):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.call.outputs",
                                "outputs must map names to string references",
                            )
                        )
                    elif not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", reference):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.call.outputs.{name}",
                                "output reference must be a binding path",
                            )
                        )
                    else:
                        bindings.add(name)
        elif control == "repeat":
            value = step[control]
            if isinstance(value, Mapping):
                _validate_unknown_fields(
                    value,
                    {"body", "max_iterations", "until", "collect"},
                    f"{step_path}.repeat",
                    diagnostics,
                )
            if not isinstance(value, Mapping) or not isinstance(
                value.get("body"), list
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.repeat.body", "repeat body must be a list"
                    )
                )
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("max_iterations"), int)
                or value["max_iterations"] <= 0
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.repeat.max_iterations",
                        "repeat max_iterations must be positive",
                    )
                )
            until = value.get("until") if isinstance(value, Mapping) else None
            if (
                not isinstance(until, Mapping)
                or not isinstance(until.get("subject"), str)
                or "equals" not in until
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.repeat.until",
                        "repeat requires subject and equals",
                    )
                )
            elif not _binding_path_is_known(until["subject"], bindings):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.repeat.until.subject",
                        f"unknown binding: {until['subject']}",
                    )
                )
            collect = value.get("collect") if isinstance(value, Mapping) else None
            if isinstance(collect, Mapping):
                _validate_collect(
                    collect,
                    f"{step_path}.repeat.collect",
                    bindings,
                    diagnostics,
                    check_value=False,
                )
            if isinstance(collect, Mapping) and isinstance(collect.get("binding"), str):
                bindings.add(collect["binding"])
            elif collect is not None:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.repeat.collect",
                        "repeat collect requires binding and value references",
                    )
                )
            nested = value.get("body") if isinstance(value, Mapping) else None
            if isinstance(nested, list):
                nested_bindings = set(bindings)
                if isinstance(collect, Mapping) and isinstance(
                    collect.get("binding"), str
                ):
                    bindings.add(collect["binding"])
                _validate_steps(
                    nested,
                    f"{step_path}.repeat.body",
                    diagnostics,
                    nested_bindings,
                    recovery_names,
                )
                bindings.update(nested_bindings)
                if isinstance(collect, Mapping):
                    _validate_collect(
                        collect,
                        f"{step_path}.repeat.collect",
                        nested_bindings,
                        diagnostics,
                    )
        elif control == "branch":
            value = step[control]
            if isinstance(value, Mapping):
                _validate_unknown_fields(
                    value,
                    {"subject", "cases", "default"},
                    f"{step_path}.branch",
                    diagnostics,
                )
            if not isinstance(value, Mapping) or not isinstance(
                value.get("subject"), str
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.branch.subject", "branch subject is required"
                    )
                )
            elif not _binding_path_is_known(value["subject"], bindings):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.branch.subject",
                        f"unknown binding: {value['subject']}",
                    )
                )
            cases = value.get("cases") if isinstance(value, Mapping) else None
            if not isinstance(cases, Mapping) or not cases:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.branch.cases",
                        "branch cases must be a non-empty mapping",
                    )
                )
            else:
                branch_results: list[set[str]] = []
                for case, nested in cases.items():
                    if not isinstance(nested, list):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.branch.cases.{case}",
                                "branch case steps must be a list",
                            )
                        )
                    else:
                        branch_bindings = set(bindings)
                        _validate_steps(
                            nested,
                            f"{step_path}.branch.cases.{case}",
                            diagnostics,
                            branch_bindings,
                            recovery_names,
                        )
                        branch_results.append(branch_bindings)
            default = value.get("default") if isinstance(value, Mapping) else None
            if default is not None and not isinstance(default, list):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.branch.default",
                        "branch default steps must be a list",
                    )
                )
            elif default is None:
                # An unmatched branch path produces no new bindings.
                branch_results.append(set())
            elif isinstance(default, list):
                default_bindings = set(bindings)
                _validate_steps(
                    default,
                    f"{step_path}.branch.default",
                    diagnostics,
                    default_bindings,
                    recovery_names,
                )
                branch_results.append(default_bindings)
            if branch_results:
                definite = set.intersection(*branch_results)
                bindings.update(definite)
        elif control == "attempt":
            value = step[control]
            if isinstance(value, Mapping):
                _validate_unknown_fields(
                    value,
                    {"id", "max_attempts", "body", "on_failure"},
                    f"{step_path}.attempt",
                    diagnostics,
                )
            if not isinstance(value, Mapping) or not isinstance(
                value.get("body"), list
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.body", "attempt body must be a list"
                    )
                )
            if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.id", "attempt id must be a string"
                    )
                )
            if (
                not isinstance(value, Mapping)
                or not isinstance(value.get("max_attempts"), int)
                or value["max_attempts"] <= 0
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.max_attempts",
                        "attempt max_attempts must be positive",
                    )
                )
            failure = value.get("on_failure") if isinstance(value, Mapping) else None
            if (
                not isinstance(failure, Mapping)
                or not isinstance(failure.get("recovery"), str)
                or not isinstance(failure.get("resume"), str)
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure",
                        "attempt requires recovery and resume",
                    )
                )
            elif isinstance(value, Mapping) and failure["resume"] != value.get("id"):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure.resume",
                        "resume must match the attempt id",
                    )
                )
            elif failure["recovery"] not in (recovery_names or set()):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.attempt.on_failure.recovery",
                        f"unknown recovery: {failure['recovery']}",
                    )
                )
            nested = value.get("body") if isinstance(value, Mapping) else None
            if isinstance(nested, list):
                nested_bindings = set(bindings) | {"failure"}
                _validate_steps(
                    nested,
                    f"{step_path}.attempt.body",
                    diagnostics,
                    nested_bindings,
                    recovery_names,
                )
                bindings.update(nested_bindings)
        elif control in {"for_each", "worklist", "call"}:
            value = step[control]
            if control == "call" and isinstance(value, Mapping) and "fragment" in value:
                continue
            if control in {"for_each", "worklist"} and isinstance(value, Mapping):
                _validate_unknown_fields(
                    value,
                    {
                        "snapshot",
                        "item_binding",
                        "item",
                        "collect",
                        "body",
                        "steps",
                        "max_admissions",
                        "max_epochs",
                    },
                    f"{step_path}.{control}",
                    diagnostics,
                )
                _validate_loop_declaration(
                    value, control, step_path, bindings, diagnostics
                )
            nested = (
                value.get("steps", value.get("body"))
                if isinstance(value, Mapping)
                else None
            )
            if not isinstance(value, Mapping) or not isinstance(nested, list):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.{control}", "nested steps must be a list"
                    )
                )
            else:
                local = set(bindings)
                for key in ("item_binding", "item"):
                    if isinstance(value.get(key), str):
                        local.add(value[key])
                if control == "for_each" and "collect" not in value:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.for_each.collect",
                            "data-driven body outputs require an explicit collect "
                            "binding",
                        )
                    )
                collect = value.get("collect")
                if collect is not None and not isinstance(collect, Mapping):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.{control}.collect",
                            "collect must be a mapping",
                        )
                    )
                if isinstance(collect, Mapping) and collect.get("mode") not in {
                    None,
                    "map",
                    "list",
                }:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.for_each.collect.mode",
                            "collect mode must be map or list",
                        )
                    )
                if isinstance(collect, str):
                    bindings.add(collect)
                elif isinstance(collect, Mapping) and isinstance(
                    collect.get("binding"), str
                ):
                    bindings.add(collect["binding"])
                _validate_steps(
                    nested,
                    f"{step_path}.{control}.body",
                    diagnostics,
                    local,
                    recovery_names,
                )
                if isinstance(collect, Mapping):
                    _validate_collect(
                        collect,
                        f"{step_path}.{control}.collect",
                        local,
                        diagnostics,
                        # A for_each body may intentionally emit a collected
                        # value only from one branch (for example, repair
                        # actions for failed validations). The binding name
                        # and shape are still checked above; branch-sensitive
                        # presence is not a required value for collection.
                        check_value=False,
                    )
                bindings.update(local)
        elif control == "operation":
            operation = step[control]
            if isinstance(operation, Mapping):
                _validate_unknown_fields(
                    operation,
                    {
                        "name",
                        "tool",
                        "parameters",
                        "command",
                        "bind",
                        "returns",
                        "source",
                    },
                    f"{step_path}.operation",
                    diagnostics,
                )
            if not isinstance(operation, Mapping) or not isinstance(
                operation.get("name", operation.get("tool")), str
            ):
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation", "operation name is required"
                    )
                )
            elif "tool" not in operation:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation.tool",
                        "a concrete tool reference is required",
                    )
                )
            elif operation.get("tool") not in KNOWN_TOOLS:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.operation.tool",
                        f"unknown tool: {operation.get('tool')}",
                    )
                )
            elif operation.get("tool") == "gather_context":
                parameters = operation.get("parameters")
                if (
                    not isinstance(parameters, Mapping)
                    or not isinstance(parameters.get("types"), list)
                    or not parameters["types"]
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.parameters",
                            "gather_context requires a non-empty types list",
                        )
                    )
            elif operation.get("tool") == "internal":
                command = operation.get("command")
                if command is None and isinstance(operation.get("parameters"), Mapping):
                    command = operation["parameters"].get("command")
                command_parts_valid = (
                    isinstance(command, list)
                    and bool(command)
                    and all(
                        isinstance(part, str)
                        or (
                            isinstance(part, Mapping)
                            and part.get("type") in {"literal", "reference"}
                            and isinstance(part.get("value"), (str, int, float, bool))
                        )
                        for part in command
                    )
                )
                if not command_parts_valid and not (
                    isinstance(command, str) and _BINDING.fullmatch(command) is not None
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.command",
                            "internal requires a non-empty string command list",
                        )
                    )
            returns = (
                operation.get("returns") if isinstance(operation, Mapping) else None
            )
            if returns is not None:
                if not isinstance(returns, Mapping):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.returns",
                            "returns must be an inline JSON Schema object",
                        )
                    )
                else:
                    try:
                        Draft7Validator.check_schema(dict(returns))
                    except SchemaError as error:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.operation.returns",
                                f"invalid return schema: {error.message}",
                            )
                        )
            if isinstance(operation, Mapping):
                references = list(_template_references(operation))
                if isinstance(operation.get("source"), list):
                    references.extend(
                        item for item in operation["source"] if isinstance(item, str)
                    )
                for reference in references:
                    root = reference.split(".", 1)[0]
                    if root not in bindings:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.operation",
                                f"unknown binding: {reference}",
                            )
                        )
            if isinstance(operation, Mapping) and isinstance(
                operation.get("bind"), str
            ):
                if not _BINDING_NAME.fullmatch(operation["bind"]):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.operation.bind",
                            "operation bind must be a valid binding name",
                        )
                    )
                else:
                    bindings.add(operation["bind"])
        elif control == "gate":
            gate = step[control]
            if not isinstance(gate, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(f"{step_path}.gate", "gate must be a mapping")
                )
            else:
                _validate_unknown_fields(
                    gate,
                    {"subject", "equals", "on_failure"},
                    f"{step_path}.gate",
                    diagnostics,
                )
                subject = gate.get("subject")
                gate_root: str | None = (
                    subject.split(".", 1)[0] if isinstance(subject, str) else None
                )
                if not isinstance(subject, str) or gate_root not in bindings:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.gate.subject", f"unknown binding: {subject}"
                        )
                    )
                if "equals" not in gate or "on_failure" not in gate:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.gate", "equals and on_failure are required"
                        )
                    )
                failure = gate.get("on_failure")
                if isinstance(failure, Mapping):
                    retry = failure.get("retry")
                    terminal = failure.get("terminal")
                    if isinstance(terminal, str) and terminal in {"failed", "blocked"}:
                        pass
                    elif (
                        not isinstance(retry, Mapping)
                        or not isinstance(retry.get("max_attempts"), int)
                        or retry["max_attempts"] <= 0
                    ):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.gate.on_failure",
                                "retry requires a positive max_attempts",
                            )
                        )
                    elif retry.get("on_exhausted") not in {"failed", "blocked"}:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.gate.on_failure.retry.on_exhausted",
                                "retry requires failed or blocked exhaustion",
                            )
                        )
        elif control == "judge":
            judge = step[control]
            if not isinstance(judge, Mapping):
                diagnostics.append(
                    DocumentDiagnostic(f"{step_path}.judge", "judge must be a mapping")
                )
            else:
                _validate_unknown_fields(
                    judge,
                    {
                        "kind",
                        "provider",
                        "question",
                        "subject",
                        "prompt_system",
                        "instructions",
                        "context",
                        "output",
                        "validation",
                        "validator",
                        "prompt_rules",
                    },
                    f"{step_path}.judge",
                    diagnostics,
                )
                for key in (
                    "question",
                    "subject",
                    "prompt_system",
                    "instructions",
                    "context",
                    "output",
                ):
                    if key not in judge:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.{key}", "field is required"
                            )
                        )
                context = judge.get("context")
                if isinstance(context, list):
                    if len(context) != len(set(context)):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.context",
                                "judge context bindings must be unique",
                            )
                        )
                    for binding in context:
                        if not isinstance(binding, str) or not _binding_path_is_known(
                            binding, bindings
                        ):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{step_path}.judge.context",
                                    f"unknown binding: {binding}",
                                )
                            )
                subject = judge.get("subject")
                if isinstance(subject, str) and not _binding_path_is_known(
                    subject, bindings
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.subject",
                            f"unknown binding: {subject}",
                        )
                    )
                prompt_rules = judge.get("prompt_rules", [])
                if not isinstance(prompt_rules, list):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.prompt_rules",
                            "prompt_rules must be a list",
                        )
                    )
                else:
                    for rule_index, rule in enumerate(prompt_rules):
                        rule_path = f"{step_path}.judge.prompt_rules[{rule_index}]"
                        if not isinstance(rule, Mapping):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    rule_path, "prompt rule must be a mapping"
                                )
                            )
                            continue
                        condition = rule.get("when")
                        if not isinstance(condition, Mapping):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.when",
                                    "prompt rule condition must be a mapping",
                                )
                            )
                            continue
                        binding = condition.get("binding")
                        if not isinstance(binding, str) or not binding.strip():
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.when.binding",
                                    "prompt rule binding must be a non-empty string",
                                )
                            )
                        elif (
                            isinstance(context, list)
                            and binding.split(".", 1)[0] not in context
                        ):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.when.binding",
                                    "prompt rule binding must be declared in "
                                    "judge.context",
                                )
                            )
                        operators = set(condition) - {"binding"}
                        if len(operators) != 1 or not operators.issubset(
                            {"equals", "not_equals", "in", "contains"}
                        ):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.when",
                                    "prompt rule needs exactly one supported "
                                    "comparison",
                                )
                            )
                        instructions = rule.get("instructions")
                        if (
                            not isinstance(instructions, list)
                            or not instructions
                            or not all(
                                isinstance(item, str) and item.strip()
                                for item in instructions
                            )
                        ):
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.instructions",
                                    "prompt rule instructions must be a non-empty "
                                    "string list",
                                )
                            )
                        mode = rule.get("mode", "append")
                        if mode not in {"append", "replace"}:
                            diagnostics.append(
                                DocumentDiagnostic(
                                    f"{rule_path}.mode",
                                    "prompt rule mode must be append or replace",
                                )
                            )
                if "validation" not in judge and "validator" not in judge:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.validation",
                            "a validation reference is required",
                        )
                    )
                output = judge.get("output")
                if not isinstance(output, Mapping) or not isinstance(
                    output.get("schema"), Mapping
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.output.schema",
                            "an inline output schema is required",
                        )
                    )
                elif isinstance(output, Mapping):
                    schema = output["schema"]
                    try:
                        Draft7Validator.check_schema(dict(schema))
                    except SchemaError as error:
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.output.schema",
                                f"invalid output schema: {error.message}",
                            )
                        )
                    output_name = output.get("name")
                    if not isinstance(output_name, str) or not _BINDING_NAME.fullmatch(
                        output_name
                    ):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.output.name",
                                "output name must be a valid binding name",
                            )
                        )
                if (
                    not isinstance(judge.get("question"), str)
                    or not str(judge.get("question", "")).strip()
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.question",
                            "question must be a non-empty string",
                        )
                    )
                for field in ("prompt_system",):
                    if (
                        not isinstance(judge.get(field), str)
                        or not judge[field].strip()
                    ):
                        diagnostics.append(
                            DocumentDiagnostic(
                                f"{step_path}.judge.{field}",
                                f"{field} must be a non-empty string",
                            )
                        )
                if not isinstance(judge.get("instructions"), list) or not all(
                    isinstance(item, str) and item.strip()
                    for item in judge.get("instructions", [])
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.instructions",
                            "instructions must be a non-empty string list",
                        )
                    )
                if not isinstance(context, list) or not all(
                    isinstance(item, str) and _BINDING_PATH.fullmatch(item)
                    for item in context
                ):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.context",
                            "context must be a list of binding paths",
                        )
                    )
                validation = judge.get("validation", judge.get("validator"))
                kind = (
                    validation.get("kind")
                    if isinstance(validation, Mapping)
                    else validation
                )
                if kind not in KNOWN_VALIDATORS:
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{step_path}.judge.validation",
                            f"unknown validator: {kind}",
                        )
                    )
            if (
                isinstance(output, Mapping)
                and isinstance(output.get("name"), str)
                and _BINDING_NAME.fullmatch(output["name"])
            ):
                bindings.add(output["name"])
                if isinstance(output, Mapping) and isinstance(
                    output.get("schema"), Mapping
                ):
                    _validate_model_output_schema(
                        output["schema"],
                        f"{step_path}.judge.output.schema",
                        diagnostics,
                    )
        elif control == "terminal":
            if step[control] not in {
                "succeeded",
                "failed",
                "blocked",
                "cancelled",
                "suspended",
            }:
                diagnostics.append(
                    DocumentDiagnostic(
                        f"{step_path}.terminal",
                        "terminal status is unsupported",
                    )
                )


def _binding_path_is_known(path: Any, bindings: set[str]) -> bool:
    if not isinstance(path, str) or _BINDING_PATH.fullmatch(path) is None:
        return False
    return path.split(".", 1)[0] in bindings


def _validate_loop_declaration(
    declaration: Mapping[str, Any],
    kind: str,
    path: str,
    bindings: set[str],
    diagnostics: list[DocumentDiagnostic],
) -> None:
    item_binding = declaration.get("item_binding", declaration.get("item"))
    if (
        not isinstance(item_binding, str)
        or _BINDING_NAME.fullmatch(item_binding) is None
    ):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{kind}.item_binding",
                "item_binding must be a valid binding name",
            )
        )

    snapshot = declaration.get("snapshot")
    if not isinstance(snapshot, Mapping):
        diagnostics.append(
            DocumentDiagnostic(f"{path}.{kind}.snapshot", "snapshot must be a mapping")
        )
        return

    name = snapshot.get("name")
    values = snapshot.get("values")
    if values is not None:
        if not isinstance(values, list):
            diagnostics.append(
                DocumentDiagnostic(
                    f"{path}.{kind}.snapshot.values",
                    "snapshot values must be a list",
                )
            )
    elif not _binding_path_is_known(name, bindings):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{kind}.snapshot.name",
                f"snapshot name must reference a known binding: {name}",
            )
        )

    max_items = snapshot.get("max_items")
    if max_items is not None and (
        not isinstance(max_items, int) or isinstance(max_items, bool) or max_items <= 0
    ):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{kind}.snapshot.max_items",
                "max_items must be a positive integer",
            )
        )

    if kind == "worklist":
        max_epochs = declaration.get("max_epochs", 1)
        if (
            not isinstance(max_epochs, int)
            or isinstance(max_epochs, bool)
            or max_epochs <= 0
        ):
            diagnostics.append(
                DocumentDiagnostic(
                    f"{path}.{kind}.max_epochs",
                    "max_epochs must be a positive integer",
                )
            )
    max_admissions = declaration.get("max_admissions")
    if max_admissions is not None and (
        not isinstance(max_admissions, int)
        or isinstance(max_admissions, bool)
        or max_admissions <= 0
    ):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{kind}.max_admissions",
                "max_admissions must be a positive integer",
            )
        )
    elif (
        isinstance(values, list)
        and isinstance(max_items, int)
        and len(values) > max_items
    ):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{kind}.snapshot.values",
                "snapshot values exceed max_items",
            )
        )


def _validate_collect(
    collect: Mapping[str, Any],
    path: str,
    bindings: set[str],
    diagnostics: list[DocumentDiagnostic],
    *,
    check_value: bool = True,
) -> None:
    binding = collect.get("binding")
    if not isinstance(binding, str) or _BINDING_NAME.fullmatch(binding) is None:
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.binding", "collect binding must be a valid binding name"
            )
        )
    mode = collect.get("mode", "map")
    if mode not in {"map", "list"}:
        diagnostics.append(
            DocumentDiagnostic(f"{path}.mode", "collect mode must be map or list")
        )
    for key in ("key", "value"):
        if key in collect and not isinstance(collect[key], str):
            diagnostics.append(
                DocumentDiagnostic(
                    f"{path}.{key}", f"collect {key} must be a binding path"
                )
            )
    value = collect.get("value")
    if (
        check_value
        and isinstance(value, str)
        and not _binding_path_is_known(value, bindings)
    ):
        diagnostics.append(
            DocumentDiagnostic(f"{path}.value", f"unknown binding: {value}")
        )


def _validate_model_output_schema(
    schema: Mapping[str, Any],
    path: str,
    diagnostics: list[DocumentDiagnostic],
) -> None:
    """Reject model output fields that must be compiler/runtime-owned.

    Judges may explain and classify facts, but identity, provenance, and
    integrity metadata must be derived from trusted inputs. Walk the complete
    JSON Schema so nested model output cannot smuggle those fields through an
    otherwise valid top-level response.
    """

    def walk(value: Any, schema_path: str) -> None:
        if not isinstance(value, Mapping):
            return
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            for name, child in properties.items():
                if isinstance(name, str) and _MODEL_OWNED_METADATA.search(name):
                    diagnostics.append(
                        DocumentDiagnostic(
                            f"{schema_path}.properties.{name}",
                            "LLM output must not generate identity, provenance, "
                            "version, reference, hash, or fingerprint metadata",
                            code="model_owned_metadata",
                        )
                    )
                walk(child, f"{schema_path}.properties.{name}")
        for keyword in (
            "additionalProperties",
            "items",
            "contains",
            "propertyNames",
            "not",
            "if",
            "then",
            "else",
        ):
            child = value.get(keyword)
            if isinstance(child, Mapping):
                walk(child, f"{schema_path}.{keyword}")
            elif isinstance(child, list):
                for index, item in enumerate(child):
                    walk(item, f"{schema_path}.{keyword}[{index}]")
        for keyword in ("allOf", "anyOf", "oneOf"):
            alternatives = value.get(keyword)
            if isinstance(alternatives, list):
                for index, item in enumerate(alternatives):
                    walk(item, f"{schema_path}.{keyword}[{index}]")

    walk(schema, path)


def _template_references(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield from (match.group(1) for match in _BINDING.finditer(value))
    elif isinstance(value, Mapping):
        if value.get("type") == "reference":
            reference = value.get("value")
            if isinstance(reference, str):
                yield reference
            return
        if value.get("type") == "literal":
            return
        for child in value.values():
            yield from _template_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _template_references(child)


def _validate_unknown_fields(
    value: Mapping[str, Any],
    allowed: set[str],
    path: str,
    diagnostics: list[DocumentDiagnostic],
) -> None:
    for name in sorted(set(value) - allowed):
        diagnostics.append(
            DocumentDiagnostic(
                f"{path}.{name}",
                f"unknown field: {name}",
                code="unknown_field",
            )
        )


def _decision_complexity(schema: Any) -> tuple[str, ...]:
    if not isinstance(schema, Mapping):
        return ("an inline object schema is required",)
    reasons: list[str] = []
    if schema.get("type") == "array":
        reasons.append("the judge returns a collection; iterate one decision at a time")
    properties = schema.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            if (
                isinstance(child, Mapping)
                and child.get("type") == "array"
                and isinstance(child.get("items"), Mapping)
                and child["items"].get("type") == "object"
            ):
                reasons.append(
                    f"property {name!r} returns multiple values; move it to a "
                    "bounded loop"
                )
    return tuple(reasons)


def _path_to_json_pointer(path: str) -> str:
    segments = re.findall(r"(?:^|\.)([^.\[\]]+)|\[(\d+)\]", path)
    values = [name or index for name, index in segments]
    return "/" + "/".join(
        value.replace("~", "~0").replace("/", "~1") for value in values
    )


__all__ = [
    "DocumentDiagnostic",
    "ParseError",
    "parse_and_validate",
    "parse_document",
    "render_document",
    "validate_single_decision",
    "validate_document",
]
