"""`tokenmaxxing skill-find "TASK"`: the SKILL.md to read for a task, from every installed skill (Jev over the index).

  tokenmaxxing skill-find "TASK" [--all] [--json] [--min P]   best skill path(s), confidence-gated
  tokenmaxxing skill-find --list                             the index (name, source, managed, path)
  tokenmaxxing skill-find --reindex                          rebuild the skill index now
  tokenmaxxing skill-find --overrides                        the skillOverrides block that hides managed skills
  tokenmaxxing skill-find --index-context [ROOT]             (re)index one project's docs and memory
"""
import json
import sys

from . import skills


def main(args):
    args = list(args)
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if args[0] == "--reindex":
        skills.build(quiet=False)
        return
    if args[0] == "--context-log":
        from . import router
        router.context_log(args[1])
        return
    if args[0] == "--index-context":
        import os

        from . import ctxload
        root = ctxload.project_root(args[1] if len(args) > 1 else os.getcwd())
        n = ctxload.index_project(root)
        print(f"context: {n} files (re)indexed for {root}")
        return
    idx = skills.load_index().get("skills")
    if idx is None:
        idx, _ = skills.build()
    if args[0] == "--list":
        for s in idx:
            flags = ("managed" if s["managed"] else "listed") + ("" if s["model_invocable"] else ", user-only")
            print(f"{s['name']:40} {s['source']:8} {flags:22} {s['path']}")
        return
    if args[0] == "--overrides":
        print(json.dumps({"skillOverrides": skills.recommended_overrides(idx)}, indent=2))
        return
    show_all = "--all" in args
    as_json = "--json" in args
    st = skills.settings()
    min_p = st["min_prob"]
    if "--min" in args:
        i = args.index("--min")
        min_p = float(args[i + 1])
        del args[i:i + 2]
    task = " ".join(a for a in args if a not in ("--all", "--json"))
    res = skills.rank(task, skills=skills.routable(idx, commands=show_all), deadline=15)
    paths = {s["name"]: s["path"] for s in idx}
    best = skills.decide(res, min_p, st["min_fits"], top=3)
    hits = [(n, p, f) for n, p, f, lit in res["ranked"]
            if show_all or n == best or (p >= min_p and f >= st["min_fits"])]
    if as_json:
        print(json.dumps({"none": res["none"], "skills": [{"name": n, "p": p, "fits": f, "path": paths[n]}
                                                          for n, p, f in hits]}, indent=1))
        return
    if not hits:
        top = res["ranked"][0] if res["ranked"] else None
        print("skill-find: no skill fits" + (f" (closest: {top[0]} p={top[1]:.2f} fits={top[2]:.2f})"
                                             if top else ""))
        return
    for n, p, f in hits[:3 if not show_all else None]:
        print(f"{p:.2f}  {n}  {paths[n]}")
