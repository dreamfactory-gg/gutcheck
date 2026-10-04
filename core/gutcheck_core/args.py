"""Argument helpers for the plugins' command lines."""
import sys


def _read(src):
    return sys.stdin.read() if src in (None, "-") else open(src, encoding="utf-8", errors="replace").read()


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        v = args[i + 1]
        del args[i:i + 2]
        return v
    return default


def _flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False
