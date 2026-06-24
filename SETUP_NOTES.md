# Skills Installation for Distribution

## What Was Done

Successfully made all 10 powdrr-lift skills installable from this repository.

## Files Created

### Installation Script
- **`scripts/install-skills.sh`** - One-line installation script for users

### Configuration
- **`opencode.json`** - OpenCode configuration enabling all skills for default agents

### Documentation
- **`OPENSENSE.md`** - Comprehensive guide for using skills with OpenCode
- **`INSTALLATION_SUMMARY.md`** - Installation notes and verification
- **`QUICK_REFERENCE.md`** - Quick reference for common operations

## Available Skills (10 total)

1. **bootstrap** - Analyze repository structure and source code to identify taxonomy-compliant entities, relationships, and features. Generate a validated system specification document from the analysis and commit it.

2. **code-edit-context** - Provide index-backed context for code edits with file and line range information

3. **implement-pr** - Find and implement proposed PRs with validation against specs and changelogs

4. **prepare-pr-changelog** - Guide through the PR changelog workflow

5. **review-pr-changelog** - Review PRs with changelog validation

6. **specify-architecture** - Create and validate architecture specifications

7. **specify-implementation** - Create and validate implementation specifications

8. **specify-prs** - Create and validate PR specifications

9. **specify-system** - Create and validate system specifications

10. **synchronize-code-and-state** - Align code with indexed state

## How Users Install

### Simple Installation
```bash
./scripts/install-skills.sh
```

### Manual Installation
```bash
# Create directory
mkdir -p ~/.config/opencode/skills

# Copy skills
cp -r skills/* ~/.config/opencode/skills/

# Install configuration
mkdir -p ~/.config/opencode
cp opencode.json ~/.config/opencode/opencode.json
```

## What Users Get

After installation, OpenCode users get:

1. **10 Installable Skills** - All skills are automatically enabled
2. **Proper Configuration** - opencode.json enables all skills
3. **Documentation** - Quick reference and comprehensive guides
4. **Easy Updates** - Can re-run install script to update

## Testing Installation

```bash
# Run the install script
./scripts/install-skills.sh

# Verify skills are installed
ls -1 ~/.config/opencode/skills/

# Should show 10 skills:
# bootstrap
# code-edit-context
# implement-pr
# prepare-pr-changelog
# review-pr-changelog
# specify-architecture
# specify-implementation
# specify-prs
# specify-system
# synchronize-code-and-state
```

## Skills Directory Structure

```
skills/
├── bootstrap/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── code-edit-context/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── implement-pr/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── prepare-pr-changelog/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── review-pr-changelog/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── specify-architecture/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── specify-implementation/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── specify-prs/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
├── specify-system/
│   ├── SKILL.md
│   └── agents/
│       └── openai.yaml
└── synchronize-code-and-state/
    ├── SKILL.md
    └── agents/
        └── openai.yaml
```

## Usage

After installation, users can:

1. Run `opencode` in their project
2. The skills will be available automatically
3. Use the `skill` tool to invoke specific skills:
   ```bash
   skill({ name: "prepare-pr-changelog" })
   skill({ name: "review-pr-changelog" })
   skill({ name: "specify-system" })
   ```

4. Skills are automatically loaded and will appear in the available skills list

## Next Steps

1. **Test installation** - Run the install script to verify it works
2. **Create PR** - When satisfied, create a pull request for the changes
3. **Document** - Add this to the main README with installation instructions

## License

All skills are MIT-licensed. See individual skill SKILL.md files for license information.