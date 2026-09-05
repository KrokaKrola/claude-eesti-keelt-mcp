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
    batches, warnings, _dose = build_topic(COURSE, topic, words, **kw)
    return batches, warnings


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
    assert any("empty 'ru'" in p for p in problems)
    assert any("pos=" in p for p in problems)


def test_duplicate_word_in_one_batch():
    with pytest.raises(NoteDataError, match="appears twice"):
        build([JOGI, JOGI])


def test_batch_cap():
    with pytest.raises(NoteDataError, match="Split into several calls"):
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
    assert len(warnings) == 1 and "form 3" in warnings[0]
    assert len(seen) == 3


def test_dry_run_touches_no_network():
    out, _dose = write_topic(COURSE, TOPIC, [JOGI], dry_run=True)
    assert "dry run, nothing written" in out
    assert "01. sõnad" in out


# --- доза новых слов колоды 03 ---------------------------------------------


def fake_novel(new_by_sentence: dict[str, tuple[list[str], list[str]]]):
    """Подставной разбор: тесты дозы про арифметику и отчёт, а не про
    морфологию — Vabamorf сюда тянуть незачем."""

    def find(sentence, taught):
        return new_by_sentence.get(sentence, ([], []))

    return find


def dose_of(words, novel, topic=TOPIC):
    _batches, _warnings, dose = build_topic(
        COURSE, topic, words, novel_words=fake_novel(novel)
    )
    return dose


def test_dose_not_counted_without_the_callback():
    _batches, _warnings, dose = build_topic(COURSE, TOPIC, [JOGI])
    assert dose.counted is False
    assert "deck-03" not in write_topic(
        COURSE, TOPIC, [JOGI], dry_run=True
    )[0]


def test_expected_dose_scales_with_topic_size_and_stops_at_the_cap():
    from anki_vocab.write import expected_dose

    assert expected_dose(0) == 0
    assert expected_dose(1) == 1      # маленькая тема всё равно получает одно
    assert expected_dose(3) == 1
    assert expected_dose(9) == 3
    assert expected_dose(30) == 5     # потолок NEW_MAX


def test_dose_collects_new_words_and_marks_a_shortfall():
    words = [JOGI, KOGU]
    dose = dose_of(words, {JOGI["sentence"][0]: ([], [])})
    assert dose.counted and dose.new == [] and dose.short
    assert dose.expected == 1


def test_dose_counts_new_words_and_keeps_half_known_apart():
    dose = dose_of(
        [JOGI],
        {JOGI["sentence"][0]: (["ümbrik"], ["sall"])},
    )
    assert dose.new == ["ümbrik"] and dose.half == ["sall"]
    assert not dose.short


def test_two_new_words_in_one_sentence_are_flagged():
    et = JOGI["sentence"][0]
    dose = dose_of([JOGI], {et: (["ümbrik", "tšekk"], [])})
    assert dose.crowded == [et]


def test_dose_line_reaches_the_report():
    out, dose = write_topic(
        COURSE, TOPIC, [JOGI, KOGU],
        novel_words=fake_novel({JOGI["sentence"][0]: (["ümbrik"], ["sall"])}),
        dry_run=True,
    )
    assert "deck-03 new words: 1 of 1 expected — ümbrik" in out
    assert "half-known" in out and "sall" in out
    assert "⚠ short" not in out
    assert dose.new == ["ümbrik"]


def test_short_dose_line_explains_itself():
    out, _dose = write_topic(
        COURSE, TOPIC, [JOGI], novel_words=fake_novel({}), dry_run=True
    )
    assert "deck-03 new words: 0 of 1 expected ⚠ short — none" in out
    assert "teaches nothing beyond its target word" in out


def test_dose_does_not_count_the_same_word_twice():
    dose = dose_of(
        [JOGI, KOGU],
        {JOGI["sentence"][0]: (["ümbrik"], []),
         KOGU["sentence"][0]: (["ümbrik"], [])},
    )
    assert dose.new == ["ümbrik"]


# --- однообразие ----------------------------------------------------------


def test_repeat_check_sees_every_sentence_of_the_topic():
    """Считать надо и примеры колоды 01, и предложения колоды 03 —
    однообразие живёт в обеих."""
    seen: dict = {}

    def spy(sentences, taught):
        seen["sentences"], seen["taught"] = sentences, taught
        return []

    write_topic(COURSE, TOPIC, [JOGI], repeat_check=spy, dry_run=True)
    assert seen["sentences"] == [et for et, _ru in JOGI["ex"]] + [JOGI["sentence"][0]]
    assert seen["taught"] == {"jõgi"}


def test_repeat_check_counts_an_invariant_example_once():
    """У несклоняемого слова один пример размножен на три слота — иначе
    каждое его слово выглядело бы троекратным повтором."""
    seen: list = []
    write_topic(COURSE, TOPIC, [KOGU],
                repeat_check=lambda s, t: seen.extend(s) or [], dry_run=True)
    assert seen == [KOGU["ex"][0][0], KOGU["sentence"][0]]


def test_repeat_line_reaches_the_report():
    from anki_vocab.query import Repeat

    out, _dose = write_topic(
        COURSE, TOPIC, [JOGI],
        repeat_check=lambda s, t: [Repeat("panema", 3, 20, crowded=True)],
        dry_run=True,
    )
    assert "leaning too hard on: panema ×3 (+20 already in the topic)" in out


def test_report_stays_quiet_without_repeats():
    out, _dose = write_topic(
        COURSE, TOPIC, [JOGI], repeat_check=lambda s, t: [], dry_run=True
    )
    assert "leaning" not in out
