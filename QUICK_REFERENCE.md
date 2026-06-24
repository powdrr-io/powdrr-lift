# Powdrr-Lift Skills Quick Reference

## Installation

### Quick Install
```bash
./scripts/install-skills.sh
```

### Manual Install
```bash
# Create global skills directory
mkdir -p ~/.config/opencode/skills

# Copy skills from repository
cp -r skills/* ~/.config/opencode/skills/

# Create or update opencode.json
# Add skill permissions to your ~/.config/opencode/opencode.json
```

## Available Skills

### For PR Workflow
- **prepare-pr-changelog** - Start a new PR changelog
- **review-pr-changelog** - Validate and review PR changelogs
- **implement-pr** - Execute proposed PRs
- **specify-prs** - Create PR specifications

### For Specification Creation
- **specify-system** - Create system-level specifications
- **specify-architecture** - Create architecture specifications
- **specify-implementation** - Create implementation specifications

### For Code Management
- **bootstrap** - Analyze repository structure and generate comprehensive specifications
- **synchronize-code-and-state** - Align code with indexed state
- **code-edit-context** - Get context for code edits

## Common Workflows

### Creating a New PR
1. Use `prepare-pr-changelog` to create the changelog
2. Make your changes
3. Use `review-pr-changelog` to validate and review

### Creating Specifications
1. Use `specify-system` for high-level system specs
2. Use `specify-architecture` for architecture specs
3. Use `specify-implementation` for implementation specs
4. Use `specify-prs` for PR-specific specifications

### Code Review
1. Use `review-pr-changelog` first to check the changelog
2. Review each change against PR intent
3. Provide feedback on validity and completeness

## Manual Skill Invocation

You can manually invoke any skill using the `skill` tool:

```bash
skill({ name: "<skill-name>" })
```

Examples:
```bash
skill({ name: "prepare-pr-changelog" })
skill({ name: "review-pr-changelog" })
skill({ name: "specify-system" })
skill({ name: "bootstrap" })
```

## Configuration

### Permission Control

Enable specific skills in `opencode.json`:

```json
{
  "permission": {
    "skill": {
      "*": "allow",
      "internal-*": "deny"
    }
  }
}
```

### Per-Agent Overrides

Different permissions for different agents:

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
- Verify `SKILL.md` is in all caps
- Check frontmatter has `name` and `description`
- Ensure skill names are lowercase alphanumeric with single hyphens
- Check permissions (deny hides skills)

### Loading Issues
- Verify skill name matches directory name
- Ensure description is 1-1024 characters
- Check skills are not hidden by permissions

## Learning More

- Full documentation: See `OPENSENSE.md`
- Installation guide: See `INSTALLATION_SUMMARY.md`
- Skill structure: See `skills/<name>/SKILL.md` for each skill