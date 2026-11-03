"""Call journal: one JSON line per tool call, in full.

server.log is written for human eyes and cuts arguments at 90 chars — it
shows that a call happened, but not which sentences were checked or what
went into the deck. Reviewing a session a week later meant digging them
out of the index and guessing. Everything is here, readable by a script.

An empty ANKI_VOCAB_JOURNAL disables the journal (the tests do this).
"""
from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR

# The response is truncated: the vocab_pool palette is 9k chars and isn't
# needed in full in the journal. Arguments aren't — they're why the journal exists.
RESULT_MAX = 8000

_log = logging.getLogger("anki_vocab.journal")
_log.propagate = False
_log.setLevel(logging.INFO)


def _open() -> Path | None:
    raw = os.environ.get("ANKI_VOCAB_JOURNAL", str(DATA_DIR / "journal.jsonl"))
    if not raw:
        return None
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError:
        return None
    handler.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(handler)
    return path


PATH = _open()


def record(
    tool: str,
    call: int,
    args: dict,
    result: str | None,
    ms: float,
    error: str | None = None,
    extra: dict | None = None,
) -> None:
    if PATH is None:
        return
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pid": os.getpid(),
        "call": call,
        "tool": tool,
        "ms": round(ms),
        "args": args,
    }
    if result is not None:
        entry["result"] = (
            result if len(result) <= RESULT_MAX
            else result[:RESULT_MAX] + f"… [{len(result)} chars]"
        )
    if error:
        entry["error"] = error
    if extra:
        entry.update(extra)
    _log.info(json.dumps(entry, ensure_ascii=False, default=str))
