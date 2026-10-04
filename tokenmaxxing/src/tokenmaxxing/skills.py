"""Skill index and router (Phase 6, items 4-5).

Index every installed skill: user (~/.claude/skills), library (~/.claude/library, never loaded by Claude Code),
enabled plugins (from installed_plugins.json) and synced claude.ai skills (~/.claude/skills/synced/<bucket>).
Stored as skills.json in the plugin data dir, as a two-level tree: source -> skill.

Routing asks Jev one Choice per tree level (a flat Choice when the roster is small), plus one Noul per
candidate ("does this skill do what the request asks?"). A skill is suggested only when both clear a threshold.

`skillOverrides` (verified against Claude Code): "user-invocable-only" hides a skill from the model and keeps
its /slash-command; it applies to user and synced claude.ai skills, not to plugin skills. So `managed` is true
for user, library and synced skills; plugin skills are indexed and routed but stay listed.
"""
import json
import os
import re
import time
from pathlib import Path

from gutcheck_core import config

HOME = Path.home()
CLAUDE_DIR = Path(os.environ.get("GUTCHECK_CLAUDE_DIR", HOME / ".claude"))
SELF_PLUGIN = "tokenmaxxing"       # tokenmaxxing's own skill is always loaded; never managed or routed
MAX_FLAT = 40                      # above this, pick a source first, then a skill (one Choice per level)
DESC_CHARS = 1200                   # whole descriptions: trigger clauses often sit at the end

DEFAULTS = {"mode": "on", "min_prob": 0.3, "min_fits": 0.5, "deadline_s": 1.8}


def settings():
    return {**DEFAULTS, **(config.load().get("skill_router") or {})}


# ---- where plugin data lives --------------------------------------------------------------------------
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def _ours(env_dir):
    """CLAUDE_PLUGIN_DATA can leak from another plugin's environment; accept only tokenmaxxing's own."""
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    try:
        if root and Path(root).resolve() == PLUGIN_ROOT:
            return True
    except OSError:
        pass
    return Path(env_dir).name.startswith(SELF_PLUGIN)


def data_dir():
    """tokenmaxxing's ${CLAUDE_PLUGIN_DATA} when set (hooks), remembered for the CLI; else the state dir."""
    if config.DATA_DIR_OVERRIDE:
        p = Path(config.DATA_DIR_OVERRIDE)
        p.mkdir(parents=True, exist_ok=True)
        return p
    ptr = config.STATE_DIR / "plugin_data_dir"
    env = os.environ.get("CLAUDE_PLUGIN_DATA")
    if env and not _ours(env):
        env = None
    if not env and not ptr.exists():
        found = sorted((CLAUDE_DIR / "plugins" / "data").glob(f"{SELF_PLUGIN}-*"))
        env = str(found[0]) if len(found) == 1 else None
    if env:
        p = Path(env)
        try:
            p.mkdir(parents=True, exist_ok=True)
            if not ptr.exists() or ptr.read_text().strip() != str(p):
                config.STATE_DIR.mkdir(parents=True, exist_ok=True)
                ptr.write_text(str(p))
            return p
        except OSError:
            pass
    try:
        p = Path(ptr.read_text().strip())
        if p.is_dir():
            return p
    except OSError:
        pass
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    return config.STATE_DIR


def locate(name):
    """An index file: tokenmaxxing's data dir first, then any other tokenmaxxing data dir (a plugin loaded another way
    gets its own), then the state dir. Writers always use data_dir()."""
    first = data_dir()
    if config.DATA_DIR_OVERRIDE:
        return first / name
    cands = [first] + sorted((CLAUDE_DIR / "plugins" / "data").glob(f"{SELF_PLUGIN}-*")) + [config.STATE_DIR]
    for d in cands:
        if (Path(d) / name).exists():
            return Path(d) / name
    return first / name


def index_path():
    return locate("skills.json")


# ---- frontmatter --------------------------------------------------------------------------------------
def frontmatter(text):
    """Minimal YAML frontmatter reader: `key: value`, quoted values, and folded/literal blocks (> |)."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out, key, block = {}, None, None
    for line in text[3:end].splitlines():
        if block is not None:
            if line.startswith((" ", "\t")) or not line.strip():
                block.append(line.strip())
                continue
            out[key] = " ".join(x for x in block if x)
            block = None
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            block = []
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
    if block is not None:
        out[key] = " ".join(x for x in block if x)
    return out


def _truthy(v):
    return str(v).strip().lower() in ("true", "yes", "1")


def _entry(path, name, source, group, managed, kind="skill"):
    try:
        fm = frontmatter(Path(path).read_text(encoding="utf-8", errors="replace")[:20000])
    except OSError:
        return None
    return {"name": name, "description": (fm.get("description") or "").strip(), "path": str(path),
            "source": source, "group": group, "kind": kind, "managed": managed,
            "model_invocable": not _truthy(fm.get("disable-model-invocation", "")),
            "user_invocable": fm.get("user-invocable", "true").strip().lower() != "false"}


# ---- discovery ------------------------------------------------------------------------------------------
def _skill_dirs(root):
    root = Path(root)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/SKILL.md") if not p.parent.name.startswith("."))


def _enabled_plugins():
    """[(plugin name, key, install path)] for installed plugins not switched off in settings."""
    try:
        inst = json.loads((CLAUDE_DIR / "plugins" / "installed_plugins.json").read_text())
    except (OSError, ValueError):
        return []
    enabled = {}
    for f in ("settings.json", "settings.local.json"):
        try:
            enabled.update(json.loads((CLAUDE_DIR / f).read_text()).get("enabledPlugins") or {})
        except (OSError, ValueError):
            pass
    out = []
    for key, recs in (inst.get("plugins") or {}).items():
        if enabled.get(key) is False:
            continue
        for r in recs if isinstance(recs, list) else [recs]:
            if r.get("scope", "user") not in ("user", "managed"):
                continue  # project/local installs belong to one project, not the user's roster
            path = Path(r.get("installPath") or "")
            if path.is_dir():
                out.append((key.split("@")[0], key, path))
                break
    return out


def discover():
    skills = []
    user = CLAUDE_DIR / "skills"
    for p in _skill_dirs(user):
        if p.parent.name == "synced":
            continue
        fm_name = frontmatter(p.read_text(errors="replace")[:4000]).get("name") or p.parent.name
        e = _entry(p, fm_name, "user", "user skills", managed=True)
        if e:
            skills.append(e)
    for p in _skill_dirs(CLAUDE_DIR / "library"):
        e = _entry(p, p.parent.name, "library", "library (not loaded)", managed=True)
        if e:
            skills.append(e)
    synced = user / "synced"
    if synced.is_dir():
        for bucket in sorted(d for d in synced.iterdir() if d.is_dir() and not d.name.startswith(".")):
            for p in _skill_dirs(bucket):
                e = _entry(p, f"anthropic-skills:{p.parent.name}", "synced", "claude.ai skills", managed=True)
                if e:
                    skills.append(e)
    for name, key, root in _enabled_plugins():
        if name == SELF_PLUGIN:
            continue
        for p in _skill_dirs(root / "skills"):
            e = _entry(p, f"{name}:{p.parent.name}", "plugin", f"plugin {name}", managed=False)
            if e:
                e["plugin"] = key
                skills.append(e)
        cmds = root / "commands"
        if cmds.is_dir():
            for p in sorted(cmds.glob("*.md")):
                e = _entry(p, f"{name}:{p.stem}", "plugin", f"plugin {name}", managed=False, kind="command")
                if e:
                    e["plugin"] = key
                    skills.append(e)
    seen, out = set(), []
    for s in skills:
        if s["name"] not in seen:
            seen.add(s["name"])
            out.append(s)
    return out


def build(quiet=True):
    """Re-index installed skills. Returns (skills, newly seen names)."""
    path = data_dir() / "skills.json"
    old = load_index()
    skills = discover()
    known = {s["name"] for s in old.get("skills", [])}
    new = [s["name"] for s in skills if s["name"] not in known]
    first_seen = old.get("first_seen", {})
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for s in skills:
        first_seen.setdefault(s["name"], now)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"built": now, "skills": skills, "first_seen": first_seen}, indent=1))
    tmp.replace(path)
    if not quiet:
        print(f"skills: {len(skills)} indexed ({sum(s['managed'] for s in skills)} manageable), "
              f"{len(new)} new -> {path}")
    return skills, new


def load_index():
    try:
        return json.loads(index_path().read_text())
    except (OSError, ValueError):
        return {}


def routable(skills, commands=True):
    """Skills the model may load. Plugin commands are slash commands typed by the user; the prompt-time router
    leaves them out (commands=False), skill-find keeps them."""
    return [s for s in skills if s["model_invocable"] and s["description"] and (commands or s["kind"] != "command")]


def router_candidates(idx=None):
    """What the prompt-time router chooses from (`route_plugins = false` limits it to managed skills)."""
    idx = load_index().get("skills", []) if idx is None else idx
    cands = routable(idx, commands=False)
    return cands if settings().get("route_plugins", True) else [s for s in cands if s["managed"]]


def recommended_overrides(skills):
    """skillOverrides entries that hide managed skills from the model and keep their /slash-command."""
    return {s["name"]: "user-invocable-only" for s in skills
            if s["managed"] and s["source"] in ("user", "synced") and s["model_invocable"]}


# ---- routing --------------------------------------------------------------------------------------------
NONE = "none"
NONE_DESC = ("No listed skill fits: the request is ordinary conversation, coding, debugging or questions that "
             "none of these skills is specifically for.")
WHICH = ("Which of these skills, if any, is the right one to load to help with the user's latest request? "
         "Choose none unless a skill is clearly for this kind of request.")


def _fits_q(s):
    return {"type": "noul", "instructions":
            f"Does the skill '{s['name']}' do the specific thing the user's latest request asks for? "
            f"It is described as: {s['description'][:DESC_CHARS]}"}


def _state(prompt, context):
    st = {"request": prompt[:3000]}
    if context:
        st["recent_context"] = context[-1200:]
    return st


def _choose_groups(prompt, context, skills, deadline):
    from gutcheck_core import jev
    groups = {}
    for s in skills:
        groups.setdefault(s["group"], []).append(s)
    crit = {f"g{i}": f"{g}: " + "; ".join(f"{s['name']} ({s['description'][:80]})" for s in ss)[:1500]
            for i, (g, ss) in enumerate(groups.items())}
    crit[NONE] = NONE_DESC
    ans = jev.call(_state(prompt, context), {"which": {"type": "choice", "instructions":
                                                       "Which group of skills, if any, has the right skill for "
                                                       "the user's latest request?", "criteria": crit}},
                   deadline=deadline)["which"]
    probs = sorted(((p, k) for k, p in ans["probabilities"].items() if k != NONE), reverse=True)
    names = list(groups)
    keep, total = [], 0.0
    for p, k in probs[:3]:
        if p < 0.05:
            break
        keep += groups[names[int(k[1:])]]
        total += p
        if total >= 0.9:
            break
    return keep


HOST_RE = re.compile(r"\b((?:[a-z0-9-]+\.)+(?:ai|com|io|dev|org|net|app|so|co))(?=[/\s)\]'\"`,;:…]|$)")
EXT_RE = re.compile(r"(?<![\w/])\.([a-z][a-z0-9]{1,4})\b")


def literals(text):
    """URL hosts and dotted file extensions named in a text (a description's literal triggers)."""
    low = text.lower()
    return set(HOST_RE.findall(low)) | {"." + e for e in EXT_RE.findall(low)}


def prompt_literals(prompt):
    low = prompt.lower()
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", low)) | set(HOST_RE.findall(low))
    exts = {"." + e for e in re.findall(r"\w\.([a-z][a-z0-9]{1,4})\b", low)} | {"." + e for e in EXT_RE.findall(low)}
    return hosts | exts


def distinctive_literals(skills):
    """{name: literals only that skill's description names}: a description listing a URL host or file type as
    its trigger is a direct hint; one named by several skills (often as an exclusion) is not."""
    lits = {s["name"]: literals(s["description"]) for s in skills}
    count = {}
    for ls in lits.values():
        for x in ls:
            count[x] = count.get(x, 0) + 1
    return {n: {x for x in ls if count[x] == 1} for n, ls in lits.items()}


def rank(prompt, context="", skills=None, deadline=None):
    """One Jev request (two for large rosters).
    Returns {"ranked": [(name, choice prob, fits noul, literal hit)], "none": p, "ms": ...}."""
    from gutcheck_core import jev
    t0 = time.monotonic()
    if skills is None:
        skills = routable(load_index().get("skills", []))
    if not skills:
        return {"ranked": [], "none": 1.0, "ms": 0}
    lits = distinctive_literals(skills)
    plits = prompt_literals(prompt)
    if len(skills) > MAX_FLAT:
        skills = _choose_groups(prompt, context, skills, deadline)
        if deadline is not None:
            deadline = max(0.1, deadline - (time.monotonic() - t0))
        if not skills:
            return {"ranked": [], "none": 1.0, "ms": int((time.monotonic() - t0) * 1000)}
    crit = {s["name"]: s["description"][:DESC_CHARS] for s in skills}
    crit[NONE] = NONE_DESC
    qs = {"which": {"type": "choice", "instructions": WHICH, "criteria": crit}}
    for i, s in enumerate(skills):
        qs[f"fits{i}"] = _fits_q(s)
    ans = jev.call(_state(prompt, context), qs, deadline=deadline)
    probs = ans["which"]["probabilities"]
    ranked = sorted(((s["name"], probs.get(s["name"], 0.0), ans[f"fits{i}"]["noul"],
                      bool(lits.get(s["name"], set()) & plits))
                     for i, s in enumerate(skills)), key=lambda r: (-r[1], -r[2]))
    return {"ranked": ranked, "none": probs.get(NONE, 0.0), "ms": int((time.monotonic() - t0) * 1000)}


def decide(result, min_prob=None, min_fits=None, top=2):
    """The suggested skill, or None. Candidates: the top `top` Choice options with probability >= min_prob.
    The Choice says which skill, the Noul says whether: a candidate needs fits >= min_fits, or a literal
    trigger from its description (a URL host or file type only it names) in the prompt. Best fits wins."""
    st = settings()
    min_prob = st["min_prob"] if min_prob is None else min_prob
    min_fits = st["min_fits"] if min_fits is None else min_fits
    ok = [r for r in result["ranked"][:top] if r[1] >= min_prob and (r[2] >= min_fits or (len(r) > 3 and r[3]))]
    if not ok:
        return None
    return max(ok, key=lambda r: (r[2] >= min_fits, r[2], r[1]))[0]


def by_name():
    return {s["name"]: s for s in load_index().get("skills", [])}
