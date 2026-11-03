"""Safe reading of the Anki collection.

We never open the live collection via SQLite — not even read-only.
A connection without mode=ro checkpoints the WAL on close, while Anki keeps
the same database open; two SQLite builds checkpoint it in a race and the
collection turns into DatabaseCorrupt. Instead we copy the files
(cp only reads and cannot damage the source) and open the copy.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import COLLECTION, COLLECTION_HINT, COLLECTION_PARTS, EXPECTED_SCHEMA_VER


class CollectionError(RuntimeError):
    pass


def _unicase(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    return (a > b) - (a < b)


def register_collations(con: sqlite3.Connection) -> None:
    """Anki's schema declares its own collation; without it queries on decks fail."""
    con.create_collation("unicase", _unicase)


def fingerprint(path: Path = COLLECTION) -> tuple:
    """Fingerprint of all database parts.

    Looking only at .anki2 is not enough: it changes rarely, all fresh
    work goes to -wal.
    """
    out = []
    for suffix in COLLECTION_PARTS:
        p = Path(str(path) + suffix)
        try:
            st = p.stat()
            out.append((suffix, st.st_mtime_ns, st.st_size))
        except FileNotFoundError:
            out.append((suffix, 0, 0))
    return tuple(out)


def _copy_parts(path: Path, dest: Path) -> None:
    # A stale -wal next to a fresh database = a guaranteed corrupt copy.
    for suffix in COLLECTION_PARTS:
        stale = Path(str(dest) + suffix)
        if stale.exists():
            stale.unlink()
    for suffix in COLLECTION_PARTS:
        src = Path(str(path) + suffix)
        if src.exists():
            shutil.copy2(src, str(dest) + suffix)


def _verify(con: sqlite3.Connection) -> None:
    status = con.execute("pragma integrity_check").fetchone()[0]
    if status != "ok":
        raise CollectionError(f"copy is corrupt: {status}")
    ver = con.execute("select ver from col").fetchone()[0]
    if ver != EXPECTED_SCHEMA_VER:
        raise CollectionError(
            f"Anki schema {ver}, expected {EXPECTED_SCHEMA_VER}. "
            "Table layout may have changed — refusing to parse blindly."
        )


def open_snapshot(
    dest: Path, path: Path = COLLECTION, attempts: int = 3
) -> sqlite3.Connection:
    """Copy the database with -wal/-shm and return a connection to the copy.

    A copy of a live database may land mid-write and come out torn —
    only the throwaway file suffers, so we just try again.
    """
    if not Path(path).exists():
        raise CollectionError(f"collection not found: {path}" + (f" ({COLLECTION_HINT})" if COLLECTION_HINT else ""))
    dest.parent.mkdir(parents=True, exist_ok=True)

    last: Exception | None = None
    for attempt in range(attempts):
        _copy_parts(Path(path), dest)
        con = sqlite3.connect(dest)
        register_collations(con)
        try:
            _verify(con)
            return con
        except CollectionError as exc:
            last = exc
            con.close()
            if attempt < attempts - 1:
                time.sleep(0.2)
    raise CollectionError(f"could not take an intact copy in {attempts} attempts: {last}")


@contextmanager
def snapshot(path: Path = COLLECTION) -> Iterator[sqlite3.Connection]:
    """A private copy per call, deleted right after.

    Claude Desktop runs the server as two processes, and with a shared copy
    file one process deleted and rewrote it while the other was reading:
    SQLite silently won't replay a -wal from one copy next to an .anki2
    from another, and the data comes out stale without any error.
    """
    with tempfile.TemporaryDirectory(prefix="anki-vocab-") as tmp:
        con = open_snapshot(Path(tmp) / "collection.anki2", path)
        try:
            yield con
        finally:
            con.close()


def deck_names(con: sqlite3.Connection) -> dict[int, str]:
    """decks.name is separated by \\x1f, not '::' — the skill expects the latter."""
    return {
        did: name.replace("\x1f", "::")
        for did, name in con.execute("select id, name from decks")
    }


def notetype_names(con: sqlite3.Connection) -> dict[int, str]:
    return dict(con.execute("select id, name from notetypes"))


def note_decks(con: sqlite3.Connection) -> dict[int, int]:
    """A note's deck via any of its cards: this collection has no notes
    with cards in different decks."""
    return {
        nid: did
        for nid, did in con.execute("select nid, min(did) from cards group by nid")
    }


def note_stats(con: sqlite3.Connection) -> dict[int, tuple[float, int, int]]:
    """note_id → (mean interval in days, review count, lapse count).

    The review interval is the only honest answer to "how well does the
    learner know this word". Card count isn't: a fully processed word
    always has 4.

    A lapse is how many times a learned card fell back to "Again".
    The interval says where the learner is now, lapses — where it was hard;
    together they separate a difficult word from a merely new one.
    """
    return {
        nid: (avg_ivl or 0.0, reps or 0, lapses or 0)
        for nid, avg_ivl, reps, lapses in con.execute(
            "select nid, avg(ivl), sum(reps), sum(lapses) from cards group by nid"
        )
    }
