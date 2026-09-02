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


class Lemmatizer:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.vm = Vabamorf.instance()
        self.mem: dict[str, tuple[tuple[tuple[str, str], ...], ...]] = {}
        self.hits = 0
        self.misses = 0
        for word, blob in con.execute("select word, analyses from lemma_cache"):
            self.mem[word] = tuple(
                tuple(tuple(a) for a in group) for group in json.loads(blob)
            )

    def _read(self, word: str, disambiguate: bool) -> tuple[tuple[str, str], ...]:
        try:
            raw = self.vm.analyze([word], disambiguate=disambiguate)
        except Exception:
            return ()
        out: list[tuple[str, str]] = []
        seen = set()
        for entry in raw:
            for a in entry.get("analysis", ()):
                lem = _MARKERS_RE.sub("", a.get("lemma", "")).lower().strip()
                pair = (lem, a.get("partofspeech", ""))
                if lem and pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        return tuple(out)

    def _cached(self, word: str) -> tuple[tuple[tuple[str, str], ...], ...]:
        key = word.lower()
        cached = self.mem.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        self.misses += 1
        primary = self._read(word, True) or ((key, ""),)
        result = (primary, self._read(word, False))
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
        выбор между sall и sallima честно неоднозначен."""
        key = word.strip().lower()
        found = {lem for lem, _ in self.analyses(key)}
        if key not in found:
            found |= {lem for lem, _ in self.variants(key)}
        return found
