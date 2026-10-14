"""SessionStart, shared part: silent unless something needs the user. Starts init/refresh in the background."""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config


def _bg(args, module=None):
    """Run `<plugin> <args>` (or another module of the plugin) detached; output goes to the state dir log."""
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    log = open(config.STATE_DIR / "background.log", "a")
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(__file__)))
    subprocess.Popen([sys.executable, "-m", module or f"{config.APP}.cli"] + args, stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True, env=env)


def _stamp(name):
    p = config.STATE_DIR / f"{name}.stamp"
    return p.stat().st_mtime if p.exists() else 0


def _touch(name):
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (config.STATE_DIR / f"{name}.stamp").write_text(time.strftime("%Y-%m-%dT%H:%M:%S"))


def init(kind, args, run):
    """`<plugin> init | refresh [--quiet]`: run(kind, quiet), after saying how to store a key when there's none."""
    from . import secrets
    if kind == "init" and "--quiet" not in args and not secrets.get("typesafe") and not secrets.get("ai-gateway"):
        print("No TypeSafe or AI Gateway key found. Store one (you'll be prompted for the value):")
        print("  " + secrets.store_hint("typesafe"))
    run(kind, quiet="--quiet" in args)


def link_cli():
    """~/.local/bin/<plugin> -> this install's CLI (plugin cache or release dir), when ~/.local/bin exists and nothing
    is at that name yet, or when it is our own link to another version of this install (same cache or release tree,
    same bin/<cli>), dangling or not. Windows uses a marked sh wrapper instead of a symlink."""
    root, link = os.environ.get("CLAUDE_PLUGIN_ROOT"), config.HOME / ".local" / "bin" / config.APP
    installs = (Path(os.environ.get("CLAUDE_CONFIG_DIR", config.HOME / ".claude")) / "plugins" / "cache",
                config.SHARED_HOME / "release")
    if not root or not link.parent.is_dir() or not any(Path(root).is_relative_to(d) for d in installs):
        return  # a --plugin-dir checkout would leave a link to a dir that goes away
    target = (Path(root) / "bin" / config.APP).as_posix()
    if os.path.lexists(link):
        old = Path(os.readlink(link)) if link.is_symlink() else None
        if sys.platform == "win32" and link.is_file() and not link.is_symlink():
            try:
                lines = link.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                return
            if len(lines) == 3 and lines[1] == f"# {config.APP} link_cli" and \
                    lines[2].startswith('exec "') and lines[2].endswith('" "$@"'):
                old = Path(re.sub(r'\\(.)', r'\1', lines[2].removeprefix('exec "').removesuffix('" "$@"')))
        if not old or old == Path(target) or old.parts[-2:] != ("bin", config.APP) or \
                old.parent.parent.parent != Path(root).parent:
            return
    tmp = link.with_name(f".{config.APP}.{os.getpid()}.tmp")
    try:  # rename: the link is never missing
        if sys.platform == "win32":
            quoted = re.sub(r'([\\"$`])', r'\\\1', target)  # inside sh double quotes
            tmp.write_text(f'#!/bin/sh\n# {config.APP} link_cli\nexec "{quoted}" "$@"\n', encoding="utf-8", newline="\n")
            tmp.chmod(0o755)
        else:
            tmp.symlink_to(target)
        os.replace(tmp, link)
    except OSError:
        tmp.unlink(missing_ok=True)


def start(inp):
    """(done, notice): done when there is nothing more to do this start (a nested probe, or no Jev key, then with
    the notice); else init or refresh is started in the background as due. Links the CLI onto PATH first."""
    link_cli()
    if os.environ.get("GUTCHECK_NESTED"):  # tokenmaxxing's own `claude -p` probes (mcpload)
        return True, None
    from . import jev
    if not jev.have_key():
        from . import secrets
        return True, (f"{config.APP}: no TypeSafe or AI Gateway key found; Jev features are off. To enable, store one "
                      f"with `{secrets.store_hint('typesafe').split('   (')[0]}` (or service `ai-gateway`), or set "
                      "GUTCHECK_SECRET_TYPESAFE in the environment Claude Code starts from.")
    if not config.CONFIG_PATH.exists():
        _bg(["init", "--quiet"])
    elif time.time() - _stamp("refresh") > 86400:
        _bg(["refresh", "--quiet"])
    return False, None
