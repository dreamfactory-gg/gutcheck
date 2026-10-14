"""Simulated-Windows import check: hooks load without fcntl, locking goes through msvcrt, withsecret returns the
child's exit code, and link_cli writes a sh wrapper instead of a symlink."""
import os
import shutil
import subprocess  # noqa: F401  imported before sys.platform is faked: it picks its backend at import
import sys
import tempfile
import types
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
home = tmp / "home"
root = home / ".claude" / "plugins" / "cache" / "mp" / "tokenmaxxing" / "abc"
(home / ".local" / "bin").mkdir(parents=True)
root.mkdir(parents=True)
os.environ.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(home / ".claude"), CLAUDE_PLUGIN_ROOT=str(root),
                  GUTCHECK_SECRET_DFHTEST="x")

sys.path.insert(0, "tokenmaxxing/src")
sys.platform = "win32"
sys.modules["fcntl"] = None
calls = []
sys.modules["msvcrt"] = types.SimpleNamespace(locking=lambda *a: calls.append(a), LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=0)

import tokenmaxxing.hooks  # noqa: E402,F401
import tokenmaxxing.transitions  # noqa: E402,F401
import tokenmaxxing.checkpoint  # noqa: E402,F401
import tokenmaxxing.index  # noqa: E402,F401
from gutcheck_core import health, lock, secrets  # noqa: E402

with open(tmp / "lockfile", "a+") as fh, lock.locked(fh):
    pass
assert calls, "locked() did not call msvcrt.locking"

try:
    secrets.withsecret("dfhtest", "T", ["sh", "-c", "exit 3"])
    raise AssertionError("withsecret returned")
except SystemExit as e:
    assert e.code == 3, e.code

health.link_cli()
link = home / ".local" / "bin" / "tokenmaxxing"
assert link.is_file() and not link.is_symlink(), "link_cli did not write a regular file"
lines = link.read_text().splitlines()
assert lines[1] == "# tokenmaxxing link_cli", lines
assert f'exec "{root}/bin/tokenmaxxing" "$@"' in lines, lines
shutil.rmtree(tmp)
print("win-import ok")
