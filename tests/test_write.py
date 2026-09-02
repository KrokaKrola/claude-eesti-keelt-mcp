"""Разворачивание слова в заметки. Anki для этих тестов не нужен."""
from __future__ import annotations

import pytest

from anki_vocab.write import (
    MAX_WORDS,
    NoteDataError,
    build_topic,
    form_labels,
    level_tag,
    topic_words,
    write_topic,
)

COURSE = "kursuse eesti keel::A2"
TOPIC = "09. Vaba aeg"

JOGI = {
    "word": "jõgi",
    "pos": "существительное",
    "ru": "река",
    "forms": ["Jõgi", "jõe", "jõge"],
    "ex": [
        ["Jõgi on suvel väga soe.", "Река летом очень тёплая."],
        ["Jõe kaldal on ilus jalutada.", "Приятно гулять по берегу реки."],
        ["Me ületasime paadiga jõge.", "Мы переправлялись на лодке через реку."],
    ],
    "sentence": ["Meile meeldib jõe ääres kalastada.", "Нам нравится рыбачить у реки."],
}

KOGU = {
    "word": "kogu",
    "pos": "местоимение",
    "ru": "весь",
    "forms": ["Kogu"],
    "ex": [["Kogu klass läks ekskursioonile.", "Весь класс поехал на экскурсию."]],
    "sentence": ["Kogu perekond käis matkal.", "Вся семья ходила в поход."],
}


def build(words, topic=TOPIC, **kw):
    return build_topic(COURSE, topic, words, **kw)


def test_topic_words_strips_number_and_sorts():
    assert topic_words("09. Vaba aeg") == ["aeg", "vaba"]
    assert topic_words("10. Liikumine on elu") == ["elu", "liikumine", "on"]
    assert topic_words("Vaba aeg") == ["aeg", "vaba"]


def test_level_from_course_tail():
    assert level_tag("kursuse eesti keel::A2") == "A2"
    assert level_tag("kursuse eesti keel::b1") == "B1"
    assert level_tag("minu eesti keel") is None


def test_labels_by_pos_and_invariance():
    assert form_labels("глагол", ["ujuma", "ujuda", "ujun"])[2] == "3-я форма (mina-vorm)"
    assert form_labels("существительное", ["jõgi", "jõe", "jõge"])[1] == "2-я форма (генитив)"
    # Совпадающие формы у существительного — это ещё не неизменяемость.
    assert form_labels("существительное", ["Arvuti", "arvuti", "arvutit"]) is not None
    assert form_labels("наречие", ["edasi", "edasi", "edasi"]) is None


def test_three_decks_and_counts():
    batches, warnings = build([JOGI])
    assert [b.deck.split("::")[-1] for b in batches] == [
        "01. sõnad", "02. sõnad typein", "03. lause typein"
    ]
    assert [len(b.notes) for b in batches] == [1, 3, 1]
    assert warnings == []


def test_deck01_fields_match_model():
    (deck01, _, _), _ = build([JOGI])
    fields = deck01.notes[0]["fields"]
    assert set(fields) == {
        "Russian", "Form1", "Form2", "Form3",
        "Ex1", "Ex1ru", "Ex2", "Ex2ru", "Ex3", "Ex3ru", "Add Reverse",
    }
    assert fields["Form1"] == "Jõgi" and fields["Form3"] == "jõge"
    assert fields["Ex2"] == "Jõe kaldal on ilus jalutada."
    assert fields["Add Reverse"] == "1"


def test_deck02_is_interleaved_word_by_word():
    """Anki показывает новые карточки в порядке добавления: три формы
    одного слова должны идти подряд."""
    (_, deck02, _), _ = build([JOGI, KOGU])
    answers = [n["fields"]["Answer"] for n in deck02.notes]
    assert answers == ["jõgi", "jõe", "jõge", "kogu"]


def test_deck02_answers_are_lowercase_with_labels():
    (_, deck02, _), _ = build([JOGI])
    assert [n["fields"]["Form"] for n in deck02.notes] == [
        "1-я форма (номинатив)", "2-я форма (генитив)", "3-я форма (партитив)"
    ]
    assert deck02.notes[0]["fields"]["Answer"] == "jõgi"
    assert deck02.notes[0]["fields"]["Example_ET"] == "Jõgi on suvel väga soe."


def test_invariant_word_gets_one_note_without_label():
    (deck01, deck02, _), _ = build([KOGU])
    assert deck01.notes[0]["fields"]["Form3"] == "Kogu"
    assert len(deck02.notes) == 1
    assert deck02.notes[0]["fields"]["Form"] == ""


def test_deck03_is_a_fresh_sentence():
    (_, _, deck03), _ = build([JOGI])
    fields = deck03.notes[0]["fields"]
    assert fields["Answer"] == "Meile meeldib jõe ääres kalastada."
    assert fields["Russian"] == "Нам нравится рыбачить у реки."
    assert fields["Example_ET"] == "" and fields["Form"] == ""


def test_tags():
    (deck01, deck02, deck03), _ = build([JOGI])
    assert deck01.notes[0]["tags"] == ["A2", "aeg", "vaba", "существительное"]
    assert deck02.notes[0]["tags"] == deck01.notes[0]["tags"]
    assert deck03.notes[0]["tags"] == ["A2", "aeg", "lause", "vaba"]


def test_all_problems_reported_at_once():
    broken = [
        {**JOGI, "forms": ["Jõgi", "jõe"]},
        {**KOGU, "ru": "", "pos": "штуковина"},
    ]
    with pytest.raises(NoteDataError) as exc:
        build(broken)
    problems = exc.value.problems
    assert len(problems) == 3
    assert any("forms" in p for p in problems)
    assert any("перевод" in p for p in problems)
    assert any("pos=" in p for p in problems)


def test_duplicate_word_in_one_batch():
    with pytest.raises(NoteDataError, match="дважды"):
        build([JOGI, JOGI])


def test_batch_cap():
    with pytest.raises(NoteDataError, match="Раздели"):
        build([{**JOGI, "word": f"sõna{i}"} for i in range(MAX_WORDS + 1)])


def test_pos_synonyms_accepted():
    (deck01, _, _), _ = build([{**JOGI, "pos": "noun"}])
    assert "существительное" in deck01.notes[0]["tags"]


def test_form_not_in_example_warns_but_does_not_block():
    seen = []

    def contains(form, sentence):
        seen.append((form, sentence))
        return form.lower() in sentence.lower()

    batches, warnings = build([{**JOGI, "forms": ["Jõgi", "jõe", "kala"]}],
                              form_in_example=contains)
    assert len(batches[0].notes) == 1  # записи не мешает
    assert len(warnings) == 1 and "форма 3" in warnings[0]
    assert len(seen) == 3


def test_dry_run_touches_no_network():
    out = write_topic(COURSE, TOPIC, [JOGI], dry_run=True)
    assert "ничего не записано" in out
    assert "01. sõnad" in out
