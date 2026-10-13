"""Validate examples against directive locations from the installed dependency."""

import json
import subprocess
from typing import Any


def check_locations(
    container: str, query: str, definitions: dict[str, Any]
) -> list[dict[str, Any]]:
    code = """import json,sys
from graphql import parse
from graphql.language import Node
query,definitions=json.loads(sys.argv[1])
issues=[]
def walk(node):
    if not isinstance(node,Node):return
    location=node.kind.upper()
    if node.kind=="operation_definition":location=node.operation.value.upper()
    for directive in getattr(node,"directives",()) or ():
        name=directive.name.value
        allowed={l.replace(" ","_").upper()
                 for l in definitions.get(name,{}).get("locations",[])}
        if name in definitions and location not in allowed:
            issues.append({"directive":name,"location":location,"allowed":definitions[name]["locations"]})
    for key in node.keys:
        value=getattr(node,key,None)
        for child in value if isinstance(value,(list,tuple)) else (value,):walk(child)
walk(parse(query))
print(json.dumps(issues))"""
    result = subprocess.check_output(
        [
            "docker",
            "exec",
            container,
            "python",
            "-c",
            code,
            json.dumps([query, definitions]),
        ],
        text=True,
        timeout=30,
    )
    return json.loads(result)
