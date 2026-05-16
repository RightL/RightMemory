#!/usr/bin/env bash
# install.sh — set up the RightMemory system on a new machine.
#
# Usage:
#   ./install.sh [--mode subagent|standalone] <memory-root> <skills-target>
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
#   ./install.sh --mode standalone ~/.rightmemory ~/.codex/skills

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 [--mode subagent|standalone] <memory-root> <skills-target>

Arguments:
  <memory-root>    Where MEMORY.md, MEMORY_*.md, and dream_logs/ will live.
  <skills-target>  Where RightMemory skill folders will be installed.

Modes:
  subagent     Install memory-orchestrator, memory-curator, and memory-dreamer skills.
  standalone  Install only a memory-orchestrator skill that calls the standalone CLI.

Options:
  --mode MODE    subagent (default) or standalone.
  -h, --help     Show this help.
EOF
}

MODE="subagent"

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
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
RIGHTMEMORY_HOME="$DATA_HOME/rightmemory"
RIGHTMEMORY_VENV="$RIGHTMEMORY_HOME/venv"
RIGHTMEMORY_BIN_DIR="$HOME/.local/bin"
RIGHTMEMORY_BIN="$RIGHTMEMORY_BIN_DIR/rightmemory"
EXAMPLE_START_MARKER="rightmemory:example:start"
EXAMPLE_END_MARKER="rightmemory:example:end"

echo "Installing RightMemory"
echo "  MODE         = $MODE"
echo "  MEMORY_ROOT  = $MEMORY_ROOT"
echo "  SKILLS_ROOT  = $SKILLS_TARGET"
if [ "$MODE" = "standalone" ]; then
  echo "  RUNTIME_HOME = $RIGHTMEMORY_HOME"
  echo "  RUNTIME_VENV = $RIGHTMEMORY_VENV"
  echo "  CLI_COMMAND  = rightmemory"
fi
echo

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

MEMORY_ROOT_SED="$(escape_sed_replacement "$MEMORY_ROOT")"
SKILLS_TARGET_SED="$(escape_sed_replacement "$SKILLS_TARGET")"

install_skill() {
  src="$1"
  skill_name="$2"
  dst_dir="$SKILLS_TARGET/$skill_name"
  dst="$dst_dir/SKILL.md"
  tmp="${dst}.tmp"
  mkdir -p "$dst_dir"
  sed -e "s|{{MEMORY_ROOT}}|$MEMORY_ROOT_SED|g" \
      -e "s|{{SKILLS_ROOT}}|$SKILLS_TARGET_SED|g" \
      "$src" > "$tmp"
  awk -v repo_root="$REPO_ROOT" '
    function emit(path, line) {
      while ((getline line < path) > 0) print line
      close(path)
    }
    $0 == "{{ROLE_PROMPT_RETRIEVE}}" { emit(repo_root "/rightmemory/prompts/retrieve.md"); next }
    $0 == "{{ROLE_PROMPT_UPDATE}}" { emit(repo_root "/rightmemory/prompts/update.md"); next }
    $0 == "{{ROLE_PROMPT_DREAMER}}" { emit(repo_root "/rightmemory/prompts/dreamer.md"); next }
    $0 == "{{ROLE_PROMPT_REVIEWER}}" { emit(repo_root "/rightmemory/prompts/reviewer.md"); next }
    { print }
  ' "$tmp" > "$dst"
  rm -f "$tmp"
  echo "  [install] $dst"
}

remove_skill_dir() {
  skill_name="$1"
  skill_dir="$SKILLS_TARGET/$skill_name"
  skill_file="$skill_dir/SKILL.md"

  if [ ! -e "$skill_dir" ]; then
    return
  fi

  if [ ! -f "$skill_file" ]; then
    echo "  [skip]    $skill_dir has no SKILL.md; left untouched"
    return
  fi

  if grep -Eq "^name:[[:space:]]*$skill_name[[:space:]]*$" "$skill_file"; then
    rm -rf "$skill_dir"
    echo "  [remove]  $skill_dir"
  else
    echo "  [skip]    $skill_dir does not identify as $skill_name; left untouched"
  fi
}

example_block_file() {
  dst="$1"
  awk -v end="$EXAMPLE_END_MARKER" '
    { print }
    index($0, end) { found = 1; exit }
    END { if (!found) exit 1 }
  ' "$REPO_ROOT/MEMORY.example.md" > "$dst"
}

refresh_marked_example() {
  memory_file="$1"
  block_file="$2"
  tmp="${memory_file}.tmp"
  awk -v start="$EXAMPLE_START_MARKER" -v end="$EXAMPLE_END_MARKER" -v block="$block_file" '
    function emit_block(line) {
      while ((getline line < block) > 0) print line
      close(block)
    }
    !skipping && index($0, start) { emit_block(); skipping = 1; changed = 1; next }
    skipping && index($0, end) { skipping = 0; next }
    !skipping { print }
    END { if (!changed || skipping) exit 1 }
  ' "$memory_file" > "$tmp"
  mv "$tmp" "$memory_file"
}

migrate_known_example() {
  memory_file="$1"
  block_file="$2"
  tmp="${memory_file}.tmp"
  first_line="$(sed -n '1p' "$memory_file")"

  if [ "$first_line" = "# Starter Knowledge Base {#starter-knowledge-base}" ]; then
    awk -v block="$block_file" '
      function emit_block(line) {
        while ((getline line < block) > 0) print line
        close(block)
      }
      NR == 1 { emit_block(); skipping = 1; changed = 1; next }
      skipping && $0 == "---" { skipping = 0; next }
      !skipping { print }
      END { if (!changed || skipping) exit 1 }
    ' "$memory_file" > "$tmp"
    mv "$tmp" "$memory_file"
    return 0
  fi

  if grep -q "^# Sample Project Graph .*{#sample-project-graph}" "$memory_file"; then
    awk -v block="$block_file" '
      function emit_block(line) {
        while ((getline line < block) > 0) print line
        close(block)
      }
      !changed && /^# Sample Project Graph .*{#sample-project-graph}/ { emit_block(); skipping = 1; changed = 1; next }
      skipping && /^# Cross-Session Agent Behavior/ { skipping = 0 }
      !skipping { print }
      END { if (!changed || skipping) exit 1 }
    ' "$memory_file" > "$tmp"
    mv "$tmp" "$memory_file"
    return 0
  fi

  rm -f "$tmp"
  return 1
}

install_or_refresh_memory() {
  memory_file="$MEMORY_ROOT/MEMORY.md"
  block_file="$(mktemp "${TMPDIR:-/tmp}/rightmemory-example-block.XXXXXX")"
  example_block_file "$block_file"

  if [ ! -f "$memory_file" ]; then
    cp "$REPO_ROOT/MEMORY.example.md" "$memory_file"
    echo "  [new]     $memory_file  (from MEMORY.example.md)"
    rm -f "$block_file"
    return
  fi

  if grep -q "$EXAMPLE_START_MARKER" "$memory_file" && grep -q "$EXAMPLE_END_MARKER" "$memory_file"; then
    refresh_marked_example "$memory_file" "$block_file"
    echo "  [refresh] $memory_file  (managed example block)"
    rm -f "$block_file"
    return
  fi

  if migrate_known_example "$memory_file" "$block_file"; then
    echo "  [refresh] $memory_file  (migrated known example block)"
  else
    echo "  [keep]    $memory_file already exists; no managed example block found"
  fi
  rm -f "$block_file"
}

install_standalone_runtime_layout() {
  if ! command -v uv >/dev/null 2>&1; then
    echo "Missing required command: uv" >&2
    echo "Install uv first: https://docs.astral.sh/uv/getting-started/installation/" >&2
    exit 1
  fi

  mkdir -p "$RIGHTMEMORY_HOME" "$RIGHTMEMORY_BIN_DIR"
  if [ ! -d "$RIGHTMEMORY_VENV" ]; then
    uv venv "$RIGHTMEMORY_VENV"
    echo "  [new]     $RIGHTMEMORY_VENV"
  else
    echo "  [keep]    $RIGHTMEMORY_VENV already exists"
  fi

  uv pip install --python "$RIGHTMEMORY_VENV/bin/python" "$REPO_ROOT"
  echo "  [install] rightmemory package into $RIGHTMEMORY_VENV"

  cat > "$RIGHTMEMORY_BIN" <<EOF
#!/usr/bin/env sh
export RIGHTMEMORY_ROOT="$MEMORY_ROOT"
exec "$RIGHTMEMORY_VENV/bin/python" -m rightmemory.cli "\$@"
EOF
  chmod 755 "$RIGHTMEMORY_BIN"
  echo "  [install] $RIGHTMEMORY_BIN"
}

# 1. MEMORY.md seed / managed example refresh
mkdir -p "$MEMORY_ROOT/dream_logs"
install_or_refresh_memory

# 2. Init git repo for memory tracking (the dreamer/reviewer need git for revertability)
if [ -d "$MEMORY_ROOT/.git" ]; then
  echo "  [keep]    $MEMORY_ROOT is already a git repo"
else
  (cd "$MEMORY_ROOT" && git init -q)
  echo "  [new]     git init in $MEMORY_ROOT"
fi

# Keep git status focused on memory artifacts. Existing user .gitignore files are preserved.
if [ -f "$MEMORY_ROOT/.gitignore" ]; then
  echo "  [keep]    $MEMORY_ROOT/.gitignore already exists"
else
  cat > "$MEMORY_ROOT/.gitignore" <<'EOF'
*
!MEMORY.md
!MEMORY_*.md
!dream_logs/
!dream_logs/*.md
EOF
  echo "  [new]     $MEMORY_ROOT/.gitignore  (memory allowlist)"
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
  install_standalone_runtime_layout
  install_skill "$REPO_ROOT/skills/memory-orchestrator-standalone/SKILL.md" "memory-orchestrator"
  remove_skill_dir "memory-curator"
  remove_skill_dir "memory-dreamer"
  echo "  [skip]    subagent skills; standalone mode uses rightmemory"
fi

echo
echo "Done. Next steps:"
echo "  1. Open $MEMORY_ROOT/MEMORY.md and replace the example domain with your own."
if [ "$MODE" = "subagent" ]; then
  echo "  2. Trigger any memory-relevant message in your AI agent — orchestrator picks it up."
  echo "  3. When you want consolidation, ask your agent to invoke the memory-dreamer skill."
else
  echo "  2. Write role model config to $MEMORY_ROOT/rightmemory.toml."
  echo "  3. Trigger any memory-relevant message in your AI agent — the installed orchestrator calls rightmemory."
fi
echo
echo "Re-run this script any time you pull updates from the RightMemory repo;"
echo "your existing MEMORY.md, MEMORY_*.md, and dream_logs/ are preserved."
