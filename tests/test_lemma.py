import sqlite3

import pytest

from anki_vocab.lemma import Lemmatizer


@pytest.fixture
def lem():
    con = sqlite3.connect(":memory:")
    con.execute("create table lemma_cache (word text primary key, analyses text not null)")
    return Lemmatizer(con)


@pytest.mark.parametrize(
    "word,expected",
    [
        ("kübarat", "kübar"),   # партитив
        ("jõe", "jõgi"),        # чередование ступени
        ("kleidi", "kleit"),    # t → d внутри основы
        ("aega", "aeg"),
        ("kalastada", "kalastama"),
        ("koerad", "koer"),
        ("salli", "sall"),   # снятие омонимии оставляет только глагол sallima
    ],
)
def test_inflected_forms_resolve(lem, word, expected):
    assert expected in lem.lemmas(word)


def test_pos_hint_picks_matching_analysis(lem):
    """Vabamorf обычно сам снимает омонимию и отдаёт одну лемму.

    Подсказка части речи — страховка на случай, когда вариантов
    несколько. Проверяем сам выбор, не полагаясь на конкретное слово.
    """
    lem.mem["teed"] = ((("tee", "S"), ("tegema", "V")), ())
    assert lem.lemma("teed", "глагол") == "tegema"
    assert lem.lemma("teed", "существительное") == "tee"
    assert lem.lemma("teed") == "tee", "без подсказки берём первый разбор"


def test_pos_hint_falls_back_to_undisambiguated_variants(lem):
    """Разбор без контекста: у 'salli' снятие омонимии оставляет один
    глагол sallima, а на карточке это существительное «шарф». Тег части
    речи должен дотянуться до отброшенного разбора."""
    assert lem.lemma("salli", "существительное") == "sall"
    assert lem.lemma("salli") == "sallima", "без подсказки — основной разбор"


def test_pos_hint_never_breaks_a_single_analysis(lem):
    assert lem.lemma("kübarat", "глагол") == "kübar"


def test_cache_survives_second_call(lem):
    lem.lemma("kübarat")
    before = lem.misses
    lem.lemma("kübarat")
    assert lem.misses == before and lem.hits >= 1


def test_proper_name_is_recognised_whatever_the_case(lem):
    """Кэш ключуется по нижнему регистру, и раньше ответ зависел от того,
    в каком виде слово попалось анализатору первым."""
    lem.analyses("tallinn")  # прогреваем кэш строчным вариантом
    assert lem.is_name("tallinn")
    assert not lem.is_name("koer"), "нарицательное с большой буквы — не имя"
    assert not lem.is_name("mari"), "у имени есть нарицательный разбор — оставляем"


def test_frozen_case_adverb_still_reaches_the_noun(lem):
    """'lukku' — наречие и иллатив от lukk разом. text_check 01.10 считал
    его новым словом, хотя lukk был среди целевых."""
    assert "lukk" in lem.lemmas("lukku")


def test_dictionary_form_still_wins_over_foreign_variants(lem):
    """Исключение для наречий не должно сломать правило 'koli'."""
    assert "kolima" not in lem.lemmas("koli")


@pytest.mark.parametrize(
    "comparative,positive",
    [("olulisem", "oluline"), ("magusam", "magus"), ("tervislikum", "tervislik"),
     ("suurem", "suur"), ("pikem", "pikk"), ("soojem", "soe"), ("parem", "hea")],
)
def test_comparative_maps_to_its_adjective(lem, comparative, positive):
    assert lem.lemma(comparative) == positive


def test_typo_is_not_in_the_dictionary(lem):
    """'söi' вместо 'sõi', 'mureled' вместо 'murelid' — обе ушли в Anki."""
    assert not lem.in_dictionary("söi")
    assert not lem.in_dictionary("mureled")
    assert lem.in_dictionary("sõi")
    assert lem.in_dictionary("kaerahelbepuder"), "сложное слово словарь собирает сам"


def test_context_resolves_what_a_lone_word_cannot(lem):
    tokens = "Ta pani kingad jalga".split()
    assert lem.sentence(tokens)[-1][0] == "jalg"
    assert lem.analyses("jalga")[0][0] == "jalga", "по одному — наречие"


def test_context_keeps_proper_names(lem):
    lemma, code = lem.sentence("Minu sõber on pärit Lätist".split())[-1]
    assert code == "H"
