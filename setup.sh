#!/bin/bash
# Sets up an isolated Python environment for the injector and prints the
# one-time setcap command you need to run yourself (it needs sudo, which
# this script deliberately does not run for you).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found -- install it first." >&2
    exit 1
fi

echo "[*] creating venv in $DIR/.venv"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install psutil

# The venv's python binary needs CAP_SYS_PTRACE to attach to processes that
# aren't its own descendants (Wine/Proton detach the actual game process
# from whatever launched it -- see README.md "How it works"). We copy the
# interpreter rather than granting the capability on a shared system-wide
# python, so the capability stays scoped to this one tool.
REAL_PY=$(readlink -f .venv/bin/python)
cp "$REAL_PY" .venv/bin/sider-inject-python

echo
echo "=============================================================================="
echo "[+] done. Run EXACTLY this command now (copy the line below, not one from"
echo "    the README -- the path is specific to where YOU cloned this repo):"
echo
echo "sudo setcap cap_sys_ptrace+eip \"$DIR/.venv/bin/sider-inject-python\""
echo
echo "=============================================================================="
echo
echo "After that, use $DIR/.venv/bin/sider-inject-python to run sider_inject.py"
echo "(see README.md and the run-*.sh templates for how to wire it into your launcher)."
