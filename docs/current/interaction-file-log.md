# Interaction file log

Production workflow execution records every model exchange and human prompt in
`.powdrr/interaction-log.json` at the repository root. The file is a JSON
document with this stable shape:

```json
{
  "schema_version": 1,
  "interactions": [
    {
      "timestamp": "2026-09-08T00:00:00+00:00",
      "actor": "llm",
      "input": [{"role": "user", "content": "..."}],
      "output": {"action": "next_step"}
    }
  ]
}
```

`actor` is either `human` or `llm`. Writes are serialized and use a temporary
file plus rename, so an interrupted process cannot leave a partially written
JSON document. Existing records are preserved and new interactions append in
execution order. A corrupt or incompatible log fails loudly instead of being
silently overwritten.
