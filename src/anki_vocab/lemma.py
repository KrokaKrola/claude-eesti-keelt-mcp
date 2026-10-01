"""Лемматизация через Vabamorf с кэшем в индексе.

Строковые эвристики тут не работают: эстонский меняет ступень внутри
основы (kleit → kleidi → kleiti, aeg → aja → aega), так что префиксное
сравнение ломается. Нужен настоящий морфоанализатор.
"""
from __future__ import annotations

import json
import re
import sqlite3

from estnltk.vabamorf.morf import Vabamorf

_MARKERS_RE = re.compile(r"[_=+]")

# Русские теги частей речи из карточек → коды Vabamorf, для снятия омонимии.
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


# Сравнительная степень, которую по основе не восстановить.
_SUPPLETIVE = {"parem": "hea", "parim": "hea"}

# Разборы, которые не равны самостоятельному слову: наречие или
# послелог, застывший из падежной формы ('jalga', 'lukku', 'samas').
_FROZEN = {"D", "K"}

Analyses = tuple[tuple[str, str], ...]


class Lemmatizer:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.vm = Vabamorf.instance()
        # слово → (основной разбор, все разборы, есть ли слово в словаре)
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
        """Сравнительная степень → исходное прилагательное.

        Vabamorf считает 'olulisem' отдельной леммой, и в отчётах
        сравнительная степень знакомого слова выходила новым словом.
        Сравнительная степень строится от основы генитива плюс -m
        (suure-m, olulise-m, tervisliku-m), иногда с a → e (pika → pikem,
        sooja → soojem): отрезаем -m и проверяем, что остаток — генитив
        прилагательного. Не вышло — оставляем как есть.
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
        # Кэш — только ускорение. Запись делаем по возможности: индекс
        # может быть заблокирован другим процессом (сборка из CLI, второй
        # сервер), и ронять из-за этого чтение нельзя. В памяти разбор
        # всё равно останется до конца работы процесса.
        try:
            self.con.execute(
                "insert or replace into lemma_cache(word, analyses) values (?, ?)",
                (key, json.dumps(result)),
            )
        except sqlite3.OperationalError:
            pass
        return result

    def analyses(self, word: str) -> tuple[tuple[str, str], ...]:
        """[(лемма, код части речи), ...] со снятой омонимией — обычно
        один разбор, и в большинстве случаев верный."""
        return self._cached(word)[0]

    def variants(self, word: str) -> tuple[tuple[str, str], ...]:
        """Все разборы, без снятия омонимии. Слова сюда приходят по
        одному, вне контекста, и снятие омонимии тогда просто выбирает
        частотный вариант: 'salli' становится глаголом sallima, хотя на
        карточке это шарф. Полный список нужен, чтобы тег части речи с
        карточки было из чего выбрать."""
        return self._cached(word)[1]

    def in_dictionary(self, word: str) -> bool:
        """Знает ли слово сам словарь Vabamorf — без угадывания.

        Иначе опечатка неотличима от нового слова: 'söin' вместо 'sõin'
        анализатор угадывает существительным, и в отчёте она выглядела
        честным новым словом, а 'mureled' вместо 'murelid' даже ушло в
        дозу. Сложные слова словарь собирает сам: kaerahelbepuder он знает.
        """
        return self._cached(word)[2]

    def sentence(self, tokens: list[str]) -> list[tuple[str, str]]:
        """(лемма, код части речи) для каждого слова — в контексте фразы.

        Слово по одному разбирается плохо: снятие омонимии тогда просто
        берёт частотный разбор, и 'jalga' в «pani kingad jalga» выходило
        наречием, а не ногой. Регистр сохраняем: только по нему видно,
        что 'Lätist' — Латвия, а не неизвестное слово.
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
        """Собственное имя: все разборы — H (Tallinn, Eesti).

        Мимо кэша, и намеренно. Регистр здесь решает всё: 'tallinn' со
        строчной буквы разбирается как обычное существительное. А кэш
        ключуется по слову в нижнем регистре, так что ответ зависел бы
        от того, в каком виде слово попалось анализатору первым.

        Строгое «все разборы H» оставляет Mari и Jaan в словах: у них
        рядом есть нарицательный разбор (mari — ягода). Так и надо —
        пропустить чужое имя дешевле, чем спрятать настоящее слово.
        """
        codes = {code for _, code in self._read(word.capitalize(), False) if code}
        return bool(codes) and codes <= {"H"}

    def lemma(self, word: str, pos: str | None = None) -> str:
        """Одна лемма. Тег части речи с карточки снимает омонимию:
        сначала по основному разбору, потом по остальным вариантам."""
        primary = self.analyses(word)
        if pos:
            codes = POS_CODES.get(pos, set())
            for lem, code in primary + self.variants(word):
                if code in codes:
                    return lem
        return primary[0][0]

    def lemmas(self, word: str) -> set[str]:
        """Кандидаты для поиска слова в индексе.

        Отброшенные омонимией разборы добавляем только к словоформам.
        Если основной разбор говорит, что слово и так стоит в словарной
        форме, ему верим: 'koli' — это хлам, и подхватив из вариантов
        чужую лемму kolima (переезжать), мы выдали бы незнакомое слово
        за знакомое. А вот 'salli' словарной формой не является, и
        выбор между sall и sallima честно неоднозначен.

        Исключение — наречие или послелог, застывший из падежной формы:
        'lukku' сам себе лемма, но это и иллатив от lukk. Поверив ему,
        text_check считал 'lukku' новым словом рядом с целевым lukk.
        Так же и с догадкой анализатора про незнакомое ему слово."""
        key = word.strip().lower()
        primary = self.analyses(key)
        found = {lem for lem, _ in primary}
        frozen = {code for _, code in primary} <= _FROZEN
        if key not in found or frozen or not self.in_dictionary(key):
            found |= {lem for lem, _ in self.variants(key)}
        return found
