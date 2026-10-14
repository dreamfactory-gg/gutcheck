import sys
from contextlib import contextmanager


@contextmanager
def locked(fh, exclusive=True, block=True):
    if sys.platform == "win32":
        import msvcrt
        mode = (msvcrt.LK_LOCK if block else msvcrt.LK_NBLCK) if exclusive else \
            (msvcrt.LK_RLCK if block else msvcrt.LK_NBRLCK)
        fh.seek(0)
        msvcrt.locking(fh.fileno(), mode, 1)
        try:
            yield
        finally:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fh, mode | (0 if block else fcntl.LOCK_NB))
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
