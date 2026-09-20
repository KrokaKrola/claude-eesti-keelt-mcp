"""MCP-сервер: доступ к словарному запасу из Anki.

Каждый вызов сначала проверяет свежесть индекса — stat() по трём файлам
коллекции стоит доли миллисекунды, зато карточка, добавленная десять
минут назад через аддон, уже видна.
"""
from __future__ import annotations

import contextvars
import functools
import itertools
import json
import logging
import os
import sys
import sqlite3
import textwrap
import threading
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .collection import deck_names, open_snapshot
from .config import COLLECTION, DATA_DIR, INDEX, PROJECT_ROOT
from .index import ensure_fresh, open_index
from .lemma import Lemmatizer
from .parse import words as split_words
from .query import (
    KNOWN,
    SEEN,
    UNKNOWN,
    Vocab,
    format_check,
    format_debt,
    format_pool,
    format_repeats,
    format_text_check,
    sentence_ok,
    topic_state as _topic_state,
)
from .passthrough import catalog, describe, invoke, mutating, names
from .write import AnkiError, NoteDataError, endpoint, write_topic

# Пишем только своим логгером и не через root: иначе на уровне DEBUG
# matplotlib, который тянет за собой estnltk, заливает лог своим шумом.
_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)-5s %(call)s%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    defaults={"call": ""},
)

# Номер вызова живёт в строках →, ← и во всём, что пишется между ними, —
# включая досборку индекса из index.py: тот пишет в этот же логгер.
# Без номера разбор двух вызовов подряд склеивается в одну кашу.
_calls = itertools.count(1)
_call_no = contextvars.ContextVar("anki_vocab_call", default=0)


class _CallNumber(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.call = f"#{_call_no.get()} " if _call_no.get() else ""
        return True


_handler = logging.StreamHandler(sys.stderr)
_handler.setFormatter(_FORMAT)
log = logging.getLogger("anki_vocab")
log.setLevel(os.environ.get("ANKI_VOCAB_LOG", "INFO").upper())
log.addFilter(_CallNumber())
log.addHandler(_handler)
log.propagate = False


def _open_log_file() -> Path | None:
    """Свой файл рядом с индексом вдобавок к stderr.

    Клиент держит свою копию stderr пару дней и обрезает её при
    перезапуске, а разбирать поток вызовов приходится и через неделю.
    Пустой ANKI_VOCAB_LOG_FILE выключает файл совсем.
    """
    raw = os.environ.get("ANKI_VOCAB_LOG_FILE", str(DATA_DIR / "server.log"))
    if not raw:
        return None
    path = Path(raw)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError as exc:
        log.warning("файл лога недоступен (%s): %s", path, exc)
        return None
    handler.setFormatter(_FORMAT)
    log.addHandler(handler)
    return path


LOG_FILE = _open_log_file()


def detail(msg: str, *args, level: int = logging.INFO) -> None:
    """Строка-вердикт внутри текущего вызова."""
    log.log(level, "  " + msg, *args)


def _listing(label: str, items, cap: int = 15) -> str:
    items = list(items)
    if not items:
        return ""
    tail = f" +{len(items) - cap}" if len(items) > cap else ""
    return label + ", ".join(items[:cap]) + tail


def _short(value: object, limit: int = 90) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Потолок дампа аргументов в DEBUG. topic_write на 33 слова — это 17k
# символов, и лог за сессию распухал бы сильнее, чем его потом читают.
# 12k покрывают тему слов на двадцать целиком, а режется только предел.
ARGS_DUMP_MAX = 12000


def _dump(kwargs: dict) -> str:
    text = json.dumps(kwargs, ensure_ascii=False, indent=1)
    if len(text) <= ARGS_DUMP_MAX:
        return text
    return (
        text[:ARGS_DUMP_MAX]
        + f"\n… обрезано, всего {len(text)} симв. (ARGS_DUMP_MAX={ARGS_DUMP_MAX})"
    )


def traced(fn):
    """Пишет вызовы в stderr — Claude Desktop складывает его в
    ~/Library/Logs/Claude/mcp-server-anki-vocab.log. ANKI_VOCAB_LOG=DEBUG
    добавляет полные аргументы и ответы инструментов."""

    @functools.wraps(fn)
    def wrapper(**kwargs):
        # Номер снимаем в finally: без этого всё, что пишется между
        # вызовами, приписывается предыдущему.
        token = _call_no.set(next(_calls))
        try:
            args = ", ".join(
                f"{k}={_short(v)}" for k, v in kwargs.items() if v is not None
            )
            log.info("→ %s(%s)", fn.__name__, args)
            # Полные аргументы отдельной строкой. В INFO они обрезаны до 90
            # символов, и по логу нельзя было понять, какие предложения
            # проверялись: вердикт есть, текста к нему нет.
            if kwargs and log.isEnabledFor(logging.DEBUG):
                log.debug("  аргументы %s:\n%s", fn.__name__, _dump(kwargs))
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
        finally:
            _call_no.reset(token)

    return wrapper


mcp = MCPServer(
    name="anki-vocab",
    instructions=(
        "The learner's Estonian vocabulary from Anki. Before composing "
        "examples, load the palette with vocab_pool and build phrases from "
        "it; check drafts with text_check. Run new word lists through "
        "vocab_check before creating cards. "
        "These tools speak English to save tokens; answer the user in Russian."
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
        detail(
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
        hits = [vocab.lookup(w) for w in words]
        # Не только счёт, но и разбивка по словам: этим же вызовом
        # набирают дозу новых слов для колоды 03 (✗) и отсеивают дубли
        # (✓), и постфактум надо видеть, что куда попало.
        counts = Counter(h.status for h in hits)
        picked = lambda status: [h.query for h in hits if h.status == status]
        detail(
            "✓%d ~%d ✗%d из %d%s%s%s",
            counts[KNOWN], counts[SEEN], counts[UNKNOWN], len(hits),
            _listing(" · ✗ новые: ", picked(UNKNOWN)),
            _listing(" · ~ только в примерах: ", picked(SEEN)),
            _listing(" · ✓ уже есть: ", picked(KNOWN)),
        )
        return format_check(hits)


@mcp.tool(
    description=(
        "The palette: words from the learner's own word cards — compose "
        "your examples out of these. They arrive in dictionary form, as "
        "written on the card: nominative for nouns, ma-infinitive for "
        "verbs. In a sentence, inflect as the grammar demands (salli, "
        "sooja, mütsi) — coverage is measured by lemma, so any form counts. "
        "Returns two groups: 'topic' (matched on topics) and 'rest of "
        "vocabulary' — the most frequent words of that level regardless of "
        "topic. For a new topic the first group is empty, which is normal: "
        "the phrase is built from the core, the topic supplies the nouns. "
        "Filters: level ('a1'/'a2'), topics (OR), pos ('существительное', "
        "'глагол', 'прилагательное', 'наречие' — Russian, as tagged on the "
        "cards). Do not pass limit: by default the whole vocabulary of that "
        "level arrives, and any number hides the same part of it forever. "
        "If the output grows too big, narrow it with pos. "
        "Two extra lines at the end. 'not used lately' — words that have "
        "not appeared in new cards for a while (threshold: stale_days), a "
        "hint for variety. 'forgotten in reviews' — words the learner keeps "
        "failing in Anki and has not carried to a mature interval; prefer "
        "these when you need a content word, each one buys an extra "
        "encounter where memory is weak. Both are hints, not constraints. "
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
        palette = vocab.pool(
            level=level, topics=topics, pos=pos, limit=limit,
            stale_days=stale_days,
        )
        # Из чего вообще было позволено составлять предложения. Без этой
        # строки однообразие в готовой теме не отличить от бедной палитры.
        detail(
            "палитра: тема %d, ядро %d, забытых %d, залежавшихся %d ·"
            " level=%s topics=%s pos=%s",
            len(palette.topical), len(palette.core),
            len(palette.weak), len(palette.stale),
            level or "—", ",".join(topics or []) or "—", pos or "—",
        )
        if limit:
            detail("⚠ передан limit=%d — скрыто слов: %d", limit, palette.dropped)
        detail("забытые: %s", " · ".join(palette.weak) or "нет", level=logging.DEBUG)
        detail("залежавшиеся: %s", " · ".join(palette.stale) or "нет",
               level=logging.DEBUG)
        return format_pool(palette)


@mcp.tool(
    description=(
        "Out of words to teach? This is the learner's own backlog: words "
        "that turn up inside their example sentences but never got a card "
        "of their own, so the forms and the meaning were never studied. "
        "Ranked by how often they have met them, with the shortest "
        "sentence they met it in. Returns 10 at a time; if none of them "
        "fit, call again with offset=10, 20, … The words arrive without a "
        "translation — there is no card to take one from."
    )
)
@traced
def vocab_debt(limit: int = 10, offset: int = 0) -> str:
    with _lock:
        vocab = _fresh()
        page = vocab.debt(limit=limit, offset=offset)
        detail("долг: %d слов, offset %d%s",
               len(page.items), page.offset,
               _listing(" · ", (item.word for item in page.items)))
        return format_debt(page)


@mcp.tool(
    description=(
        "The learner says they already know some of the words vocab_debt "
        "offered? Mark them here and they stop being offered — for good, "
        "in this and every later chat. Without it the same ten words come "
        "back every time: vocab_debt reads the index, the index is built "
        "from Anki, and the learner's answer is nowhere in it. Takes any "
        "form of the word. undo=true un-marks them again."
    )
)
@traced
def vocab_debt_skip(words: list[str], undo: bool = False) -> str:
    with _lock:
        vocab = _fresh()
        n, total = vocab.debt_skip(words, undo=undo)
        verb = "un-marked" if undo else "marked as known"
        detail("%s: %d из %d, всего помечено %d%s",
               verb, n, len(words), total, _listing(" · ", words))
        return f"{verb}: {n} · {total} words marked in total"


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
        "candidates for the next topic.\n"
        "Also reports monotony — words the batch keeps reusing, counted by "
        "lemma so 'panin' and 'pani' are one word. Coverage cannot see this: "
        "ten sentences built on the same verb score a perfect 100%. Pass "
        "topic ('12. Poes') and the count includes the sentences already "
        "written there, which is the only way to catch a word you spread "
        "thin across several calls — always pass it when filling a topic."
    )
)
@traced
def text_check(
    sentences: list[str],
    targets: list[str] | None = None,
    min_coverage: int = 70,
    topic: str | None = None,
) -> str:
    with _lock:
        vocab = _fresh()
        goal = {t.strip().lower() for t in targets or []}
        results = [vocab.check_sentence(s, targets=goal) for s in sentences]
        repeats = vocab.repeats(
            [content for _p, _n, content in results], topic=topic, targets=goal
        )
        _log_text_check(
            sentences, results, goal, vocab, min_coverage, repeats, topic
        )
        return format_text_check(results, min_coverage, repeats, bool(topic))


def _log_text_check(
    sentences, results, goal, vocab: Vocab, min_coverage: int, repeats, topic
) -> None:
    """Сводка по вызову, каждое проваленное предложение и разбор слов.

    Процента мало: 'soojat' в отчёте выглядит нормальным новым словом, и
    только пара «форма → лемма» показывает, что это партитив от soe, у
    которого карточка есть. Единственный вердикт low за весь прошлый лог
    оказался именно таким промахом морфологии, и по логу это было не
    видно — пришлось перезапускать разбор руками.
    """
    failed = [
        i for i, (pct, new, _c) in enumerate(results, 1)
        if not sentence_ok(pct, new, min_coverage)
    ]
    fresh = {w for _p, new, _c in results for w in new}
    average = round(sum(pct for pct, _n, _c in results) / len(results)) if results else 0
    detail("%d предложений, средний охват %d%%, low %d, новых слов %d%s",
           len(results), average, len(failed), len(fresh),
           _listing(": ", sorted(fresh)))
    _log_monotony(repeats)
    # Что именно не передали, видно и по строке вызова, но там это
    # отсутствие аргумента, а здесь — потерянная проверка.
    if not goal:
        detail("⚠ targets не переданы: слова, которым учит тема, "
               "посчитаны новыми и попали в однообразие")
    if topic is None:
        detail("⚠ topic не передан: повторы против уже написанного "
               "в теме не считались")

    verbose = log.isEnabledFor(logging.DEBUG)
    for i, (text, (pct, new, _c)) in enumerate(zip(sentences, results), 1):
        if not new or not (verbose or i in failed):
            continue
        novel = vocab.novel(text, goal)
        pairs = ", ".join(f"{form}→{lemma}" for lemma, form in novel.unknown)
        half = ", ".join(f"{form}→{lemma}" for lemma, form in novel.half)
        detail("%s %2d %3d%%  новые: %s%s  | %s",
               "⚠ low" if i in failed else "     ", i, pct, pairs,
               f"  полузнакомые: {half}" if half else "", text,
               level=logging.INFO if i in failed else logging.DEBUG)


def _log_monotony(repeats) -> None:
    """Одна строка на оба места, где считают однообразие: text_check
    смотрит черновик, topic_write — то, что реально ляжет в колоду."""
    if not repeats:
        return
    detail("⚠ однообразие: %s", ", ".join(
        f"{r.lemma}×{r.in_batch}" + (f" (+{r.in_topic} в теме)" if r.in_topic else "")
        for r in repeats
    ))


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
            detail("%s — нет в коллекции", word)
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
    detail("%s → %s (%s) · формы %s · примеров %d",
           word, hit.lemma, hit.pos or "?", "/".join(hit.forms), len(examples))
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
        rows = _con.execute(
            "select deck, count(*) from entry where deck like ? group by 1 order by 1",
            (f"%{topic.strip()}%",),
        ).fetchall()
        detail("%s: %s", topic, " · ".join(
            f"{deck.split('::')[-1]}={n}" for deck, n in rows
        ) or "нет в индексе")
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


def _novel_words(vocab: Vocab):
    """Новые слова предложения колоды 03 — материал счётчика дозы.

    Слова темы приходят из write.py словарными формами: там про
    морфологию ничего не знают, поэтому в леммы их разворачиваем здесь.
    """
    goals: dict[frozenset, set[str]] = {}

    def find(sentence: str, taught: set[str]) -> tuple[list[str], list[str]]:
        key = frozenset(taught)
        goal = goals.get(key)
        if goal is None:
            goal = set(taught)
            for word in taught:
                goal |= vocab.lem.lemmas(word)
            goals[key] = goal
        novel = vocab.novel(sentence, goal)
        return (
            [lemma for lemma, _form in novel.unknown],
            [lemma for lemma, _form in novel.half],
        )

    return find


def _repeat_check(vocab: Vocab, topic: str):
    """Однообразие того, что реально уезжает в колоду.

    Тему передаём: без неё видно только текущий вызов, а panema набрался
    в 12. Poes именно тем, что расходился по вызовам двойками и тройками.
    """
    def find(sentences: list[str], taught: set[str]):
        goal = set(taught)
        for word in taught:
            goal |= vocab.lem.lemmas(word)
        batch = [vocab.check_sentence(s, targets=goal)[2] for s in sentences]
        found = vocab.repeats(batch, topic=topic, targets=goal)
        # Держим результат при себе: отчёт уходит модели, а в лог его
        # надо положить отдельно — иначе постфактум видно только то,
        # что модель решила написать в ответе.
        find.found = found
        return found

    find.found = []
    return find


@mcp.tool(
    description=(
        "Writes a topic into Anki: one word → 6 notes across three subdecks "
        "(5 for a verb, 3 for an invariant word) "
        "(01. sõnad, 02. sõnad typein, 03. lause typein). Form labels, tags, "
        "note order and deck creation are handled here — supply only the "
        "content. Anki must be running.\n"
        "words — a list of dicts: word (the Estonian word), pos "
        "('существительное'/'глагол'/'прилагательное'/'наречие'/"
        "'местоимение'/… — Russian, as tagged on the cards), ru (the Russian "
        "translation), forms (4 for a declinable word — номинатив, генитив, "
        "партитив, партитив мн. ч.; 3 for a verb — ma-/da-инфинитив, "
        "mina-vorm; one form is enough for an invariant word), ex (3 pairs "
        "['estonian example','russian'], example N shows form N; one pair is "
        "enough for an invariant word), sentence (pair ['new sentence', "
        "'russian'] for deck 03).\n"
        "The 4th form takes no example — ex stays at 3 pairs. "
        "Up to 25 words per call. Validation runs before the write: on bad "
        "data no note is created at all and every complaint comes back at "
        "once. dry_run=true shows what would be written, touching nothing.\n"
        "The report also counts the deliberate new words in the deck-03 "
        "sentences against the expected dose — nothing else can, since "
        "text_check gets a flat list and cannot tell which sentences are "
        "deck 03 — and flags the words the batch leans on too hard, "
        "counting the sentences already in the topic. Neither blocks the "
        "write; dry_run shows both beforehand."
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
        seek_repeats = _repeat_check(vocab, topic)
        try:
            report, dose = write_topic(
                course, topic, words,
                form_in_example=_form_in_example(vocab),
                novel_words=_novel_words(vocab),
                repeat_check=seek_repeats,
                dry_run=dry_run,
            )
        except NoteDataError as exc:
            detail("⚠ ничего не записано, ошибок в данных %d: %s",
                   len(exc.problems), " · ".join(exc.problems[:5]))
            return "nothing written, fix the data:\n" + "\n".join(
                f"  {p}" for p in exc.problems
            )
        except AnkiError as exc:
            detail("⚠ ничего не записано: %s", exc)
            return f"nothing written: {exc}"
        # Отчёт целиком, а не только его цифры: он и есть запись о том,
        # что уехало в колоду, и переживает чат только здесь.
        detail("%s%s\n%s", topic, " · dry run" if dry_run else "",
               textwrap.indent(report, " " * 8))
        # Дозу и однообразие дублируем отдельными строками поверх отчёта:
        # их ищут грепом по всем сессиям сразу.
        detail(
            "доза колоды 03: %d из %d ожидаемых%s — %s%s%s",
            len(dose.new), dose.expected,
            " ⚠ мало" if dose.short else "",
            ", ".join(dose.new) or "нет",
            f" · полузнакомых {len(dose.half)}: {', '.join(dose.half)}"
            if dose.half else "",
            f" · ⚠ два новых слова разом в {len(dose.crowded)} предл."
            if dose.crowded else "",
        )
        _log_monotony(seek_repeats.found)
        if not dry_run:
            # Свои заметки помечаем: иначе досборка отчитается о них как
            # о появившихся мимо сервера.
            ensure_fresh(_con, own_write=True)  # новые слова сразу видны vocab_check
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
    detail("'%s': колод %d%s", pattern, len(hits), _listing(" · ", hits, cap=3))
    if not hits:
        return f"no deck matches '{pattern}'"
    return "\n".join(hits[:40]) + (f"\n… +{len(hits) - 40} more" if len(hits) > 40 else "")


@mcp.tool(
    description=(
        "Catalogue of the Anki add-on's tools — everything this server does "
        "not do itself: deleting and editing notes, finding notes by query, "
        "decks, note types and templates, card scheduling (due, suspend, "
        "bury, flags, FSRS), media files, driving the Anki GUI, and syncing "
        "to AnkiWeb. Their schemas are not preloaded (they cost ~15k tokens), "
        "so look them up here first. No pattern: the list of names. With a "
        "pattern (a name or a keyword): the full schema of the matching "
        "tools, ready to pass to anki_call. Works with Anki closed from a "
        "cached list."
    )
)
@traced
def anki_tools(pattern: str = "") -> str:
    try:
        tools, source = catalog()
    except AnkiError as exc:
        detail("⚠ каталог недоступен: %s", exc)
        return f"{exc}"
    detail("каталог [%s]: инструментов %d, шаблон '%s'",
           source, len(tools), pattern.strip() or "—")
    if not pattern.strip():
        return f"[{source}] {len(tools)} add-on tools:\n{names(tools)}\n" \
               "anki_tools('<name or keyword>') for a schema"
    return describe(tools, pattern, source)


@mcp.tool(
    description=(
        "Runs one tool of the Anki add-on. Look its exact name and arguments "
        "up with anki_tools first — guessing them wastes a round trip. Anki "
        "must be running. For adding topic cards use topic_write instead: it "
        "is built for that and needs no lookup."
    )
)
@traced
def anki_call(tool: str, args: dict | None = None) -> str:
    try:
        tools, _ = catalog()
    except AnkiError as exc:
        detail("⚠ каталог недоступен: %s", exc)
        return f"{exc}"
    # Запись мимо topic_write — ровно тот случай, когда потом ничего не
    # сходится: доза не посчитана, формы не проверены, а заметки в колоде
    # уже лежат. В логе это должно быть видно сразу.
    if mutating(tool):
        detail("⚠ запись через аддон мимо topic_write: %s", _write_digest(tool, args))
    out = invoke(tools, tool, args)
    head = next((line for line in out.splitlines() if line.strip()), "пусто")
    detail("%s → %s", tool, _short(head, 160))
    return out


def _write_digest(tool: str, args: dict | None) -> str:
    args = args or {}
    parts = [tool]
    for key in ("deck", "deck_name", "deckName", "note_type", "query"):
        if value := args.get(key):
            parts.append(f"{key}={_short(value, 60)}")
    for key in ("notes", "note_ids", "notes_ids", "cards", "card_ids"):
        if isinstance(args.get(key), list):
            parts.append(f"{key}×{len(args[key])}")
    return " · ".join(parts)


def _code_version() -> str:
    """Коммит, который реально запущен.

    По логу иначе не отличить, какая версия его писала: старая сборка
    молчит ровно там, где новая пишет вердикт, и отсутствие строки
    читается как «проверка не сработала».
    """
    git = PROJECT_ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        try:
            sha = (git / ref).read_text().strip()
        except OSError:
            packed = (git / "packed-refs").read_text().splitlines()
            sha = next(
                line.split()[0] for line in packed if line.endswith(" " + ref)
            )
        return f"{ref.rsplit('/', 1)[-1]}@{sha[:7]}"
    except (OSError, StopIteration):
        return "?"


def main() -> None:
    log.info(
        "anki-vocab запущен · код %s · лог %s%s · коллекция %s · индекс %s · аддон %s",
        _code_version(), logging.getLevelName(log.level),
        f" + файл {LOG_FILE}" if LOG_FILE else " (только stderr)",
        COLLECTION, INDEX, endpoint(),
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
