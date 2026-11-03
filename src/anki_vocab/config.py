"""Paths, local settings and tag classification."""
from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

# Personal settings live outside git; anki-vocab.example.toml documents them.
SETTINGS_FILE = Path(os.environ.get("ANKI_VOCAB_CONFIG", PROJECT_ROOT / "anki-vocab.toml"))


def _load_settings() -> dict:
    try:
        return tomllib.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"{SETTINGS_FILE}: {exc}") from exc


SETTINGS = _load_settings()


def anki_base() -> Path:
    """Anki's data folder, which holds one subfolder per profile."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Anki2"
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / "Anki2"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "Anki2"


def profile_collections() -> list[Path]:
    base = anki_base()
    return sorted(base.glob("*/collection.anki2")) if base.is_dir() else []


def _resolve_collection() -> tuple[Path, str | None]:
    """Collection path plus a hint when it couldn't be determined.

    Env var → settings file → the only profile. With several profiles,
    guessing would index the wrong one silently, so we ask instead."""
    explicit = os.environ.get("ANKI_COLLECTION") or SETTINGS.get("collection")
    if explicit:
        return Path(explicit).expanduser(), None
    found = profile_collections()
    if len(found) == 1:
        return found[0], None
    where = anki_base()
    if not found:
        return where / "collection.anki2", f"no Anki profile found in {where}"
    names = ", ".join(p.parent.name for p in found)
    return found[0], (
        f"several Anki profiles ({names}): set collection in {SETTINGS_FILE.name}"
    )


COLLECTION, COLLECTION_HINT = _resolve_collection()
INDEX = DATA_DIR / "vocab.sqlite"

# Anki MCP Server add-on settings: the host and port it listens on.
# The user can change them in the add-on's config dialog. Anki's folder
# differs per OS, so the path is derived from the collection:
# <Anki2>/<profile>/collection.anki2 → <Anki2>/addons21/.
ADDON_META = COLLECTION.parent.parent / "addons21/124672614/meta.json"

# SQLite files that must be copied together: without -wal the data lags behind.
COLLECTION_PARTS = ("", "-wal", "-shm")

EXPECTED_SCHEMA_VER = 18
INDEX_VERSION = 8

# Bump on any change to the parsing logic in lemma.py: otherwise the cache
# silently serves analyses computed by the old code.
LEMMATIZER_VERSION = 3

# Index scope. Matching is on the '::' boundary, so 'A1' won't catch
# a sibling branch with a similar prefix.
# Default course root for topic_write: topics live under <course>::<NN. Topic>.
COURSE = SETTINGS.get("course", "kursuse eesti keel::A2")
INCLUDED_DECK_PREFIXES = tuple(SETTINGS.get("include_decks", [COURSE.split("::")[0]]))
# Branches inside the scope to leave out (e.g. decks in older card formats).
EXCLUDED_DECK_PREFIXES = tuple(SETTINGS.get("exclude_decks", []))


def deck_in_scope(deck: str) -> bool:
    def under(prefix: str) -> bool:
        return deck == prefix or deck.startswith(prefix + "::")

    if INCLUDED_DECK_PREFIXES and not any(under(p) for p in INCLUDED_DECK_PREFIXES):
        return False
    return not any(under(p) for p in EXCLUDED_DECK_PREFIXES)

LEVEL_TAGS = {"a1", "a2", "b1", "b2", "c1", "c2"}

POS_TAGS = {
    "существительное": "существительное",
    "noun": "существительное",
    "глагол": "глагол",
    "verb": "глагол",
    "прилагательное": "прилагательное",
    "adjective": "прилагательное",
    "наречие": "наречие",
    "adverb": "наречие",
    "местоимение": "местоимение",
    "pronoun": "местоимение",
    "числительное": "числительное",
    "numeral": "числительное",
    "предлог": "предлог",
    "послелог": "послелог",
    "союз": "союз",
    "частица": "частица",
    "междометие": "междометие",
}

# Tags about the card format, not the word's meaning.
SERVICE_TAGS = {"typein", "lause", "предложение", "аудио", "audio", "marked", "leech"}

# Function words: counted neither as known nor as new in coverage.
FUNCTION_WORDS = {
    "olema", "ei", "ja", "ning", "aga", "või", "et", "kui", "siis", "ka",
    "ma", "sa", "ta", "me", "te", "nad", "mina", "sina", "tema", "meie",
    "teie", "nemad", "see", "need", "too", "oma", "ise",
    "mis", "kes", "kus", "kuidas", "millal", "miks", "kas", "kumb",
    "väga", "nii", "veel", "juba", "ainult", "ära", "palun",
    "on", "olen", "oled", "oleme", "olete", "pole", "ole",
    "üks", "kõik", "iga", "mõni", "palju", "vähe",
}

# Dose of new words in deck-03 sentences. The only place these numbers
# live: topic_write computes with them, the skill refers to them.
# Sentences built only from known words teach nothing beyond the target
# word — topic 11. Lähme välja brought in not a single new one.
NEW_EVERY = 3   # roughly one new word per three topic words
NEW_MAX = 5     # cap per topic fill

# A topic is filled by several topic_write calls, and the dose can't be
# counted per call: five one-word calls demanded five new words, and a
# topic split into five batches got five caps. So calls for one topic
# within this window add up to a single fill.
DOSE_WINDOW_HOURS = 12

# How many times a word must appear in examples to stop counting as
# half-known. The A1 branch is excluded from the index (EXCLUDED_DECK_PREFIXES),
# and without a threshold täna (143 hits), koer (93), ema (47) ended up
# "half-known" — exactly the words the learner knows best. Genuinely
# half-known words appeared once or twice: ümbrik, pakiautomaat.
HALF_KNOWN_MAX = 2

# Repetition of one word across examples. Coverage alone doesn't guard
# against it: ten sentences in a row with panema score an honest 100%, and
# text_check let them through. That's what happened in topic 12. Poes —
# panema in 10 of 133 sentences, nine of them within two days.
#
# The in-batch threshold is the square root of the batch size. Two repeats
# in three sentences is monotony, two in eighty is coincidence, so a
# constant won't do; a ratio won't either, since nobody reaches 25% of
# eighty. The root sits in between: 2 of 6, 4 of 16, 9 of 80.
# Measured on all A2 topics: a constant cap of 4 gave 20 words per line
# (max 111), the root — 4 on average and never more than 9.
REPEAT_MIN = 2          # never count fewer than this as a repeat
REPEAT_SHOWN = 6       # words shown in each of the two lines

# The second trigger is the word's weight across the whole topic; without
# it the drip case isn't caught at all: panema accumulated in 12. Poes one
# or two per call, and the batch threshold can't fire on that by design.
#
# One number isn't enough: in 12. Poes, with 613 sentences, half the
# vocabulary reaches "six times". So we take the top of the topic's
# ranking. Its length was tuned on the same topic: with 12 words panema
# didn't make the top until the very end (uus, suur, müüja and others
# crowded it out), and only two of its ten sentences would have been
# flagged; with 25 — all ten.
REPEAT_IN_TOPIC = 6     # below this, don't look at the ranking at all
REPEAT_TOP = 25         # length of the top

# Scaffold verbs: you can't build a sentence without them and there's no
# point rotating them — the same words vocab_pool leaves out of hints.
STRUCTURAL_VERBS = {
    "minema", "tulema", "tegema", "saama", "tahtma", "meeldima",
    "pidama", "võima", "käima", "hakkama", "olema",
}
