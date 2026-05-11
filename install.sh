#!/usr/bin/env bash
# install.sh — set up the RightMemory system on a new machine.
#
# Usage:
#   ./install.sh [--mode subagent|standalone] [--rightmemory-cmd CMD] <memory-root> <skills-target>
#
# Arguments:
#   <memory-root>    Where MEMORY.md, MEMORY_*.md, and dream_logs/ will live.
#                    e.g. ~/.rightmemory  or  ~/Documents/memory
#
#   <skills-target>  Where RightMemory skill folders will be installed.
#                    Common locations:
#                      Claude Code (user):     ~/.claude/skills
#                      Claude Code (project):  <project>/.claude/skills
#                      Codex:                  ~/.codex/skills
#                      Other agents: see your agent's skill loading docs.
#
# Modes:
#   subagent    Install orchestrator, curator, and dreamer skills for agents with subagents.
#   standalone Install an orchestrator skill that calls the standalone rightmemory CLI.
#
# Example:
#   ./install.sh ~/.rightmemory ~/.claude/skills
#   ./install.sh --mode standalone --rightmemory-cmd ~/.local/bin/rightmemory ~/.rightmemory ~/.codex/skills

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 [--mode subagent|standalone] [--rightmemory-cmd CMD] <memory-root> <skills-target>

Arguments:
  <memory-root>    Where MEMORY.md, MEMORY_*.md, and dream_logs/ will live.
  <skills-target>  Where RightMemory skill folders will be installed.

Modes:
  subagent     Install memory-orchestrator, memory-curator, and memory-dreamer skills.
  standalone  Install only a memory-orchestrator skill that calls the standalone CLI.

Options:
  --mode MODE              subagent (default) or standalone.
  --rightmemory-cmd CMD    Command used by the standalone orchestrator skill (default: rightmemory).
  -h, --help               Show this help.
EOF
}

MODE="subagent"
RIGHTMEMORY_CMD="rightmemory"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode)
      if [ "$#" -lt 2 ]; then
        echo "Missing value for --mode" >&2
        usage
        exit 1
      fi
      MODE="$2"
      shift 2
      ;;
    --mode=*)
      MODE="${1#--mode=}"
      shift
      ;;
    --rightmemory-cmd)
      if [ "$#" -lt 2 ]; then
        echo "Missing value for --rightmemory-cmd" >&2
        usage
        exit 1
      fi
      RIGHTMEMORY_CMD="$2"
      shift 2
      ;;
    --rightmemory-cmd=*)
      RIGHTMEMORY_CMD="${1#--rightmemory-cmd=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
    *)
      break
      ;;
  esac
done

if [ "$#" -ne 2 ]; then
  usage
  exit 1
fi

case "$MODE" in
  subagent|standalone)
    ;;
  *)
    echo "Invalid --mode: $MODE" >&2
    usage
    exit 1
    ;;
esac

MEMORY_ROOT="$1"
SKILLS_TARGET="$2"

mkdir -p "$MEMORY_ROOT" "$SKILLS_TARGET"
MEMORY_ROOT="$(cd "$MEMORY_ROOT" && pwd)"
SKILLS_TARGET="$(cd "$SKILLS_TARGET" && pwd)"

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "Installing RightMemory"
echo "  MODE         = $MODE"
echo "  MEMORY_ROOT  = $MEMORY_ROOT"
echo "  SKILLS_ROOT  = $SKILLS_TARGET"
if [ "$MODE" = "standalone" ]; then
  echo "  CLI_COMMAND  = $RIGHTMEMORY_CMD"
fi
echo

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

MEMORY_ROOT_SED="$(escape_sed_replacement "$MEMORY_ROOT")"
SKILLS_TARGET_SED="$(escape_sed_replacement "$SKILLS_TARGET")"
RIGHTMEMORY_CMD_SED="$(escape_sed_replacement "$RIGHTMEMORY_CMD")"

install_skill() {
  src="$1"
  skill_name="$2"
  dst_dir="$SKILLS_TARGET/$skill_name"
  dst="$dst_dir/SKILL.md"
  mkdir -p "$dst_dir"
  sed -e "s|{{MEMORY_ROOT}}|$MEMORY_ROOT_SED|g" \
      -e "s|{{SKILLS_ROOT}}|$SKILLS_TARGET_SED|g" \
      -e "s|{{RIGHTMEMORY_CMD}}|$RIGHTMEMORY_CMD_SED|g" \
      "$src" > "$dst"
  echo "  [install] $dst"
}

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

# 3. Install shared schema and mode-specific skills with path substitution
schema_dst="$SKILLS_TARGET/rightmemory-schema.md"
cp "$REPO_ROOT/skills/rightmemory-schema.md" "$schema_dst"
echo "  [install] $schema_dst"

if [ "$MODE" = "subagent" ]; then
  for skill in memory-orchestrator memory-curator memory-dreamer; do
    install_skill "$REPO_ROOT/skills/$skill/SKILL.md" "$skill"
  done
else
  install_skill "$REPO_ROOT/skills/memory-orchestrator-standalone/SKILL.md" "memory-orchestrator"
  echo "  [skip]    memory-curator and memory-dreamer skills; standalone mode uses RIGHTMEMORY_ROOT=$MEMORY_ROOT $RIGHTMEMORY_CMD"
  if [ -e "$SKILLS_TARGET/memory-curator" ] || [ -e "$SKILLS_TARGET/memory-dreamer" ]; then
    echo "  [note]    Existing memory-curator or memory-dreamer skill folders were left untouched."
    echo "            Disable or remove them in your agent if you want standalone-only behavior."
  fi
fi

echo
echo "Done. Next steps:"
echo "  1. Open $MEMORY_ROOT/MEMORY.md and replace the example domain with your own."
if [ "$MODE" = "subagent" ]; then
  echo "  2. Trigger any memory-relevant message in your AI agent — orchestrator picks it up."
  echo "  3. When you want consolidation, ask your agent to invoke the memory-dreamer skill."
else
  echo "  2. Install and configure the standalone CLI if needed:"
  echo "     uv --cache-dir .uv-cache venv .venv"
  echo "     uv --cache-dir .uv-cache pip install -e . --python .venv/bin/python"
  echo "     Write role model config to $MEMORY_ROOT/rightmemory.toml."
  echo "  3. Trigger any memory-relevant message in your AI agent — the installed orchestrator calls $RIGHTMEMORY_CMD with RIGHTMEMORY_ROOT=$MEMORY_ROOT."
fi
echo
echo "Re-run this script any time you pull updates from the RightMemory repo;"
echo "your existing MEMORY.md, MEMORY_*.md, and dream_logs/ are preserved."
