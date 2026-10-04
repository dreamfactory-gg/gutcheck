#!/bin/sh
# Create the venv gutcheck and tokenmaxxing share (Python 3.12+, tree-sitter for the shell parser, lizard for review).
# Idempotent; either plugin's first hook runs it.
set -e
home="${GUTCHECK_HOME:-$HOME/.local/share/gutcheck}"
here=$(cd "$(dirname "$(realpath "$0")")" && pwd)
mkdir -p "$home"
lock="$home/.setup.lock"
mkdir "$lock" 2>/dev/null || exit 0
trap 'rmdir "$lock"' EXIT
if command -v uv >/dev/null 2>&1; then
  # uv fetches a Python 3.12 build itself when the machine has none
  [ -x "$home/venv/bin/python" ] || uv venv -q -p 3.12 "$home/venv" || uv venv -q -p '>=3.12' "$home/venv"
  uv pip install -q -p "$home/venv/bin/python" tree-sitter tree-sitter-bash lizard
else
  if [ ! -x "$home/venv/bin/python" ]; then
    sys_py=$("$here/bin/gutcheck-python" --system) || {
      echo "gutcheck: needs Python 3.12 or newer; install uv (https://docs.astral.sh/uv/) or python3.12+" >&2; exit 1; }
    "$sys_py" -m venv "$home/venv"
  fi
  "$home/venv/bin/pip" install -q tree-sitter tree-sitter-bash lizard
fi
echo "gutcheck: venv ready at $home/venv"
