"""Deterministic parts of the pre-push house-rules check (no Jev). Fixtures are made up.
Run: ~/.local/share/gutcheck/venv/bin/python tests/test_pushcheck.py"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gutcheck", "src"))
os.environ["GUTCHECK_STATE_DIR"] = tempfile.mkdtemp()
os.environ["GUTCHECK_CONFIG_DIR"] = tempfile.mkdtemp()
from gutcheck import pushcheck as pc  # noqa: E402

# ---- push check ---------------------------------------------------------------------------------
assert pc.push_target("git status && git push origin main", "/r") == ("/r", ["origin", "main"])
assert pc.push_target("git -C sub push", "/r")[0] == "/r/sub"
assert pc.push_target("git commit -m 'push it'", "/r") is None
assert pc.push_target("echo git push", "/r") is None
cands = pc._candidates("# Rules\n- Never use `any`; prefer\n  `unknown` instead.\n\n```\n- not a rule\n```\n| a | b |\n")
assert cands == [(2, "Never use `any`; prefer `unknown` instead.")], cands
diff = ("diff --git a/src/a.ts b/src/a.ts\n@@ -1,2 +1,3 @@ fn\n const x = 1\n+let y: any = 2\n+const company = 3\n"
        "diff --git a/.env b/.env\n@@ -0,0 +1 @@\n+X=1\n"
        "diff --git a/b.py b/b.py\n@@ -1 +1,2 @@\n+<<<<<<< HEAD\n")
hk = pc.hunks(diff)
assert [h[0] for h in hk] == ["src/a.ts", ".env", "b.py"]
found = pc.deterministic(diff, [".env"], [("CLAUDE.md:2", "Never use `any`; prefer `unknown`.")])
kinds = sorted(k for k, _, _ in found)
assert kinds == ["conflict marker", "rule literal", "secret file"], found
assert sum(k == "rule literal" for k, _, _ in found) == 1  # "company" doesn't match `any`
assert pc.summary({"deterministic": [], "violations": []}) is None

# pending diff in a scratch repo with an upstream
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
assert pc.pending_base(str(r)) == "origin/main"

# ast-grep runs only when the repo has an sgconfig.yml; missing ast-grep is a note, not a failure
(r / "CLAUDE.md").unlink()
g("commit", "-qam", "no rules")
calls = []
which = pc.shutil.which
pc.shutil.which = lambda *a, **k: calls.append(a) or which(*a, **k)
res = pc.check_repo(str(r))
assert not calls and res["ast_grep"] is None, res
(r / "sgconfig.yml").write_text("ruleDirs: [lint]\n")
(r / "lint").mkdir()
(r / "lint" / "no-eval.yml").write_text("id: no-eval\nlanguage: python\nseverity: error\nmessage: no eval\n"
                                        "rule:\n  pattern: eval($$$)\n")
(r / "a.py").write_text("y = eval('2')\n")
g("add", ".")
g("commit", "-qm", "lint")
res = pc.check_repo(str(r))
if which("ast-grep"):
    assert ("ast-grep", "a.py", "no-eval (line 1): no eval") in res["deterministic"], res
    (r / "lint" / "hint.yml").write_text("id: style\nlanguage: python\nseverity: hint\nmessage: hint\n"
                                          "rule:\n  pattern: eval($$$)\n")
    assert pc.ast_grep_hits(str(r), ["a.py"], 20) == ([("ast-grep", "a.py", "no-eval (line 1): no eval")], None)
    (r / "lint" / "bad.yml").write_text("id: bad\nlanguage: pythn\nrule:\n  pattern: x\n")
    hits, note = pc.ast_grep_hits(str(r), ["a.py"], 20)
    assert not hits and note.startswith("ast-grep: scan failed"), note
path = os.environ["PATH"]
os.environ["PATH"] = "/usr/bin:/bin"  # git, no ast-grep
res = pc.check_repo(str(r))
os.environ["PATH"] = path
assert res["ast_grep"] == "ast-grep not found; sgconfig.yml rules skipped", res

print("ok")
