"""Fake local stdio MCP server for the gutcheck token eval (stdlib only, made-up data).

Tools return big outputs so gutcheck's MCP output layer has something to do:
  fetch_ci_logs(run_id)   ~800-line job log with one real failure       (log kind -> receipt view)
  list_orders(region)     ~300-order JSON document, one order is stuck  (read kind -> JSON structure view)
Output is deterministic per argument.
"""
import json
import random
import sys

SERVER = {"name": "ci-fixture", "version": "1.0.0"}
TOOLS = [
    {"name": "fetch_ci_logs", "description": "Fetch the full log of a CI run (all jobs, in order).",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "integer", "description": "CI run number"}},
                     "required": ["run_id"]}},
    {"name": "list_orders", "description": "List every order in a sales region as a JSON document.",
     "inputSchema": {"type": "object", "properties": {"region": {"type": "string", "description": "region name"}},
                     "required": ["region"]}},
]
JOBS = ["lint", "unit", "build-image", "integration", "deploy-staging", "smoke", "deploy-canary", "promote"]


def ci_logs(run_id):
    r = random.Random(f"logs-{run_id}")
    out, t = [], 0
    for job in JOBS:
        out.append(f"== job {job} (run {run_id}) ==")
        for i in range(110 if job != "promote" else 12):
            t += r.randint(1, 40)
            step = r.choice(["fetch", "compile", "copy", "cache", "probe", "sync", "render", "hash"])
            out.append(f"{t:08d}ms {job}: {step} chunk {i:04d} ok ({r.randint(1, 999)} kB, {r.randint(1, 60)} ms)")
            if r.random() < 0.02:
                out.append(f"{t:08d}ms {job}: note: cache miss for layer {r.getrandbits(32):08x}, rebuilt")
        if job == "deploy-canary":
            out.append(f"{t:08d}ms deploy-canary: ERROR health check failed: upstream returned 503 (code HC-7731)")
            out.append(f"{t:08d}ms deploy-canary: job failed after 3 health-check attempts")
            break
        out.append(f"== job {job} passed ==")
    out.append(f"run {run_id} finished: 1 job failed, {JOBS.index('deploy-canary')} passed, 1 skipped")
    return "\n".join(out)


def orders(region):
    r = random.Random(f"orders-{region}")
    stuck = r.randint(60, 260)
    items = []
    for i in range(300):
        status = "stuck" if i == stuck else r.choice(["paid", "shipped", "delivered", "delivered", "refunded"])
        items.append({"id": f"ORD-{58000 + i * 7 + (i % 5)}", "region": region, "status": status,
                      "customer": f"cust-{r.randint(1000, 9999)}", "total_cents": r.randint(500, 90000),
                      "items": r.randint(1, 9), "created": f"2026-0{r.randint(1, 8)}-{r.randint(10, 28)}"})
    return json.dumps({"region": region, "count": len(items), "orders": items}, indent=1)


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
        return {"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": pv, "serverInfo": SERVER,
                                                        "capabilities": {"tools": {}}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        args = p.get("arguments") or {}
        if p.get("name") == "fetch_ci_logs":
            text = ci_logs(int(args.get("run_id", 0)))
        elif p.get("name") == "list_orders":
            text = orders(str(args.get("region", "")))
        else:
            return {"jsonrpc": "2.0", "id": mid, "result": {"isError": True,
                                                            "content": [{"type": "text", "text": "unknown tool"}]}}
        return {"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if mid is None:
        return None  # notifications
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # keep serving
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
