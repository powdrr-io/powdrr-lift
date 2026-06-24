# Powdrr-Lift Skills for OpenCode

This directory contains all the installable skills for the powdrr-lift memory system that can be used with OpenCode.

## Available Skills

1. **bootstrap** - Analyze repository specs and source to identify taxonomy-compliant entities, draft a validated changelog v2 document, and commit it.

2. **code-edit-context** - Use when you are about to edit code and need index-backed context for a file and line ranges. Ask powdrr-lift for the file and line ranges, inspect prior intent and justification, then decide whether to honor or supersede the earlier work.

3. **implement-pr** - Find a proposed PR by fuzzy search, inspect the full proposal, validate it against the current indexed specs and changelogs, implement the requested changes, review the proposal again, and then optionally generate the matching PR changelog.

4. **prepare-pr-changelog** - Use when preparing a pull request or getting a PR ready. Guides the agent through the PR changelog workflow with powdrr-lift.

5. **review-pr-changelog** - Use during code review when the change includes a changelog. This skill complements general code-review skills; do not replace normal review. Check for the changelog first, validate it, then review each change against the PR intent and report the feedback.

6. **specify-architecture** - Create, fill, and validate architecture specification templates with the repository's architecture-specification CLI or MCP endpoints. Use when Codex needs to define an architecture spec from a provided set of entity types, ensure entity types are allowed, and verify that relationship, invariant, and guidance references point to listed entities.

7. **specify-implementation** - Create, fill, and validate implementation specification templates with the repository's implementation-specification CLI or MCP endpoints. Use when Codex needs to define an implementation spec for a known architecture id, keep entity and relationship references constrained to that architecture version, and ensure feature and decision ids are unique.

8. **specify-prs** - Create, fill, and validate proposed PR specification templates with the repository's pr-specification CLI or MCP endpoints. Use when Codex needs to describe a proposed PR with feature references, intent, reasoning, and optional file updates, then validate that the PR id is unique and referenced features/files exist.

9. **specify-system** - Create, fill, and validate system specification templates with the repository's system-specification CLI or MCP endpoints. Use when Codex needs to draft a system description with requirements and approach items, enforce state-driven supersedence rules, and iterate until the specification validates.

10. **synchronize-code-and-state** - Generate the current codebase-state snapshot, compare it to the source tree and changelog index, and reconcile mismatches by changing code and/or the changelog while preserving the repo's intent. Use when Codex needs to align actual code with the indexed state after a PR, merged change, or state drift.

## Installation

### For OpenCode Users

1. These skills are automatically installed to `~/.config/opencode/skills/`
2. The `opencode.json` configuration file enables all skills for use by default agents
3. OpenCode will automatically discover and load these skills when available

### For Local Development

To use these skills in a specific project:

1. Copy the skills to the project's `.opencode/skills/` directory:
   ```bash
   cp -r skills/* .opencode/skills/
   ```

2. Update the project's `opencode.json` to include:
   ```json
   {
     "agent": {
       "default": {
         "permission": {
           "skill": {
             "bootstrap": "allow",
             "code-edit-context": "allow",
             "implement-pr": "allow",
             "prepare-pr-changelog": "allow",
             "review-pr-changelog": "allow",
             "specify-architecture": "allow",
             "specify-implementation": "allow",
             "specify-prs": "allow",
             "specify-system": "allow",
             "synchronize-code-and-state": "allow"
           }
         }
       }
     }
   }
   ```

## Usage

### Automatic Skill Loading

When you run OpenCode in a project with these skills installed, they will automatically be available. You can:

1. Call the `skill` tool with a skill name to load it:
   ```
   skill({ name: "specify-system" })
   ```

2. The agent will display available skills and can load them as needed

### Manual Skill Loading

You can manually invoke any skill by using the `skill` tool:

```bash
skill({ name: "bootstrap" })
skill({ name: "prepare-pr-changelog" })
skill({ name: "review-pr-changelog" })
```

### Skill Workflow Examples

1. **Preparing a PR**:
   - Use `prepare-pr-changelog` to guide through the PR changelog workflow
   - Use `review-pr-changelog` to validate and review the changelog during code review

2. **Implementing Changes**:
   - Use `specify-prs` to create a specification for proposed changes
   - Use `implement-pr` to execute the proposed changes
   - Use `code-edit-context` to get context for specific edits

3. **Code Review**:
   - Use `review-pr-changelog` to check changelogs and provide feedback
   - Use `specify-implementation` if you need to update the implementation spec

4. **Codebase Maintenance**:
   - Use `bootstrap` to analyze repository specs and create changelogs
   - Use `synchronize-code-and-state` to align code with indexed state
   - Use `specify-system` to create and validate system specifications

## Configuration

### Permissions

Skills can be configured with permissions to control which agents can access them:

```json
{
  "permission": {
    "skill": {
      "*": "allow",
      "internal-*": "deny",
      "experimental-*": "ask"
    }
  }
}
```

### Per-Agent Overrides

You can set different permissions for specific agents:

```json
{
  "agent": {
    "plan": {
      "permission": {
        "skill": {
          "internal-*": "allow"
        }
      }
    }
  }
}
```

## Troubleshooting

### Skills Not Appearing

1. Verify `SKILL.md` is spelled in all caps
2. Check that frontmatter includes `name` and `description`
3. Ensure skill names are unique across all locations
4. Check permissions—skills with `deny` are hidden from agents

### Loading Issues

If a skill doesn't load properly:

1. Check the skill's `SKILL.md` file structure
2. Verify the skill name matches the directory name
3. Ensure the description is 1-1024 characters
4. Check that skill names are lowercase alphanumeric with single hyphen separators

## Development

### Adding New Skills

1. Create a new directory under `skills/`
2. Create a `SKILL.md` file with YAML frontmatter:
   ```yaml
   ---
   name: my-skill
   description: A brief description of what this skill does
   license: MIT
   compatibility: opencode
   metadata:
     audience: developers
     workflow: standard
   ---
   ```

3. Add skill implementation in the `SKILL.md` file
4. Test the skill by loading it with the `skill` tool
5. Update this documentation as needed

## License

See individual skill licenses in their respective `SKILL.md` files.