"""Безопасное чтение коллекции Anki.

Живую коллекцию мы не открываем через SQLite никогда — даже на чтение.
Соединение без mode=ro делает чекпоинт WAL при закрытии, а Anki держит
ту же базу открытой; две сборки SQLite чекпоинтят её наперегонки и
коллекция превращается в DatabaseCorrupt. Вместо этого копируем файлы
(cp только читает и повредить источник не может) и открываем копию.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from .config import COLLECTION, COLLECTION_PARTS, EXPECTED_SCHEMA_VER, SNAPSHOT


class CollectionError(RuntimeError):
    pass


def _unicase(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    return (a > b) - (a < b)


def register_collations(con: sqlite3.Connection) -> None:
    """Схема Anki объявляет свою коллацию; без неё падают запросы к decks."""
    con.create_collation("unicase", _unicase)


def fingerprint(path: Path = COLLECTION) -> tuple:
    """Отпечаток всех частей базы.

    Смотреть только на .anki2 нельзя: он меняется редко, вся свежая
    работа идёт в -wal.
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
    # Старый -wal рядом с новой базой = гарантированно битая копия.
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
        raise CollectionError(f"копия повреждена: {status}")
    ver = con.execute("select ver from col").fetchone()[0]
    if ver != EXPECTED_SCHEMA_VER:
        raise CollectionError(
            f"схема Anki {ver}, ожидалась {EXPECTED_SCHEMA_VER}. "
            "Раскладка таблиц могла измениться — разбирать вслепую нельзя."
        )


def open_snapshot(
    path: Path = COLLECTION, dest: Path = SNAPSHOT, attempts: int = 3
) -> sqlite3.Connection:
    """Копирует базу вместе с -wal/-shm и отдаёт соединение к копии.

    Копия живой базы может попасть в середину записи и выйти порванной —
    страдает только одноразовый файл, поэтому просто пробуем ещё раз.
    """
    if not Path(path).exists():
        raise CollectionError(f"коллекция не найдена: {path}")
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
    raise CollectionError(f"не удалось снять целую копию за {attempts} попытки: {last}")


def deck_names(con: sqlite3.Connection) -> dict[int, str]:
    """decks.name разделён \\x1f, а не '::' — скилл ждёт второй вид."""
    return {
        did: name.replace("\x1f", "::")
        for did, name in con.execute("select id, name from decks")
    }


def notetype_names(con: sqlite3.Connection) -> dict[int, str]:
    return dict(con.execute("select id, name from notetypes"))


def note_decks(con: sqlite3.Connection) -> dict[int, int]:
    """Колода заметки через любую её карточку: заметок с карточками
    в разных колодах в этой коллекции нет."""
    return {
        nid: did
        for nid, did in con.execute("select nid, min(did) from cards group by nid")
    }


def note_stats(con: sqlite3.Connection) -> dict[int, tuple[float, int, int]]:
    """note_id → (средний интервал в днях, число повторов, число срывов).

    Интервал повторения — единственный честный ответ на вопрос
    «насколько хорошо человек знает это слово». Число карточек таким
    ответом не является: у полностью обработанного слова их всегда 4.

    Срыв (lapses) — сколько раз выученная карточка снова уехала в «Again».
    Интервал говорит, где человек сейчас, срывы — где ему было тяжело;
    вместе они и отделяют трудное слово от просто нового.
    """
    return {
        nid: (avg_ivl or 0.0, reps or 0, lapses or 0)
        for nid, avg_ivl, reps, lapses in con.execute(
            "select nid, avg(ivl), sum(reps), sum(lapses) from cards group by nid"
        )
    }
