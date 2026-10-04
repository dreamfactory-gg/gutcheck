"""gutcheck command line.

  gutcheck withsecret SERVICE ENV_NAME -- COMMAND...
  gutcheck check-bash "COMMAND"             dry-run the pre-run Bash check
  gutcheck house-rules [--repo D]           check the unpushed diff against the project's CLAUDE.md rules
  gutcheck report [DAYS]                    what the guards did
  gutcheck init | refresh
"""
import os
import sys

from gutcheck_core import jev


def withsecret(args):
    from gutcheck_core import secrets
    if len(args) < 4 or args[2] != "--":
        sys.exit("usage: gutcheck withsecret SERVICE ENV_NAME -- COMMAND...")
    service, name, cmd = args[0], args[1], args[3:]
    v = secrets.get(service)
    if not v:
        sys.exit(f"withsecret: no secret stored for '{service}'. Store it with: {secrets.store_hint(service)}")
    os.environ[name] = v
    os.execvp(cmd[0], cmd)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        sys.exit(__doc__)
    cmd, args = argv[0], argv[1:]
    try:
        if cmd == "withsecret":
            withsecret(args)
        elif cmd == "check-bash":
            from . import bashcheck
            bashcheck.cli(args)
        elif cmd == "report":
            from . import report
            report.main(args)
        elif cmd in ("init", "refresh"):
            from . import init
            init.main(cmd, args)
        elif cmd == "house-rules":
            from . import pushcheck
            pushcheck.main(args)
        elif cmd == "hook":
            from . import hooks
            hooks.main(args)
        else:
            sys.exit(__doc__)
    except jev.JevError as e:
        sys.exit(f"gutcheck: jev: {e}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
