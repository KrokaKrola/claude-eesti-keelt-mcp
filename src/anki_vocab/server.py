"""MCP-сервер: доступ к словарному запасу из Anki.

Каждый вызов сначала проверяет свежесть индекса — stat() по трём файлам
коллекции стоит доли миллисекунды, зато карточка, добавленная десять
минут назад через аддон, уже видна.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import sqlite3
import threading
import time

from mcp.server.mcpserver import MCPServer

from .collection import deck_names, open_snapshot
from .config import INDEX
from .index import ensure_fresh, open_index
from .lemma import Lemmatizer
from .parse import words as split_words
from .query import (
    UNKNOWN,
    Vocab,
    format_check,
    format_pool,
    format_text_check,
    topic_state as _topic_state,
)
from .write import AnkiError, NoteDataError, write_topic

# Пишем только своим логгером и не через root: иначе на уровне DEBUG
# matplotlib, который тянет за собой estnltk, заливает лог своим шумом.
_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")
)
log = logging.getLogger("anki_vocab")
log.setLevel(os.environ.get("ANKI_VOCAB_LOG", "INFO").upper())
log.addHandler(_handler)
log.propagate = False


def _short(value: object, limit: int = 90) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


def traced(fn):
    """Пишет вызовы в stderr — Claude Desktop складывает его в
    ~/Library/Logs/Claude/mcp-server-anki-vocab.log. ANKI_VOCAB_LOG=DEBUG
    добавляет полные ответы инструментов."""

    @functools.wraps(fn)
    def wrapper(**kwargs):
        args = ", ".join(f"{k}={_short(v)}" for k, v in kwargs.items() if v is not None)
        log.info("→ %s(%s)", fn.__name__, args)
        started = time.perf_counter()
        try:
            out = fn(**kwargs)
        except Exception:
            log.exception("✗ %s упал", fn.__name__)
            raise
        ms = (time.perf_counter() - started) * 1000
        log.info("← %s: %d симв. ≈%d токенов, %.0f мс",
                 fn.__name__, len(out), len(out) // 3, ms)
        log.debug("  ответ %s:\n%s", fn.__name__, out)
        return out

    return wrapper


mcp = MCPServer(
    name="anki-vocab",
    instructions=(
        "Словарный запас пользователя из Anki (эстонский). "
        "Перед тем как сочинять примеры, возьми палитру через vocab_pool "
        "и строй фразы из неё; черновики проверяй через text_check. "
        "Перед добавлением новых карточек прогоняй список через vocab_check."
    ),
)

# Индекс открываем лениво: побочный эффект на импорте мешает тестам
# и заставляет платить за подключение даже когда инструмент не вызвали.
_con: sqlite3.Connection | None = None
_vocab: Vocab | None = None
_lock = threading.Lock()


def _fresh() -> Vocab:
    global _con, _vocab
    if _con is None:
        _con = open_index(same_thread=False)
        _vocab = Vocab(_con)
        log.info(
            "индекс: %s заметок, %s лемм, палитра %s · %s",
            *[
                _con.execute(q).fetchone()[0]
                for q in (
                    "select count(*) from entry",
                    "select count(*) from lemma",
                    "select count(distinct lower(headword)) from entry"
                    " where notetype in ('Estonian 3 Forms', 'Estonian Word Forms')",
                )
            ],
            INDEX,
        )
    if ensure_fresh(_con):
        _vocab.lem = Lemmatizer(_con)  # перечитать кэш лемм после досборки
    return _vocab


@mcp.tool(
    description=(
        "Есть ли слова в коллекции. Принимает любые формы: kübarat найдёт "
        "kübar. ✓ — слово из словарной карточки, ~ — встречалось только "
        "внутри предложений, ✗ — нет совсем. Используй перед созданием "
        "карточек, чтобы не плодить дубли."
    )
)
@traced
def vocab_check(words: list[str]) -> str:
    with _lock:
        vocab = _fresh()
        return format_check([vocab.lookup(w) for w in words])


@mcp.tool(
    description=(
        "Палитра: слова со словарных карточек пользователя — из них и "
        "надо составлять примеры. Приходят в словарной форме, как "
        "написано на карточке: номинатив у существительных, "
        "ma-инфинитив у глаголов. В предложении ставь слово в ту форму, "
        "которой требует грамматика (salli, kaela, sooja) — покрытие "
        "считается по леммам, любая форма засчитывается. "
        "Возвращает две группы: «по теме» (совпали по topics) и «ещё из "
        "словаря» — самые частотные слова уровня независимо от темы. "
        "У новой темы первая группа пустая, и это нормально: фраза "
        "строится из ядра, тема даёт существительные. "
        "Фильтры: level ('a1'/'a2'), topics (по ИЛИ), pos "
        "('существительное', 'глагол', 'прилагательное', 'наречие'). "
        "limit не передавай: по умолчанию приходит весь словарь уровня, "
        "а любое число прячет одну и ту же его часть навсегда. Если "
        "выдача стала слишком большой, сужай фильтром pos. "
        "Отдельной строкой идут слова, давно не попадавшие в новые "
        "карточки (порог — stale_days): это подсказка, чем разбавить "
        "примеры, а не ограничение. "
        "Важно: палитра уже того, что человек знает — слова без "
        "словарной карточки (встречались только в предложениях) сюда не "
        "попадают, хотя знакомыми считаются. Отсутствие слова в палитре "
        "не значит, что оно новое; это знают только vocab_check и "
        "text_check."
    )
)
@traced
def vocab_pool(
    level: str | None = None,
    topics: list[str] | None = None,
    pos: str | None = None,
    limit: int | None = None,
    stale_days: int = 14,
) -> str:
    with _lock:
        vocab = _fresh()
        return format_pool(
            *vocab.pool(
                level=level, topics=topics, pos=pos, limit=limit,
                stale_days=stale_days,
            )
        )


@mcp.tool(
    description=(
        "Проверяет черновики предложений по словарю пользователя. Для "
        "каждого возвращает процент покрытия и незнакомые слова. "
        "targets — целевые слова карточек: они новые по определению и "
        "в счёт не идут, всегда их передавай. Правило мягкое: одно новое "
        "слово допустимо всегда, дальше смотрим на покрытие "
        "(min_coverage, по умолчанию 70%). Служебные слова, послелоги и "
        "союзы не считаются. Помеченное «низко» перепиши, опираясь на "
        "vocab_pool; остальные новые слова просто перечисли в отчёте "
        "как кандидатов в следующую тему."
    )
)
@traced
def text_check(
    sentences: list[str],
    targets: list[str] | None = None,
    min_coverage: int = 70,
) -> str:
    with _lock:
        vocab = _fresh()
        goal = {t.strip().lower() for t in targets or []}
        return format_text_check(
            [vocab.check_sentence(s, targets=goal) for s in sentences], min_coverage
        )


@mcp.tool(
    description=(
        "Формы слова и готовые примеры с его карточки. Нужен, чтобы "
        "повторное добавление слова не изобретало второй вариант форм."
    )
)
@traced
def word_forms(word: str) -> str:
    with _lock:
        vocab = _fresh()
        hit = vocab.lookup(word)
        if hit.status == UNKNOWN:
            return f"{word}: в коллекции нет"
        rows = vocab.con.execute(
            "select e.examples from form f join entry e on e.note_id = f.note_id"
            " where f.lemma = ? and e.is_phrase = 0",
            (hit.lemma,),
        ).fetchall()

    examples = []
    for (blob,) in rows:
        for et, ru in json.loads(blob):
            if (et, ru) not in examples:
                examples.append((et, ru))
    out = [f"{hit.lemma} — {hit.ru or '?'} ({hit.pos or '?'}, {hit.level or '?'})"]
    out.append("формы: " + "/".join(hit.forms))
    for et, ru in examples[:3]:
        out.append(f"  {et} — {ru}")
    return "\n".join(out)


@mcp.tool(
    description=(
        "Состояние темы: какие из подколод 01. sõnad / 02. sõnad typein / "
        "03. lause typein уже существуют, сколько в них заметок и какие "
        "слова там лежат. Заменяет перебор колод перед добавлением карточек."
    )
)
@traced
def topic_state(topic: str) -> str:
    with _lock:
        _fresh()
        return _topic_state(_con, topic)


def _form_in_example(vocab: Vocab):
    """Проверка «пример содержит форму» по леммам, а не по буквам:
    у глагола algama пример законно идёт с algab, и строковое сравнение
    ругалось бы на каждой второй карточке."""

    def check(form: str, sentence: str) -> bool:
        target = vocab.lem.lemmas(form)
        if not target:  # не разобрали слово — не придираемся
            return True
        return any(target & vocab.lem.lemmas(w) for w in split_words(sentence))

    return check


@mcp.tool(
    description=(
        "Записывает тему в Anki: одно слово → 5 заметок трёх подколод "
        "(01. sõnad, 02. sõnad typein, 03. lause typein). Подписи форм, "
        "теги, порядок заметок и создание колод сервер делает сам — "
        "передавай только содержание. Anki должен быть запущен.\n"
        "words — список словарей: word (эстонское слово), pos "
        "('существительное'/'глагол'/'прилагательное'/'наречие'/"
        "'местоимение'/…), ru (перевод), forms (3 формы по правилам "
        "части речи; для неизменяемого слова достаточно одной), ex "
        "(3 пары ['эстонский пример','перевод'], пример N показывает "
        "форму N; для неизменяемого хватит одной пары), sentence "
        "(пара ['новое предложение','перевод'] для колоды 03).\n"
        "До 33 слов за вызов. Проверка идёт до записи: если данные "
        "кривые, не создаётся ни одной заметки и приходит список всех "
        "претензий сразу. dry_run=true показывает, что будет записано, "
        "ничего не трогая."
    )
)
@traced
def topic_write(
    topic: str,
    words: list[dict],
    course: str = "kursuse eesti keel::A2",
    dry_run: bool = False,
) -> str:
    with _lock:
        vocab = _fresh()
        try:
            report = write_topic(
                course, topic, words,
                form_in_example=_form_in_example(vocab),
                dry_run=dry_run,
            )
        except NoteDataError as exc:
            return "не записано, поправь данные:\n" + "\n".join(
                f"  {p}" for p in exc.problems
            )
        except AnkiError as exc:
            return f"не записано: {exc}"
        if not dry_run:
            ensure_fresh(_con)  # новые слова сразу видны vocab_check
    return report


@mcp.tool(
    description=(
        "Ищет колоду по куску имени — когда точное написание забыто или "
        "в нём опечатка. Читает копию коллекции, Anki для этого не нужен."
    )
)
@traced
def deck_find(pattern: str) -> str:
    needle = pattern.strip().lower()
    if not needle:
        return "пустой запрос"
    con = open_snapshot()
    try:
        hits = sorted(n for n in deck_names(con).values() if needle in n.lower())
    finally:
        con.close()
    if not hits:
        return f"колод с «{pattern}» нет"
    return "\n".join(hits[:40]) + (f"\n… ещё {len(hits) - 40}" if len(hits) > 40 else "")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
