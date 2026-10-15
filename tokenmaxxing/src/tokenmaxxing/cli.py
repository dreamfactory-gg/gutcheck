"""tokenmaxxing command line.

  tokenmaxxing withsecret SERVICE ENV_NAME -- COMMAND...
  tokenmaxxing jev ask STATE QUESTIONS          STATE: file or - ; QUESTIONS: JSON string or file
  tokenmaxxing jev filter "CONDITION" [FILE|-] [--by line|block] [--min 0.5] [--all] [--context N]
  tokenmaxxing jev pick "QUESTION" "name: description" ... [--state FILE|-]
  tokenmaxxing recall "QUESTION" [--days N] | --at PATH:LINE | --error "LINE"   past decisions, fixes, approaches
  tokenmaxxing checkpoint build TRANSCRIPT [--until-line N] [--full] [--meta]
  tokenmaxxing skill-find "TASK"
  tokenmaxxing report [DAYS]                    what the hooks did
  tokenmaxxing studio list | preload on|off|status   Roblox Studio instances; preload the Studio tools
  tokenmaxxing studio sync|path|check           experimental local script mirror (off unless [studio] mirror = true)
  tokenmaxxing soldier-note [AGENT_TYPE] [--path] [--dir D]   a retired subagent's handoff note for this project
  tokenmaxxing relevance [--repo D] [--commit SHA|--base REF] [--focus T]   shared relevance pass for a change
  tokenmaxxing review --background|--wait "FOCUS" [--repo D] [--commit SHA|--base REF]   Codex review + test-gap + dup-complexity + docs-drift
  tokenmaxxing review --status [ID]             where background review results are
  tokenmaxxing evidence REPORT_FILE|- [--cwd D] [--transcript JSONL]   check a report's cited evidence
  tokenmaxxing explain [--repo D] [--commit SHA|--base REF] [--slug S] [--background]   Codex-written explainer page
  tokenmaxxing mcp [--measure] | off|on NAME...   what each MCP server costs per request; default-off list
  tokenmaxxing init | refresh
  tokenmaxxing config SECTION | log NAME JSON | save NAME EXT   for the mod (hooks/register.ts): a config section;
                                                a log record; stdin saved to the saved-output store (prints its path)

Inside a Claude session the owner can also type /tokenmaxxing:report [days], /tokenmaxxing:recall QUESTION and
/tokenmaxxing:init (user-only skills: they cost no context until typed).
"""
import json
import sys

from gutcheck_core import jev
from gutcheck_core.args import _flag, _opt, _read


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
            from gutcheck_core import secrets
            if len(args) < 4 or args[2] != "--":
                sys.exit("usage: tokenmaxxing withsecret SERVICE ENV_NAME -- COMMAND...")
            secrets.withsecret(args[0], args[1], args[3:])
        elif cmd == "recall":
            from . import recall
            recall.main(args)
        elif cmd == "checkpoint":
            from . import checkpoint
            checkpoint.main(args)
        elif cmd == "skill-find":
            from . import skillfind
            skillfind.main(args)
        elif cmd == "report":
            from . import report
            report.main(args)
        elif cmd == "savings":
            from . import savings
            savings.main(args)
        elif cmd in ("init", "refresh"):
            from gutcheck_core import health

            from . import init
            health.init(cmd, args, init.run)
        elif cmd == "mcp":
            from . import mcpload
            mcpload.main(args)
        elif cmd == "studio":
            from .adapters import roblox_studio
            roblox_studio.cli(args)
        elif cmd == "soldier-note":
            from . import soldier
            soldier.cli(args)
        elif cmd in ("relevance", "review", "evidence", "explain"):
            import importlib
            importlib.import_module(f"tokenmaxxing.{cmd}").main(args)
        elif cmd == "config":
            from gutcheck_core import config
            print(json.dumps(config.load().get(args[0], {})))
        elif cmd == "log":
            from gutcheck_core.hookio import log
            log(args[0], json.loads(args[1]))
        elif cmd == "save":
            from . import mcpout
            print(mcpout.save(args[0], sys.stdin.read(), args[1]))
        elif cmd == "hook":
            from . import hooks
            hooks.main(args)
        else:
            sys.exit(__doc__)
    except jev.JevError as e:
        sys.exit(f"tokenmaxxing: jev: {e}")
    except BrokenPipeError:
        pass


if __name__ == "__main__":
    main()
