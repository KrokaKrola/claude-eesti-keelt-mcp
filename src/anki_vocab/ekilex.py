"""Word forms from the EKI dictionary via the Ekilex API.

The forms on a card are what the learner memorizes, and a guessed form is
worse than a missing one: that's how 'kudub' got into the deck instead of
koob, and the whole word 'kesküte' instead of keskküte. Vabamorf's
morphology is no judge here — it synthesizes forms by rule but doesn't
know which ones the dictionary accepts, or whether the word has a plural
at all.

Ekilex is the database Sõnaveeb is built from. The key is free, from your
profile at ekilex.ee: the EKILEX_API_KEY variable or the file
data/ekilex_api_key. Responses are cached in the index forever — a word's
forms don't change, and the network is needed only for the first request.
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

# "Not in the dictionary" is cached too, but not forever: the dictionary grows.
MISS_TTL_DAYS = 30

# The card's canonical forms as Ekilex codes.
NOUN_CODES = ("SgN", "SgG", "SgP", "PlP")
VERB_CODES = ("Sup", "Inf", "IndPrSg1")

# Ekilex returns a missing form as the string '-'.
ABSENT = "-"


class EkilexError(RuntimeError):
    """The dictionary is unavailable: no key, no network, or the API refused."""


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
    """All paradigms of all Estonian homonyms of the word, raw from the API."""
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
    """Cached dictionary access on top of the index."""

    def __init__(self, con: sqlite3.Connection, key: str | None = None):
        self.con = con
        self.key = key if key is not None else api_key()
        # How many words came from the network vs. the cache — for the log.
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
        """Word → its paradigms; an empty list means the word isn't in the dictionary.

        Network only for what isn't cached, and in parallel: twenty-five
        topic words one by one is fifty requests in a row.
        Without a key or network it's EkilexError, not an empty answer:
        "didn't ask the dictionary" and "word not in the dictionary" must
        be distinguishable.
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
    """Variants of each canonical form across all paradigms and homonyms.

    Variants are merged, not chosen: armas has two legitimate paradigms
    (armsat and armast), tee (tea/road) has both teid and teesid as the
    partitive plural. An empty list in a form's place means the dictionary
    doesn't accept it: the mass noun keskküte has no plural at all.
    """
    codes = VERB_CODES if verb else NOUN_CODES
    # One headword occasionally holds both a verb and a noun — take our own class.
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


# A word that lives in the plural (püksid, kaerahelbed) is written on the
# card with plural forms — and that's legitimate, not a form error.
PLURAL_OF = {"SgN": "PlN", "SgG": "PlG", "SgP": "PlP"}


def allowed_forms(paradigms: list[Paradigm], verb: bool) -> list[list[str]]:
    """What's accepted at each card position: the canonical variants, and for
    a noun's first three also the plural form of the same case."""
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
    """(rejections, warnings) for one word's forms.

    A word missing from the dictionary is only a warning: a rare compound
    may well be absent, and it can't be forbidden. A form that disagrees
    with the dictionary is a rejection, along with what the dictionary
    considers correct.
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
