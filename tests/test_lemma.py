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
        ("kübarat", "kübar"),   # partitive
        ("jõe", "jõgi"),        # grade alternation
        ("kleidi", "kleit"),    # t → d inside the stem
        ("aega", "aeg"),
        ("kalastada", "kalastama"),
        ("koerad", "koer"),
        ("salli", "sall"),   # disambiguation leaves only the verb sallima
    ],
)
def test_inflected_forms_resolve(lem, word, expected):
    assert expected in lem.lemmas(word)


def test_pos_hint_picks_matching_analysis(lem):
    """Vabamorf usually disambiguates itself and returns one lemma.

    The POS hint is a safeguard for when there are several variants.
    We test the choice itself, not relying on a specific word.
    """
    lem.mem["teed"] = ((("tee", "S"), ("tegema", "V")), ())
    assert lem.lemma("teed", "глагол") == "tegema"
    assert lem.lemma("teed", "существительное") == "tee"
    assert lem.lemma("teed") == "tee", "without a hint take the first analysis"


def test_pos_hint_falls_back_to_undisambiguated_variants(lem):
    """Context-free parsing: for 'salli' disambiguation leaves only the verb
    sallima, while on the card it's the noun "scarf". The POS tag must
    reach the discarded analysis."""
    assert lem.lemma("salli", "существительное") == "sall"
    assert lem.lemma("salli") == "sallima", "without a hint — the primary analysis"


def test_pos_hint_never_breaks_a_single_analysis(lem):
    assert lem.lemma("kübarat", "глагол") == "kübar"


def test_cache_survives_second_call(lem):
    lem.lemma("kübarat")
    before = lem.misses
    lem.lemma("kübarat")
    assert lem.misses == before and lem.hits >= 1


def test_proper_name_is_recognised_whatever_the_case(lem):
    """The cache is keyed by lowercase, and the answer used to depend on
    which form the analyzer happened to see first."""
    lem.analyses("tallinn")  # warm the cache with the lowercase form
    assert lem.is_name("tallinn")
    assert not lem.is_name("koer"), "a capitalized common noun is not a name"
    assert not lem.is_name("mari"), "the name has a common-noun reading — keep it"


def test_frozen_case_adverb_still_reaches_the_noun(lem):
    """'lukku' is both an adverb and the illative of lukk. text_check on 01.10
    counted it as a new word even though lukk was among the targets."""
    assert "lukk" in lem.lemmas("lukku")


def test_dictionary_form_still_wins_over_foreign_variants(lem):
    """The adverb exception must not break the 'koli' rule."""
    assert "kolima" not in lem.lemmas("koli")


@pytest.mark.parametrize(
    "comparative,positive",
    [("olulisem", "oluline"), ("magusam", "magus"), ("tervislikum", "tervislik"),
     ("suurem", "suur"), ("pikem", "pikk"), ("soojem", "soe"), ("parem", "hea")],
)
def test_comparative_maps_to_its_adjective(lem, comparative, positive):
    assert lem.lemma(comparative) == positive


def test_typo_is_not_in_the_dictionary(lem):
    """'söi' for 'sõi', 'mureled' for 'murelid' — both went into Anki."""
    assert not lem.in_dictionary("söi")
    assert not lem.in_dictionary("mureled")
    assert lem.in_dictionary("sõi")
    assert lem.in_dictionary("kaerahelbepuder"), "the dictionary assembles compounds itself"


def test_context_resolves_what_a_lone_word_cannot(lem):
    tokens = "Ta pani kingad jalga".split()
    assert lem.sentence(tokens)[-1][0] == "jalg"
    assert lem.analyses("jalga")[0][0] == "jalga", "alone — an adverb"


def test_context_keeps_proper_names(lem):
    lemma, code = lem.sentence("Minu sõber on pärit Lätist".split())[-1]
    assert code == "H"
