# Local Validation Attestation Implementation Plan

Status: proposed implementation contract

Date: 2026-10-09

## 1. Outcome and scope

Build a local validation provider that coding agents can invoke against an exact
Git commit. The provider executes the repository's approved validation policy and
returns signed evidence. GitHub accepts that evidence only when an allowed
provider for the PR author signed it, it covers the current PR head, and its
observed results satisfy the current policy. Expensive PR validation starts only
after this decision succeeds.

This document specifies the complete v1, including installation, enrollment,
execution, signing, PR transport, GitHub enforcement, agent integrations, release,
and recovery. Implement the PRs in section 12. Acceptance criteria are mandatory;
a prototype that signs caller-supplied results does not satisfy this plan.

Navigation:

- [Trust assumptions](#13-security-guarantee-and-explicit-limits)
- [Configuration contracts](#4-configuration-contracts)
- [GitHub enforcement](#9-github-trust-and-inexpensive-verification)
- [Implementation PRs and acceptance criteria](#12-implementation-pr-sequence)
- [Adversarial validation matrix](#13-end-to-end-and-adversarial-validation-matrix)

### 1.1 Agreed product requirements

- Start with macOS developer machines and GitHub.com repositories.
- Agents retain their ordinary setup and can request execution outside their
  sandbox. They invoke the provider through a CLI.
- Validation consumes local compute and permits ordinary network access.
- One committed file describes the validation sequence, including arbitrary
  command arrays, Make targets, and explicitly declared shell scripts.
- A second committed file maps GitHub users to public keys for their authorized
  providers, following the ownership and review pattern of CODEOWNERS.
- Initial onboarding provisions a provider and registers its public key once.
- Attestations travel in PR descriptions or comments, without creating a commit.
- Evidence for an earlier commit cannot unlock validation for a newer PR head.
- Enrollment changes become effective after trusted review and merge.

### 1.2 Concrete v1 implementation decisions

These choices fill in details not settled in the design conversation. Implement
them as written unless the user changes the requirement.

| Area | Decision |
| --- | --- |
| macOS support | macOS 14 or later; Apple Silicon and Intel |
| Native components | Swift 6 language mode; signed native CLI and launch daemon |
| Portable verifier | Independent Python 3.12+ package, with PyYAML and cryptography |
| Validation policy | `.github/local-validation.yaml` |
| Authorization registry | `.github/attestors.yaml` |
| Signature | Ed25519 using established crypto libraries |
| Envelope | DSSE containing an in-toto Statement v1 and a custom predicate |
| Provider keys | Provisioned by the protected service; root-owned software key storage in v1 |
| Execution | One active job per machine, under a dedicated unprivileged account and inherited macOS App Sandbox |
| PR transport | Inline signed envelope, with a compact machine-readable marker |
| GitHub verification | Trusted GitHub Actions workflow; check publication through a dedicated GitHub App |
| GitHub App secrets | Protected Actions environment restricted to the trusted default branch |
| Initial trusted target | Repository default branch only; additional targets require explicit later support |
| Freshness | 24 hours maximum by default; 5 minutes permitted clock skew |
| Automatic retries | None; each explicit retry creates a new run ID |
| Merge queues | Enforced v1 adoption blocks when a merge queue is enabled; report-only mode preserves existing queue CI |

Use the repository's existing shared Python environment for implementation checks,
as AGENTS.md requires. That development convenience is separate from the product's
protected execution environment. The production daemon must never depend on this
checkout's Python environment or editable installation.

### 1.3 Security guarantee and explicit limits

The v1 guarantee is:

> An agent running as the ordinary developer user, including outside its sandbox,
> cannot fabricate the provider's observed process outcomes, substitute an
> uncommitted workspace for the requested commit, extract its signing key, or
> obtain a passing signature without the required command sequence executing.

Trust the macOS administrator, OS, approved installed provider, GitHub, repository
administrators, reviewed default branch, and protected verifier workflow. Treat
the CLI caller, local checkout, proposed commit, repository execution code,
command output, dependency installation scripts, and PR text as untrusted.

Unrestricted sudo/root access and repository administration are outside this
guarantee. A host administrator can subvert a conventional local observer, and a
repository administrator can change GitHub enforcement. Do not describe this
system as secure against either actor. If the user requires that stronger threat
model, stop implementation of this architecture and redesign the execution trust
boundary; a remote signing endpoint alone is insufficient.

The attestation establishes command execution and observed exit criteria. It does
not establish test quality, coverage, or the honesty of arbitrary repository code.
For example, the approved command `make test` can execute a Makefile modified by
the PR. Protecting the policy file alone does not freeze that target's behavior.
Changes to tests, Makefiles, and scripts remain visible and subject to review.
Do not infer trustworthy test counts from caller-controlled JUnit or stdout.

This distinction must appear in product documentation and check summaries. It is
not an implementation bug that can be solved by signing more fields. Requiring
independently protected test implementations is a separate future policy feature.

## 2. Repository placement and component ownership

Implement this as a standalone tool housed in this repository. Keep its security
boundary independent of Powdrr's workflow runtime and existing validation reports.

```text
attestation/
  README.md
  schemas/
    policy-v1.schema.json
    attestors-v1.schema.json
    predicate-v1.schema.json
    ipc-v1.schema.json
  fixtures/
    valid/
    invalid/
    crypto/
  macos/
    Package.swift
    Package.resolved
    Sources/AttestationProtocol/
    Sources/AttestationDaemon/
    Sources/AttestationCLI/
    Sources/AttestationWorker/
    Tests/
    packaging/
  verifier/
    pyproject.toml
    src/powdrr_attestation/
    tests/
  github/
    verify-pr.py
    report-ci.py
    workflows/
  skills/
    attested-local-validation/SKILL.md
    references/
  examples/
    local-validation.yaml
    attestors.yaml
    CODEOWNERS
  scripts/
    verify-all.sh
    macos-security-smoke.sh
```

`attestation/github/workflows/` holds distributable templates. Install actual
workflows into `.github/workflows/` only during the adoption PR, after the tool
works. Do not replace current repository CI while the provider is incomplete.

| Component | Responsibilities | Forbidden responsibilities |
| --- | --- | --- |
| CLI | Prepare source transport, request jobs, show progress, export evidence, publish metadata | Choose authoritative outcomes or sign manifests |
| Daemon | Authenticate caller UID, resolve trusted policy, verify Git objects, supervise execution, construct and sign evidence | Execute repository commands with controller privileges |
| Worker | Run setup and validation commands in the private job workspace | Access signing state, GitHub controller credentials, or daemon administration |
| Portable verifier | Parse, authorize, verify signatures, evaluate policy and freshness | Execute PR code or trust keys supplied by the envelope |
| GitHub controller | Fetch current PR and trusted files, publish App checks, dispatch CI | Install dependencies or run scripts from PR head |
| CI validation job | Run the existing integration checks on the selected PR revision | Access App private key or mint accepted gate results |
| Agent skill | Drive the CLI and explain results | Supply evidence of execution or modify trust state |

Existing `src/powdrr_lift/workrr/coding_agent_validation.py` can inform the readable
failure model. Its reports are generated in an agent-accessible environment and
are not acceptable signed evidence. A later Powdrr integration may invoke the CLI;
it must not convert those existing reports into trusted attestations.

## 3. End-to-end workflows

### 3.1 Repository setup

1. A human maintainer installs reviewed provider and verifier releases.
2. Commit the policy, an initial attestors registry, CODEOWNERS coverage, and
   trusted workflow templates using existing repository review requirements.
3. Create the GitHub App and protected environment described in section 9.
4. Test in report-only mode. Existing CI continues to run normally in this stage.
5. Enroll at least one maintainer and prove the complete happy and rejection paths.
6. Enable required checks and switch official PR CI to gated dispatch atomically.

Do not enable an unsatisfiable required check before the initial key is enrolled.

### 3.2 Developer onboarding

1. Install the signed macOS package through a human-authorized installer.
2. Provision a provider profile that binds a local macOS UID to a GitHub account
   ID. The service generates its own key and never imports an agent-supplied key.
3. Human-authorize the GitHub identity and repository read credential, when the
   repository is private. Administrative onboarding is not a callable run IPC
   method.
4. Export a public enrollment record containing GitHub user ID, readable login,
   provider ID, public key, fingerprint, and provider release.
5. An already enrolled maintainer opens and validates the PR adding that record
   to `.github/attestors.yaml`. A maintainer verifies the trusted installation and
   ownership of the enrollment record rather than accepting arbitrary pasted keys.
6. After reviewed merge, the new developer runs `powdrr-attest doctor` and a sample
   validation. Their subsequent development PRs use their enrolled key.

For a single-developer repository, perform initial registration before enabling
the gate. Key replacement uses the same reviewed process. Losing the only key
requires a documented human administrative recovery, never automatic enrollment.

### 3.3 Normal agent development

1. Finish edits and create the final commit, including any required changelog.
2. Invoke `powdrr-attest run --repo . --github-repo OWNER/REPO --commit HEAD --wait`.
3. The daemon independently resolves the repository identity and trusted policy,
   imports and verifies the requested Git revision, and runs all required steps.
4. The CLI returns readable results, JSON status, and a path to the signed envelope.
5. On failure, fix the change, create a new commit, and run again.
6. On success, push that exact commit and create or update the PR.
7. Invoke `powdrr-attest publish --repo . --pr NUMBER --run RUN_ID --location comment`.
8. GitHub re-verifies against the current PR author, head, policy, and registry.
9. Only an accepted result starts the official expensive CI workflow.

Publishing never commits, amends, pushes, or merges. The skill can use the agent's
ordinary Git and PR tools for those separately authorized operations. If HEAD
changes after validation, publishing reports stale evidence and requires rerun.

### 3.4 Policy changes

The policy used for a PR is the current policy on its trusted target branch,
fetched independently by the daemon. A proposed policy file never governs its own
PR's gate. This makes policy updates implementable: their PR runs the old approved
policy; after reviewed merge, subsequent runs use the new policy.

The manifest records both the effective policy digest and the candidate commit's
policy digest, or null if that candidate file is absent. GitHub compares the
effective digest against the current approved file. It does not require candidate
and approved bytes to match. Display proposed policy changes to reviewers.

A change to approved policy bytes invalidates older attestations even if the head
SHA is unchanged. A base-branch advance with unchanged policy does not by itself
invalidate head-only local evidence; integration CI handles the merged result.

## 4. Configuration contracts

### 4.1 Validation policy

```yaml
version: 1
freshness:
  max_age_seconds: 86400
environment:
  toolchain: python312-uv-v1
  network: allowed
defaults:
  timeout_seconds: 1800
steps:
  - id: setup
    argv: [uv, sync, --locked, --extra, dev]
  - id: tests
    argv: [make, test]
    timeout_seconds: 3600
  - id: quality
    shell: |
      make format-check &&
      make lint &&
      make typecheck
    cwd: .
    env:
      CI: "1"
```

Define these semantics in the schema and both parsers:

- UTF-8 YAML with `version: 1`; maximum raw size 64 KiB and 32 steps.
- Reject unknown fields, duplicate mapping keys, aliases, anchors, custom tags,
  non-string environment values, duplicate IDs, and empty step lists.
- IDs match `[a-z][a-z0-9_-]{0,63}` and are unique.
- A step has exactly one of `argv` or `shell`. `argv` is a nonempty array of
  nonempty strings. `shell` is a nonempty string, at most 16 KiB.
- Every step is required, runs sequentially, and succeeds only on normal exit 0.
  There is no `continue_on_error`, caller-selected subset, allowed failure,
  caller-supplied command override, or implicit retry in authoritative mode.
- Shell scripts run using `/bin/sh -eu -c` with sanitized environment. Shell
  command success follows shell semantics; users must use explicit chaining when
  appropriate. Do not claim individual commands inside a shell step were observed
  separately.
- Timeouts are integer seconds in `[1, 14400]`; default is 1800.
- `cwd` defaults to `.` and must resolve inside the job workspace, including
  after setup steps. Reject traversal and escapes through symlinks.
- `env` overlays only the worker environment. Reject overrides of service
  credentials, launch variables, `DYLD_*`, `LD_*`, `PATH`, `HOME`, and reserved
  `POWDRR_ATTEST_*` variables. `VIRTUAL_ENV`, `PYTHONPATH`, and temporary directory
  locations are managed by the runner and cannot select agent-owned paths.
- `environment.toolchain` identifies a locally installed, administrator-approved
  bundle. Unknown or missing toolchains block execution. Repositories cannot
  configure arbitrary executable search paths or host mounts.
- Network is allowed in v1. Do not accept `network: denied` unless it is actually
  enforced. Record permitted network access; do not claim hermetic execution.
- `max_age_seconds` is in `[300, 604800]`; hard verifier cap is seven days. Clock
  skew is fixed at five minutes and cannot be enlarged by a proposed policy.
- Required setup belongs in the same sequence and is attested as a required step.
- There is no repository-configurable arbitrary root operation, secret injection,
  plugin load, or path outside the managed execution environment.

Hash the exact approved blob bytes using SHA-256. YAML formatting changes therefore
invalidate evidence intentionally. Preserve line endings; do not reserialize
before hashing. Parsed policy is typed and normalized only for execution.

### 4.2 Attestors registry

```yaml
version: 1
users:
  example-developer:
    github_user_id: 12345678
    providers:
      - provider_id: macbook-01
        key_id: sha256:PUBLIC_KEY_FINGERPRINT
        algorithm: ed25519
        public_key: BASE64_RAW_32_BYTE_PUBLIC_KEY
        status: active
        not_before: "2026-10-09T00:00:00Z"
```

- The username is a readable label. Match the numeric GitHub account ID obtained
  from the PR API, never a Git commit's author field or PR text.
- Permit multiple providers per account. Reject duplicate account IDs, key IDs,
  and provider IDs within an account. A key cannot be assigned to multiple users.
- Public keys are standard base64 of the raw 32-byte Ed25519 public key. Compute
  `key_id = "sha256:" + lowercase_hex(SHA256(raw_public_key))`; labels supplied by
  the caller cannot choose a different key identity.
- `status` is `active` or `revoked`. Optional `not_after` and required `not_before`
  use UTC timestamps. Revocation takes precedence over an earlier valid signature.
- Reject malformed dates, unsupported algorithms, unknown schema versions, YAML
  hazards listed above, and registry sizes over 256 KiB.
- Read the file from the current trusted target branch of the base repository,
  not the head branch, fork, synthetic merge tree, or local remote-tracking ref.
- Only reviewed merge authorizes a new key. Current registry state decides
  acceptance; possession of an enrollment snippet does not.

Cover the policy, registry, CODEOWNERS itself, verifier source or pinned release
reference, and privileged workflow templates with required owner review. Update
the repository rules to enforce that review and disable agent-accessible bypass.

## 5. Native service and source preparation

### 5.1 Installation and identity separation

Install a signed package containing immutable native executables and a system
launch daemon. Use a root-owned installation directory and controller state under
`/Library/Application Support/PowdrrAttest/`, with daemon configuration under
`/Library/LaunchDaemons/`. Never load executable provider code from a repository,
Homebrew prefix writable by the agent, user site-packages, or caller PATH.

Use a dedicated hidden account `_powdrrattest` for repository commands. It has no
interactive login, sudo rights, access to the developer's home, or access to
controller state. The controller performs privileged setup and supervision, but
all repository commands execute only after dropping supplementary groups, GID,
and UID to that worker. Verify the drop succeeded before exec.

UID separation alone does not deny reads of world-readable developer files. Run
the worker through a signed App Sandbox bootstrap with container-confined file
access and permitted network client/server access. Place job data in that worker
app's container under the dedicated account's home; the controller prepares it
without granting the developer UID access. Child tools must inherit the sandbox.
Use Apple's supported sandbox inheritance mechanism and explicitly audit/sign
bundled child tools as needed. Do not disable isolation to make a tool work, rely
on undocumented sandbox profiles as the sole production boundary, or grant broad
home-directory access. PR 03 must demonstrate this mechanism on real macOS before
the rest of the provider is built; inability to support ordinary Python/Make/shell
children safely is a blocking architecture finding, not permission to ship a
weaker security profile. The trusted bootstrap has no signing authority, closes
its private control descriptors before launching repository children, and reports
OS-observed child results to the controller.

Create a root-controlled IPC socket directory. Authenticate peer credentials from
the OS, not a UID in request JSON. Only provisioned developer UIDs can request
runs. The worker UID is explicitly rejected. Administration, enrollment, key
rotation, upgrades, and uninstallation use the human-authorized installer path,
not ordinary run IPC.

Implement root-side parsing and file operations with bounded input, typed records,
descriptor-based containment, no caller-selected destinations, and no shell
interpolation. Apply code signing, hardened runtime, and available launch/library
constraints. Treat the privileged native implementation as security-sensitive
code requiring independent review before release.

### 5.2 Source transport and verification

The unprivileged CLI resolves `--commit` once into a full commit ID and exports a
Git bundle. The daemon consumes the bundle as untrusted bytes; it does not trust
the CLI's report of what the bundle contains. Stream bundle bytes over IPC or
through a daemon-created bounded spool, never follow a caller-controlled output
path as root.

Use an installed trusted Git executable with an empty home, disabled system/user
configuration, disabled hooks and replacement objects, and no inherited Git or
dynamic-loader environment. Import into a fresh daemon-owned bare repository,
validate objects using strict integrity checks, then resolve the exact requested
commit and tree. Do not use local clone alternates, checkout hooks, filters, or
files from the agent's `.git/config`.

Materialize committed files into a private job workspace inaccessible to the
developer UID. Preserve executable bits. Reject absolute paths, traversal,
special files, `.git` entries, filesystem name collisions, and symlinks that resolve
outside the workspace. Perform extraction without traversing symlinks as root.

Fail explicitly on submodules and LFS pointer files in v1 (`UNSUPPORTED_SOURCE`).
Do not silently omit them or claim they were tested. Supporting either requires a
separate source-resolution extension with fetched-object verification and manifest
materials. This limitation must appear in setup diagnostics before adoption.

The source snapshot starts with the verified committed tree. Build and validation
commands may create generated files and change their own workspace; record this
as ordinary repository execution rather than claiming source stayed immutable.
External developer processes cannot modify the job workspace. Tests that alter
their own behavior remain within the semantic limitation in section 1.3.

### 5.3 Trusted policy resolution and private repositories

The profile records enrolled GitHub user ID and allowed repository IDs. Resolve
the requested `OWNER/REPO` through the GitHub API and verify its numeric repository
ID. A local remote URL is only a convenience hint.

Fetch current default-branch commit and approved policy blob over authenticated
HTTPS using controller-owned credentials. Disable caller proxy configuration,
restrict credentials to GitHub API requests, and do not follow redirects to
arbitrary credential recipients. A run before push can use local candidate objects
because only policy and repository identity need to exist remotely.

Public repositories may use unauthenticated API reads within rate limits. Private
repositories require a separately provisioned read-only credential protected from
the worker and caller. Do not inherit `GH_TOKEN`, the user's Git credential helper,
SSH agent socket, or browser session into the daemon or worker.

Pin the fetched policy bytes for the entire run. GitHub will reject them if the
approved policy changes before submission. GitHub/API unavailability blocks new
authoritative runs in v1; do not fall back to an agent-owned stale policy cache.

## 6. Execution, observations, and key management

### 6.1 Managed execution environment

Support an initial administrator-installed toolchain with a trusted Git, Python
3.12, uv, Make, and system shell. Place bundled tools and required libraries under
root-owned storage. A system-owned executable is also acceptable. Do not rely on a
Homebrew installation the agent can rewrite.

Create a fresh worker home, workspace, temporary directory, and project environment
for each run. Worker PATH contains only approved toolchain locations. Clear Git,
Python, shell startup, dynamic-loader, and credential variables before applying
allowed policy environment values. Toolchain metadata includes versions and
digests; the signed environment identifies this metadata digest.

Allow worker-owned dependencies and generated state within the current job. Setup
may fetch dependencies and start local services using normal network access.
Persist no worker-writable executable environment between jobs in v1. Persistent
caches are initially disabled. A future cache must validate cached content and
prevent poisoning across runs before it becomes an optimization.

Daemon/controller credentials are never available to validation. Tests requiring
private service credentials need a later explicit scoped-secret mechanism; do not
copy the developer's entire login environment as a workaround.

### 6.2 Job state and supervision

Use this state machine:

```text
queued -> preparing -> running -> cleaning -> passed -> signed
                         |            |
                         +------------+-> failed | timed_out | cancelled | blocked
```

Terminal failures cannot transition to passed. On startup, interrupted jobs become
blocked and their execution state is cleaned before accepting another job. Never
resume a partially executed run as successful after daemon restart.

Spawn each step directly with trusted executable resolution or the declared
system shell. The controller observes exit status using OS process supervision;
stdout, stderr, JSON reports, and files cannot set status. Close all unintended
file descriptors before exec. The worker receives no daemon IPC capability or
key handle.

Enforce deadlines using a monotonic clock. Capture normal exits, signal exits,
spawn failures, cancellations, and timeout independently. Stop subsequent steps
after the first unsuccessful required step and mark them `not_run`.

Capture stdout/stderr into controller-owned logs through pipes. Default total
output limit is 64 MiB per step; exceeding it blocks the run with
`OUTPUT_LIMIT_EXCEEDED` rather than dropping evidence silently. Escape terminal
control sequences for display while hashing original captured bytes. Do not put
raw logs or secrets in the PR envelope.

After all steps, terminate remaining worker processes, including detached
descendants and setup services, verify the dedicated worker UID is quiescent, and
revoke/remove job state. Process-group cleanup alone is insufficient because a
child can detach. Bound cleanup attempts; unsuccessful cleanup blocks signing and
blocks new jobs until human recovery. Hold the per-machine execution lock through
cleanup. This prevents persistence between runs under the shared worker UID.

### 6.3 Keys and signing

Provision one Ed25519 key per provider profile using CryptoKit. Store private key
bytes only in root-owned controller state, mode 0600 beneath directories not
traversable by the developer or worker. Never print, export, log, place in an
environment variable, or expose the private key through an ordinary IPC method.
Generate keys inside the service; do not accept an imported private key.

The daemon constructs the statement from its internal immutable run record after
successful cleanup. An internal signing operation checks that every required step
has an observed normal exit 0, the source/policy identities are verified, and the
state is passed. It atomically persists the statement and signature before marking
the run signed. An ordinary `export` returns these already-produced bytes.

No public method accepts a manifest to sign, caller-provided success, previous logs
as evidence, or a selection of passing steps. A local re-verification command can
inspect an envelope but cannot bless it as a newly executed job.

Key rotation is an administrator operation. Provision a replacement, merge its
public record, validate with it, then revoke the old record. Retain revoked entries
for audit. Uninstalling the provider does not revoke its key remotely; document
both actions. Secure Enclave P-256 signing can be a future protocol extension;
hardware key protection alone does not establish truthful execution.

## 7. Signed statement and verification algorithm

### 7.1 Envelope

Use standard DSSE fields `payloadType`, `payload`, and `signatures`. Set
`payloadType` to `application/vnd.in-toto+json`. `payload` is standard base64 of the
exact UTF-8 statement bytes. V1 permits exactly one signature; its `keyid` is the
registered fingerprint and `sig` is base64 of the 64-byte Ed25519 signature.

Sign the DSSE pre-authentication encoding of payload type and payload using the
published DSSE protocol. Do not sign only the payload hash, invent an envelope, or
verify a parsed/reserialized replacement of the signed bytes. Both Swift and
Python implementations must use the same golden byte vectors.

Limit the complete envelope to 16 KiB. Reject invalid base64, JSON duplicate keys,
unknown envelope fields, unexpected types, extra signatures, unsupported payload
types, and invalid lengths. Signature verification uses the registry key selected
by a matching authorized key ID, never a public key embedded in the envelope.

### 7.2 Predicate

Use predicate type `https://powdrr.io/attestation/local-validation/v1`. This URI is
a schema identifier and is not fetched during verification. The schema must
define all fields below and reject unknown fields in v1.

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [{
    "name": "git+https://github.com/OWNER/REPO",
    "digest": {"sha1": "FULL_COMMIT_ID"}
  }],
  "predicateType": "https://powdrr.io/attestation/local-validation/v1",
  "predicate": {
    "repository": {"id": 123, "full_name": "OWNER/REPO"},
    "principal": {"github_user_id": 456},
    "provider": {
      "id": "macbook-01",
      "version": "RELEASE_VERSION",
      "key_id": "sha256:FINGERPRINT",
      "security_profile": "macos-protected-daemon-v1"
    },
    "source": {
      "object_format": "sha1",
      "commit": "FULL_COMMIT_ID",
      "tree": "GIT_TREE_ID",
      "snapshot_sha256": "HEX_DIGEST"
    },
    "policy": {
      "path": ".github/local-validation.yaml",
      "trusted_ref": "refs/heads/main",
      "resolved_commit": "POLICY_SOURCE_COMMIT",
      "sha256": "APPROVED_POLICY_BYTES_DIGEST",
      "candidate_sha256": "CANDIDATE_POLICY_DIGEST_OR_NULL"
    },
    "environment": {
      "os": "macos",
      "os_version": "VERSION",
      "architecture": "arm64",
      "toolchain_id": "python312-uv-v1",
      "toolchain_sha256": "HEX_DIGEST",
      "network": "allowed",
      "execution_identity": "dedicated-worker"
    },
    "run": {
      "id": "UUID",
      "started_at": "2026-10-09T01:00:00Z",
      "finished_at": "2026-10-09T01:03:00Z",
      "duration_ms": 180000,
      "result": "passed",
      "cleanup": "complete"
    },
    "steps": [{
      "id": "tests",
      "definition_sha256": "HEX_DIGEST",
      "status": "passed",
      "exit_code": 0,
      "signal": null,
      "duration_ms": 180000,
      "stdout_sha256": "HEX_DIGEST",
      "stderr_sha256": "HEX_DIGEST"
    }]
  }
}
```

This example shows field shapes; the actual steps must include every configured
step, including setup. `candidate_sha256` is JSON null when absent. V1 supports
GitHub's SHA-1 Git object IDs explicitly; do not mistake the commit ID for a hash of
a directory archive. Adding another object format requires a schema extension.

The per-step `definition_sha256` is SHA256 of the exact effective policy bytes,
followed by one NUL byte, followed by the step ID in UTF-8. It binds each observed
step to the known definition without requiring cross-language YAML serialization.
The verifier computes the expected value using the trusted policy bytes.

Snapshot digest is SHA256 over the following byte sequence: ASCII
`POWDRR-ATTEST-SNAPSHOT-V1` followed by NUL, unsigned big-endian uint64 record count,
then records sorted by original UTF-8 path bytes. Each record contains big-endian
uint32 path length, path bytes, a one-byte type (0 regular file, 1 symlink), a
one-byte executable flag (0 or 1; always 0 for symlinks), big-endian uint64 original
content length, and the raw 32-byte SHA256 of original content. For symlinks,
content means the link target bytes. Directory records are omitted. PR 01 freezes
golden fixtures before PR 04 materializes it. No host paths, hostname, private
credentials, raw command output, test-report claims, or PR number belong in the
signed statement. Omitting PR number allows pre-push execution; GitHub supplies
the PR context later.

Use whole-second UTC timestamps with `Z`; integer millisecond durations; lowercase
hex digests; no floating-point, non-finite numbers, or duplicate JSON keys. The
manifest's account identity comes from the provisioned profile, never request JSON.
Provider IDs are labels and do not establish identity without the authorized key.

### 7.3 Portable acceptance algorithm

Inputs are envelope bytes, trusted policy bytes, trusted registry bytes, current
GitHub PR context, current GitHub commit/tree information, and verifier time.
`verify()` returns a typed decision with stable rejection code and safe summary.

1. Bound and parse the envelope without executing or fetching anything.
2. Resolve the actual PR author's numeric account ID in the trusted registry.
3. Find an active key matching the signature key ID; reject unregistered keys.
4. Verify the Ed25519 signature over the exact DSSE encoding.
5. Parse signed payload strictly and validate schema and predicate version.
6. Match subject, repository ID, actual current head commit, and GitHub tree ID.
7. Match the signed principal, provider ID, and key fingerprint to the selected
   authorization record. Reject unexpected security profile or malformed version.
8. Match current approved policy digest, trusted branch, toolchain ID, network
   setting, exact ordered step IDs, and each step definition digest.
9. Require observed exit 0 with no signal for all required steps, overall passed,
   and completed cleanup. Missing, duplicated, extra, skipped, or non-passing
   steps fail. Check structurally plausible, bounded timing fields.
10. Enforce key validity at issuance and verification, current revocation state,
    maximum age, future timestamp limits, and completion after start.
11. Return accepted only when every condition succeeds.

Multiple matching passing runs for the same SHA are allowed. A full explicit retry
may produce new evidence. This v1 requires existence of a passing complete run;
it does not claim absence of earlier failures or eliminate flakes.

Representative stable rejection codes: `UNREGISTERED_AUTHOR`, `UNAUTHORIZED_KEY`,
`REVOKED_KEY`, `INVALID_SIGNATURE`, `INVALID_SCHEMA`, `WRONG_REPOSITORY`,
`STALE_HEAD`, `TREE_MISMATCH`, `POLICY_CHANGED`, `INCOMPLETE_STEPS`, `NOT_PASSED`,
`EXPIRED`, `FUTURE_TIMESTAMP`, and `UNSUPPORTED_PROVIDER`. Operational failures
such as API unavailability produce blocked decisions, never acceptance.

## 8. CLI, IPC, and PR transport

### 8.1 Stable commands

```text
powdrr-attest doctor --repo PATH --github-repo OWNER/REPO --json
powdrr-attest config validate --file FILE --json
powdrr-attest run --repo PATH --github-repo OWNER/REPO --commit HEAD --wait --json
powdrr-attest status --run UUID --json
powdrr-attest logs --run UUID --step STEP_ID
powdrr-attest cancel --run UUID
powdrr-attest export --run UUID --output FILE
powdrr-attest enrollment show --json
powdrr-attest publish --repo PATH --pr NUMBER --run UUID --location comment
powdrr-attest publish --repo PATH --pr NUMBER --run UUID --location description
powdrr-attest skills install --agent AGENT --scope repo --repo PATH
```

CLI `run` waits by default; `--wait` makes agent usage explicit. JSON status goes
to stdout, progress and diagnostics to stderr. A waited run returns 0 only for a
signed passing run. Exit codes: 1 validation failure, 2 invalid input/config, 3
blocked environment/service, 4 stale evidence, 5 authorization rejection, 6
cancelled/timeout. Keep the structured state more precise than the exit code.

`doctor` reports service identity/health, enrollment, trusted repository identity,
toolchain availability, supported source types, and registry authorization. It
does not silently install packages, request admin elevation, register keys, or
change repository configuration. Administrative setup remains explicit.

Ordinary IPC methods are `health`, `run`, `status`, `logs`, `cancel`, `export`, and
`public_enrollment_record`. Requests use protocol version 1, bounded typed fields,
and caller-owned run IDs. `run` accepts repository identity and source transport,
not a manifest, result, environment, executable override, or private key. Run IDs
are generated by the daemon. The daemon checks authenticated UID ownership for
every read, export, or cancellation. Unknown fields/methods are rejected.

The daemon streams export bytes. The unprivileged CLI writes a user-selected output
path, so privileged code never follows that destination. Retain controller-owned
evidence and logs for seven days by default. An exported envelope may outlive local
logs, but freshness is still enforced by GitHub.

### 8.2 Metadata syntax

Use a marked block containing standard base64 of the complete compact envelope:

```text
<!-- powdrr-attest:v1:begin -->
BASE64_ENCODED_DSSE_ENVELOPE
<!-- powdrr-attest:v1:end -->
```

Display a short readable summary alongside it: abbreviated SHA, provider label,
completion time, policy digest, and command outcomes. The verifier trusts only the
signed bytes; the readable summary is informational.

`publish` fetches the PR, checks current author/repository/head, and locally
verifies the selected envelope against current trusted files before writing.
Use ordinary user GitHub credentials for this operation, never the provider's
private key or GitHub App private key. Read and preserve existing PR prose; use
structured API requests rather than shell interpolation. If the PR body changed
during preparation, abort and refetch instead of intentionally overwriting it.

Comment mode is the default. Update the current user's existing managed comment
when one is present; otherwise create one. Description mode replaces only the
managed block and its adjacent generated summary. Neither mode edits Git state.

The verifier reads marked blocks from the PR body and paginated PR comments.
Comments are metadata regardless of who posted them; a copied valid signed blob
cannot change its repository, author, or SHA binding. Deduplicate envelopes by
digest. Ignore invalid candidates when another candidate fully verifies. Choose
the newest accepted candidate by completion timestamp, then run ID.

Bound scanning to 1000 comments and 100 distinct candidate blocks. If limits are
exceeded, report `METADATA_LIMIT_EXCEEDED` and require cleanup; do not assume an
unread candidate exists. Bound each candidate before base64 decoding and apply the
16 KiB decoded-envelope limit. Never fetch arbitrary URLs from PR text.

After publication, explicitly request the trusted verifier workflow on the default
branch as a fallback, because not every bot-generated GitHub event triggers a new
workflow. Publication is idempotent; repeated calls do not generate new evidence.

## 9. GitHub trust and inexpensive verification

### 9.1 Protected check issuer

Create a dedicated repository-installed GitHub App, `Powdrr Local Validation`.
Required permissions are metadata read, contents read, pull requests read,
checks write, and actions write. No code write, administration, or PR write is
required. Limit installation to explicitly selected repositories.

Store the App ID, installation configuration, and App private key in an Actions
environment named `attestation-control`, not ordinary repository secrets. Configure
its deployment rule for the selected default **branch** name only. Do not permit
tags, wildcard branches, PR merge refs, or all protected branches. Verify actual
repository plan/visibility supports environment branch restrictions before rollout.
Never silently fall back to unrestricted secrets.

The App key is a GitHub check-publishing credential, distinct from every local
provider key. Keep it out of validation jobs, artifacts, logs, the CLI, and agents.
Mint short-lived installation tokens only inside trusted control/report jobs.

Require `local-validation/attestation` with this App as its expected source. Require
`local-validation/ci` from the same App once gated CI reporting exists. A same-name
status from a developer token or another Actions workflow must not satisfy either.
CODEOWNERS review alone does not protect secrets during a PR-authored workflow run;
the environment branch restriction is essential.

### 9.2 Verifier workflow

Install `.github/workflows/local-attestation.yml` on the trusted default branch.
Triggers:

- `pull_request_target`: opened, synchronize, reopened, edited, ready_for_review.
- `issue_comment`: created, edited, deleted, filtered to comments on PRs.
- `workflow_dispatch`: validated PR number input, for explicit rerun/fallback.
- Default-branch pushes affecting the registry, policy, verifier, or control
  workflows: re-evaluate all open PRs in bounded batches.
- Hourly schedule: re-evaluate freshness for open PRs and reconcile interrupted
  check updates. Operational retry does not rerun local tests or invent evidence.

Use the current PR API result as authoritative, not event actor, event-time head,
`GITHUB_SHA`, commit author, comment author, or manifest-supplied PR context.
Resolve the trusted branch tip once per decision and fetch policy and registry
from that same commit. Reject PRs targeting any other branch in v1.

The controller runs only verifier code from the trusted default branch or a pinned
reviewed verifier release. Checkout is explicit and trusted, with credential
persistence disabled. Python imports and executable search paths must never refer
to PR artifacts. Third-party Actions are pinned by full commit SHA. Pass untrusted
text as JSON/data, never through expression interpolation into shell code.

It never checks out, imports, builds, installs dependencies from, or executes PR
head. This rule applies equally to `issue_comment` and manually dispatched runs.
Keep the ordinary workflow token read-only; use the restricted App token for
publishing checks and dispatch only.

### 9.3 Decision and dispatch procedure

1. Fetch current open PR, author, repository, target, and head. Closed PRs do not
   dispatch. Filter unrelated issue comments before accessing the App credential.
2. Resolve current trusted policy/registry and collect bounded metadata candidates.
3. Create/update the canonical App check for this head as in progress. Never leave
   an older success standing intentionally while a new decision is pending.
4. Run the portable acceptance algorithm and produce a structured decision.
5. Refetch PR head/author/target and trusted file digests before publishing success.
   If they changed, abandon that decision and reconcile current context.
6. On rejection, set the required check to failure with a remedy. Missing evidence
   is `ATTESTATION_REQUIRED`, not skipped, neutral, or successful. API errors are
   blocked/failing; rate-limit handling uses bounded backoff.
7. On acceptance, publish success with envelope digest, effective policy digest,
   registry source commit, PR number, run ID, and actual head SHA in its summary.
8. Dispatch the trusted CI workflow only for that verified context. Refetching and
   the CI preflight handle a push racing with dispatch.

GitHub checks are attached to commits rather than uniquely to PR authors. For v1,
detect multiple open PRs in the base repository with the same head SHA but different
authors or targets. Fail/reconcile their gate checks as `AMBIGUOUS_PR_CONTEXT`
instead of letting an accepted check for one author authorize another PR. A future
extension may define safe sharing semantics; do not guess at them.

Serialize control updates by repository and PR/head, and serialize mutations of a
shared SHA check. Stale workflow runs must refetch context rather than overwriting
new results. Scheduled reconciliation repairs checks left pending by cancellation.
An updated registry or policy must re-evaluate prior accepted heads. Require strict
branch freshness for merging so an intervening trusted-branch change also requires
the PR to update; administrative bypass remains a trusted action.

## 10. Gated expensive CI

Create `.github/workflows/attested-ci.yml`, dispatchable only with the default
branch workflow definition. Inputs are PR number, expected head SHA, and accepted
envelope digest. They are hints to revalidate, not authorization themselves.

Use three isolated jobs:

1. **Preflight:** trusted verifier only; read the current PR and trusted files,
   locate the named envelope, and fully verify it again. It has no App key. It
   emits immutable structured context only after success: actual PR number, head,
   author ID, base commit, policy digest, and envelope digest. A direct manual
   dispatch with fabricated inputs cannot reach validation.
2. **Validation:** runs only after preflight success. It has no protected
   environment or App token, uses a fresh hosted runner, checks out the verified
   source with `persist-credentials: false`, and executes the existing full CI
   suite. The ordinary token is read-only and no repository secrets are injected.
3. **Report:** always runs after terminal job outcomes, uses trusted source and
   `attestation-control`, and publishes `local-validation/ci` using the App. It
   reads GitHub/Actions job outcomes and trusted preflight outputs; it never trusts
   a success file, command stdout, or artifact generated by validation.

The report job revalidates current PR context, signer authorization, freshness,
policy, and selected source before publishing success. Failure, cancellation,
timeout, skipped validation, missing preflight output, or changed context cannot
produce success. A failed preflight creates no success for a caller-supplied SHA.
Reconciliation detects cancelled runs whose report job never executes.

Preserve current CI behavior by default: test the merge of verified head with the
selected current base, rather than silently switching to head-only integration
tests. Preflight records the base commit. Validation fetches that exact base and
head, constructs the merge in the unprivileged job, and records the resulting tree.
Merge conflicts fail. The report job verifies base freshness; a moved base schedules
a new preflight/integration run. Local head evidence can be reused only if its
policy and freshness still match. Do not compare a local head signature against
GitHub's synthetic merge SHA.

Official PR CI must have no parallel unconditional `pull_request` path once the
gate is enforced. Preserve separate validation for trusted default-branch pushes.
During report-only rollout, preserve existing `merge_group` behavior. Enforced v1
setup refuses adoption if a merge queue is enabled: required App checks would
otherwise also need a separate queue-aware trust path on synthetic group SHAs.
Never enable enforcement while leaving queue commits with unsatisfiable required
checks. Queue-aware enforcement is an explicit later extension; do not fake a
developer attestation for a synthetic queue commit.

Do not rerun an already successful official CI run merely because the same valid
envelope is republished. Identify dispatches by PR, head, selected base commit,
policy digest, envelope digest, and pinned CI workflow revision. Deduplicate queued
or running dispatches by this tuple using App check metadata and Actions APIs.
Support an explicit human/agent CI rerun that still passes preflight. Registry
changes revalidate authorization even if the execution tuple is unchanged.

The resource-saving guarantee applies to this repository's configured official PR
pipeline. A contributor who can author unrelated workflows may cause other GitHub
Actions runs; this tool is not a universal GitHub compute-spending policy.

## 11. Agent skill contract

Ship one canonical `attested-local-validation` skill and installation adapters for
Codex, Claude Code, GitHub Copilot, Cursor, and OpenCode. Use standard `SKILL.md`
frontmatter with a concise name and description. All adapters invoke the same CLI
and follow section 3.3; agent-specific syntax never changes authorization.

| Agent | Repository installation directory |
| --- | --- |
| Codex | `.agents/skills/attested-local-validation/` |
| Claude Code | `.claude/skills/attested-local-validation/` |
| GitHub Copilot | `.github/skills/attested-local-validation/` |
| Cursor | `.cursor/skills/attested-local-validation/` |
| OpenCode | `.opencode/skills/attested-local-validation/` |

Agents may recognize multiple compatible locations. Installation deduplicates by
skill name and reports existing conflicting definitions rather than installing
several copies accidentally. Require an explicit adapter selection; never change
global agent permissions, auto-approve sudo, or replace unrelated skill content.

The skill must explain: commit before authoritative validation; run the full
approved policy; wait for signed completion; read failures and repair them; rerun
after any new commit; push the exact tested SHA; publish the signed envelope; and
inspect the required gate result. Use machine-readable states rather than parsing
human output. Permission-denied errors are actionable failures, not invitations
to access provider keys. Missing installation/enrollment requires human setup.

Skill installation into a repository changes tracked files and must happen before
the final commit and validation. Personal installation can be offered later.
Skill text and scripts are convenience code in the caller's trust domain; modifying
them cannot grant authority to sign or bypass CI.

## 12. Implementation PR sequence

Each PR is independently reviewable and must leave existing repository behavior
working. Use feature branches and dedicated worktrees. Open a PR for every change
set and let the user merge. Do not bundle native privilege code, GitHub enforcement,
and agent skills into one unreviewable change.

Each PR description must name its acceptance IDs, link evidence for each, state
which commands were run, and distinguish mocks from real OS/GitHub validation.
All existing repository checks required by AGENTS.md must pass or have an explicit
reviewed baseline explanation. New product verification belongs in
`attestation/scripts/verify-all.sh` and must not require an agent-owned production
key. Cryptographic test keys live only in fixtures and are labeled non-production.

| PR | Title | Dependencies | Reviewable result |
| --- | --- | --- | --- |
| 01 | Define attestation schemas and shared fixtures | None | Frozen protocol and configuration parsers |
| 02 | Implement portable signature and authorization verifier | 01 | Strict offline acceptance/rejection library |
| 03 | Install protected macOS service and authenticated IPC | 01 | Real privilege boundary, with signing/execution still disabled |
| 04 | Resolve trusted policy and materialize verified Git snapshots | 03 | Exact source preparation independent of caller state |
| 05 | Execute complete policies in a managed worker environment | 04 | Supervised outcomes, deadlines, and verified cleanup |
| 06 | Provision provider keys and sign completed runs | 02, 05 | First genuine passing local attestation |
| 07 | Deliver stable agent-callable CLI and diagnostics | 06 | Usable local development cycle |
| 08 | Implement reviewed enrollment and key lifecycle | 02, 07 | CODEOWNERS-style user-to-provider authorization |
| 09 | Publish immutable evidence into PR metadata | 07, 08 | Exact-head PR transport without changing Git state |
| 10 | Verify PR attestations and publish protected GitHub checks | 02, 08, 09 | Cheap report-only gate with authorized App issuer |
| 11 | Gate expensive CI and reconcile stale decisions | 10 | Enforced PR pipeline with protected result reporting |
| 12 | Ship skills for five coding agents | 07, 09, 11 | Agent adapters using one CLI contract |
| 13 | Validate adversarial boundaries and release v1 | All prior PRs | Reviewed, installable, operationally tested release |

### PR 01: Define attestation schemas and shared fixtures

**Scope:** Create `attestation/schemas/`, the independent Python verifier package,
the native protocol target, examples, and cross-language fixtures. No service or
CI enforcement is enabled.

**Implementation tasks:**

1. Encode sections 4, 7, and 8 as versioned JSON schemas and typed Swift/Python
   records. Use schemas for external documentation and fixture validation; parser
   behavior must also enforce duplicate-key and resource limits.
2. Pin Python dependencies and Swift YAML parser dependencies. Use PyYAML with a
   strict loader and a pinned Swift parser such as Yams; audit parse events to
   reject aliases/tags/duplicate mappings before conversion to typed records.
3. Implement policy and registry parsers, raw-blob hashing, key fingerprints,
   snapshot record encoding, and step-definition digests.
4. Add fixture generators using fixed test inputs. Include golden DSSE statement
   bytes, PAE bytes, keys, signatures, and configuration errors.
5. Add independent package build/type/lint checks to a product verification script.
   Keep native tests separate from platform-neutral checks.

**Acceptance criteria:**

- **01-AC1:** The sample Make and shell policies parse identically in Swift and
  Python; exact byte digests and ordered IDs match the frozen fixtures.
- **01-AC2:** Every forbidden schema condition in section 4 has a rejecting fixture
  exercised by both implementations, including duplicate YAML keys and aliases.
- **01-AC3:** Two fixtures with different YAML formatting have different raw
  policy digests even when their parsed meaning matches.
- **01-AC4:** Snapshot digest fixtures cover ordinary files, executable files,
  symlinks, empty files, non-ASCII paths, and differing sort orders.
- **01-AC5:** Unknown protocol/schema versions, floats in integer fields, oversized
  inputs, malformed dates, and key/fingerprint mismatches fail explicitly.
- **01-AC6:** The verifier wheel can be imported without Powdrr, a coding-agent
  runtime, network access, or the native service.

### PR 02: Implement portable signature and authorization verifier

**Scope:** `attestation/verifier/src/powdrr_attestation/{dsse,verify,decision}.py`
and tests. Pure verification only; GitHub access is an injected context provider.

**Implementation tasks:**

1. Implement DSSE PAE and Ed25519 verification using cryptography; do not implement
   curve arithmetic or substitute a homegrown signature format.
2. Implement the eleven-step acceptance algorithm with typed decisions and stable
   rejection codes. Inject time for deterministic boundary tests.
3. Implement registry selection using actual numeric author ID, key fingerprint,
   provider ID, active status, and validity windows.
4. Provide a verifier CLI that reads explicit envelope/context/policy/registry
   inputs for reproducible diagnostics. It does not create execution evidence.

**Acceptance criteria:**

- **02-AC1:** The golden valid envelope verifies; changing one signed byte or the
  payload type causes rejection.
- **02-AC2:** A valid signature from an unregistered key, another user's key, a
  revoked key, or a key merely embedded in the envelope is rejected.
- **02-AC3:** Wrong repository, head, tree, provider, policy, toolchain, or ordered
  steps each has a targeted failing fixture and stable reason.
- **02-AC4:** Missing/skipped/extra steps and fabricated success with nonzero or
  signal termination cannot be accepted even when correctly signed by a test key.
- **02-AC5:** Expiration, future time, validity-window boundaries, and skew limits
  have deterministic tests immediately before/at/after each boundary.
- **02-AC6:** Duplicate JSON keys, unsupported fields/algorithms, invalid base64,
  and malformed signatures fail without exceptions escaping the typed API.
- **02-AC7:** Verification performs no HTTP requests, repository commands, file
  imports from PR state, or writes to an authorization registry.

### PR 03: Install protected macOS service and authenticated IPC

**Scope:** Native daemon skeleton, worker account provisioning, package installer,
service management, health IPC, and privileged integration tests.

**Implementation tasks:**

1. Build signed native executables, root-owned installation/state paths, a system
   launch daemon, and `_powdrrattest` worker identity.
2. Implement bounded IPC, OS-derived peer UID, profile ownership checks, and strict
   administrative/ordinary endpoint separation.
3. Add launch/library constraints and prevent user-owned dynamic dependencies.
4. Implement safe installation, upgrade, health, and explicit uninstall behavior.
   There is no automatic sudo invocation from `run`.
5. Expose no signing or validation success while source/execution PRs are absent.

**Acceptance criteria:**

- **03-AC1:** An ordinary enrolled developer UID can call health; a different
  unprovisioned UID and the worker UID cannot access profile operations.
- **03-AC2:** Requests cannot impersonate another user by changing JSON fields.
- **03-AC3:** Ordinary developer processes cannot modify the executable, daemon
  definition, profile state, socket parent directory, or future key directory.
- **03-AC4:** PATH, Python packages, loader variables, caller cwd, and a repository
  executable named like the provider do not alter daemon code or dependencies.
- **03-AC5:** Caller-controlled paths/symlinks cannot make installation or IPC
  write outside the fixed state locations.
- **03-AC6:** Service restart and upgrade preserve profile ownership; uninstall
  stops the service and clearly reports that GitHub revocation is separate.
- **03-AC7:** Real macOS tests demonstrate identity/file permission separation.
  A mock subprocess or container UID test is insufficient evidence for this PR.
- **03-AC8:** The sandboxed worker and its shell/Python/Make children can use their
  job container and network, but cannot read developer-home fixtures, controller
  files, or an agent-controlled Python installation. Attempted child sandbox
  escape is rejected; no silently unsandboxed compatibility mode is accepted.

### PR 04: Resolve trusted policy and materialize verified Git snapshots

**Scope:** Native source importer, trusted GitHub reads, policy resolution,
repository profiles, and private workspace preparation.

**Implementation tasks:**

1. Implement bounded Git bundle transport and fresh bare-object import with
   strict object verification and sanitized trusted Git invocation.
2. Resolve GitHub repository ID/default branch and fetch approved policy bytes
   independently of the caller's local refs. Add read-only credential provisioning
   to the administrative installer for private repositories.
3. Materialize and digest the verified tree without executing checkout hooks or
   filters; apply containment and macOS filename collision checks.
4. Record candidate policy digest separately from effective trusted policy.
5. Return explicit blocked states for unsupported sources or inaccessible policy.

**Acceptance criteria:**

- **04-AC1:** Given a commit with files A, dirty changes B and untracked files C,
  the private workspace contains exactly committed A before execution.
- **04-AC2:** Editing local `origin/main`, remote URLs, Git hooks, replacement
  objects, filters, or bundle labels cannot choose a different effective policy
  or substitute source for the requested commit.
- **04-AC3:** A commit not yet pushed can be imported, while the effective policy
  still comes from the independently fetched trusted GitHub branch.
- **04-AC4:** Corrupt objects, wrong commit claims, missing objects, symlink escapes,
  traversal, and filesystem collisions fail before validation begins.
- **04-AC5:** Submodules/LFS fail with an actionable unsupported-source message.
- **04-AC6:** The daemon obtains the same snapshot digest as the shared fixtures.
- **04-AC7:** A policy-update PR executes the existing base policy; its new policy
  digest is informational until merge. Private read credentials never appear in
  worker environment, logs, or exported records.

### PR 05: Execute complete policies in a managed worker environment

**Scope:** Toolchain installation, worker environment construction, process
supervision, logs, timeouts, cancellation, and cleanup. Signing remains disabled.

**Implementation tasks:**

1. Provision the first root-owned Python/uv/Make toolchain and verify its versions
   and component digests. Never bootstrap it from an agent-writable installation.
2. Create private fresh job environment; execute every required step in order under
   the worker UID, including setup. Permit ordinary network access.
3. Observe process exits directly; implement timeout/cancel/log-limit handling and
   safe terminal rendering.
4. Track all worker processes, including detached descendants, and enforce
   bounded quiescent cleanup before releasing the machine execution lock.
5. Persist internal job states atomically and block interrupted runs on restart.

**Acceptance criteria:**

- **05-AC1:** A real policy that sets up dependencies, starts a local test service,
  runs tests, and executes Make/shell quality steps returns observed per-step data.
- **05-AC2:** A failed setup or validation step prevents later steps from running;
  stdout containing `passed` or a forged report file cannot change that outcome.
- **05-AC3:** Tests show normal exits, signals, spawn errors, timeouts, output caps,
  and cancellation are distinguished and never become passed.
- **05-AC4:** Ordinary developer UID processes cannot replace tools/dependencies in
  the active managed environment, access its workspace, or signal its worker.
- **05-AC5:** Worker commands cannot read controller credentials, future private
  key state, developer home, or access ordinary/admin daemon IPC.
- **05-AC6:** Detached/background workers are removed after the job; attempted
  persistence cannot affect the next run. Failed cleanup blocks new execution.
- **05-AC7:** Concurrent requests serialize; cancellation by another profile fails;
  daemon restart cannot turn an interrupted run into a passed run.
- **05-AC8:** Setup and tests never execute as root. An integration fixture records
  UID/GID/groups inside each child to prove this.

### PR 06: Provision provider keys and sign completed runs

**Scope:** Protected key provisioning, internal manifest construction, signature
production, atomic evidence persistence, and key protection tests.

**Implementation tasks:**

1. Generate keys inside the protected provider; expose public enrollment records.
2. Construct the complete in-toto statement exclusively from daemon records.
3. Implement terminal passed-to-signed transition and DSSE signing using CryptoKit.
4. Persist immutable evidence atomically, return already-signed bytes on export,
   and implement admin-only rotation preparation.
5. Ensure development/fake execution modes cannot produce a production security
   profile or sign using a production provider key.

**Acceptance criteria:**

- **06-AC1:** A real completed macOS run produces an envelope that PR 02's Python
  verifier accepts under a test registry containing its actual provisioned key.
- **06-AC2:** No signature is issued for nonzero, signal, timeout, cancellation,
  partial steps, interrupted state, unverified source, or unsuccessful cleanup.
- **06-AC3:** An ordinary caller cannot submit a manifest/result to sign, import a
  private key, alter stored results, or replay prior logs as a new execution.
- **06-AC4:** Ordinary developer and worker attempts to read, export, or access the
  key fail; it is absent from process arguments, environment, logs, and artifacts.
- **06-AC5:** Swift-generated signatures verify against shared vectors and the
  Python implementation; repeated export returns the same immutable bytes.
- **06-AC6:** A crash during persistence yields either a complete verifiable record
  or a blocked unsigned run, never partial accepted evidence.

### PR 07: Deliver stable agent-callable CLI and diagnostics

**Scope:** Full CLI described in section 8, predictable JSON, progress/log output,
safe user-side export, and local recovery guidance.

**Implementation tasks:**

1. Implement run/status/logs/cancel/export/config/doctor commands and stable exits.
2. Resolve HEAD once before transport and show the exact resolved commit prominently.
3. Provide remediation for missing toolchain, enrollment, source support, network,
   service health, validation failure, and stale policy.
4. Keep machine output isolated from progress; handle interrupted CLI without
   inventing terminal success or losing the daemon's run identity.

**Acceptance criteria:**

- **07-AC1:** A shell agent can run, wait, read JSON, and export a successful signed
  envelope without parsing prose or interacting with a GUI during normal execution.
- **07-AC2:** Waited failure, timeout, blocked state, invalid input, and cancellation
  have the documented nonzero exit codes and typed machine-readable reasons.
- **07-AC3:** A commit made while a job runs does not change its source identity.
- **07-AC4:** Malicious stdout cannot corrupt JSON status or inject terminal actions.
- **07-AC5:** Export writes only through the unprivileged CLI; output path symlinks
  cannot cause privileged file writes.
- **07-AC6:** Missing installation or enrollment produces explicit human setup
  instructions; the CLI does not request or retain arbitrary admin authority.

### PR 08: Implement reviewed enrollment and key lifecycle

**Scope:** Public enrollment records, registry-edit helper, onboarding/runbooks,
CODEOWNERS templates, rotation and revocation checks.

**Implementation tasks:**

1. Export a complete record from a provisioned profile and validate fingerprints.
2. Add an unprivileged helper that prepares a registry diff without auto-merging or
   altering GitHub repository settings. Preserve unrelated mappings.
3. Document trusted installation checks, current author identity selection, first
   maintainer bootstrap, already-enrolled-maintainer onboarding, and lost-key repair.
4. Add examples covering multiple machines, rename of readable login, rotation,
   expiry, and revocation. Protect registry and CODEOWNERS through required reviews.

**Acceptance criteria:**

- **08-AC1:** A registry entry authorizes only the mapped numeric GitHub account and
  listed provider key; a different PR author cannot use that key successfully.
- **08-AC2:** A key added only in the candidate branch does not authorize its PR.
- **08-AC3:** An enrolled maintainer can submit a reviewed onboarding PR for a new
  user without granting that user a self-enrollment gate exception.
- **08-AC4:** Multiple active machines work; rotated keys can overlap during
  migration; revoked keys fail even for previously signed valid envelopes.
- **08-AC5:** Registry edits are auditable, preserve existing entries, and never
  disclose a private key. No ordinary run method changes enrollment.
- **08-AC6:** Setup documents human recovery for loss of the last provider and
  separates local uninstall from remote revocation.

### PR 09: Publish immutable evidence into PR metadata

**Scope:** CLI publication, managed marker encoding/decoding, bounded candidate
collection, and GitHub API transport tests. It does not enable enforcement.

**Implementation tasks:**

1. Implement comment and description modes, readable summaries, and exact-byte
   envelope transport. Preserve user-written PR content.
2. Fetch current PR context and trusted files, verify evidence before publication,
   and refuse stale heads. Use the caller's ordinary GitHub credentials.
3. Implement idempotent updates and digest deduplication; keep the verifier's
   collection logic in the portable package for reuse by GitHub control workflows.
4. Add explicit verifier workflow dispatch fallback after successful publication.
   If the workflow is not yet installed, report that status without forging a
   check or claiming GitHub acceptance.

**Acceptance criteria:**

- **09-AC1:** Both metadata locations transport the exact signed envelope, and the
  portable verifier accepts the extracted bytes without re-signing.
- **09-AC2:** Publication leaves HEAD, index, tracked files, and branch refs unchanged.
- **09-AC3:** After a new commit/push, old evidence is rejected before publication;
  a push racing publication still cannot pass GitHub's subsequent verification.
- **09-AC4:** Repeated publication updates the managed content without accumulating
  duplicate comments or erasing unrelated PR prose.
- **09-AC5:** Malicious markdown, shell metacharacters, arbitrary URLs, large blocks,
  and malformed base64 cannot execute code or bypass resource limits.
- **09-AC6:** A valid candidate can be found across paginated comments despite
  invalid surrounding candidates; exceeded scan limits fail explicitly.
- **09-AC7:** Publication uses no provider private key and does not register users,
  amend commits, push, merge, or publish a gate decision directly.

### PR 10: Verify PR attestations and publish protected GitHub checks

**Scope:** Trusted controller workflow/template, GitHub App setup, protected
environment setup instructions, API context adapters, and real test-repository
checks. Run in report-only mode; existing official CI remains unchanged.

**Implementation tasks:**

1. Implement the controller and triggers in section 9 with pinned dependencies
   and bounded API reads. Resolve policy and registry from a single trusted tip.
2. Publish `local-validation/attestation` through the dedicated App. All missing,
   invalid, operationally blocked, and ambiguous contexts produce explicit failure.
3. Document environment **branch-only** restriction, App installation, minimum
   repository plan, expected check issuer, and separation from ordinary secrets.
4. Add event adapters and head/author/base rechecks before publishing decisions.
5. Add context collision protection for multiple PRs sharing a SHA and serialized
   check updates. Expose decisions as safe readable check summaries.

**Acceptance criteria:**

- **10-AC1:** A real GitHub PR with a real enrolled provider envelope passes; a
  missing envelope and every PR 02 rejection category fail the App gate.
- **10-AC2:** Adding an attacker's public key only to the PR does not authorize it;
  the mapped author comes from GitHub's PR API rather than spoofable metadata.
- **10-AC3:** A feature-branch or PR workflow cannot access the protected App key.
  A same-name success from a developer token or the GitHub Actions App cannot
  satisfy a check configured for the dedicated App.
- **10-AC4:** A malicious PR head containing install/import hooks is never executed
  by the privileged verifier; a test sentinel proves it was not invoked.
- **10-AC5:** A comment arriving after PR creation and an edited description cause
  fresh verification. Explicit dispatch works when an automatic event does not.
- **10-AC6:** A push racing verification cannot publish success for the new head
  using old evidence. Concurrent/stale controllers do not overwrite newer context.
- **10-AC7:** API failures, pagination limits, unknown authors, wrong targets, and
  ambiguous author contexts fail visibly without skipping the required gate.
- **10-AC8:** Report-only mode proves outcomes without changing existing CI routing
  or enabling required checks prematurely.

### PR 11: Gate expensive CI and reconcile stale decisions

**Scope:** Trusted dispatch/preflight/validation/report workflow, default-branch
and freshness reconciliation, deduplication, adoption tooling, and official CI
routing. Enable enforcement only after real test-repository acceptance.

**Implementation tasks:**

1. Implement the three-job split from section 10, preserving the existing full CI
   suite and merged-result semantics.
2. Dispatch only accepted contexts; run the full portable verifier again before
   any expensive setup, source checkout, dependency installation, or tests.
3. Publish authoritative CI outcome through the App from a trusted report job
   that sees only OS/GitHub job outcomes and trusted preflight outputs.
4. Handle pushes, base changes, policy updates, revocation, expiration, interrupted
   controllers, and cancelled CI. Implement hourly and trusted-push reconciliation.
5. Configure both required checks with the App issuer, strict branch freshness,
   owner reviews, and no agent-accessible bypass. Record configuration diagnostics.
6. Replace unconditional official PR CI only at enforcement cutover. Preserve
   trusted default-branch validation, and block adoption with active merge queues.
7. Deduplicate execution tuples and provide explicit reruns that still verify.

**Acceptance criteria:**

- **11-AC1:** Without valid local evidence, the cheap gate runs but the official
  expensive validation job does not check out head, install dependencies, or test.
- **11-AC2:** A passing exact-head attestation starts one official CI run whose
  validation source is the verified head merged with the selected base.
- **11-AC3:** A direct workflow dispatch with forged SHA, PR number, or envelope
  digest fails preflight and cannot start expensive validation or publish success.
- **11-AC4:** Failed, skipped, timed-out, cancelled, or missing validation cannot
  produce an accepted `local-validation/ci` App check.
- **11-AC5:** A revoked key, changed approved policy, expired evidence, or new head
  invalidates/reconciles prior gate decisions and prevents new CI dispatch.
- **11-AC6:** A push or base change during CI cannot publish current-context success
  using stale execution. The next run requires fresh applicable preflight.
- **11-AC7:** Duplicate publication/event delivery does not duplicate active or
  successful official CI; an explicit rerun remains available.
- **11-AC8:** Tests prove the App key is absent from the head-executing job and
  head-generated artifacts cannot control either required App result.
- **11-AC9:** Current default-branch push validation remains functional. Setup fails
  clearly on an enabled merge queue or unavailable environment branch protections.
- **11-AC10:** An enrolled maintainer can update policy under the current approved
  policy, then produce evidence under the new policy after reviewed merge.

### PR 12: Ship skills for five coding agents

**Scope:** Canonical skill, five adapters, explicit installation command, examples,
and agent workflow evaluations. No new agent permissions are granted.

**Implementation tasks:**

1. Write the canonical instructions from section 11 and generate minimal adapter
   frontmatter/location differences. Keep CLI behavior and error handling shared.
2. Implement explicit repository-scope installation with conflict detection,
   deduplication, version metadata, and no unrelated file replacement.
3. Add realistic transcripts/evaluations for success, test repair, changed commit,
   missing enrollment, stale policy, permission denial, and bot-authored PRs.
4. Document tested agent versions and invocation syntax using their current
   official skill documentation. Reconfirm paths at implementation time.

**Acceptance criteria:**

- **12-AC1:** Each of Codex, Claude Code, GitHub Copilot, Cursor, and OpenCode can
  discover the installed skill using its documented repository location.
- **12-AC2:** Each adapter invokes the same run/wait/publish sequence and includes
  signed evidence without a post-validation commit.
- **12-AC3:** Evaluation shows non-passing JSON results cause repair or a clear
  blocked report, never self-authored replacement evidence or key access.
- **12-AC4:** A commit after validation triggers rerun; a missing enrolled PR-author
  account or bot-author mismatch is explained without impersonating another user.
- **12-AC5:** Installing a skill happens before the final tested commit, preserves
  unrelated skills, and does not alter global approval/sandbox settings.
- **12-AC6:** Editing or replacing an adapter cannot grant signing authority or
  satisfy the protected GitHub gate with fabricated evidence.

### PR 13: Validate adversarial boundaries and release v1

**Scope:** Real macOS/GitHub end-to-end scenarios, attack regression harness,
independent security review, signed/notarized packaging, operational runbooks,
and gradual adoption in this repository.

**Implementation tasks:**

1. Run every scenario in section 13 on real supported macOS installations and a
   disposable GitHub repository with production-equivalent branch/environment
   protections. Preserve safe logs and check URLs as review evidence.
2. Obtain an independent human security review of privileged source handling,
   worker sandbox inheritance, signing state transitions, IPC, enrollment, and
   GitHub secret/check boundaries. This is an implementation release prerequisite,
   not permission required to write this plan.
3. Build reproducible release artifacts with pinned dependencies, Developer ID
   signing, notarization, checksums, and reviewed release metadata. Private Apple
   signing credentials require human provisioning and never enter test fixtures.
4. Publish installation, repository setup, onboarding, policy changes, revocation,
   lost-key recovery, service crash/disk-full recovery, privacy, and rollback docs.
5. Adopt report-only mode in this repository first. Gather pass/rejection examples
   and timing, then submit a separately reviewable enforcement/settings change.

**Acceptance criteria:**

- **13-AC1:** A less experienced agent completes a real commit-to-attestation-to-PR
  flow using the docs and skill without architectural decisions or access to keys.
- **13-AC2:** All adversarial matrix scenarios have recorded results, including
  direct attempts from an ordinary unsandboxed developer process.
- **13-AC3:** Real Apple Silicon and Intel tests demonstrate supported worker
  sandbox, toolchain, network, cleanup, and signed-package installation behavior.
- **13-AC4:** The independent security review has no unresolved release-blocking
  findings; unsupported administrator/root threats are disclosed accurately.
- **13-AC5:** Required checks cannot be satisfied by arbitrary developer statuses,
  unregistered signing keys, untrusted workflows, or stale revision evidence.
- **13-AC6:** Recovery and rotation exercises restore service without self-enrolling
  an attacker key or silently disabling enforcement.
- **13-AC7:** A reviewed rollback restores the prior official CI pipeline. Provider
  uninstallation never silently changes GitHub requirements or authorization.
- **13-AC8:** Documentation identifies command-execution semantics, network/input
  limitations, unsupported submodules/LFS, secret-dependent tests, merge queues,
  admin threat exclusions, and verified GitHub-plan prerequisites.

## 13. End-to-end and adversarial validation matrix

These are behavioral tests, not assertions that mirror implementation internals.
Mocks can support unit coverage, but OS identity, sandbox, file permissions,
protected environment access, App issuer enforcement, and real event routing
require actual platform validation.

| ID | Scenario | Required observation |
| --- | --- | --- |
| E01 | Enrolled author, valid policy, passing real commit | Signed envelope; App gate passes; official CI starts once |
| E02 | First/middle/last required command exits nonzero | No passing signature; remaining steps not run; no CI dispatch |
| E03 | Caller fabricates logs, JSON, JUnit, or manifest | Cannot obtain a provider signature for fabricated success |
| E04 | Valid signature from attacker-generated key | Unauthorized-key rejection |
| E05 | Registered key for a different PR author | Author/provider authorization rejection |
| E06 | Add attacker key in PR branch or fork | Trusted base registry remains authoritative |
| E07 | Dirty/untracked source makes local tests pass | Authoritative workspace uses committed source only |
| E08 | Change local refs, hooks, replacements, or filters | Verified source/policy remain unaffected or import fails |
| E09 | Agent edits PATH tool, Python environment, or helper | Protected tools/code remain in use; no false success |
| E10 | Child reads key, profile credentials, or developer files | OS boundary denies access; no sensitive bytes exported |
| E11 | Caller supplies arbitrary file/symlink destinations | No privileged writes or source containment escape |
| E12 | Detached child persists or cleanup fails | No signature until quiescent; next job cannot inherit state |
| E13 | Worker exits by signal, times out, or log cap is exceeded | Typed non-pass; no signature |
| E14 | Service crashes before/during/after signing persistence | No partial accepted record; interrupted job stays blocked |
| E15 | Commit/push after local validation | Previous envelope rejected for current head |
| E16 | New policy or revoked key after a previously accepted gate | Reconciliation/preflight/report reject previous authorization |
| E17 | Signature altered, payload type changed, duplicate JSON keys | Strict cryptographic/schema rejection |
| E18 | Old/future timestamps or expired/revoked key | Freshness/authorization rejection |
| E19 | Malicious PR workflows reference App environment | Branch restriction prevents credential access |
| E20 | Developer posts same-name successful status | Expected-App required check remains unsatisfied |
| E21 | PR code emits forged CI-success artifacts | Trusted report uses job outcomes; no forged App success |
| E22 | Attestation comment arrives after initial PR failure | Fresh verification and eligible CI dispatch |
| E23 | Duplicate events/publication and push races | No duplicated active CI; no acceptance for wrong context |
| E24 | Two different PR authors share the same head SHA | Ambiguous-context rejection; no cross-author status reuse |
| E25 | Manual dispatch bypass attempt | Full preflight still blocks expensive validation |
| E26 | GitHub/network unavailable or rate limited | Blocked result; no stale-policy fallback acceptance |
| E27 | Revoke only provider and attempt automatic self-enrollment | Human recovery required; no bypass |
| E28 | Proposed config changes commands to `true` | Current trusted policy still executes for that PR |
| E29 | Proposed Makefile changes `make test` semantics | Executed source change is visible; no claim of protected test meaning |
| E30 | Submodule/LFS/private-secret dependency/active merge queue | Explicit unsupported/setup diagnosis; no partial success |

Do not run a production attack harness against the user's machine without an
explicitly provisioned test profile and clearly scoped fixtures. Run tests that
exercise administrator/root powers only on a disposable machine, and classify
their success as outside the supported threat model rather than pretending to
prevent them.

## 14. Operations, rollout, and remaining external prerequisites

### 14.1 Logging and privacy

Record daemon job IDs, source and policy identities, step starts/outcomes,
cleanup/signing decisions, and verifier rejection codes. Controller audit state
is not writable by agents or workers. Keep raw stdout/stderr local, expire it after
seven days, and document that tests can print sensitive data. Envelopes expose
GitHub account/repository IDs, digests, timings, provider labels, and tool versions,
not private tokens, hostnames, or raw paths. Public enrollment records are public
keys and identifiers; copying them grants no signing authority.

### 14.2 Adoption readiness

Before enforcing in a target repository, prove all of the following:

- At least one working provider is installed and enrolled on the trusted branch.
- The approved policy is nonempty and representative of that repository's checks.
- Required toolchains are installed and source/secret limitations are compatible.
- Human maintainers own the registry, policy, CODEOWNERS, verifier, and privileged
  workflow configuration; the agent cannot bypass their review.
- The GitHub App is installed, its key is confined to the branch-restricted
  environment, and both required checks are pinned to that App.
- Report-only acceptance/rejection and final CI outcomes have been exercised.
- The official PR workflow has no unconditional expensive path at cutover.
- Branch freshness and reconciliation are configured; merge queues are disabled
  for enforced v1; rollback and last-key recovery have an assigned human owner.

Repository setup tooling may inspect these settings and prepare concrete changes,
but administrative setting changes must be reviewable and human-authorized. Do not
weaken an unavailable security feature to make setup appear successful.

### 14.3 External inputs needed during implementation

These are operational credentials or deployment facts, not unresolved product
architecture. Do not synthesize fake production values:

- Apple Developer ID team, signing certificate, and notarization access for release.
- Human-reviewed GitHub App registration/installation and protected environment.
- Actual repository administrators/code owners and first enrolled GitHub account.
- Read-only private-repository credential, if needed.
- Real supported macOS test machines and a disposable GitHub test repository.
- Independent security reviewer before production release.

Normal agent sandbox escalation to the developer UID is supported. If the user
clarifies that unrestricted root escalation must also be resistant to cheating,
the architecture must be reconsidered before implementation. Do not silently
turn this documented trust assumption into a stronger claim.

### 14.4 Deliberate later extensions

Add only after the v1 boundary works: Secure Enclave signing, Linux/Windows,
verified submodule/LFS resolution, scoped validation secrets, protected external
test suites, content-verified caches, multiple concurrent workers, GitHub Enterprise,
additional trusted target branches, queue-aware required checks, and hosted
attestation storage for envelopes exceeding metadata limits. Each extension needs
its own source/authorization/acceptance model and independently reviewed PRs.

## 15. Implementation handoff rules

1. Start with PR 01, then follow the dependency table. Do not open the enforcement
   adoption PR until the real provider and protected verifier exist.
2. Copy the relevant acceptance IDs into each PR and provide observed evidence.
   Security-sensitive tests must prove denial paths as well as happy paths.
3. Preserve the schema contracts and shared fixtures. A schema change needs an
   explicit version decision; do not make Swift/Python silently disagree.
4. Treat any inability to enforce the worker sandbox, provider-key isolation,
   trusted policy source, or protected GitHub App issuer as a blocking finding.
   Do not replace it with a prompt instruction or a mock test.
5. Follow repository AGENTS.md: dedicated worktree, shared development environment,
   scoped changes, complete required verification, feature branch, PR, no self-merge.
6. Keep administrator setup, review, and release approvals explicit. Ordinary
   validation must require no recurring human approval beyond the agent's existing
   command execution permissions.

## 16. Primary references

The architecture above is this project's proposed design. The linked standards
and product documentation establish the formats and platform behavior used by it.
Reconfirm platform details while implementing the associated PR.

- Unforgeable provenance separates trusted generation/signing from user execution:
  [SLSA build requirements](https://slsa.dev/spec/v1.2/build-requirements).
- Statement subjects and predicates:
  [in-toto Statement v1](https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md).
- Envelope format and byte/type binding:
  [in-toto envelope](https://github.com/in-toto/attestation/blob/main/spec/v1/envelope.md)
  and [DSSE protocol](https://github.com/secure-systems-lab/dsse/blob/master/protocol.md).
- Privileged component launch and library protection:
  [Apple launch/library constraints](https://developer.apple.com/documentation/security/applying-launch-environment-and-library-constraints).
- Worker sandbox and child inheritance:
  [Apple App Sandbox](https://developer.apple.com/documentation/security/protecting-user-data-with-app-sandbox)
  and [embedding command-line tools](https://developer.apple.com/documentation/xcode/embedding-a-helper-tool-in-a-sandboxed-app).
- Hardware key protection, which does not attest execution semantics:
  [Apple Secure Enclave keys](https://developer.apple.com/documentation/security/protecting-keys-with-the-secure-enclave).
- CODEOWNERS uses the target branch's ownership file; required review must be configured:
  [GitHub code owners](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).
- PR head, comment event context, and dispatch behavior:
  [GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
  and [workflow dispatch API](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).
- Privileged triggers must not execute untrusted PR code:
  [Secure use of pull_request_target](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
- Environment branch restrictions are matched to the workflow ref:
  [GitHub deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments).
- Required checks can pin the issuing App; skipped/neutral checks are insufficient
  to express a failed attestation gate:
  [GitHub protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)
  and [job conditions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions).
- Agent installation conventions:
  [Codex skills](https://learn.chatgpt.com/docs/build-skills),
  [Claude Code skills](https://code.claude.com/docs/en/skills),
  [GitHub Copilot skills](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills),
  [Cursor skills](https://cursor.com/docs/skills), and
  [OpenCode skills](https://opencode.ai/docs/skills/).
