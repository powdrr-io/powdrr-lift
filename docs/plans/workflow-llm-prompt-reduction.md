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

## Priority 2: make capability descriptions conditional

### 7. Include available skills only when needed

The complete skill catalog is unnecessary for most steps. Include it only when:

- the current step permits `invoke_skill`; or
- the model is recovering from an invalid `invoke_skill` action.

Otherwise omit the catalog or provide only skill names.

### 8. Include context types only for context-gathering steps

The complete context-type catalog should be sent only when the current step can
use `gather_context`. Other steps do not need descriptions of every context
type.

### 9. Send only relevant tool descriptions

Keep the current step's permitted tool list, but do not include descriptions
for tools that cannot be called in that step. Use a compact shared description
for tools with stable behavior.

### 10. Make the action system prompt modular

Shorten the always-present action prompt to the core rules:

- return exactly one action;
- make progress on the current step;
- do not invent unavailable tools or identifiers;
- use the supplied action schema.

Move detailed instructions and examples into conditional sections for complex
actions such as `gather_context`, `yaml_edit`, `invoke_skill`, and
`read_document`. The parser remains the final validation authority.

## Priority 3: reduce incidental metadata

### 11. Minimize workflow context

Instead of serializing the complete previous workflow context every roundtrip,
send only the fields relevant to the current step:

- proposed PR ID;
- workflow directory;
- active task ID;
- integration and task branch names;
- relevant PR URLs and statuses;
- the current consistency/recovery state when applicable.

### 12. Use relative paths

The worktree is already the command execution root. Prefer `.` or omit the
absolute `worktree_root` path. Keep relative file paths in model-visible
context.

### 13. Trim selection-only data after selection

The routing prompt needs the full skill catalog and work-item matches, but the
execution prompt does not. Once a skill is selected, retain only the selected
skill identity and the execution-specific state.

## Suggested implementation sequence

1. Add prompt-size instrumentation by field and execution mode.
2. Bound or remove full-file context.
3. Replace the full selected-skill payload with the active-step payload.
4. Introduce a bounded, deduplicated durable-facts structure.
5. Stop sending duplicate transcript/event/result data.
6. Make skill, context-type, and tool descriptions conditional.
7. Minimize workflow metadata and absolute paths.
8. Add tests asserting both prompt-size bounds and preservation of required
   action context.

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
