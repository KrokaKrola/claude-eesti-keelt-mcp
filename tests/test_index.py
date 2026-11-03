import collections
import json
import logging
import sqlite3

import pytest

from anki_vocab import collection, index
from anki_vocab.index import (
    SCHEMA,
    _headword_lemmas,
    ensure_fresh,
    is_stale,
    open_index,
    reconcile_decks,
    refresh_stats,
)


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.create_collation("unicase", lambda a, b: (a > b) - (a < b))
    c.executescript(SCHEMA)
    c.executemany(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values (?,?,?,'[]','','x','x','[]',0,0)",
        [(1, "Basic", "course::A2::07. Aias"),
         (2, "Basic", "course::A2::07. Aias"),
         (3, "Basic", "course::A2::12. Poes")],
    )
    return c


def test_rename_is_picked_up(con):
    """Renaming a deck doesn't change notes.mod, so the mod-based increment
    doesn't see it — decks are reconciled separately."""
    deck_of = {1: "course::A2::07. Aias UUS", 2: "course::A2::07. Aias UUS",
               3: "course::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 2
    decks = [d for (d,) in con.execute("select deck from entry order by note_id")]
    assert decks == ["course::A2::07. Aias UUS", "course::A2::07. Aias UUS",
                     "course::A2::12. Poes"]


def test_move_between_decks_is_picked_up(con):
    """change_deck moves cards.did and leaves notes.mod alone —
    and the skill itself moves notes when creating subdecks."""
    deck_of = {1: "course::A2::12. Poes", 2: "course::A2::07. Aias",
               3: "course::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 1
    assert con.execute("select deck from entry where note_id=1").fetchone()[0] \
        == "course::A2::12. Poes"


def test_nothing_to_do_is_free(con):
    deck_of = {1: "course::A2::07. Aias", 2: "course::A2::07. Aias",
               3: "course::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 0


def test_note_missing_from_map_is_left_alone(con):
    """A note missing from the deck map is left alone: removing it is the
    scope reconciliation's job, not this function's."""
    assert reconcile_decks(con, {1: "course::A2::07. Aias"}) == 0


def test_review_stats_are_refreshed_without_a_note_edit(con):
    """A card review doesn't change notes.mod, so the increment doesn't
    re-read the note — and the "hard words" hint lives on exactly these numbers."""
    assert refresh_stats(con, {1: (45.0, 12, 3), 2: (0.0, 0, 0)}) == 3
    rows = dict(
        (nid, (ivl, reps, lapses))
        for nid, ivl, reps, lapses in con.execute(
            "select note_id, ivl, reps, lapses from entry"
        )
    )
    assert rows[1] == (45.0, 12, 3)
    assert rows[3] == (0.0, 0, 0), "notes without cards are zeroed, not dropped"


def test_version_bump_leaves_no_index_pretending_to_be_fresh(tmp_path):
    """A version change drops the tables. The collection fingerprint used to
    survive — and refresh answered "collection unchanged" over an empty
    index: the server silently knew zero words, everything looked new."""
    path = tmp_path / "vocab.sqlite"
    con = open_index(path)
    con.execute(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values"
        " (1,'Basic','course::A2::07. Aias','[]','','x','x','[]',0,0)"
    )
    # A real fingerprint: with a fake one is_stale would return True for
    # another reason and the test would pass even with a broken version check.
    con.execute(
        "insert or replace into meta values ('fingerprint', ?)",
        (json.dumps(collection.fingerprint()),),
    )
    con.commit()
    con.close()

    con = sqlite3.connect(path)
    con.execute("update meta set value = '0' where key = 'index_version'")
    con.commit()
    con.close()

    con = open_index(path)
    assert con.execute("select count(*) from entry").fetchone()[0] == 0
    assert is_stale(con), "an empty index must count as stale"


@pytest.fixture
def lem():
    from anki_vocab.lemma import Lemmatizer

    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return Lemmatizer(c)


def test_headword_of_a_basic_card_is_recognised(lem):
    """For Basic, parsing picks the Estonian side by Latin word count and
    yields the back with the example — the headword was lost, and a word
    with a card ended up in the debt."""
    assert _headword_lemmas("hommik\x1fутро Hommik on ilus.", lem) == {"hommik"}


def test_headword_skips_sentences_and_translations(lem):
    assert _headword_lemmas("Ma tahan lugeda raamatut.\x1fЯ хочу", lem) == set()
    assert _headword_lemmas("ребёнок\x1flaps", lem) == set()
    assert _headword_lemmas("", lem) == set()


def test_headword_keeps_phrasal_verbs(lem):
    """Two words are allowed for 'kaasa elama' — it's a vocabulary unit."""
    assert _headword_lemmas("kaasa elama\x1fболеть за", lem) == {"kaasa", "elama"}


@pytest.fixture
def rebuild(monkeypatch):
    """Refresh without a collection: only the verdict it logs matters."""
    def fake(appeared=(), stored=0):
        stats = {
            "stored": stored, "moved": 0, "removed": 0, "lemmas": 0,
            "seconds": 0.1, "appeared": collections.Counter(appeared),
        }
        monkeypatch.setattr(index, "is_stale", lambda con: True)
        monkeypatch.setattr(index, "build", lambda con, full, quiet: stats)
    return fake


def test_writes_past_the_server_are_reported(rebuild, caplog):
    rebuild(appeared=["course::A2::12. Poes::03. lause typein"] * 12, stored=12)
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None)
    assert "bypassing the server" in caplog.text
    assert "12" in caplog.text


def test_own_write_is_not_reported_as_foreign(rebuild, caplog):
    rebuild(appeared=["course::A2::12. Poes::03. lause typein"] * 12, stored=12)
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None, own_write=True)
    assert "bypassing the server" not in caplog.text


def test_rebuild_that_changed_nothing_stays_out_of_the_log(rebuild, caplog):
    """A running Anki touches -wal on every answer: the fingerprint diverges
    a dozen times per session, and at INFO that's just noise."""
    rebuild()
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None)
    assert caplog.text == ""
