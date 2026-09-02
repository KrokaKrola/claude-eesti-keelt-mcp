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
        "The learner's Estonian vocabulary from Anki. Before composing "
        "examples, load the palette with vocab_pool and build phrases from "
        "it; check drafts with text_check. Run new word lists through "
        "vocab_check before creating cards."
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
        "Are these words already in the collection? Takes any inflected "
        "form: kübarat finds kübar. ✓ — the word has a word card, ~ — it "
        "only ever appeared inside example sentences, ✗ — not there at all. "
        "Use it before creating cards so duplicates don't pile up."
    )
)
@traced
def vocab_check(words: list[str]) -> str:
    with _lock:
        vocab = _fresh()
        return format_check([vocab.lookup(w) for w in words])


@mcp.tool(
    description=(
        "The palette: words from the learner's own word cards — compose "
        "your examples out of these. They arrive in dictionary form, as "
        "written on the card: nominative for nouns, ma-infinitive for "
        "verbs. In a sentence, inflect as the grammar demands (salli, "
        "kaela, sooja) — coverage is measured by lemma, so any form counts. "
        "Returns two groups: 'topic' (matched on topics) and 'rest of "
        "vocabulary' — the most frequent words of that level regardless of "
        "topic. For a new topic the first group is empty, which is normal: "
        "the phrase is built from the core, the topic supplies the nouns. "
        "Filters: level ('a1'/'a2'), topics (OR), pos ('существительное', "
        "'глагол', 'прилагательное', 'наречие' — Russian, as tagged on the "
        "cards). Do not pass limit: by default the whole vocabulary of that "
        "level arrives, and any number hides the same part of it forever. "
        "If the output grows too big, narrow it with pos. "
        "A separate line lists words that have not appeared in new cards "
        "for a while (threshold: stale_days) — a hint for variety, not a "
        "constraint. "
        "Note: the palette is narrower than what the learner knows — words "
        "without a word card (met only inside sentences) never show up here "
        "even though they count as known. Absence from the palette does not "
        "mean a word is new; only vocab_check and text_check know that."
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
        "Checks draft sentences against the learner's vocabulary. For each "
        "one returns a coverage percentage and the unfamiliar words. "
        "targets — the words the cards are teaching: they are new by "
        "definition and must not count, always pass them. The rule is soft: "
        "one new word is always fine, beyond that coverage decides "
        "(min_coverage, 70% by default). Function words, postpositions and "
        "conjunctions are ignored. Rewrite whatever comes back 'low' leaning "
        "on vocab_pool; list the remaining new words in the final report as "
        "candidates for the next topic."
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
        "The forms of a word and the ready examples from its card. Use it "
        "so re-adding a word does not invent a second set of forms."
    )
)
@traced
def word_forms(word: str) -> str:
    with _lock:
        vocab = _fresh()
        hit = vocab.lookup(word)
        if hit.status == UNKNOWN:
            return f"{word}: not in the collection"
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
    out.append("forms: " + "/".join(hit.forms))
    for et, ru in examples[:3]:
        out.append(f"  {et} — {ru}")
    return "\n".join(out)


@mcp.tool(
    description=(
        "State of a topic: which of the subdecks 01. sõnad / 02. sõnad "
        "typein / 03. lause typein exist, how many notes each holds and "
        "which words are already in them. Replaces browsing decks before "
        "adding cards."
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
        "Writes a topic into Anki: one word → 5 notes across three subdecks "
        "(01. sõnad, 02. sõnad typein, 03. lause typein). Form labels, tags, "
        "note order and deck creation are handled here — supply only the "
        "content. Anki must be running.\n"
        "words — a list of dicts: word (the Estonian word), pos "
        "('существительное'/'глагол'/'прилагательное'/'наречие'/"
        "'местоимение'/… — Russian, as tagged on the cards), ru (the Russian "
        "translation), forms (3 forms per the rules of that part of speech; "
        "one form is enough for an invariant word), ex (3 pairs "
        "['estonian example','russian'], example N shows form N; one pair is "
        "enough for an invariant word), sentence (pair ['new sentence', "
        "'russian'] for deck 03).\n"
        "Up to 33 words per call. Validation runs before the write: on bad "
        "data no note is created at all and every complaint comes back at "
        "once. dry_run=true shows what would be written, touching nothing."
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
            return "nothing written, fix the data:\n" + "\n".join(
                f"  {p}" for p in exc.problems
            )
        except AnkiError as exc:
            return f"nothing written: {exc}"
        if not dry_run:
            ensure_fresh(_con)  # новые слова сразу видны vocab_check
    return report


@mcp.tool(
    description=(
        "Finds a deck by a fragment of its name — when the exact spelling is "
        "forgotten or misspelled. Reads a copy of the collection, so Anki "
        "does not need to be running."
    )
)
@traced
def deck_find(pattern: str) -> str:
    needle = pattern.strip().lower()
    if not needle:
        return "empty pattern"
    con = open_snapshot()
    try:
        hits = sorted(n for n in deck_names(con).values() if needle in n.lower())
    finally:
        con.close()
    if not hits:
        return f"no deck matches '{pattern}'"
    return "\n".join(hits[:40]) + (f"\n… +{len(hits) - 40} more" if len(hits) > 40 else "")


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
