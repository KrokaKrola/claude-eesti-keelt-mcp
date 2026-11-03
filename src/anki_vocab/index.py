"""Building the vocabulary index from the Anki collection."""
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

-- Words the learner marked as "I know this". The only index table that
-- isn't in the collection: a rebuild can't restore it, so neither
-- --rebuild nor a schema version change drops it.
create table if not exists debt_skip (
  lemma text primary key,
  added integer not null
) without rowid;

-- What's already been written to the topic during the current fill: the
-- new-word dose is counted across all topic_write calls for one topic, not
-- per call. Like debt_skip, it can't be restored from the collection and
-- is never dropped.
create table if not exists dose_log (
  topic text    not null,
  at    integer not null,
  words integer not null,
  new   text    not null
);

-- Ekilex dictionary responses: word forms don't change, so the network is
-- needed once. Not from the collection, so an index rebuild leaves them alone.
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
    """same_thread=False is for MCP: the SDK runs sync tools in worker
    threads; outside access is serialized by a lock.

    The path is resolved at call time, not import time: otherwise tests
    can't substitute their own index and end up touching the real one.
    """
    from . import config

    path = Path(path) if path is not None else config.INDEX
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=same_thread)
    # WAL: the Desktop server keeps the index open, and in the default mode
    # a reader blocks the writer — the CLI failed with 'database is locked'.
    # Switching itself needs an exclusive lock, so with a live connection in
    # the old mode it won't go through: don't fail, the mode will be set on
    # the next solo open.
    con.execute("pragma busy_timeout = 10000")
    try:
        con.execute("pragma journal_mode = WAL")
    except sqlite3.OperationalError:
        log.debug("could not enable WAL — index is open by another process")
    col.register_collations(con)
    con.executescript(SCHEMA)
    _guard_lemma_cache(con)
    _guard_index_version(con)
    return con


def _guard_index_version(con: sqlite3.Connection) -> None:
    """The index schema changed — old rows can't be patched, simpler to
    rebuild: it takes two seconds.

    Drop the tables, not just clear them: 'create table if not exists'
    above finds the old table and silently keeps it without new columns,
    which then fails on insert, mid-build.
    """
    if _meta_get(con, "index_version") == str(INDEX_VERSION):
        return
    if con.execute("select count(*) from entry").fetchone()[0]:
        log.info("index schema version %s — rebuilding", INDEX_VERSION)
    # debt_skip must never be here: it's built from the learner's answers,
    # not from the collection, and a rebuild can't restore it.
    for table in ("entry", "form", "lemma", "outside", "entry_fts"):
        con.execute(f"drop table if exists {table}")
    con.executescript(SCHEMA)
    _meta_set(con, "last_note_mod", 0)
    _meta_set(con, "index_version", INDEX_VERSION)
    # The fingerprint is dropped together with the data. Refresh compares only
    # it, and a surviving fingerprint meant "collection unchanged, index up to
    # date" — on top of empty tables. The server silently answered that it
    # knew zero words: no duplicates, empty palette, everything new.
    con.execute("delete from meta where key = 'fingerprint'")
    con.commit()


def _guard_lemma_cache(con: sqlite3.Connection) -> None:
    """The cache survives rebuilds, so it must be reset when the parsing
    logic changes — otherwise it serves results of the old code."""
    saved = _meta_get(con, "lemmatizer_version")
    if saved == str(LEMMATIZER_VERSION):
        return
    stale = con.execute("select count(*) from lemma_cache").fetchone()[0]
    if stale:
        log.info("lemmatizer version %s (was %s) — resetting cache, %s entries",
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
        # A phrasal verb is a vocabulary unit of its own. Splitting it into
        # words would hand the gloss of 'kokku saama' (to meet) to the plain
        # verb saama (to get). The key is the whole dictionary form.
        key = parsed.headword.lower()
        for slot, text in parsed.slots:
            rows.add((text.lower(), key, note_id, slot))
    elif parsed.is_phrase:
        # Parse the sentence as a whole: a POS tag on it refers to the target
        # word, not to every word, and context resolves the ambiguity.
        for slot, text in parsed.slots:
            rows |= _sentence_rows(text, lem, note_id, slot)
    else:
        for slot, text in parsed.slots:
            for word in words(text):
                rows.add((word.lower(), lem.lemma(word, parsed.pos), note_id, slot))
    # Words from examples: the learner reads them on every review, so they
    # aren't unfamiliar. They don't enter the palette (slot 'example'), but
    # give a second tier and a "last seen" mark.
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
    """Sentence words with lemmas disambiguated in context.

    On its own, 'jalga' in «pani kingad jalga» parsed as an adverb, and the
    index got an extra lemma — which then surfaced in the debt as a word
    without a card.
    """
    tokens = words(text)
    return {
        (token.lower(), lemma, note_id, slot)
        for token, (lemma, _code) in zip(tokens, lem.sentence(tokens))
    }


def rebuild_lemmas(con: sqlite3.Connection) -> int:
    """Rebuild the whole vocabulary: for 6.5k notes that's cheaper than
    incremental.

    TypeIn notes are stored one per form, so the word is assembled here —
    otherwise the palette would come back with duplicates aeg/aega/ajal.
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
        # An Anki note_id is the note's creation time in milliseconds.
        from_example = slot == "example"
        slot = agg.setdefault(
            lemma,
            {
                "forms": set(), "notes": set(), "word_notes": set(),
                # A sentence card holds a dozen words and says nothing about
                # the POS of each: "Koer on suur" tagged «прилагательное» is
                # about suur, not koer. So words and phrases vote separately;
                # phrase votes are the fallback.
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
        # Gloss and POS come only from dictionary cards: a sentence's
        # translation isn't the word's translation, and a POS tag on a phrase
        # refers to its target word, not to every word in it.
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
        """Level and topics may come from phrases, gloss and POS may not."""
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
                # Lapses are summed, not averaged: "how many times was this
                # word forgotten" is about the total. Averaging over four
                # cards made a hard word look calm.
                sum(s["lapses"].get(n, 0) for n in (s["word_notes"] or s["notes"])),
                s["first_seen"],
                s["last_used"],
            )
            for lemma, s in agg.items()
        ],
    )
    return len(agg)


def reconcile_decks(con: sqlite3.Connection, deck_of: dict[int, str]) -> int:
    """Rewrite the deck of notes that moved or whose deck was
    renamed.

    Neither changes notes.mod, so the mod-based increment doesn't see them.
    And the skill moves notes itself: they are created in the nearest
    existing deck and then moved with change_deck.
    """
    updates = [
        (deck_of[note_id], note_id)
        for note_id, stored in con.execute("select note_id, deck from entry")
        if note_id in deck_of and deck_of[note_id] != stored
    ]
    con.executemany("update entry set deck = ? where note_id = ?", updates)
    return len(updates)


def refresh_stats(con: sqlite3.Connection, stats: dict[int, tuple]) -> int:
    """Rewrite interval, reviews and lapses for every note in the index.

    A card review doesn't change notes.mod, so the increment doesn't see
    such a note and its stats freeze at the last text edit. On this
    collection one in twelve notes drifted — and the whole value of the
    "hard words" hint is in these numbers being fresh. Thousands of rows;
    one executemany is cheaper than any trick.
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
    """The lemma from the first field, if it looks like a card headword.

    Note parsing doesn't fit here: for Basic it picks the Estonian side by
    Latin word count, and the card «hommik | утро Hommik on ilus.» yields
    the back side with the example, losing the headword — the word ended
    up in the debt even though it has a card.

    A headword is a short field with no Cyrillic and no sentence punctuation.
    Two words are allowed for phrasal verbs ('kaasa elama').
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
    """Words that have their own card, but outside the index.

    The A1 branch is out of scope — it's in old formats awaiting
    conversion. But the cards there are real: 149 "three forms" notes,
    564 typein and dictionary Basic cards like «laps | ребёнок».
    For vocab_debt this is everything: without this list the tool claimed
    the learner had never studied laps, koer and sõber — the very first
    lines of the output, exactly the ones they'd pick up.

    Only dictionary-entry notes count: a sentence where the word just
    appears doesn't make it that word's card. The whole pass takes ~50 ms,
    so it's refreshed entirely on every build.
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

        # A note's deck can change without editing the note itself —
        # by renaming or moving cards. So scope is recomputed every time
        # from the current deck names.
        deck_of = {
            note_id: decks.get(did, "")
            for note_id, did in note_deck.items()
        }
        live_ids = {row[0] for row in snap.execute("select id from notes")}
        scoped_ids = {
            note_id for note_id in live_ids if deck_in_scope(deck_of.get(note_id, ""))
        }
        known_ids = {row[0] for row in con.execute("select note_id from entry")}

        # Gone from the collection or moved out of scope.
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
        # A note may have entered scope without changing itself.
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

    # Notes absent from the index and created after the previous build:
    # an Anki note id is its creation time in ms. Notes moved into scope
    # don't count, nor does the first fill of the index: everything is new
    # there.
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
        mode = "full build" if full else "incremental"
        print(
            f"{mode}: notes processed {stored}, skipped {skipped}, "
            f"removed {len(removed)}"
        )
        print(f"  in index: {stats['entries']} notes, {lemmas} lemmas")
        print(f"  with own card outside scope: {outside} words")
        if moved:
            print(f"  changed deck (move or rename): {moved}")
        print(
            f"  lemmatizer: {lem.hits} from cache, {lem.misses} computed"
            f"  ·  {stats['seconds']} s"
        )
    return stats


def is_stale(con: sqlite3.Connection) -> bool:
    saved = _meta_get(con, "fingerprint")
    return saved is None or json.loads(saved) != [list(p) for p in col.fingerprint()]


def ensure_fresh(con: sqlite3.Connection, own_write: bool = False) -> bool:
    """Lazy refresh. The stat() check on three files costs a fraction of a ms.

    own_write=True — refresh right after our own write: new notes are
    expected and don't deserve a warning.
    """
    if not is_stale(con):
        log.debug("collection unchanged — index up to date")
        return False
    stats = build(con, full=False, quiet=True)
    appeared = stats["appeared"]
    # A running Anki touches -wal on every answer, so the fingerprint
    # diverges a dozen times per session with nothing to update.
    # Such refreshes stay at DEBUG, otherwise they flood the log.
    changed = stats["stored"] or stats["moved"] or stats["removed"]
    log.log(
        logging.INFO if changed else logging.DEBUG,
        "refresh: updated %s, new %s, changed deck %s, removed %s,"
        " lemmas %s, %.0f ms",
        stats["stored"], sum(appeared.values()), stats["moved"],
        stats["removed"], stats["lemmas"], stats["seconds"] * 1000,
    )
    if appeared and not own_write:
        # The only trace of a write not made by this server: the notes are
        # in the deck, but the call that created them isn't in the log.
        log.info(
            "  ⚠ notes appeared bypassing the server: %d · %s",
            sum(appeared.values()),
            ", ".join(f"{deck} ×{n}" for deck, n in appeared.most_common(5)),
        )
    return True


def print_stats(con: sqlite3.Connection) -> None:
    q = lambda sql: con.execute(sql).fetchall()
    entries = q("select count(*) from entry")[0][0]
    lemmas = q("select count(*) from lemma")[0][0]
    single = q("select count(*) from lemma where n_word_notes > 0")[0][0]
    print(f"notes: {entries}   lemmas: {lemmas}   single-word: {single}")
    print(f"collection: {_meta_get(con, 'collection')}")
    print("\nby level:")
    for level, n in q(
        "select coalesce(level,'—'), count(*) from lemma group by 1 order by 2 desc"
    ):
        print(f"  {level:6} {n}")
    print("\nby part of speech:")
    for pos, n in q(
        "select coalesce(pos,'—'), count(*) from lemma group by 1 order by 2 desc"
    ):
        print(f"  {pos:16} {n}")
    print("\nby note type:")
    for nt, n in q("select notetype, count(*) from entry group by 1 order by 2 desc"):
        print(f"  {nt:34} {n}")
    print("\ntop 20 topics:")
    counter: collections.Counter = collections.Counter()
    for (topics,) in q("select topics from lemma"):
        counter.update(json.loads(topics))
    for topic, n in counter.most_common(20):
        print(f"  {topic:22} {n}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="anki_vocab.index")
    ap.add_argument("--rebuild", action="store_true", help="full rebuild")
    ap.add_argument("--refresh", action="store_true", help="incremental refresh")
    ap.add_argument("--stats", action="store_true", help="show summary")
    ap.add_argument("--prune-cache", action="store_true",
                    help="drop words from the lemma cache that appear in no "
                         "indexed note")
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
            print(f"lemma cache: was {before}, now {after}, dropped {before - after}")
        if args.rebuild:
            build(con, full=True)
        elif args.refresh:
            if is_stale(con):
                build(con, full=False)
            else:
                print("collection unchanged — index up to date")
        if args.stats or not (args.rebuild or args.refresh or args.prune_cache):
            print_stats(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
