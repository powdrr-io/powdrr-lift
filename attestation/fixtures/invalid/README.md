# Invalid YAML fixture cases

`verifier/tests/test_configuration.py` and the native protocol tests exercise
these rejection classes with the same compact input bytes: duplicate mapping
keys, aliases, explicit tags, unknown fields, non-string environment values,
duplicate step IDs, missing or multiple commands, floating-point timeouts, empty
or oversized step lists, forbidden environment variables and paths, unsupported
versions, oversized blobs, malformed dates, and key fingerprint mismatches.

The test names and expected stable error codes are the manifest for these cases.
