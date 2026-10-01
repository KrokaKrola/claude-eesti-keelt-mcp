"""Сверка форм со словарём EKI — без сети, на парадигмах из живых ответов
Ekilex (сокращены до нужных кодов)."""
import json
import sqlite3

import pytest

from anki_vocab import ekilex
from anki_vocab.ekilex import Dictionary, Paradigm, check_forms
from anki_vocab.index import SCHEMA
from anki_vocab.write import NOUN_LABELS, VERB_LABELS, NoteDataError, build_topic

KUDUMA = [Paradigm("kuduma", "verb", {"Sup": ["kuduma"], "Inf": ["kududa"], "IndPrSg1": ["koon"]})]
KESKKUTE = [Paradigm("keskküte", "noomen", {"SgN": ["keskküte"], "SgG": ["keskkütte"], "SgP": ["keskkütet"]})]
ARMAS = [
    Paradigm("armas", "noomen", {"SgN": ["armas"], "SgG": ["armsa"], "SgP": ["armast"], "PlP": ["armsaid"]}),
    Paradigm("armas", "noomen", {"SgN": ["armas"], "SgG": ["armsa"], "SgP": ["armsat"], "PlP": ["armsaid"]}),
]
PUKSID = [Paradigm("püksid", "noomen", {
    "SgN": ["püks"], "SgG": ["püksi"], "SgP": ["püksi"], "PlP": ["pükse", "püksisid"],
    "PlN": ["püksid"], "PlG": ["pükste"],
})]
EDASI = [Paradigm("edasi", "muutumatu", {})]


def test_wrong_verb_form_is_rejected_with_the_right_one():
    """'kudub' вместо koob так и записалось в 14. Riidepoes."""
    wrong, _ = check_forms("kuduma", True, ["Kuduma", "kududa", "kudun"], KUDUMA, VERB_LABELS)
    assert wrong == ["kuduma: 3-я форма (mina-vorm) 'kudun' — the dictionary has koon"]


def test_mass_noun_cannot_get_a_fourth_form():
    wrong, _ = check_forms("keskküte", False,
                           ["Keskküte", "keskkütte", "keskkütet", "keskkütteid"],
                           KESKKUTE, NOUN_LABELS)
    assert wrong == ["keskküte: the dictionary has no partitive plural — send 3 forms"]
    assert check_forms("keskküte", False, ["keskküte", "keskkütte", "keskkütet"],
                       KESKKUTE, NOUN_LABELS) == ([], [])


def test_every_dictionary_variant_passes():
    for partitive in ("armsat", "armast"):
        assert check_forms("armas", False, ["Armas", "armsa", partitive, "armsaid"],
                           ARMAS, NOUN_LABELS) == ([], [])


def test_plural_only_word_may_be_written_in_the_plural():
    assert check_forms("püksid", False, ["Püksid", "pükste", "pükse"],
                       PUKSID, NOUN_LABELS) == ([], [])


def test_unknown_word_only_warns():
    wrong, unsure = check_forms("kaerahelbepuuder", False, ["a", "b", "c"], [], NOUN_LABELS)
    assert wrong == [] and "not in the EKI dictionary" in unsure[0]


def test_pos_mismatch_is_named():
    wrong, _ = check_forms("edasi", True, ["a", "b", "c"], EDASI, VERB_LABELS)
    assert "no verb" in wrong[0]


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def test_cache_spares_the_network(con, monkeypatch):
    calls = []

    def fake(word, key):
        calls.append(word)
        return [{"word": word, "word_class": "verb", "forms": {"Sup": [word]}}]

    monkeypatch.setattr(ekilex, "fetch", fake)
    Dictionary(con, key="k").lookup_many(["kuduma", "Kuduma"])
    Dictionary(con, key="k").lookup_many(["kuduma"])
    assert calls == ["kuduma"]


def test_no_key_is_an_error_not_an_empty_dictionary(con):
    """«Словарь не спросили» и «слова нет в словаре» должны различаться."""
    with pytest.raises(ekilex.EkilexError):
        Dictionary(con, key="").lookup_many(["kuduma"])


def test_cached_answer_needs_no_key(con):
    con.execute("insert into ekilex_cache values (?, ?, 0)",
                ("kuduma", json.dumps([{"word": "kuduma", "word_class": "verb", "forms": {}}])))
    assert Dictionary(con, key="").lookup_many(["kuduma"])["kuduma"][0].word_class == "verb"


def test_dictionary_rejection_stops_the_whole_write():
    word = {
        "word": "kuduma", "pos": "глагол", "ru": "вязать",
        "forms": ["Kuduma", "kududa", "kudun"],
        "ex": [["a", "a"], ["b", "b"], ["c", "c"]], "sentence": ["d", "d"],
    }
    with pytest.raises(NoteDataError) as exc:
        build_topic("kursuse eesti keel::A2", "99. Test", [word],
                    form_check=lambda words: (["kuduma: wrong"], []))
    assert exc.value.problems == ["kuduma: wrong"]
