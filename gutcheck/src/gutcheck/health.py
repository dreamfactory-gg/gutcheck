"""SessionStart: silent unless something needs the user. Starts init/refresh in the background."""
from gutcheck_core import health


def session_start(inp):
    done, notice = health.start(inp)
    if done:
        return notice
    from . import report
    return report.attention_line()
