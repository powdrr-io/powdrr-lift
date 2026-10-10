"""Regression checks for blinded inputs, rendering, accounting, and resumability."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from science.deepswe.acceptance_templates import common
from science.deepswe.acceptance_templates.cli import collect
from science.deepswe.acceptance_templates.common import (
    Recorder,
    digest,
    generation_input,
    load_json,
    source_spans,
    write_json,
)
from science.deepswe.acceptance_templates.evaluation import aggregate, score
from science.deepswe.acceptance_templates.generation import (
    generate,
    load_catalog,
    render_instance,
    save_generation,
    validate_bindings,
)
from science.deepswe.acceptance_templates.references import draft_reference
from science.deepswe.acceptance_templates.summary import combine

HERE = Path(__file__).resolve().parents[1] / "science/deepswe/acceptance_templates"


def task() -> dict[str, Any]:
    text = "Each entry calls the factory.\nHistory restores direct children.\n"
    return {
        "task_id": "example",
        "instruction": text,
        "source_spans": source_spans(text),
    }


def test_generation_allowlist_excludes_embedded_evaluation_artifacts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "input.json"
    write_json(
        path,
        {
            "task_id": "x",
            "instruction": "Expose value().",
            "solution": "PATCH SECRET",
            "reference": {"answer": 42},
            "source_spans": [{"text": "INJECTED PATCH"}],
        },
    )
    payload = generation_input(path)
    assert set(payload) == {"task_id", "instruction", "source_spans"}
    assert "PATCH" not in str(payload)
    assert payload["source_spans"][0]["text"] == "Expose value()."


def test_source_offsets_recover_original_unicode_and_line_endings() -> None:
    instruction = "\r\nRésumé: values → output.\r\n\n  Preserve spaces.\n"
    spans = source_spans(instruction)
    assert [instruction[row["start"] : row["end"]] for row in spans] == [
        "Résumé: values → output.",
        "  Preserve spaces.",
    ]


def _make_task(root: Path, name: str, repo: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "instruction.md").write_text("Expose a feature.\n")
    (directory / "task.toml").write_text(f'[metadata]\nrepository_url="{repo}"\n')


def test_collection_excludes_other_tasks_from_catalog_repository_families(
    tmp_path: Path,
) -> None:
    _make_task(tmp_path, "development", "https://github.com/org/lib.git")
    _make_task(tmp_path, "sibling", "https://github.com/org/lib/")
    manifest = tmp_path / "development.json"
    write_json(manifest, {"tasks": [{"task": "development"}]})
    with pytest.raises(ValueError, match="excluded"):
        collect(
            tmp_path, tmp_path / "output", ["sibling"], development_manifest=manifest
        )


def test_collected_inputs_never_include_patch_contents(tmp_path: Path) -> None:
    _make_task(tmp_path, "new", "https://github.com/org/new")
    patch = tmp_path / "new/solution/solution.patch"
    patch.parent.mkdir()
    patch.write_text("HIDDEN ANSWER")
    manifest = tmp_path / "development.json"
    write_json(manifest, {"tasks": []})
    result = collect(
        tmp_path, tmp_path / "output", ["new"], development_manifest=manifest
    )
    payload = load_json(tmp_path / "output/inputs/new.json")
    assert set(payload) == {"task_id", "instruction"}
    assert "HIDDEN ANSWER" not in str(payload)
    assert result["tasks"][0]["artifacts"]["solution/solution.patch"]["sha256"]


def test_frozen_catalog_has_52_renderable_cards() -> None:
    catalog = load_catalog(HERE / "catalog.json")
    assert [card["id"] for card in catalog["templates"]] == [
        f"T{i:02}" for i in range(1, 53)
    ]


def test_optional_history_behavior_is_not_invented_by_renderer() -> None:
    card = next(
        card
        for card in load_catalog(HERE / "catalog.json")["templates"]
        if card["id"] == "T27"
    )
    rendered = render_instance(
        card,
        {
            "slots": [
                {"name": "history_kind", "value": "shallow history"},
                {"name": "included_states", "value": "the direct children"},
            ]
        },
    )
    assert (
        rendered == "After saving and recalling shallow history, "
        "restore data for the direct children."
    )
    assert "deep copy" not in rendered


def _binding() -> tuple[dict[str, Any], dict[str, Any]]:
    catalog = {
        "templates": [
            {
                "id": "T99",
                "slots": [{"name": "owner", "kind": "Scope", "required": True}],
            }
        ]
    }
    response = {
        "decisions": [
            {
                "requirement_id": "r001",
                "template_id": "T99",
                "applicability": "yes",
                "reason": "entry ownership",
                "instances": [
                    {
                        "prerequisites": [],
                        "slots": [
                            {
                                "name": "owner",
                                "kind": "Scope",
                                "value": "each state instance",
                                "basis": "instruction",
                                "source_ids": ["s001"],
                                "source_quotes": [
                                    {
                                        "source_id": "s001",
                                        "quote": "Each entry calls the factory.",
                                    }
                                ],
                                "support_explanation": "Entry owns the factory call.",
                            }
                        ],
                    }
                ],
            }
        ]
    }
    return catalog, response


@pytest.mark.parametrize("mutation", ["citation", "slot", "kind", "required", "pair"])
def test_binding_rejects_unsupported_structure_and_missing_evidence(
    mutation: str,
) -> None:
    catalog, response = _binding()
    slot = response["decisions"][0]["instances"][0]["slots"][0]
    if mutation == "citation":
        slot["source_ids"] = ["s999"]
    elif mutation == "slot":
        slot["name"] = "unrequested_depth"
    elif mutation == "kind":
        slot["kind"] = "Expression"
    elif mutation == "required":
        response["decisions"][0]["instances"][0]["slots"] = []
    else:
        response["decisions"][0]["requirement_id"] = "r999"
    with pytest.raises(ValueError):
        validate_bindings(
            response,
            selected=[{"requirement_id": "r001", "template_ids": ["T99"]}],
            catalog=catalog,
            task=task(),
        )


@pytest.mark.parametrize("mutation", ["invented_quote", "wrong_span", "empty_reason"])
def test_binding_requires_exact_slot_evidence_and_explanation(mutation: str) -> None:
    catalog, response = _binding()
    slot = response["decisions"][0]["instances"][0]["slots"][0]
    if mutation == "invented_quote":
        slot["source_quotes"][0]["quote"] = "Factory calls return the identical object."
    elif mutation == "wrong_span":
        slot["source_ids"] = ["s002"]
    else:
        slot["support_explanation"] = ""
    with pytest.raises(ValueError):
        validate_bindings(
            response,
            selected=[{"requirement_id": "r001", "template_ids": ["T99"]}],
            catalog=catalog,
            task=task(),
        )


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "unknown", "invented_quote"]
)
def test_yes_binding_requires_all_catalog_prerequisites(mutation: str) -> None:
    catalog, response = _binding()
    catalog["templates"][0]["prerequisites"] = [
        {
            "id": "ownership",
            "claim": "Entry owns the action",
            "reject_inference": "No global owner",
        }
    ]
    proof: dict[str, Any] = {
        "id": "ownership",
        "source_ids": ["s001"],
        "source_quotes": [
            {"source_id": "s001", "quote": "Each entry calls the factory."}
        ],
        "support_explanation": "The entry performs the call.",
    }
    proofs = [proof]
    if mutation == "missing":
        proofs = []
    elif mutation == "duplicate":
        proofs = [proof, copy.deepcopy(proof)]
    elif mutation == "unknown":
        proof["id"] = "unrequested"
    else:
        proof["source_quotes"][0]["quote"] = "The child overrides the parent."
    response["decisions"][0]["instances"][0]["prerequisites"] = proofs
    with pytest.raises(ValueError):
        validate_bindings(
            response,
            selected=[{"requirement_id": "r001", "template_ids": ["T99"]}],
            catalog=catalog,
            task=task(),
        )


def test_valid_yes_proof_and_declined_guard_both_validate() -> None:
    catalog, response = _binding()
    catalog["templates"][0]["prerequisites"] = [{"id": "ownership"}]
    response["decisions"][0]["instances"][0]["prerequisites"] = [
        {
            "id": "ownership",
            "source_ids": ["s001"],
            "source_quotes": [
                {"source_id": "s001", "quote": "Each entry calls the factory."}
            ],
            "support_explanation": "Entry owns the call.",
        }
    ]
    selected = [{"requirement_id": "r001", "template_ids": ["T99"]}]
    assert validate_bindings(response, selected=selected, catalog=catalog, task=task())
    response["decisions"][0].update(applicability="unknown", instances=[])
    assert validate_bindings(response, selected=selected, catalog=catalog, task=task())


def test_catalog_prerequisites_are_reproducible_and_explicit() -> None:
    from science.deepswe.acceptance_templates.build_catalog import (
        DEFAULT_PROPOSAL,
        build_catalog,
    )

    catalog = load_catalog(HERE / "catalog.json")
    assert catalog == build_catalog(DEFAULT_PROPOSAL)
    guarded = {card["id"] for card in catalog["templates"] if card["prerequisites"]}
    assert guarded == {"T17", "T18", "T31"}


def test_calibration_fixture_labels_do_not_enter_generation_or_review_inputs() -> None:
    from science.deepswe.acceptance_templates.calibration import (
        fixture_criteria,
        inputs,
    )

    fixture = load_json(HERE / "data/contrasts.json")["cases"][0]
    task_input, inv = inputs(fixture)
    criteria = fixture_criteria(fixture, task_input)
    payload = {**task_input, "inventory": inv, "criteria": criteria}
    assert "expected" not in json.dumps(payload)
    assert "bindings" not in json.dumps(payload)
    assert "label_status" not in payload
    assert fixture["id"] not in json.dumps(payload)
    assert task_input["task_id"].startswith("contrast-")


@pytest.mark.parametrize("mutation", ["input", "inventory", "status", "arm"])
def test_controlled_rerun_rejects_unrelated_or_modified_baselines(
    mutation: str,
) -> None:
    from science.deepswe.acceptance_templates.rerun import reused_inventory

    inv = {"items": [{"id": "r001", "text": "original requirement"}]}
    baseline = {
        "status": "completed",
        "arm": "templates",
        "task_id": task()["task_id"],
        "input_sha256": digest(task()),
        "inventory": inv,
        "inventory_sha256": digest(inv),
    }
    assert reused_inventory(baseline, task()) == inv
    if mutation == "input":
        baseline["input_sha256"] = "other"
    elif mutation == "inventory":
        baseline["inventory"]["items"][0]["text"] = "modified requirement"
    elif mutation == "status":
        baseline["status"] = "failed"
    else:
        baseline["arm"] = "direct"
    with pytest.raises(ValueError, match="matching"):
        reused_inventory(baseline, task())


def test_pilot_review_replay_preserves_original_payload_and_hides_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from science.deepswe.acceptance_templates.calibration import replay_check
    from science.deepswe.acceptance_templates.evaluation import (
        COVERAGE_PROMPT,
        SUPPORT_PROMPT,
    )

    case = load_json(HERE / "data/pilot-review-replays.json")["cases"][0]
    recorder = Recorder(None, tmp_path / "calls", provider="fake", model="fake")
    captured = []

    def fake_call(
        stage: str,
        prompt: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
        validator: Any,
    ) -> Any:
        captured.append({"payload": payload, "schema": schema})
        response = copy.deepcopy(case["original_response"])
        for row in response["support"]:
            if row["criterion_id"] == case["target_id"]:
                row["status"] = "partial"
        return validator(response)

    monkeypatch.setattr(recorder, "call", fake_call)
    checks = replay_check(case, recorder, COVERAGE_PROMPT, SUPPORT_PROMPT)
    assert captured[0]["payload"] == case["payload"]
    assert captured[0]["schema"] == case["schema"]
    assert "expected" not in json.dumps(captured)
    assert "original_response" not in json.dumps(captured)
    assert checks[0]["passed"]
    assert checks[0]["original_observed"] == "supported"


@pytest.mark.parametrize(
    "statuses, expected",
    [
        (["supported", "supported"], "supported"),
        (["supported", "unsupported"], "partial"),
        (["unsupported"], "unsupported"),
        (["supported", "uncertain"], "uncertain"),
    ],
)
def test_atomic_support_verdict_is_derived_from_every_assertion(
    statuses: list[str], expected: str
) -> None:
    from science.deepswe.acceptance_templates.atomic_review import combined_status

    assert combined_status(statuses) == expected
    with pytest.raises(ValueError):
        combined_status([])


def test_evidence_can_quote_multiple_source_clauses_without_combining_them() -> None:
    from science.deepswe.acceptance_templates.generation import validate_evidence

    evidence = {
        "source_ids": ["s001", "s002"],
        "source_quotes": [
            {"source_id": "s001", "quote": "Each entry calls the factory."},
            {"source_id": "s002", "quote": "History restores direct children."},
        ],
        "support_explanation": "Entry and history behaviors are specified.",
    }
    validate_evidence(evidence, task())
    evidence["source_quotes"] = [
        {
            "source_id": "s001",
            "quote": "Each entry calls the factory. History restores direct children.",
        }
    ]
    with pytest.raises(ValueError):
        validate_evidence(evidence, task())


def test_review_payload_grades_text_without_unrendered_generator_explanations() -> None:
    from science.deepswe.acceptance_templates.evaluation import rendered_criteria

    assert rendered_criteria(
        [
            {
                "id": "c001",
                "text": "An observable criterion.",
                "bindings": [{"invented_policy": "trusted explanation"}],
                "source_ids": ["s999"],
                "template_ids": ["T31"],
            }
        ]
    ) == [{"id": "c001", "text": "An observable criterion."}]


def test_atomic_review_preserves_assertions_and_grades_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from science.deepswe.acceptance_templates.atomic_review import review_assertions

    captured = []

    def fake_complete(
        client: Any, messages: list[dict[str, str]], *, response_schema: dict[str, Any]
    ) -> dict[str, Any]:
        payload = json.loads(messages[1]["content"])
        captured.append(payload)
        if "assertions" in response_schema["properties"]:
            return {
                "assertions": [
                    {"text": "Each entry calls the factory."},
                    {"text": "The factory must return a new object."},
                ]
            }
        criteria = payload["criteria"]
        assert len(criteria) == 1
        row = criteria[0]
        return {
            "support": [
                {
                    "criterion_id": row["id"],
                    "status": "supported" if "a001" in row["id"] else "unsupported",
                    "source_ids": ["s001"],
                    "rationale": "Invocation is required; result identity is not.",
                }
            ]
        }

    monkeypatch.setattr(common, "complete_json", fake_complete)
    recorder = Recorder(None, tmp_path / "calls", provider="fake", model="fake")
    result = review_assertions(
        task(),
        {
            "id": "c001",
            "text": "Each entry calls the factory, which returns a new object.",
            "bindings": [{"invented": "explanation"}],
        },
        recorder,
    )
    assert result["derived_status"] == "partial"
    assert len(result["assertions"]) == 2
    assert "instruction" not in captured[0]
    assert all(
        set(row) == {"id", "text"}
        for payload in captured[1:]
        for row in payload["criteria"]
    )
    assert len(list((tmp_path / "calls").glob("*.json"))) == 3


def test_coverage_schema_constrains_namespaces_verdict_count_and_empty_lists() -> None:
    from science.deepswe.acceptance_templates.evaluation import coverage_response_schema

    schema = coverage_response_schema(
        [{"id": "v001"}],
        {
            "inventory": {"items": [{"id": "r001", "kind": "requirement"}]},
            "criteria": [{"id": "c001"}],
        },
    )
    array = schema["properties"]["coverage"]
    props = array["items"]["properties"]
    assert array["minItems"] == array["maxItems"] == 1
    assert props["reference_id"]["enum"] == ["v001"]
    assert props["inventory_requirement_ids"]["items"]["enum"] == ["r001"]
    assert props["criterion_ids"]["items"]["enum"] == ["c001"]
    empty = coverage_response_schema(
        [{"id": "v001"}], {"inventory": {"items": []}, "criteria": []}
    )
    assert (
        empty["properties"]["coverage"]["items"]["properties"]["criterion_ids"][
            "maxItems"
        ]
        == 0
    )


@pytest.mark.parametrize("decision", ["yes", "no", "unknown", "failed"])
def test_semantic_guard_confirms_or_retains_rejected_instances_without_blocking(
    decision: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from science.deepswe.acceptance_templates.generation import render_bindings
    from science.deepswe.acceptance_templates.guarding import confirm_guards

    catalog, response = _binding()
    catalog["templates"][0].update(
        prerequisites=[
            {
                "id": "ownership",
                "claim": "The entry owns the action",
                "reject_inference": "No global owner",
            }
        ],
        required_sentence="{owner} calls the factory.",
        optional_sentences=[],
    )
    inv = {
        "items": [
            {
                "id": "r001",
                "kind": "requirement",
                "text": "Each entry calls the factory.",
                "source_ids": ["s001"],
            }
        ]
    }
    before = copy.deepcopy(response["decisions"])

    def fake_complete(
        client: Any, messages: list[dict[str, str]], *, response_schema: Any
    ) -> dict[str, Any]:
        payload = json.loads(messages[1]["content"])
        assert "support_explanation" not in json.dumps(payload)
        if decision == "failed":
            raise RuntimeError("Provider unavailable")
        return {
            "decision": decision,
            "source_ids": ["s001"] if decision == "yes" else [],
            "source_quotes": [
                {"source_id": "s001", "quote": "Each entry calls the factory."}
            ]
            if decision == "yes"
            else [],
            "support_explanation": "Entry ownership is specified.",
        }

    monkeypatch.setattr(common, "complete_json", fake_complete)
    recorder = Recorder(None, tmp_path / "calls", provider="fake", model="fake")
    result = confirm_guards(task(), inv, catalog, response["decisions"], recorder)
    assert response["decisions"] == before
    assert result[0]["applicability"] == (
        "unknown" if decision == "failed" else decision
    )
    assert bool(render_bindings(result, catalog)) == (decision == "yes")
    if decision != "yes":
        assert result[0]["rejected_instances"]


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("select_one_source_value", "yes"),
        ("retain_both_in_order", "no"),
        ("combine_values", "no"),
        ("unspecified", "unknown"),
        ("uncertain", "unknown"),
    ],
)
def test_source_relation_controls_eligibility_without_showing_proposed_rule(
    relation: str, expected: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from science.deepswe.acceptance_templates.build_catalog import RELATIONS
    from science.deepswe.acceptance_templates.guarding import confirm_guards

    catalog, response = _binding()
    catalog["templates"][0]["prerequisites"] = [
        {"id": "collision_winner", **RELATIONS["T18"]}
    ]
    instance = response["decisions"][0]["instances"][0]
    instance["slots"].append(
        {"name": "winner_rule", "kind": "Outcome", "value": "LEAKED WINNER"}
    )
    inv = {"items": [{"id": "r001", "text": "Each entry calls the factory."}]}

    def complete(client: Any, messages: Any, **kwargs: Any) -> dict[str, Any]:
        payload = json.loads(messages[1]["content"])
        assert set(payload["operation_context"]) == {"owner"}
        assert "LEAKED WINNER" not in json.dumps(payload)
        assert "proposed_criterion" not in payload
        assert "required_relation" not in payload
        schema = kwargs["response_schema"]
        assert "decision" not in schema["properties"]
        assert relation in schema["properties"]["relation"]["enum"]
        return {
            "relation": relation,
            "support_explanation": "Source analysis.",
            "source_ids": ["s001"],
            "source_quotes": [
                {"source_id": "s001", "quote": "Each entry calls the factory."}
            ],
        }

    monkeypatch.setattr(common, "complete_json", complete)
    recorder = Recorder(None, tmp_path, provider="fake", model="fake")
    result = confirm_guards(task(), inv, catalog, response["decisions"], recorder)
    assert result[0]["applicability"] == expected
    raw = load_json(next(tmp_path.glob("*.json")))["response"]
    assert "decision" not in raw


def test_binding_reuse_allows_only_prerequisite_metadata_changes() -> None:
    from science.deepswe.acceptance_templates.apply_guards import compatible_catalog

    old = load_catalog(HERE / "data/catalog-evidence-v2.json")
    current = load_catalog(HERE / "catalog.json")
    compatible_catalog(current, old, digest(old))
    with pytest.raises(ValueError, match="fingerprint"):
        compatible_catalog(current, old, "wrong")
    changed = copy.deepcopy(current)
    changed["templates"][0]["required_sentence"] += " New obligation."
    with pytest.raises(ValueError, match="renderers"):
        compatible_catalog(changed, old, digest(old))


def test_quote_alignment_restores_source_markup_and_offsets() -> None:
    from science.deepswe.acceptance_templates.generation import validate_evidence

    instruction = "`ReuseValues`  appends old before new."
    example = {"source_spans": source_spans(instruction)}
    row: dict[str, Any] = {
        "source_ids": ["s001"],
        "source_quotes": [
            {"source_id": "s001", "quote": "ReuseValues appends old before new."}
        ],
        "support_explanation": "Ordering is stated.",
    }
    validate_evidence(row, example)
    quote = row["source_quotes"][0]
    assert quote["quote"] == instruction
    assert instruction[quote["start"] : quote["end"]] == quote["quote"]
    row["source_quotes"][0]["quote"] = "ReuseValues old config wins."
    with pytest.raises(ValueError):
        validate_evidence(row, example)


def test_recorder_keeps_model_response_separate_from_validator_annotations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_response = {"rows": [{"text": "Model text"}]}
    monkeypatch.setattr(
        common, "complete_json", lambda *args, **kwargs: copy.deepcopy(model_response)
    )

    def annotate(raw: dict[str, Any]) -> dict[str, Any]:
        raw["rows"][0]["id"] = "assigned-by-code"
        return raw

    recorder = Recorder(None, tmp_path, provider="fake", model="fake")
    result = recorder.call("stage", "prompt", {}, {}, annotate)
    assert result["rows"][0]["id"] == "assigned-by-code"
    assert load_json(tmp_path / "stage-0.json")["response"] == model_response


def test_recovered_cached_response_preserves_superseded_call_cost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter([{"value": "initial"}, {"value": "repaired"}])
    monkeypatch.setattr(
        common, "complete_json", lambda *args, **kwargs: next(responses)
    )

    def old_validator(raw: dict[str, Any]) -> dict[str, Any]:
        if raw["value"] == "initial":
            raise ValueError("Old formatting validator rejected it")
        return raw

    recorder = Recorder(None, tmp_path, provider="fake", model="fake")
    recorder.call("stage", "prompt", {}, {}, old_validator)
    resumed = Recorder(None, tmp_path, provider="fake", model="fake", resume=True)
    assert (
        resumed.call("stage", "prompt", {}, {}, lambda raw: raw)["value"] == "initial"
    )
    assert len(resumed.calls) == 2
    assert resumed.calls[1]["superseded"]
    assert all(call["cached"] for call in resumed.calls)


def test_guard_schema_places_analysis_before_decision() -> None:
    from science.deepswe.acceptance_templates.guarding import GUARD_SCHEMA

    assert list(GUARD_SCHEMA["properties"])[0] == "support_explanation"
    assert list(GUARD_SCHEMA["properties"])[-1] == "decision"


def test_generation_updates_preserve_the_exact_prior_reviewed_snapshot(
    tmp_path: Path,
) -> None:
    original = {
        "task_id": "x",
        "arm": "templates",
        "criteria": [{"text": "Original criterion."}],
        "residual_requirements": [],
    }
    save_generation(tmp_path, original)
    old_review = {"generation_sha256": digest(original)}
    write_json(tmp_path / "review.json", old_review)
    updated = {**original, "criteria": [{"text": "Updated criterion."}]}
    save_generation(tmp_path, updated)
    history = tmp_path / "history" / digest(original)
    assert load_json(history / "generation.json") == original
    assert load_json(history / "review.json") == old_review
    assert "Original criterion." in (history / "prompt.md").read_text()
    assert load_json(tmp_path / "generation.json") == updated


def test_checkpoint_bundle_includes_namespaced_review_calls(tmp_path: Path) -> None:
    from science.deepswe.acceptance_templates.bundle import bundle

    run = tmp_path / "run"
    path = run / "task/templates/review-calls/fingerprint/coverage-000-0.json"
    write_json(
        path, {"status": "completed", "stage": "coverage-000", "request_sha256": "test"}
    )
    manifest = bundle(run, tmp_path)
    assert manifest["file_count"] == 1
    assert manifest["files"][0]["path"].endswith("fingerprint/coverage-000-0.json")


def test_no_match_completes_prompt_and_retains_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from science.deepswe.acceptance_templates import generation

    inv = {
        "items": [
            {
                "id": "r001",
                "kind": "requirement",
                "source_ids": ["s001"],
                "text": "Each entry calls the factory.",
            }
        ]
    }
    monkeypatch.setattr(
        generation,
        "select",
        lambda *args: [{"requirement_id": "r001", "template_ids": []}],
    )
    recorder = Recorder(None, tmp_path / "calls", provider="fake", model="fake")
    result = generate(task(), inv, {"templates": []}, recorder, "templates", 6)
    save_generation(tmp_path, result)
    assert result["status"] == "completed"
    assert result["criteria"] == []
    assert result["residual_requirements"] == inv["items"]
    assert "Each entry calls the factory." in (tmp_path / "prompt.md").read_text()


def _scoring_fixture() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    generation = {
        "task_id": "example",
        "input_sha256": digest(task()),
        "arm": "templates",
        "inventory": {"items": [{"id": "r001", "kind": "requirement"}]},
        "criteria": [{"id": f"c{i:03}"} for i in range(1, 5)],
        "selections": [{"requirement_id": "r001", "template_ids": ["T33"]}],
        "decisions": [
            {"requirement_id": "r001", "template_id": "T33", "applicability": "yes"}
        ],
        "residual_requirements": [],
        "calls": [],
    }
    reference = {
        "label_status": "agent_authored",
        "validations": [
            {"id": f"v{i:03}", "template_groups": [["T33"]]} for i in range(1, 4)
        ],
    }
    review = {
        "task_id": "example",
        "arm": "templates",
        "review_status": "automated",
        "generation_sha256": digest(generation),
        "reference_sha256": digest(reference),
        "coverage": [
            {
                "reference_id": f"v{i:03}",
                "inventory_status": "full",
                "inventory_requirement_ids": ["r001"],
                "criterion_status": status,
                "criterion_ids": [f"c{i:03}"] if status != "missing" else [],
                "rationale": "specified outcome comparison",
            }
            for i, status in enumerate(["full", "partial", "missing"], 1)
        ],
        "support": [
            {
                "criterion_id": f"c{i:03}",
                "status": status,
                "source_ids": ["s001"],
                "rationale": "instruction support comparison",
            }
            for i, status in enumerate(
                ["supported", "partial", "unsupported", "uncertain"], 1
            )
        ],
    }
    return generation, reference, review


def test_score_keeps_partial_and_uncertain_out_of_strict_metrics() -> None:
    generation, reference, review = _scoring_fixture()
    result = score(generation, reference, review, task())
    assert result["strict_recall"] == pytest.approx(1 / 3)
    assert result["including_partial_recall"] == pytest.approx(2 / 3)
    assert result["strict_precision"] == 0.25
    assert result["missed_or_partial"] == 2
    assert (
        result["unsupported"] == result["partly_supported"] == result["uncertain"] == 1
    )
    assert result["review_status"] == "automated"
    assert result["all_reference_validations_present"] is False
    assert result["generation_usage"]["total_tokens"] is None


@pytest.mark.parametrize(
    "change", ["stale", "missing_reference", "missing_criterion", "duplicate"]
)
def test_scores_reject_stale_or_incomplete_reviews(change: str) -> None:
    generation, reference, review = _scoring_fixture()
    if change == "stale":
        generation["criteria"][0]["text"] = "Changed output"
    elif change == "missing_reference":
        review["coverage"].pop()
    elif change == "missing_criterion":
        review["support"].pop()
    else:
        review["coverage"].append(copy.deepcopy(review["coverage"][0]))
    with pytest.raises(ValueError):
        score(generation, reference, review, task())


def test_empty_denominators_do_not_become_perfect_scores() -> None:
    generation, reference, review = _scoring_fixture()
    generation["criteria"] = []
    reference["validations"] = []
    review.update(
        coverage=[],
        support=[],
        generation_sha256=digest(generation),
        reference_sha256=digest(reference),
    )
    result = score(generation, reference, review, task())
    assert result["strict_recall"] is None
    assert result["strict_precision"] is None
    assert aggregate([result])["templates"]["strict_precision"] is None


def test_checkpoint_reuse_requires_exact_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(1)
        return {"answer": "valid"}

    monkeypatch.setattr(common, "complete_json", complete)
    first = Recorder(None, tmp_path, provider="fake", model="a")
    first.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    cached = Recorder(None, tmp_path, provider="fake", model="a", resume=True)
    cached.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    assert len(calls) == 1 and cached.calls[0]["cached"] is True
    with pytest.raises(ValueError, match="stale checkpoint"):
        cached.call("stage", "prompt", {"instruction": "two"}, {}, lambda raw: raw)


def test_failed_call_records_stage_and_redacts_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "secret-test-token-abc"
    monkeypatch.setenv("DEEPINFRA_API_TOKEN", secret)

    def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError(f"empty response for bearer {secret}")

    monkeypatch.setattr(common, "complete_json", fail)
    recorder = Recorder(None, tmp_path, provider="fake", model="a")
    with pytest.raises(RuntimeError, match="bind failed"):
        recorder.call("bind", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    artifact = load_json(tmp_path / "bind-0.json")
    assert artifact["status"] == "failed" and artifact["stage"] == "bind"
    assert "empty response" in artifact["error_detail"]
    assert secret not in (tmp_path / "bind-0.json").read_text()
    assert len(recorder.calls) == 1


def test_resuming_a_repaired_stage_makes_no_new_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replies = iter([{"answer": "bad"}, {"answer": "good"}])
    count = []

    def complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        count.append(1)
        return next(replies)

    def validate(raw: dict[str, Any]) -> dict[str, Any]:
        if raw["answer"] != "good":
            raise ValueError("answer must be good")
        return raw

    monkeypatch.setattr(common, "complete_json", complete)
    first = Recorder(None, tmp_path, provider="fake", model="a", repairs=1)
    first.call("stage", "prompt", {"instruction": "one"}, {}, validate)
    resumed = Recorder(
        None, tmp_path, provider="fake", model="a", resume=True, repairs=1
    )
    assert resumed.call("stage", "prompt", {"instruction": "one"}, {}, validate) == {
        "answer": "good"
    }
    assert len(count) == 2
    assert all(row["cached"] for row in resumed.calls)


@pytest.mark.parametrize("recovery_succeeds", [True, False])
def test_transport_resume_preserves_failure_and_cannot_reset_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery_succeeds: bool
) -> None:
    count = []

    def complete(*args: Any, **kwargs: Any) -> dict[str, Any]:
        count.append(1)
        if len(count) == 1 or not recovery_succeeds:
            raise RuntimeError("stream ended before completion")
        return {"answer": "good"}

    monkeypatch.setattr(common, "complete_json", complete)
    first = Recorder(None, tmp_path, provider="fake", model="a", repairs=1)
    with pytest.raises(RuntimeError, match="stage failed"):
        first.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    original = load_json(tmp_path / "stage-0.json")
    resumed = Recorder(
        None, tmp_path, provider="fake", model="a", resume=True, repairs=1
    )
    if recovery_succeeds:
        resumed.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    else:
        with pytest.raises(RuntimeError, match="stage failed"):
            resumed.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
        with pytest.raises(RuntimeError, match="exhausted"):
            resumed.call("stage", "prompt", {"instruction": "one"}, {}, lambda raw: raw)
    assert len(count) == 2
    assert load_json(tmp_path / "stage-0.json") == original


def test_reference_drafting_excludes_patch_only_and_uncertain_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = tmp_path / "example/tests/test.patch"
    patch.parent.mkdir(parents=True)
    patch.write_text("PATCH-ONLY EVIDENCE")
    payloads = []

    def complete(client: Any, messages: Any, **kwargs: Any) -> dict[str, Any]:
        payloads.append(json.loads(messages[1]["content"]))
        return {
            "validations": [
                {
                    "behavior": "Each entry calls the factory.",
                    "source_ids": ["s001"],
                    "template_groups": [["T33"]],
                    "recoverability": "instruction",
                    "rationale": "explicit entry rule",
                },
                {
                    "behavior": "An unrequested internal exception code.",
                    "source_ids": [],
                    "template_groups": [],
                    "recoverability": "patch_only",
                    "rationale": "only the patch specifies it",
                },
                {
                    "behavior": "Recursive copy of history values.",
                    "source_ids": ["s002"],
                    "template_groups": [],
                    "recoverability": "uncertain",
                    "rationale": "hierarchy depth does not settle object copy depth",
                },
            ]
        }

    monkeypatch.setattr(common, "complete_json", complete)
    recorder = Recorder(None, tmp_path / "calls", provider="fake", model="a")
    result = draft_reference(
        task(),
        {
            "templates": [
                {
                    "id": "T33",
                    "name": "Factory",
                    "applies_when": "entry invokes a callable",
                }
            ]
        },
        recorder,
        tmp_path,
    )
    assert result["label_status"] == "model_draft"
    assert len(result["validations"]) == 1
    assert len(result["excluded_labels"]) == 2
    assert "PATCH-ONLY EVIDENCE" in str(payloads[0]["offline_patch_evidence"])


def _cohort(name: str) -> dict[str, Any]:
    generation, reference, review = _scoring_fixture()
    row = score(generation, reference, review, task())
    row["task_id"] = name
    other = {**row, "arm": "direct"}
    return {
        "run": {
            "provider": "fake",
            "model": "a",
            "catalog_sha256": "same",
            "batch_size": 6,
            "output_tokens": 100,
            "repairs": 1,
            "tasks": [name],
        },
        "scores": [row, other],
        "failures": [],
        "limitations": [],
    }


def test_combined_cohorts_require_matching_inputs_and_no_repeated_pairs() -> None:
    first = _cohort("first")
    second = _cohort("second")
    result = combine([first, second])
    assert result["paired_task_ids"] == ["first", "second"]
    assert result["paired_aggregate"]["templates"]["reference_count"] == 6
    assert "automated/agent_authored" in result["scores_by_evidence_level"]
    with pytest.raises(ValueError, match="repeated"):
        combine([first, first])
    second["scores"][0]["input_sha256"] = "different"
    with pytest.raises(ValueError, match="identical inputs"):
        combine([first, second])
