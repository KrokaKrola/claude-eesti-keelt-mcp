import sqlite3

import pytest

from anki_vocab.index import SCHEMA
from anki_vocab.query import KNOWN, SEEN, UNKNOWN, Vocab, format_text_check


@pytest.fixture
def vocab():
    con = sqlite3.connect(":memory:")
    con.create_collation("unicase", lambda a, b: (a > b) - (a < b))
    con.executescript(SCHEMA)
    rows = [
        # lemma, ru, pos, level, topics, forms, n_notes, n_word_notes
        # lemma, ru, pos, level, topics, forms, n_notes, n_word_notes, strength
        ("jõgi", "река", "существительное", "a2", '["vaba"]', '["jõgi","jõe","jõge"]', 4, 4, 90.0),
        ("kübar", "шляпа", "существительное", "a1", '["riided"]', '["kübar","kübara"]', 2, 2, 10.0),
        ("kalastama", None, None, "a2", "[]", '["kalastab"]', 1, 0, 0.0),  # только из фразы
        ("õun", "яблоко", "существительное", "a2", '["еда"]', '["õun"]', 4, 4, 120.0),
    ]
    con.executemany(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,first_seen) values (?,?,?,?,?,?,?,?,?,0)", rows)
    con.executemany("insert into form(form,lemma,note_id,slot) values (?,?,?,'F')",
                    [("jõe", "jõgi", 1), ("kübarat", "kübar", 2)])
    # Палитра берётся с карточек, а не из лемм: заголовок карточки — уже
    # словарная форма. 'Kindad' и 'Kaasa elama' проверяют именно это:
    # морфология сделала бы из них 'kinnas' и три записи вместо одной.
    cards = [
        (101, "a2", "существительное", '["vaba"]', "Jõgi"),
        (107, "a2", "существительное", '["kodused_tööd"]', "Lapp"),
        (108, None, "существительное", '["дом"]', "Laud"),
        (102, "a1", "существительное", '["riided"]', "Kübar"),
        (103, "a2", "существительное", '["еда"]', "Õun"),
        (104, "a2", "существительное", '["riided"]', "Kindad"),
        (105, "a2", "глагол", '["vaba"]', "Kaasa elama"),
    ]
    add_cards(con, cards)
    return Vocab(con)


def add_cards(con, cards):
    con.executemany(
        "insert into entry (note_id,notetype,deck,level,pos,topics,ru,headword,"
        "et_raw,examples,is_phrase,mod) values"
        " (?,'Estonian 3 Forms','колода',?,?,?,'перевод',?,'','[]',0,0)",
        cards,
    )


def test_exact_form_lookup(vocab):
    assert vocab.lookup("jõe").lemma == "jõgi"


def test_inflected_form_via_morphology(vocab):
    """kübarat нет в form-таблице как слово — находим лемматизацией."""
    hit = vocab.lookup("KÜBARAT")
    assert hit.lemma == "kübar" and hit.status == KNOWN


def test_sentence_only_word_is_seen_not_known(vocab):
    assert vocab.lookup("kalastama").status == SEEN


def test_unknown(vocab):
    assert vocab.lookup("xyzzy").status == UNKNOWN


def test_pool_excludes_sentence_only_words(vocab):
    topical, core, _, _ = vocab.pool()
    assert "kalastama" not in topical + core
    assert "jõgi" in topical + core


def test_pool_matches_underscored_topic_tags(vocab):
    """Старые темы помечены одним тегом 'kodused_tööd', новые — словами.
    Скилл считает теги из имени колоды словами, и без нормализации
    группа «по теме» у старых тем молча приезжала пустой."""
    topical, _, _, _ = vocab.pool(topics=["kodused", "tööd"])
    assert topical == ["lapp"]
    assert vocab.pool(topics=["kodused_tööd"])[0] == ["lapp"]


def test_pool_keeps_cards_without_a_level_tag(vocab):
    """Карточка без тега уровня — всё равно выученное слово. В колоде
    '03. minu kodu' без тега лежит вся бытовая лексика."""
    _, core, _, _ = vocab.pool(level="a2")
    assert "laud" in core


def test_pool_shows_the_card_headword_verbatim(vocab):
    """Первое поле карточки — уже словарная форма. Лемматизация делала
    из 'Kindad' → 'kinnas', а фразовый глагол размножала по формам."""
    topical, core, _, _ = vocab.pool()
    palette = topical + core
    assert "kindad" in palette and "kinnas" not in palette
    assert palette.count("kaasa elama") == 1


def test_pool_ignores_words_without_a_dictionary_card(vocab):
    """helistama человек видел только в предложениях — в палитру он не
    идёт, но знакомым остаётся: покрытие считает весь индекс."""
    vocab.con.execute(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,first_seen) values ('helistama',NULL,NULL,'a2','[]',"
        "'[\"helistage\"]',3,1,0.0,0)")
    topical, core, _, _ = vocab.pool()
    assert "helistama" not in topical + core
    assert vocab.lookup("helistama").status in (KNOWN, SEEN)


def test_pool_filters(vocab):
    # laud без тега уровня — показываем на любом: это выученное слово
    assert vocab.pool(level="a1") == ([], ["kübar", "laud"], 0, [])
    assert vocab.pool(pos="глагол") == ([], ["kaasa elama"], 0, [])


def test_pool_splits_topical_from_core(vocab):
    """У новой темы совпадений нет, и палитра выходила пустой —
    поэтому к теме всегда добавляется ядро."""
    topical, core, _, _ = vocab.pool(topics=["vaba"])
    assert topical == ["jõgi", "kaasa elama"]
    assert "õun" in core and "kübar" in core


def test_truncation_is_never_silent(vocab):
    """Любой предел прячет одну и ту же часть словаря, поэтому о срезе
    надо сказать вслух — иначе слова просто исчезают."""
    from anki_vocab.query import format_pool

    topical, core, dropped, _ = vocab.pool(limit=1)
    assert dropped == len(vocab.pool()[1]) - 1
    assert "обрезано" in format_pool(topical, core, dropped)
    assert "обрезано" not in format_pool(*vocab.pool())


def test_pool_on_unknown_topic_still_returns_core(vocab):
    topical, core, _, _ = vocab.pool(topics=["töö", "amet"])
    assert topical == []
    assert len(core) == 7, "ядро должно приехать даже при нулевом совпадении"


def test_pool_ranks_by_usage_then_retention(vocab):
    """Ходовость (n_notes) важнее и числа карточек, и интервала:
    по числу карточек сортировка падала в алфавит, а по одному интервалу
    наверх всплывали порядковые числительные."""
    vocab.con.execute(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,first_seen) values ('üheksasajas','девятисотый',NULL,'a2','[]',"
        "'[\"üheksasajas\"]',1,1,171.0,0)")
    add_cards(vocab.con, [(106, "a2", "числительное", "[]", "Üheksasajas")])
    _, core, _, _ = vocab.pool(limit=3)
    assert "üheksasajas" not in core, "лёгкое числительное не должно вытеснять рабочие слова"
    assert "jõgi" in core and "õun" in core


def test_function_words_are_not_counted(vocab):
    pct, new, content = vocab.check_sentence("See on jõgi")
    assert "see" not in content and "on" not in content
    assert pct == 100 and new == []


def test_coverage_reports_new_words(vocab):
    pct, new, _ = vocab.check_sentence("Ma nägin lennukit")
    assert new and pct < 100


def test_one_new_word_is_always_allowed(vocab):
    """Жёсткий потолок ломал естественность фраз — одно новое слово ок
    независимо от покрытия."""
    assert "низко" not in format_text_check([(20, ["lennuk"], ["a", "b"])])


def test_low_coverage_with_several_new_words_is_flagged(vocab):
    assert "низко" in format_text_check([(40, ["lennuk", "auto"], ["a", "b"])])


def test_good_coverage_passes_even_with_several_new(vocab):
    assert "низко" not in format_text_check([(85, ["lennuk", "auto"], ["a"])])


def test_new_words_are_reported_as_candidates(vocab):
    out = format_text_check([(85, ["lennuk"], ["a"])])
    assert "кандидаты" in out and "lennuk" in out


def test_target_word_does_not_spend_budget(vocab):
    """Целевое слово карточки новое по определению — бюджет ему не тратим."""
    _, new, _ = vocab.check_sentence("Ma nägin lennukit")
    assert "lennuk" in new
    pct, new2, _ = vocab.check_sentence("Ma nägin lennukit", targets={"lennuk"})
    assert "lennuk" not in new2, "целевое слово не должно попадать в новые"
    assert len(new2) == len(new) - 1


def test_adpositions_and_conjunctions_are_function_words(vocab):
    assert vocab._is_function("mööda")   # послелог
    assert vocab._is_function("ääres")   # послелог
    assert vocab._is_function("ja")      # союз
    assert not vocab._is_function("jõgi")


def test_topic_of_strips_subdeck():
    from anki_vocab.query import topic_of

    assert topic_of("kursuse eesti keel::A2::09. Vaba aeg::01. sõnad") == "09. Vaba aeg"
    assert topic_of("kursuse eesti keel::A2::09. Vaba aeg") == "09. Vaba aeg"
    assert topic_of("en words") == "en words"


def test_lookup_reports_where_the_word_already_lives(vocab):
    """Дубль внутри той же темы и дубль из другой — разные решения,
    поэтому в отчёте должно быть видно место."""
    vocab.con.execute(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values"
        " (1, 'Estonian 3 Forms', 'kursuse eesti keel::A2::09. Vaba aeg::01. sõnad',"
        " '[]', 'река', 'jõgi', 'jõgi', '[]', 0, 0)"
    )
    assert vocab.lookup("jõe").decks == ("09. Vaba aeg",)
