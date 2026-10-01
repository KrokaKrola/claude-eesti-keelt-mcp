"""Сборка индекса словарного запаса из коллекции Anki."""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sqlite3
import time
from pathlib import Path

from . import collection as col
from .config import (
    COLLECTION,
    INDEX,
    INDEX_VERSION,
    LEMMATIZER_VERSION,
    deck_in_scope,
)
from .lemma import Lemmatizer
from .parse import (
    FIELD_SEP,
    LEVEL_ORDER,
    ParsedNote,
    clean,
    has_cyrillic,
    parse_note,
    words,
)

log = logging.getLogger("anki_vocab")

SCHEMA = """
create table if not exists meta (key text primary key, value text);

create table if not exists entry (
  note_id   integer primary key,
  notetype  text    not null,
  deck      text    not null,
  level     text,
  pos       text,
  topics    text    not null,
  ru        text,
  headword  text    not null,
  et_raw    text    not null,
  examples  text    not null,
  is_phrase integer not null,
  ivl       real    not null default 0,
  reps      integer not null default 0,
  lapses    integer not null default 0,
  mod       integer not null
);

create table if not exists form (
  form    text    not null,
  lemma   text    not null,
  note_id integer not null,
  slot    text    not null,
  primary key (form, note_id, slot)
) without rowid;
create index if not exists form_lemma_idx on form(lemma);
create index if not exists form_note_idx  on form(note_id);

create table if not exists lemma (
  lemma        text primary key,
  ru           text,
  pos          text,
  level        text,
  topics       text    not null,
  forms        text    not null,
  n_notes      integer not null,
  n_word_notes integer not null,
  strength     real    not null default 0,
  lapses       integer not null default 0,
  first_seen   integer,
  last_used    integer
);

create table if not exists lemma_cache (word text primary key, analyses text not null);

create table if not exists outside (lemma text primary key) without rowid;

-- Слова, про которые человек сказал «это я знаю». Единственная таблица
-- индекса, которой нет в коллекции: восстановить её пересборкой нельзя,
-- поэтому её не сносят ни --rebuild, ни смена версии схемы.
create table if not exists debt_skip (
  lemma text primary key,
  added integer not null
) without rowid;

-- Что уже записано в тему за текущее заполнение: доза новых слов
-- считается по всем вызовам topic_write одной темы, а не по каждому.
-- Как и debt_skip, из коллекции не восстанавливается и не сносится.
create table if not exists dose_log (
  topic text    not null,
  at    integer not null,
  words integer not null,
  new   text    not null
);

-- Ответы словаря Ekilex: формы слова не меняются, сеть нужна один раз.
-- Не из коллекции, поэтому пересборка индекса их не трогает.
create table if not exists ekilex_cache (
  word    text primary key,
  data    text    not null,
  fetched integer not null
) without rowid;

create virtual table if not exists entry_fts using fts5(ru, et_raw, note_id unindexed);
"""


def open_index(
    path: Path | None = None, same_thread: bool = True
) -> sqlite3.Connection:
    """same_thread=False — для MCP: SDK гоняет синхронные инструменты
    в рабочих потоках, доступ снаружи сериализуется замком.

    Путь берётся в момент вызова, а не при импорте: иначе тесты не могут
    подсунуть свой индекс и лезут в рабочий.
    """
    from . import config

    path = Path(path) if path is not None else config.INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=same_thread)
    # WAL: сервер в Desktop держит индекс открытым, а в обычном режиме
    # читатель блокирует писателя — CLI падал с 'database is locked'.
    # Само переключение требует эксклюзивной блокировки, поэтому при
    # живом соединении в старом режиме оно не пройдёт: не падаем,
    # режим встанет при следующем одиночном открытии.
    con.execute("pragma busy_timeout = 10000")
    try:
        con.execute("pragma journal_mode = WAL")
    except sqlite3.OperationalError:
        log.debug("не удалось включить WAL — индекс открыт другим процессом")
    col.register_collations(con)
    con.executescript(SCHEMA)
    _guard_lemma_cache(con)
    _guard_index_version(con)
    return con


def _guard_index_version(con: sqlite3.Connection) -> None:
    """Схема индекса поменялась — старые строки не дополнить, проще
    собрать заново: это две секунды.

    Именно снести таблицы, а не почистить: 'create table if not exists'
    выше застаёт старую таблицу и молча оставляет её без новых колонок,
    а падает это уже на вставке, посреди сборки.
    """
    if _meta_get(con, "index_version") == str(INDEX_VERSION):
        return
    if con.execute("select count(*) from entry").fetchone()[0]:
        log.info("схема индекса версии %s — пересобираю", INDEX_VERSION)
    # debt_skip тут быть не должно никогда: она собрана из ответов
    # человека, а не из коллекции, и пересборкой не восстанавливается.
    for table in ("entry", "form", "lemma", "outside", "entry_fts"):
        con.execute(f"drop table if exists {table}")
    con.executescript(SCHEMA)
    _meta_set(con, "last_note_mod", 0)
    _meta_set(con, "index_version", INDEX_VERSION)
    # Отпечаток снимаем вместе с данными. Досборка сравнивает только его,
    # и уцелевший отпечаток означал «коллекция не менялась, индекс
    # актуален» — поверх пустых таблиц. Сервер молча отвечал, что знает
    # ноль слов: дублей нет, палитра пуста, всё новое.
    con.execute("delete from meta where key = 'fingerprint'")
    con.commit()


def _guard_lemma_cache(con: sqlite3.Connection) -> None:
    """Кэш переживает пересборки, поэтому при смене логики разбора его
    надо сбрасывать — иначе он отдаёт результаты старого кода."""
    saved = _meta_get(con, "lemmatizer_version")
    if saved == str(LEMMATIZER_VERSION):
        return
    stale = con.execute("select count(*) from lemma_cache").fetchone()[0]
    if stale:
        log.info("лемматизатор версии %s (был %s) — сбрасываю кэш, %s записей",
                 LEMMATIZER_VERSION, saved or "—", stale)
    con.execute("delete from lemma_cache")
    _meta_set(con, "lemmatizer_version", LEMMATIZER_VERSION)
    con.commit()


def _meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("select value from meta where key = ?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(con: sqlite3.Connection, key: str, value) -> None:
    con.execute(
        "insert or replace into meta(key, value) values (?, ?)", (key, str(value))
    )


def _store_note(
    con: sqlite3.Connection,
    lem: Lemmatizer,
    note_id: int,
    notetype: str,
    deck: str,
    mod: int,
    parsed: ParsedNote,
    stats: tuple[float, int, int] = (0.0, 0, 0),
) -> None:
    con.execute(
        """insert or replace into entry
           (note_id, notetype, deck, level, pos, topics, ru, headword,
            et_raw, examples, is_phrase, ivl, reps, lapses, mod)
           values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            note_id, notetype, deck, parsed.level, parsed.pos,
            json.dumps(parsed.topics, ensure_ascii=False),
            parsed.ru, parsed.headword, parsed.et_text,
            json.dumps(parsed.examples, ensure_ascii=False),
            int(parsed.is_phrase), stats[0], stats[1], stats[2], mod,
        ),
    )
    con.execute("delete from form where note_id = ?", (note_id,))
    rows = set()
    if parsed.force_lexical and len(words(parsed.headword)) > 1:
        # Фразовый глагол — самостоятельная единица словаря. Разложив его
        # по словам, мы бы отдали толкование 'kokku saama' (встречаться)
        # обычному глаголу saama (получать). Ключ — словарная форма целиком.
        key = parsed.headword.lower()
        for slot, text in parsed.slots:
            rows.add((text.lower(), key, note_id, slot))
    elif parsed.is_phrase:
        # Предложение разбираем целиком: тег части речи на нём относится
        # к целевому слову, а не к каждому, и омонимию снимает контекст.
        for slot, text in parsed.slots:
            rows |= _sentence_rows(text, lem, note_id, slot)
    else:
        for slot, text in parsed.slots:
            for word in words(text):
                rows.add((word.lower(), lem.lemma(word, parsed.pos), note_id, slot))
    # Слова из примеров: человек их читает на каждом повторе, поэтому
    # незнакомыми они не являются. В палитру не идут (слот 'example'),
    # но дают второй ярус и отметку «когда слово встречалось в последний раз».
    for et, _ru in parsed.examples:
        rows |= _sentence_rows(et, lem, note_id, "example")
    con.executemany(
        "insert or replace into form(form, lemma, note_id, slot) values (?,?,?,?)",
        rows,
    )
    con.execute("delete from entry_fts where note_id = ?", (note_id,))
    con.execute(
        "insert into entry_fts(ru, et_raw, note_id) values (?,?,?)",
        (parsed.ru, parsed.et_text, note_id),
    )


def _sentence_rows(
    text: str, lem: Lemmatizer, note_id: int, slot: str
) -> set[tuple[str, str, int, str]]:
    """Слова предложения с леммами, снятыми в контексте.

    По одному слову 'jalga' в «pani kingad jalga» разбиралось наречием, и
    в индексе заводилась лишняя лемма — она же потом всплывала в долге
    словом без карточки.
    """
    tokens = words(text)
    return {
        (token.lower(), lemma, note_id, slot)
        for token, (lemma, _code) in zip(tokens, lem.sentence(tokens))
    }


def rebuild_lemmas(con: sqlite3.Connection) -> int:
    """Пересборка словаря целиком: на 6.5k заметок это дешевле инкремента.

    Заметки типа TypeIn лежат по одной на форму, поэтому слово собирается
    здесь — иначе палитра вернулась бы с дублями aeg/aega/ajal.
    """
    con.execute("delete from lemma")
    agg: dict[str, dict] = {}
    query = """
        select f.lemma, f.form, f.slot, e.note_id, e.ru, e.pos, e.level,
               e.topics, e.is_phrase, e.mod, e.ivl, e.lapses
        from form f join entry e on e.note_id = f.note_id
    """
    for (lemma, form, slot, note_id, ru, pos, level, topics, is_phrase, mod,
         ivl, lapses) in con.execute(query):
        # note_id в Anki — время создания заметки в миллисекундах.
        from_example = slot == "example"
        slot = agg.setdefault(
            lemma,
            {
                "forms": set(), "notes": set(), "word_notes": set(),
                # Карточка-предложение содержит десяток слов и ничего не
                # сообщает о части речи каждого из них: «Koer on suur» с
                # тегом «прилагательное» — про suur, не про koer. Поэтому
                # у слов и у фраз отдельные голоса, фразовые идут в запас.
                "word": {"ru": collections.Counter(), "pos": collections.Counter(),
                         "level": None, "topics": set()},
                "any": {"ru": collections.Counter(), "pos": collections.Counter(),
                        "level": None, "topics": set()},
                "first_seen": mod, "ivl": {}, "lapses": {}, "last_used": 0,
            },
        )
        slot["ivl"][note_id] = ivl
        slot["lapses"][note_id] = lapses
        slot["last_used"] = max(slot["last_used"], note_id)
        slot["forms"].add(form)
        slot["notes"].add(note_id)
        targets = [slot["any"]]
        if not is_phrase and not from_example:
            slot["word_notes"].add(note_id)
            targets.append(slot["word"])
        # Толкование и часть речи берём только со словарных карточек:
        # перевод предложения — не перевод слова, а тег части речи на
        # фразе относится к её целевому слову, не к каждому.
        if not is_phrase and not from_example:
            if ru and has_cyrillic(ru):
                slot["word"]["ru"][ru] += 1
            if pos:
                slot["word"]["pos"][pos] += 1
        for target in targets:
            if level and (
                target["level"] is None
                or LEVEL_ORDER.get(level, 99) < LEVEL_ORDER.get(target["level"], 99)
            ):
                target["level"] = level
            target["topics"].update(json.loads(topics))
        slot["first_seen"] = min(slot["first_seen"], mod)

    def vote(slot: dict) -> dict:
        """Уровень и темы можно взять с фраз, толкование и часть речи — нет."""
        base = slot["word"] if slot["word_notes"] else slot["any"]
        return {
            "ru": slot["word"]["ru"],
            "pos": slot["word"]["pos"],
            "level": base["level"],
            "topics": base["topics"],
        }

    con.executemany(
        """insert into lemma
           (lemma, ru, pos, level, topics, forms, n_notes, n_word_notes,
            strength, lapses, first_seen, last_used)
           values (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                lemma,
                vote(s)["ru"].most_common(1)[0][0] if vote(s)["ru"] else None,
                vote(s)["pos"].most_common(1)[0][0] if vote(s)["pos"] else None,
                vote(s)["level"],
                json.dumps(sorted(vote(s)["topics"]), ensure_ascii=False),
                json.dumps(sorted(s["forms"]), ensure_ascii=False),
                len(s["notes"]),
                len(s["word_notes"]),
                round(
                    sum(s["ivl"].get(n, 0) for n in (s["word_notes"] or s["notes"]))
                    / max(1, len(s["word_notes"] or s["notes"])),
                    1,
                ),
                # Срывы складываем, а не усредняем: вопрос «сколько раз это
                # слово забывалось» — про сумму. Усреднение по четырём
                # карточкам делало из трудного слова спокойное.
                sum(s["lapses"].get(n, 0) for n in (s["word_notes"] or s["notes"])),
                s["first_seen"],
                s["last_used"],
            )
            for lemma, s in agg.items()
        ],
    )
    return len(agg)


def reconcile_decks(con: sqlite3.Connection, deck_of: dict[int, str]) -> int:
    """Переписывает колоду у заметок, которые переехали или чью колоду
    переименовали.

    Ни то, ни другое не меняет notes.mod, так что инкремент по mod их
    не видит. А перенос делает сам скилл: заметки создаются в ближайшей
    существующей колоде и потом двигаются change_deck'ом.
    """
    updates = [
        (deck_of[note_id], note_id)
        for note_id, stored in con.execute("select note_id, deck from entry")
        if note_id in deck_of and deck_of[note_id] != stored
    ]
    con.executemany("update entry set deck = ? where note_id = ?", updates)
    return len(updates)


def refresh_stats(con: sqlite3.Connection, stats: dict[int, tuple]) -> int:
    """Переписывает интервал, повторы и срывы у всех заметок индекса.

    Повтор карточки не меняет notes.mod, поэтому инкремент такую заметку
    не видит и её статистика застывает в момент последней правки текста.
    На этой коллекции разъезжалась каждая двенадцатая заметка — а вся
    ценность подсказки «трудные слова» именно в свежести этих чисел.
    Строк тысячи, один executemany дешевле любой хитрости.
    """
    rows = [
        (ivl, reps, lapses, note_id)
        for (note_id,) in con.execute("select note_id from entry")
        for ivl, reps, lapses in [stats.get(note_id, (0.0, 0, 0))]
    ]
    con.executemany(
        "update entry set ivl = ?, reps = ?, lapses = ? where note_id = ?", rows
    )
    return len(rows)


def _headword_lemmas(flds: str, lem: Lemmatizer) -> set[str]:
    """Лемма из первого поля, если оно похоже на заголовок карточки.

    Разбор заметки сюда не годится: у Basic он выбирает эстонскую сторону
    по числу латинских слов, и карточка «hommik | утро Hommik on ilus.»
    отдаёт заднюю сторону с примером, а заголовок теряется — слово
    попадало в долг, хотя карточка у него есть.

    Заголовок — короткое поле без кириллицы и без знаков предложения.
    Два слова разрешаем ради фразовых глаголов ('kaasa elama').
    """
    head = clean(flds.split(FIELD_SEP)[0]) if flds else ""
    if not head or has_cyrillic(head) or set(head) & set(".?!;:"):
        return set()
    parts = words(head)
    if not parts or len(parts) > 2 or len(parts) != len(head.split()):
        return set()
    return {lem.lemma(w) for w in parts}


def scan_outside(
    con: sqlite3.Connection,
    snap: sqlite3.Connection,
    notetypes: dict[int, str],
    deck_of: dict[int, str],
    lem: Lemmatizer,
) -> int:
    """Слова, у которых своя карточка есть, но за пределами индекса.

    Ветка A1 из области видимости исключена — она в старых форматах, и
    её конвертация впереди. Но карточки там настоящие: 149 заметок
    «трёх форм», 564 typein и словарные Basic вроде «laps | ребёнок».
    Для vocab_debt это решает всё: без этого списка инструмент уверял,
    что laps, koer и sõber человек никогда не учил, — а это первые
    строчки выдачи, ровно те, за которые он и возьмётся.

    Считаем только заметки-словарные статьи: предложение, где слово
    просто попалось, карточкой этого слова не делает. Весь проход —
    полсотни миллисекунд, поэтому обновляем целиком на каждой сборке.
    """
    found: set[str] = set()
    for note_id, mid, tags, flds in snap.execute(
        "select id, mid, tags, flds from notes"
    ):
        if deck_in_scope(deck_of.get(note_id, "")):
            continue
        parsed = parse_note(notetypes.get(mid, ""), flds, tags)
        if parsed is not None and not parsed.is_phrase:
            for _slot, text in parsed.slots:
                for word in words(text):
                    found.add(lem.lemma(word, parsed.pos))
        found.update(_headword_lemmas(flds, lem))
    con.execute("delete from outside")
    con.executemany("insert into outside(lemma) values (?)", [(w,) for w in found])
    return len(found)


def build(con: sqlite3.Connection, full: bool = False, quiet: bool = False) -> dict:
    started = time.time()
    lem = Lemmatizer(con)
    with col.snapshot() as snap:
        decks = col.deck_names(snap)
        notetypes = col.notetype_names(snap)
        note_deck = col.note_decks(snap)
        stats = col.note_stats(snap)

        built_at = int(_meta_get(con, "built_at") or 0)
        if full:
            con.execute("delete from entry")
            con.execute("delete from form")
            con.execute("delete from entry_fts")
            last_mod = 0
        else:
            last_mod = int(_meta_get(con, "last_note_mod") or 0)

        # Колода заметки может смениться без правки самой заметки —
        # переименованием или переносом карточек. Поэтому область
        # видимости пересчитываем каждый раз по текущим именам колод.
        deck_of = {
            note_id: decks.get(did, "")
            for note_id, did in note_deck.items()
        }
        live_ids = {row[0] for row in snap.execute("select id from notes")}
        scoped_ids = {
            note_id for note_id in live_ids if deck_in_scope(deck_of.get(note_id, ""))
        }
        known_ids = {row[0] for row in con.execute("select note_id from entry")}

        # Ушли из коллекции или уехали за пределы области видимости.
        removed = known_ids - scoped_ids
        for note_id in removed:
            con.execute("delete from entry where note_id = ?", (note_id,))
            con.execute("delete from form where note_id = ?", (note_id,))
            con.execute("delete from entry_fts where note_id = ?", (note_id,))

        pending = [
            row
            for row in snap.execute(
                "select id, mid, mod, tags, flds from notes where mod > ?", (last_mod,)
            )
            if row[0] in scoped_ids
        ]
        # Заметка могла въехать в область видимости, не изменившись сама.
        fresh = scoped_ids - known_ids - {r[0] for r in pending}
        if fresh:
            marks = ",".join("?" * len(fresh))
            pending += snap.execute(
                f"select id, mid, mod, tags, flds from notes where id in ({marks})",
                tuple(fresh),
            ).fetchall()

        stored = skipped = 0
        max_mod = last_mod
        for note_id, mid, mod, tags, flds in pending:
            notetype = notetypes.get(mid, "")
            parsed = parse_note(notetype, flds, tags)
            max_mod = max(max_mod, mod)
            if parsed is None:
                skipped += 1
                continue
            _store_note(
                con, lem, note_id, notetype, deck_of[note_id], mod, parsed,
                stats.get(note_id, (0.0, 0, 0)),
            )
            stored += 1

        moved = reconcile_decks(con, deck_of)
        outside = scan_outside(con, snap, notetypes, deck_of, lem)
        refresh_stats(con, stats)
        lemmas = rebuild_lemmas(con)
        _meta_set(con, "last_note_mod", max_mod)
        _meta_set(con, "fingerprint", json.dumps(col.fingerprint()))
        _meta_set(con, "index_version", INDEX_VERSION)
        _meta_set(con, "collection", str(COLLECTION))
        _meta_set(con, "built_at", int(time.time()))
        con.commit()

    # Заметки, которых в индексе не было и которые созданы после
    # прошлой сборки: id заметки Anki — это время её создания в мс.
    # Заехавшие в область видимости переносом сюда не попадают, как и
    # первое наполнение индекса: там ново вообще всё.
    appeared = collections.Counter(
        deck_of.get(note_id, "") or "?"
        for note_id in scoped_ids - known_ids
        if built_at and not full and note_id > built_at * 1000
    )

    stats = {
        "moved": moved,
        "appeared": appeared,
        "stored": stored,
        "skipped": skipped,
        "removed": len(removed),
        "lemmas": lemmas,
        "outside": outside,
        "entries": con.execute("select count(*) from entry").fetchone()[0],
        "cache_hits": lem.hits,
        "cache_misses": lem.misses,
        "seconds": round(time.time() - started, 2),
    }
    if not quiet:
        mode = "полная сборка" if full else "инкремент"
        print(
            f"{mode}: заметок обработано {stored}, пропущено {skipped}, "
            f"удалено {len(removed)}"
        )
        print(f"  в индексе: {stats['entries']} заметок, {lemmas} лемм")
        print(f"  со своей карточкой вне области видимости: {outside} слов")
        if moved:
            print(f"  сменили колоду (перенос или переименование): {moved}")
        print(
            f"  лемматизатор: {lem.hits} из кэша, {lem.misses} посчитано"
            f"  ·  {stats['seconds']} с"
        )
    return stats


def is_stale(con: sqlite3.Connection) -> bool:
    saved = _meta_get(con, "fingerprint")
    return saved is None or json.loads(saved) != [list(p) for p in col.fingerprint()]


def ensure_fresh(con: sqlite3.Connection, own_write: bool = False) -> bool:
    """Ленивая досборка. Проверка stat() по трём файлам стоит доли миллисекунды.

    own_write=True — досборка сразу после своей же записи: появившиеся
    заметки ожидаемы и жалобы не заслуживают.
    """
    if not is_stale(con):
        log.debug("коллекция не менялась — индекс актуален")
        return False
    stats = build(con, full=False, quiet=True)
    appeared = stats["appeared"]
    # Открытый Anki трогает -wal на каждом ответе, так что отпечаток
    # расходится по десятку раз за сессию, а менять при этом нечего.
    # Такую досборку держим в DEBUG, иначе она забивает лог.
    changed = stats["stored"] or stats["moved"] or stats["removed"]
    log.log(
        logging.INFO if changed else logging.DEBUG,
        "досборка: обновлено %s, новых %s, сменили колоду %s, удалено %s,"
        " лемм %s, %.0f мс",
        stats["stored"], sum(appeared.values()), stats["moved"],
        stats["removed"], stats["lemmas"], stats["seconds"] * 1000,
    )
    if appeared and not own_write:
        # Единственный след записи, сделанной не этим сервером: заметки
        # в колоде есть, а вызова, который их создал, в логе нет.
        log.info(
            "  ⚠ заметок появилось мимо сервера: %d · %s",
            sum(appeared.values()),
            ", ".join(f"{deck} ×{n}" for deck, n in appeared.most_common(5)),
        )
    return True


def print_stats(con: sqlite3.Connection) -> None:
    q = lambda sql: con.execute(sql).fetchall()
    entries = q("select count(*) from entry")[0][0]
    lemmas = q("select count(*) from lemma")[0][0]
    single = q("select count(*) from lemma where n_word_notes > 0")[0][0]
    print(f"заметок: {entries}   лемм: {lemmas}   из них однословных: {single}")
    print(f"коллекция: {_meta_get(con, 'collection')}")
    print("\nпо уровням:")
    for level, n in q(
        "select coalesce(level,'—'), count(*) from lemma group by 1 order by 2 desc"
    ):
        print(f"  {level:6} {n}")
    print("\nпо частям речи:")
    for pos, n in q(
        "select coalesce(pos,'—'), count(*) from lemma group by 1 order by 2 desc"
    ):
        print(f"  {pos:16} {n}")
    print("\nпо типам заметок:")
    for nt, n in q("select notetype, count(*) from entry group by 1 order by 2 desc"):
        print(f"  {nt:34} {n}")
    print("\nтоп-20 тем:")
    counter: collections.Counter = collections.Counter()
    for (topics,) in q("select topics from lemma"):
        counter.update(json.loads(topics))
    for topic, n in counter.most_common(20):
        print(f"  {topic:22} {n}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="anki_vocab.index")
    ap.add_argument("--rebuild", action="store_true", help="полная пересборка")
    ap.add_argument("--refresh", action="store_true", help="инкрементальная досборка")
    ap.add_argument("--stats", action="store_true", help="показать сводку")
    ap.add_argument("--prune-cache", action="store_true",
                    help="выбросить из кэша лемм слова, которых нет ни в одной "
                         "индексируемой заметке")
    args = ap.parse_args()

    con = open_index()
    try:
        if args.prune_cache:
            before = con.execute("select count(*) from lemma_cache").fetchone()[0]
            con.execute(
                "delete from lemma_cache where word not in (select form from form)"
            )
            con.commit()
            after = con.execute("select count(*) from lemma_cache").fetchone()[0]
            print(f"кэш лемм: было {before}, стало {after}, выброшено {before - after}")
        if args.rebuild:
            build(con, full=True)
        elif args.refresh:
            if is_stale(con):
                build(con, full=False)
            else:
                print("коллекция не менялась — индекс актуален")
        if args.stats or not (args.rebuild or args.refresh or args.prune_cache):
            print_stats(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
