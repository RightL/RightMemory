#!/usr/bin/env bash
# install.sh - bootstrap the shared RightMemory installer on macOS, Linux, or WSL.

set -u

for argument in "$@"; do
  if [ "$argument" = "-h" ] || [ "$argument" = "--help" ]; then
    echo "Usage: ./install.sh [--mode cli-agent|standalone] [<memory-root> <skills-target>]"
    exit 0
  fi
done

print_uv_install_guidance() {
  cat >&2 <<'EOF'

RightMemory uses uv to provision its Python runtime.
Install uv, restart your shell if needed, then rerun ./install.sh.

macOS:
  brew install uv
  # or: curl -LsSf https://astral.sh/uv/install.sh | sh

Linux / WSL:
  curl -LsSf https://astral.sh/uv/install.sh | sh

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
  # or: brew install git

Linux / WSL Debian or Ubuntu:
  sudo apt update && sudo apt install -y git

Linux Fedora:
  sudo dnf install git

Official git install guide:
  https://git-scm.com/book/en/v2/Getting-Started-Installing-Git
EOF
}

print_uv_python_guidance() {
  cat >&2 <<'EOF'

Could not find or provision Python >=3.11 with uv.

Make sure uv Python downloads are enabled, install Python 3.11+, or upgrade uv,
then rerun ./install.sh.

uv Python guide:
  https://docs.astral.sh/uv/guides/install-python/
EOF
}

echo "Checking installer requirements..."
missing=0
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

if ! bootstrap_python="$(uv python find --no-project ">=3.11" 2>/dev/null)" || [ -z "$bootstrap_python" ]; then
  print_uv_python_guidance
  exit 1
fi
if [ ! -x "$bootstrap_python" ]; then
  print_uv_python_guidance
  exit 1
fi
echo "  [ok]      Python >=3.11 via uv"
echo

repo_root="$(cd "$(dirname "$0")" && pwd)"
cd "$repo_root"
PYTHONUTF8=1 exec "$bootstrap_python" -m rightmemory.install_core "$@"
