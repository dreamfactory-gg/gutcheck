"""Parse a Bash command into the simple commands it actually runs.

Splits compound commands (&&, ;, pipes, subshells, $(...)), strips wrappers (env assignments,
sudo, nohup, timeout, time, xargs, ...), unwraps nested shells (bash -c, ssh host '...',
docker/kubectl exec) and resolves variables assigned earlier in the same command.

Each result is a Cmd: argv (list of str; dynamic parts are kept as their source text and flagged),
plus context (via: wrappers/shells it came through, remote: host, stdin_from: previous pipeline stage).
"""
import re
from dataclasses import dataclass, field

import tree_sitter as ts
import tree_sitter_bash as tsb

_parser = ts.Parser(ts.Language(tsb.language()))

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
# wrapper -> number of leading option args it takes that are *values* (handled ad hoc below)
SIMPLE_WRAPPERS = {"nohup", "time", "command", "builtin", "exec", "caffeinate", "stdbuf", "unbuffer",
                   "chronic", "noglob", "ionice", "arch", "watch", "env", "sudo", "doas", "nice",
                   "timeout", "gtimeout", "xargs", "parallel", "script", "flock", "setsid", "then",
                   "do", "else"}
MAX_DEPTH = 6


@dataclass
class Cmd:
    argv: list
    dynamic: bool = False          # some word is only known at runtime ($(...), $VAR unknown)
    dyn_args: list = field(default_factory=list)  # per-argv dynamic flags
    via: list = field(default_factory=list)
    remote: str = None
    stdin_from: list = None        # argv of the previous pipeline stage
    heredoc: str = None            # heredoc/herestring body fed to this command
    redirects: list = field(default_factory=list)  # (op, target)
    raw: str = ""

    @property
    def name(self):
        return self.argv[0].rsplit("/", 1)[-1] if self.argv else ""


@dataclass
class Parsed:
    cmds: list
    notes: list = field(default_factory=list)   # e.g. "runtime-constructed", "parse-error"
    functions: dict = field(default_factory=dict)


def _t(node, src):
    return src[node.start_byte:node.end_byte].decode(errors="replace")


class _Ctx:
    def __init__(self, env, depth, via, remote):
        self.env, self.depth, self.via, self.remote = env, depth, via, remote
        self.heredocs = []


def parse(command, env=None, depth=0, via=None, remote=None):
    out = Parsed([])
    _parse_into(command, out, _Ctx(dict(env or {}), depth, list(via or []), remote))
    return out


_HEREDOC_START = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


def heredoc_bodies(command):
    """[(byte offset of <<, body)] found by scanning the text; covers what the grammar can't parse
    (for example a heredoc followed by `| head` on the same line)."""
    out = []
    lines = command.split("\n")
    offs, pos = [], 0
    for ln in lines:
        offs.append(pos)
        pos += len(ln.encode()) + 1
    i = 0
    while i < len(lines):
        pending = [(m.start(), m.group(1), m.group(3)) for m in _HEREDOC_START.finditer(lines[i])]
        j = i + 1
        for start, dash, delim in pending:
            body = []
            while j < len(lines) and (lines[j].strip() if dash else lines[j]) != delim:
                body.append(lines[j])
                j += 1
            out.append((offs[i] + len(lines[i][:start].encode()), "\n".join(body)))
            j += 1
        i = j if pending else i + 1
    return out


def _parse_into(command, out, ctx):
    if ctx.depth > MAX_DEPTH:
        out.notes.append("nesting too deep")
        return
    src = command.encode()
    tree = _parser.parse(src)
    ctx.heredocs = heredoc_bodies(command) if "<<" in command else []
    if tree.root_node.has_error:
        out.notes.append("parse-error")
    _walk(tree.root_node, src, out, ctx, None)


def _word(node, src, ctx, out):
    """Return (text, dynamic) for an argument node, expanding known variables."""
    t = node.type
    if t in ("word", "number"):
        return _unescape(_t(node, src)), False
    if t == "raw_string":
        return _t(node, src)[1:-1], False
    if t == "ansi_c_string":
        s = _t(node, src)[2:-1]
        try:
            return s.encode().decode("unicode_escape"), False
        except Exception:
            return s, False
    if t in ("simple_expansion", "expansion"):
        name = _var_name(node, src)
        if name and name in ctx.env:
            return ctx.env[name], False
        return _t(node, src), True
    if t in ("command_substitution", "process_substitution"):
        for c in node.named_children:
            _walk(c, src, out, _Ctx(ctx.env, ctx.depth, ctx.via + ["$()"], ctx.remote), None)
        return _t(node, src), True
    if t in ("string", "concatenation", "translated_string"):
        parts, dyn = [], False
        last = node.start_byte + (1 if t == "string" else 0)
        for c in node.children:
            if c.type in ('"',):
                continue
            if c.type == "string_content":
                parts.append(_t(c, src))
                last = c.end_byte
                continue
            if c.is_named:
                # keep literal text between named children (tree-sitter drops some whitespace)
                if c.start_byte > last and t == "string":
                    parts.append(src[last:c.start_byte].decode(errors="replace"))
                s, d = _word(c, src, ctx, out)
                parts.append(s)
                dyn = dyn or d
                last = c.end_byte
        if t == "string" and node.end_byte - 1 > last:
            parts.append(src[last:node.end_byte - 1].decode(errors="replace"))
        return "".join(parts), dyn
    if t == "arithmetic_expansion":
        return _t(node, src), True
    return _t(node, src), node.type not in ("word",)


def _unescape(s):
    return re.sub(r"\\(.)", r"\1", s)


def _var_name(node, src):
    for c in node.named_children:
        if c.type in ("variable_name", "special_variable_name"):
            return _t(c, src)
    m = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", _t(node, src))
    return m.group(1) if m else None


def _walk(node, src, out, ctx, stdin_from):
    t = node.type
    if t == "command":
        _command(node, src, out, ctx, stdin_from)
    elif t == "pipeline":
        prev = None
        for c in node.named_children:
            before = len(out.cmds)
            _walk(c, src, out, ctx, prev)
            prev = out.cmds[-1].argv if len(out.cmds) > before else prev
    elif t == "variable_assignment":
        _assign(node, src, out, ctx)
    elif t == "declaration_command":
        for c in node.named_children:
            if c.type == "variable_assignment":
                _assign(c, src, out, ctx)
    elif t == "redirected_statement":
        body = node.child_by_field_name("body")
        before = len(out.cmds)
        heredoc = None
        redirs = []
        for c in node.named_children:
            if c.type == "heredoc_redirect":
                for hc in c.named_children:
                    if hc.type == "heredoc_body":
                        heredoc = _t(hc, src)
                if heredoc is None:
                    heredoc = next((b for off, b in ctx.heredocs if off == c.start_byte), None)
            elif c.type == "herestring_redirect":
                heredoc = _word(c.named_children[-1], src, ctx, out)[0] if c.named_children else None
            elif c.type == "file_redirect":
                op = "".join(_t(x, src) for x in c.children if not x.is_named) or ">"
                dest = c.child_by_field_name("destination")
                redirs.append((op, _word(dest, src, ctx, out)[0] if dest else ""))
        if body is not None:
            _walk(body, src, out, ctx, stdin_from)
        if len(out.cmds) > before:
            first, last = out.cmds[before], out.cmds[-1]
            last.redirects += redirs
            if heredoc is not None:
                first.heredoc = heredoc
                _reexamine_stdin_shell(first, out, ctx)
    elif t == "function_definition":
        name = node.child_by_field_name("name")
        body = node.child_by_field_name("body")
        if name is not None and body is not None:
            out.functions[_t(name, src)] = _t(body, src)
            # judge the body where it's defined (same host and wrappers), as if it runs
            _walk(body, src, out, _Ctx(dict(ctx.env), ctx.depth, ctx.via + [f"fn {_t(name, src)}"], ctx.remote),
                  None)
    else:
        for c in node.named_children:
            _walk(c, src, out, ctx, stdin_from)


def _assign(node, src, out, ctx):
    name = node.child_by_field_name("name")
    val = node.child_by_field_name("value")
    if name is None:
        return
    if val is None:
        ctx.env[_t(name, src)] = ""
        return
    s, dyn = _word(val, src, ctx, out)
    if dyn:
        ctx.env.pop(_t(name, src), None)
    else:
        ctx.env[_t(name, src)] = s


def _command(node, src, out, ctx, stdin_from):
    argv, dyn, flags = [], False, []
    for c in node.named_children:
        if c.type == "variable_assignment":
            # prefix assignment: applies to this command only, but record for later resolution too
            _assign(c, src, out, ctx)
            continue
        if c.type == "file_redirect":
            continue
        if c.type == "command_name":
            inner = c.named_children[0] if c.named_children else None
            s, d = _word(inner, src, ctx, out) if inner is not None else (_t(c, src), False)
            if inner is not None and inner.type in ("simple_expansion", "expansion") and not d and " " in s.strip():
                words = s.split()
                argv.extend(words)
                flags.extend([False] * len(words))
                continue
        else:
            s, d = _word(c, src, ctx, out)
        argv.append(s)
        flags.append(d)
        dyn = dyn or d
    if not argv:
        return
    cmd = Cmd(argv=argv, dynamic=dyn, dyn_args=flags, via=list(ctx.via), remote=ctx.remote, stdin_from=stdin_from,
              raw=_t(node, src))
    for c in node.named_children:
        if c.type == "file_redirect":
            op = "".join(_t(x, src) for x in c.children if not x.is_named) or ">"
            dest = c.child_by_field_name("destination")
            cmd.redirects.append((op, _word(dest, src, ctx, out)[0] if dest else ""))
    _unwrap(cmd, out, ctx)


def _emit(cmd, out, ctx):
    out.cmds.append(cmd)
    # an eval / shell reading stdin from a pipeline is handled by rules; record runtime construction
    if cmd.name == "eval":
        body = " ".join(cmd.argv[1:])
        if cmd.dynamic:
            out.notes.append("runtime-constructed: eval of a runtime value")
        else:
            _parse_into(body, out, _Ctx(ctx.env, ctx.depth + 1, ctx.via + ["eval"], ctx.remote))


def _unwrap(cmd, out, ctx):
    a = cmd.argv
    name = cmd.name
    rest = None
    if name in ("env",):
        i = 1
        while i < len(a) and (a[i].startswith("-") or "=" in a[i]):
            if a[i] in ("-u", "-C", "-S"):
                i += 1
            i += 1
        rest = a[i:]
    elif name in ("sudo", "doas"):
        i = 1
        while i < len(a) and a[i].startswith("-"):
            if a[i] in ("-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"):
                i += 1
            i += 1
        while i < len(a) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", a[i]):
            i += 1
        rest = a[i:]
    elif name in ("timeout", "gtimeout"):
        i = 1
        while i < len(a) and a[i].startswith("-"):
            if a[i] in ("-s", "-k", "--signal", "--kill-after"):
                i += 1
            i += 1
        rest = a[i + 1:]
    elif name == "nice":
        i = 1
        while i < len(a) and a[i].startswith("-"):
            if a[i] == "-n":
                i += 1
            i += 1
        rest = a[i:]
    elif name == "xargs":
        i = 1
        while i < len(a) and a[i].startswith("-"):
            if a[i] in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-J", "-R", "-S"):
                i += 1
            i += 1
        rest = a[i:] or ["echo"]
    elif name in ("nohup", "time", "command", "builtin", "exec", "caffeinate", "noglob", "chronic",
                  "unbuffer", "setsid"):
        i = 1
        while i < len(a) and a[i].startswith("-"):
            i += 1
        rest = a[i:]
    elif name == "stdbuf":
        i = 1
        while i < len(a) and a[i].startswith("-"):
            i += 1
        rest = a[i:]
    elif name == "watch":
        i = 1
        while i < len(a) and a[i].startswith("-"):
            if a[i] in ("-n", "-d"):
                i += 1
            i += 1
        rest = a[i:]
        if len(rest) == 1:
            _nested(rest[0], cmd, out, ctx, "watch")
            out.cmds.append(cmd)
            return
    elif name == "rtk" and len(a) > 1:
        # rtk proxies a real command; judge that command (rtk env prints values unmasked, like env)
        sub = a[1]
        if sub in ("test", "err", "summary", "smart"):
            rest = a[2:]
        elif sub in ("read", "json"):
            rest = ["cat"] + [x for x in a[2:] if not x.startswith("-")]
        elif sub == "env":
            f = next((a[i + 1] for i, x in enumerate(a) if x in ("-f", "--filter") and i + 1 < len(a)), None)
            rest = ["printenv", f] if f else ["env"]
        elif sub in ("gain", "init", "config", "cc-economics", "deps", "--version", "--help"):
            rest = None
        else:
            rest = a[1:]
    if rest is not None:
        if not rest:
            out.cmds.append(cmd)
            return
        inner = Cmd(argv=rest, dynamic=cmd.dynamic, dyn_args=cmd.dyn_args[len(a) - len(rest):],
                    via=cmd.via + [name], remote=cmd.remote,
                    stdin_from=cmd.stdin_from, heredoc=cmd.heredoc, redirects=cmd.redirects, raw=cmd.raw)
        _unwrap(inner, out, ctx)
        return

    # nested shells
    if name in SHELLS:
        script = None
        for i, x in enumerate(a[1:], 1):
            if x.startswith("-") and "c" in x[1:] and not x.startswith("--"):
                if i + 1 < len(a):
                    script = a[i + 1]
                break
        if script is not None:
            out.cmds.append(cmd)
            if cmd.dynamic and "$" in script:
                out.notes.append(f"runtime-constructed: {name} -c with a runtime value")
            _nested(script, cmd, out, ctx, f"{name} -c")
            return
    if name == "ssh":
        host, i = None, 1
        while i < len(a):
            x = a[i]
            if x.startswith("-"):
                if x in ("-i", "-p", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-E", "-b", "-c", "-e", "-m",
                         "-O", "-Q", "-S", "-W", "-w", "-B", "-I"):
                    i += 1
                i += 1
                continue
            host = x
            break
        cmd.remote = host
        out.cmds.append(cmd)
        remote_cmd = a[i + 1:] if host is not None else []
        if remote_cmd:
            _nested(" ".join(remote_cmd), cmd, out, ctx, "ssh", remote=host)
        return
    if name in ("docker", "podman") and len(a) > 2 and a[1] == "exec" or name == "kubectl" and "exec" in a[1:3]:
        out.cmds.append(cmd)
        inner, target = None, f"{name}:?"
        if name in ("docker", "podman"):
            i = 2
            while i < len(a) and a[i].startswith("-"):
                if a[i] in ("-e", "--env", "-u", "--user", "-w", "--workdir", "--env-file"):
                    i += 1
                i += 1
            if i < len(a):
                target = f"{name}:{a[i]}"
                inner = a[i + 1:]
        if "--" in a:
            inner = a[a.index("--") + 1:]
        if inner:
            sub = Cmd(argv=inner, dynamic=cmd.dynamic, via=cmd.via + [f"{name} exec"], remote=target,
                      raw=cmd.raw)
            _unwrap(sub, out, ctx)
        return
    _emit(cmd, out, ctx)


def _ssh_host_index(a):
    i = 1
    while i < len(a):
        if a[i].startswith("-"):
            if a[i] in ("-i", "-p", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-E", "-b", "-c", "-e", "-m",
                        "-O", "-Q", "-S", "-W", "-w", "-B", "-I"):
                i += 1
            i += 1
            continue
        return i
    return None


def _nested(script, parent, out, ctx, how, remote=None):
    _parse_into(script, out, _Ctx(dict(ctx.env), ctx.depth + 1, parent.via + [how], remote or parent.remote))


def _reexamine_stdin_shell(cmd, out, ctx):
    """`bash <<EOF ... EOF`, `ssh host bash -s <<EOF`, `ssh host <<EOF`: the heredoc is the script."""
    if not cmd.heredoc:
        return
    if cmd.name in SHELLS and not any(x.startswith("-c") for x in cmd.argv[1:]):
        _nested(cmd.heredoc, cmd, out, ctx, f"{cmd.name} <<heredoc")
        return
    if cmd.name == "ssh" or cmd.name in ("docker", "podman", "kubectl") and "exec" in cmd.argv[1:3]:
        # the remote side runs a shell on stdin when its command is empty or a shell without -c
        if cmd.name == "ssh":
            host_i = _ssh_host_index(cmd.argv)
            remote = " ".join(cmd.argv[host_i + 1:]) if host_i else ""
        else:
            remote = " ".join(x for x in cmd.argv[2:] if not x.startswith("-"))
        if not remote.strip() or re.search(r"(^|[\s/])(ba|z|da|k)?sh(\s+-s)?\s*$", remote):
            _nested(cmd.heredoc, cmd, out, ctx, f"{cmd.name} <<heredoc", remote=cmd.remote)
