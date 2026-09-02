import sqlite3

import pytest

from anki_vocab.index import SCHEMA, reconcile_decks


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.create_collation("unicase", lambda a, b: (a > b) - (a < b))
    c.executescript(SCHEMA)
    c.executemany(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values (?,?,?,'[]','','x','x','[]',0,0)",
        [(1, "Basic", "курс::A2::07. Aias"),
         (2, "Basic", "курс::A2::07. Aias"),
         (3, "Basic", "курс::A2::12. Poes")],
    )
    return c


def test_rename_is_picked_up(con):
    """Переименование колоды не меняет notes.mod, поэтому инкремент
    по mod его не видит — колоды сверяются отдельно."""
    deck_of = {1: "курс::A2::07. Aias UUS", 2: "курс::A2::07. Aias UUS",
               3: "курс::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 2
    decks = [d for (d,) in con.execute("select deck from entry order by note_id")]
    assert decks == ["курс::A2::07. Aias UUS", "курс::A2::07. Aias UUS",
                     "курс::A2::12. Poes"]


def test_move_between_decks_is_picked_up(con):
    """change_deck двигает cards.did, а notes.mod не трогает —
    а перенос делает сам скилл при создании подколод."""
    deck_of = {1: "курс::A2::12. Poes", 2: "курс::A2::07. Aias",
               3: "курс::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 1
    assert con.execute("select deck from entry where note_id=1").fetchone()[0] \
        == "курс::A2::12. Poes"


def test_nothing_to_do_is_free(con):
    deck_of = {1: "курс::A2::07. Aias", 2: "курс::A2::07. Aias",
               3: "курс::A2::12. Poes"}
    assert reconcile_decks(con, deck_of) == 0


def test_note_missing_from_map_is_left_alone(con):
    """Заметку, которой нет в карте колод, не трогаем: её удалением
    занимается сверка области видимости, а не эта функция."""
    assert reconcile_decks(con, {1: "курс::A2::07. Aias"}) == 0
