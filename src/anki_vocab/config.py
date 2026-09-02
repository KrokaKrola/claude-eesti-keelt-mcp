"""Пути и классификация тегов."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"

_DEFAULT_COLLECTION = (
    Path.home()
    / "Library/Application Support/Anki2/Kekw/collection.anki2"
)

COLLECTION = Path(os.environ.get("ANKI_COLLECTION", _DEFAULT_COLLECTION))
SNAPSHOT = DATA_DIR / "snapshot.anki2"
INDEX = DATA_DIR / "vocab.sqlite"

# Настройки аддона Anki MCP Server: оттуда берём адрес и порт, на
# которых он слушает. Пользователь может поменять их мышкой.
ADDON_META = (
    Path.home() / "Library/Application Support/Anki2/addons21/124672614/meta.json"
)

# Файлы SQLite, которые нужно копировать вместе: без -wal данные отстают.
COLLECTION_PARTS = ("", "-wal", "-shm")

EXPECTED_SCHEMA_VER = 18
INDEX_VERSION = 4

# Поднимать при любой правке логики разбора в lemma.py: иначе кэш молча
# отдаёт разборы, посчитанные старым кодом.
LEMMATIZER_VERSION = 2

# Область видимости индекса. Сопоставление идёт по границе '::',
# так что 'A1' не зацепит соседнюю ветку с похожим началом.
INCLUDED_DECK_PREFIXES = ("kursuse eesti keel",)

# Временно: эти ветки ещё в старых форматах, их конвертация впереди.
EXCLUDED_DECK_PREFIXES = (
    "en words",
    "kursuse eesti keel::A1",
    'kursuse eesti keel::A2::00. А1',
)


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

# Теги про формат карточки, а не про смысл слова.
SERVICE_TAGS = {"typein", "lause", "предложение", "аудио", "audio", "marked", "leech"}

# Служебные слова: в покрытии не считаются ни известными, ни новыми.
FUNCTION_WORDS = {
    "olema", "ei", "ja", "ning", "aga", "või", "et", "kui", "siis", "ka",
    "ma", "sa", "ta", "me", "te", "nad", "mina", "sina", "tema", "meie",
    "teie", "nemad", "see", "need", "too", "oma", "ise",
    "mis", "kes", "kus", "kuidas", "millal", "miks", "kas", "kumb",
    "väga", "nii", "veel", "juba", "ainult", "ka", "ära", "palun",
    "on", "olen", "oled", "oleme", "olete", "pole", "ole",
    "üks", "kõik", "iga", "mõni", "palju", "vähe",
}
