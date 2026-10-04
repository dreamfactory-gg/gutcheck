"""Minimal MCP stdio client (JSON-RPC 2.0, newline-delimited) for talking to local MCP servers from code."""
import json
import subprocess
import threading


class MCP:
    def __init__(self, cmd, timeout=60):
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, bufsize=1)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "gutcheck", "version": "0.1"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg):
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method, params=None):
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            box = {}

            def read():
                while True:
                    line = self.p.stdout.readline()
                    if not line:
                        return
                    try:
                        m = json.loads(line)
                    except ValueError:
                        continue
                    if m.get("id") == rid:
                        box["m"] = m
                        return

            t = threading.Thread(target=read, daemon=True)
            t.start()
            t.join(self.timeout)
            if "m" not in box:
                raise TimeoutError(f"MCP {method} timed out")
            if "error" in box["m"]:
                raise RuntimeError(box["m"]["error"])
            return box["m"]["result"]

    def tools(self):
        return self.request("tools/list")["tools"]

    def call(self, name, args):
        r = self.request("tools/call", {"name": name, "arguments": args})
        return "\n".join(c.get("text", "") for c in r.get("content", []) if c.get("type") == "text"), r

    def close(self):
        try:
            self.p.stdin.close()
            self.p.terminate()
        except Exception:
            pass
