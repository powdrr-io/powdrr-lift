"""Capture installed GraphQL declarations in the isolated planning container.

Imports the dependency, not task implementation modules. This is evidence
gathering, not schema validation or implementation test execution.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from science.deepswe.acceptance_templates.common import load_json, write_json

CODE = """import ast, hashlib, inspect, json
from pathlib import Path
import graphql

def declaration(obj):
    path = Path(inspect.getfile(obj))
    return {"path": str(path), "start": inspect.getsourcelines(obj)[1],
            "source": inspect.getsource(obj),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

result = {"version": graphql.__version__, "types": {}, "directives": {}}
result["types"]["ExecutionResult"] = declaration(graphql.ExecutionResult)
for name, obj in sorted(vars(graphql).items()):
    if (inspect.isclass(obj) and not name.startswith("_")
            and "Incremental" in name and "Result" in name):
        result["types"][name] = declaration(obj)
    if isinstance(obj, graphql.GraphQLDirective):
        path = Path(inspect.getfile(type(obj)))
        source = path.read_text()
        assignments = [n for n in ast.parse(source).body
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in n.targets)]
        result["directives"][obj.name] = {
            "locations": [location.value for location in obj.locations],
            "path": str(path),
            "definition": (ast.get_source_segment(source, assignments[0])
                           if assignments else None),
            "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "arguments": {k: str(v.type) for k, v in obj.args.items()}}
print(json.dumps(result))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    container = load_json(args.setup / "environment.json")["container"]
    raw = subprocess.check_output(
        ["docker", "exec", container, "python", "-c", CODE],
        text=True,
        timeout=30,
    )
    write_json(args.output, json.loads(raw))


if __name__ == "__main__":
    main()
