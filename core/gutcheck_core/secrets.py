"""Secret store behind one small interface: macOS Keychain, Windows Credential Manager, Linux secret-tool, or env vars.

Values are returned to callers in-process only; nothing here prints a secret.
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


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
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Credential(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                        ("CredentialBlob", ctypes.POINTER(wintypes.BYTE)), ("Persist", wintypes.DWORD),
                        ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

        advapi32 = ctypes.WinDLL("advapi32")
        pcred = ctypes.POINTER(Credential)()
        advapi32.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(type(pcred))]
        advapi32.CredReadW.restype = wintypes.BOOL
        advapi32.CredFree.argtypes = [ctypes.c_void_p]
        advapi32.CredFree.restype = None
        if not advapi32.CredReadW(service, 1, 0, ctypes.byref(pcred)):  # CRED_TYPE_GENERIC
            return None
        try:
            cred = pcred.contents
            return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16le")
        finally:
            advapi32.CredFree(pcred)
    if shutil.which("secret-tool"):
        r = subprocess.run(["secret-tool", "lookup", "service", service],
                           capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    return None


def store_hint(service):
    """How the user should store a secret for `service` (no value involved)."""
    if sys.platform == "darwin":
        return f"security add-generic-password -a \"$USER\" -s {service} -w   (prompts for the value)"
    if sys.platform == "win32":
        return f"MSYS_NO_PATHCONV=1 cmdkey /generic:{service} /user:{shlex.quote(os.environ['USERNAME'])} /pass"
    if shutil.which("secret-tool"):
        return f"secret-tool store --label={service} service {service}   (prompts for the value)"
    return f"export {_env_name(service)}=...   (in your shell profile; no system keyring found)"


def withsecret(service, env, argv):
    v = get(service)
    if not v:
        sys.exit(f"withsecret: no secret stored for '{service}'. Store it with: {store_hint(service)}")
    if sys.platform == "win32":
        # through Git Bash's sh: CreateProcess can't run shebang scripts or our extensionless wrappers. Absolute PATH
        # entries only: which() and CreateProcess both try the cwd first, where a project's sh.exe would get the secret.
        # sh.exe first: an extensionless sh earlier on PATH is a script CreateProcess can't run.
        dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if Path(d).is_absolute()]
        sh = next((p for n in ("sh.exe", "sh") for d in dirs if os.path.isfile(p := os.path.join(d, n))), None)
        if not sh:
            sys.exit("withsecret: Git Bash's sh is not on PATH")
        # MSYS2_ENV_CONV_EXCL: MSYS converts values that look like paths; a secret must reach the child as stored
        excl = ";".join(filter(None, (os.environ.get("MSYS2_ENV_CONV_EXCL"), env)))
        sys.exit(subprocess.run([sh, "-c", 'exec "$@"', "sh", *argv],
                                env={**os.environ, env: v, "MSYS2_ENV_CONV_EXCL": excl}).returncode)
    os.environ[env] = v
    os.execvp(argv[0], argv)
