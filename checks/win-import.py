"""Simulated-Windows import check: hooks load without fcntl, locking goes through msvcrt, withsecret returns the
child's exit code, link_cli writes a sh wrapper instead of a symlink, and process liveness goes through kernel32."""
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

(tmp / "a").mkdir()
(tmp / "a" / "sh").write_text("#!/bin/sh\nexit 9\n")  # an extensionless sh earlier on PATH than sh.exe
(tmp / "a" / "sh").chmod(0o755)
(tmp / "b").mkdir()
(tmp / "b" / "sh.exe").symlink_to("/bin/sh")
os.environ["PATH"] = os.pathsep.join([str(tmp / "a"), str(tmp / "b"), os.environ["PATH"]])
try:
    secrets.withsecret("dfhtest", "T", ["/bin/sh", "-c", '[ "$MSYS2_ENV_CONV_EXCL" = T= ] && [ "$T" = x ] && exit 3'])
    raise AssertionError("withsecret returned")
except SystemExit as e:
    assert e.code == 3, e.code

health.link_cli()
link = home / ".local" / "bin" / "tokenmaxxing"
assert link.is_file() and not link.is_symlink(), "link_cli did not write a regular file"
lines = link.read_text().splitlines()
assert lines[1] == "# tokenmaxxing link_cli", lines
assert f'exec "{root}/bin/tokenmaxxing" "$@"' in lines, lines
import ctypes  # noqa: E402
from tokenmaxxing import jobs, transitions  # noqa: E402

procs = {10: 259, 11: 0}  # pid -> exit code; 12 is someone else's (access denied); others don't exist
last = []


def open_process(access, inherit, pid):
    last[:] = [0 if pid in procs else 5 if pid == 12 else 87]
    return pid + 1000 if pid in procs else 0


def exit_code(h, ref):
    ref._obj.value = procs[h - 1000]
    return 1


closed = []
kernel32 = types.SimpleNamespace(OpenProcess=open_process, GetExitCodeProcess=exit_code, CloseHandle=closed.append)
ctypes.WinDLL = lambda name, use_last_error=False: kernel32 if name == "kernel32" and use_last_error else None
ctypes.get_last_error = lambda: last[0]
got = [jobs.alive(p) for p in (10, 11, 12, 13, None)]
assert got == [True, False, True, False, False], got
assert closed == [1010, 1011], closed
assert transitions._claude_pid() is None
shutil.rmtree(tmp)
print("win-import ok")
