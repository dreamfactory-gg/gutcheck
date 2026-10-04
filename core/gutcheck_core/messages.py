"""Owner messages in a transcript: who wrote a message, its text blocks, and its units (the pieces a standing rule
is kept as). Shared by the standing-rules store (rules.py) and tokenmaxxing's checkpoint."""
import re

CHARS_PER_TOKEN = 3.5        # conservative for English prose mixed with paths
_NOT_OWNER = ("Another Claude session", "<bash-input>", "<bash-stdout>", "<bash-stderr>", "<local-command",
              "<command-", "<system-reminder", "Caveat:", "<task-notification", "[Request interrupted",
              "This session is being continued from a previous conversation", "<user-prompt-submit-hook",
              "<artifact-comment", "<cross-session", "<agent-message")


def est_tokens(text):
    return int(len(text) / CHARS_PER_TOKEN) + 1


def _texts(content):
    if isinstance(content, str):
        return [content]
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return out


def owner_source(text):
    """'owner' for a message the owner typed, 'lead' for the team lead's message to a teammate, else None."""
    t = text.lstrip()
    if not t:
        return None
    if t.startswith('<teammate-message teammate_id="team-lead"'):
        return "lead"
    if t.startswith(_NOT_OWNER) or t.startswith("<teammate-message"):
        return None
    return "owner"


def _clean(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r'\s*<pasted_content id="[^"]*">\s*(.*?)\s*</pasted_content id="[^"]*">\s*',
                  lambda m: f" [pasted: {m.group(1)}] " if len(m.group(1)) <= 300 else
                  f" [pasted text, {len(m.group(1))} chars] ", text, flags=re.S)   # long pastes are logs or replies
    text = re.sub(r"</?teammate-message[^>]*>", "", text)
    return text.strip()


_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[*`])")


def units(msg_text, long_chars=500):
    """A short message is one unit; a long one splits into paragraphs, and long paragraphs into sentences
    or list items, so the kept rule is verbatim but not the whole brief around it."""
    t = msg_text.strip()
    if re.fullmatch(r"/[\w:-]+(\s.*)?", t, flags=re.S) and len(t) < 200:
        return []  # a slash command, not an instruction
    if len(t) <= long_chars:
        return [t] if len(t) >= 6 else []
    out = []
    for para in re.split(r"\n\s*\n", t):
        para = para.strip()
        if not para:
            continue
        if len(para) <= long_chars and para.count("\n") < 4:
            out.append(para)
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) <= long_chars:
                out.append(line)
            else:
                out.extend(x.strip() for x in _SENT_RE.split(line) if x.strip())
    return [u for u in out if len(u) >= 6 and not re.fullmatch(r"[#\-*`=|_ ]+", u)]


_HEUR = re.compile(r"(?i)\b(always|never|don'?t|do not|must|only|from now on|going forward|make sure|keep|"
                   r"avoid|prefer|no need|stop|ask (?:me )?before|without asking|should not|shouldn'?t|"
                   r"constraint|rule)\b")



def _fmt_ts(ts):
    return (ts or "")[:16].replace("T", " ")
