"""Matched adaptive diagnostic: specify completed-feature behavior explicitly."""

from concurrent.futures import ThreadPoolExecutor

from run import HERE, TASKS, finish, resolve

from science.deepswe.acceptance_templates.common import digest, load_json, write_json


def main() -> None:
    scenarios = [
        (task, row)
        for task in TASKS
        for row in load_json(HERE / "scenarios" / f"{task}.json")["scenarios"]
    ]
    definition = {
        "selection": "All frozen scenarios, not a gold-selected subset.",
        "change": (
            "Add explicit completed-feature framing to answer and review; no "
            "scenario, source, schema or model changes."
        ),
        "adaptive": True,
        "scenario_digest": digest(scenarios),
        "oracle_access": False,
    }
    target = HERE / "future/definition.json"
    if target.exists() and load_json(target) != definition:
        raise ValueError("changed diagnostic inputs")
    write_json(target, definition)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda job: resolve(job, future=True), scenarios))
    # Render separate diagnostic prompts; preserve all primary results/prompts.
    primary = [
        load_json(HERE / "results" / task / f"{row['id']}.json")
        for task, row in scenarios
    ]
    finish(scenarios, primary)
    for task in TASKS:
        original = (HERE / "prompts" / f"{task}.md").read_text()
        before, after = original.split("# Acceptance criteria\n", 1)
        _, tail = after.split("Original requirements remain", 1)
        criteria = dict.fromkeys(
            r["rendered"] for r in results if r["task_id"] == task and r["rendered"]
        )
        path = HERE / "future/prompts" / f"{task}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            before
            + "# Acceptance criteria\n\n"
            + "\n".join(f"- {c}" for c in criteria)
            + "\n\nOriginal requirements remain"
            + tail
        )
    write_json(
        HERE / "future/summary.json",
        {
            "scenarios": len(results),
            "resolved": sum(r["status"] == "resolved" for r in results),
            "supported": sum(r["jev"] == "supported" for r in results),
            "contradicted": sum(r["jev"] == "contradicted" for r in results),
            "unknown": sum(r["jev"] == "unknown" for r in results),
            "answer_failures": sum(r["status"] == "failed" for r in results),
        },
    )


if __name__ == "__main__":
    main()
