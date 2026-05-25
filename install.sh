#!/usr/bin/env bash
# install.sh — set up the RightMemory system on a new machine.
#
# Usage:
#   ./install.sh [--mode cli-agent|standalone] [<memory-root> <skills-target>]
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
# If no paths are provided, use:
#   memory root:    ~/.rightmemory
#   skill targets:  ~/.codex/skills and ~/.claude/skills
#
# Modes:
#   cli-agent  Install a command-backed orchestrator skill that calls rightmemory.
#   standalone Install the same command-backed runtime layout for local standalone use.
#
# Requirements:
#   git and uv must be available on PATH. uv provisions Python >=3.11.
#
# Example:
#   ./install.sh
#   ./install.sh ~/.rightmemory ~/.codex/skills
#   ./install.sh --mode cli-agent ~/.rightmemory ~/.claude/skills

set -euo pipefail

usage() {
  cat >&2 <<EOF
Usage: $0 [--mode cli-agent|standalone] [<memory-root> <skills-target>]

Arguments:
  <memory-root>    Where MEMORY.md, MEMORY_*.md, and dream_logs/ will live.
  <skills-target>  Where RightMemory skill folders will be installed.
                   If omitted, defaults to ~/.rightmemory plus both
                   ~/.codex/skills and ~/.claude/skills.

Modes:
  cli-agent   Install a memory-orchestrator skill that calls the rightmemory command.
  standalone  Install the same command-backed runtime layout for local standalone use.

Requirements:
  git and uv must be available on PATH. uv provisions Python >=3.11.

Options:
  --mode MODE    standalone (default) or cli-agent.
  -h, --help     Show this help.
EOF
}

MODE="standalone"

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

if [ "$#" -eq 0 ]; then
  MEMORY_ROOT="$HOME/.rightmemory"
  SKILLS_TARGETS=("$HOME/.codex/skills" "$HOME/.claude/skills")
elif [ "$#" -eq 2 ]; then
  MEMORY_ROOT="$1"
  SKILLS_TARGETS=("$2")
else
  usage
  exit 1
fi

case "$MODE" in
  cli-agent|standalone)
    ;;
  subagent)
    echo "Unsupported --mode: subagent" >&2
    echo "Use --mode cli-agent for command-backed agent skill installs." >&2
    exit 1
    ;;
  *)
    echo "Invalid --mode: $MODE" >&2
    usage
    exit 1
    ;;
esac

print_uv_install_guidance() {
  cat >&2 <<'EOF'

RightMemory uses uv to create an isolated Python runtime.
Install uv, restart your shell if needed, then rerun ./install.sh.

macOS:
  brew install uv
  # or:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Linux / WSL:
  curl -LsSf https://astral.sh/uv/install.sh | sh

Windows:
  Use WSL, then run the Linux commands inside your WSL distro.

Official uv install guide:
  https://docs.astral.sh/uv/getting-started/installation/
EOF
}

print_git_install_guidance() {
  cat >&2 <<'EOF'

RightMemory uses git for inspectable memory changes, rollback, isolated worktrees, and sync.
Install git, restart your shell if needed, then rerun ./install.sh.

macOS:
  xcode-select --install
  # or:
  brew install git

Linux / WSL Debian or Ubuntu:
  sudo apt update && sudo apt install -y git

Linux Fedora:
  sudo dnf install git

Windows:
  Use WSL, then run the Linux commands inside your WSL distro.

Official git install guide:
  https://git-scm.com/book/en/v2/Getting-Started-Installing-Git
EOF
}

print_uv_python_guidance() {
  cat >&2 <<'EOF'

Could not find or provision Python >=3.11 with uv.

RightMemory asks uv to supply the Python runtime. Try again with network access,
make sure uv Python downloads are enabled, or install Python 3.11+ so uv can
discover it. If your uv is old, upgrade it and rerun ./install.sh.

uv Python guide:
  https://docs.astral.sh/uv/guides/install-python/
EOF
}

preflight_requirements() {
  missing=0

  echo "Checking installer requirements..."
  if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then
    echo "  [ok]      git"
  else
    echo "Missing or unusable required command: git" >&2
    print_git_install_guidance
    missing=1
  fi

  if command -v uv >/dev/null 2>&1 && uv --version >/dev/null 2>&1; then
    echo "  [ok]      uv"
  else
    echo "Missing or unusable required command: uv" >&2
    print_uv_install_guidance
    missing=1
  fi

  if [ "$missing" -ne 0 ]; then
    exit 1
  fi

  if uv python find --no-project ">=3.11" >/dev/null 2>&1; then
    echo "  [ok]      Python >=3.11 via uv"
  else
    print_uv_python_guidance
    exit 1
  fi
  echo
}

preflight_requirements

mkdir -p "$MEMORY_ROOT"
MEMORY_ROOT="$(cd "$MEMORY_ROOT" && pwd)"
for index in "${!SKILLS_TARGETS[@]}"; do
  mkdir -p "${SKILLS_TARGETS[$index]}"
  SKILLS_TARGETS[$index]="$(cd "${SKILLS_TARGETS[$index]}" && pwd)"
done

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
RIGHTMEMORY_HOME="$DATA_HOME/rightmemory"
RIGHTMEMORY_VENV="$RIGHTMEMORY_HOME/venv"
RIGHTMEMORY_BIN_DIR="$HOME/.local/bin"
RIGHTMEMORY_BIN="$RIGHTMEMORY_BIN_DIR/rightmemory"
EXAMPLE_START_MARKER="rightmemory:example:start"
EXAMPLE_END_MARKER="rightmemory:example:end"
INSTALL_STAMP="$MEMORY_ROOT/.runtime/install.stamp"
MEMORY_INSTALL_ACTION=""

echo "Installing RightMemory"
echo "  MODE         = $MODE"
echo "  MEMORY_ROOT  = $MEMORY_ROOT"
echo "  SKILLS_ROOTS = ${SKILLS_TARGETS[*]}"
echo "  RUNTIME_HOME = $RIGHTMEMORY_HOME"
echo "  RUNTIME_VENV = $RIGHTMEMORY_VENV"
echo "  CLI_COMMAND  = rightmemory"
echo

escape_sed_replacement() {
  printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

MEMORY_ROOT_SED="$(escape_sed_replacement "$MEMORY_ROOT")"

install_skill() {
  src="$1"
  skill_name="$2"
  skills_target="$3"
  skills_target_sed="$(escape_sed_replacement "$skills_target")"
  dst_dir="$skills_target/$skill_name"
  dst="$dst_dir/SKILL.md"
  tmp="${dst}.tmp"
  mkdir -p "$dst_dir"
  sed -e "s|{{MEMORY_ROOT}}|$MEMORY_ROOT_SED|g" \
      -e "s|{{SKILLS_ROOT}}|$skills_target_sed|g" \
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
  skills_target="$2"
  skill_dir="$skills_target/$skill_name"
  skill_file="$skill_dir/SKILL.md"

  if [ ! -e "$skill_dir" ]; then
    return
  fi

  if [ ! -f "$skill_file" ]; then
    echo "  [skip]    $skill_dir has no SKILL.md; left untouched"
    return
  fi

  if grep -Eq "^name:[[:space:]]*$skill_name[[:space:]]*$" "$skill_file" \
    && grep -q "subagent execution wrapper for RightMemory" "$skill_file"; then
    rm -rf "$skill_dir"
    echo "  [remove]  $skill_dir"
  else
    echo "  [skip]    $skill_dir is not an old RightMemory role skill; left untouched"
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
    MEMORY_INSTALL_ACTION="new"
    rm -f "$block_file"
    return
  fi

  if grep -q "$EXAMPLE_START_MARKER" "$memory_file" && grep -q "$EXAMPLE_END_MARKER" "$memory_file"; then
    refresh_marked_example "$memory_file" "$block_file"
    echo "  [refresh] $memory_file  (managed example block)"
    MEMORY_INSTALL_ACTION="refresh"
    rm -f "$block_file"
    return
  fi

  if migrate_known_example "$memory_file" "$block_file"; then
    echo "  [refresh] $memory_file  (migrated known example block)"
    MEMORY_INSTALL_ACTION="migrate"
  else
    echo "  [keep]    $memory_file already exists; no managed example block found"
    MEMORY_INSTALL_ACTION="keep"
  fi
  rm -f "$block_file"
}

install_cli_runtime_layout() {
  mkdir -p "$RIGHTMEMORY_HOME" "$RIGHTMEMORY_BIN_DIR"
  if [ ! -d "$RIGHTMEMORY_VENV" ]; then
    if ! uv venv --no-project --python ">=3.11" "$RIGHTMEMORY_VENV"; then
      print_uv_python_guidance
      exit 1
    fi
    echo "  [new]     $RIGHTMEMORY_VENV"
  else
    echo "  [keep]    $RIGHTMEMORY_VENV already exists"
  fi

  if ! uv pip install --python "$RIGHTMEMORY_VENV/bin/python" "$REPO_ROOT"; then
    cat >&2 <<EOF

Could not install RightMemory into the uv-managed runtime:
  $RIGHTMEMORY_VENV

Check the uv output above, then rerun ./install.sh.
EOF
    exit 1
  fi
  echo "  [install] rightmemory package into $RIGHTMEMORY_VENV"

  cat > "$RIGHTMEMORY_BIN" <<EOF
#!/usr/bin/env sh
export RIGHTMEMORY_ROOT="$MEMORY_ROOT"
exec "$RIGHTMEMORY_VENV/bin/python" -m rightmemory.cli "\$@"
EOF
  chmod 755 "$RIGHTMEMORY_BIN"
  echo "  [install] $RIGHTMEMORY_BIN"
}

refresh_semantic_upgrades() {
  "$RIGHTMEMORY_VENV/bin/python" -m rightmemory.semantic_upgrades refresh --memory-root "$MEMORY_ROOT"
}

baseline_semantic_upgrades() {
  "$RIGHTMEMORY_VENV/bin/python" -m rightmemory.semantic_upgrades baseline --memory-root "$MEMORY_ROOT"
}

warn_if_rightmemory_not_on_path() {
  resolved_rightmemory="$(command -v rightmemory || true)"
  if [ -z "$resolved_rightmemory" ]; then
    cat <<EOF
  [notice]  rightmemory is installed at $RIGHTMEMORY_BIN, but ~/.local/bin is not on PATH for this shell.
            Add it to your shell profile, then restart the agent or terminal:

              export PATH="\$HOME/.local/bin:\$PATH"

            For zsh, a common place is ~/.zshrc. For bash, use ~/.bashrc or ~/.bash_profile.
EOF
    return
  fi

  if [ "$resolved_rightmemory" != "$RIGHTMEMORY_BIN" ]; then
    cat <<EOF
  [notice]  rightmemory is installed at $RIGHTMEMORY_BIN, but PATH currently resolves rightmemory to:

              $resolved_rightmemory

            Put $RIGHTMEMORY_BIN_DIR earlier on PATH, then restart the agent or terminal:

              export PATH="\$HOME/.local/bin:\$PATH"

            Otherwise the orchestrator may call stale code or use the wrong RIGHTMEMORY_ROOT.
EOF
  fi
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

install_cli_runtime_layout
if [ "$MEMORY_INSTALL_ACTION" = "new" ]; then
  baseline_semantic_upgrades
else
  refresh_semantic_upgrades
fi
for skills_target in "${SKILLS_TARGETS[@]}"; do
  schema_dst="$skills_target/rightmemory-schema.md"
  cp "$REPO_ROOT/skills/rightmemory-schema.md" "$schema_dst"
  echo "  [install] $schema_dst"
  install_skill "$REPO_ROOT/skills/memory-orchestrator-cli/SKILL.md" "memory-orchestrator" "$skills_target"
  remove_skill_dir "memory-curator" "$skills_target"
  remove_skill_dir "memory-dreamer" "$skills_target"
done
echo "  [skip]    generated role skills; $MODE mode uses rightmemory"
warn_if_rightmemory_not_on_path

mkdir -p "$MEMORY_ROOT/.runtime"
if [ ! -f "$MEMORY_ROOT/.runtime/.gitignore" ]; then
  printf '*\n' > "$MEMORY_ROOT/.runtime/.gitignore"
fi
{
  date -u '+%Y-%m-%dT%H:%M:%SZ'
  printf 'mode=%s\n' "$MODE"
  printf 'repo=%s\n' "$REPO_ROOT"
} > "$INSTALL_STAMP"
echo "  [refresh] $INSTALL_STAMP"
echo "             running watch processes refresh after their current cycle or sleep check"
echo "             run rightmemory watch start or restart to start newly added watch targets"

echo
echo "Done. Next steps:"
echo "  1. Open $MEMORY_ROOT/MEMORY.md and replace the example domain with your own."
if [ "$MODE" = "cli-agent" ]; then
  echo "  2. Write [agent_cli] and [<role>.agent_cli] provider/model config to $MEMORY_ROOT/rightmemory.toml."
else
  echo "  2. Write role model config to $MEMORY_ROOT/rightmemory.toml."
fi
echo "  3. Trigger any memory-relevant message in your AI agent — the installed orchestrator calls rightmemory."
echo "  4. Optional background review, pruning, and dreams: rightmemory watch start"
echo
echo "Re-run this script any time you pull updates from the RightMemory repo;"
echo "your existing MEMORY.md, MEMORY_*.md, and dream_logs/ are preserved."
