"""Secret store behind one small interface: macOS Keychain, Linux secret-tool, or env vars.

Values are returned to callers in-process only; nothing here prints a secret.
"""
import os
import shutil
import subprocess
import sys


def _env_name(service):
    return "GUTCHECK_SECRET_" + "".join(c if c.isalnum() else "_" for c in service).upper()


def get(service):
    """Return the secret for `service`, or None. Env var GUTCHECK_SECRET_<SERVICE> wins."""
    v = os.environ.get(_env_name(service))
    if v:
        return v
    if sys.platform == "darwin" and shutil.which("security"):
        r = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                           capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    if shutil.which("secret-tool"):
        r = subprocess.run(["secret-tool", "lookup", "service", service],
                           capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    return None


def store_hint(service):
    """How the user should store a secret for `service` (no value involved)."""
    if sys.platform == "darwin":
        return f"security add-generic-password -a \"$USER\" -s {service} -w   (prompts for the value)"
    if shutil.which("secret-tool"):
        return f"secret-tool store --label={service} service {service}   (prompts for the value)"
    return f"export {_env_name(service)}=...   (in your shell profile; no system keyring found)"
