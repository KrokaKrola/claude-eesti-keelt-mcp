"""MCP server: access to the vocabulary stored in Anki.

Every call first checks the index is fresh — stat() on the collection's
three files costs a fraction of a millisecond, and a card added ten
minutes ago via the add-on is already visible.
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

# Log only through our own logger, not root: otherwise at DEBUG level
# matplotlib, pulled in by estnltk, floods the log with noise.
# PID on every line: Claude Desktop starts the server as two processes,
# both write to one file, and their #N call numbers overlap.
_FORMAT = logging.Formatter(
    "%(asctime)s %(levelname)-5s [%(process)d] %(call)s%(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    defaults={"call": ""},
)

# The call number lives in the →, ← lines and everything logged between
# them — including index refresh from index.py, which uses the same logger.
# Without it, two consecutive calls blend into one mess.
_calls = itertools.count(1)
_call_no = contextvars.ContextVar("anki_vocab_call", default=0)
# What a tool wants to add to its journal entry beyond arguments and
# response (topic_write puts the workflow summary here).
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
    """A log file next to the index, in addition to stderr.

    The client keeps its copy of stderr for a couple of days and truncates
    it on restart, while the call stream sometimes needs reviewing a week
    later. An empty ANKI_VOCAB_LOG_FILE disables the file entirely.
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
        log.warning("log file unavailable (%s): %s", path, exc)
        return None
    handler.setFormatter(_FORMAT)
    log.addHandler(handler)
    return path


LOG_FILE = _open_log_file()


def detail(msg: str, *args, level: int = logging.INFO) -> None:
    """A verdict line inside the current call."""
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


# Cap on the DEBUG argument dump. topic_write with 33 words is 17k chars,
# and the session log would bloat more than anyone reads it. 12k covers a
# ~20-word topic in full; only the excess is cut.
ARGS_DUMP_MAX = 12000


def _dump(kwargs: dict) -> str:
    text = json.dumps(kwargs, ensure_ascii=False, indent=1)
    if len(text) <= ARGS_DUMP_MAX:
        return text
    return (
        text[:ARGS_DUMP_MAX]
        + f"\n… truncated, {len(text)} chars total (ARGS_DUMP_MAX={ARGS_DUMP_MAX})"
    )


def traced(fn):
    """Log calls to stderr — Claude Desktop collects it in
    ~/Library/Logs/Claude/mcp-server-anki-vocab.log. ANKI_VOCAB_LOG=DEBUG
    adds full tool arguments and responses."""

    @functools.wraps(fn)
    def wrapper(**kwargs):
        # The number is reset in finally: otherwise everything logged between
        # calls is attributed to the previous one.
        token = _call_no.set(next(_calls))
        extra_token = _journal_extra.set({})
        try:
            args = ", ".join(
                f"{k}={_short(v)}" for k, v in kwargs.items() if v is not None
            )
            log.info("→ %s(%s)", fn.__name__, args)
            # Full arguments on a separate line. At INFO they're cut to 90
            # chars, and the log couldn't tell which sentences were checked:
            # the verdict is there, the text isn't.
            if kwargs and log.isEnabledFor(logging.DEBUG):
                log.debug("  arguments %s:\n%s", fn.__name__, _dump(kwargs))
            started = time.perf_counter()
            try:
                out = fn(**kwargs)
            except Exception as exc:
                log.exception("✗ %s failed", fn.__name__)
                journal.record(
                    fn.__name__, _call_no.get(), kwargs, None,
                    (time.perf_counter() - started) * 1000,
                    error=f"{type(exc).__name__}: {exc}",
                    extra=_journal_extra.get(),
                )
                raise
            ms = (time.perf_counter() - started) * 1000
            log.info("← %s: %d chars ≈%d tokens, %.0f ms",
                     fn.__name__, len(out), len(out) // 3, ms)
            log.debug("  response %s:\n%s", fn.__name__, out)
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

# The index is opened lazily: a side effect on import gets in the way of
# tests and costs a connection even when no tool is called.
_con: sqlite3.Connection | None = None
_vocab: Vocab | None = None
_lock = threading.Lock()


def _fresh() -> Vocab:
    global _con, _vocab
    if _con is None:
        _con = open_index(same_thread=False)
        _vocab = Vocab(_con)
        detail(
            "index: %s notes, %s lemmas, palette %s · %s",
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
        _vocab.lem = Lemmatizer(_con)  # reload the lemma cache after refresh
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
        # Not just counts but a per-word breakdown: the same call is used to
        # pick new words for the deck-03 dose (✗) and to filter duplicates
        # (✓), and afterwards you need to see what went where.
        counts = Counter(h.status for h in hits)
        picked = lambda status: [h.query for h in hits if h.status == status]
        detail(
            "✓%d ~%d ✗%d of %d%s%s%s",
            counts[KNOWN], counts[SEEN], counts[UNKNOWN], len(hits),
            _listing(" · ✗ new: ", picked(UNKNOWN)),
            _listing(" · ~ only in examples: ", picked(SEEN)),
            _listing(" · ✓ already there: ", picked(KNOWN)),
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
        # What sentences were allowed to be built from at all. Without this
        # line, monotony in a finished topic can't be told from a poor palette.
        detail(
            "palette: topic %d, core %d, forgotten %d, stale %d ·"
            " level=%s topics=%s pos=%s",
            len(palette.topical), len(palette.core),
            len(palette.weak), len(palette.stale),
            level or "—", ",".join(topics or []) or "—", pos or "—",
        )
        if limit:
            detail("⚠ limit=%d passed — words hidden: %d", limit, palette.dropped)
        detail("forgotten: %s", " · ".join(palette.weak) or "none", level=logging.DEBUG)
        detail("stale: %s", " · ".join(palette.stale) or "none",
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
        detail("debt: %d words, offset %d%s",
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
        detail("%s: %d of %d, %d marked in total%s",
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
            # A typo in the topic name — and the monotony check against what's
            # already written silently counted zero.
            detail("⚠ topic '%s' not in index — repeats counted within the batch only", topic)
            out += (
                f"\ntopic '{topic}' is not in the index — monotony was counted "
                "within this batch only (a new topic, or check the spelling)"
            )
        return out


def _log_text_check(
    sentences, results, goal, vocab: Vocab, min_coverage: int, repeats, topic
) -> None:
    """Call summary, every failed sentence and a word breakdown.

    The percentage isn't enough: 'soojat' looks like a normal new word in
    the report, and only the "form → lemma" pair shows it's the partitive
    of soe, which has a card. The only low verdict in the whole previous
    log turned out to be exactly such a morphology miss, and the log didn't
    show it — the analysis had to be rerun by hand.
    """
    failed = [
        i for i, r in enumerate(results, 1)
        if not sentence_ok(r.pct, r.new, min_coverage)
    ]
    fresh = {w for r in results for w in r.new}
    typos = {w for r in results for w in r.typos}
    average = round(sum(r.pct for r in results) / len(results)) if results else 0
    detail("%d sentences, mean coverage %d%%, low %d, new words %d%s%s",
           len(results), average, len(failed), len(fresh),
           _listing(": ", sorted(fresh)),
           _listing(" · ⚠ typos?: ", sorted(typos)))
    _log_monotony(repeats)
    # What was omitted shows in the call line too, but there it's a missing
    # argument, while here it's a lost check.
    if not goal:
        detail("⚠ targets not passed: the words the topic teaches were "
               "counted as new and ended up in the monotony check")
    if topic is None:
        detail("⚠ topic not passed: repeats against what the topic already "
               "has were not counted")

    verbose = log.isEnabledFor(logging.DEBUG)
    for i, (text, (pct, new, _c, _t)) in enumerate(zip(sentences, results), 1):
        if not new or not (verbose or i in failed):
            continue
        novel = vocab.novel(text, goal)
        pairs = ", ".join(f"{form}→{lemma}" for lemma, form in novel.unknown)
        half = ", ".join(f"{form}→{lemma}" for lemma, form in novel.half)
        detail("%s %2d %3d%%  new: %s%s  | %s",
               "⚠ low" if i in failed else "     ", i, pct, pairs,
               f"  half-known: {half}" if half else "", text,
               level=logging.INFO if i in failed else logging.DEBUG)


def _log_monotony(repeats) -> None:
    """One line for both places that check monotony: text_check looks at
    the draft, topic_write at what actually goes into the deck."""
    if not repeats:
        return
    detail("⚠ monotony: %s", ", ".join(
        f"{r.lemma}×{r.in_batch}" + (f" (+{r.in_topic} in topic)" if r.in_topic else "")
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
            detail("%s — not in the collection", word)
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
    detail("%s → %s (%s) · forms %s · examples %d",
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
        ) or "not in index")
        return _topic_state(_con, topic)


def _form_in_example(vocab: Vocab):
    """The "example contains the form" check by lemma, not by letters:
    the verb algama legitimately has an example with algab, and a string
    comparison would complain on every other card."""

    def check(form: str, sentence: str) -> bool:
        if form.strip().lower() in sentence.lower():
            return True
        # A phrasal verb is several words, and each must be found: previously
        # 'saan teada' was parsed as one word and never matched.
        sentence_lemmas = [vocab.lem.lemmas(w) for w in split_words(sentence)]
        for part in split_words(form):
            target = vocab.lem.lemmas(part)
            if target and not any(target & found for found in sentence_lemmas):
                return False
        return True

    return check


def _novel_words(vocab: Vocab):
    """New words in deck-03 sentences — input for the dose counter.

    Topic words come from write.py in dictionary form: it knows nothing
    about morphology, so they're expanded into lemmas here.
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
    """Monotony of what actually goes into the deck.

    The topic is passed: without it only the current call is visible, and
    panema accumulated in 12. Poes precisely by spreading over calls in
    twos and threes.
    """
    def find(sentences: list[str], taught: set[str]):
        goal = vocab.goal(taught)
        batch = [vocab.check_sentence(s, targets=goal).content for s in sentences]
        found = vocab.repeats(batch, topic=topic, targets=goal)
        # Keep the result: the report goes to the model, and the log needs
        # it separately — otherwise afterwards you only see what the model
        # chose to write in its reply.
        find.found = found
        return found

    find.found = []
    return find


def _drafted_sentences(words: list) -> list[str]:
    """All sentences a topic_write call carries into the deck."""
    out = []
    for w in words:
        if not isinstance(w, dict):
            continue
        for pair in (w.get("ex") or []) + [w.get("sentence")]:
            if isinstance(pair, (list, tuple)) and pair:
                out.append(str(pair[0]))
    return out


def _dictionary_check(vocab: Vocab):
    """Word forms against the EKI dictionary — before writing, rejecting on mismatch.

    Doesn't depend on whether the model called word_paradigm: 'kudub' for
    koob and 'kesküte' for keskküte would get through here too if only the
    model checked them. Dictionary unavailable — write, but say the forms
    weren't checked.
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
    """Words not in the dictionary — across everything that goes into the deck.

    'Laps söi' (for sõi) and 'mustad mureled' (for murelid) were written
    as is: text_check showed them as new words, and that line can't tell
    a typo from a new word.
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
        "translation), forms (4 for a declinable word — nominative, genitive, "
        "partitive, partitive plural — or just 3 of them when the partitive "
        "plural is not used in practice (mass noun: vesi, jahu, mesi, "
        "sool, või): a made-up 4th form is worse than none; 3 for a verb — "
        "ma-/da-infinitive, mina-vorm; one form is enough for an invariant "
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
            # Step summary before writing: it later shows where the model cut
            # corners, even if the write then failed.
            steps = _flow.before_write(
                topic,
                [w.get("word", "") for w in words if isinstance(w, dict)],
                _drafted_sentences(words),
            )
            detail("workflow: %s", flow_summary(steps))
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
            detail("⚠ nothing written, data errors %d: %s",
                   len(exc.problems), " · ".join(exc.problems))
            return "nothing written, fix the data:\n" + "\n".join(
                f"  {p}" for p in exc.problems
            )
        except AnkiError as exc:
            detail("⚠ nothing written: %s", exc)
            return f"nothing written: {exc}"
        # The full report, not just its numbers: it is the record of what
        # went into the deck, and only here does it outlive the chat.
        detail("%s%s\n%s", topic, " · dry run" if dry_run else "",
               textwrap.indent(report, " " * 8))
        # Dose and monotony are duplicated as separate lines over the report:
        # they're grepped across all sessions at once.
        detail(
            "deck-03 dose: %d of %d expected%s%s — %s%s%s%s",
            len(dose.new), dose.expected,
            " ⚠ low" if dose.short else "",
            " ⚠ too many" if dose.over else "",
            ", ".join(dose.new) or "none",
            f" · earlier in this topic fill: words {dose.prior_words}, new "
            f"{len(dose.prior_new)}" if dose.prior_words else "",
            f" · half-known {len(dose.half)}: {', '.join(dose.half)}"
            f" · ⚠ two new words at once in {len(dose.crowded)} sent."
            if dose.crowded else "",
        )
        _log_monotony(seek_repeats.found)
        if seek_forms.unchecked:
            detail("⚠ forms not checked against the dictionary: %s", seek_forms.unchecked)
        else:
            detail("forms checked against the EKI dictionary: words %d", seek_forms.checked)
        if seek_typos.found:
            detail("⚠ typos?: %s", ", ".join(w for w, _ in seek_typos.found))
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
            # Mark our own notes: otherwise refresh reports them as
            # appearing bypassing the server.
            ensure_fresh(_con, own_write=True)  # new words visible to vocab_check at once
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
            # A word in text may be an inflected form: the dictionary searches
            # by headword only, so misses are retried by lemma.
            lemmas = {
                w: sorted(vocab.lem.lemmas(w) - {w})
                for w, paradigms in found.items() if not paradigms
            }
            by_lemma = dictionary.lookup_many([l for ls in lemmas.values() for l in ls])
        except ekilex.EkilexError as exc:
            detail("⚠ dictionary unavailable: %s", exc)
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
    detail("dictionary: %d words, not found %d%s · from network %d, from cache %d",
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
        # püksid: legitimately written with plural forms on the card
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
    detail("'%s': decks %d%s", pattern, len(hits), _listing(" · ", hits, cap=3))
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
        detail("⚠ catalogue unavailable: %s", exc)
        return f"{exc}"
    detail("catalogue [%s]: tools %d, pattern '%s'",
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
        detail("⚠ catalogue unavailable: %s", exc)
        return f"{exc}"
    # A write bypassing topic_write is exactly the case where nothing adds up
    # later: the dose isn't counted, forms aren't checked, and the notes are
    # already in the deck. The log must show it right away.
    if mutating(tool, args):
        detail("⚠ write via add-on bypassing topic_write: %s", _write_digest(tool, args))
    out = invoke(tools, tool, args)
    head = next((line for line in out.splitlines() if line.strip()), "empty")
    detail("%s → %s", tool, _short(head, 160))
    return out


def _write_digest(tool: str, args: dict | None) -> str:
    args = args or {}
    # Multi-purpose tools hide the action and ids inside params.
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
    """The commit actually running.

    Otherwise the log can't tell which version wrote it: an old build is
    silent exactly where a new one logs a verdict, and a missing line reads
    as "the check didn't fire".
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
    """'+dirty' if the running code differs from the commit.

    Without it, on 20.09 the log labeled as main@486f558 a process that
    accepted three forms for vesi — while that commit rejected them: edits
    not yet in history were running. Git unavailable — honestly mark '+?'.
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
        "anki-vocab started · code %s · log %s%s · journal %s · collection %s · index %s"
        " · add-on %s · Ekilex key %s",
        _code_version(), logging.getLevelName(log.level),
        f" + file {LOG_FILE}" if LOG_FILE else " (stderr only)",
        journal.PATH or "off",
        COLLECTION, INDEX, endpoint(), "set" if ekilex.api_key() else "MISSING",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
