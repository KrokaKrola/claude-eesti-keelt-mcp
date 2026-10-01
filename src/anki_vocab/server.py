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
import subprocess
import sys
import sqlite3
import textwrap
import threading
import time
from collections import Counter
from logging.handlers import RotatingFileHandler
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import ekilex, journal
from .flow import Flow, summary as flow_summary
from .collection import deck_names, snapshot
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
    topic_decks,
    topic_state as _topic_state,
)
from .passthrough import catalog, describe, invoke, mutating, names
from .write import AnkiError, NoteDataError, endpoint, form_labels, write_topic

# Пишем только своим логгером и не через root: иначе на уровне DEBUG
# matplotlib, который тянет за собой estnltk, заливает лог своим шумом.
# PID в каждой строке: Claude Desktop поднимает сервер двумя процессами,
# оба пишут в один файл, и номера вызовов #N у них пересекаются.
_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)-5s [%(process)d] %(call)s%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    defaults={"call": ""},
)

# Номер вызова живёт в строках →, ← и во всём, что пишется между ними, —
# включая досборку индекса из index.py: тот пишет в этот же логгер.
# Без номера разбор двух вызовов подряд склеивается в одну кашу.
_calls = itertools.count(1)
_call_no = contextvars.ContextVar("anki_vocab_call", default=0)
# Что инструмент хочет добавить в свою запись журнала сверх аргументов
# и ответа (topic_write кладёт сюда сводку сценария).
_journal_extra: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "anki_vocab_journal_extra", default=None
)
_flow = Flow()


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
        extra_token = _journal_extra.set({})
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
            except Exception as exc:
                log.exception("✗ %s упал", fn.__name__)
                journal.record(
                    fn.__name__, _call_no.get(), kwargs, None,
                    (time.perf_counter() - started) * 1000,
                    error=f"{type(exc).__name__}: {exc}",
                    extra=_journal_extra.get(),
                )
                raise
            ms = (time.perf_counter() - started) * 1000
            log.info("← %s: %d симв. ≈%d токенов, %.0f мс",
                     fn.__name__, len(out), len(out) // 3, ms)
            log.debug("  ответ %s:\n%s", fn.__name__, out)
            journal.record(
                fn.__name__, _call_no.get(), kwargs, out, ms,
                extra=_journal_extra.get(),
            )
            return out
        finally:
            _journal_extra.reset(extra_token)
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
        "only ever appeared inside example sentences (comes with how often "
        "and one sentence where it was met), ✗ — not there at all. "
        "Use it before creating cards so duplicates don't pile up. A '~' "
        "word is not a duplicate: the learner decides whether it gets a card."
    )
)
@traced
def vocab_check(words: list[str]) -> str:
    with _lock:
        vocab = _fresh()
        hits = vocab.check(words)
        marks = {}
        for h in hits:
            mark = {KNOWN: "✓", SEEN: "~", UNKNOWN: "✗"}[h.status]
            marks[h.query.strip().lower()] = mark
            if h.lemma:
                marks.setdefault(h.lemma, mark)
        _flow.note("vocab_check", marks=marks)
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
        _flow.note("vocab_pool", topics=topics or [])
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
        goal = vocab.goal(targets)
        results = [vocab.check_sentence(s, targets=goal) for s in sentences]
        _flow.note("text_check", topic=topic, sentences=[s.strip() for s in sentences])
        known_topic = bool(topic) and bool(topic_decks(vocab.con, topic))
        repeats = vocab.repeats(
            [r.content for r in results],
            topic=topic if known_topic else None,
            targets=goal,
        )
        _log_text_check(
            sentences, results, goal, vocab, min_coverage, repeats, topic
        )
        out = format_text_check(results, min_coverage, repeats, known_topic)
        if topic and not known_topic:
            # Опечатка в имени темы — и проверка однообразия против уже
            # написанного молча считала ноль.
            detail("⚠ темы '%s' нет в индексе — повторы считались только в пачке", topic)
            out += (
                f"\ntopic '{topic}' is not in the index — monotony was counted "
                "within this batch only (a new topic, or check the spelling)"
            )
        return out


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
        i for i, r in enumerate(results, 1)
        if not sentence_ok(r.pct, r.new, min_coverage)
    ]
    fresh = {w for r in results for w in r.new}
    typos = {w for r in results for w in r.typos}
    average = round(sum(r.pct for r in results) / len(results)) if results else 0
    detail("%d предложений, средний охват %d%%, low %d, новых слов %d%s%s",
           len(results), average, len(failed), len(fresh),
           _listing(": ", sorted(fresh)),
           _listing(" · ⚠ опечатки?: ", sorted(typos)))
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
    for i, (text, (pct, new, _c, _t)) in enumerate(zip(sentences, results), 1):
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
        _flow.note("topic_state", topic=topic)
        decks = topic_decks(_con, topic)
        rows = _con.execute(
            f"select deck, count(*) from entry where deck in ({','.join('?' * len(decks))})"
            " group by 1 order by 1",
            decks,
        ).fetchall() if decks else []
        detail("%s: %s", topic, " · ".join(
            f"{deck.split('::')[-1]}={n}" for deck, n in rows
        ) or "нет в индексе")
        return _topic_state(_con, topic)


def _form_in_example(vocab: Vocab):
    """Проверка «пример содержит форму» по леммам, а не по буквам:
    у глагола algama пример законно идёт с algab, и строковое сравнение
    ругалось бы на каждой второй карточке."""

    def check(form: str, sentence: str) -> bool:
        if form.strip().lower() in sentence.lower():
            return True
        # Фразовый глагол — несколько слов, и каждое должно найтись: раньше
        # 'saan teada' разбиралось как одно слово и не находилось никогда.
        sentence_lemmas = [vocab.lem.lemmas(w) for w in split_words(sentence)]
        for part in split_words(form):
            target = vocab.lem.lemmas(part)
            if target and not any(target & found for found in sentence_lemmas):
                return False
        return True

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
            goal = goals[key] = vocab.goal(taught)
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
        goal = vocab.goal(taught)
        batch = [vocab.check_sentence(s, targets=goal).content for s in sentences]
        found = vocab.repeats(batch, topic=topic, targets=goal)
        # Держим результат при себе: отчёт уходит модели, а в лог его
        # надо положить отдельно — иначе постфактум видно только то,
        # что модель решила написать в ответе.
        find.found = found
        return found

    find.found = []
    return find


def _drafted_sentences(words: list) -> list[str]:
    """Все предложения, которые вызов topic_write несёт в колоду."""
    out = []
    for w in words:
        if not isinstance(w, dict):
            continue
        for pair in (w.get("ex") or []) + [w.get("sentence")]:
            if isinstance(pair, (list, tuple)) and pair:
                out.append(str(pair[0]))
    return out


def _dictionary_check(vocab: Vocab):
    """Формы слов против словаря EKI — до записи, с отказом при расхождении.

    Не зависит от того, звала ли модель word_paradigm: 'kudub' вместо
    koob и 'kesküte' вместо keskküte прошли бы и сюда, проверяй их только
    модель. Словарь недоступен — пишем, но говорим, что формы не проверены.
    """
    def check(words: list[dict]) -> tuple[list[str], list[str]]:
        lexical = [w for w in words if len({f.lower() for f in w["forms"]}) > 1]
        if not lexical:
            return [], []
        try:
            found = ekilex.Dictionary(vocab.con).lookup_many([w["word"] for w in lexical])
        except ekilex.EkilexError as exc:
            check.unchecked = str(exc)
            return [], [f"forms NOT checked against the dictionary: {exc}"]
        problems: list[str] = []
        warnings: list[str] = []
        for w in lexical:
            verb = w["pos"] == "глагол"
            wrong, unsure = ekilex.check_forms(
                w["word"], verb, w["forms"], found[w["word"].strip().lower()],
                form_labels(w["pos"], w["forms"]) or (),
            )
            problems += wrong
            warnings += unsure
        check.checked = len(lexical)
        return problems, warnings

    check.checked = 0
    check.unchecked = None
    return check


def _spell_check(vocab: Vocab):
    """Слова, которых нет в словаре, — по всему, что уезжает в колоду.

    'Laps söi' (вместо sõi) и 'mustad mureled' (вместо murelid) так и
    записались: text_check показал их новыми словами, а опечатку от
    нового слова по той строке не отличить.
    """
    def find(sentences: list[str], taught: set[str]) -> list[tuple[str, str]]:
        goal = vocab.goal(taught)
        find.found = [
            (word, text)
            for text in sentences
            for word in vocab.check_sentence(text, targets=goal).typos
        ]
        return find.found

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
        "партитив, партитив мн. ч. — or just 3 of them when the partitive "
        "plural is not used in practice (вещественное: vesi, jahu, mesi, "
        "sool, või): a made-up 4th form is worse than none; 3 for a verb — "
        "ma-/da-инфинитив, mina-vorm; one form is enough for an invariant "
        "word), ex (3 pairs "
        "['estonian example','russian'], example N shows form N; one pair is "
        "enough for an invariant word), sentence (pair ['new sentence', "
        "'russian'] for deck 03).\n"
        "The 4th form takes no example — ex stays at 3 pairs. "
        "Forms are checked against the EKI dictionary before writing: a form "
        "the dictionary does not have rejects the whole call and the reply "
        "shows the right one — take the forms from word_paradigm first. "
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
        seek_typos = _spell_check(vocab)
        seek_forms = _dictionary_check(vocab)
        dose_key = f"{course}::{topic}".strip().lower()
        if dry_run:
            _flow.note("topic_write_dry", topic=topic)
        else:
            # Сводка шагов — до записи: по ней потом видно, на чём модель
            # срезала путь, даже если запись затем упала.
            steps = _flow.before_write(
                topic,
                [w.get("word", "") for w in words if isinstance(w, dict)],
                _drafted_sentences(words),
            )
            detail("сценарий: %s", flow_summary(steps))
            _journal_extra.get()["flow"] = steps
        try:
            report, dose = write_topic(
                course, topic, words,
                form_in_example=_form_in_example(vocab),
                novel_words=_novel_words(vocab),
                repeat_check=seek_repeats,
                spell_check=seek_typos,
                form_check=seek_forms,
                dose_history=vocab.dose_history(dose_key),
                dry_run=dry_run,
            )
        except NoteDataError as exc:
            detail("⚠ ничего не записано, ошибок в данных %d: %s",
                   len(exc.problems), " · ".join(exc.problems))
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
            "доза колоды 03: %d из %d ожидаемых%s%s — %s%s%s%s",
            len(dose.new), dose.expected,
            " ⚠ мало" if dose.short else "",
            " ⚠ перебор" if dose.over else "",
            ", ".join(dose.new) or "нет",
            f" · ранее в заполнении темы: слов {dose.prior_words}, новых "
            f"{len(dose.prior_new)}" if dose.prior_words else "",
            f" · полузнакомых {len(dose.half)}: {', '.join(dose.half)}"
            if dose.half else "",
            f" · ⚠ два новых слова разом в {len(dose.crowded)} предл."
            if dose.crowded else "",
        )
        _log_monotony(seek_repeats.found)
        if seek_forms.unchecked:
            detail("⚠ формы не сверены со словарём: %s", seek_forms.unchecked)
        else:
            detail("формы сверены со словарём EKI: слов %d", seek_forms.checked)
        if seek_typos.found:
            detail("⚠ опечатки?: %s", ", ".join(w for w, _ in seek_typos.found))
        _journal_extra.get().update({
            "dose": {
                "new": dose.new, "half": dose.half, "expected": dose.expected,
                "prior_words": dose.prior_words, "prior_new": dose.prior_new,
                "short": dose.short, "over": dose.over,
            },
            "typos": seek_typos.found,
            "forms_unchecked": seek_forms.unchecked,
        })
        if not dry_run:
            vocab.record_dose(dose_key, dose.words, dose.new)
            # Свои заметки помечаем: иначе досборка отчитается о них как
            # о появившихся мимо сервера.
            ensure_fresh(_con, own_write=True)  # новые слова сразу видны vocab_check
    return report


@mcp.tool(
    description=(
        "The canonical card forms of words straight from the EKI dictionary "
        "(Ekilex — the base behind Sõnaveeb): nominative / genitive / "
        "partitive / partitive plural for a declinable word, ma- / "
        "da-infinitive / mina-vorm for a verb. Call it once for the whole "
        "list before composing cards and take the forms from it — do not "
        "derive them yourself. Where the dictionary gives variants they are "
        "joined with '~': take the first. 'no partitive plural' means send "
        "3 forms. Inflected input (Erinevaid) is resolved to its lemma. "
        "topic_write checks the forms against the same dictionary."
    )
)
@traced
def word_paradigm(words: list[str]) -> str:
    with _lock:
        vocab = _fresh()
        dictionary = ekilex.Dictionary(vocab.con)
        try:
            found = dictionary.lookup_many(words)
            # Слово в тексте бывает словоформой: словарь ищет только по
            # заголовку, поэтому ненайденное пробуем ещё раз леммой.
            lemmas = {
                w: sorted(vocab.lem.lemmas(w) - {w})
                for w, paradigms in found.items() if not paradigms
            }
            by_lemma = dictionary.lookup_many([l for ls in lemmas.values() for l in ls])
        except ekilex.EkilexError as exc:
            detail("⚠ словарь недоступен: %s", exc)
            return f"dictionary unavailable: {exc}"
    lines = []
    missing = []
    for word, paradigms in found.items():
        shown = word
        if not paradigms:
            hits = [(l, by_lemma[l]) for l in lemmas.get(word, []) if by_lemma.get(l)]
            if not hits:
                missing.append(word)
                lines.append(f"{word} — not in the dictionary: check the spelling")
                continue
            lemma, paradigms = hits[0]
            shown = f"{word} → {lemma}"
        lines += _paradigm_lines(shown, paradigms)
    _flow.note("word_paradigm", words=list(found) + [
        l for ls in lemmas.values() for l in ls if by_lemma.get(l)
    ])
    detail("словарь: %d слов, не найдено %d%s · из сети %d, из кеша %d",
           len(found), len(missing), _listing(": ", missing),
           dictionary.fetched, dictionary.cached)
    return "\n".join(lines)


def _paradigm_lines(shown: str, paradigms: list[ekilex.Paradigm]) -> list[str]:
    if ekilex.is_invariant(paradigms):
        return [f"{shown} — invariant: send 1 form"]
    classes = {p.word_class for p in paradigms}
    kinds = ([False] if classes - {"verb"} else []) + ([True] if "verb" in classes else [])
    out = []
    for verb in kinds:
        forms = ekilex.canonical(paradigms, verb)
        if not verb and not forms[3]:
            text = " / ".join(" ~ ".join(v) for v in forms[:3])
            tail = " — no partitive plural: send 3 forms"
        else:
            text = " / ".join(" ~ ".join(v) or "—" for v in forms)
            tail = ""
        out.append(f"{shown} — {'verb' if verb else 'noun/adj'}: {text}{tail}")
    word = shown.split(" → ")[-1]
    plural = [p.slot("PlN") for p in paradigms]
    if any(word in forms for forms in plural) and not any(word in p.slot("SgN") for p in paradigms):
        # püksid: на карточке законно пишут формами мн. ч.
        shown_plural = " / ".join(
            " ~ ".join(paradigms[0].slot(code)) or "—" for code in ("PlN", "PlG", "PlP")
        )
        out.append(f"  used in the plural: {shown_plural} — also accepted on the card")
    return out


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
    with snapshot() as con:
        hits = sorted(n for n in deck_names(con).values() if needle in n.lower())
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
    if mutating(tool, args):
        detail("⚠ запись через аддон мимо topic_write: %s", _write_digest(tool, args))
    out = invoke(tools, tool, args)
    head = next((line for line in out.splitlines() if line.strip()), "пусто")
    detail("%s → %s", tool, _short(head, 160))
    return out


def _write_digest(tool: str, args: dict | None) -> str:
    args = args or {}
    # Многоцелевые инструменты прячут действие и id внутри params.
    if isinstance(args.get("params"), dict):
        args = {**args, **args["params"]}
    parts = [tool]
    for key in ("action", "deck", "deck_name", "deckName", "note_type", "query", "tags"):
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
        return f"{ref.rsplit('/', 1)[-1]}@{sha[:7]}{_dirty()}"
    except (OSError, StopIteration):
        return "?"


def _dirty() -> str:
    """'+dirty', если запущенный код расходится с коммитом.

    Без этого 20.09 лог подписал main@486f558 процесс, который принимал
    три формы у vesi — а этот коммит их отклонял: работали правки, ещё
    не попавшие в историю. Git недоступен — честно ставим '+?'.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain", "--", "src"],
            capture_output=True, text=True, timeout=3, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "+?"
    return "+dirty" if out.strip() else ""


def main() -> None:
    log.info(
        "anki-vocab запущен · код %s · лог %s%s · журнал %s · коллекция %s · индекс %s"
        " · аддон %s · ключ Ekilex %s",
        _code_version(), logging.getLevelName(log.level),
        f" + файл {LOG_FILE}" if LOG_FILE else " (только stderr)",
        journal.PATH or "выключен",
        COLLECTION, INDEX, endpoint(), "есть" if ekilex.api_key() else "НЕТ",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
