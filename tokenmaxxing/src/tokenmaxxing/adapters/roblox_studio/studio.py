"""Roblox Studio pack (experimental; off unless `[studio] mirror = true`): a local mirror of every script in the open place, so reading, grepping and outlining
happen locally (no MCP round trips, no script text in Claude's context until it asks for a range). Edits
still go through the Studio MCP (`multi_edit`); `tokenmaxxing studio sync` refreshes changed scripts.

  tokenmaxxing studio list                          connected Studio instances
  tokenmaxxing studio sync [--studio ID]            hash every script, export the changed ones; prints the mirror dir
  tokenmaxxing studio outline FILE                  signatures + line ranges of a mirrored script
  tokenmaxxing studio path FILE                     dotted target_file for script_read / multi_edit
  tokenmaxxing studio check FILE                    is the mirror copy still identical to Studio? (before editing)
"""
import json
import os
import re
import sys
import time
from pathlib import Path

from ...mcpclient import MCP

STUDIO_MCP = os.environ.get("GUTCHECK_STUDIO_MCP", "/Applications/RobloxStudio.app/Contents/MacOS/StudioMCP")
CACHE = Path(os.environ.get("GUTCHECK_STUDIO_CACHE", Path.home() / ".cache" / "gutcheck" / "studio"))
SLICE = 40_000  # source chars per execute_luau reply (JSON escaping stays under the 100k output cap)
SUFFIX = {"Script": ".server.luau", "LocalScript": ".client.luau", "ModuleScript": ".luau"}

# FNV-1a 32-bit, computed identically in Luau and Python. prime = 2^24 + 403, split to stay exact in doubles.
LUAU_HASH = """
local function fnv(s)
  local x = 2166136261
  for i = 1, #s do
    x = bit32.bxor(x, string.byte(s, i))
    x = (x * 403 + bit32.lshift(x, 24)) % 4294967296
  end
  return x
end
"""

LUAU_LIST = LUAU_HASH + """
local HttpService = game:GetService("HttpService")
local out = {}
local function segs(inst)
  local parts = {}
  local cur = inst
  while cur and cur ~= game do
    table.insert(parts, 1, cur.Name)
    cur = cur.Parent
  end
  return parts
end
for _, d in ipairs(game:GetDescendants()) do
  if d:IsA("LuaSourceContainer") then
    local ok, src = pcall(function() return d.Source end)
    if ok then
      table.insert(out, {p = segs(d), c = d.ClassName, h = fnv(src), n = #src})
    end
  end
end
return HttpService:JSONEncode({place = game.Name, placeId = game.PlaceId, scripts = out})
"""

LUAU_SLICE = """
local HttpService = game:GetService("HttpService")
local cur = game
for _, name in ipairs(HttpService:JSONDecode([==[%s]==])) do cur = cur and cur:FindFirstChild(name) end
if not (cur and cur:IsA("LuaSourceContainer")) then return "null" end
return HttpService:JSONEncode(string.sub(cur.Source, %d, %d))
"""

LUAU_FETCH = """
local HttpService = game:GetService("HttpService")
local want = HttpService:JSONDecode([==[%s]==])
local res = {}
for i, parts in ipairs(want) do
  local cur = game
  for _, name in ipairs(parts) do
    cur = cur and cur:FindFirstChild(name)
  end
  res[i] = cur and cur:IsA("LuaSourceContainer") and cur.Source or false
end
return HttpService:JSONEncode(res)
"""


def fnv(s):
    x = 2166136261
    for b in s.encode("utf-8"):
        x ^= b
        x = (x * 403 + ((x << 24) & 0xFFFFFFFF)) % 4294967296
    return x


def _safe(name):
    return re.sub(r"[/\\:\0]", "_", name) or "_"


class Studio:
    def __init__(self, studio_id=None, timeout=120):
        self.m = MCP([STUDIO_MCP], timeout=timeout)
        self.sid = studio_id
        for _ in range(20):
            txt, _ = self.m.call("list_roblox_studios", {})
            ids = [s["id"] for s in json.loads(txt).get("studios", [])]
            if ids:
                break
            time.sleep(2)
        if not ids:
            raise RuntimeError("no Roblox Studio instance is connected to the MCP")
        if self.sid is None:
            if len(ids) > 1:
                raise RuntimeError(f"several Studio instances are connected ({', '.join(ids)}); pass --studio ID")
            self.sid = ids[0]
        elif self.sid not in ids:
            raise RuntimeError(f"Studio {self.sid} is not connected (connected: {', '.join(ids)})")

    def luau(self, code):
        txt, _ = self.m.call("execute_luau", {"studio_id": self.sid, "code": code, "datamodel_type": "Edit"})
        return txt

    def close(self):
        self.m.close()


def _json_from(txt):
    dec = json.JSONDecoder()
    for i, ch in enumerate(txt):
        if ch in "[{":
            try:
                return dec.raw_decode(txt[i:])[0]
            except ValueError:
                continue
    raise ValueError(f"no JSON in Studio output: {txt[:200]}")


def mirror_dir(place, place_id):
    return CACHE / f"{_safe(place)}-{place_id}"


def rel_path(parts, cls):
    return Path(*[_safe(p) for p in parts[:-1]]) / (_safe(parts[-1]) + SUFFIX.get(cls, ".luau"))


def sync(studio_id=None, quiet=False, batch_chars=150_000):
    t0 = time.time()
    st = Studio(studio_id)
    try:
        info = _json_from(st.luau(LUAU_LIST))
        root = mirror_dir(info["place"], info["placeId"])
        root.mkdir(parents=True, exist_ok=True)
        man_path = root / ".manifest.json"
        old = json.loads(man_path.read_text()) if man_path.exists() else {"files": {}}
        files, todo = {}, []
        for s in info["scripts"]:
            rp = str(rel_path(s["p"], s["c"]))
            files[rp] = {"parts": s["p"], "class": s["c"], "hash": s["h"], "chars": s["n"]}
            prev = old["files"].get(rp)
            if not prev or prev["hash"] != s["h"] or not (root / rp).exists():
                todo.append(rp)
        # fetch changed sources: small scripts in batches, big ones in slices (execute_luau output caps at 100k)
        fetched, failed = 0, []

        def save(rp, src):
            nonlocal fetched
            if src is None or fnv(src) != files[rp]["hash"]:
                failed.append(rp)  # changed mid-sync or truncated: never save content we can't verify
                return
            p = root / rp
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src)
            fetched += 1

        small = [rp for rp in todo if files[rp]["chars"] <= SLICE]
        big = [rp for rp in todo if files[rp]["chars"] > SLICE]
        batch, size = [], 0
        for rp in small + [None]:
            if rp is None or (batch and size + files[rp]["chars"] > SLICE):
                if batch:
                    srcs = _json_from(st.luau(LUAU_FETCH % json.dumps([files[x]["parts"] for x in batch])))
                    if len(srcs) != len(batch):
                        failed += batch
                    else:
                        for x, src in zip(batch, srcs):
                            save(x, src if isinstance(src, str) else None)
                batch, size = [], 0
            if rp is not None:
                batch.append(rp)
                size += files[rp]["chars"]
        for rp in big:
            parts, n = [], files[rp]["chars"]
            for a in range(1, n + 1, SLICE):
                txt = st.luau(LUAU_SLICE % (json.dumps(files[rp]["parts"]), a, min(n, a + SLICE - 1)))
                piece = json.loads(txt) if txt.strip() != "null" else None
                if piece is None:
                    break
                parts.append(piece)
            save(rp, "".join(parts) if parts else None)
        removed = [rp for rp in old["files"] if rp not in files]
        for rp in removed:
            try:
                (root / rp).unlink()
            except OSError:
                pass
        for rp in failed:
            files[rp]["hash"] = None
        man_path.write_text(json.dumps({"place": info["place"], "placeId": info["placeId"], "studio": st.sid,
                                        "synced": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files}, indent=1))
        if not quiet:
            print(f"{root}\n  {len(files)} scripts, {fetched} exported, {len(files) - len(todo)} unchanged, "
                  f"{len(removed)} removed ({time.time() - t0:.1f}s)" +
                  (f"\n  NOT exported (changed during sync or unreadable; run sync again): {failed}" if failed else ""))
        for rp in failed:
            files[rp]["hash"] = None  # forces a re-fetch next sync
        return root, {"scripts": len(files), "exported": fetched, "removed": len(removed)}
    finally:
        st.close()


def _manifest_for(path):
    p = Path(path).resolve()
    for d in [p] + list(p.parents):
        m = d / ".manifest.json"
        if m.exists():
            return d, json.loads(m.read_text())
    raise RuntimeError(f"{path} is not inside a Studio mirror")


def target(path):
    root, man = _manifest_for(path)
    rp = str(Path(path).resolve().relative_to(root))
    f = man["files"].get(rp)
    if not f:
        raise RuntimeError(f"{rp} is not in the mirror manifest")
    return "game." + ".".join(f["parts"])


def check(path):
    """True if Studio's current source still matches the mirror copy (safe to edit from it)."""
    root, man = _manifest_for(path)
    rp = str(Path(path).resolve().relative_to(root))
    f = man["files"][rp]
    st = Studio(man.get("studio"))
    try:
        srcs = _json_from(st.luau(LUAU_FETCH % json.dumps([f["parts"]])))
    finally:
        st.close()
    local = Path(path).read_text()
    return srcs[0] is not False and fnv(srcs[0]) == fnv(local)


def main(args):
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__doc__)
    cmd, rest = args[0], args[1:]
    sid = None
    if "--studio" in rest:
        i = rest.index("--studio")
        sid = rest[i + 1]
        del rest[i:i + 2]
    if cmd == "list":
        m = MCP([STUDIO_MCP])
        try:
            for _ in range(10):
                txt, _ = m.call("list_roblox_studios", {})
                if json.loads(txt).get("studios"):
                    break
                time.sleep(2)
            print(txt)
        finally:
            m.close()
    elif cmd == "sync":
        sync(sid)
    elif cmd == "outline":
        from . import luau_outline
        src = Path(rest[0]).read_text().splitlines()
        print(luau_outline.render(src, None, rest[0]))
    elif cmd == "path":
        print(target(rest[0]))
    elif cmd == "check":
        ok = check(rest[0])
        print("in sync" if ok else "STALE: Studio's copy changed since the last sync; run `tokenmaxxing studio sync` and "
                                   "re-read before editing")
        sys.exit(0 if ok else 1)
    else:
        sys.exit(__doc__)
