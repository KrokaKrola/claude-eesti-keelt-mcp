from anki_vocab.parse import clean, parse_note, strip_cyrillic

SEP = "\x1f"


def note(notetype, *fields, tags=""):
    return parse_note(notetype, SEP.join(fields), tags)


def test_clean_strips_html_and_sound():
    raw = '<div style="font-weight:bold">koer</div>[sound:x.mp3]&nbsp;'
    assert clean(raw) == "koer"


def test_three_forms():
    p = note(
        "Estonian 3 Forms", "шляпа", "kübar", "kübara", "kübarat",
        "See kübar on ilus.", "Эта шляпа красивая.",
        "Kübara värv on must.", "Цвет чёрный.",
        "Ma ei taha kübarat kanda.", "Не хочу носить.", "1",
        tags="A2 одежда существительное",
    )
    assert p.ru == "шляпа"
    assert [f for _, f in p.slots] == ["kübar", "kübara", "kübarat"]
    assert p.level == "a2" and p.pos == "существительное"
    assert p.topics == ["одежда"]
    assert len(p.examples) == 3
    assert not p.is_phrase


def test_typein_is_one_form_per_note():
    p = note(
        "Estonian Word Forms with TypeIn", "делать", "1-я форма (ma-инфинитив)",
        "tegema", "Ma pean kodutööd tegema.", "Мне нужно делать домашнее задание.",
        tags="A1 глагол",
    )
    assert p.slots == [("Answer", "tegema")]
    assert p.examples == [("Ma pean kodutööd tegema.", "Мне нужно делать домашнее задание.")]


def test_reversed_card_has_russian_in_front():
    p = note("Basic (and reversed card)", "собака", "koer", tags="животные существительное")
    assert p.ru == "собака"
    assert p.slots == [("Back", "koer")]


def test_form_list_stops_before_example_sentence():
    back = ("pulm 1-я форма (номинатив): pulm 2-я форма (генитив): pulma "
            "мн.ч. (nom.pl.): pulmad Pulm oli väga ilus.")
    p = note("Basic (and reversed card)", "свадьба", back, tags="существительное")
    assert [f for _, f in p.slots] == ["pulm", "pulma", "pulmad"]
    assert not p.is_phrase


def test_russian_instruction_is_stripped_from_estonian_side():
    p = note("Basic", "Переведи: Ma olen õpetaja.", "Я учитель.")
    assert p.slots == [("Front", "Ma olen õpetaja")]
    assert p.is_phrase


def test_fill_in_drill_is_not_a_vocabulary_card():
    p = note("Basic", "___ kleidid on (ilus). (эти)", "Need kleidid on ilusad")
    assert p.is_phrase, "упражнение не должно попадать в палитру"


def test_all_estonian_card_is_a_drill():
    p = note("Basic", "riided", "Need riided on ilusad")
    assert p.is_phrase, "обе стороны эстонские — это тренажёр, не словарная статья"


def test_strip_cyrillic():
    assert strip_cyrillic("Переведи: Ma olen siin.") == "Ma olen siin"
