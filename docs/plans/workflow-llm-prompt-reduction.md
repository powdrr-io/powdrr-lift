# Workflow LLM Prompt Reduction Plan

## Objective

Reduce the amount of recurring context sent to workflow LLMs so that each
request stays focused on the current decision, while preserving the facts
needed to execute the current step safely.

The plan applies to both interactive workflow chat and durable workflow-task
execution. It does not change the action contract or the complete execution
summary retained for diagnostics.

## Prompt information that must remain

Every execution request should retain:

- the user request or durable task goal;
- the current skill/task step and its completion condition;
- the current step index and execution mode;
- the allowed tools and constraints for the current step;
- the latest relevant action result or validation error;
- the current file path and concise file state when a file is in scope;
- durable requirements, invariants, decisions, and acceptance criteria;
- branch, worktree, and pull-request state when the current step operates on it.

The full transcript, event history, file contents, and diagnostic records remain
available to the runner and execution summary even when omitted from a prompt.

## Priority 0: remove the largest recurring payloads

### 1. Replace full-file context with bounded context

`_current_file_context` currently serializes every line of the active file on
each request. Replace it with:

- the relative path;
- whether the file exists;
- line count and a lightweight change marker;
- a small bounded excerpt only when useful;
- the latest `read_document` result when the model explicitly requested lines.

The prompt should direct the model to use `read_document` for additional
content. The full file must not be sent merely because it is the current file.

### 2. Send only the active skill step

The action prompt currently includes both `current_step` and the complete
`selected_skill`, including all steps. Replace the full skill payload with:

- skill name and file path;
- current step ID/index and total step count;
- the current step's description, details, completion condition, and permitted
  tools;
- a compact list of step IDs and descriptions only when navigation requires it.

Do not repeat the complete step definitions on every roundtrip.

### 3. Bound accumulated step context

`step_context` can grow with document excerpts, decisions, and corrections.
Maintain a compact state instead of sending the entire list:

- retain the latest relevant document/context result;
- retain unresolved validation feedback;
- retain explicit decisions needed by later steps;
- deduplicate repeated or superseded entries;
- cap both entry count and total characters.

When the cap is reached, preserve the newest actionable entries and a short
summary of older durable facts.

## Priority 1: remove duplicate representations

### 4. Separate execution state from the event log

The same information is currently represented in `transcript`,
`execution_context`, and `execution_events`. In particular, document and tool
results are copied into multiple structures.

Use a compact prompt state containing:

```text
goal
current step
durable facts
latest action
latest action result
latest error or correction
```

Continue retaining the complete transcript and events for persistence,
diagnostics, and recovery, but do not serialize all three into every LLM
request.

### 5. Compact event results for durable tasks

Durable tasks currently include recent event results, each with a relatively
large per-event limit. Send:

- recent event metadata;
- the latest failed result in enough detail to repair it;
- concise summaries for older successful actions;
- full output only when a later action explicitly depends on it.

Trigger context compaction before the prompt becomes large rather than waiting
until the model context is nearly exhausted.

### 6. Deduplicate decisions and corrections

`decisions_and_context`, validation errors, and no-progress corrections can be
copied into event metadata, transcript messages, and step context. Store each
fact once in a structured durable-facts section and reference it from action
history.

## Priority 2: make step handoffs explicit

Reducing context is only safe if the workflow knows which information must
cross a step boundary. A later step should not have to reconstruct its inputs
by rereading an arbitrary transcript.

### 7. Give every step an input/output contract

Represent each step with explicit declarations such as:

```yaml
id: validate-specification
inputs:
  - name: specification_path
    type: path
    required: true
    source: workflow_context
  - name: gathered_requirements
    type: context_records
    required: true
    source: previous_step
outputs:
  - name: validation_result
    type: validation_result
    required_for_next_step: true
```

Each input should identify its type, whether it is required, and where it is
resolved from. Each output should identify its type, whether it must be
available to the next step, and how long it remains in scope.

The skill templates and validators should require these declarations for steps
that depend on prior work. Existing skills can initially use inferred contracts
as a migration fallback, but new skills should be explicit.

### 8. Store handoff data as named context records

Actions should produce structured records rather than only appending free-form
text to the transcript. A record should include:

- a stable name or ID;
- a typed value or compact summary;
- the producing step and action;
- source/provenance, such as a file path or `gather_context` query;
- the scope and expiration point;
- whether later steps may rely on the record.

For example, `gather_context` should produce named records for the requested
context types and identifiers, while `read_document` should produce a document
excerpt record with its exact path and line range.

### 9. Build each next-step prompt from declared inputs

When a step completes, resolve the next step's declared inputs and construct a
handoff containing only:

- the next step's inputs;
- required durable facts and decisions;
- the immediately preceding result summary;
- unresolved errors or warnings;
- the current workflow/task identity.

Do not pass the entire prior step transcript by default. Keep the transcript in
the execution record for debugging, and expose only the records selected by
the next step's input contract.

### 10. Validate handoffs before advancing

Before `next_step` is accepted, validate that:

- every required output for the next step is present;
- each output has the declared type and expected identifier;
- referenced paths and work-item/proposed-PR identifiers resolve;
- context records are not stale or from an unrelated nested skill;
- the next prompt contains every required input exactly once.

If a required input is missing, return a structured correction to the current
step or use `prompt_user` when the missing fact cannot be produced locally.
Never silently advance with an empty or guessed handoff.

### 11. Keep provenance and scope through nested skills

Nested skills should declare which outputs they return to their caller. A clean
nested invocation may receive only its declared inputs and should return only
its declared outputs plus a concise completion summary. This prevents nested
transcripts and unrelated context from leaking into the parent prompt.

### 12. Test context flow independently from prompt size

Add workflow tests that verify:

- a producer step's named output appears in the consumer step's prompt;
- unrelated transcript entries do not appear in that prompt;
- missing required outputs prevent advancement;
- `gather_context`, `read_document`, `yaml_edit`, and validation results retain
  their identifiers and provenance;
- nested-skill outputs are returned to the parent with the declared scope;
- prompt compaction preserves all required handoff records.

## Priority 3: make capability descriptions conditional

### 13. Include available skills only when needed

The complete skill catalog is unnecessary for most steps. Include it only when:

- the current step permits `invoke_skill`; or
- the model is recovering from an invalid `invoke_skill` action.

Otherwise omit the catalog or provide only skill names.

### 14. Include context types only for context-gathering steps

The complete context-type catalog should be sent only when the current step can
use `gather_context`. Other steps do not need descriptions of every context
type.

### 15. Send only relevant tool descriptions

Keep the current step's permitted tool list, but do not include descriptions
for tools that cannot be called in that step. Use a compact shared description
for tools with stable behavior.

### 16. Make the action system prompt modular

Shorten the always-present action prompt to the core rules:

- return exactly one action;
- make progress on the current step;
- do not invent unavailable tools or identifiers;
- use the supplied action schema.

Move detailed instructions and examples into conditional sections for complex
actions such as `gather_context`, `yaml_edit`, `invoke_skill`, and
`read_document`. The parser remains the final validation authority.

## Priority 4: reduce incidental metadata

### 17. Minimize workflow context

Instead of serializing the complete previous workflow context every roundtrip,
send only the fields relevant to the current step:

- proposed PR ID;
- workflow directory;
- active task ID;
- integration and task branch names;
- relevant PR URLs and statuses;
- the current consistency/recovery state when applicable.

### 18. Use relative paths

The worktree is already the command execution root. Prefer `.` or omit the
absolute `worktree_root` path. Keep relative file paths in model-visible
context.

### 19. Trim selection-only data after selection

The routing prompt needs the full skill catalog and work-item matches, but the
execution prompt does not. Once a skill is selected, retain only the selected
skill identity and the execution-specific state.

## Suggested implementation sequence

1. Add prompt-size instrumentation by field and execution mode.
2. Bound or remove full-file context.
3. Replace the full selected-skill payload with the active-step payload.
4. Define step input/output contracts and named context records.
5. Build and validate each step handoff from declared inputs.
6. Introduce a bounded, deduplicated durable-facts structure.
7. Stop sending duplicate transcript/event/result data.
8. Make skill, context-type, and tool descriptions conditional.
9. Minimize workflow metadata and absolute paths.
10. Add tests asserting prompt-size bounds, context-flow correctness, and
    preservation of required action context.

## Safety and correctness constraints

- Never remove the latest action result or error needed to choose the next
  action.
- Never remove explicit requirements, invariants, acceptance criteria, or
  human decisions that remain in scope.
- Preserve complete execution data outside the prompt for diagnostics and
  recovery.
- Keep `read_document`, `gather_context`, and other retrieval actions available
  so omitted information can be requested rather than guessed.
- Validate that YAML/specification workflows still receive the identifiers and
  file paths required by their action instructions.

## Success criteria

- Prompt size is bounded independently of total workflow history.
- Large files do not automatically create large recurring prompts.
- Each request contains the current step and latest actionable state.
- LLM behavior remains correct for edits, YAML edits, context gathering,
  nested skills, validation failures, and pull-request operations.
- Full execution history remains available for logging, debugging, and
  recovery.
