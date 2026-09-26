"""gutcheck command line.

  gutcheck jev ask STATE QUESTIONS          STATE: file or - ; QUESTIONS: JSON string or file
  gutcheck jev filter "CONDITION" [FILE|-] [--by line|block] [--min 0.5] [--all] [--context N]
  gutcheck jev pick "QUESTION" "name: description" ... [--state FILE|-]
  gutcheck withsecret SERVICE ENV_NAME -- COMMAND...
  gutcheck recall "QUESTION" [--days N] | --at PATH:LINE | --error "LINE"   past decisions, fixes, approaches
  gutcheck checkpoint build TRANSCRIPT [--until-line N] [--full] [--meta]
  gutcheck skill-find "TASK"
  gutcheck check-bash "COMMAND"             dry-run the pre-run Bash check
  gutcheck report                           what the hooks did
  gutcheck studio list | preload on|off|status   Roblox Studio instances; preload the Studio tools
  gutcheck studio sync|path|check           experimental local script mirror (off unless [studio] mirror = true)
  gutcheck soldier-note [AGENT_TYPE] [--path] [--dir D]   a retired subagent's handoff note for this project
  gutcheck relevance [--repo D] [--commit SHA|--base REF] [--focus T]   shared relevance pass for a change
  gutcheck review --background|--wait "FOCUS" [--repo D] [--commit SHA|--base REF]   Codex review + test-gap + dup-complexity + docs-drift
  gutcheck review --status [ID]             where background review results are
  gutcheck evidence REPORT_FILE|- [--cwd D] [--transcript JSONL]   check a report's cited evidence
  gutcheck explain [--repo D] [--commit SHA|--base REF] [--slug S] [--background]   Codex-written explainer page
  gutcheck house-rules [--repo D]           check the unpushed diff against the project's CLAUDE.md rules
  gutcheck init | refresh

Inside a Claude session the owner can also type /gutcheck:report [days], /gutcheck:recall QUESTION and
/gutcheck:init (user-only skills: they cost no context until typed).
"""
import json
import os
import sys

from . import jev


def _read(src):
    return sys.stdin.read() if src in (None, "-") else open(src, encoding="utf-8", errors="replace").read()


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def jev_ask(args):
    state = _read(args[0])
    q = args[1]
    questions = json.loads(q if q.lstrip().startswith("{") else open(q).read())
    print(json.dumps(jev.call(state, questions), indent=1))


def split_items(text, by):
    if by == "block":
        items, cur, start = [], [], 0
        for n, line in enumerate(text.splitlines()):
            if line.strip():
                if not cur:
                    start = n
                cur.append(line)
            elif cur:
                items.append((start, "\n".join(cur)))
                cur = []
        if cur:
            items.append((start, "\n".join(cur)))
        return items
    return [(n, t) for n, t in enumerate(text.splitlines()) if t.strip()]


def jev_filter(args):
    by = _opt(args, "--by", "line")
    threshold = float(_opt(args, "--min", "0.5"))
    ctx = int(_opt(args, "--context", "0"))
    show_all = _flag(args, "--all")
    condition = args[0]
    text = _read(args[1] if len(args) > 1 else "-")
    items = split_items(text, by)
    probs = jev.noul_each(items, condition)
    kept = [(n, probs[n], t) for n, t in items if show_all or probs[n] >= threshold]
    if ctx and by == "line":
        lines = text.splitlines()
        shown = set()
        for n, _, _ in kept:
            shown.update(range(max(0, n - ctx), min(len(lines), n + ctx + 1)))
        hits = {n: p for n, p, _ in kept}
        prev = None
        for n in sorted(shown):
            if prev is not None and n != prev + 1:
                print("--")
            mark = f"{hits[n]:.2f}" if n in hits else "    "
            print(f"{n + 1}\t{mark}\t{lines[n]}")
            prev = n
    else:
        for n, p, t in kept:
            print(f"{n + 1}\t{p:.2f}\t{t}")
    unit = "line" if by == "line" else "block start line"
    print(f"# {len(kept)}/{len(items)} items matched (min {threshold}); first column is the 1-based {unit}",
          file=sys.stderr)


def jev_pick(args):
    state_src = _opt(args, "--state")
    question, options = args[0], args[1:]
    criteria = {}
    for o in options:
        name, _, desc = o.partition(":")
        criteria[name.strip()] = desc.strip() or name.strip()
    print(json.dumps(jev.pick(question, criteria, _read(state_src) if state_src else None)))


def withsecret(args):
    from . import secrets
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
        if cmd == "jev":
            if not args or args[0] not in ("ask", "filter", "pick"):
                sys.exit(__doc__)
            {"ask": jev_ask, "filter": jev_filter, "pick": jev_pick}[args[0]](args[1:])
        elif cmd == "withsecret":
            withsecret(args)
        elif cmd == "recall":
            from . import recall
            recall.main(args)
        elif cmd == "checkpoint":
            from . import checkpoint
            checkpoint.main(args)
        elif cmd == "skill-find":
            from . import skillfind
            skillfind.main(args)
        elif cmd == "check-bash":
            from . import bashcheck
            bashcheck.cli(args)
        elif cmd == "report":
            from . import report
            report.main(args)
        elif cmd == "savings":
            from . import savings
            savings.main(args)
        elif cmd in ("init", "refresh"):
            from . import init
            init.main(cmd, args)
        elif cmd == "studio":
            from .adapters import roblox_studio
            roblox_studio.cli(args)
        elif cmd == "soldier-note":
            from . import soldier
            soldier.cli(args)
        elif cmd in ("relevance", "review", "evidence", "explain", "house-rules"):
            import importlib
            mod = {"house-rules": "pushcheck"}.get(cmd, cmd)
            importlib.import_module(f"gutcheck.{mod}").main(args)
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
