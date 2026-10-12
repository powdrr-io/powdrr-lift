"""Post-call manual source audit; never imported by the generation runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from collections import Counter
from pathlib import Path

from science.deepswe.acceptance_templates.common import digest, load_json, write_json

HERE = Path(__file__).resolve().parent
PREVIOUS = HERE.parent / "decision-instructions-v1"
K = "koota-composite-trait-aspects"
G = "gql-incremental-graphql-delivery"
B = "bandit-structured-nosec-directives"

# These labels were authored after calls, with the solutions and answers visible.
# "neither" means neither literal candidate is a safe complete criterion for
# its declared condition/scope. It is not a label saying the task is unsolvable.
ANNOTATIONS: dict[str, dict[str, tuple[str, str, str]]] = {
    K: {
        "d001": ("a", "solution.patch:33-40", "Nested aspect traits flatten."),
        "d002": (
            "b",
            "solution.patch:64-76",
            "Tag traits contribute no schema fields.",
        ),
        "d003": (
            "neither",
            "solution.patch:1069-1079",
            (
                "Fresh outer object uses Object.assign; nested references "
                "remain shared. A's blanket mutation-isolation claim is too "
                "broad; B's caching is false."
            ),
        ),
        "d004": (
            "a",
            "solution.patch:1082-1098",
            "Only supplied fields are distributed; omitted fields stay unchanged.",
        ),
        "d005": (
            "neither",
            "solution.patch:139-155;1101-1134",
            (
                "Internal addAspect and public entity.add return void. "
                "Neither boolean nor aspect-object return is correct."
            ),
        ),
        "d006": (
            "b",
            "solution.patch:495-519",
            "Aspect slot contains merged top-level constituent fields.",
        ),
        "d007": (
            "neither",
            "solution.patch:568-610",
            (
                "updateEach commits mutations to callback state; it does not "
                "consume a returned partial object. Both choices assume an "
                "unsupported return contract."
            ),
        ),
        "d008": (
            "a",
            "solution.patch:705-716",
            "Not(aspect) uses notAll, allowing partially present entities.",
        ),
        "d009": (
            "b",
            "solution.patch:659-663;723-734",
            "Changed aspect requires all-present and some constituent change.",
        ),
        "d010": (
            "a",
            "solution.patch:650-651",
            "Added detects incomplete-to-complete transition.",
        ),
        "d011": (
            "neither",
            "solution.patch:1174-1178",
            (
                "callback(entity, undefined), not sole entity argument or "
                "entity plus aspect."
            ),
        ),
        "d012": (
            "a",
            "solution.patch:1241-1251",
            (
                "A wrapper subscribes separately to each constituent; no "
                "per-operation deduplication."
            ),
        ),
    },
    B: {
        "d001": (
            "a",
            "solution.patch:336-343",
            "Absent/empty selector maps to blanket empty-set sentinel.",
        ),
        "d002": (
            "unscored",
            "solution.patch:546-596",
            (
                "Ill-posed overlap: an exact name without wildcard is not also "
                "an ID-prefix glob. The alternatives do not specify a concrete "
                "token distinguishing the alleged overlap."
            ),
        ),
        "d003": (
            "neither",
            "solution.patch:598-657",
            (
                "Actual precedence is negation, intersection, difference, then "
                "union. A incorrectly puts difference and union at the same "
                "precedence; B discards precedence."
            ),
        ),
        "d004": (
            "a",
            "solution.patch:189-193",
            "New region starts on the next physical line.",
        ),
        "d005": (
            "a",
            "solution.patch:219-223",
            "Indent-based pop occurs before recording the later line.",
        ),
        "d006": ("a", "solution.patch:227-230", "One end event pops one stack entry."),
        "d007": (
            "a",
            "solution.patch:690-720; original tester.py:127-144",
            (
                "Suppression combines finding line and any line in that "
                "statement's context range."
            ),
        ),
        "d008": (
            "a",
            "solution.patch:453-492",
            "Next-statement anchor skips ignorable and trivial grouping tokens.",
        ),
        "d009": (
            "a",
            "solution.patch:705-720",
            "Empty-set blanket sentinel dominates specific selections.",
        ),
        "d010": (
            "b",
            "solution.patch:344-347; original tester.py:78-97",
            (
                "Nonempty specific set remains specific even if it covers every "
                "enabled test; skipped_tests, not nosec."
            ),
        ),
    },
    G: {
        "batch-01-q1": (
            "a",
            "solution.patch:355-365",
            "Ordinary response without hasNext defaults to completed result.",
        ),
        "batch-01-q2": (
            "a",
            "solution.patch:575-601",
            "Yield follows a received transport result; no synthetic initial yield.",
        ),
        "batch-01-q3": (
            "a",
            "solution.patch:344-382;575-601",
            "Result errors are delivered rather than raised by this generator.",
        ),
        "batch-02-q1": (
            "neither",
            "solution.patch:368-382",
            (
                "An errors-only later payload retains accumulated data, which "
                "can be nonempty; neither universal None nor empty dict is "
                "correct."
            ),
        ),
        "batch-02-q2": (
            "neither",
            "solution.patch:287-309;355-365",
            (
                "Object-valued defer data merges existing keys; scalar field "
                "values replace. Top-level data responses replace accumulated "
                "data. Neither universal alternative binds these conditions."
            ),
        ),
        "batch-02-q3": (
            "neither",
            "solution.patch:585-591;648-659",
            (
                "Wire omission is not generator extensions=None: WebSocket adds "
                "hasNext/incremental metadata. Observation boundary repeats the "
                "previous experiment's flaw."
            ),
        ),
        "batch-02-q4": (
            "b",
            "solution.patch:287-309",
            "Deferred object fields merge into an existing parent object.",
        ),
        "batch-02-q5": (
            "neither",
            "solution.patch:244-252;299-309",
            (
                "Terminal null/missing value can be replaced; traversing a "
                "null/missing parent is not handled by create-or-skip. The "
                "question conflates them."
            ),
        ),
        "batch-03-q1": (
            "b",
            "solution.patch:280-284",
            "Stream items replace addressed positions without shifting.",
        ),
        "batch-03-q2": (
            "a",
            "solution.patch:290-294",
            "Object-valued root deferred data merges root fields.",
        ),
        "batch-03-q3": (
            "neither",
            "solution.patch:244-252;254-284",
            (
                "Integer list addressing is supported, but A adds a silent-skip "
                "guarantee for nonlists that the patch does not implement. B's "
                "dict conversion is also false."
            ),
        ),
        "batch-04-q1": (
            "neither",
            "solution.patch:244-252;312-318",
            (
                "Traversing null is different from assigning null at the "
                "terminal path. Patch supports terminal assignment, not "
                "arbitrary null-parent creation or skip. Routed r013 adds the "
                "traversing scope."
            ),
        ),
        "batch-04-q2": (
            "neither",
            "solution.patch:304-309",
            (
                "Existing dict plus dict merges; other values replace. Both "
                "universal choices omit this type condition."
            ),
        ),
        "batch-04-q3": (
            "a",
            "solution.patch:371-375",
            "Incremental array order governs processing.",
        ),
        "batch-04-q4": (
            "neither",
            "solution.patch:352-365",
            (
                "Presence of data without incremental does not fix has_next: an "
                "initial data response can explicitly have hasNext=True."
            ),
        ),
        "batch-05-q1": (
            "a",
            "solution.patch:368-382",
            "hasNext-only payload retains accumulated data and yields.",
        ),
        "batch-05-q2": (
            "neither",
            "solution.patch:371-382;599-601",
            (
                "All items are processed and errors combined before one result "
                "per payload. Both alternatives promise an early per-item yield."
            ),
        ),
        "batch-05-q3": (
            "a",
            "solution.patch:407-474",
            "HTTP multipart parts are processed individually.",
        ),
        "batch-05-q4": (
            "a",
            "solution.patch:635-659",
            "WebSocket recognizes incremental messages without data.",
        ),
        "batch-05-q5": (
            "a",
            "solution.patch:648-654",
            "Control metadata travels through ExecutionResult.extensions.",
        ),
        "batch-06-q1": (
            "a",
            "solution.patch:648-659",
            (
                "One ExecutionResult per next message retains data and control "
                "metadata (the latter in extensions)."
            ),
        ),
        "batch-06-q2": (
            "a",
            "solution.patch:635-659",
            "Incremental-only next message is accepted; transport result data is None.",
        ),
        "batch-06-q3": (
            "neither",
            "solution.patch:130-146;199-207; original dsl.py:1370",
            (
                "DSLFragment.ast_field is a FragmentSpreadNode, so defer "
                "decorates the spread, not the definition or each field."
            ),
        ),
        "batch-06-q4": (
            "a",
            "solution.patch:182-191",
            "Fragment-spread defer decorates that spread.",
        ),
        "batch-06-q5": (
            "a",
            "solution.patch:152-175",
            "Supplied label and initial_count become directive arguments.",
        ),
        "batch-06-q6": (
            "neither",
            "solution.patch:155-169",
            (
                "No-argument stream emits initialCount:0 and omits label; "
                "neither bare directive nor explicit null label is correct."
            ),
        ),
    },
}

# Full refers to the sampled facet, not full solution coverage. Partial means
# a nearby behavior is discussed without settling the full frozen distinction.
TARGETS: dict[str, tuple[str, str, list[str], str]] = {
    "k-assignment": (K, "missing", [], "No property-reassignment decision."),
    "k-traits-container": (K, "missing", [], "No constituent-list mutation decision."),
    "k-schema-container": (K, "missing", [], "No schema-container mutation decision."),
    "k-nested-freeze": (
        K,
        "missing",
        [],
        "No shallow-versus-recursive protection decision.",
    ),
    "k-nested-reference": (
        K,
        "missing",
        [],
        (
            "No reference-sharing decision; d003's isolation overclaim is not a "
            "correct clarification."
        ),
    ),
    "k-callback-slot": (
        K,
        "full",
        ["d006"],
        "Merged callback object rather than individual constituent objects.",
    ),
    "k-set-equality": (K, "missing", [], "No equal-value set notification decision."),
    "k-callback-subject": (
        K,
        "partial",
        ["d011"],
        (
            "Both options already use entity first and add wrong exact-arity "
            "alternatives; consensus withholds."
        ),
    ),
    "g-result-snapshot": (
        G,
        "missing",
        [],
        "No prior-result immutability or reference-lifetime question.",
    ),
    "g-stream-position": (
        G,
        "full",
        ["batch-03-q1"],
        (
            "Exact overwrite-versus-shift distinction is found, but both models "
            "choose shift."
        ),
    ),
    "g-progress-only": (
        G,
        "partial",
        ["batch-05-q1"],
        (
            "hasNext-only yield covered, empty incremental arrays not "
            "explicitly clarified."
        ),
    ),
    "g-error-continuation": (
        G,
        "full",
        ["batch-01-q3"],
        (
            "Result errors do not terminate delivery. Another generated "
            "criterion adds a contradictory early per-item yield."
        ),
    ),
    "g-errors-lifetime": (
        G,
        "partial",
        ["batch-01-q3", "batch-05-q2"],
        (
            "Questions concern delivery/continuation but do not exclude "
            "retaining earlier payload errors."
        ),
    ),
    "b-none-selection": (B, "missing", [], "No None-selector sentinel question."),
    "b-unrecognized-selection": (
        B,
        "missing",
        [],
        "No supplied nonempty selector resolving no tests question.",
    ),
    "b-blanket-combination": (
        B,
        "full",
        ["d009"],
        "Blanket dominance correctly settled.",
    ),
    "b-compound-header": (
        B,
        "missing",
        [],
        (
            "Multi-line statement d007 is not the compound-header versus "
            "independent-body distinction."
        ),
    ),
    "b-nested-begin-line": (
        B,
        "partial",
        ["d004"],
        (
            "New begin starts next line, but prior outer-region applicability "
            "on begin line is not clarified."
        ),
    ),
}


def main() -> None:
    definition = load_json(HERE / "experiment-definition.json")
    previous_manifest = load_json(PREVIOUS / "manifest.json")
    oracle = {r["id"]: r for r in load_json(PREVIOUS / "oracle.json")["labels"]}
    assert set(TARGETS) == {
        key
        for key, row in oracle.items()
        if row["expected"] != "unknown" and key != "g-extensions-lifetime"
    }
    provenance = {}
    for task in ANNOTATIONS:
        info = previous_manifest["tasks"][task]
        actual_sha = subprocess.check_output(
            ["git", "-C", info["base"], "rev-parse", "HEAD"], text=True
        ).strip()
        assert actual_sha == info["sha"]
        packet = load_json(HERE / "inputs" / f"{task}.json")
        for excerpt in packet["original_code"]:
            lines = (Path(info["base"]) / excerpt["file"]).read_text().splitlines()
            assert excerpt["text"] == "\n".join(
                lines[excerpt["start"] - 1 : excerpt["end"]]
            )
        root = Path.home() / "code/powdrr-deep-swe/tasks" / task
        assert (
            packet["instruction"].strip()
            == (root / "instruction.md").read_text().strip()
        )
        for name, expected in previous_manifest["task_hashes"][task].items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected
        previous_rows = load_json(PREVIOUS / "routing" / f"{task}.json")["inventory"][
            "items"
        ]
        assert packet["routed_clauses"] == [
            {k: r[k] for k in ("id", "text", "route", "source_ids")}
            for r in previous_rows
        ]
        prompt = (HERE / "prompts" / f"{task}.md").read_text()
        assert prompt.count("\n# ") == 5
        for row in packet["routed_clauses"]:
            if row["route"] in {"include", "include_prohibition"}:
                assert row["text"] in prompt
        provenance[task] = {
            "base_commit": actual_sha,
            "input_digest": digest(packet),
            "task_artifact_hashes": previous_manifest["task_hashes"][task],
            "previous_routing_sha256": hashlib.sha256(
                (PREVIOUS / "routing" / f"{task}.json").read_bytes()
            ).hexdigest(),
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        }
    write_json(HERE / "provenance.json", provenance)
    rows = []
    outcomes: dict[str, Counter[str]] = {
        m: Counter() for m in ("flash", "jev", "consensus")
    }
    quality: Counter[str] = Counter()
    for task, task_labels in ANNOTATIONS.items():
        questions = {
            r["id"]: r
            for r in load_json(HERE / "discovery" / f"{task}.json")["decisions"]
        }
        assert set(questions) == set(task_labels)
        packet = load_json(HERE / "inputs" / f"{task}.json")
        assert digest(packet) == definition["input_hashes"][task]
        for ident, (gold, evidence, note) in task_labels.items():
            result = load_json(HERE / "results" / task / f"{ident}.json")
            q = questions[ident]
            kind = (
                "valid_pair"
                if gold in {"a", "b"}
                else "missing_safe_complete_choice"
                if gold == "neither"
                else "ill_posed"
            )
            quality[kind] += 1
            scores = {}
            for model in outcomes:
                value = result[model]
                if gold == "unscored":
                    score = "unscored"
                elif gold == "neither":
                    score = (
                        "safe_withholding"
                        if value == "unknown"
                        else "unsafe_candidate_selected"
                    )
                else:
                    score = (
                        "correct"
                        if value == gold
                        else "missing"
                        if value == "unknown"
                        else "wrong"
                    )
                outcomes[model][score] += 1
                scores[model] = score
            if result["rendered"]:
                assert result["rendered"] == q["alternatives"][result["consensus"]]
                assert (
                    result["rendered"] in (HERE / "prompts" / f"{task}.md").read_text()
                )
            rows.append(
                {
                    "task_id": task,
                    "id": ident,
                    "question": q,
                    "expected": gold,
                    "pair_quality": kind,
                    "evidence": evidence,
                    "note": note,
                    "answers": {m: result[m] for m in outcomes},
                    "outcomes": scores,
                    "rendered": result["rendered"],
                }
            )
    indexed = {(r["task_id"], r["id"]): r for r in rows}
    targets = []
    for ident, (task, status, question_ids, note) in TARGETS.items():
        selected = [indexed[task, value] for value in question_ids]
        outcome = "partial" if status == "partial" else "missing"
        if status == "full":
            outcome = (
                "correct"
                if any(r["outcomes"]["consensus"] == "correct" for r in selected)
                else "wrong"
                if any(r["outcomes"]["consensus"] == "wrong" for r in selected)
                else "missing"
            )
        targets.append(
            {
                "id": ident,
                "task_id": task,
                "discovery": status,
                "decision_ids": question_ids,
                "added_criterion": outcome,
                "note": note,
            }
        )
    comparisons = {}
    for split in ("development", "evaluation", "all"):
        chosen = [
            r
            for r in targets
            if split == "all" or (r["task_id"] == K) == (split == "development")
        ]
        comparisons[split] = {
            "positive_targets": len(chosen),
            "previous_manual_revised_correct": sum(
                load_json(PREVIOUS / "results/revised" / f"{r['id']}.json")["consensus"]
                == oracle[r["id"]]["expected"]
                for r in chosen
            ),
            "discovery": dict(Counter(r["discovery"] for r in chosen)),
            "added_criterion": dict(Counter(r["added_criterion"] for r in chosen)),
        }
    write_json(
        HERE / "source-audit.json",
        {
            "authoring": (
                "Manual post-call source audit by agent with solution and "
                "answers visible; not blinded and not independent human "
                "adjudication."
            ),
            "scope": (
                "All 48 generated literal answer pairs; strict applicability "
                "and observation boundaries. Does not cover every solution "
                "behavior."
            ),
            "rows": rows,
        },
    )
    write_json(
        HERE / "target-mapping.json",
        {
            "excluded": [
                "g-extensions-lifetime",
                "three unsupported-cap probes (not proposed to discovery)",
            ],
            "targets": targets,
        },
    )
    rendered = [r for r in rows if r["rendered"]]
    summary = {
        "pair_quality": dict(quality),
        "answer_quality": {k: dict(v) for k, v in outcomes.items()},
        "rendered": {
            "count": len(rendered),
            "correct": sum(r["outcomes"]["consensus"] == "correct" for r in rendered),
            "wrong_valid_pair": sum(
                r["outcomes"]["consensus"] == "wrong" for r in rendered
            ),
            "unsafe_candidate": sum(
                r["outcomes"]["consensus"] == "unsafe_candidate_selected"
                for r in rendered
            ),
        },
        "previous_target_comparison": comparisons,
    }
    write_json(HERE / "evaluation.json", summary)
    # Verify archived request fingerprints, source/label boundary and credentials.
    checkpoints = load_json(HERE / "call-checkpoints.json")["checkpoints"]
    with tarfile.open(HERE / "call-checkpoints.tar.gz") as archive:
        for row in checkpoints:
            member = archive.extractfile(row["file"])
            assert member is not None
            raw = member.read()
            assert hashlib.sha256(raw).hexdigest() == row["sha256"]
            saved = json.loads(raw)
            if "request_sha256" in row:
                assert digest(saved["request"]) == row["request_sha256"]
                payload = json.loads(saved["request"]["messages"][1]["content"])
                assert (
                    not {
                        "expected",
                        "labels",
                        "evidence",
                        "solution",
                        "patch",
                        "oracle",
                        "previous_questions",
                    }
                    & payload.keys()
                )
    secrets = [
        v.encode()
        for k, v in os.environ.items()
        if len(v) >= 10
        and any(s in k.upper() for s in ("TOKEN", "API_KEY", "SECRET", "PASSWORD"))
    ]
    for path in HERE.rglob("*"):
        if path.is_file() and path.suffix in {".json", ".md", ".py"}:
            assert not any(secret in path.read_bytes() for secret in secrets), (
                "Credential scan failed"
            )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
