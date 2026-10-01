"""Формы слова из словаря EKI через Ekilex API.

Формы на карточке — то, что человек заучивает, и угаданная форма хуже
отсутствующей: так в колоду попали 'kudub' вместо koob и целое слово
'kesküte' вместо keskküte. Морфология Vabamorf тут не судья — она
синтезирует форму по правилам, но не знает, какие из них словарь
признаёт и есть ли у слова множественное число вообще.

Ekilex — это база, из которой собран Sõnaveeb. Ключ бесплатный, из
профиля на ekilex.ee: переменная EKILEX_API_KEY или файл
data/ekilex_api_key. Ответы кешируются в индексе навсегда — формы слова
не меняются, а сеть нужна только на первом запросе.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import DATA_DIR

API = "https://ekilex.ee/api"
KEY_FILE = DATA_DIR / "ekilex_api_key"
TIMEOUT = 15.0

# «Нет в словаре» тоже кешируем, но не навсегда: словарь пополняется.
MISS_TTL_DAYS = 30

# Канонические формы карточки в кодах Ekilex.
NOUN_CODES = ("SgN", "SgG", "SgP", "PlP")
VERB_CODES = ("Sup", "Inf", "IndPrSg1")

# Отсутствующую форму Ekilex отдаёт строкой '-'.
ABSENT = "-"


class EkilexError(RuntimeError):
    """Словарь недоступен: нет ключа, нет сети или отказ API."""


@dataclass
class Paradigm:
    word: str
    word_class: str          # noomen | verb | muutumatu
    forms: dict[str, list[str]] = field(default_factory=dict)

    def slot(self, code: str) -> list[str]:
        return self.forms.get(code, [])


def api_key() -> str | None:
    if key := os.environ.get("EKILEX_API_KEY", "").strip():
        return key
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def _get(path: str, key: str):
    request = urllib.request.Request(API + path, headers={"ekilex-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise EkilexError("Ekilex rejected the API key") from exc
        raise EkilexError(f"Ekilex answered {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise EkilexError(f"Ekilex unreachable: {exc}") from exc


def fetch(word: str, key: str) -> list[dict]:
    """Все парадигмы всех эстонских омонимов слова, как есть из API."""
    found = _get("/word/search/" + urllib.parse.quote(word), key).get("words", [])
    out = []
    for entry in found:
        if entry.get("lang") != "est" or entry.get("wordValue", "").lower() != word:
            continue
        for paradigm in _get(f"/paradigm/details/{entry['wordId']}", key):
            forms: dict[str, list[str]] = {}
            for form in paradigm.get("paradigmForms", []):
                value = (form.get("value") or "").strip()
                if value and value != ABSENT:
                    bucket = forms.setdefault(form["morphCode"], [])
                    if value not in bucket:
                        bucket.append(value)
            out.append({
                "word": entry["wordValue"],
                "word_class": paradigm.get("wordClass") or "",
                "forms": forms,
            })
    return out


class Dictionary:
    """Кешированный доступ к словарю поверх индекса."""

    def __init__(self, con: sqlite3.Connection, key: str | None = None):
        self.con = con
        self.key = key if key is not None else api_key()
        # Сколько слов пришло из сети, а сколько из кеша — для лога.
        self.fetched = 0
        self.cached = 0

    def _cached(self, word: str) -> list[dict] | None:
        row = self.con.execute(
            "select data, fetched from ekilex_cache where word = ?", (word,)
        ).fetchone()
        if not row:
            return None
        data = json.loads(row[0])
        if not data and row[1] < time.time() - MISS_TTL_DAYS * 86400:
            return None
        return data

    def lookup_many(self, words: list[str]) -> dict[str, list[Paradigm]]:
        """Слово → его парадигмы; пустой список — слова в словаре нет.

        Сеть только для того, чего нет в кеше, и параллельно: двадцать
        пять слов темы по одному — это полсотни запросов подряд.
        Без ключа и без сети — EkilexError, а не пустой ответ: «словарь
        не спросили» и «слова в словаре нет» должны различаться.
        """
        wanted = list(dict.fromkeys(w.strip().lower() for w in words if w.strip()))
        result: dict[str, list[dict]] = {}
        missing = []
        for word in wanted:
            cached = self._cached(word)
            if cached is None:
                missing.append(word)
            else:
                result[word] = cached
        self.cached += len(wanted) - len(missing)
        self.fetched += len(missing)
        if missing:
            if not self.key:
                raise EkilexError(
                    "no Ekilex API key: set EKILEX_API_KEY or put the key into "
                    f"{KEY_FILE}"
                )
            with ThreadPoolExecutor(max_workers=8) as pool:
                fetched = list(pool.map(lambda w: fetch(w, self.key), missing))
            now = int(time.time())
            self.con.executemany(
                "insert or replace into ekilex_cache(word, data, fetched) values (?, ?, ?)",
                [(w, json.dumps(d, ensure_ascii=False), now) for w, d in zip(missing, fetched)],
            )
            self.con.commit()
            result.update(zip(missing, fetched))
        return {
            word: [Paradigm(p["word"], p["word_class"], p["forms"]) for p in result[word]]
            for word in wanted
        }


def canonical(paradigms: list[Paradigm], verb: bool) -> list[list[str]]:
    """Варианты каждой канонической формы по всем парадигмам и омонимам.

    Варианты складываем, а не выбираем: у armas две законные парадигмы
    (armsat и armast), у tee (чай/дорога) партитив мн. ч. и teid, и teesid.
    Пустой список на месте формы — словарь её не признаёт: у вещественного
    keskküte нет множественного числа вовсе.
    """
    codes = VERB_CODES if verb else NOUN_CODES
    # Под одним заголовком изредка живут и глагол, и имя — берём свой класс.
    own = [p for p in paradigms if (p.word_class == "verb") == verb] or paradigms
    out: list[list[str]] = []
    for code in codes:
        values: list[str] = []
        for paradigm in own:
            values += [v for v in paradigm.slot(code) if v not in values]
        out.append(values)
    return out


def is_verb(paradigms: list[Paradigm]) -> bool:
    return any(p.word_class == "verb" for p in paradigms)


def is_invariant(paradigms: list[Paradigm]) -> bool:
    return bool(paradigms) and all(
        p.word_class == "muutumatu" or not p.forms for p in paradigms
    )


# Слово, которое живёт во множественном числе (püksid, kaerahelbed), на
# карточке пишут формами мн. ч. — и это законно, а не ошибка в форме.
PLURAL_OF = {"SgN": "PlN", "SgG": "PlG", "SgP": "PlP"}


def allowed_forms(paradigms: list[Paradigm], verb: bool) -> list[list[str]]:
    """Что принимается на каждой позиции карточки: канонические варианты,
    а у имени на первых трёх — ещё и форма мн. ч. того же падежа."""
    allowed = canonical(paradigms, verb)
    if not verb:
        for i, code in enumerate(NOUN_CODES[:3]):
            for paradigm in paradigms:
                allowed[i] += [
                    v for v in paradigm.slot(PLURAL_OF[code]) if v not in allowed[i]
                ]
    return allowed


def check_forms(
    word: str, verb: bool, forms: list[str], paradigms: list[Paradigm], labels
) -> tuple[list[str], list[str]]:
    """(отказы, предупреждения) по формам одного слова.

    Слова нет в словаре — только предупреждение: редкое сложное слово
    там может и отсутствовать, а запрещать его нельзя. Форма расходится
    со словарём — отказ, вместе с тем, что словарь считает верным.
    """
    if not paradigms:
        return [], [f"{word}: not in the EKI dictionary — forms not verified"]
    classes = {p.word_class for p in paradigms}
    if verb and "verb" not in classes:
        return [f"{word}: pos is глагол, but the dictionary has no verb '{word}'"], []
    if not verb and classes == {"verb"}:
        return [f"{word}: the dictionary knows '{word}' only as a verb — pos глагол"], []
    if is_invariant(paradigms):
        return [], []
    problems: list[str] = []
    warnings: list[str] = []
    expected = canonical(paradigms, verb)
    allowed = allowed_forms(paradigms, verb)
    for i, form in enumerate(forms):
        label = labels[i] if i < len(labels) else f"form {i + 1}"
        if not expected[i]:
            if i == 3:
                problems.append(
                    f"{word}: the dictionary has no partitive plural — send 3 forms"
                )
            else:
                warnings.append(f"{word}: the dictionary lists no {label}")
            continue
        if form.strip().lower() not in {v.lower() for v in allowed[i]}:
            problems.append(
                f"{word}: {label} '{form}' — the dictionary has "
                + " ~ ".join(expected[i])
            )
    return problems, warnings
