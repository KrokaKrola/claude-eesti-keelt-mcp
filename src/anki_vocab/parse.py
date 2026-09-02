"""Разбор заметок Anki: поля → (эстонские формы, перевод, примеры, теги)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .config import LEVEL_TAGS, POS_TAGS, SERVICE_TAGS

FIELD_SEP = "\x1f"

_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_SPACE_RE = re.compile(r"[\s ]+")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_CYRILLIC_RUN_RE = re.compile(r"[а-яёА-ЯЁ]+[\s:.,—-]*")
_WORD_RE = re.compile(r"[A-Za-zÄÖÜÕŠŽäöüõšž]+(?:['’-][A-Za-zÄÖÜÕŠŽäöüõšž]+)*")
# Часть карточек Basic — это «слово + список его форм» с русскими подписями:
#   koer  1-я форма (номинатив): koer  2-я форма (генитив): koera  ...
# Ловим саму метку целиком. Проверять просто наличие слова «форма» нельзя:
# на карточках-предложениях бывает пояснение «nad lähevad — мн.ч. от minema»,
# и тогда предложение разлеталось на «формы».
_FORM_LABEL_RE = re.compile(
    r"(?:\d\s*-?\s*я\s+форма|мн\.\s*ч\.)\s*(?:\([^)]*\))?\s*:", re.I
)
# Карточки-тренажёры с пропуском: '___ kleidid on (ilus). (эти)'.
# Это упражнение, а не словарная статья — переводом там и не пахнет.
_EXERCISE_RE = re.compile(r"_{2,}")
# Хвосты подписей, которые латиницей и потому пролезают в слова.
_LABEL_NOISE = {"nom", "pl", "sg", "gen", "part", "acc", "ma", "da", "vorm", "mina"}

LEVEL_ORDER = {lvl: i for i, lvl in enumerate(["a1", "a2", "b1", "b2", "c1", "c2"])}


def clean(raw: str) -> str:
    """Поля хранят HTML: у Basic (and reversed card) там инлайновые div со стилями."""
    s = _SOUND_RE.sub(" ", raw or "")
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _SPACE_RE.sub(" ", s).strip()


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def strip_cyrillic(text: str) -> str:
    """Убирает русские вставки: 'Переведи: Ma olen õpetaja.' → 'Ma olen õpetaja.'"""
    return _SPACE_RE.sub(" ", _CYRILLIC_RUN_RE.sub(" ", text)).strip(" :-—,.")


def estonian_words(text: str) -> list[str]:
    return [w for w in words(text) if w.lower() not in _LABEL_NOISE]


@dataclass
class ParsedNote:
    ru: str
    slots: list[tuple[str, str]] = field(default_factory=list)
    examples: list[tuple[str, str]] = field(default_factory=list)
    level: str | None = None
    pos: str | None = None
    topics: list[str] = field(default_factory=list)
    force_phrase: bool = False
    force_lexical: bool = False

    @property
    def et_text(self) -> str:
        return " / ".join(t for _, t in self.slots)

    @property
    def headword(self) -> str:
        return self.slots[0][1] if self.slots else ""

    @property
    def is_phrase(self) -> bool:
        """Фразы и упражнения не участвуют в палитре и не дают толкований.

        Структурированные типы гарантируют словарную статью, даже если
        слов несколько: 'korda tegema', 'välja viima' — это фразовые
        глаголы, полноценные единицы словаря, а не предложения.
        """
        if self.force_lexical:
            return False
        return self.force_phrase or len(words(self.headword)) > 1


def parse_tags(raw: str) -> tuple[str | None, str | None, list[str]]:
    level = pos = None
    topics: list[str] = []
    for tag in (raw or "").split():
        low = tag.lower()
        if low in LEVEL_TAGS:
            if level is None or LEVEL_ORDER.get(low, 99) < LEVEL_ORDER.get(level, 99):
                level = low
        elif low in POS_TAGS:
            pos = POS_TAGS[low]
        elif low in SERVICE_TAGS:
            continue
        else:
            topics.append(tag)
    return level, pos, sorted(set(topics))


def _three_forms(f: list[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    slots = [
        (name, clean(f[i]))
        for i, name in ((1, "Form1"), (2, "Form2"), (3, "Form3"))
        if i < len(f) and clean(f[i])
    ]
    examples = []
    for et_i, ru_i in ((4, 5), (6, 7), (8, 9)):
        if et_i < len(f) and ru_i < len(f):
            et, ru = clean(f[et_i]), clean(f[ru_i])
            if et:
                examples.append((et, ru))
    return slots, examples


def parse_note(notetype: str, flds: str, tags: str) -> ParsedNote | None:
    f = flds.split(FIELD_SEP)
    level, pos, topics = parse_tags(tags)

    lexical = False

    if notetype in ("Estonian 3 Forms", "Estonian Word Forms"):
        slots, examples = _three_forms(f)
        ru = clean(f[0]) if f else ""
        lexical = True

    elif notetype == "Estonian Word Forms with TypeIn":
        # Одна заметка = одна форма одного слова; склейка по (ru, лемма) в индексе.
        answer = clean(f[2]) if len(f) > 2 else ""
        # Поле Form заполнено только у карточек на форму слова;
        # у карточек-предложений (колода '03. lause typein') оно пустое.
        lexical = bool(clean(f[1])) if len(f) > 1 else False
        slots = [("Answer", answer)] if answer else []
        ru = clean(f[0]) if f else ""
        examples = []
        if len(f) > 4 and clean(f[3]):
            examples.append((clean(f[3]), clean(f[4])))

    elif notetype == "Audio card":
        text = clean(f[1]) if len(f) > 1 else ""
        slots = [("Text", text)] if text else []
        ru = clean(f[2]) if len(f) > 2 else ""
        examples = []

    elif notetype.startswith("Basic"):
        # Стороны непостоянны, и обе могут быть смешанными: спереди бывает
        # «Переведи: <эстонская фраза>», сзади — слово со списком форм
        # и русскими подписями. Ориентируемся на то, где больше латиницы.
        front = clean(f[0]) if f else ""
        back = clean(f[1]) if len(f) > 1 else ""
        # Упражнение, а не словарная статья: пропуск '___' или обе стороны
        # на эстонском (грамматические дриллы вроде 'Need riided on ilusad').
        drill = bool(_EXERCISE_RE.search(front) or _EXERCISE_RE.search(back))
        lat_front, lat_back = estonian_words(front), estonian_words(back)
        if not lat_front and not lat_back:
            return None
        if lat_back and (not lat_front or len(lat_back) > len(lat_front)):
            et_side, ru, slot = back, front, "Back"
        else:
            et_side, ru, slot = front, back, "Front"

        ru = strip_cyrillic(ru) and ru or ru
        if not has_cyrillic(ru):
            drill = True
        segments = _FORM_LABEL_RE.split(et_side)
        if len(segments) >= 3:  # минимум две подписи — это точно список форм
            # Из каждого отрезка берём первое эстонское слово: остальное —
            # пояснения и пример, который часто идёт следом за формами.
            forms = []
            for segment in segments:
                found = estonian_words(segment)
                if found:
                    forms.append(found[0])
            forms = list(dict.fromkeys(forms))
            slots = [(f"{slot}{i + 1}", w) for i, w in enumerate(forms)]
            examples = []
        else:
            text = strip_cyrillic(et_side)
            slots = [(slot, text)] if text else []
            examples = []

    else:
        return None

    if not slots:
        return None
    return ParsedNote(
        ru=ru, slots=slots, examples=examples, level=level, pos=pos,
        topics=topics, force_phrase=locals().get("drill", False),
        force_lexical=lexical,
    )
