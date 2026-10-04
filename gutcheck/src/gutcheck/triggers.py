"""Risk triggers per simple command.

Each trigger is either definite (ask without Jev) or needs a focused Jev judgment over a small state
(the one simple command plus its annotations), which is where Jev is reliable. Commands with no trigger
run as today.
"""
import os
import re
import subprocess

from gutcheck_core import secretscan

LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "::1")
DEPLOY_WORDS = re.compile(r"(?i)^(deploy|publish|release|upload|promote|rollout|migrate|apply|destroy|drop|purge|"
                          r"prune|rollback|restart|shutdown|reboot|uninstall|wipe|nuke|flush|flushall|flushdb|"
                          r"truncate|decommission|revoke|rotate)$")
SCRIPT_RISK = re.compile(
    r"systemctl\s+(restart|stop|kill|disable|mask|reload)|docker[^\n]{0,60}\b(stop|kill|rm|restart|down)\b|"
    r"\bpkill\b|\bkillall\b|os\.kill|\.kill\(|signal\.SIG(TERM|KILL)|\bscp\b|\brsync\b|"
    r"requests\.(post|put|patch|delete)|method\s*=\s*['\"](POST|PUT|PATCH|DELETE)|urlopen\([^)]*data|"
    r"-X\s*(POST|PUT|PATCH|DELETE)|shutil\.rmtree|os\.remove|os\.unlink|\.unlink\(|rm\s+-[a-z]*r[a-z]*f|"
    r"\b(DROP|TRUNCATE|DELETE\s+FROM|ALTER\s+TABLE)\b|git\s+push|git\s+reset\s+--hard|wrangler\s+(deploy|publish|"
    r"secret|kv|d1|r2)|npm\s+publish|\bssh\b", re.I)


def _flags(argv):
    out = set()
    for x in argv[1:]:
        if x == "--":
            break
        if x.startswith("--"):
            out.add(x.split("=", 1)[0])
        elif x.startswith("-") and len(x) > 1:
            out.update("-" + ch for ch in x[1:])
    return out


def _pos(argv, start=1):
    return [x for x in argv[start:] if not x.startswith("-")]


def _git_sub(argv):
    i = 1
    while i < len(argv):
        x = argv[i]
        if x in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
            continue
        if x.startswith("-"):
            i += 1
            continue
        return x, argv[i + 1:]
    return "", []


def _git_dir(argv, cwd):
    if "-C" in argv:
        i = argv.index("-C")
        if i + 1 < len(argv):
            d = os.path.expanduser(argv[i + 1])
            return d if os.path.isabs(d) else os.path.join(cwd or ".", d)
    return cwd


def _label_for(text, cfg):
    s = cfg["sensitivity"]
    hits = []
    for k, v in s.get("projects", {}).items():
        if k and k in text:
            hits.append((k, v))
    return hits


def _is_live(path_or_text, cfg):
    """True if the text names something the config marks live/production (and not scratch)."""
    labels = _label_for(path_or_text, cfg)
    live = [v for k, v in labels if re.search(r"(?i)live|production|prod\b|mainnet", v)]
    scratch = [k for k, v in labels if re.search(r"(?i)scratch|safe to change|temporary", v)]
    return bool(live) and not scratch


def _protected_branch(argv, cfg):
    sub, rest = _git_sub(argv)
    branches = cfg["sensitivity"].get("branches", {})
    for x in _pos(["git"] + rest)[1:]:
        dest = x.split(":")[-1]
        dest = dest.removeprefix("refs/heads/")
        if dest in branches:
            return dest
    return None


def _tracked(path, cwd):
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "ls-files", "--error-unmatch", "--", path],
                           capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def _ignored(path, cwd):
    try:
        r = subprocess.run(["git", "-C", cwd or ".", "check-ignore", "-q", "--", path], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False


def _is_tmp(p):
    return p.startswith(("/tmp/", "/private/tmp/", "/var/folders/", "$TMPDIR", "${TMPDIR")) or "/scratchpad" in p


def cmd_text(c):
    s = " ".join(c.argv)
    tags = []
    if c.remote:
        tags.append(f"runs on {c.remote}")
    if c.via:
        tags.append("via " + " > ".join(c.via))
    return secretscan.redact(s[:1500]) + (f"   [{', '.join(tags)}]" if tags else "")


# Focused Jev questions (asked about one item at a time)
Q = {
    "stop_target": ("running", "This command stops, kills or restarts something. Is the target a long-running "
                               "service, server, daemon, container, database, bot, editor or desktop app (for example a "
                               "systemd unit, a docker container, a model server, Roblox Studio or Blender), rather than "
                               "a test run, build, compile, one-off script, or a background job the same command "
                               "started?"),
    "remote_write": ("exfil", "Does this HTTP request create, change, delete, upload or send data on a remote "
                              "service (for example creating a record, posting a message, uploading a file, changing "
                              "config), rather than only querying or reading data? JSON-RPC read calls such as "
                              "eth_call, search queries and GraphQL queries count as reading."),
    "deployish": ("shared", "Does this command deploy, publish, upload, delete, stop or change something outside the "
                            "local working copy (a remote server, cloud service, package registry, production system "
                            "or shared account)? Building, testing, previewing or running locally does not count."),
    "script_lines": ("running", "These are the risky-looking calls (with a few lines of context) from a script the "
                                "command runs. Do they stop or restart services or containers, deploy, permanently "
                                "delete the user's data, or create, change or delete records on a remote service? "
                                "Deleting temporary, cache, partial-download or scratch files the script itself "
                                "created does not count, and neither does sending a prompt or query to a model or "
                                "search API, or reading data."),
}


def _checkout_paths(rest, cwd):
    """git checkout <path>... (no branch switch): any argument that is an existing file."""
    pos = [x for x in rest if not x.startswith("-")]
    if not pos:
        return False
    for x in pos[1:] if len(pos) > 1 else pos:
        p = x if os.path.isabs(x) else os.path.join(cwd or ".", x)
        if os.path.isfile(p) or ("/" in x and re.search(r"\.[A-Za-z0-9]{1,6}$", x)):
            return True
    return False


def _paths_after_dashdash(rest):
    if "--" in rest:
        return [x for x in rest[rest.index("--") + 1:]]
    pos = [x for x in rest if not x.startswith("-")]
    return pos[1:] if len(pos) > 1 else pos


def _has_changes(repo, paths, remote):
    """Whether the paths have uncommitted changes. Unknown (remote, no repo, error) counts as yes."""
    if remote or not repo or not os.path.isdir(repo):
        return True
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--porcelain", "--"] + [p for p in paths if "$" not in p],
                           capture_output=True, text=True, timeout=3)
        return r.returncode != 0 or bool(r.stdout.strip())
    except Exception:
        return True


def _own_job(argv, session):
    """True when the kill target was started earlier in this session (its pattern or PID shows up there)."""
    session = session or ""
    targets = [x for x in argv[1:] if not x.startswith("-")]
    if not targets:
        return False
    for t in targets:
        t = t.strip("'\"")
        if re.search(r"-[0-9a-f]{16}$", t) or re.search(r"\b(cargo test|cargo nextest|pytest|node -e|python3? -c|"
                                                        r"npm (run )?test|vitest|jest|tsc --watch)\b", t) or \
                re.match(r"^(ssh\b.*\s-[LRDN]|journalctl\b|tail -[fF]|sleep\b|watch\b|until\b|caffeinate\b)", t):
            continue  # a test binary or one-off script, not a service
        if t.isdigit() or t.startswith("$"):
            if t.isdigit() and re.search(r"(?<!\d)" + t + r"(?!\d)", session):
                continue
            return False
        # pkill -f patterns: literal words of the pattern must appear in an earlier command/output
        words = [w for w in re.split(r"[^A-Za-z0-9_\-./]+", t) if len(w) >= 4]
        if not words or not all(w in session for w in words):
            return False
    return True


def triggers(an, cwd, cfg, session=None):
    """Return list of dicts: {check, why, definite: bool, q: key into Q, item: text}."""
    out = []
    cur = cwd
    for c in an.cmds:
        a, n = c.argv, c.name
        if not a:
            continue
        if n == "cd" and not c.remote:
            p = _pos(a)
            if p and "$" not in p[0]:
                d = os.path.expanduser(p[0])
                cur = os.path.normpath(d if os.path.isabs(d) else os.path.join(cur or "/", d))
            continue
        text = " ".join(a)
        fl = _flags(a)
        remote_live = bool(c.remote) and _is_live(text, cfg)

        def add(check, why, definite=True, q=None):
            out.append({"check": check, "why": why, "definite": definite, "q": q, "item": cmd_text(c)})

        # --- stopping things that may be running
        if n in ("kill", "pkill", "killall") and not any(x in ("-0", "-s0", "-l", "-L") for x in a[1:]):
            ka = a
            if n == "kill" and all(x.startswith("$") or x.startswith("-") for x in a[1:]):
                # `for p in $(pgrep -f PAT); do kill $p`: judge the pgrep pattern
                src = next((x for x in an.cmds if x.name in ("pgrep", "pidof") and x.remote == c.remote), None)
                if src is not None:
                    ka = ["pkill"] + [y for y in src.argv[1:] if not y.startswith("-") and not y.isdigit()]
            if _own_job(ka, session):
                pass  # the target is a job this session started (or a test binary)
            elif c.remote:
                add("running", f"{n} on {c.remote}: {' '.join(a[1:])[:80]}")
            else:
                add("running", f"{n} {' '.join(a[1:])[:80]}", definite=False, q="stop_target")
        elif n in ("systemctl", "service") and _pos(a) and _pos(a)[0] in ("restart", "stop", "kill", "reload",
                                                                         "disable", "mask", "try-restart",
                                                                         "reload-or-restart", "isolate"):
            add("running", f"systemctl {_pos(a)[0]} {' '.join(_pos(a)[1:])[:60]}")
        elif n in ("systemctl", "service") and c.remote and _pos(a) and _pos(a)[0] in ("start", "enable",
                                                                                        "daemon-reload", "unmask",
                                                                                        "edit", "set-property",
                                                                                        "link", "preset"):
            add("shared", f"systemctl {_pos(a)[0]} on {c.remote}")
        elif n == "launchctl" and _pos(a) and _pos(a)[0] in ("unload", "bootout", "stop", "remove", "kickstart",
                                                             "kill", "disable"):
            add("running", f"launchctl {_pos(a)[0]}")
        elif n in ("docker", "podman", "nerdctl"):
            pos = [x for i, x in enumerate(a[1:], 1) if not x.startswith("-") and a[i - 1] not in ("-H", "--host",
                                                                                                  "--context")]
            if pos[:1] == ["compose"]:
                pos = pos[1:]
            if c.remote and pos[:1] in (["start"], ["up"], ["create"], ["update"], ["exec"]) and \
                    pos[:1] != ["exec"]:
                add("shared", f"{n} {pos[0]} on {c.remote}")
            if pos and pos[0] in ("stop", "kill", "rm", "restart", "down", "pause", "rmi", "prune") or \
                    pos[:2] in (["container", "rm"], ["container", "stop"], ["system", "prune"], ["volume", "rm"],
                                ["volume", "prune"], ["network", "rm"]) or \
                    (pos[:1] == ["up"] and ("--force-recreate" in a or "-V" in a)) or \
                    (pos[:1] == ["run"] and "--rm" not in a and "--name" in a):
                add("running", f"{n} {' '.join(pos[:3])}")
        elif n in ("brew",) and a[1:2] == ["services"] and _pos(a)[1:2] and _pos(a)[1] in ("stop", "restart",
                                                                                          "kill"):
            add("running", "brew services " + _pos(a)[1])
        elif n == "tmux" and _pos(a) and _pos(a)[0] in ("kill-session", "kill-server", "kill-window", "kill-pane"):
            add("running", f"tmux {_pos(a)[0]}", definite=False, q="stop_target")
        elif n == "osascript" and re.search(r"(?i)\bquit\b", text):
            add("running", "quits an app via osascript")
        elif n in ("pm2", "supervisorctl") and _pos(a) and _pos(a)[0] in ("stop", "restart", "delete", "kill",
                                                                          "reload"):
            add("running", f"{n} {_pos(a)[0]}")
        elif n in ("reboot", "shutdown", "halt", "poweroff"):
            add("running", n)

        # --- git
        if n == "git":
            sub, rest = _git_sub(a)
            rfl = _flags(["git"] + rest)
            if sub == "push":
                b = _protected_branch(a, cfg)
                if b and not ({"--dry-run", "-n"} & rfl):
                    add("shared", f"pushes to {b}")
            elif sub == "checkout" and ("--" in rest or "-f" in rfl or "--force" in rfl or rest[:1] == ["."] or
                                        _checkout_paths(rest, _git_dir(a, cur))):
                if _has_changes(_git_dir(a, cur), _paths_after_dashdash(rest), c.remote):
                    add("git_discard", "git checkout of paths/force discards uncommitted changes")
            elif sub == "restore" and "--staged" not in rest or sub == "restore" and "--worktree" in rest:
                if _has_changes(_git_dir(a, cur), _paths_after_dashdash(rest), c.remote):
                    add("git_discard", "git restore discards uncommitted changes")
            elif sub == "stash" and rest[:1] in (["drop"], ["clear"]):
                add("git_discard", f"git stash {rest[0]}")
            elif sub == "rebase" and not ({"--abort", "--continue", "--skip"} & set(rest)):
                add("git_discard", "git rebase rewrites history")
            elif sub == "commit" and "--amend" in rest:
                add("git_discard", "git commit --amend rewrites history")
            elif sub == "branch" and ({"-D"} & rfl or "--delete" in rest and "--force" in rest):
                add("git_discard", "git branch -D deletes unmerged work")
            elif sub == "reset" and not ({"--soft"} & set(rest)) and _pos(["git"] + rest)[1:] and \
                    "--hard" not in rest and "--mixed" not in rest and not _pos(["git"] + rest)[1:][0].startswith("HEAD"):
                pass  # `git reset <path>` only unstages
            elif sub == "worktree" and rest[:1] == ["remove"] and ("--force" in rest or "-f" in rfl):
                add("git_discard", "git worktree remove --force discards changes")
            elif sub in ("checkout", "switch", "merge", "pull", "reset") and remote_live:
                add("shared", f"git {sub} in a live checkout ({c.remote})")

        # --- deletes
        if n in ("rm", "unlink", "rmdir") and not c.remote:
            targets = [t for t in _pos(a) if t not in ("--",)]
            for t in targets:
                p = os.path.expanduser(t)
                ap = os.path.normpath(p if os.path.isabs(p) else os.path.join(cur or "/", p))
                if _is_tmp(ap) or "$" in t or "*" in t and _is_tmp(ap):
                    continue
                if not os.path.exists(ap):
                    continue
                if os.path.isfile(ap) and _tracked(ap, os.path.dirname(ap)):
                    continue  # recoverable from git
                if _ignored(ap, os.path.dirname(ap) if os.path.isfile(ap) else ap) and \
                        os.path.basename(ap.rstrip("/")) in ("target", "node_modules", "dist", "build", "__pycache__",
                                                             ".pytest_cache", ".mypy_cache", ".next", ".turbo",
                                                             "coverage", ".cache", "out"):
                    continue
                add("delete", f"deletes {t} (exists, not recoverable from git)")
                break
        elif n in ("rm",) and c.remote:
            if _is_live(text, cfg):
                add("delete", "deletes files on a live system")
            elif "-r" in fl or "-R" in fl:
                tgt = " ".join(_pos(a))
                if not all(t.startswith("/tmp/") for t in _pos(a)):
                    add("delete", f"rm -r on {c.remote}: {tgt[:80]}")
        elif n in ("truncate", "shred", "mkfs", "wipefs") or n == "dd" and any(x.startswith("of=/dev/") for x in a):
            add("delete", n)
        elif n in ("find",) and ("-delete" in a or ("-exec" in a and re.search(r"-exec\s+rm\b", text))):
            add("delete", "find -delete/-exec rm", definite=False, q="deployish")

        # --- databases
        if n in ("sqlite3", "psql", "mysql", "clickhouse-client", "clickhouse", "redis-cli", "mongosh") or \
                (n == "curl" and re.search(r"(?i)clickhouse|:8123|:9000", text)):
            sql = text
            if re.search(r"(?i)\b(drop\s+(table|database|view|index|schema|user|dictionary)|truncate(\s+table)?\s+\w|"
                         r"delete\s+from|alter\s+table\s+\S+\s+(delete|drop|update)|flushall|flushdb)\b", sql):
                add("delete", f"{n}: destructive SQL")
            elif re.search(r"(?i)\b(insert\s+into|update\s+\w+\s+set|create\s+user|grant\s|alter\s+(table|user))",
                           sql) and (c.remote or _is_live(sql, cfg)):
                add("shared", f"{n}: writes to a remote/live database")

        # --- write SQL handed to any command on a remote host (custom query wrappers, docker exec clients)
        elif c.remote and n not in ("echo", "printf", "grep", "cat", "sed", "awk", "tee") and \
                re.search(r"(?i)\b(drop\s+(table|database|view|user)|delete\s+from|truncate\s+table|"
                          r"alter\s+table\s+\S+\s+(delete|update|drop)|insert\s+into|create\s+(user|table)|grant\s)",
                          text):
            add("shared", f"writes to a database on {c.remote}")

        # --- network writes
        if n in ("curl", "wget", "http", "https", "xh"):
            urls = [x for x in a if re.match(r"(?i)https?://", x)]
            local = urls and all(any(h in u for h in LOCAL_HOSTS) for u in urls)
            writes = bool(fl & {"-d", "-F", "-T"} or {"--data", "--data-raw", "--data-binary", "--data-urlencode",
                                                      "--form", "--upload-file", "--json"} & fl or
                          re.search(r"-X\s*(POST|PUT|PATCH|DELETE)|--request\s+(POST|PUT|PATCH|DELETE)", text))
            from gutcheck_core.pyscan import INFERENCE_HOSTS
            hosts = [re.sub(r"^https?://([^/:]+).*$", r"\1", u) for u in urls]
            inference = hosts and all(any(h == ih or h.endswith("." + ih) for ih in INFERENCE_HOSTS) for h in hosts)
            if writes and not local and not inference:
                if re.search(r"-X\s*DELETE|--request\s+DELETE", text):
                    add("delete", "HTTP DELETE to a remote service")
                else:
                    add("exfil", "HTTP write request to a remote service", definite=False, q="remote_write")
        if n in ("scp", "rsync", "sftp") or (n == "rclone" and _pos(a)[:1] in (["copy"], ["sync"], ["move"],
                                                                               ["delete"], ["purge"])):
            dests = _pos(a)[-1:] if n != "rclone" else _pos(a)[-1:]
            dest = dests[0] if dests else ""
            if ":" in dest and not dest.startswith("/") and not ({"-n", "--dry-run"} & (fl | set(a))):
                host = dest.split(":")[0].split("@")[-1]
                own = any(h in host for h in cfg["sensitivity"].get("hosts", {}))
                if not own:
                    add("exfil", f"copies files to {host}")
                elif _is_live(dest, cfg):
                    add("shared", f"copies files into a live path on {host}")
                if n == "rsync" and ("--delete" in a or "--delete-after" in a) and (not own or _is_live(dest, cfg)):
                    add("delete", "rsync --delete to a remote")
        if n == "aws" and len(a) > 2 and a[1] in ("s3",) and a[2] in ("cp", "sync", "mv", "rm", "rb"):
            add("delete" if a[2] in ("rm", "rb") else "exfil", f"aws s3 {a[2]}")

        # --- deploy / publish tools
        sub = " ".join(_pos(a)[:2])
        if n in ("npm", "pnpm", "yarn", "cargo", "twine", "gem", "poetry", "uv") and _pos(a)[:1] == ["publish"]:
            add("shared", f"{n} publish")
        elif n == "gh" and (sub in ("pr merge", "pr close", "release create", "release delete", "repo delete",
                                     "repo edit", "secret set", "secret delete", "workflow run", "run cancel",
                                     "issue close", "issue delete") or
                            _pos(a)[:1] == ["api"] and (re.search(r"-X\s*(POST|PUT|PATCH|DELETE)|--method\s+(POST|PUT|"
                                                                   r"PATCH|DELETE)", text) or
                                                         re.search(r"\s-[fF]\s|--(raw-)?field", text) and
                                                         not re.search(r"-X\s*GET|--method\s+GET", text))):
            add("shared", f"gh {sub}")
        elif n in ("wrangler",) or (n == "npx" and _pos(a)[:1] == ["wrangler"]):
            w = _pos(a)[1:] if n == "npx" else _pos(a)
            if w[:1] in (["deploy"], ["publish"], ["rollback"], ["delete"], ["triggers"]) or \
                    w[:2] in (["secret", "put"], ["secret", "delete"], ["kv", "key"], ["kv:key", "put"],
                              ["r2", "object"], ["versions", "deploy"]) or \
                    (w[:2] == ["d1", "execute"] and re.search(r"(?i)\b(insert|update|delete|drop|alter|create)\b",
                                                              text)) or \
                    (w[:1] == ["dev"] and "--remote" in a):
                add("shared", f"wrangler {' '.join(w[:2])}")
        elif n in ("vercel", "netlify", "fly", "flyctl", "firebase", "heroku", "terraform", "pulumi", "helm",
                   "kubectl", "serverless", "sls", "cdk", "railway", "rojo") and _pos(a)[:1] and \
                _pos(a)[0] in ("deploy", "apply", "destroy", "delete", "publish", "upload", "install", "upgrade",
                               "uninstall", "rollout", "scale", "patch", "replace", "up", "down", "promote"):
            add("shared", f"{n} {_pos(a)[0]}")
        elif any(DEPLOY_WORDS.match(x) for x in _pos(a)[:3]) and n not in ("git", "echo", "printf", "grep", "rg",
                                                                            "cat", "sed", "awk", "docker",
                                                                            "systemctl", "kubectl", "npm", "cargo",
                                                                            "find", "ls", "head", "tail"):
            add("shared", f"{n} {' '.join(_pos(a)[:2])}", definite=False, q="deployish")

        # --- scripts whose names say what they do
        runs = [a[0]] + ([_pos(a)[0]] if n in ("bash", "sh", "zsh", "python", "python3", "node", "uv", "bun",
                                                "deno", "ruby", "perl", "lune") and _pos(a) and
                         not {"-c", "-e", "-m", "-n"} & set(a[1:3]) else [])
        if {"--dry-run", "--dryrun", "--check", "--help", "--plan", "--diff"} & set(a[1:]):
            runs = []
        for x in runs:
            base = os.path.basename(x)
            if re.search(r"(?i)(^|[_\-.])(deploy|publish|release|upload|rotate|migrate|install|rollout|promote|"
                         r"provision|teardown|destroy)([_\-.]|$)", base) and "." in base:
                p = x if os.path.isabs(os.path.expanduser(x)) else os.path.join(cur or ".", x)
                readable = os.path.isfile(os.path.expanduser(p))
                # unreadable at check time: we can't see what it does, so ask
                add("shared", f"runs {base}" + ("" if readable else " (script not readable)"),
                    definite=not readable, q="deployish")
                break

        # --- remote privileged changes on live systems
        if c.remote and remote_live and n in ("cp", "mv", "install", "tee", "chmod", "chown", "ln", "sed",
                                               "rsync", "truncate") and (n != "sed" or "-i" in a):
            add("shared", f"{n} changes a live path on {c.remote}")
        if c.remote and n in ("tee",) and any(x.startswith("/etc/") for x in a):
            add("shared", f"writes system config on {c.remote}")
        for op, t in c.redirects:
            if ">" in op and c.remote and _is_live(t, cfg):
                add("shared", f"writes {t} on a live system")
            if ">" in op and c.remote and t.startswith("/etc/"):
                add("shared", f"writes {t} on {c.remote}")

    # risky call sites found in Python the command runs; Jev judges just those lines
    for label, lines in an.extra.get("py_findings") or []:
        out.append({"check": "running", "why": f"{label}: {lines[0][:80]}", "definite": False, "q": "script_lines",
                    "item": "\n".join(lines)[:4000]})
    for body in an.extra.get("heredoc_scripts") or []:
        blines = body.splitlines()
        hits = [i for i, ln in enumerate(blines) if SCRIPT_RISK.search(ln)]
        if hits:
            ctx = sorted({j for i in hits for j in range(max(0, i - 2), min(len(blines), i + 3))})
            out.append({"check": "running", "why": "inline script may act on running or remote systems",
                        "definite": False, "q": "script_lines",
                        "item": secretscan.redact("\n".join(blines[j] for j in ctx))[:4000]})
    return out
