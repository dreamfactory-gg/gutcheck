"""Client for TypeSafe's Jev (a System One model: typed judgments over text, not an LLM).

call(state, questions) -> answers dict. Routes: direct API, then the Vercel AI Gateway.
Hooks call with a short deadline and treat JevError as "fail open".
"""
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import config, secrets

MAX_STATE_CHARS = 60_000   # ~15k tokens; Jev allows 32k tokens for state + the longest question
MAX_ITEMS = 150            # questions per request
ITEM_CHARS = 4000


class JevError(Exception):
    """waf=True when every route that answered rejected the payload at a firewall (content-based block),
    as opposed to an outage or timeout. Callers treat waf blocks as "couldn't check", not "fail open"."""

    def __init__(self, msg, waf=False):
        super().__init__(msg)
        self.waf = waf


def _is_waf(code, body):
    b = body.lower()
    return code == 403 and ("<html" in b or "cloudflare" in b or "attention required" in b or "blocked" in b)


def routes():
    c = config.load()
    return [
        (c["keys"]["typesafe_service"], "https://api.typesafe.ai/v1/systemone", "jev-latest"),
        (c["keys"]["gateway_service"], "https://ai-gateway.vercel.sh/typesafe/v1/systemone", "typesafe-ai/jev"),
    ]


_keys = {}


def _key(service):
    if service not in _keys:
        _keys[service] = secrets.get(service)
    return _keys[service]


def have_key():
    return any(_key(s) for s, _, _ in routes())


def call(state, questions, timeout=60, deadline=None):
    """One Jev request. `deadline` (seconds, total) bounds retries and fallbacks for hooks."""
    start = time.monotonic()
    errors = []
    waf_routes = 0
    for service, url, model in routes():
        k = _key(service)
        if not k:
            errors.append(f"{service}: no key")
            continue
        body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
        req = urllib.request.Request(url, body, {"Authorization": f"Bearer {k}",
                                                 "Content-Type": "application/json"})
        for attempt in range(3):
            left = timeout if deadline is None else deadline - (time.monotonic() - start)
            if left <= 0.05:
                raise JevError(f"deadline exceeded; errors: {'; '.join(errors) or 'none'}")
            try:
                with urllib.request.urlopen(req, timeout=min(timeout, left)) as r:
                    return json.load(r)["answers"]
            except urllib.error.HTTPError as e:
                body = e.read()[:2000].decode(errors="replace")
                waf = _is_waf(e.code, body)
                err = f"{service}: HTTP {e.code} " + ("firewall block (HTML page)" if waf else body[:200])
                errors.append(err)
                if waf:
                    waf_routes += 1
                    break
                if e.code in (400, 422):
                    raise JevError(f"request rejected: {err}")
                if (e.code == 429 or e.code >= 500) and attempt < 2 and not waf_routes:
                    # (after a firewall block upstream, a gateway 5xx is the same block: don't wait and retry)
                    time.sleep(min(0.5 * 2 ** attempt, 0.25 if deadline else 4))
                    continue
                break
            except Exception as e:  # network, timeout, bad JSON
                errors.append(f"{service}: {type(e).__name__}: {str(e)[:150]}")
                break
    # A firewall block on the direct route plus a 5xx from the gateway (which forwards to the same
    # backend) is still a content block, not an outage.
    raise JevError(f"all routes failed; errors: {' | '.join(errors)}", waf=waf_routes > 0)


def _batches(items):
    out, cur, size = [], [], 0
    for key, text in items:
        text = text[:ITEM_CHARS]
        if cur and (len(cur) >= MAX_ITEMS or size + len(text) > MAX_STATE_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append((key, text))
        size += len(text) + 12
    if cur:
        out.append(cur)
    return out


def noul_each(items, condition, context=None, workers=8, deadline=None):
    """items: list of (key, text). Ask `condition` once per item. Returns {key: p}."""
    def run(batch):
        state = {"items": {str(k): t for k, t in batch}}
        if context:
            state["context"] = context
        qs = {f"q{n}": {"type": "noul", "instructions": f'Consider only items["{k}"]. {condition}'}
              for n, (k, _) in enumerate(batch)}
        ans = call(state, qs, deadline=deadline)
        return {k: ans[f"q{n}"]["noul"] for n, (k, _) in enumerate(batch)}

    out = {}
    with ThreadPoolExecutor(workers) as ex:
        for part in ex.map(run, _batches(items)):
            out.update(part)
    return out


def pick(question, options, state=None, deadline=None):
    """options: {name: description}. Returns {choice, confidence, probabilities}."""
    a = call(state if state is not None else question,
             {"pick": {"type": "choice", "instructions": question, "criteria": options}},
             deadline=deadline)["pick"]
    return {"choice": a["choice"], "confidence": a.get("confidence"), "probabilities": a["probabilities"]}
