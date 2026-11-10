# Structrr source reader

The service-owned `SourceReader` reads Python source against a frozen
`ReferenceCatalog`. It never imports candidate code or executes search text.
The compatibility command is `python -m powdrr_lift.structrr.source_reader`:

```text
read relative/file.py --start 10 --end 30
search 'literal text' relative/directory
context --ref=opaque:ID --offset=0
```

The caller supplies `POWDRR_STRUCTRR_WORKSPACE` and
`POWDRR_STRUCTRR_CATALOG` (a serialized catalog file). Optional
`POWDRR_STRUCTRR_READER_LIMITS` contains the versioned `ReaderLimits` JSON.
There are no command flags for choosing another workspace or catalog. These
environment variables are configuration, not isolation or authorization. The
miniSWE environment integration remains a separate implementation step and must
independently verify the observation's source bytes and trusted catalog.

## Observation contract

`structrr-source-read-v1` separates original decoded physical source lines from
line labels, annotation locations, resolution statuses, and context pages.
CRLF and CR endings are preserved. Digests identify the exact bounded byte
snapshot used for hashing, decoding, and scope indexing. A session caches at
most 64 indexes by relative path and source digest; timestamps never establish
cache validity. The serialized envelope fingerprint detects accidental changes;
it does not authenticate an untrusted producer.

Body-only reads inherit all applicable module, class, function, and block
annotations. Each distinct ID resolves once per observation, with every
contributing location retained. Invalid Python can still return original source,
but never claims complete inherited scope context. Unknown and inactive IDs
produce explicit incomplete context, preserving lifecycle information and
successors without redirecting IDs. Diagnostics conservatively prevent claims
of completeness.

Context is deterministic JSON of the catalog resolution. Pages contain slices
of that rendering, preserving exact statement and rationale strings when
reassembled and decoded. The character budget applies to the sum of page text
in one observation; envelope metadata and source lines are separate. Overflow
lists remaining IDs and argument-vector continuation commands. Offsets are
character offsets in this renderer revision. A continuation uses the same
catalog and limits, identified in each envelope.

Defaults are 1 MiB per file, 200 source lines, 50 matched lines per search, and
16,000 context characters. Oversized read ranges fail explicitly and request a
smaller range, including implicit ranges through end of file. Search returns
one excerpt per matching physical line, ordered by relative path and line.
Exceeding either search or source-line limits reports incomplete results.
Exit codes are 0 for complete, 1 for incomplete/error observations, and 2 for
invalid startup configuration or command usage. Startup failures use
`structrr-source-reader-initialization-error-v1`.

## Filesystem boundary

Reads reject absolute paths, traversal, Windows drive paths, non-Python paths,
escaping symlinks, nonregular files, binary NULs, and decoding failures. Internal
file links are resolved within the workspace. POSIX descriptor-relative opens
with `O_NOFOLLOW` protect the resolved traversal from component replacement;
platforms without this facility return an explicit unsupported-filesystem error.
File reads stop at the byte limit plus one byte for overflow detection.

Recursive search skips `.git`, `.venv`, `.worktrees`, `node_modules`,
`__pycache__`, `.mypy_cache`, `.pytest_cache`, and `.ruff_cache`. Other directory
symlinks are skipped with diagnostics. Unreadable or oversized candidate files
also make search incomplete. The limits digest includes these exclusions and
the renderer revision. Search discovery is not a transactional filesystem
snapshot; each excerpt identifies its own bounded file snapshot.
