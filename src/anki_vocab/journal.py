"""Журнал вызовов: одна JSON-строка на вызов инструмента, целиком.

server.log пишется для глаз и режет аргументы до 90 символов — по нему
видно, что вызов был, но не видно, какие предложения проверялись и что
ушло в колоду. Разбирая сессию через неделю, их приходилось вытаскивать
из индекса и угадывать. Здесь лежит всё, и это читается скриптом.

Пустой ANKI_VOCAB_JOURNAL выключает журнал (так делают тесты).
"""
from __future__ import annotations

import json
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import DATA_DIR

# Ответ режем: палитра vocab_pool — это 9k символов, и в журнале она
# не нужна целиком. Аргументы не режем — ради них журнал и заведён.
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
