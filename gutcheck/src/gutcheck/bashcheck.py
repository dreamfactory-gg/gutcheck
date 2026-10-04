"""Pre-run Bash check. Returns None (run as today) or an `ask` reason. Never allows, never denies.

Order: parse and unwrap → deterministic ask rules → read-only allowlist → one Jev request.
"""
import json
import os
import time

from gutcheck_core import config, secretscan
from gutcheck_core.commands import _git_sub, analyze, normalize

from . import triggers


def annotate(command, cmds, cfg):
    notes = []
    s = cfg["sensitivity"]
    for host, label in s.get("hosts", {}).items():
        if host in command:
            notes.append(f"{host} is {label}")
    for proj, label in s.get("projects", {}).items():
        if proj in command:
            notes.append(f"{proj}: {label}")
    for c in cmds:
        if c.name == "git":
            sub, rest = _git_sub(c.argv)
            if sub == "push":
                for b, label in s.get("branches", {}).items():
                    if any(x == b or x.endswith(":" + b) or x.endswith("/" + b) for x in rest):
                        notes.append(f"pushes to {b} ({label})")
        if c.remote:
            label = next((l for h, l in s.get("hosts", {}).items() if h in c.remote), None)
            notes.append(f"runs on remote host {c.remote}" + (f" ({label})" if label else ""))
    return sorted(set(notes))

QUESTIONS = {
    "running": "Does this command stop, restart, kill, reset or reinstall a process, service, server, container, bot, "
               "database or application that may already be running (for example kill, pkill, systemctl stop or "
               "restart, docker stop/rm/restart, docker compose down, quitting an editor or game engine)? Starting "
               "something new does not count.",
    "delete": "Does this command permanently delete, truncate or overwrite existing files, directories, database "
              "tables or records? Writing new files, or deleting files in a temporary directory, does not count.",
    "git_discard": "Does this command discard uncommitted changes or rewrite git history (for example git checkout "
                   "-- <path>, git restore, git reset, git stash drop, git rebase, git commit --amend, "
                   "git branch -D, force push)? Committing, fetching or pushing normally does not count.",
    "shared": "Does this command change something on production or on a shared remote system (deploy, publish a "
              "package or release, act on a mainnet contract, restart or modify a shared server, push to a default "
              "branch), rather than only reading from it or changing local files?",
    "exfil": "Does this command send or upload local data, files or secrets to a third-party outside service "
             "(POST/PUT requests with data, uploads to a cloud or public service, posting messages)? Copying files "
             "to the user's own servers listed under targets, and downloading or reading, do not count.",
}


def jev_state(an, cwd, cfg):
    st = {"command": secretscan.redact(an.command)[:8000]}
    parts = []
    for c in an.cmds[:60]:
        line = " ".join(c.argv)[:400]
        tags = []
        if c.remote:
            tags.append(f"on {c.remote}")
        if c.via:
            tags.append("via " + " > ".join(c.via))
        parts.append(secretscan.redact(line) + (f"   [{', '.join(tags)}]" if tags else ""))
    st["runs"] = parts
    ann = annotate(an.command, an.cmds, cfg)
    if ann:
        st["targets"] = ann
    if cwd:
        st["working_directory"] = os.path.basename(cwd.rstrip("/"))
    for k, v in an.extra.items():
        st[k] = v
    s = json.dumps(st)
    if len(s) > 60_000:  # keep within Jev's state budget
        for k in ("scripts", "heredoc_scripts"):
            if k in st:
                st[k] = {p: t[:1500] for p, t in st[k].items()} if isinstance(st[k], dict) else [t[:1500] for t in st[k]]
    return st


def check(command, cwd=None, use_jev=True, deadline=None, session=None):
    """Full decision. Returns dict: {decision: None|"ask", reason, check, p, stage, ms}."""
    from gutcheck_core import jev
    t0 = time.monotonic()
    cfg = config.load()
    bc = cfg["bash_check"]
    deadline = deadline or bc.get("deadline_s", 3.0)
    an = analyze(command, cwd, cfg)
    res = {"decision": None, "reason": None, "check": None, "stage": None, "deletes": an.deletes}
    for pat in bc.get("approved_patterns", []):
        if normalize(command) == pat:
            res.update(stage="approved-pattern")
            res["ms"] = int((time.monotonic() - t0) * 1000)
            return res
    if an.asks:
        chk, why = an.asks[0]
        res.update(decision="ask", reason=f"gutcheck: {why}", check=chk, stage="rule", all=an.asks)
    elif an.readonly:
        res.update(stage="readonly")
    else:
        trig = triggers.triggers(an, cwd, cfg, session)
        definite = [t for t in trig if t["definite"]]
        fuzzy = [t for t in trig if not t["definite"]]
        res["triggers"] = [{k: t[k] for k in ("check", "why", "definite")} for t in trig]
        if definite:
            t = definite[0]
            res.update(decision="ask", check=t["check"], stage="trigger", reason=f"gutcheck: {t['why']}")
        elif fuzzy and use_jev:
            try:
                st = {"items": {f"c{i}": t["item"] for i, t in enumerate(fuzzy)}}
                ann = annotate(an.command, an.cmds, cfg)
                if ann:
                    st["targets"] = ann
                qs = {f"c{i}": {"type": "noul", "instructions": f'Consider only items["c{i}"]. ' +
                                triggers.Q[t["q"]][1]} for i, t in enumerate(fuzzy)}
                left = deadline - (time.monotonic() - t0)
                ans = jev.call(st, qs, timeout=left, deadline=left)
                th = bc["thresholds"]
                probs = [(round(ans[f"c{i}"]["noul"], 3), t) for i, t in enumerate(fuzzy)]
                res["probs"] = [(p, t["q"]) for p, t in probs]
                res["stage"] = "jev"
                over = sorted(((p, t) for p, t in probs if p >= th.get(t["q"], 0.5)), key=lambda x: -x[0])
                if over:
                    p, t = over[0]
                    res.update(decision="ask", check=t["check"], p=p, reason=f"gutcheck: {t['why']} (p={p:.2f})")
            except jev.JevError as e:
                if e.waf:  # the payload itself was blocked: we couldn't check it, so ask
                    res.update(stage="jev-waf-blocked", decision="ask", check="unchecked", error=str(e)[:300],
                               reason="gutcheck: couldn't check this command (TypeSafe's firewall rejected it)")
                else:  # outage or timeout: fail open
                    res.update(stage="jev-failed-open", error=str(e)[:300])
        elif fuzzy:
            res["stage"] = "needs-jev"
        else:
            res["stage"] = "no-trigger"
    res["ms"] = int((time.monotonic() - t0) * 1000)
    return res


LABELS = {"running": "may stop/restart something already running", "delete": "may permanently delete data",
          "git_discard": "may discard uncommitted work or rewrite git history",
          "shared": "acts on production or a shared remote system", "exfil": "sends data to an outside service"}


def cli(args):
    use_jev = "--no-jev" not in args
    args = [a for a in args if a != "--no-jev"]
    cwd = os.getcwd()
    if "--cwd" in args:
        i = args.index("--cwd")
        cwd = args[i + 1]
        del args[i:i + 2]
    cmd = " ".join(args) if args else __import__("sys").stdin.read()
    print(json.dumps(check(cmd, cwd, use_jev=use_jev), indent=1))
