"""Parsing Anki notes: fields → (Estonian forms, translation, examples, tags)."""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field

from .config import LEVEL_TAGS, POS_TAGS, SERVICE_TAGS

FIELD_SEP = "\x1f"

_TAG_RE = re.compile(r"<[^>]+>")
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_SPACE_RE = re.compile(r"[\s ]+")
_CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
_CYRILLIC_RUN_RE = re.compile(r"[а-яёА-ЯЁ]+[\s:.,—-]*")
# A word is any letters except Cyrillic: Cyrillic is what separates the
# Estonian side from the Russian one. A narrow "Estonian alphabet only" set
# silently cut the typo 'éesti' into 'e' and 'esti', and the check missed it.
_LETTER = r"(?:(?![а-яёА-ЯЁ])[^\W\d_])"
_WORD_RE = re.compile(rf"{_LETTER}+(?:['’-]{_LETTER}+)*")
# Some Basic cards are "word + list of its forms" with Russian labels:
#   koer  1-я форма (номинатив): koer  2-я форма (генитив): koera  ...
# We match the whole label. Just checking for the word «форма» won't do:
# sentence cards sometimes carry a note like «nad lähevad — мн.ч. от minema»,
# and the sentence then got split into "forms".
_FORM_LABEL_RE = re.compile(
    r"(?:\d\s*-?\s*я\s+форма|мн\.\s*ч\.)\s*(?:\([^)]*\))?\s*:", re.I
)
# Fill-in-the-blank drill cards: '___ kleidid on (ilus). (эти)'.
# That's an exercise, not a dictionary entry — there's no translation in it.
_EXERCISE_RE = re.compile(r"_{2,}")
# Label tails written in Latin script that would otherwise leak into words.
_LABEL_NOISE = {"nom", "pl", "sg", "gen", "part", "acc", "ma", "da", "vorm", "mina"}

LEVEL_ORDER = {lvl: i for i, lvl in enumerate(["a1", "a2", "b1", "b2", "c1", "c2"])}


def clean(raw: str) -> str:
    """Fields hold HTML: Basic (and reversed card) has inline styled divs."""
    s = _SOUND_RE.sub(" ", raw or "")
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s)
    return _SPACE_RE.sub(" ", s).strip()


def words(text: str) -> list[str]:
    # Diacritics may be combining (e + U+0301) — without NFC the letter
    # falls apart and the word gets split in two.
    return _WORD_RE.findall(unicodedata.normalize("NFC", text))


def has_cyrillic(text: str) -> bool:
    return bool(_CYRILLIC_RE.search(text or ""))


def strip_cyrillic(text: str) -> str:
    """Strips Russian inserts: 'Переведи: Ma olen õpetaja.' → 'Ma olen õpetaja.'"""
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
        """Phrases and exercises don't enter the palette and give no glosses.

        Structured note types guarantee a dictionary entry even with
        several words: 'korda tegema', 'välja viima' are phrasal verbs,
        full vocabulary units, not sentences.
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


def _three_forms(
    f: list[str], n_forms: int = 3
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Positional layout: Russian, forms, three example pairs.

    The form count comes from outside because the note types diverged:
    'Estonian 3 Forms' gained a Form4 field (partitive plural), while the
    old 'Estonian Word Forms' kept three — so their examples are shifted
    by one position relative to each other.
    """
    slots = [
        (f"Form{i}", clean(f[i]))
        for i in range(1, n_forms + 1)
        if i < len(f) and clean(f[i])
    ]
    examples = []
    base = n_forms + 1
    for et_i, ru_i in ((base, base + 1), (base + 2, base + 3), (base + 4, base + 5)):
        if et_i < len(f) and ru_i < len(f):
            et, ru = clean(f[et_i]), clean(f[ru_i])
            if et:
                examples.append((et, ru))
    return slots, examples


def parse_note(notetype: str, flds: str, tags: str) -> ParsedNote | None:
    f = flds.split(FIELD_SEP)
    level, pos, topics = parse_tags(tags)

    lexical = drill = False

    if notetype in ("Estonian 3 Forms", "Estonian Word Forms"):
        slots, examples = _three_forms(f, 4 if notetype == "Estonian 3 Forms" else 3)
        ru = clean(f[0]) if f else ""
        lexical = True

    elif notetype == "Estonian Word Forms with TypeIn":
        # One note = one form of one word; merged by (ru, lemma) in the index.
        answer = clean(f[2]) if len(f) > 2 else ""
        # The Form field is filled only on word-form cards;
        # on sentence cards (deck '03. lause typein') it's empty.
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
        # Sides are inconsistent and both may be mixed: the front may say
        # «Переведи: <Estonian phrase>», the back — a word with a form list
        # and Russian labels. Go by whichever side has more Latin script.
        front = clean(f[0]) if f else ""
        back = clean(f[1]) if len(f) > 1 else ""
        # An exercise, not a dictionary entry: a '___' blank or both sides
        # in Estonian (grammar drills like 'Need riided on ilusad').
        drill = bool(_EXERCISE_RE.search(front) or _EXERCISE_RE.search(back))
        lat_front, lat_back = estonian_words(front), estonian_words(back)
        if not lat_front and not lat_back:
            return None
        if lat_back and (not lat_front or len(lat_back) > len(lat_front)):
            et_side, ru, slot = back, front, "Back"
        else:
            et_side, ru, slot = front, back, "Front"

        if not has_cyrillic(ru):
            drill = True
        segments = _FORM_LABEL_RE.split(et_side)
        if len(segments) >= 3:  # at least two labels — surely a form list
            # Take the first Estonian word of each segment: the rest is
            # explanations and an example that often follows the forms.
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
        topics=topics, force_phrase=drill,
        force_lexical=lexical,
    )
