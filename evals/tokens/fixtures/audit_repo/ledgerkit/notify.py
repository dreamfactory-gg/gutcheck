import time

RETRIES = 3


def send_with_retry(send, message):
    for attempt in range(RETRIES):
        try:
            return send(message)
        except Exception:
            time.sleep(2 ** attempt)


def digest(lines, width=72):
    return "\n".join(line[:width] for line in lines)
