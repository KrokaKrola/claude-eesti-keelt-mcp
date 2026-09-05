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
INDEX_VERSION = 6

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

# Доза новых слов в предложениях колоды 03. Единственное место, где эти
# числа живут: topic_write по ним считает, скилл на них ссылается.
# Предложения, собранные только из известного, не учат ничему сверх
# целевого слова — тема 11. Lähme välja не принесла ни одного нового.
NEW_EVERY = 3   # одно новое слово примерно на каждое третье слово темы
NEW_MAX = 5     # потолок на один вызов topic_write

# Сколько раз слово должно попасться в примерах, чтобы перестать считаться
# полузнакомым. Ветка A1 из индекса исключена (EXCLUDED_DECK_PREFIXES), и
# без порога в «полузнакомые» уезжали täna (143 попадания), koer (93),
# ema (47) — то есть ровно то, что человек знает лучше всего. Настоящее
# полузнакомое встречалось один-два раза: ümbrik, pakiautomaat.
HALF_KNOWN_MAX = 2

# Повтор одного слова в примерах. Само по себе покрытие от этого не
# защищает: десять предложений подряд с panema дают честные 100%, и
# text_check их пропускал. В теме 12. Poes так и вышло — panema в 10
# предложениях из 133, девять из них за два дня.
#
# Порог внутри пачки — корень из её размера. Два повтора на три
# предложения это однообразие, два на восемьдесят — совпадение, так что
# константа не годится; доля тоже не годится, потому что 25% от восьмидесяти
# не наберёт никто. Корень держится посередине: 2 из 6, 4 из 16, 9 из 80.
# Замеры на всех темах A2: постоянный потолок в 4 давал по 20 слов в строке
# (максимум 111), корень — 4 в среднем и не больше 9.
REPEAT_MIN = 2          # ниже этого не считаем повтором никогда
REPEAT_SHOWN = 6       # столько слов в каждой из двух строк

# Второй повод — вес слова во всей теме, и без него капельный случай не
# ловится вовсе: panema набирался в 12. Poes по одному-два за вызов, а
# порог пачки на такое не срабатывает по определению.
#
# Одного числа мало: у 12. Poes на 613 предложений «шесть раз» набирает
# половина словаря. Поэтому берём вершину рейтинга темы. Длину вершины
# подбирали по той же теме: при 12 словах panema не попадал в неё до
# самого конца (его закрывали uus, suur, müüja и прочие), и из десяти его
# предложений предупреждение получили бы два; при 25 — все десять.
REPEAT_IN_TOPIC = 6     # реже — на рейтинг не смотрим вовсе
REPEAT_TOP = 25         # длина вершины

# Глаголы-каркас: без них фразу не построить, и ротировать их незачем —
# те же слова, которые vocab_pool не показывает в подсказках.
STRUCTURAL_VERBS = {
    "minema", "tulema", "tegema", "saama", "tahtma", "meeldima",
    "pidama", "võima", "käima", "hakkama", "olema",
}
