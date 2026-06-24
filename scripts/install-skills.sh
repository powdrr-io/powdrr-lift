#!/bin/bash
set -e

# Install Powdrr-Lift skills for OpenCode

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_DIR="$REPO_ROOT/skills"
GLOBAL_CONFIG_DIR="$HOME/.config/opencode/skills"

echo "🔧 Installing Powdrr-Lift skills for OpenCode..."
echo ""

# Create global config directory if it doesn't exist
mkdir -p "$GLOBAL_CONFIG_DIR"

# Copy skills to global config
echo "📦 Copying skills to: $GLOBAL_CONFIG_DIR"
if [ -d "$SKILLS_DIR" ]; then
    cp -r "$SKILLS_DIR"/* "$GLOBAL_CONFIG_DIR/"
    echo "✓ Installed $(ls -1 "$GLOBAL_CONFIG_DIR" | wc -l | tr -d ' ') skills"
else
    echo "✗ Skills directory not found: $SKILLS_DIR"
    exit 1
fi

# Copy opencode.json to home directory
echo ""
echo "⚙️  Installing opencode.json configuration..."
OPCODE_CONFIG="$HOME/.config/opencode/opencode.json"
if [ -f "$OPCODE_CONFIG" ]; then
    echo "  opencode.json already exists at $OPCODE_CONFIG"
    echo "  Please manually merge the skill permissions into your existing config"
else
    cp "$REPO_ROOT/opencode.json" "$OPCODE_CONFIG"
    echo "✓ Installed opencode.json"
fi

echo ""
echo "✅ Installation complete!"
echo ""
echo "📚 Skills are now available in OpenCode."
echo "📖 See OPENSENSE.md for usage documentation."
echo ""
echo "To verify the installation, run:"
echo "  ls -1 ~/.config/opencode/skills/"
