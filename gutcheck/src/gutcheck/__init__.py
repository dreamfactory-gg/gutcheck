"""gutcheck: guards against rogue agent actions (Bash risk check, key guards, secret files, don't-delete, pre-push)."""
from gutcheck_core import config

config.configure("gutcheck", {
    "bash_check": {
        "mode": "log",            # "log" = logging only; "on" = emit asks
        "deadline_s": 3.0,
        "thresholds": {"stop_target": 0.5, "remote_write": 0.5, "deployish": 0.5, "script_lines": 0.5},
        "allow_prefixes": [],      # mined read-only command heads
        "approved_patterns": [],   # auto-learned "don't ask again"
        "ask_patterns": [],        # learned misses
    },
    "sensitivity": {"hosts": {}, "branches": {"main": "default branch", "master": "default branch"}, "projects": {}},
    "aliases": {},
    "keyblock": {"mode": "on"},
    "key_guard": {"mode": "on"},     # literal keys in tool inputs are denied, keys in Bash output are redacted
    "delete_guard": {"mode": "on"},  # deleting what the owner said not to delete asks first
})
