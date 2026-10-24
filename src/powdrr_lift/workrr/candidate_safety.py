"""Deterministic guards for compatibility behavior in feature candidates."""

from __future__ import annotations

import ast
import re


def existing_test_contract_changes(
    path: str,
    baseline_source: str,
    candidate_source: str,
    *,
    accepted_contract: str,
) -> tuple[str, ...]:
    """Find edits to existing tests that are not explicitly part of the request.

    Added tests are allowed. Existing test functions, fixtures, imports, and
    class bases/decorators must remain stable so an implementation cannot make
    its local validation pass by rewriting the repository's compatibility suite.
    """
    try:
        baseline = ast.parse(baseline_source, filename=path)
        candidate = ast.parse(candidate_source, filename=path)
    except SyntaxError as error:
        return (f"could not verify existing tests in {path}: {error}",)

    baseline_contract = _test_contract(baseline)
    candidate_contract = _test_contract(candidate)
    missing = sorted(baseline_contract.keys() - candidate_contract.keys())
    changed = sorted(
        name
        for name in baseline_contract.keys() & candidate_contract.keys()
        if baseline_contract[name] != candidate_contract[name]
    )
    findings = [
        f"existing test contract changed in {path}: {name}"
        for name in (*missing, *changed)
        if not _contract_explicitly_names(accepted_contract, path, name)
    ]
    return tuple(findings)


def unapproved_public_default_changes(
    path: str,
    baseline_source: str,
    candidate_source: str,
    *,
    accepted_contract: str,
) -> tuple[str, ...]:
    """Find changed public class or callable defaults absent from the request."""
    try:
        baseline = ast.parse(baseline_source, filename=path)
        candidate = ast.parse(candidate_source, filename=path)
    except SyntaxError as error:
        return (f"could not verify public defaults in {path}: {error}",)

    before = _public_defaults(baseline)
    after = _public_defaults(candidate)
    findings: list[str] = []
    for key in sorted(before):
        if key in after and before[key] == after[key]:
            continue
        if _default_change_is_requested(accepted_contract, path, key):
            continue
        findings.append(f"unapproved public default change in {path}: {key}")
    for key in sorted(after.keys() - before.keys()):
        if not _default_change_is_requested(accepted_contract, path, key):
            findings.append(f"unapproved public default addition in {path}: {key}")
    return tuple(findings)


def _test_contract(tree: ast.Module) -> dict[str, str]:
    contract: dict[str, str] = {}
    imports: dict[str, int] = {}

    def visit(body: list[ast.stmt], scope: tuple[str, ...]) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if not scope:
                    fingerprint = ast.dump(node, include_attributes=False)
                    imports[fingerprint] = imports.get(fingerprint, 0) + 1
            elif isinstance(node, ast.ClassDef):
                key = ".".join((*scope, node.name))
                contract[f"class:{key}"] = "|".join(
                    ast.dump(part, include_attributes=False)
                    for part in (
                        *node.bases,
                        *node.decorator_list,
                        *node.keywords,
                    )
                )
                visit(node.body, (*scope, node.name))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = ".".join((*scope, node.name))
                contract[f"function:{key}"] = ast.dump(node, include_attributes=False)
                visit(node.body, (*scope, node.name))

    visit(tree.body, ())
    for fingerprint, count in imports.items():
        contract[f"import:{fingerprint}"] = str(count)
    return contract


def _public_defaults(tree: ast.Module) -> dict[str, str]:
    defaults: dict[str, str] = {}

    def target_names(target: ast.expr) -> tuple[str, ...]:
        if isinstance(target, ast.Name):
            return (target.id,) if not target.id.startswith("_") else ()
        if isinstance(target, (ast.Tuple, ast.List)):
            return tuple(name for item in target.elts for name in target_names(item))
        return ()

    def visit_class(node: ast.ClassDef, scope: tuple[str, ...]) -> None:
        class_scope = (*scope, node.name)
        for item in node.body:
            if isinstance(item, ast.ClassDef):
                visit_class(item, class_scope)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    for name in target_names(target):
                        defaults[f"{'.'.join(class_scope)}.{name}"] = ast.dump(
                            item.value, include_attributes=False
                        )
            elif isinstance(item, ast.AnnAssign):
                for name in target_names(item.target):
                    if item.value is not None:
                        defaults[f"{'.'.join(class_scope)}.{name}"] = ast.dump(
                            item.value, include_attributes=False
                        )
            elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_scope = (*class_scope, item.name)
                positional = [*item.args.posonlyargs, *item.args.args]
                for argument, value in zip(
                    positional[-len(item.args.defaults) :]
                    if item.args.defaults
                    else (),
                    item.args.defaults,
                    strict=True,
                ):
                    if not argument.arg.startswith("_"):
                        defaults[f"{'.'.join(function_scope)}({argument.arg})"] = (
                            ast.dump(value, include_attributes=False)
                        )
                for argument, keyword_value in zip(
                    item.args.kwonlyargs, item.args.kw_defaults, strict=True
                ):
                    if keyword_value is not None and not argument.arg.startswith("_"):
                        defaults[f"{'.'.join(function_scope)}({argument.arg})"] = (
                            ast.dump(keyword_value, include_attributes=False)
                        )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            visit_class(node, ())
    return defaults


def _contract_explicitly_names(contract: str, path: str, name: str) -> bool:
    return _contains_identifier(contract, path) or _contains_identifier(contract, name)


def _default_change_is_requested(contract: str, path: str, key: str) -> bool:
    if _contains_identifier(contract, path) or _contains_identifier(contract, key):
        return True
    if "(" not in key:
        return _contains_identifier(contract, key.rsplit(".", 1)[-1])
    callable_name, argument = key.split("(", 1)
    return _contains_identifier(
        contract, callable_name.rsplit(".", 1)[-1]
    ) and _contains_identifier(contract, argument.removesuffix(")"))


def _contains_identifier(text: str, identifier: str) -> bool:
    if not identifier:
        return False
    if "." in identifier or "/" in identifier:
        return identifier in text
    return re.search(rf"(?<![\w]){re.escape(identifier)}(?![\w])", text) is not None


__all__ = ["existing_test_contract_changes", "unapproved_public_default_changes"]
