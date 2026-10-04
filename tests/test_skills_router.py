"""Skill index, skill router and doc/memory routing, with made-up fixtures and a fake Jev.
Run: python3 tests/test_skills_router.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="gc-test-")).resolve()
CL = TMP / "claude"
os.environ.update(GUTCHECK_CLAUDE_DIR=str(CL), TOKENMAXXING_STATE_DIR=str(TMP / "state"),
                  TOKENMAXXING_CONFIG_DIR=str(TMP / "config"))
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tokenmaxxing", "src"))


def write(p, text):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---- fixtures: one user skill, one synced skill, one plugin with a skill and a user-only command -------------
write(CL / "skills/tidy-notes/SKILL.md", "---\nname: tidy-notes\ndescription: >\n  Reformat meeting notes into\n"
      "  a clean summary. Use when the user pastes raw notes.\n---\nbody\n")
write(CL / "skills/synced/bucket-1/sheets/SKILL.md", "---\nname: sheets\ndescription: \"Edit spreadsheets; use for any "
      ".ods file the user names.\"\n---\n")
write(CL / "skills/synced/.bucket-1", "")
PLUG = TMP / "plugins/toolkit"
write(PLUG / "skills/helper/SKILL.md", "---\nname: helper\ndescription: Draft release notes from a changelog.\n---\n")
write(PLUG / "commands/ship.md", "---\ndescription: Ship it\ndisable-model-invocation: true\n---\n")
write(TMP / "plugins/tokenmaxxing/skills/jev/SKILL.md", "---\nname: jev\ndescription: own skill\n---\n")
write(TMP / "plugins/off/skills/x/SKILL.md", "---\nname: x\ndescription: disabled plugin\n---\n")
write(CL / "plugins/installed_plugins.json", json.dumps({"version": 2, "plugins": {
    "toolkit@market": [{"scope": "user", "installPath": str(PLUG)}],
    "tokenmaxxing@local": [{"scope": "user", "installPath": str(TMP / "plugins/tokenmaxxing")}],
    "off@market": [{"scope": "user", "installPath": str(TMP / "plugins/off")}]}}))
write(CL / "settings.json", json.dumps({"enabledPlugins": {"off@market": False}}))

from gutcheck_core import config, jev  # noqa: E402

from tokenmaxxing import ctxload, router, skills  # noqa: E402

# ---- skill index ---------------------------------------------------------------------------------------------
idx, new = skills.build()
names = {s["name"]: s for s in idx}
assert set(names) == {"tidy-notes", "anthropic-skills:sheets", "toolkit:helper", "toolkit:ship"}, names
assert names["tidy-notes"]["description"].startswith("Reformat meeting notes into a clean summary")
assert names["tidy-notes"]["managed"] and names["anthropic-skills:sheets"]["managed"]
assert not names["toolkit:helper"]["managed"]
assert not names["toolkit:ship"]["model_invocable"] and names["toolkit:ship"]["kind"] == "command"
assert skills.recommended_overrides(idx) == {"tidy-notes": "user-invocable-only",
                                             "anthropic-skills:sheets": "user-invocable-only"}
assert {s["name"] for s in skills.router_candidates()} == {"tidy-notes", "anthropic-skills:sheets", "toolkit:helper"}
assert skills.build()[1] == []  # nothing new the second time
assert skills.distinctive_literals(skills.router_candidates())["anthropic-skills:sheets"] == {".ods"}
assert ".ods" in skills.prompt_literals("open budget.ods please")

# ---- decisions: the Choice says which, the Noul says whether; a literal trigger stands in for the Noul -------
R = lambda *rows: {"ranked": list(rows), "none": 0.0}  # noqa: E731
assert skills.decide(R(("a", 0.9, 0.9, False)), 0.3, 0.5) == "a"
assert skills.decide(R(("a", 0.9, 0.2, False)), 0.3, 0.5) is None
assert skills.decide(R(("a", 0.45, 0.4, False), ("b", 0.41, 0.6, False)), 0.3, 0.5) == "b"
assert skills.decide(R(("a", 0.41, 0.1, True)), 0.3, 0.5) == "a"
assert skills.decide(R(("a", 0.2, 0.9, True)), 0.3, 0.5) is None
assert skills.decide(R(("a", 0.1, 0.1, False), ("b", 0.05, 0.9, False), ("c", 0.9, 0.9, False)), 0.3, 0.5) is None


# ---- fake Jev: picks the option whose text shares the most words with the request -----------------------------
def words(t):
    return {w.strip(".,;:!?()\"'").lower() for w in t.split() if len(w) > 3}


calls = []


def fake_call(state, questions, timeout=60, deadline=None):
    calls.append(questions)
    req = words(state["request"] if isinstance(state, dict) else state)
    out = {}
    for k, q in questions.items():
        if q["type"] == "choice":
            sc = {o: len(req & words(d)) for o, d in q["criteria"].items() if o != "none"}
            best = max(sc, key=sc.get) if sc and max(sc.values()) > 0 else "none"
            out[k] = {"choice": best, "probabilities": {o: (0.9 if o == best else 0.1 / len(q["criteria"]))
                                                        for o in q["criteria"]}}
        else:
            text = q["instructions"]
            if "passages[" in text:
                key = text.split('passages["')[1].split('"')[0]
                text = state["passages"][key]
            out[k] = {"noul": 0.9 if len(req & words(text)) >= 2 else 0.1}
    return out


jev.call = fake_call

res = skills.rank("please reformat these raw meeting notes", skills=skills.router_candidates())
assert res["ranked"][0][0] == "tidy-notes" and skills.decide(res) == "tidy-notes", res
assert skills.decide(skills.rank("what is a monad", skills=skills.router_candidates())) is None

# ---- router: one line, once per session; slash commands and agent messages are skipped -----------------------
write(TMP / "config/config.toml", "[context]\nmode = \"off\"\n[skill_router]\nmode = \"on\"\n")
config.load(refresh=True)
inp = {"prompt": "please reformat these raw meeting notes", "session_id": "s1", "cwd": str(TMP)}
line = router.for_prompt(inp)
assert line == f"relevant skill: tidy-notes, read {names['tidy-notes']['path']}", line
assert router.for_prompt(inp) is None  # already suggested in this session
assert router.for_prompt(dict(inp, session_id="s2")) == line
assert router.for_prompt(dict(inp, prompt="/tidy-notes now")) is None
assert router.for_prompt(dict(inp, prompt="<task-notification>x</task-notification>")) is None
write(TMP / "config/config.toml", "[context]\nmode = \"off\"\n[skill_router]\nmode = \"log\"\n")
config.load(refresh=True)
assert router.for_prompt(dict(inp, session_id="s3")) is None  # logging only
logged = [json.loads(ln) for ln in open(config.STATE_DIR / "router.jsonl")]
assert logged[-1]["skill"]["suggest"] == "tidy-notes" and logged[-1]["skill"]["mode"] == "log"

# ---- docs and memory: sections, index, routing, never the same section twice -----------------------------------
md = "# Ops\n\nintro line\n\n## Restarts\n" + "restart the ingest worker with the drain flag first. " * 60 + \
     "\n\n```\n# not a heading\n```\n\n## Backups\n" + "nightly backup runs at two and keeps seven copies. " * 60 + "\n"
secs = ctxload.sections(md)
assert [s[2] for s in secs] == ["Restarts", "Backups"], [s[2] for s in secs]
assert ctxload.sections("---\ndescription: d\n---\n# Small\nshort body\n")[0][2] == "Small"

proj = TMP / "proj"
(proj / ".git").mkdir(parents=True)
write(proj / "OPS.md", md)
write(proj / "CLAUDE.md", "# loaded by Claude Code already\n")
mem = ctxload.memory_dir(str(proj))
write(mem / "MEMORY.md", "# Memory index\n## Rules\n- [Ledgers frozen](ledgers-frozen.md) — never regenerate ledgers\n")
write(mem / "ledgers-frozen.md", "---\nname: ledgers-frozen\ndescription: ledgers are frozen\n---\n"
      "The ledgers were certified once; never regenerate ledgers after a commit lands.\n")
(proj / "sub" / "dir").mkdir(parents=True)
root = ctxload.project_root(str(proj / "sub" / "dir"))
assert root == str(proj.resolve()), root
assert ctxload.index_project(root) == 2
assert ctxload.index_project(root) == 0  # unchanged files are not re-read
db = ctxload.connect_for(root)
labels = [r[0] for r in db.execute("select label from nodes where level='file'")]
assert any(lb.startswith("memory ledgers-frozen: Ledgers frozen — never regenerate ledgers") for lb in labels), labels
assert not any("CLAUDE.md" in lb for lb in labels)

r = ctxload.route(root, "should we regenerate the ledgers after this commit lands?")
assert r["status"] == "ok" and [s["path"].endswith("ledgers-frozen.md") for s in r["sections"]] == [True], r

write(TMP / "config/config.toml", "[context]\nmode = \"on\"\n[skill_router]\nmode = \"off\"\n")
config.load(refresh=True)
cinp = {"prompt": "should we regenerate the ledgers after this commit lands?", "session_id": "c1", "cwd": root}
text = router.for_prompt(cinp)
assert text and "tokenmaxxing context (memory/ledgers-frozen.md" in text and "never regenerate ledgers" in text, text
assert router.for_prompt(cinp) is None  # same section, same session: not again
text = router.for_prompt(dict(cinp, prompt="how do restarts of the ingest worker work with the drain flag?"))
assert text and "OPS.md lines 5-11 (OPS.md § Restarts); read it" in text, text  # long: a pointer

# log mode hands the work to a background job file and returns nothing on the prompt's path
write(TMP / "config/config.toml", "[context]\nmode = \"log\"\n[skill_router]\nmode = \"off\"\n")
config.load(refresh=True)
started = []
router._background = lambda args: started.append(args)
assert router.for_prompt(dict(cinp, session_id="c2")) is None
assert started and started[-1][:2] == ["skill-find", "--context-log"]
router.context_log(started[-1][2])
last = [json.loads(ln) for ln in open(config.STATE_DIR / "router.jsonl")][-1]
assert last["background"] and last["context"]["status"] == "ok" and last["context"]["sections"], last
# ---- one round: the file Choice and the section Nouls go out together; a failed Noul request loses only its own
write(TMP / "config/config.toml", "[context]\nmode = \"on\"\nmax_candidates = 2\nbatch_chars = 10\n"
      "[skill_router]\nmode = \"off\"\n")
config.load(refresh=True)
for i in range(4):
    write(mem / f"note-{i}.md", f"---\ndescription: note {i}\n---\nThe staging cluster {i} runs weekly load drills.\n")
assert ctxload.index_project(root) == 4
db = ctxload.connect_for(root)
ids = ctxload.candidates(db, root, "what about the ledgers after a commit?", "", 2)
assert len(ids) == 2 and db.execute("select path from nodes where id=?", (ids[0],)).fetchone()[0].endswith(
    "ledgers-frozen.md"), ids  # full-text ranked: the ledger note first
db.close()
calls.clear()
r = ctxload.route(root, "should we regenerate the ledgers after this commit lands?")
assert r["status"] == "ok" and r["requests"] == 3 and len(calls) == 3, (r, calls)  # choice + 2 Noul requests
assert [s["path"].endswith("ledgers-frozen.md") for s in r["sections"]] == [True], r
real = jev.call


def flaky(state, questions, **kw):
    if "passages" in state and any("ledgers" in t for t in state["passages"].values()):
        raise jev.JevError("firewall block", waf=True)
    return real(state, questions, **kw)


jev.call = flaky
r = ctxload.route(root, "should we regenerate the ledgers after this commit lands?")
assert r["status"] == "ok" and r["partial"] == 1 and r["sections"] == [], r
jev.call = real

# an index from before the full-text table is started over rather than read
import sqlite3  # noqa: E402

old_db = skills.data_dir() / "context.db"
con = sqlite3.connect(old_db)
con.execute("pragma user_version = 1")
con.commit()
con.close()
assert ctxload.route(root, "should we regenerate the ledgers?")["status"] == "not indexed"
assert ctxload.index_project(root) == 6
print("skills/router ok")
