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


def test_review_stats_are_refreshed_without_a_note_edit(con):
    """Повтор карточки не меняет notes.mod, поэтому инкремент заметку не
    перечитывает — а подсказка «трудные слова» живёт ровно на этих числах."""
    assert refresh_stats(con, {1: (45.0, 12, 3), 2: (0.0, 0, 0)}) == 3
    rows = dict(
        (nid, (ivl, reps, lapses))
        for nid, ivl, reps, lapses in con.execute(
            "select note_id, ivl, reps, lapses from entry"
        )
    )
    assert rows[1] == (45.0, 12, 3)
    assert rows[3] == (0.0, 0, 0), "заметки без карточек обнуляются, а не пропадают"


def test_version_bump_leaves_no_index_pretending_to_be_fresh(tmp_path):
    """Смена версии сносит таблицы. Отпечаток коллекции при этом раньше
    оставался — и досборка отвечала «коллекция не менялась» поверх пустого
    индекса: сервер молча знал ноль слов, всё выглядело новым."""
    path = tmp_path / "vocab.sqlite"
    con = open_index(path)
    con.execute(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values"
        " (1,'Basic','курс::A2::07. Aias','[]','','x','x','[]',0,0)"
    )
    # Отпечаток настоящий: с выдуманным is_stale вернул бы True по
    # другой причине и тест прошёл бы даже со сломанной проверкой версии.
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
    assert is_stale(con), "пустой индекс обязан считаться устаревшим"


@pytest.fixture
def lem():
    from anki_vocab.lemma import Lemmatizer

    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return Lemmatizer(c)


def test_headword_of_a_basic_card_is_recognised(lem):
    """У Basic разбор выбирает эстонскую сторону по числу латинских слов
    и отдаёт заднюю с примером — заголовок терялся, и слово с карточкой
    уезжало в долг."""
    assert _headword_lemmas("hommik\x1fутро Hommik on ilus.", lem) == {"hommik"}


def test_headword_skips_sentences_and_translations(lem):
    assert _headword_lemmas("Ma tahan lugeda raamatut.\x1fЯ хочу", lem) == set()
    assert _headword_lemmas("ребёнок\x1flaps", lem) == set()
    assert _headword_lemmas("", lem) == set()


def test_headword_keeps_phrasal_verbs(lem):
    """Два слова разрешены ради 'kaasa elama' — это словарная единица."""
    assert _headword_lemmas("kaasa elama\x1fболеть за", lem) == {"kaasa", "elama"}


@pytest.fixture
def rebuild(monkeypatch):
    """Досборка без коллекции: интересен только вердикт, который она пишет."""
    def fake(appeared=(), stored=0):
        stats = {
            "stored": stored, "moved": 0, "removed": 0, "lemmas": 0,
            "seconds": 0.1, "appeared": collections.Counter(appeared),
        }
        monkeypatch.setattr(index, "is_stale", lambda con: True)
        monkeypatch.setattr(index, "build", lambda con, full, quiet: stats)
    return fake


def test_writes_past_the_server_are_reported(rebuild, caplog):
    rebuild(appeared=["курс::A2::12. Poes::03. lause typein"] * 12, stored=12)
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None)
    assert "мимо сервера" in caplog.text
    assert "12" in caplog.text


def test_own_write_is_not_reported_as_foreign(rebuild, caplog):
    rebuild(appeared=["курс::A2::12. Poes::03. lause typein"] * 12, stored=12)
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None, own_write=True)
    assert "мимо сервера" not in caplog.text


def test_rebuild_that_changed_nothing_stays_out_of_the_log(rebuild, caplog):
    """Открытый Anki трогает -wal на каждом ответе: отпечаток расходится
    по десятку раз за сессию, и в INFO от этого одна вода."""
    rebuild()
    with caplog.at_level(logging.INFO, logger="anki_vocab"):
        ensure_fresh(None)
    assert caplog.text == ""
