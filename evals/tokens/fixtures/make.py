"""Regenerate the token eval's generated fixtures (deterministic; the outputs are committed).
All names, numbers and messages are made up.
  python evals/tokens/fixtures/make.py
"""
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent

MODULES = ["invoice", "shipping", "auth", "catalog", "search", "cart", "ledger", "notify", "export", "report"]
FAILED = {  # nodeid -> traceback tail
    "tests/test_invoice.py::test_rounding_half_even":
        ["    assert round_money(Decimal('2.675')) == Decimal('2.68')",
         "E   AssertionError: assert Decimal('2.67') == Decimal('2.68')"],
    "tests/test_shipping.py::test_zone_lookup_unknown":
        ["    zone = zones.lookup('ZZ-999')",
         "E   KeyError: 'ZZ-999'"],
    "tests/test_auth.py::test_token_refresh_race":
        ["    assert session.token != old_token",
         "E   AssertionError: token was not rotated after concurrent refresh"],
}


def build_log():
    r = random.Random(4127)
    out, t = [], 0.0

    def ts():
        nonlocal t
        t += r.uniform(0.001, 0.08)
        return f"[{int(t // 60):02d}:{t % 60:06.3f}]"
    out.append(f"{ts()} runner: starting job 'unit-tests' on worker ci-worker-07 (image py311-slim)")
    for i in range(220):
        pkg = f"pkg{r.randint(1, 400):03d}"
        out.append(f"{ts()} pip: Collecting {pkg}=={r.randint(0, 9)}.{r.randint(0, 30)}.{r.randint(0, 9)}")
        if i % 17 == 0:
            out.append(f"{ts()} pip: WARNING: slow mirror, retrying download (attempt 1/3) ... ok")
    out.append(f"{ts()} pip: Successfully installed 220 packages")
    out.append(f"{ts()} pytest: ============================= test session starts ==============================")
    out.append(f"{ts()} pytest: collected 1242 items")
    fail_at = {300: "tests/test_invoice.py::test_rounding_half_even", 777: "tests/test_shipping.py::test_zone_lookup_unknown",
               1105: "tests/test_auth.py::test_token_refresh_race"}
    verbs = ["handles", "rejects", "accepts", "formats", "parses", "merges", "caches", "retries", "sorts", "splits"]
    nouns = ["empty_input", "large_batch", "unicode_names", "negative_totals", "missing_fields", "duplicate_ids",
             "stale_cache", "timezone_offsets", "zero_quantity", "long_descriptions"]
    for i in range(1242):
        if i in fail_at:
            out.append(f"{ts()} pytest: {fail_at[i]} FAILED")
            continue
        m = MODULES[i % len(MODULES)]
        name = f"test_{r.choice(verbs)}_{r.choice(nouns)}_{i}"
        out.append(f"{ts()} pytest: tests/test_{m}.py::{name} PASSED")
        if r.random() < 0.05:
            out.append(f"{ts()} pytest:   DeprecationWarning: utcnow() is deprecated; use now(timezone.utc)")
        if r.random() < 0.03:
            out.append(f"{ts()} pytest:   log: retry budget 3, nothing retried so far")
        if r.random() < 0.6:
            out.append(f"{ts()} app: DEBUG request id={r.getrandbits(40):010x} status=200 ms={r.randint(1, 90)}")
    out.append(f"{ts()} pytest: =================================== FAILURES ===================================")
    for nodeid, tail in FAILED.items():
        fn = nodeid.split("::")[1]
        out.append(f"{ts()} pytest: ____________________________ {fn} ____________________________")
        for k in range(r.randint(8, 14)):
            out.append(f"{ts()} pytest: src/{r.choice(MODULES)}/core.py:{r.randint(10, 900)}: in {r.choice(verbs)}_{k}")
        out += [f"{ts()} pytest: {x}" for x in tail]
    out.append(f"{ts()} pytest: ========== 3 failed, 1239 passed, 61 warnings in {t:.2f}s ==========")
    out.append(f"{ts()} runner: job 'unit-tests' finished with exit code 1")
    (HERE / "ci_log" / "build.log").write_text("\n".join(out) + "\n")


def ledger():
    r = random.Random(250)
    parts = ['"""Ledger helpers for a made-up billing service (fixture for the gutcheck token eval)."""',
             "import math", "from dataclasses import dataclass", "", ""]
    shapes = ["sum", "clamp", "scale", "bucket", "normalise", "diff"]
    decoy_at, real_at = 41, 97
    for i in range(160):
        if i == decoy_at:
            parts.append('''def late_fee_legacy(balance_cents, days_late):
    """Pre-2021 late fee, kept for re-billing old invoices. Not used for new invoices."""
    if days_late <= 0:
        return 0
    return min(5000, 300 + balance_cents * days_late // 1000)


''')
        if i == real_at:
            parts.append('''def late_fee(balance_cents, days_late):
    """Late fee for an overdue invoice, in cents.

    Five grace days; then 1.5% of the balance for every started week of lateness,
    at least 250 cents and at most 4000 cents.
    """
    over = days_late - 5
    if over <= 0 or balance_cents <= 0:
        return 0
    weeks = math.ceil(over / 7)
    fee = balance_cents * 15 * weeks // 1000
    return max(250, min(4000, fee))


''')
        kind = shapes[i % len(shapes)]
        a, b = r.randint(2, 97), r.randint(2, 97)
        name = f"{kind}_{['entries', 'rows', 'amounts', 'credits', 'debits', 'totals'][r.randint(0, 5)]}_{i}"
        body = {
            "sum": ["    total = 0", "    for x in values:", f"        if x % {a} != 0:", "            total += x",
                    f"    return total + {b}"],
            "clamp": [f"    lo, hi = {a}, {a + b}", "    return [min(hi, max(lo, x)) for x in values]"],
            "scale": [f"    factor = {a} / {b}", "    return [round(x * factor, 2) for x in values]"],
            "bucket": ["    out = {}", "    for x in values:", f"        out.setdefault(x // {a}, []).append(x)",
                       "    return out"],
            "normalise": ["    peak = max(values) if values else 1", f"    return [x / (peak or 1) * {b} for x in values]"],
            "diff": [f"    return [b - a for a, b in zip(values, values[{1 + a % 3}:])]"],
        }[kind]
        parts.append(f"def {name}(values):")
        parts.append(f'    """{kind.capitalize()} helper #{i} (made up)."""')
        parts += body
        for k in range(r.randint(3, 9)):
            parts.append(f"    # note {k}: {r.choice(['kept for audit', 'see billing runbook', 'rounding per policy', 'legacy path', 'checked nightly'])}")
        parts += ["", ""]
        if i % 20 == 19:
            parts += ["@dataclass", f"class Batch{i}:", "    id: int", f"    amount_cents: int = {a * b}",
                      "    currency: str = 'EUR'", "", ""]
    (HERE / "big_source" / "ledger.py").write_text("\n".join(parts).rstrip() + "\n")


def freeze(n=760, folder="freeze"):
    """A pip-freeze listing with the asked-for pin in the middle, where a head+tail view misses it."""
    r = random.Random(11)
    syl = ["al", "bre", "cor", "dax", "el", "fen", "gor", "hul", "ix", "jor", "kel", "lum", "mor", "nex", "or", "pyr",
           "qua", "ros", "sil", "tor", "ul", "vex", "wyn", "zor"]
    names = set()
    while len(names) < n:
        names.add("".join(r.choice(syl) for _ in range(r.randint(2, 3))) + r.choice(["", "-py", "-core", "kit"]))
    names.discard("quillstone")
    lines = [f"{n}=={r.randint(0, 9)}.{r.randint(0, 30)}.{r.randint(0, 12)}" for n in sorted(names)]
    lines.insert(len(lines) // 2, "quillstone==4.17.2")
    (HERE / folder).mkdir(exist_ok=True)
    (HERE / folder / "freeze.txt").write_text("\n".join(lines) + "\n")


LONG_LOOP_FACTS = ("VERMILION-HARE-31", "48213", "c0ffee9a1d")   # run.py's check_long_loop asks for these


def long_loop(n=13):
    """A long multi-stage session: init.py prints three facts once and deletes itself, then modules under the
    Read gate's 30k chars are read in full, each followed by small check.py calls."""
    r = random.Random(77)
    d = HERE / "long_loop"
    (d / "modules").mkdir(parents=True, exist_ok=True)
    verbs = ["sync", "load", "merge", "flush", "probe", "index", "prune", "stage"]
    nouns = ["orders", "queues", "shards", "tokens", "leases", "routes", "blobs", "tenants"]
    for m in range(1, n + 1):
        parts = [f'"""Module m{m:02d} of a made-up storage service (fixture for the gutcheck token eval)."""',
                 "RETRY_BASE = 2", "", ""]
        i = 0
        while len("\n".join(parts)) < 24_000:
            name = f"{r.choice(verbs)}_{r.choice(nouns)}_{m:02d}{i:02d}"
            a, b = r.randint(2, 9), r.randint(10, 99)
            parts += [f"def {name}(items, retries={a}):",
                      f'    """{name.split("_")[0].capitalize()} step {i} of m{m:02d}: at most {a} retries, batch {b}."""',
                      "    done = []", f"    for start in range(0, len(items), {b}):",
                      f"        batch = items[start:start + {b}]", "        for attempt in range(retries):",
                      "            if all(x is not None for x in batch):", "                done.extend(batch)",
                      "                break", f"            batch = [x for x in batch if x is not None][:{b}]",
                      f"    # {r.choice(['kept for audit', 'see storage runbook', 'retry per policy', 'legacy path'])}",
                      "    return done", "", ""]
            i += 1
        (d / "modules" / f"m{m:02d}.py").write_text("\n".join(parts).rstrip() + "\n")
    codename, build, checksum = LONG_LOOP_FACTS
    (d / "init.py").write_text(f'''"""One-shot release init: prints the release facts once, then removes itself."""
import os

print("release codename: {codename}")
print("build number: {build}")
print("artifact checksum: {checksum}")
os.remove(__file__)
''')
    (d / "check.py").write_text('''"""Quick per-module check: python3 check.py NN [NN ...]"""
import ast
import sys

for n in sys.argv[1:]:
    fns = [f.name for f in ast.parse(open(f"modules/m{int(n):02d}.py").read()).body if isinstance(f, ast.FunctionDef)]
    print(f"m{int(n):02d}: {len(fns)} functions, {sum(f.startswith('sync_') for f in fns)} sync_, ok")
''')



def sales(n=80):
    """heredoc_iter: a sales ledger with a few refunds (negative qty), which the task's bug report says to skip."""
    r = random.Random(5150)
    d = HERE / "heredoc_iter"
    d.mkdir(exist_ok=True)
    rows = ["date,region,customer,product,qty,unit_price_cents"]
    for i in range(n):
        qty = -r.randint(1, 3) if i % 11 == 7 else r.randint(1, 12)
        rows.append(f"2026-0{r.randint(1, 9)}-{r.randint(10, 28)},{r.choice(['north', 'south', 'east', 'west'])},"
                    f"cust-{r.randint(100, 140)},{r.choice(['anvil', 'bolt', 'crate', 'drill', 'easel'])},{qty},"
                    f"{r.randint(150, 9900)}")
    (d / "sales.csv").write_text("\n".join(rows) + "\n")

if __name__ == "__main__":
    (HERE / "ci_log").mkdir(exist_ok=True)
    (HERE / "big_source").mkdir(exist_ok=True)
    build_log()
    ledger()
    freeze()
    freeze(270, "freeze_small")
    long_loop()
    sales()
    print("fixtures written")
