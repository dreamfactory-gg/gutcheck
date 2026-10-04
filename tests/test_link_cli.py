"""SessionStart's ~/.local/bin/<cli> link: created when absent, repointed when it is ours to an older version, foreign
links untouched. Run: ~/.local/share/gutcheck/venv/bin/python tests/test_link_cli.py"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
os.environ.pop("CLAUDE_PLUGIN_DATA", None)
from gutcheck_core import config, health  # noqa: E402

config.HOME = Path(tempfile.mkdtemp())
config.configure("gutcheck", {})
bin_dir = config.HOME / ".local" / "bin"
bin_dir.mkdir(parents=True)
link = bin_dir / "gutcheck"
cache = config.HOME / "cache" / "mkt" / "gutcheck"
old, new = str(cache / "0.1" / "bin" / "gutcheck"), str(cache / "0.2" / "bin" / "gutcheck")
os.environ["CLAUDE_PLUGIN_ROOT"] = str(cache / "0.2")

health.link_cli()
assert os.readlink(link) == new
for foreign in ("/opt/elsewhere/gutcheck", str(config.HOME / "cache" / "other" / "gutcheck" / "0.1" / "bin" / "gutcheck"),
                str(cache / "0.1" / "bin" / "tokenmaxxing")):
    link.unlink()
    link.symlink_to(foreign)
    health.link_cli()
    assert os.readlink(link) == foreign, foreign
link.unlink()
link.symlink_to(old)
health.link_cli()
assert os.readlink(link) == new and sorted(os.listdir(bin_dir)) == ["gutcheck"]
print("ok")
