#!/usr/bin/env bash
# install.sh — set up the rightmem memory system on a new machine.
#
# Usage:
#   ./install.sh <memory-root> <skills-target>
#
# Arguments:
#   <memory-root>    Where MEMORY.md and dream_logs/ will live.
#                    e.g. ~/.rightmem  or  ~/Documents/memory
#
#   <skills-target>  Where the three skill folders will be installed.
#                    Common locations:
#                      Claude Code (user):     ~/.claude/skills
#                      Claude Code (project):  <project>/.claude/skills
#                      Codex:                  ~/.codex/skills
#                      Other agents: see your agent's skill loading docs.
#
# Example:
#   ./install.sh ~/.rightmem ~/.claude/skills

set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <memory-root> <skills-target>" >&2
  exit 1
fi

MEMORY_ROOT="$1"
SKILLS_TARGET="$2"

mkdir -p "$MEMORY_ROOT" "$SKILLS_TARGET"
MEMORY_ROOT="$(cd "$MEMORY_ROOT" && pwd)"
SKILLS_TARGET="$(cd "$SKILLS_TARGET" && pwd)"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Installing rightmem memory system"
echo "  MEMORY_ROOT  = $MEMORY_ROOT"
echo "  SKILLS_ROOT  = $SKILLS_TARGET"
echo

# 1. Initial MEMORY.md (do not overwrite existing user data)
mkdir -p "$MEMORY_ROOT/dream_logs"
if [ -f "$MEMORY_ROOT/MEMORY.md" ]; then
  echo "  [keep]    $MEMORY_ROOT/MEMORY.md already exists"
else
  cp "$REPO_ROOT/MEMORY.example.md" "$MEMORY_ROOT/MEMORY.md"
  echo "  [new]     $MEMORY_ROOT/MEMORY.md  (from MEMORY.example.md)"
fi

# 2. Init git repo for memory tracking (the dreamer needs git for revertability)
if [ -d "$MEMORY_ROOT/.git" ]; then
  echo "  [keep]    $MEMORY_ROOT is already a git repo"
else
  (cd "$MEMORY_ROOT" && git init -q)
  echo "  [new]     git init in $MEMORY_ROOT"
fi

# 3. Install skills with path substitution
for skill in memory-orchestrator memory-curator memory-dreamer; do
  src="$REPO_ROOT/skills/$skill/SKILL.md"
  dst_dir="$SKILLS_TARGET/$skill"
  dst="$dst_dir/SKILL.md"
  mkdir -p "$dst_dir"
  sed -e "s|{{MEMORY_ROOT}}|$MEMORY_ROOT|g" \
      -e "s|{{SKILLS_ROOT}}|$SKILLS_TARGET|g" \
      "$src" > "$dst"
  echo "  [install] $dst"
done

echo
echo "Done. Next steps:"
echo "  1. Open $MEMORY_ROOT/MEMORY.md and replace the example domain with your own."
echo "  2. Trigger any memory-relevant message in your AI agent — orchestrator picks it up."
echo "  3. When you want consolidation, ask your agent to invoke the memory-dreamer skill."
echo
echo "Re-run this script any time you pull updates from the rightmem repo;"
echo "your existing MEMORY.md and dream_logs/ are preserved."
