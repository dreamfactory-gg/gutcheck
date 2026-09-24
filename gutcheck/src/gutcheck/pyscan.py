"""Find the side-effecting calls in Python source without running it.

Returns shell commands the code runs (from literal subprocess/os.system arguments, so they can be
checked like any other command) and risky call sites (network writes, deletes, kills) as short lines.
Text that merely *mentions* a command inside a string (docs, file edits, test data) is ignored.
"""
import ast

SHELL_CALLS = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
               "subprocess.Popen", "os.system", "os.popen", "sp.run", "sp.call", "sp.check_output", "sp.Popen",
               "sp.check_call", "run", "check_output", "check_call", "Popen"}
RISKY = {
    "os.kill": "kill", "os.killpg": "kill", "signal.pthread_kill": "kill",
    "shutil.rmtree": "delete", "os.remove": "delete", "os.unlink": "delete", "os.rmdir": "delete",
    "os.removedirs": "delete",
    "requests.post": "net-write", "requests.put": "net-write", "requests.patch": "net-write",
    "requests.delete": "net-delete", "httpx.post": "net-write", "httpx.put": "net-write",
    "httpx.patch": "net-write", "httpx.delete": "net-delete", "session.post": "net-write",
    "s.post": "net-write", "client.post": "net-write",
    "smtplib.SMTP": "net-write", "ftplib.FTP": "net-write", "paramiko.SSHClient": "remote",
    "boto3.client": "cloud", "boto3.resource": "cloud",
}
METHOD_WRITE = {"POST", "PUT", "PATCH", "DELETE"}


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    return None


def _literal_cmd(arg):
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, (ast.List, ast.Tuple)) and all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                                                      for e in arg.elts):
        import shlex
        return " ".join(shlex.quote(e.value) for e in arg.elts)
    if isinstance(arg, ast.JoinedStr):
        parts = []
        for v in arg.values:
            parts.append(v.value if isinstance(v, ast.Constant) else "$X")
        return "".join(parts)
    return None


def scan(source):
    """Return (commands, findings, ok). ok=False when the source doesn't parse."""
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return [], [], False
    lines = source.splitlines()
    cmds, finds = [], []
    bare_ok = "from subprocess import" in source
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func) or ""
        short = ".".join(name.split(".")[-2:])
        lo, hi = max(0, node.lineno - 4), min(len(lines), node.lineno + 3)
        line = " ⏎ ".join(x.strip() for x in lines[lo:hi] if x.strip())[:700]
        if (name in SHELL_CALLS or short in SHELL_CALLS) and ("." in name or bare_ok):
            arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
            lit = _literal_cmd(arg) if arg is not None else None
            if lit is not None:
                cmds.append(lit)
            else:
                finds.append(("shell-dynamic", line))
            continue
        if name in RISKY or short in RISKY:
            finds.append((RISKY.get(name) or RISKY[short], line))
            continue
        if short.endswith(("urlopen", "Request")) or name.endswith(("urlopen", "Request")):
            method = next((k.value.value for k in node.keywords if k.arg == "method"
                           and isinstance(k.value, ast.Constant)), None)
            has_data = any(k.arg == "data" for k in node.keywords) or len(node.args) > 1
            if (method or "").upper() in METHOD_WRITE or has_data and (method or "POST").upper() != "GET":
                finds.append(("net-write", line))
            continue
        if short.endswith(("execute", "executescript")) and node.args:
            sql = _literal_cmd(node.args[0]) or ""
            import re
            if re.search(r"(?i)\b(drop\s+(table|view|index|database)|delete\s+from|truncate)\b", sql) and \
                    "sqlite3.connect" not in source:  # local sqlite files are the agent's own working data
                finds.append(("sql-delete", line))
        if name.endswith((".unlink", ".rmdir")) and not name.startswith("os."):
            finds.append(("delete", line))
    if all_urls_inference(source):
        finds = [f for f in finds if f[0] != "net-write"]
    return cmds, finds, True


# Model-inference and local endpoints: POSTing a prompt there computes an answer, it doesn't change records.
INFERENCE_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "api.anthropic.com", "api.openai.com", "openrouter.ai",
                   "api.typesafe.ai", "ai-gateway.vercel.sh", "generativelanguage.googleapis.com",
                   "api.deepseek.com", "api.together.xyz", "api.groq.com", "api.mistral.ai", "api.fireworks.ai",
                   "api.x.ai", "api.cohere.com", "api.z.ai", "open.bigmodel.cn")


def all_urls_inference(source):
    import re
    hosts = re.findall(r"https?://([^/'\"\s:]+)", source)
    return bool(hosts) and all(any(h == ih or h.endswith("." + ih) for ih in INFERENCE_HOSTS) for h in hosts)
