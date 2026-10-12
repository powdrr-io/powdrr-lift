"""Offline, post-call manual assessment; never passed to either model."""

import hashlib
import json
import tarfile
from collections import Counter

from run import HERE, TASKS, finish

from science.deepswe.acceptance_templates.common import load_json, write_json

# Ordered exactly as the frozen scenario files, one note per scenario.
NOTES = {
    "koota-composite-trait-aspects": [
        (
            "Withheld requested identity/immutability behavior because "
            "implementation is absent; observation ends mid-word."
        ),
        (
            "Withheld nested-aspect deduplication; solution flattens and "
            "deduplicates constituent trait IDs."
        ),
        (
            "world.set operates on the world entity; world.set(E,A,data) is not "
            "the entity API."
        ),
        (
            "Wrong world.add signature; also overwrites existing Position.x, "
            "whereas solution preserves existing constituents."
        ),
        "Wrong world.add signature; use E.add(A(values)) for entity E.",
        (
            "Wrong world.remove/has signatures; world.has(E,trait) actually "
            "checks whether E is alive."
        ),
        "Correct: only the entity with all constituents matches the aspect query.",
        (
            "Wrong callback shape: aspect contributes one merged object inside "
            "the existing state array, not a flat state object. Query reference "
            "also lacks readEach."
        ),
        (
            "Unresolved; proposed state.x mutation ignores existing callback "
            "array wrapper, and query reference lacks updateEach."
        ),
        (
            "Withheld Not(A) behavior despite matching solution: entities "
            "missing any constituent match."
        ),
        (
            "Correct: Changed(A) requires all constituents and detects a change "
            "to a constituent."
        ),
        (
            "Unresolved; before-first-evaluation counterfactual misses tracker "
            "snapshot at query construction."
        ),
        (
            "Moves the Not observation to after removal; original when sequence "
            "ends after re-add, where Not does not match."
        ),
        (
            "Wrong entity API signatures and unclear re-evaluation of query "
            "result; unresolved."
        ),
        (
            "Wrong world.add signature; abstract onAdd observation plausible "
            "for corrected entity operation."
        ),
        (
            "Wrong world.set signature; abstract onChange observation plausible "
            "for corrected entity operation."
        ),
        "Wrong world.add/set/remove signatures; unresolved.",
        (
            "Correct distinct IDs/trait arrays; observe has a dangling suffix "
            "after its otherwise clear question."
        ),
        (
            "Wrong world.has(E,aspect) signature; it checks whether E is alive. "
            "Unresolved."
        ),
        (
            "Wrong world.add signature; abstract deduplicated trait behavior "
            "plausible for corrected entity operation."
        ),
        (
            "Wrong world.remove signature; abstract removal behavior plausible "
            "for corrected entity operation."
        ),
    ],
    "gql-incremental-graphql-delivery": [
        (
            "Correct data merge, but adds extensions=None without specifying "
            "HTTP vs websocket; websocket exposes control metadata there."
        ),
        (
            "Correct streamed data, but adds extensions=None or absent without "
            "a transport boundary; output also ends mid-word."
        ),
        "Incomplete starting payload ending data:{; outcome withheld correctly.",
        (
            "Incomplete starting payload; answer invents "
            "id=1/Alice/deferred_value rather than resolving supplied values."
        ),
        "Incomplete starting payload; answer invents data and extensions.",
        "Incomplete starting payload; answer invents data and has_next.",
        "Valid stream append at index 2 withheld because implementation is absent.",
        (
            "Deferred data at index equal to list length is not stream append; "
            "solution indexes existing element and raises here."
        ),
        "Correct: missing path merges at root and preserves existing user.",
        (
            "Correct streamed data but adds carried-forward has_next=True; "
            "absent current hasNext actually defaults False. "
            "Transport-unspecified extensions assertion also wrong."
        ),
        (
            "Path traverses None; solution raises rather than creating the "
            "missing profile container."
        ),
        (
            "Correct HTTP progress-only delivery: accumulated data preserved, "
            "has_next False, current errors/extensions None."
        ),
        (
            "Unresolved; proposed websocket transport data is wrong. "
            "Incremental-only transport message has data=None; session "
            "accumulates later."
        ),
    ],
    "bandit-structured-nosec-directives": [
        (
            "import subprocess is B404, not a B602 finding; uppercase directive "
            "assertion uses an invalid finding fixture."
        ),
        (
            "Neither import subprocess nor import os produces B602; next-line "
            "criterion assumes nonexistent findings."
        ),
        (
            "import subprocess is B404, not B602; conditional region behavior "
            "does not validate this fixture."
        ),
        (
            "import subprocess is B404, not B602; conditional next-line "
            "behavior does not validate this fixture."
        ),
        "subprocess.call('ls') without shell=True is B603, not B602.",
        (
            "eval(input()) is B307, not B602; additionally answer uses empty "
            "set for no-effect none, whereas solution uses None."
        ),
        (
            "eval(input()) is B307 and not selected by B6*; answer incorrectly "
            "suppresses both it and B603."
        ),
        (
            "eval(input()) is B307 and not selected by B602|B603; answer "
            "incorrectly suppresses it."
        ),
        (
            "Wrong subprocess finding ID; unresolved proposed answer also "
            "incorrectly calls B101|B102 an empty selector."
        ),
        (
            "Correct blanket begin suppresses B404 import finding; output has a "
            "dangling final suffix."
        ),
        "Correct unindented region remains active to EOF.",
        "Correct end closes region and later unmatched end is a no-op.",
        "Correct trailing text on nosec-end is ignored.",
        (
            "Correct indented region ends at dedent; statement before dedent "
            "suppressed, after dedent reported."
        ),
        (
            "Wrong: begin line 6 and end line 7 leave zero suppressed lines in "
            "statement 5-8; statement must be reported."
        ),
        (
            "Ignore-nosec conditional outcome correct, but fixture claims "
            "subprocess.call('ls') normally triggers B602 rather than B603."
        ),
        (
            "Correct blanket dominance over specific next-line selector, with "
            "blanket metric only. Actual subprocess finding is B603."
        ),
        (
            "eval(input()) is B307, not B602, and generated directive line 0 is "
            "invalid; metrics are conditional on an impossible stated fixture."
        ),
        (
            "eval(input()) is B307, not B602, and generated directive line 0 is "
            "invalid; metrics are conditional on an impossible stated fixture."
        ),
        (
            "eval(input()) is B307, not B602, and generated directive line 0 is "
            "invalid; metrics are conditional on an impossible stated fixture."
        ),
        (
            "eval(input()) is B307, not B602, and generated directive line 0 is "
            "invalid; metrics are conditional on an impossible stated fixture."
        ),
    ],
}
READY = {
    "koota-composite-trait-aspects": {
        "batch-04-scenario-2",
        "batch-05-scenario-2",
        "batch-07-s1",
    },
    "gql-incremental-graphql-delivery": {"batch-03-s3", "batch-05-scenario_1"},
    "bandit-structured-nosec-directives": {
        "batch-05-scenario-1",
        "batch-06-s1",
        "batch-06-s2",
        "batch-06-s3",
        "batch-06-s4",
        "batch-08-scenario_2",
    },
}
FACETS = {
    "k-assignment": (
        "Wrong receiver/signature and wrong existing-constituent overwrite in "
        "batch-03-s2."
    ),
    "k-traits-container": "No accepted outer-container immutability criterion.",
    "k-schema-container": "No accepted outer-container immutability criterion.",
    "k-nested-freeze": "No criterion separating outer freeze from nested values.",
    "k-nested-reference": "No fresh-container/shared-nested-reference criterion.",
    "k-callback-slot": "Wrong callback array wrapper in batch-04-scenario-3.",
    "k-set-equality": "No setter notification criterion for equal values.",
    "k-callback-subject": (
        "Wrong world operation receivers; no fully correct callback "
        "entity/payload criterion."
    ),
    "g-result-snapshot": "No earlier-result immutability criterion.",
    "g-stream-position": (
        "Append cases insufficient for addressed replacement, no shift, and padding."
    ),
    "g-progress-only": (
        "Partial: HTTP hasNext-only covered; empty incremental array not covered."
    ),
    "g-error-continuation": "No errors-plus-valid-incremental continuation criterion.",
    "g-errors-lifetime": (
        "HTTP omission covered once; no full earlier/current error lifetime "
        "distinction."
    ),
    "b-none-selection": (
        "Wrong finding fixture and None/empty-set sentinel in batch-03-scenario_1."
    ),
    "b-unrecognized-selection": "No correct unknown-selector/no-effect criterion.",
    "b-blanket-combination": "Covered: batch-08-scenario_2 blanket dominance/metric.",
    "b-compound-header": "No explicit header/body independence criterion.",
    "b-nested-begin-line": (
        "Wrong zero-active-lines suppression in batch-07-scenario_1; no nested "
        "begin-line rule."
    ),
}


def main() -> None:
    scenarios = []
    rows = []
    for task in TASKS:
        cases = load_json(HERE / "scenarios" / f"{task}.json")["scenarios"]
        assert len(cases) == len(NOTES[task])
        for case, note in zip(cases, NOTES[task], strict=True):
            primary = load_json(HERE / "results" / task / f"{case['id']}.json")
            future = load_json(HERE / "future/results" / task / f"{case['id']}.json")
            ready = case["id"] in READY[task]
            assert not ready or future["jev"] == "supported"
            rows.append(
                {
                    "task": task,
                    "id": case["id"],
                    "primary_emitted": bool(primary["rendered"]),
                    "future_emitted": bool(future["rendered"]),
                    "usable_future_criterion": ready,
                    "note": note,
                }
            )
            scenarios.append((task, case))
            for result in (primary, future):
                expected = (
                    f"Given {case['given']} When {case['when']} Then {result['then']}"
                )
                assert result["rendered"] == (
                    expected if result["jev"] == "supported" else None
                )
    # Refresh only the archive and summaries from frozen artifacts; no provider calls.
    finish(
        scenarios,
        [
            load_json(HERE / "results" / task / f"{case['id']}.json")
            for task, case in scenarios
        ],
    )
    with tarfile.open(HERE / "call-checkpoints.tar.gz") as archive:
        for row in load_json(HERE / "call-checkpoints.json")["checkpoints"]:
            stream = archive.extractfile(row["file"])
            assert stream is not None
            assert hashlib.sha256(stream.read()).hexdigest() == row["sha256"]
    write_json(
        HERE / "audit.json",
        {
            "method": (
                "Manual post-call comparison to solution patches and original "
                "API/plugin definitions; strict usable criterion checks include "
                "premise validity and all emitted assertions. Labels never enter "
                "model requests. Formatting suffixes noted separately."
            ),
            "limitations": (
                "Manual, not blinded; one sample per arm; incomplete original-code "
                "retrieval. Usability counts are not solution coverage or agent "
                "test-pass rates. Direct contradiction calibration does not "
                "validate scenario premises."
            ),
            "counts": {
                "scenarios": len(rows),
                "primary_emitted": sum(r["primary_emitted"] for r in rows),
                "future_emitted": sum(r["future_emitted"] for r in rows),
                "primary_usable": sum(
                    r["primary_emitted"] and r["usable_future_criterion"] for r in rows
                ),
                "future_usable": sum(r["usable_future_criterion"] for r in rows),
            },
            "future_usable_by_task": dict(
                Counter(r["task"] for r in rows if r["usable_future_criterion"])
            ),
            "scenarios": rows,
            "same_18_target_facets": FACETS,
            "excluded": [
                (
                    "g-extensions-lifetime: original reference mixed transport and "
                    "processed-result boundaries"
                ),
                (
                    "three unrequested-cap controls: not proposed to discovery; cannot "
                    "count withheld negatives"
                ),
            ],
        },
    )
    print(json.dumps(load_json(HERE / "audit.json")["counts"], indent=2))


if __name__ == "__main__":
    main()
