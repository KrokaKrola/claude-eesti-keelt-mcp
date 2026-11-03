"""Lemmatization via Vabamorf with a cache in the index.

String heuristics don't work here: Estonian changes the stem grade
(kleit → kleidi → kleiti, aeg → aja → aega), so prefix comparison
breaks. A real morphological analyzer is needed.
"""
from __future__ import annotations

import json
import re
import sqlite3

from estnltk.vabamorf.morf import Vabamorf

_MARKERS_RE = re.compile(r"[_=+]")

# Russian part-of-speech tags from cards → Vabamorf codes, for disambiguation.
POS_CODES = {
    "существительное": {"S", "H"},
    "глагол": {"V"},
    "прилагательное": {"A", "C", "U", "G"},
    "наречие": {"D"},
    "местоимение": {"P"},
    "числительное": {"N", "O"},
    "предлог": {"K"},
    "послелог": {"K"},
    "союз": {"J"},
}


# Comparatives that can't be restored from the stem.
_SUPPLETIVE = {"parem": "hea", "parim": "hea"}

# Analyses that aren't a standalone word: an adverb or postposition
# frozen from a case form ('jalga', 'lukku', 'samas').
_FROZEN = {"D", "K"}

Analyses = tuple[tuple[str, str], ...]


class Lemmatizer:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.vm = Vabamorf.instance()
        # word → (primary analysis, all analyses, whether the dictionary knows it)
        self.mem: dict[str, tuple[Analyses, Analyses, bool]] = {}
        self.positives: dict[str, str] = {}
        self.hits = 0
        self.misses = 0
        for word, blob in con.execute("select word, analyses from lemma_cache"):
            primary, variants, known = json.loads(blob)
            self.mem[word] = (
                tuple(tuple(a) for a in primary),
                tuple(tuple(a) for a in variants),
                known,
            )

    def _pairs(self, entry: dict) -> list[tuple[str, str]]:
        out = []
        for a in entry.get("analysis", ()):
            lem = _MARKERS_RE.sub("", a.get("lemma", "")).lower().strip()
            code = a.get("partofspeech", "")
            if code == "C":
                lem = self.positive(lem)
            if lem and (lem, code) not in out:
                out.append((lem, code))
        return out

    def _read(self, word: str, disambiguate: bool) -> Analyses:
        try:
            raw = self.vm.analyze([word], disambiguate=disambiguate)
        except Exception:
            return ()
        return tuple(pair for entry in raw for pair in self._pairs(entry))

    def positive(self, comparative: str) -> str:
        """Comparative → base adjective.

        Vabamorf treats 'olulisem' as a lemma of its own, and in reports the
        comparative of a known word came out as a new word. A comparative
        is the genitive stem plus -m (suure-m, olulise-m, tervisliku-m),
        sometimes with a → e (pika → pikem, sooja → soojem): strip the -m
        and check that the rest is an adjective's genitive. If not, keep
        the word as is.
        """
        if comparative in _SUPPLETIVE:
            return _SUPPLETIVE[comparative]
        cached = self.positives.get(comparative)
        if cached is not None:
            return cached
        found = comparative
        if comparative.endswith("m") and len(comparative) > 3:
            stem = comparative[:-1]
            stems = [stem] + ([stem[:-1] + "a"] if stem.endswith("e") else [])
            for candidate in stems:
                try:
                    raw = self.vm.analyze(
                        [candidate], disambiguate=False, guess=False, propername=False
                    )
                except Exception:
                    break
                lemma = next(
                    (
                        _MARKERS_RE.sub("", a["lemma"]).lower()
                        for entry in raw
                        for a in entry.get("analysis", ())
                        if a.get("partofspeech") == "A" and a.get("form") == "sg g"
                    ),
                    None,
                )
                if lemma:
                    found = lemma
                    break
        self.positives[comparative] = found
        return found

    def _in_dictionary(self, word: str) -> bool:
        try:
            raw = self.vm.analyze(
                [word], disambiguate=False, guess=False, propername=False
            )
        except Exception:
            return True
        return any(entry.get("analysis") for entry in raw)

    def _cached(self, word: str) -> tuple[Analyses, Analyses, bool]:
        key = word.lower()
        cached = self.mem.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        primary = self._read(word, True) or ((key, ""),)
        result = (primary, self._read(word, False), self._in_dictionary(key))
        self.mem[key] = result
        # The cache is only a speedup. Writing is best-effort: the index
        # may be locked by another process (a CLI build, a second server),
        # and reads must not fail because of it. The analysis stays in
        # memory until the process exits anyway.
        try:
            self.con.execute(
                "insert or replace into lemma_cache(word, analyses) values (?, ?)",
                (key, json.dumps(result)),
            )
        except sqlite3.OperationalError:
            pass
        return result

    def analyses(self, word: str) -> tuple[tuple[str, str], ...]:
        """[(lemma, POS code), ...] after disambiguation — usually one
        analysis, and correct in most cases."""
        return self._cached(word)[0]

    def variants(self, word: str) -> tuple[tuple[str, str], ...]:
        """All analyses, without disambiguation. Words arrive here one at a
        time, without context, so disambiguation just picks the frequent
        reading: 'salli' becomes the verb sallima, while on the card it's a
        scarf. The full list is needed so the card's POS tag has something
        to choose from."""
        return self._cached(word)[1]

    def in_dictionary(self, word: str) -> bool:
        """Whether Vabamorf's own dictionary knows the word — no guessing.

        Otherwise a typo is indistinguishable from a new word: the analyzer
        guesses 'söin' (for 'sõin') as a noun, and in the report it looked
        like an honest new word, while 'mureled' (for 'murelid') even made
        it into the dose. The dictionary assembles compounds itself: it
        knows kaerahelbepuder.
        """
        return self._cached(word)[2]

    def sentence(self, tokens: list[str]) -> list[tuple[str, str]]:
        """(lemma, POS code) for each word — in the context of the phrase.

        Words parse poorly one at a time: disambiguation then just takes the
        frequent reading, and 'jalga' in «pani kingad jalga» came out as an
        adverb, not "leg". Case is kept: only by it can you tell 'Lätist'
        is Latvia and not an unknown word.
        """
        if not tokens:
            return []
        try:
            raw = self.vm.analyze(list(tokens), disambiguate=True)
        except Exception:
            raw = []
        if len(raw) != len(tokens):
            return [self.analyses(t)[0] for t in tokens]
        return [
            (pairs[0] if (pairs := self._pairs(entry)) else self.analyses(tok)[0])
            for tok, entry in zip(tokens, raw)
        ]

    def is_name(self, word: str) -> bool:
        """Proper name: all analyses are H (Tallinn, Eesti).

        Bypasses the cache on purpose. Case decides everything here:
        lowercase 'tallinn' parses as a common noun. The cache is keyed
        by the lowercased word, so the answer would depend on which form
        the analyzer happened to see first.

        Strict "all analyses H" keeps Mari and Jaan as words: they have a
        common-noun reading nearby (mari — berry). That's intended —
        missing someone's name is cheaper than hiding a real word.
        """
        codes = {code for _, code in self._read(word.capitalize(), False) if code}
        return bool(codes) and codes <= {"H"}

    def lemma(self, word: str, pos: str | None = None) -> str:
        """A single lemma. The card's POS tag disambiguates: first among
        the primary analyses, then among the other variants."""
        primary = self.analyses(word)
        if pos:
            codes = POS_CODES.get(pos, set())
            for lem, code in primary + self.variants(word):
                if code in codes:
                    return lem
        return primary[0][0]

    def lemmas(self, word: str) -> set[str]:
        """Candidates for looking a word up in the index.

        Analyses dropped by disambiguation are added only for inflected
        forms. If the primary analysis says the word is already in its
        dictionary form, we trust it: 'koli' is junk, and by picking the
        foreign lemma kolima (to move house) from the variants we would
        pass an unknown word off as known. 'salli', on the other hand, is
        not a dictionary form, and sall vs. sallima is genuinely ambiguous.

        The exception is an adverb or postposition frozen from a case form:
        'lukku' is its own lemma, but also the illative of lukk. Trusting
        it, text_check counted 'lukku' as a new word next to the target lukk.
        Same for the analyzer's guess about a word it doesn't know."""
        key = word.strip().lower()
        primary = self.analyses(key)
        found = {lem for lem, _ in primary}
        frozen = {code for _, code in primary} <= _FROZEN
        if key not in found or frozen or not self.in_dictionary(key):
            found |= {lem for lem, _ in self.variants(key)}
        return found
