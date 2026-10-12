"""Probe the unchanged Jev review with supported observations and contradictions."""

import os

from run import HERE, REVIEW, REVIEW_SCHEMA, TASKS, checked, finish, prior

from science.deepswe.acceptance_templates.common import Recorder, load_json, write_json

CASES = [
    (
        TASKS[0],
        "batch-04-scenario-2",
        (
            "The query includes E2, E3 and E4 and excludes E1; entities with "
            "all constituents do not match."
        ),
    ),
    (
        TASKS[1],
        "batch-03-s3",
        (
            "The resulting data is {'age':30}; the existing user field is "
            "discarded rather than preserved by root-level merge."
        ),
    ),
    (
        TASKS[1],
        "batch-05-scenario_1",
        (
            "The second hasNext-only payload is skipped entirely and produces "
            "no yielded result."
        ),
    ),
    (
        TASKS[2],
        "batch-08-scenario_1",
        (
            "The directives suppress the findings even though ignore-nosec is "
            "enabled; no findings are reported."
        ),
    ),
]
FRAMING = (
    "Describe expected behavior AFTER the requested feature is implemented. "
    "The original code intentionally predates the feature: absence of its "
    "implementation is never evidence that a requested outcome is unresolved. "
    "The instruction defines the new behavior; existing code supplies API "
    "boundaries and compatible behavior, not a veto on implementing the task. "
)


def main() -> None:
    scenarios = [
        (task, row)
        for task in TASKS
        for row in load_json(HERE / "scenarios" / f"{task}.json")["scenarios"]
    ]
    found = {(task, row["id"]): row for task, row in scenarios}
    results = []
    for task, ident, wrong in CASES:
        original = load_json(HERE / "future/results" / task / f"{ident}.json")
        for variant, proposed in (("positive", original["then"]), ("negative", wrong)):
            directory = HERE / "calls/calibration" / task / ident / variant
            rec = Recorder(
                prior.ObservedJev(directory / "provider.json"),
                directory,
                provider="typesafe.ai",
                model=os.environ.get("SYSTEM_ONE_MODEL", "jev-latest"),
                resume=True,
                repairs=0,
            )
            answer = rec.call(
                "review",
                "Question:\n" + REVIEW + "\n\nInstructions:\n" + FRAMING + REVIEW,
                {
                    **prior.source_packet(task),
                    "scenario": found[task, ident],
                    "proposed_observation": proposed,
                },
                REVIEW_SCHEMA,
                lambda r: checked(r, REVIEW_SCHEMA),
            )
            results.append(
                {
                    "task_id": task,
                    "id": ident,
                    "expected": "supported"
                    if variant == "positive"
                    else "contradicted",
                    "actual": answer["choice"],
                    "proposed": proposed,
                }
            )
            print(ident, variant, answer["choice"], flush=True)
    write_json(
        HERE / "calibration.json",
        {
            "cases": results,
            "note": (
                "After observing universal acceptance, four manually selected "
                "direct-instruction contradictions and matched positive "
                "observations; labels never enter requests."
            ),
        },
    )
    baseline = [
        load_json(HERE / "results" / task / f"{row['id']}.json")
        for task, row in scenarios
    ]
    finish(scenarios, baseline)


if __name__ == "__main__":
    main()
