"""Pre-run Bash check. Returns None (run as today) or an `ask` reason. Never allows, never denies.

Order: parse and unwrap → deterministic ask rules → read-only allowlist → one Jev request.
"""
import base64
import binascii
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, secretscan, shellparse, triggers

# ---- read-only allowlist -------------------------------------------------------------------
READONLY = set("""
echo printf grep egrep fgrep rg ag head tail cat ls wc cut sort uniq tr pwd which type whereis file stat du df
diff cmp comm basename dirname realpath readlink jq yq shasum sha1sum sha256sum sha512sum md5 md5sum cksum od
strings hexdump tree uptime hostname whoami id uname nproc ps pgrep lsof ss netstat dig nslookup host ping seq
true false test [ [[ sleep cd pushd popd set export unset read wait break continue exit : column nl fold rev
paste join expand unexpand fmt less more bat fd locate mdfind sw_vers system_profiler sysctl ioreg vm_stat
iostat top htop free env printenv date cal bc expr numfmt tput clear history alias local return shift
getconf locale arch lscpu lsblk mount findmnt ip ifconfig route arp traceroute mtr whois curlie http
nvidia-smi rocm-smi journalctl git gh systemctl docker podman kubectl cargo npm pnpm yarn bun uv pip pip3
python python3 node deno go rustc rustup mise brew launchctl crontab tmux sqlite3 psql redis-cli
awk gawk sed find xxd base64 tee mktemp touch mkdir
""".split())
# names whose read-only-ness depends on arguments (checked in _readonly)
CONDITIONAL = {"git", "gh", "systemctl", "docker", "podman", "kubectl", "cargo", "npm", "pnpm", "yarn", "bun", "uv",
               "pip", "pip3", "python", "python3", "node", "deno", "go", "rustc", "rustup", "mise", "brew",
               "launchctl", "crontab", "tmux", "sqlite3", "psql", "redis-cli", "awk", "gawk", "sed", "find", "xxd",
               "base64", "tee", "date", "env", "printenv", "touch", "mkdir", "sysctl", "top", "journalctl",
               "set", "export", "less", "more", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "strings",
               "bat", "echo", "printf", "sort"}

GIT_RO = {"status", "log", "show", "diff", "grep", "rev-parse", "ls-files", "ls-tree", "cat-file", "merge-base",
          "rev-list", "describe", "blame", "shortlog", "ls-remote", "fetch", "whatchanged", "name-rev",
          "for-each-ref", "show-ref", "count-objects", "check-ignore", "check-attr", "var", "help", "version",
          "diff-tree", "diff-index", "diff-files", "cherry", "range-diff", "annotate", "verify-commit",
          "show-branch", "fsck"}
DOCKER_RO = {"ps", "inspect", "logs", "images", "version", "info", "top", "port", "diff", "history", "events",
             "search"}
SYSTEMCTL_RO = {"status", "show", "is-active", "is-enabled", "is-failed", "cat", "list-units", "list-timers",
                "list-unit-files", "list-dependencies", "list-sockets", "help"}
KUBECTL_RO = {"get", "describe", "logs", "top", "explain", "version", "api-resources", "cluster-info"}
CARGO_RO = {"check", "clippy", "tree", "metadata", "fmt", "doc", "search", "version", "build", "test", "bench",
            "nextest"}
BREW_RO = {"list", "info", "search", "outdated", "deps", "uses", "config", "doctor", "--prefix", "--version",
           "leaves", "desc", "home"}
TMP_PREFIXES = ("/tmp/", "/private/tmp/", "/dev/null", "/dev/stdout", "/dev/stderr", "/var/folders/")

INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "python", "python3", "node", "perl", "ruby", "php",
                "deno", "bun", "osascript", "pwsh", "lua", "luajit", "lune", "eval", "source", "."}
DECODERS = [
    ("base64", lambda a: any(x in ("-d", "--decode", "-D") for x in a[1:])),
    ("xxd", lambda a: any(x.startswith("-r") for x in a[1:])),
    ("openssl", lambda a: "-d" in a or "base64" in a),
    ("printf", lambda a: any(re.search(r"\\x[0-9a-fA-F]{2}|\\[0-7]{3}", x) for x in a[1:])),
    ("rev", lambda a: True),
    ("gunzip", lambda a: True), ("zcat", lambda a: True), ("uudecode", lambda a: True),
]
FETCHERS = {"curl", "wget", "fetch", "http", "aria2c"}
INLINE_EXEC = re.compile(r"(?<![\w.])(exec|eval|compile)\s*\(|\b(b64decode|a85decode|b32decode|atob|fromCharCode|"
                         r"decodestring)\s*\(|__import__\s*\(\s*['\"](base64|codecs|marshal|zlib)|"
                         r"\bnew\s+Function\s*\(|codecs\.decode\s*\(|marshal\.loads\s*\(")


@dataclass
class Analysis:
    command: str
    cmds: list
    asks: list = field(default_factory=list)       # [(check, reason)]
    readonly: bool = False
    extra: dict = field(default_factory=dict)      # extra Jev state: decoded, scripts, runners
    notes: list = field(default_factory=list)
    deletes: list = field(default_factory=list)    # absolute paths the command deletes or overwrites


# ---- helpers -------------------------------------------------------------------------------
def _flags(argv):
    """Set of single-letter flags and long flags."""
    out = set()
    for x in argv[1:]:
        if x == "--":
            break
        if x.startswith("--"):
            out.add(x.split("=", 1)[0])
        elif x.startswith("-") and len(x) > 1:
            out.update("-" + ch for ch in x[1:])
    return out


def _git_sub(argv):
    i = 1
    while i < len(argv):
        x = argv[i]
        if x in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if x.startswith("-"):
            i += 1
            continue
        return x, argv[i + 1:]
    return "", []


def _positional(argv, start=1):
    return [x for x in argv[start:] if not x.startswith("-")]


def _writes_file(cmd):
    for op, target in cmd.redirects:
        if (">" in op and "&" not in op) or op in (">", ">>", ">|", "&>", "&>>"):
            t = target.strip("'\"")
            if not t.startswith(TMP_PREFIXES) and not t.startswith("$TMPDIR") and t not in ("&1", "&2"):
                return True
    return False


def _expand_home(p):
    return os.path.expanduser(p.replace("$HOME", "~").replace("${HOME}", "~"))


# ---- deterministic ask rules ---------------------------------------------------------------
def _rule_git(c, repo_root, cfg):
    if c.name != "git":
        return None
    sub, rest = _git_sub(c.argv)
    fl = _flags(["git"] + rest)
    if sub == "push":
        if fl & {"-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete", "-d"} or \
                any(x.startswith("+") for x in rest if not x.startswith("-")) or \
                any(x.startswith(":") for x in rest if not x.startswith("-")):
            return ("git-history", "force-push or remote branch deletion")
    if sub == "reset" and "--hard" in rest:
        return ("git-discard", "git reset --hard discards uncommitted work")
    if sub == "clean" and ("-f" in fl or "--force" in fl):
        return ("git-discard", "git clean -f deletes untracked files")
    return None


def _rule_rm(c, cwd, repo_root):
    if c.name not in ("rm", "rmdir", "shred", "srm", "trash") or c.name == "rmdir":
        return None
    fl = _flags(c.argv)
    if not ({"-r", "-R", "--recursive"} & fl and ({"-f", "--force"} & fl)) and c.name != "shred":
        return None
    targets = _positional(c.argv)
    if not targets:
        return None
    base = repo_root or cwd
    if not base:
        return ("delete", "rm -rf with an unknown working directory")
    for t in targets:
        raw = t
        if "$" in t or "`" in t or c.dynamic and t.startswith("$"):
            return ("delete", f"rm -rf with a runtime target ({raw})")
        p = _expand_home(t)
        ap = os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd or "/", p))
        if ap.startswith(("/tmp/", "/private/tmp/", "/var/folders/")) and ap.rstrip("/") not in (
                "/tmp", "/private/tmp", "/var/folders"):
            continue
        if not base or not (ap == base or ap.startswith(base.rstrip("/") + "/")) or ap == base:
            return ("delete", f"rm -rf outside the repo: {raw}")
        if any(ch in raw for ch in "*?") and os.path.dirname(ap) == base:
            return ("delete", f"rm -rf with a glob at the repo root: {raw}")
    return None


def _deletes(c, cwd):
    """Paths this command deletes or overwrites, resolved against cwd (globs expanded). The whole repo for git
    commands that discard work without naming files. `find -delete` with -name/-path filters gives fnmatch
    patterns (start/NAME, start/*/NAME, PATH) instead of its start directories."""
    import glob
    if c.remote or not cwd:
        return []
    sub, rest = _git_sub(c.argv) if c.name == "git" else ("", [])
    fl = _flags(["git"] + rest)
    args, pats = [], []
    if c.name in ("rm", "rmdir", "unlink", "shred", "srm", "trash"):
        args = _positional(c.argv)
    elif sub == "rm" and "--cached" not in rest:
        args = _positional(["git"] + rest)
    elif sub == "checkout" and "--" in rest:
        args = rest[rest.index("--") + 1:]
    elif (sub == "clean" and fl & {"-f", "--force"} and not fl & {"-n", "--dry-run"} or
          sub == "reset" and "--hard" in rest or sub == "stash" and rest[:1] == ["drop"]):
        args = [_repo_root(cwd) or "."]
    elif c.name == "find" and "-delete" in c.argv:
        n = next((i for i, x in enumerate(c.argv[1:], 1) if x.startswith(("-", "(", "!"))), len(c.argv))
        starts = c.argv[1:n] or ["."]
        flt = [(x, c.argv[i + 1]) for i, x in enumerate(c.argv[:-1])
               if x in ("-name", "-iname", "-path", "-ipath", "-wholename")]
        pats = [os.path.realpath(os.path.join(cwd, *p)) for s in starts for f, v in flt
                for p in ([(s, v), (s, "*", v)] if f in ("-name", "-iname") else [(v,)])]
        args = [] if flt else starts
    over = [t.strip("'\"") for op, t in c.redirects if op in (">", ">|", "&>")]  # overwrites: existing paths only
    if c.name == "mv" and len(pos := _positional(c.argv)) > 1:
        dest = os.path.join(cwd, _expand_home(pos[-1]))
        over += [os.path.join(dest, os.path.basename(x)) for x in pos[:-1]] if os.path.isdir(dest) else [dest]
    out = []
    for a in args + over:
        if "$" in a or "`" in a or a.startswith("&"):
            continue
        p = os.path.join(cwd, _expand_home(a))
        hits = glob.glob(p) if any(ch in a for ch in "*?[") else [p]
        out += [os.path.realpath(h) for h in hits if a in args or os.path.lexists(h)]
    return out + pats


READERS = {"cat", "head", "tail", "less", "more", "bat", "strings", "grep", "egrep", "fgrep", "rg", "sed", "awk",
           "gawk", "xxd", "od", "hexdump", "base64", "scp", "rsync", "nl", "sort", "uniq", "diff",
           "jq", "yq", "column", "tee", "pbcopy"}


def _keynames_only(c):
    """Commands over a secret file that print key names, counts or redacted values, not the values."""
    a = c.argv
    fl = _flags(a)
    if c.name == "cut" and any(x in ("-d=", "-d", "=") for x in a) and ("-f1" in a or "-f" in a and "1" in a):
        return True
    if c.name in ("grep", "rg", "egrep") and fl & {"-c", "-l", "-L", "--count", "--files-with-matches", "-q",
                                                   "--quiet"}:
        return True
    if c.name in ("grep", "rg", "egrep") and "-o" in fl:
        pats = [a[i + 1] for i, x in enumerate(a) if x in ("-e", "--regexp") and i + 1 < len(a)] or \
            [x for x in _positional(a) if not secretscan.is_secret_file(x)][:1]
        # the output is only the key name, or only the literal text typed in the pattern
        return bool(pats) and all(not re.search(r"=(?![$|)\\'\"]|$)", p) or
                                  not re.search(r"[.*+?\[\]{}()|\\]", p.split("=", 1)[1]) for p in pats)
    if c.name == "sed" and re.search(r"s([/|#])=\.\*\1", " ".join(a)):
        return True
    if c.name in ("awk", "gawk") and re.search(r"-F\s*=?['\"]?=|FS\s*=\s*\"=\"", " ".join(a)) and \
            re.search(r"print \$1\b(?!.*\$2)", " ".join(a)):
        return True
    return False


_DIGEST = {"wc", "sha256sum", "shasum", "sha1sum", "md5", "md5sum", "cksum"}


def _downstream_hides(c, cmds, depth=0):
    """Output piped (possibly through filters) into something that keeps only key names or a digest."""
    if depth > 6:
        return False
    for x in cmds:
        if x.stdin_from is c.argv:
            if _keynames_only(x) or x.name in _DIGEST:
                return True
            if x.name in ("grep", "egrep", "sort", "uniq", "head", "tail", "tr") and _downstream_hides(x, cmds,
                                                                                                    depth + 1):
                return True
    return False


def _rule_secrets(c, cmds):
    a = c.argv
    name = c.name
    # reading secret files
    if (name in READERS or name in ("cut",)) and "$()" not in c.via:
        files = [x for x in a[1:] if secretscan.is_secret_file(x)]
        redirs = [t for op, t in c.redirects if "<" in op and secretscan.is_secret_file(t)]
        if files or redirs:
            if _keynames_only(c) or _downstream_hides(c, cmds):
                return None
            f = (files or redirs)[0]
            return ("secret", f"{name} would print or copy a secret file ({f})")
    # piped into a filter that doesn't select secret-named variables (e.g. `env | grep ^CLAUDE`)
    piped = any(x.stdin_from is a and (_keynames_only(x) or not any(
        secretscan.is_secret_var(w) for w in re.findall(r"[A-Za-z_]+", " ".join(x.argv[1:])))) for x in cmds)
    if name in ("printenv",) and len(_positional(a)) == 0 and not piped:
        return ("secret", "printenv with no filter prints every secret in the environment")
    if name == "printenv" and "rtk" in c.via and not all(x.upper() in ("PATH", "HOME", "SHELL", "LANG", "TERM", "USER",
                                                                   "PWD", "TMPDIR", "EDITOR", "LC_ALL")
                                                         for x in _positional(a)):
        return ("secret", "rtk env -f matches by substring and prints values unmasked")
    if name in ("printenv",) and any(secretscan.is_secret_var(x) or "KEY" in x.upper() or "TOKEN" in x.upper()
                                     for x in _positional(a)):
        return ("secret", "printenv of a secret-named variable")
    if name == "env" and len(a) == 1 and not piped:
        return ("secret", "env with no arguments prints every secret in the environment")
    if name == "set" and len(a) == 1:
        return ("secret", "set with no arguments prints every variable")
    if name in ("echo", "printf") and "$()" not in c.via and not _redirected_to_file(c) and \
            not any(x.stdin_from is a for x in cmds):  # piped into another program, not printed
        for x in a[1:]:
            for m in re.finditer(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", x):
                if secretscan.is_secret_var(m.group(1)) and not re.search(r"\$\{#", x) and \
                        not re.search(r"\$\{" + m.group(1) + r":[+?]", x):
                    return ("secret", f"{name} of ${m.group(1)} prints a secret")
    if name == "security" and len(a) > 1 and a[1] in ("find-generic-password", "find-internet-password") and \
            ("-w" in a or "-g" in a):
        if "$()" not in c.via:
            return ("secret", "security find-*-password -w prints the secret (capture it with $(...) instead)")
    if name in ("gh",) and a[1:3] == ["auth", "token"] and "$()" not in c.via:
        return ("secret", "gh auth token prints a token")
    if name == "cat" and any(x.endswith(".credentials.json") for x in a):
        return ("secret", "prints Claude credentials")
    return None


def _redirected_to_file(c):
    return any(">" in op for op, _ in c.redirects)


def _is_decoder(c):
    return any(c.name == n and f(c.argv) for n, f in DECODERS)


def _feeds_interpreter(c):
    if c.name not in INTERPRETERS:
        return False
    if c.name in ("python", "python3", "node", "perl", "ruby", "php", "deno", "bun", "lune"):
        pos = _positional(c.argv)
        return not pos or pos[0] == "-" or "-" in c.argv
    if c.name in ("sh", "bash", "zsh", "dash", "ksh", "fish"):
        return not any(x.startswith("-c") for x in c.argv[1:]) and not _positional(c.argv) or "-s" in c.argv
    return True


def _rule_hidden(cmds, parsed_notes):
    by_stdin = {}
    for c in cmds:
        if c.stdin_from is not None and _feeds_interpreter(c):
            src_name = os.path.basename(c.stdin_from[0]) if c.stdin_from else ""
            src = next((x for x in cmds if x.argv is c.stdin_from), None)
            if src_name in FETCHERS:
                return ("hidden", f"downloads and executes: {src_name} | {c.name}")
            if src is not None and _is_decoder(src):
                return ("hidden", f"decodes data and executes it: {src_name} | {c.name}")
            if src_name in ("echo", "printf", "cat") and src is not None and \
                    any(_looks_encoded(x) for x in src.argv[1:]):
                return ("hidden", f"pipes encoded data into {c.name}")
        by_stdin[id(c)] = c
    for c in cmds:
        a = c.argv
        if c.name in ("python", "python3", "node", "perl", "ruby", "deno", "bun") :
            for flag in ("-c", "-e", "-E", "--eval", "-p"):
                if flag in a:
                    i = a.index(flag)
                    code = a[i + 1] if i + 1 < len(a) else ""
                    if INLINE_EXEC.search(code):
                        return ("hidden", f"{c.name} {flag} code calls exec/eval/decode")
        if c.name in ("sh", "bash", "zsh", "dash") and c.dynamic:
            for i, x in enumerate(a[1:], 1):
                if x.startswith("-") and not x.startswith("--") and "c" in x and i + 1 < len(a):
                    if i + 1 < len(c.dyn_args) and c.dyn_args[i + 1] and ("$(" in a[i + 1] or "`" in a[i + 1]):
                        return ("hidden", f"{c.name} -c runs a command built at runtime")
                    break
        if c.name in ("source", ".") and any(x.startswith("<(") or "/dev/fd" in x for x in a[1:]):
            return ("hidden", "sources the output of another command")
        if c.name in ("source", ".") and "<(" in c.raw:
            return ("hidden", "sources the output of another command")
    for n in parsed_notes:
        if n.startswith("runtime-constructed"):
            return ("runtime", "command is constructed at runtime")
    return None


_B64 = re.compile(r"^[A-Za-z0-9+/]{16,}={0,2}$")
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2}){8,}$|^(?:\\x[0-9a-fA-F]{2}){4,}$")


def _looks_encoded(s):
    s = s.strip("'\"")
    return bool(_B64.match(s) and len(s) % 4 == 0 and re.search(r"[A-Z]", s) and re.search(r"[a-z]", s)) or \
        bool(_HEX.match(s))


def decode_literals(command):
    """Decode base64/hex literals in the command; return printable plaintexts."""
    out = []
    for tok in re.findall(r"[A-Za-z0-9+/]{16,}={0,2}|(?:\\x[0-9a-fA-F]{2}){4,}|(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}){8,}",
                          command):
        raw = None
        try:
            if tok.startswith("\\x"):
                raw = bytes(int(h, 16) for h in re.findall(r"\\x([0-9a-fA-F]{2})", tok))
            elif re.fullmatch(r"[0-9a-fA-F]+", tok) and len(tok) % 2 == 0 and not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tok):
                raw = binascii.unhexlify(tok)
            elif _looks_encoded(tok):
                raw = base64.b64decode(tok + "=" * (-len(tok) % 4), validate=False)
        except Exception:
            raw = None
        if raw:
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if sum(ch.isprintable() or ch in "\n\t" for ch in txt) >= 0.95 * len(txt) and re.search(r"[a-z]{2}", txt):
                out.append(txt)
    return out


# ---- read-only classification --------------------------------------------------------------
def _readonly(c):
    n = c.name
    a = c.argv
    if n not in READONLY:
        return False
    if _writes_file(c):
        return False
    if n not in CONDITIONAL:
        return True
    fl = _flags(a)
    if n == "git":
        sub, rest = _git_sub(a)
        if sub in GIT_RO:
            return not (sub == "fetch" and ("--prune" in rest or "-p" in rest))
        if sub == "branch":
            return not (_flags(["git"] + rest) & {"-d", "-D", "-m", "-M", "-f", "--delete", "--move", "--force",
                                                  "-c", "-C", "--set-upstream-to", "-u"}) and \
                (not _positional(["git"] + rest) or "--list" in rest or "-l" in rest or "--contains" in rest)
        if sub == "remote":
            return not rest or rest[0] in ("-v", "show", "get-url")
        if sub == "stash":
            return bool(rest) and rest[0] in ("list", "show")
        if sub == "tag":
            return not rest or rest[0] in ("-l", "--list", "--contains", "--points-at", "-n")
        if sub == "config":
            return any(x in rest for x in ("--get", "--get-all", "--list", "-l", "--get-regexp", "--show-origin"))
        if sub == "worktree":
            return bool(rest) and rest[0] == "list"
        if sub == "reflog":
            return not rest or rest[0] == "show"
        if sub == "notes":
            return not rest or rest[0] in ("list", "show")
        if sub == "submodule":
            return not rest or rest[0] == "status"
        return False
    if n == "gh":
        if len(a) > 1 and a[1] == "api":
            method = next((a[i + 1] for i, x in enumerate(a) if x in ("-X", "--method") and i + 1 < len(a)), "GET")
            return method.upper() == "GET" and not fl & {"-f", "-F", "--field", "--raw-field", "--input"}
        return len(a) > 2 and a[1] in ("pr", "issue", "run", "repo", "release", "workflow", "search", "label",
                                       "gist", "cache") and a[2] in ("view", "list", "diff", "checks", "status",
                                                                    "download", "watch") or \
            a[1:3] in (["auth", "status"],)
    if n == "systemctl":
        return bool(_positional(a)) and _positional(a)[0] in SYSTEMCTL_RO
    if n in ("docker", "podman"):
        pos = [x for i, x in enumerate(a[1:], 1) if not x.startswith("-") and a[i - 1] not in ("-H", "--host",
                                                                                              "--context")]
        if not pos:
            return True
        if pos[0] in ("compose",):
            return len(pos) > 1 and pos[1] in ("ps", "logs", "config", "images", "top", "ls")
        if pos[0] in ("container", "image", "volume", "network", "system"):
            return len(pos) > 1 and pos[1] in ("ls", "inspect", "list", "df", "info", "logs")
        if pos[0] == "stats":
            return "--no-stream" in a
        return pos[0] in DOCKER_RO
    if n == "kubectl":
        return bool(_positional(a)) and _positional(a)[0] in KUBECTL_RO
    if n == "cargo":
        pos = _positional(a)
        return bool(pos) and pos[0] in CARGO_RO or "--version" in a
    if n in ("npm", "pnpm", "yarn", "bun"):
        pos = _positional(a)
        return bool(pos) and pos[0] in ("ls", "list", "view", "info", "outdated", "why", "audit", "explain",
                                        "config") and "fix" not in pos or "--version" in a or "-v" in a
    if n in ("uv", "pip", "pip3"):
        pos = _positional(a)
        return bool(pos) and (pos[0] in ("list", "show", "freeze", "tree", "check") or pos[:2] == ["pip", "list"]) or \
            "--version" in a
    if n in ("python", "python3", "node", "deno", "go", "rustc", "rustup", "mise", "sqlite3", "psql", "redis-cli",
             "tmux", "crontab", "launchctl"):
        if "--version" in a or "-V" in a or a[1:] == ["version"]:
            return True
        if n == "mise":
            return bool(_positional(a)) and _positional(a)[0] in ("ls", "list", "which", "where", "current",
                                                                  "doctor", "env", "tasks")
        if n == "tmux":
            return bool(_positional(a)) and _positional(a)[0] in ("ls", "list-sessions", "list-windows",
                                                                  "list-panes", "capture-pane", "display-message",
                                                                  "show-options", "has-session")
        if n == "crontab":
            return a[1:] == ["-l"]
        if n == "launchctl":
            return bool(_positional(a)) and _positional(a)[0] in ("list", "print", "blame")
        if n == "sqlite3":
            sql = " ".join(a[2:]).strip().lower() if len(a) > 2 else ""
            return "-readonly" in a or bool(sql) and bool(re.match(r"^\.?(select|\.tables|\.schema|pragma|explain|"
                                                                     r"with|\.indexes|\.headers|\.mode)", sql)) and \
                not re.search(r"\b(insert|update|delete|drop|alter|create|replace|attach|vacuum)\b", sql)
        if n == "rustup":
            return bool(_positional(a)) and _positional(a)[0] in ("show", "which")
        return False
    if n == "brew":
        return bool(_positional(a)) and _positional(a)[0] in BREW_RO
    if n in ("awk", "gawk"):
        prog = next((x for x in a[1:] if not x.startswith("-")), "")
        return not re.search(r"system\s*\(|\|\s*\"|print[^;}]*>|getline|fflush", prog) and "-i" not in a
    if n == "sed":
        if "-i" in a or any(x.startswith(("-i", "--in-place")) for x in a[1:]) or "-I" in a:
            return False
        script = " ".join(x for x in a[1:] if not x.startswith("-"))
        return not re.search(r"(^|[;}\s])w\s|/w\s|\be\b\s|/e(\s|$)", script)
    if n == "find":
        return not fl & {"-delete"} and not any(x in a for x in ("-delete", "-exec", "-execdir", "-ok", "-okdir",
                                                                 "-fprint", "-fprint0", "-fprintf", "-fls"))
    if n == "xxd":
        return not any(x.startswith("-r") for x in a[1:])
    if n == "base64":
        return True
    if n == "tee":
        return all(x.startswith(TMP_PREFIXES) or x.startswith("-") for x in a[1:])
    if n == "date":
        return "-s" not in a and "--set" not in a
    if n in ("env", "printenv", "set", "export"):
        return len(a) > 1  # bare forms are handled by the secret rules
    if n in ("touch", "mkdir"):
        return True
    if n == "sysctl":
        return "-w" not in a and not any("=" in x for x in a[1:])
    if n == "top":
        return "-l" in a or "-b" in a or "-n" in a
    if n == "journalctl":
        return not fl & {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush"}
    if n in ("cat", "head", "tail", "less", "more", "grep", "egrep", "fgrep", "rg", "strings", "bat", "echo", "printf",
             "sort"):
        if n == "sort" and ("-o" in a or "--output" in a):
            return False
        return True
    return False


# ---- task runners and scripts --------------------------------------------------------------
def _find_up(cwd, name, stop=None):
    d = Path(cwd or ".").resolve()
    for _ in range(8):
        p = d / name
        if p.exists():
            return p
        if d.parent == d or (stop and str(d) == stop):
            break
        d = d.parent
    return None


def _make_recipe(makefile, target, depth=0, seen=None):
    seen = seen or set()
    if target in seen or depth > 2:
        return []
    seen.add(target)
    try:
        lines = makefile.read_text(errors="replace").splitlines()
    except OSError:
        return []
    out, cur, deps = [], False, []
    for ln in lines:
        m = re.match(r"^([A-Za-z0-9_.\-/ ]+?)\s*:(?!=)\s*(.*)$", ln)
        if m and not ln.startswith("\t"):
            names = m.group(1).split()
            cur = target in names
            if cur:
                deps = [d for d in m.group(2).split("#")[0].split() if not d.startswith("$")]
            continue
        if cur and ln.startswith("\t"):
            out.append(ln.lstrip("\t").lstrip("@-+ "))
        elif cur and ln.strip() and not ln.startswith("\t"):
            cur = False
    for d in deps:
        out = _make_recipe(makefile, d, depth + 1, seen) + out
    return out


def _default_make_target(makefile):
    try:
        for ln in makefile.read_text(errors="replace").splitlines():
            m = re.match(r"^([A-Za-z0-9_\-][A-Za-z0-9_.\-/]*)\s*:(?!=)", ln)
            if m and not m.group(1).startswith("."):
                return m.group(1)
    except OSError:
        pass
    return None


def resolve_runner(c, cwd):
    """Return (label, [shell lines]) for task-runner invocations, else None."""
    a = c.argv
    n = c.name
    try:
        if n in ("make", "gmake"):
            d = cwd
            if "-C" in a and a.index("-C") + 1 < len(a):
                d = os.path.join(cwd, a[a.index("-C") + 1])
            mf = next((Path(d) / f for f in ("GNUmakefile", "makefile", "Makefile") if (Path(d) / f).exists()), None)
            if not mf:
                return None
            targets = [x for x in _positional(a) if "=" not in x and x != a[a.index("-C") + 1 if "-C" in a else 0]]
            targets = targets or [_default_make_target(mf)]
            lines = []
            for t in targets:
                if t:
                    lines += _make_recipe(mf, t)
            return (f"make {' '.join(t for t in targets if t)}", lines)
        if n in ("npm", "pnpm", "yarn", "bun"):
            pos = _positional(a)
            if not pos:
                return None
            if pos[0] in ("run", "run-script") and len(pos) > 1:
                script = pos[1]
            elif pos[0] in ("test", "start", "stop", "restart", "t") or (n in ("yarn", "pnpm", "bun") and
                                                                          pos[0] not in ("install", "add", "i", "x",
                                                                                         "dlx", "exec", "remove")):
                script = {"t": "test"}.get(pos[0], pos[0])
            else:
                return None
            pj = _find_up(cwd, "package.json")
            if not pj:
                return None
            scripts = json.loads(pj.read_text()).get("scripts", {})
            lines = [scripts[k] for k in (f"pre{script}", script, f"post{script}") if k in scripts]
            return (f"{n} run {script}", lines) if lines else None
        if n == "just":
            jf = _find_up(cwd, "justfile") or _find_up(cwd, "Justfile")
            pos = _positional(a)
            if not jf:
                return None
            text = jf.read_text(errors="replace").splitlines()
            recipe = pos[0] if pos else None
            out, cur = [], False
            for ln in text:
                m = re.match(r"^@?([A-Za-z0-9_\-]+)(\s[^:]*)?:", ln)
                if m and not ln.startswith((" ", "\t")):
                    cur = recipe is None and not out or m.group(1) == recipe
                    if recipe is None and out:
                        break
                    continue
                if cur and ln.startswith((" ", "\t")) and ln.strip():
                    out.append(ln.strip().lstrip("@-"))
            return (f"just {recipe or '(default)'}", out)
        if n == "mise" and len(a) > 2 and a[1] in ("run", "r"):
            mt = _find_up(cwd, "mise.toml") or _find_up(cwd, ".mise.toml")
            if not mt:
                return None
            import tomllib
            task = tomllib.loads(mt.read_text()).get("tasks", {}).get(a[2], {})
            run = task.get("run") if isinstance(task, dict) else task
            lines = run if isinstance(run, list) else [run] if run else []
            return (f"mise run {a[2]}", lines)
        if n == "cargo" and len(a) > 1 and a[1] == "xtask":
            return ("cargo xtask", ["# runs the project's xtask Rust program; contents not resolved"])
    except Exception:
        return None
    return None


SCRIPT_EXT = (".sh", ".bash", ".zsh", ".py", ".js", ".mjs", ".ts", ".rb", ".pl", ".lua", ".luau")


def script_path(c, cwd):
    """Local script this command executes, if any."""
    a = c.argv
    cand = None
    if c.name in ("bash", "sh", "zsh", "source", ".", "python", "python3", "node", "ruby", "perl", "bun", "deno",
                  "lune", "uv", "tsx", "ts-node"):
        pos = [x for x in a[1:] if not x.startswith("-")]
        if c.name == "uv" and pos[:1] == ["run"]:
            pos = pos[1:]
        if c.name == "lune" and pos[:1] == ["run"]:
            pos = pos[1:]
        if c.name in ("deno", "bun") and pos[:1] == ["run"]:
            pos = pos[1:]
        if any(x in ("-c", "-e", "-m", "-n", "-s") for x in a[1:3]) or "-" in a[1:]:
            return None
        cand = pos[0] if pos else None
    elif a and ("/" in a[0] or a[0].endswith(SCRIPT_EXT)):
        cand = a[0]
    if not cand or "$" in cand:
        return None
    p = Path(_expand_home(cand))
    if not p.is_absolute():
        p = Path(cwd or ".") / p
    if c.name == "lune" and not p.suffix:
        for ext in (".luau", ".lua"):
            if p.with_suffix(ext).exists():
                p = p.with_suffix(ext)
    try:
        if p.is_file() and p.stat().st_size < 2_000_000 and not str(p).startswith(("/usr/", "/bin/", "/opt/homebrew/")):
            return p
    except OSError:
        pass
    return None


# ---- annotation ------------------------------------------------------------------------------
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


def _expand_aliases(command, aliases):
    if not aliases:
        return command
    head = command.lstrip().split(" ", 1)
    if head and head[0] in aliases:
        return aliases[head[0]] + (" " + head[1] if len(head) > 1 else "")
    return command


# ---- main entry ------------------------------------------------------------------------------
def analyze(command, cwd=None, cfg=None, depth=0, seen=None):
    cfg = cfg or config.load()
    seen = seen if seen is not None else set()
    repo_root = _repo_root(cwd)
    command = _expand_aliases(command, cfg.get("aliases", {}))
    parsed = shellparse.parse(command)
    an = Analysis(command, parsed.cmds, notes=list(parsed.notes))

    # decode literals and check the plaintext as well
    decoded = decode_literals(command)
    if decoded:
        an.extra["decoded"] = [secretscan.redact(d)[:2000] for d in decoded]
        for d in decoded:
            if depth < 2:
                sub = analyze(d, cwd, cfg, depth + 1, seen)
                for chk, why in sub.asks:
                    an.asks.append((chk, f"decoded data: {why}"))

    # resolve task runners and scripts
    for c in list(an.cmds):
        r = resolve_runner(c, cwd)
        if r and depth < 2:
            label, lines = r
            an.extra.setdefault("task_runners", {})[label] = [secretscan.redact(x)[:500] for x in lines][:40]
            for ln in lines:
                sub = analyze(ln, cwd, cfg, depth + 1, seen)
                an.asks += [(chk, f"{label}: {why}") for chk, why in sub.asks]
                an.cmds += sub.cmds
        sp = script_path(c, cwd)
        if sp and str(sp) not in seen and depth < 2:
            seen.add(str(sp))
            try:
                text = sp.read_text(errors="replace")
            except OSError:
                text = ""
            an.extra.setdefault("scripts", {})[str(sp)] = secretscan.redact(text[:6000])
            if sp.suffix in (".sh", ".bash", ".zsh", "") and text and not text.startswith("#!/usr/bin/env python"):
                sub = analyze(text, str(sp.parent), cfg, depth + 1, seen)
                an.asks += [(chk, f"{sp.name}: {why}") for chk, why in sub.asks]
                an.cmds += sub.cmds
                an.extra["scripts"].update(sub.extra.get("scripts", {}))
            elif sp.suffix == ".py" or text.startswith("#!/usr/bin/env python"):
                _scan_python(an, text, sp.name, cwd, cfg, depth, seen)
            else:
                an.notes.append(f"runs {sp.name}")
        if c.heredoc and c.name in ("python", "python3"):
            _scan_python(an, c.heredoc, "inline python", cwd, cfg, depth, seen)
        elif c.heredoc and c.name in ("node", "ruby", "perl"):
            an.extra.setdefault("heredoc_scripts", []).append(secretscan.redact(c.heredoc[:6000]))
        if c.name in ("python", "python3") and "-c" in c.argv:
            i = c.argv.index("-c")
            if i + 1 < len(c.argv):
                _scan_python(an, c.argv[i + 1], "python -c", cwd, cfg, depth, seen)

    cur = cwd
    for c in an.cmds:
        if c.name == "cd" and not c.remote and "$()" not in c.via:
            pos = _positional(c.argv)
            if pos and not c.dynamic:
                p = _expand_home(pos[0])
                cur = os.path.normpath(p if os.path.isabs(p) else os.path.join(cur or "/", p))
            elif pos:
                cur = None
            continue
        an.deletes += _deletes(c, cur)
        hits = [_rule_git(c, repo_root, cfg), _rule_secrets(c, an.cmds)]
        if not c.remote:  # remote deletes go to Jev with the host's label
            hits.append(_rule_rm(c, cur, _repo_root(cur) if cur != cwd else repo_root))
        an.asks += [h for h in hits if h]
    hid = _rule_hidden(an.cmds, an.notes)
    if hid:
        an.asks.append(hid)
    for pat in cfg["bash_check"].get("ask_patterns", []):
        try:
            if re.search(pat, normalize(command)):
                an.asks.append(("learned", f"matches a learned risky pattern ({pat})"))
        except re.error:
            pass

    has_scripts = bool(an.extra.get("scripts") or an.extra.get("task_runners") or an.extra.get("heredoc_scripts")
                       or an.extra.get("py_findings"))
    an.readonly = (not an.asks and not has_scripts and not decoded and
                   not any(n.startswith("runtime") for n in an.notes) and
                   all(_readonly(c) for c in an.cmds if not _is_wrapper_shell(c)))
    return an


def _scan_python(an, source, label, cwd, cfg, depth, seen):
    """Commands the code runs are checked like any other; risky calls go to Jev as short lines."""
    from . import pyscan
    cmds, finds, ok = pyscan.scan(source)
    if not ok:
        an.extra.setdefault("heredoc_scripts", []).append(secretscan.redact(source[:6000]))
        return
    for cmd in cmds[:20]:
        if depth < 2:
            sub = analyze(cmd, cwd, cfg, depth + 1, seen)
            an.asks += [(chk, f"{label} runs `{cmd[:60]}`: {why}") for chk, why in sub.asks]
            an.cmds += sub.cmds
    if cmds:
        an.extra.setdefault("py_commands", []).extend(c[:300] for c in cmds[:20])
    if finds:
        an.extra.setdefault("py_findings", []).append(
            (label, [f"{kind}: {secretscan.redact(line)}" for kind, line in finds[:30]]))


def _is_wrapper_shell(c):
    """ssh/bash -c/docker exec entries whose inner commands are already in the list."""
    if c.name in shellparse.SHELLS and any(x.startswith("-") and "c" in x for x in c.argv[1:2]):
        return True
    if c.name == "ssh" and len(_positional(c.argv)) > 1:
        return True
    if c.name in ("docker", "podman", "kubectl") and "exec" in c.argv[1:3]:
        return True
    return False


_root_cache = {}


def _repo_root(cwd):
    if not cwd:
        return None
    if cwd in _root_cache:
        return _root_cache[cwd]
    d = Path(cwd)
    root = None
    for _ in range(20):
        if (d / ".git").exists():
            root = str(d)
            break
        if d.parent == d:
            break
        d = d.parent
    _root_cache[cwd] = root
    return root


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
    from . import jev
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


def normalize(command):
    """Pattern key for approvals: collapse whitespace, replace digits/hex/temp paths."""
    s = " ".join(command.split())
    s = re.sub(r"/(private/)?tmp/[^\s'\"]+", "<TMP>", s)
    s = re.sub(r"\b[0-9a-f]{7,40}\b", "<HEX>", s)
    s = re.sub(r"\b\d+\b", "<N>", s)
    return s


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
