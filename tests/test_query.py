import sqlite3

import pytest

from anki_vocab.index import SCHEMA
from anki_vocab.query import (
    KNOWN,
    format_debt,
    SEEN,
    UNKNOWN,
    Palette,
    Vocab,
    format_check,
    format_text_check,
    Check,
)


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
    con.executemany("insert into form(form,lemma,note_id,slot) values (?,?,?,'Form2')",
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
    pool = vocab.pool()
    assert "kalastama" not in pool.topical + pool.core
    assert "jõgi" in pool.topical + pool.core


def test_pool_matches_underscored_topic_tags(vocab):
    """Старые темы помечены одним тегом 'kodused_tööd', новые — словами.
    Скилл считает теги из имени колоды словами, и без нормализации
    группа «по теме» у старых тем молча приезжала пустой."""
    assert vocab.pool(topics=["kodused", "tööd"]).topical == ["lapp"]
    assert vocab.pool(topics=["kodused_tööd"]).topical == ["lapp"]


def test_pool_keeps_cards_without_a_level_tag(vocab):
    """Карточка без тега уровня — всё равно выученное слово. В колоде
    '03. minu kodu' без тега лежит вся бытовая лексика."""
    assert "laud" in vocab.pool(level="a2").core


def test_pool_shows_the_card_headword_verbatim(vocab):
    """Первое поле карточки — уже словарная форма. Лемматизация делала
    из 'Kindad' → 'kinnas', а фразовый глагол размножала по формам."""
    pool = vocab.pool()
    palette = pool.topical + pool.core
    assert "kindad" in palette and "kinnas" not in palette
    assert palette.count("kaasa elama") == 1


def test_pool_ignores_words_without_a_dictionary_card(vocab):
    """helistama человек видел только в предложениях — в палитру он не
    идёт, но знакомым остаётся: покрытие считает весь индекс."""
    vocab.con.execute(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,first_seen) values ('helistama',NULL,NULL,'a2','[]',"
        "'[\"helistage\"]',3,1,0.0,0)")
    pool = vocab.pool()
    assert "helistama" not in pool.topical + pool.core
    assert vocab.lookup("helistama").status in (KNOWN, SEEN)


def test_pool_filters(vocab):
    # laud без тега уровня — показываем на любом: это выученное слово
    assert vocab.pool(level="a1") == Palette([], ["kübar", "laud"])
    assert vocab.pool(pos="глагол") == Palette([], ["kaasa elama"])


def test_pool_splits_topical_from_core(vocab):
    """У новой темы совпадений нет, и палитра выходила пустой —
    поэтому к теме всегда добавляется ядро."""
    pool = vocab.pool(topics=["vaba"])
    assert pool.topical == ["jõgi", "kaasa elama"]
    assert "õun" in pool.core and "kübar" in pool.core


def test_truncation_is_never_silent(vocab):
    """Любой предел прячет одну и ту же часть словаря, поэтому о срезе
    надо сказать вслух — иначе слова просто исчезают."""
    from anki_vocab.query import format_pool

    cut = vocab.pool(limit=1)
    assert cut.dropped == len(vocab.pool().core) - 1
    assert "truncated by limit" in format_pool(cut)
    assert "truncated by limit" not in format_pool(vocab.pool())


def test_pool_on_unknown_topic_still_returns_core(vocab):
    pool = vocab.pool(topics=["töö", "amet"])
    assert pool.topical == []
    assert len(pool.core) == 7, "ядро должно приехать даже при нулевом совпадении"


def test_pool_ranks_by_usage_then_retention(vocab):
    """Ходовость (n_notes) важнее и числа карточек, и интервала:
    по числу карточек сортировка падала в алфавит, а по одному интервалу
    наверх всплывали порядковые числительные."""
    vocab.con.execute(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,first_seen) values ('üheksasajas','девятисотый',NULL,'a2','[]',"
        "'[\"üheksasajas\"]',1,1,171.0,0)")
    add_cards(vocab.con, [(106, "a2", "числительное", "[]", "Üheksasajas")])
    core = vocab.pool(limit=3).core
    assert "üheksasajas" not in core, "лёгкое числительное не должно вытеснять рабочие слова"
    assert "jõgi" in core and "õun" in core


def test_function_words_are_not_counted(vocab):
    pct, new, content, _ = vocab.check_sentence("See on jõgi")
    assert "see" not in content and "on" not in content
    assert pct == 100 and new == []


def test_coverage_reports_new_words(vocab):
    pct, new, _, _ = vocab.check_sentence("Ma nägin lennukit")
    assert new and pct < 100


def test_novel_keeps_half_known_words_apart(vocab):
    """kalastama есть только внутри чужой фразы. Для покрытия это
    известное слово, для дозы — материал помягче, но материал."""
    novel = vocab.novel("See jõgi on ilus")
    assert "jõgi" not in dict(novel.unknown) and "jõgi" not in dict(novel.half)

    novel = vocab.novel("Ta käib kalastamas")
    assert "kalastama" in dict(novel.half)
    assert "kalastama" not in dict(novel.unknown)


def test_novel_ignores_the_words_being_taught(vocab):
    assert "lennuk" in dict(vocab.novel("Ma nägin lennukit").unknown)
    taught = vocab.novel("Ma nägin lennukit", taught={"lennuk"})
    assert "lennuk" not in dict(taught.unknown)


def test_novel_keeps_the_form_next_to_the_lemma(vocab):
    """Пара нужна, чтобы промах морфологии было видно в логе: без формы
    рядом чужая лемма выглядит честным новым словом."""
    assert dict(vocab.novel("Ma nägin lennukit").unknown)["lennuk"] == "lennukit"


def test_one_new_word_is_always_allowed(vocab):
    """Жёсткий потолок ломал естественность фраз — одно новое слово ок
    независимо от покрытия."""
    assert "low" not in format_text_check([Check(20, ["lennuk"], ["a", "b"], [])])


def test_low_coverage_with_several_new_words_is_flagged(vocab):
    assert "low" in format_text_check([Check(40, ["lennuk", "auto"], ["a", "b"], [])])


def test_good_coverage_passes_even_with_several_new(vocab):
    assert "low" not in format_text_check([Check(85, ["lennuk", "auto"], ["a"], [])])


def test_new_words_are_reported_as_candidates(vocab):
    out = format_text_check([Check(85, ["lennuk"], ["a"], [])])
    assert "candidates" in out and "lennuk" in out


def test_target_word_does_not_spend_budget(vocab):
    """Целевое слово карточки новое по определению — бюджет ему не тратим."""
    _, new, _, _ = vocab.check_sentence("Ma nägin lennukit")
    assert "lennuk" in new
    pct, new2, _, _ = vocab.check_sentence("Ma nägin lennukit", targets={"lennuk"})
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
    assert vocab.lookup("jõe").decks == ("09. Vaba aeg 3F",)


def test_lookup_separates_a_word_card_from_a_passing_mention(vocab):
    """«Слово уже есть в 09. Vaba aeg» значит разное: карточка трёх форм —
    это дубль, попадание в чужой пример — нет."""
    vocab.con.execute(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values"
        " (1, 'Estonian 3 Forms', 'kursuse eesti keel::A2::09. Vaba aeg::01. sõnad',"
        " '[]', 'река', 'jõgi', 'jõgi', '[]', 0, 0)"
    )
    vocab.con.execute(
        "insert into entry (note_id, notetype, deck, topics, ru, headword,"
        " et_raw, examples, is_phrase, mod) values"
        " (2, 'Estonian 3 Forms', 'kursuse eesti keel::A2::08. Loomad::01. sõnad',"
        " '[]', 'рыба', 'kala', 'kala', '[]', 0, 0)"
    )
    vocab.con.execute(
        "insert into form(form,lemma,note_id,slot) values ('jõel','jõgi',2,'example')"
    )
    assert vocab.lookup("jõgi").decks == ("09. Vaba aeg 3F", "08. Loomad ex")


# --- трудные слова и долг ------------------------------------------------


def add_lemma(con, lemma, *, n_notes=4, n_word=4, strength=0.0, lapses=0,
              level="a2", pos="существительное"):
    con.execute(
        "insert into lemma (lemma,ru,pos,level,topics,forms,n_notes,n_word_notes,"
        "strength,lapses,first_seen) values (?,'перевод',?,?,'[]',?,?,?,?,?,0)",
        (lemma, pos, level, f'["{lemma}"]', n_notes, n_word, strength, lapses),
    )


def test_weak_hint_takes_words_the_learner_keeps_failing(vocab):
    """Срыв без интервала ничего не значит: слово могли ронять полгода
    назад и с тех пор выучить намертво."""
    add_lemma(vocab.con, "kestma", strength=19.1, lapses=45)
    add_lemma(vocab.con, "ahi", strength=102.7, lapses=29)
    add_lemma(vocab.con, "korv", strength=3.0, lapses=0)
    add_cards(vocab.con, [(201, "a2", "глагол", "[]", "Kestma"),
                          (202, "a2", "существительное", "[]", "Ahi"),
                          (203, "a2", "существительное", "[]", "Korv")])
    weak = vocab.pool().weak
    assert "kestma" in weak
    assert "ahi" not in weak, "интервал 103 дня — слово уже выучено"
    assert "korv" not in weak, "новое слово ещё не трудное, оно просто новое"


def test_weak_hint_is_ordered_by_severity(vocab):
    """Подсказку читают сверху, поэтому алфавит здесь потерял бы сигнал."""
    add_lemma(vocab.con, "kestma", strength=19.1, lapses=45)
    add_lemma(vocab.con, "oskus", strength=14.5, lapses=24)
    add_cards(vocab.con, [(201, "a2", "глагол", "[]", "Kestma"),
                          (204, "a2", "существительное", "[]", "Oskus")])
    assert vocab.pool().weak == ["kestma", "oskus"]


def test_weak_hint_respects_the_palette_filters(vocab):
    add_lemma(vocab.con, "kestma", strength=19.1, lapses=45)
    add_cards(vocab.con, [(201, "a2", "глагол", "[]", "Kestma")])
    assert vocab.pool(pos="существительное").weak == []


def test_debt_lists_words_that_never_got_a_card(vocab):
    """kalastama человек читает в чужих примерах, но форм и перевода
    у него не учил — это и есть долг."""
    words = [d.word for d in vocab.debt().items]
    assert "kalastama" in words
    assert "jõgi" not in words, "у слова есть словарная карточка"


def test_debt_skips_function_words_and_names(vocab):
    add_lemma(vocab.con, "ning", n_word=0, n_notes=50, pos=None)
    add_lemma(vocab.con, "tallinn", n_word=0, n_notes=40, pos=None)
    words = [d.word for d in vocab.debt().items]
    assert "ning" not in words and "tallinn" not in words


def test_debt_pages_through_the_list(vocab):
    for i, word in enumerate(["aitama", "helistama", "jooma"]):
        add_lemma(vocab.con, word, n_word=0, n_notes=30 - i, pos=None)
    first = vocab.debt(limit=2)
    assert [d.word for d in first.items] == ["aitama", "helistama"]
    assert first.more, "страница неполной не бывает молча"
    second = vocab.debt(limit=2, offset=2)
    assert "aitama" not in [d.word for d in second.items]
    assert [d.word for d in second.items][0] == "jooma"


def test_debt_shows_where_the_word_was_met(vocab):
    """Голое слово ничего не напоминает — решает узнавание контекста."""
    add_lemma(vocab.con, "vihma", n_word=0, n_notes=5, pos=None)
    vocab.con.execute(
        "insert into entry (note_id,notetype,deck,topics,ru,headword,et_raw,"
        "examples,is_phrase,mod) values (9,'Estonian 3 Forms','колода','[]',"
        "'дождь','x','x','[[\"Väljas sajab vihma.\",\"На улице дождь.\"]]',0,0)"
    )
    vocab.con.execute(
        "insert into form(form,lemma,note_id,slot) values ('vihma','vihma',9,'example')"
    )
    assert vocab.debt().items[0].example == "Väljas sajab vihma."


def test_debt_bounds_are_pulled_back_into_range(vocab):
    """limit=0 отвечал «слов больше нет» при полном списке — это читается
    как «долгов нет». Отрицательное смещение печатало страницу с «#-4»."""
    for word in ["aitama", "helistama", "jooma"]:
        add_lemma(vocab.con, word, n_word=0, n_notes=30, pos=None)
    assert len(vocab.debt(limit=0).items) == 1
    assert vocab.debt(offset=-5).offset == 0
    assert "#-" not in format_debt(vocab.debt(offset=-5))


def test_debt_page_has_a_ceiling(vocab):
    """Инструмент отдаёт limit наружу, а 400 слов — это 8k токенов
    одним ответом, при том что смысл проекта — экономия контекста."""
    from anki_vocab.query import DEBT_PAGE_MAX

    for i in range(DEBT_PAGE_MAX + 20):
        add_lemma(vocab.con, f"sõna{i}", n_word=0, n_notes=30, pos=None)
    assert len(vocab.debt(limit=400).items) == DEBT_PAGE_MAX


def test_debt_ignores_words_carded_outside_the_index(vocab):
    """Ветка A1 из области видимости исключена, но карточки там настоящие.
    Без этой проверки инструмент уверял, что laps и koer никогда не учили."""
    add_lemma(vocab.con, "koer", n_word=0, n_notes=90, pos=None)
    assert "koer" in [d.word for d in vocab.debt().items]
    vocab.con.execute("insert into outside(lemma) values ('koer')")
    assert "koer" not in [d.word for d in vocab.debt().items]


def test_known_words_stay_dismissed(vocab):
    """Иначе в новом чате приезжает ровно тот же десяток: ответа человека
    в коллекции нет и взяться ему неоткуда."""
    add_lemma(vocab.con, "hakkama", n_word=0, n_notes=90, pos=None)
    assert vocab.debt_skip(["hakkama"]) == (1, 1)
    assert "hakkama" not in [d.word for d in vocab.debt().items]
    assert vocab.debt().skipped == 1
    assert vocab.debt_skip(["hakkama"], undo=True) == (1, 0)
    assert "hakkama" in [d.word for d in vocab.debt().items]


def test_dismissal_takes_any_form_of_the_word(vocab):
    """Человек отвечает тем словом, которое увидел, но может и
    словоформой из примера."""
    add_lemma(vocab.con, "pere", n_word=0, n_notes=90, pos=None)
    vocab.debt_skip(["Pered"])
    assert "pere" not in [d.word for d in vocab.debt().items]


def test_half_known_ignores_words_met_often(vocab):
    """Порог отсекает базовую лексику: ветка A1 в индекс не входит, и без
    него ema с её 47 попаданиями считалась бы полузнакомой."""
    from anki_vocab.config import HALF_KNOWN_MAX

    vocab.con.execute("update lemma set n_notes = ? where lemma = 'kalastama'",
                      (HALF_KNOWN_MAX + 1,))
    assert vocab.novel("Ta käib kalastamas").half == []

    vocab.con.execute("update lemma set n_notes = ? where lemma = 'kalastama'",
                      (HALF_KNOWN_MAX,))
    assert "kalastama" in dict(vocab.novel("Ta käib kalastamas").half)


def test_log_and_report_share_one_verdict(vocab):
    """Формулу вердикта считают отчёт и лог. Пока она одна — они не
    разъедутся; тест держит это свойство."""
    from anki_vocab.query import sentence_ok

    for pct, new in ((100, []), (20, ["a"]), (40, ["a", "b"]), (85, ["a", "b"])):
        in_report = "low" not in format_text_check([Check(pct, new, ["x"], [])])
        assert in_report is sentence_ok(pct, new)


# --- однообразие ----------------------------------------------------------


def add_sentences(con, deck, texts, first_id=900):
    """Заметка-предложение темы: слова уезжают в form как slot 'example'."""
    for i, words_ in enumerate(texts):
        note_id = first_id + i
        con.execute(
            "insert into entry (note_id,notetype,deck,level,pos,topics,ru,headword,"
            "et_raw,examples,is_phrase,mod) values"
            " (?,'Estonian 3 Forms',?,'a2',NULL,'[]','перевод','','','[]',0,0)",
            (note_id, deck),
        )
        con.executemany(
            "insert into form(form,lemma,note_id,slot) values (?,?,?,'example')",
            [(w, w, note_id) for w in words_],
        )


def test_repeat_flags_a_word_the_batch_leans_on(vocab):
    batch = [["panema", "õun"], ["panema", "kingitus"], ["panema", "puuvili"],
             ["ostma", "sokk"], ["lugema", "raamat"], ["jooksma", "park"]]
    hits = {r.lemma: r for r in vocab.repeats(batch)}
    assert hits["panema"].in_batch == 3
    assert "ostma" not in hits, "одиночное слово повтором не считается"


def test_repeat_ignores_structural_verbs(vocab):
    """olema, minema и прочий каркас ротировать бессмысленно — без них
    фразу не построить."""
    batch = [["minema", "pood"]] * 4
    assert [r.lemma for r in vocab.repeats(batch)] == ["pood"]


def test_repeat_ignores_the_words_being_taught(vocab):
    """Пример к слову обязан его содержать — иначе каждое слово темы
    выглядело бы повтором."""
    batch = [["kaal", "õun"], ["kaal", "leib"], ["kaal", "liha"]]
    assert vocab.repeats(batch, targets={"kaal"}) == []


def test_repeat_threshold_follows_the_batch_size(vocab):
    """Порог — корень из размера пачки: два повтора на три предложения
    это однообразие, два на двадцать — совпадение."""
    small = [["panema", "õun"], ["panema", "leib"], ["lugema", "raamat"]]
    assert [r.lemma for r in vocab.repeats(small)] == ["panema"]

    big = small[:2] + [[f"sõna{i}"] for i in range(18)]
    assert vocab.repeats(big) == []

    # 5 из 20 корень уже перебирают, а постоянный потолок в 4 на пачке в
    # 80 предложений выдавал по два десятка слов разом.
    louder = [["panema", f"sõna{i}"] for i in range(5)] + [[f"muu{i}"] for i in range(15)]
    assert [r.lemma for r in vocab.repeats(louder)] == ["panema"]

    huge = [["panema", f"sõna{i}"] for i in range(5)] + [[f"muu{i}"] for i in range(75)]
    assert vocab.repeats(huge) == [], "5 повторов на 80 предложений — не однообразие"


def test_repeat_counts_the_sentences_already_in_the_topic(vocab):
    """Капельный случай: по одному разу за вызов, десять раз за тему."""
    add_sentences(vocab.con, "колода::12. Poes::03. lause typein",
                  [["panema", f"asi{i}"] for i in range(10)])
    batch = [["panema", "kviitung"], ["lugema", "raamat"], ["jooksma", "park"]]
    assert vocab.repeats(batch) == [], "без темы одиночного повтора не видно"
    hits = {r.lemma: r for r in vocab.repeats(batch, topic="12. Poes")}
    assert hits["panema"].in_batch == 1 and hits["panema"].in_topic == 10


def test_repeat_spares_the_words_the_topic_teaches(vocab):
    """'pood' в теме «12. Poes» обязан попадаться часто: жалоба на него
    научила бы только не читать эту строку."""
    deck = "колода::12. Poes::03. lause typein"
    add_sentences(vocab.con, deck, [["pood", f"asi{i}"] for i in range(10)])
    vocab.con.execute(
        "insert into entry (note_id,notetype,deck,level,pos,topics,ru,headword,"
        "et_raw,examples,is_phrase,mod) values"
        " (800,'Estonian 3 Forms','колода::12. Poes::01. sõnad','a2',NULL,'[]',"
        "'магазин','Pood','','[]',0,0)")
    vocab.con.execute(
        "insert into form(form,lemma,note_id,slot) values ('pood','pood',800,'Form1')")
    batch = [["pood", "kviitung"], ["lugema", "raamat"], ["jooksma", "park"]]
    assert vocab.repeats(batch, topic="12. Poes") == []


def test_repeat_line_appears_in_the_report(vocab):
    from anki_vocab.query import Repeat

    out = format_text_check([Check(100, [], ["panema"], [])], 70,
                            [Repeat("panema", 3, 20, crowded=True)], with_topic=True)
    assert "leaning too hard on: panema ×3 (+20 already in the topic)" in out


def test_batch_and_topic_signals_get_separate_lines(vocab):
    """«Перепиши» и «учти на будущее» в одной строке читаются как один шум."""
    from anki_vocab.query import Repeat

    out = format_text_check(
        [Check(100, [], ["panema"], [])], 70,
        [Repeat("panema", 3, 20, crowded=True), Repeat("uus", 1, 76)],
        with_topic=True,
    )
    assert "leaning too hard on: panema ×3" in out
    assert "already heavy in this topic: uus (76)" in out
    assert "uus" not in out.split("already heavy")[0]


def test_report_stays_quiet_without_repeats(vocab):
    assert "leaning" not in format_text_check([Check(100, [], ["panema"], [])])


def test_topic_is_matched_by_deck_level_not_substring():
    from anki_vocab.query import topic_decks

    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    decks = ["k::A2::12. Poes::01. sõnad", "k::A2::14. Riidepoes::01. sõnad",
             "k::A2::16. Toidupoes::03. lause typein"]
    con.executemany(
        "insert into entry (note_id,notetype,deck,topics,headword,et_raw,examples,"
        "is_phrase,mod) values (?,'x',?,'[]','x','x','[]',0,0)",
        list(enumerate(decks)),
    )
    assert topic_decks(con, "Poes") == [decks[0]]
    assert topic_decks(con, "12. Poes") == [decks[0]]
    assert topic_decks(con, "2. Poes") == []
    assert topic_decks(con, "16. toidupoes") == [decks[2]]


def test_targets_match_by_lemma(vocab):
    """'kaerahelbed' в targets, 'kaerahelveste' в тексте — одно слово."""
    goal = vocab.goal(["kaerahelbed"])
    check = vocab.check_sentence("Kaerahelveste kilo", targets=goal)
    assert "kaerahelves" not in check.new


def test_typo_is_reported_apart_from_new_words(vocab):
    check = vocab.check_sentence("Laps söi jõe ääres")
    assert check.typos == ["söi"] and "söi" not in check.new
    assert "typo?" in format_text_check([check])


def test_proper_name_is_not_a_new_word(vocab):
    new = vocab.check_sentence("Ta on pärit Lätist").new
    assert not {"läti", "lätist"} & set(new)


def test_typo_already_in_the_collection_is_still_a_typo(vocab):
    """Попав в пример, опечатка становилась «знакомым» словом."""
    vocab.con.execute(
        "insert into lemma (lemma,topics,forms,n_notes,n_word_notes) "
        "values ('söi','[]','[\"söi\"]',2,0)"
    )
    vocab.con.execute("insert into form values ('söi','söi',9,'example')")
    assert vocab.check_sentence("Laps söi").typos == ["söi"]


def test_sentence_only_word_comes_with_where_it_was_met(vocab):
    """'~' показывают человеку на решение — с предложением, где он слово
    видел, а не с тегами чужой карточки."""
    vocab.con.execute(
        "insert into entry (note_id,notetype,deck,topics,headword,et_raw,examples,"
        "is_phrase,mod) values (50,'x','k::A2::09. Vaba aeg::01. sõnad','[]','x','x',?,0,0)",
        ('[["Ma kalastan jõel.", ""]]',),
    )
    vocab.con.execute("insert into form values ('kalastan','kalastama',50,'example')")
    vocab.con.execute("insert into debt_skip values ('kalastama', 0)")
    out = format_check(vocab.check(["kalastama"]))
    assert "~ seen 1×" in out and "«Ma kalastan jõel.»" in out
    assert "marked it known" in out and "not a duplicate" in out
