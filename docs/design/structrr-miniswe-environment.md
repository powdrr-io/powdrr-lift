# Structrr context in miniSWE observations

The miniSWE provider can now attach accepted Structrr context to source inspection.
`MiniSWEAgentProvider` accepts `source_reference_mode` (`off`, `observe`, or
`enforce`), a frozen `source_reference_catalog`, and `source_reader_limits`.
Legacy callers default to `off`. Both enabled modes require a catalog and a
diagnostics root before starting a worker. No enabled mode silently falls back
to a plain environment if initialization or the observation service fails.

This completes the environment portion of the source annotations plan.
Placement handoffs, final reference validation, and automatic Workrr rollout
remain separate steps. In particular, the current `enforce` setting requires
valid context initialization and prevents configuration downgrade; it does not
yet establish that the submitted code satisfies placement obligations.

## Package and trust boundary

The isolated agent distribution cannot import the private service package.
`powdrr_agent.execution.minisweagent_environment.StructrrLocalEnvironment`
therefore calls a local IPC bridge composed by `powdrr_local.structrr_context`.
The service owns `SourceObservationSession`, the workspace reader, and the
immutable catalog. Only observations cross the socket; the worker receives no
catalog path or serialized catalog. Provider launches clear inherited Structrr
configuration and set the attempt's own mode and endpoint.

The bridge uses a short Unix socket path in a private temporary directory. It
supports identity, bounded reader, observation, and submission requests, with
the existing bounded JSON frame transport. It offers no command execution or
write operation. This is local POSIX composition, suitable for a local/container
process sharing that socket mount. It is not a remote transport or a sandbox
against arbitrary process/filesystem access. A remote Harbor deployment still
needs to arrange the endpoint mount or supply a separate transport.

Use the isolated worker command:

```text
python -m powdrr_agent.execution.source_reader read retry.py --start 15 --end 30
python -m powdrr_agent.execution.source_reader search 'literal text' payments
python -m powdrr_agent.execution.source_reader context --ref=opaque:ID --offset=0
```

The configured environment passes `POWDRR_STRUCTRR_ENDPOINT` to shell children.
The proxy requires no service installation and emits only JSON. Continuations
in appended context use this importable agent command. The standalone service
reader remains available for local callers; an arbitrary service-reader envelope
is not automatically trusted by a worker session.

## Verification and delivery

The proxy returns a `structrr-issued-source-read-v1` receipt containing a unique
session ticket and the reader's `structrr-source-read-v1` observation. Each ticket
is associated with its exact request in a bounded session cache (128 entries).
Distinct requests retain distinct tickets even if their observations are identical
(for example, two searches with no matches). When output contains a receipt, the
service validates the observation's
schema and fingerprint, requires that the session issued it, and independently
repeats the bounded request against the current workspace and trusted catalog.
The entire observation must agree. This detects stale source, altered context,
changed search results, and worker-generated envelopes with recomputed hashes.
Expired/unissued envelopes require another reader call. No authority is inferred
from a self-consistent hash in command output.

Other output gets best-effort visible-marker discovery, including `path:line:`
prefixes. Distinct visible IDs share the reader's context budget, with a cap on
IDs matching the search limit. These observations always report visible-only
coverage and never claim complete enclosing scope context. A body-only `sed`
read with no marker remains unenriched. This implementation does not infer file
accesses from arbitrary shell commands or mediate every possible source read.

The environment executes the original command once and preserves its output
as a prefix, return code, timeout, and exception details. It appends a delimited
service-context section and retains that section separately. The session wraps
the configured model formatter: it formats/truncates the original source view
first, then appends the exact context to the corresponding text tool message.
The configured formatter must preserve one message per executed action; miniSWE's
padded unexecuted actions remain intact. The provider's real default formatter
and a deliberately tiny source template are tested with long accepted wording.
Context is repeated on later reads rather than deduplicated across turns.

Submission detection stays in miniSWE's base environment. The submission artifact
passes through unchanged, with a digest-only submission event and no appended
context. Normal, task-file, and resumed launches all use the same configuration
and formatting path. Explicit inherited `enforce` mode cannot be downgraded by
the loaded environment configuration.

## Attempt evidence

Enabled provider attempts write `<attempt>.source-events.ndjson` and
`<attempt>.source-events.inputs.json` in the diagnostics root. The input record
binds mode, base commit, catalog digest, and limits digest; reusing that attempt
path with different inputs fails. Each returned action and submission records
command/output digests, catalog/source digests, IDs, diagnostics, completeness,
enrichment mode, and a digest of the context section. Credentials and full
environment variables are not copied into these events. The exact appended
context is retained in the tool-message content and trajectory metadata.

These files are evidence produced by the local runner, not a claim of immutable
storage against an unrestricted worker. Protecting attempt artifacts and final
acceptance belongs to the later orchestration and validation work.
