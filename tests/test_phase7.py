"""Deterministic parts of Phase 7 (no Jev, no Codex). Fixtures are made up.
Run: ~/.local/share/gutcheck/venv/bin/python tests/test_phase7.py"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tokenmaxxing", "src"))
tmp_state = tempfile.mkdtemp()
os.environ["TOKENMAXXING_STATE_DIR"] = tmp_state
os.environ["TOKENMAXXING_CONFIG_DIR"] = tempfile.mkdtemp()
from tokenmaxxing import evidence as ev  # noqa: E402
from tokenmaxxing import explain, jobs
from tokenmaxxing import relevance as rel

# ---- evidence: claim parsing -------------------------------------------------------------------
c = ev.claims("- The limiter caps reads at 20 per window (`src/limits.py:12-18`, `:40`).\n```\nx.py:1\n```\nplain line")
assert len(c) == 1, c
files = [x for x in c[0]["cites"] if x["kind"] == "file"]
assert [(f["path"], f["start"], f["end"]) for f in files] == [("src/limits.py", 12, 18), ("src/limits.py", 40, 40)], files
assert ev.claims("see [notes](/abs/dir/notes.md:7) for why")[0]["cites"][0]["path"] == "/abs/dir/notes.md"
assert ev.claims("app.ts:12:5 has the call")[0]["cites"][0]["end"] == 12  # column, not a range

# numbers: citations, link text, section refs, list numbering and code spans are references, not claims
t = "3. **Timeout is 45 s** in [cfg:10-48](/a/cfg.py:10) per §§8, 10.4 and `retry(7)`; see L90-95."
cl = ev.claims(t)[0]
assert ev.claim_numbers(cl["text"], cl["cites"]) == ["45"], ev.claim_numbers(cl["text"], cl["cites"])
assert ev.number_in("1200", "limit = 1,200 rows") and ev.number_in("45", "SCAN = 45\n")
assert not ev.number_in("45", "SCAN = 450") and not ev.number_in("4", "v4.5")

# commands: only a command the agent actually ran (or a segment of it) counts as evidence
log = [("cd app && npm run check 2>&1 | tail -3", "16 files, 483 passed"), ('rg -n "page: true" src', "hit")]
assert ev.command_evidence("npm run check", log).endswith("483 passed")
assert ev.command_evidence("page: true", log) is None
assert ev.command_evidence("ls", log) is None

# file evidence and past-the-end lines
d = Path(tempfile.mkdtemp())
(d / "m.py").write_text("\n".join(f"line{i}" for i in range(1, 21)) + "\n")
txt, err = ev.file_evidence(str(d / "m.py"), 10, 10)
assert err is None and "10: line10" in txt and "7: line7" in txt and "14: line14" not in txt
assert "past the end" in ev.file_evidence(str(d / "m.py"), 99, 99)[1]
assert ev.resolve("m.py", str(d), {}) == str(d / "m.py")
assert ev.resolve("nope.py", str(d), {}) is None

# decisions: precision first
base = {"problems": [], "numbers_missing": []}
assert ev.decide({**base, "relation": "supports", "confidence": 0.9}) == (False, None)
assert ev.decide({**base, "relation": "says_nothing", "confidence": 0.6})[0] is False       # below the gate
assert ev.decide({**base, "relation": "contradicts", "confidence": 0.85})[0] is True
assert ev.decide({**base, "relation": "says_nothing", "confidence": 0.95,
                  "wide": {"relation": "supports", "confidence": 0.9}})[0] is False       # wider look wins
assert ev.decide({**base, "relation": "says_nothing", "confidence": 0.3, "numbers_missing": ["45"]})[0] is True
assert ev.decide({**base, "problems": ["x.py:99: line 99 is past the end"]})[0] is True
# model classifiers flag only "contradicts" (and past-EOF lines); says_nothing / missing numbers are logged
mb = {**base, "model_meta": {"seconds": 1}}
assert ev.decide({**mb, "relation": "contradicts"})[0] is True
assert ev.decide({**mb, "relation": "says_nothing", "numbers_missing": ["45"]})[0] is False
assert ev.decide({**mb, "model_error": "x", "problems": ["a.py:9: line 9 is past the end"]})[0] is True
assert ev._parse_relations('ok [{"i": 2, "relation": "contradicts"}, {"i": 1, "relation": "supports"}]', 3) == \
    [{"relation": "supports", "confidence": 1.0}, {"relation": "contradicts", "confidence": 1.0}, None]
msg = ev.feedback([{"flag": True, "claim": "The cap is 45", "cites": ["a.py:3"], "reason": "number 45 isn't in"}])
assert msg.count("\n") == 0 and "a.py:3" in msg

# hook: never re-fires on the continuation, never runs when off, silent on reports without citations
assert ev.on_subagent_stop({"stop_hook_active": True, "last_assistant_message": "a.py:1 x"}) is None
assert ev.on_subagent_stop({"last_assistant_message": "All done, nothing to cite."}) is None

# a scratch repo with an upstream
r = Path(tempfile.mkdtemp())
g = lambda *a: subprocess.run(["git", "-C", str(r), *a], capture_output=True, text=True, check=True)  # noqa: E731
g("init", "-q", "-b", "main")
g("config", "user.email", "t@example.com")
g("config", "user.name", "t")
(r / "CLAUDE.md").write_text("- Keep functions short.\n")
(r / "a.py").write_text("x = 1\n")
g("add", ".")
g("commit", "-qm", "one")
g("remote", "add", "origin", str(r))
g("update-ref", "refs/remotes/origin/main", "HEAD")
g("branch", "-u", "origin/main")
(r / "a.py").write_text("x = 1\n<<<<<<< HEAD\n")
g("commit", "-qam", "two")

# ---- relevance: diff parsing and changed symbols --------------------------------------------------
files = rel.parse_diff(g("show", "--format=", "HEAD").stdout)
assert files[0]["path"] == "a.py" and files[0]["added"] == 1 and files[0]["hunks"][0]["new_start"] == 1
label, _ = rel.resolve_scope(r, commit="HEAD")
assert label.startswith("commit ")
(r / "b.py").write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
g("add", "b.py")
g("commit", "-qm", "three")
(r / "b.py").write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 3\n")
f = rel.parse_diff(g("diff", "HEAD").stdout)[0]
names = [s["name"] for s in rel.changed_symbols(r, f)]
assert names == ["beta"], names
assert rel.symbol_name("function Pipeline:_drain(self)") == "_drain"
assert rel.TEST_RE.search("tests/core/x.test.luau") and rel.TEST_RE.search("pkg/test_a.py")
assert not rel.TEST_RE.search("src/contest.py")

# ---- explainer and jobs ---------------------------------------------------------------------------
assert explain.extract_html("Here:\n```html\n<!DOCTYPE html><html><body>x</body></html>\n```") == \
    "<!DOCTYPE html><html><body>x</body></html>"
page = "<html>" + "<h2>a</h2>" * 4 + "<p>quiz</p><script>1</script></html>"
assert explain.check_html(page) == []
assert "loads a network resource" in explain.check_html(page.replace("<script>", '<script src="https://x.io/a.js">'))
quiz = page.replace("<p>quiz</p>", '<button data-c="a">1</button><button data-c="b">2</button><button>3</button>')
assert any("lack data-c" in x for x in explain.check_html(quiz))
assert jobs.slug("Server: never persist an acknowledged drop marker at shutdown", 30) == "server-never-persist-an"
jid, jd = jobs.new("review", "demo")
jobs.write_status(jd, state="done", a=1)
assert jobs.read_status(jd)["a"] == 1 and jobs.listing("review")[0][1]["state"] == "done"

print("ok")
