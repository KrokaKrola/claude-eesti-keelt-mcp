"""Tool logic: word checks, palette, text coverage.

Two vocabulary tiers. The palette is the first fields of dictionary cards
(deck 01. sõnad); Claude composes from it. The second tier is everything
else the learner has seen: other forms from cards, words from examples
and drills. We don't suggest composing from them, but count them as known,
otherwise the check would complain about words from the learner's own cards.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import NamedTuple

from .config import (
    DOSE_WINDOW_HOURS,
    FUNCTION_WORDS,
    HALF_KNOWN_MAX,
    REPEAT_IN_TOPIC,
    REPEAT_MIN,
    REPEAT_SHOWN,
    REPEAT_TOP,
    STRUCTURAL_VERBS,
)
from .lemma import Lemmatizer
from .parse import words

KNOWN, SEEN, UNKNOWN = "known", "seen", "unknown"

# Note types of a dictionary card: three word forms in one headword field.
WORD_CARD_NOTETYPES = ("Estonian 3 Forms", "Estonian Word Forms")


def _tag_words(tags: list[str] | set[str]) -> set[str]:
    """A topic tag and the words in it.

    The naming convention changed over time: old topics are tagged with
    one underscored tag ('tule_mulle_külla', 'kodused_tööd'), new ones
    with words ('elu', 'liikumine', 'on'). The skill derives tags from the
    deck name by the second rule, so old topics never matched and the
    "by topic" group silently came back empty. Comparing by word makes
    both schemes agree.
    """
    out = set()
    for tag in tags:
        low = tag.strip().lower()
        if low:
            out.add(low)
            out.update(part for part in low.split("_") if part)
    return out
MARK = {KNOWN: "✓", SEEN: "~", UNKNOWN: "✗"}

# Where a word in a topic came from. The note slot is more honest than the
# deck: old topics use other formats, while Form1/2/3 is always a three-forms card.
SLOT_MARK = {
    # forms card; Form4 (partitive plural) was added later than the others
    "Form1": "3F", "Form2": "3F", "Form3": "3F", "Form4": "3F",
    "Answer": "ty",                               # typein, one form
    "example": "ex",                              # only inside an example
}
MARK_ORDER = ["3F", "ty", "other", "ex"]

# Debt page cap. The project's point is saving context, and the tool exposes
# limit: 400 words is 8k tokens in a single response.
DEBT_PAGE_MAX = 50


@dataclass
class Palette:
    """vocab_pool output. Five fields already, and a positional tuple at
    the call site read as (a, b, _, _)."""

    topical: list[str]
    core: list[str]
    dropped: int = 0
    stale: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


@dataclass
class Repeat:
    """A word the examples lean on too often."""

    lemma: str
    in_batch: int          # in how many sentences of this batch
    in_topic: int = 0      # plus how many already written to the topic
    own: bool = False      # the topic's own word: it has its own card here
    # The batch is built on this word right here and now — rewrite it.
    # Otherwise the word is just long overused in the topic: a suggestion.
    crowded: bool = False


@dataclass
class Novel:
    """New words of one sentence, as (lemma, form in text) pairs."""

    unknown: list[tuple[str, str]] = field(default_factory=list)
    half: list[tuple[str, str]] = field(default_factory=list)


class Token(NamedTuple):
    """A content word of a sentence after parsing."""

    form: str
    status: str
    lemma: str
    times: int
    # Not in Vabamorf's dictionary: more likely a typo than a new word.
    typo: bool = False


class Check(NamedTuple):
    """Verdict for one sentence."""

    pct: int
    new: list[str]        # unknown lemmas, typos excluded
    content: list[str]    # all content lemmas — for monotony
    typos: list[str]      # forms not in the dictionary


@dataclass
class Debt:
    """A word the learner reads in their own examples but never made
    a card for."""

    word: str
    times: int
    example: str | None = None


@dataclass
class DebtPage:
    """A debt page together with the offset to print it from.

    The offset is returned rather than taken from the caller: bounds are
    clamped inside, and with a raw offset=-5 numbering would drift to "#-4".
    """

    items: list[Debt]
    offset: int = 0
    more: bool = False
    skipped: int = 0


@dataclass
class Hit:
    query: str
    status: str
    lemma: str | None = None
    ru: str | None = None
    level: str | None = None
    pos: str | None = None
    topics: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    decks: tuple[str, ...] = ()
    times: int = 0
    # Only for '~': where the learner saw the word and whether they said they know it.
    example: str | None = None
    marked_known: bool = False


class Vocab:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.lem = Lemmatizer(con)

    # --- lookup -----------------------------------------------------------
    def _row(self, lemma: str) -> Hit | None:
        row = self.con.execute(
            "select lemma, ru, level, pos, topics, forms, n_word_notes, n_notes"
            " from lemma where lemma = ?",
            (lemma,),
        ).fetchone()
        if not row:
            return None
        lem, ru, level, pos, topics, forms, n_word, n_notes = row
        decks = self._where(lem)
        return Hit(
            query=lemma,
            status=KNOWN if n_word else SEEN,
            lemma=lem, ru=ru, level=level, pos=pos,
            topics=tuple(json.loads(topics)), forms=tuple(json.loads(forms)),
            decks=tuple(dict.fromkeys(decks)), times=n_notes,
        )

    def _where(self, lemma: str) -> list[str]:
        """Topic plus marks for what kind of entry lives there.

        The topic alone isn't enough: "word already in 09. Vaba aeg" may mean
        a full three-forms card or an incidental hit in someone else's
        example. Those are different decisions, and the topic name can't
        tell them apart.
        """
        # Keep the strongest mark per topic, not all: a word with a three-forms
        # card almost always has typein and example hits too — listing them
        # means paying tokens for what's already clear.
        best: dict[str, int] = {}
        for deck, slot in self.con.execute(
            "select distinct e.deck, f.slot from form f"
            " join entry e on e.note_id = f.note_id"
            " where f.lemma = ? and e.is_phrase = 0",
            (lemma,),
        ):
            topic = topic_of(deck)
            rank = MARK_ORDER.index(SLOT_MARK.get(slot, "other"))
            best[topic] = min(rank, best.get(topic, len(MARK_ORDER)))
        # A topic with a three-forms card is the main answer to "is this a
        # duplicate", and list truncation must not eat it.
        return [
            f"{topic} {MARK_ORDER[rank]}"
            for topic, rank in sorted(best.items(), key=lambda kv: kv[1])
        ]

    def lookup(self, word: str, hint: str | None = None) -> Hit:
        """Exact form → the form's lemma → lemma by morphology.

        hint is the lemma disambiguated in the sentence's context: more
        reliable than parsing the word alone, tried right after known forms."""
        raw = word.strip().lower()
        if not raw:
            return Hit(query=word, status=UNKNOWN)

        if " " in raw:  # a phrasal verb is a unit of its own
            hit = self._row(raw)
            if hit:
                hit.query = word
                return hit
            return Hit(query=word, status=UNKNOWN)

        # One form may hang on several lemmas: inside a sentence a word is
        # parsed without a POS tag, and 'salli' goes to the verb sallima,
        # while the dictionary card is the noun "scarf". The lemma with
        # dictionary cards comes first.
        candidates = [
            lemma for (lemma,) in self.con.execute(
                "select f.lemma from form f"
                " left join lemma l on l.lemma = f.lemma"
                " where f.form = ?"
                " group by f.lemma"
                " order by coalesce(max(l.n_word_notes), 0) desc, f.lemma",
                (raw,),
            )
        ]
        if hint:
            candidates.append(hint)
        candidates.append(raw)
        candidates.extend(self.lem.lemmas(raw))

        for candidate in dict.fromkeys(candidates):
            hit = self._row(candidate)
            if hit:
                hit.query = word
                return hit
        return Hit(query=word, status=UNKNOWN)

    def check(self, words: list[str]) -> list[Hit]:
        """lookup for vocab_check, plus context for '~' words.

        '~' isn't a duplicate: the word has no card, it only flashed by in
        other cards' examples. The learner decides — do they know it or
        will they learn it — and decides by where they saw it: a bare word
        recalls nothing.
        """
        hits = [self.lookup(w) for w in words]
        for hit in hits:
            if hit.status == SEEN and hit.lemma:
                hit.example = self._sample(hit.lemma)
                hit.marked_known = bool(self.con.execute(
                    "select 1 from debt_skip where lemma = ?", (hit.lemma,)
                ).fetchone())
        return hits

    # --- palette ----------------------------------------------------------
    def pool(
        self,
        level: str | None = None,
        topics: list[str] | None = None,
        pos: str | None = None,
        limit: int | None = None,
        stale_days: int = 14,
        stale_hint: int = 40,
        weak_hint: int = 30,
        mature_ivl: int = 21,
    ) -> Palette:
        """Topic words, the core, and two hints — forgotten and stale.

        The palette is the first fields of dictionary cards, as the learner
        wrote them: nominative for nouns, ma-infinitive for verbs. They're
        not run through morphology. The card already holds the dictionary
        form, and a context-free analyzer can only spoil it: 'Lisaks'
        turned into 'lisa', 'Kindad' into 'kinnas', the card 'Harjutama'
        spawned a second lemma harjuma (to get used to — a different verb),
        and a phrasal verb tripled into 'kaasa elama', 'kaasa elada' and
        'elan kaasa'. Morphology stays where it's irreplaceable — in lookup
        and check_sentence, which parse arbitrary text.

        The flip side: words seen only in sentences and grammar drills
        (jooma, aitama, helistama) don't enter the palette — they have no
        dictionary card. They still count as known: coverage is computed
        over the whole index.

        There's no default limit. Any number here hides part of the
        vocabulary, always the same part — and the vocabulary grows by
        ~240 words a month, so any constant goes stale within weeks. If a
        limit is passed and kicks in, the dropped count is returned so the
        truncation isn't silent.

        The topic filter alone doesn't work: the median is 5 words per
        topic, a new topic has no matches at all, and the palette came out
        empty. So topic words are joined by the core — the most frequent
        words of the level regardless of topic. The core builds an ordinary
        sentence; the topic supplies the nouns.

        Then come two hints, answering different questions. "Not used in a
        while" — common words that haven't appeared in new cards for a long
        time; that's about example monotony. "Hard" — words the learner kept
        forgetting on reviews and never brought to a mature interval; that's
        about their memory, and a new sentence with such a word adds an
        encounter exactly where it's needed.

        Nothing is excluded — we just show what to dilute with. Excluding
        "recent" words won't do: that bucket holds olema, meeldima, tegema,
        without which no sentence can be built.
        """
        marks = ",".join("?" * len(WORD_CARD_NOTETYPES))
        sql = [f"select headword, topics from entry where notetype in ({marks})"]
        args: list = list(WORD_CARD_NOTETYPES)
        if level:
            # A card without a level tag is still a learned word and must
            # not be hidden: the old deck «03. minu kodu» has no tags and
            # holds all the household vocabulary (laud, kapp, köök, lamp) —
            # exactly the nouns an A2 sentence is built from.
            sql.append("and (level = ? or level is null)")
            args.append(level.lower())
        if pos:
            sql.append("and pos = ?")
            args.append(pos)
        # SQLite's lower() only knows ASCII and would keep 'Ämber' as a
        # separate word next to 'ämber' — case is folded in Python.
        seen: dict[str, set[str]] = {}
        for headword, blob in self.con.execute(" ".join(sql), args):
            word = headword.strip().lower()
            if word:
                seen.setdefault(word, set()).update(_tag_words(json.loads(blob)))

        # Frequency and "last seen" come from the lemma index by string match.
        # For 96% of words the card's lemma equals the first field; for the
        # rest the signal is just zero and they sink to the end of the
        # ranking — it doesn't affect the palette's contents.
        #
        # Rank by frequency: n_notes is how often the word occurs across the
        # whole collection, including examples inside sentences. That's
        # "how much of a working word it is". The review interval breaks ties.
        #
        # Two dead ends we went through. Card count doesn't work: 457 words
        # have exactly four, and sorting fell back to alphabetical — words
        # starting with ä and õ never made the output. Pure interval doesn't
        # work either: ordinal numerals like «üheksasajas» float to the top —
        # they're easy, so the interval is huge, but you can't build a
        # sentence from them.
        blank = (0, 0.0, 0, None)
        meta = {
            lemma: (n_notes, strength, lapses, last_used)
            for lemma, n_notes, strength, lapses, last_used in self.con.execute(
                "select lemma, n_notes, strength, lapses, last_used from lemma"
            )
        }
        ranked = sorted(
            seen, key=lambda w: (-meta.get(w, blank)[0], -meta.get(w, blank)[1], w)
        )

        wanted = _tag_words(topics or [])
        topical, rest = [], []
        cutoff = (time.time() - stale_days * 86400) * 1000
        stale: list[tuple[int, str]] = []
        for word in ranked:
            (topical if wanted & seen[word] else rest).append(word)
            n_notes, _, _, last_used = meta.get(word, blank)
            if last_used and last_used < cutoff:
                stale.append((n_notes, word))
        stale.sort(key=lambda x: (-x[0], x[1]))
        hint = sorted(word for _, word in stale[:stale_hint])
        weak = self._weak(ranked, meta, weak_hint, mature_ivl)

        if limit is None:
            return Palette(sorted(topical), sorted(rest), 0, hint, weak)
        kept_topical = topical[:limit]
        kept_core = rest[: max(0, limit - len(kept_topical))]
        dropped = (len(topical) - len(kept_topical)) + (len(rest) - len(kept_core))
        return Palette(
            sorted(kept_topical), sorted(kept_core), dropped, hint, weak
        )

    def _weak(
        self, ranked: list[str], meta: dict, want: int, mature_ivl: int
    ) -> list[str]:
        """Words the learner kept forgetting and never got to a mature interval.

        Lapses without the interval are useless: a word may have been
        dropped half a year ago and learned solidly since (ahi — 29 lapses
        and a 103-day interval). The maturity threshold is Anki's own: 21 days.

        Ordered by severity, the most problematic first: the hint is meant
        to be taken from the top. Alphabetical order would lose all signal.

        Function words are filtered on output, not across the vocabulary:
        parsing costs a morphology call, and only ~30 words are needed.
        """
        candidates = sorted(
            (w for w in ranked if meta.get(w, (0, 0.0, 0, None))[2] >= 1
             and meta[w][1] < mature_ivl),
            key=lambda w: (-meta[w][2], meta[w][1], w),
        )
        out: list[str] = []
        for word in candidates:
            if len(out) >= want:
                break
            if not self._is_function(word):
                out.append(word)
        return out

    # --- coverage ---------------------------------------------------------
    def _is_function(self, word: str) -> bool:
        """A function word. The list covers frequent forms of olema and ei,
        and the Vabamorf codes cover postpositions (K) and conjunctions (J),
        which shouldn't be in the vocabulary anyway: that's grammar, not lexis."""
        analyses = self.lem.analyses(word)
        if {lem for lem, _ in analyses} & FUNCTION_WORDS or word in FUNCTION_WORDS:
            return True
        codes = {code for _, code in analyses if code}
        return bool(codes) and codes <= {"K", "J"}

    def goal(self, targets) -> set[str]:
        """Target words together with their lemmas.

        The model passes a word the way it's taught: 'kaerahelbed', 'botased'.
        In the text it stands in another form, and comparison goes by lemma
        (kaerahelves, botas); without this step the target word came out
        new and ended up in the monotony check."""
        out: set[str] = set()
        for target in targets or ():
            low = target.strip().lower()
            if low:
                out.add(low)
                out |= self.lem.lemmas(low)
        return out

    def _scan(self, text: str, targets: set[str]) -> list[Token]:
        """Content words of a sentence.

        The form is kept next to the lemma for a reason: morphology misses
        show only in the pair. 'soojat' is the partitive of soe, but Vabamorf
        returns it as a lemma of its own, and in the report it looks like an
        honest new word. With the form alongside, it's clear what happened.

        Proper names are skipped: 'Lätist' isn't a new word, it's Latvia.
        That only shows by case, so parsing runs on the original text,
        before lowercasing.

        targets are the cards' target words: new by definition, and they
        don't spend the budget.
        """
        out: list[Token] = []
        tokens = words(text)
        for word, (lemma, code) in zip(tokens, self.lem.sentence(tokens)):
            low = word.lower()
            if code == "H" or self._is_function(low):
                continue
            if targets and ({low, lemma} | self.lem.lemmas(low)) & targets:
                continue
            hit = self.lookup(low, hint=lemma)
            # The index doesn't fix a typo: 'söi' once got into an example,
            # became "known" and then passed silently. Only a word with its
            # own card is left alone — the dictionary doesn't know
            # 'baleriinad' or 'wc-pott', but the learner studies them on purpose.
            typo = hit.status != KNOWN and not self.lem.in_dictionary(low)
            out.append(Token(low, hit.status, hit.lemma or lemma, hit.times, typo))
        return out

    def check_sentence(self, text: str, targets: set[str] | None = None) -> Check:
        """Coverage, unknown lemmas, content lemmas and typos.

        Content words are returned as lemmas, not forms from the text:
        monotony is counted by them, and 'panin'/'pani'/'panime' must
        collapse into one word, otherwise the repeat isn't visible.

        A typo doesn't count toward coverage as a known word, but isn't
        called a new word either: it's an error in the text, to be fixed,
        not learned.
        """
        scan = self._scan(text, targets or set())
        if not scan:
            return Check(100, [], [], [])
        unknown = list(dict.fromkeys(
            t.lemma for t in scan if t.status == UNKNOWN and not t.typo
        ))
        covered = sum(t.status != UNKNOWN for t in scan)
        return Check(
            round(100 * covered / len(scan)),
            unknown,
            list(dict.fromkeys(t.lemma for t in scan)),
            list(dict.fromkeys(t.form for t in scan if t.typo)),
        )

    # --- monotony ---------------------------------------------------------
    def topic_sentence_use(self, topic: str) -> dict[str, int]:
        """How many sentences already written to the topic contain each lemma.

        Topic sentences are dictionary-card examples (slot 'example') and
        deck-03 answers (is_phrase). A deck-02 answer is a single word form,
        not a sentence, and doesn't count.
        """
        decks = topic_decks(self.con, topic)
        if not decks:
            return {}
        rows = self.con.execute(
            "select f.lemma, count(distinct f.note_id) from form f"
            " join entry e on e.note_id = f.note_id"
            f" where e.deck in ({','.join('?' * len(decks))})"
            "   and (f.slot = 'example' or (f.slot = 'Answer' and e.is_phrase = 1))"
            " group by f.lemma",
            decks,
        )
        return dict(rows)

    def topic_headwords(self, topic: str) -> set[str]:
        """The words the topic itself teaches.

        Their frequency is a property of the topic, not monotony: 'pood' in
        topic «12. Poes» must show up often, and complaining about it would
        teach the model to ignore this line altogether.
        """
        decks = topic_decks(self.con, topic)
        if not decks:
            return set()
        return {
            lemma for (lemma,) in self.con.execute(
                "select distinct f.lemma from form f"
                " join entry e on e.note_id = f.note_id"
                f" where e.deck in ({','.join('?' * len(decks))})"
                "   and e.is_phrase = 0 and f.slot <> 'example'",
                decks,
            )
        }

    def repeats(
        self,
        batch: list[list[str]],
        topic: str | None = None,
        targets: set[str] | None = None,
    ) -> list[Repeat]:
        """Words a batch of sentences overuses.

        Two independent reasons to complain. The first is the share within
        the batch itself: it catches a call where three of six sentences are
        built on one verb. The second is the top of the topic's ranking: it
        catches what the first can't see — two or three repeats per call
        that over several rounds add up to ten panema.

        Topic words are excluded from the second reason: 'pood' in «12. Poes»
        must show up often, and complaining would only teach ignoring this
        line. They aren't excluded from the first — the topic teaches the
        word, it doesn't license building the whole set on it.
        """
        if not batch:
            return []
        skip = (targets or set()) | STRUCTURAL_VERBS
        counts: dict[str, int] = {}
        for lemmas in batch:
            for lemma in set(lemmas) - skip:
                counts[lemma] = counts.get(lemma, 0) + 1

        prior = self.topic_sentence_use(topic) if topic else {}
        own = self.topic_headwords(topic) if topic else set()
        hot = self._hot(prior, skip | own | FUNCTION_WORDS)
        floor = max(REPEAT_MIN, round(len(batch) ** 0.5))

        out = []
        for lemma, n in counts.items():
            if n >= floor or lemma in hot:
                out.append(Repeat(lemma, n, prior.get(lemma, 0), lemma in own, n >= floor))
        # format_repeats truncates, per line: five "rewrite" items must not
        # eat the room of "keep in mind".
        out.sort(key=lambda r: (not r.crowded, -r.in_batch, -r.in_topic, r.lemma))
        return out

    @staticmethod
    def _hot(usage: dict[str, int], skip: set[str]) -> set[str]:
        """Top of the topic's ranking — words it already has too many of."""
        ranked = sorted(
            ((n, w) for w, n in usage.items()
             if n >= REPEAT_IN_TOPIC and w not in skip),
            key=lambda x: (-x[0], x[1]),
        )
        return {w for _n, w in ranked[:REPEAT_TOP]}

    def novel(self, text: str, taught: set[str] | None = None) -> Novel:
        """What's new in a sentence — input for the deck-03 dose.

        Differs from check_sentence in that rare '~' words don't pass
        silently. There, a word glimpsed in another card's example honestly
        counts as known — otherwise the check would complain about words
        from the learner's own cards. For the dose it's a different
        question: one encounter in someone else's example isn't the same as
        learning it, and such a word also grows the vocabulary, just more
        gently than a completely unknown one.

        The HALF_KNOWN_MAX threshold is essential: the A1 branch isn't in the
        index, so without it ema, koer and täna ended up "half-known" —
        the most familiar words there are.
        """
        unknown: dict[str, str] = {}
        half: dict[str, str] = {}
        for t in self._scan(text, taught or set()):
            # A typo doesn't count toward the dose: 'mureled' for 'murelid'
            # was counted as a new topic word.
            if t.status == UNKNOWN and not t.typo:
                unknown.setdefault(t.lemma, t.form)
            elif t.status == SEEN and t.times <= HALF_KNOWN_MAX:
                half.setdefault(t.lemma, t.form)
        return Novel(
            unknown=[(lemma, form) for lemma, form in unknown.items()],
            half=[(lemma, form) for lemma, form in half.items()],
        )


    # --- dose across the whole topic fill ---------------------------------
    def dose_history(self, topic_key: str) -> tuple[int, list[str]]:
        """How many words, and which new words, already went into the topic within the window."""
        since = int(time.time() - DOSE_WINDOW_HOURS * 3600)
        words_total, new = 0, []
        for n, blob in self.con.execute(
            "select words, new from dose_log where topic = ? and at >= ? order by at",
            (topic_key, since),
        ):
            words_total += n
            new += [w for w in json.loads(blob) if w not in new]
        return words_total, new

    def record_dose(self, topic_key: str, words_count: int, new: list[str]) -> None:
        self.con.execute(
            "insert into dose_log(topic, at, words, new) values (?, ?, ?, ?)",
            (topic_key, int(time.time()), words_count, json.dumps(new, ensure_ascii=False)),
        )
        self.con.commit()

    # --- debt: words without their own card -------------------------------
    def _frozen_case(self, lemma: str) -> bool:
        """An adverb frozen from a case of a word that has a card: 'lukku'
        with a card for lukk. That's not debt but a form of a known word,
        and context doesn't fix it — Vabamorf treats 'lukku' as an adverb
        even in a sentence."""
        if not {code for _, code in self.lem.analyses(lemma)} <= {"D", "K"}:
            return False
        others = {lem for lem, _ in self.lem.variants(lemma)} - {lemma}
        if not others:
            return False
        marks = ",".join("?" * len(others))
        return bool(self.con.execute(
            f"select 1 from lemma where lemma in ({marks}) and n_word_notes > 0",
            tuple(others),
        ).fetchone())

    def _sample(self, lemma: str, cap: int = 80) -> str | None:
        """The shortest sentence where the word appeared. A bare word without
        context recalls nothing — and it's recognition ("ah, that's where it's
        from") that decides whether to take it into a topic."""
        best: str | None = None
        for form, examples, et_raw, is_phrase in self.con.execute(
            "select f.form, e.examples, e.et_raw, e.is_phrase from form f"
            " join entry e on e.note_id = f.note_id where f.lemma = ?",
            (lemma,),
        ):
            texts = [et for et, _ru in json.loads(examples)]
            if is_phrase:
                texts.append(et_raw)
            for text in texts:
                if form.lower() in text.lower() and (
                    best is None or len(text) < len(best)
                ):
                    best = text
        if best and len(best) > cap:
            best = best[: cap - 1].rstrip() + "…"
        return best

    def debt_skip(self, words: list[str], undo: bool = False) -> tuple[int, int]:
        """ "I know this" — so the word isn't suggested again.

        Otherwise every new chat brings the same dozen: the tool reads the
        index, the index is built from Anki, and the learner's answer isn't
        there and can't come from anywhere.

        The word is reduced to its lemma: the learner answers with the word
        they saw, but it may be an inflected form from an example.
        """
        keys = set()
        for word in words:
            low = word.strip().lower()
            if not low:
                continue
            known = self.con.execute(
                "select 1 from lemma where lemma = ?", (low,)
            ).fetchone()
            keys.add(low if known else self.lem.lemma(low))
        if not keys:
            return 0, self.con.execute(
                "select count(*) from debt_skip"
            ).fetchone()[0]
        marks = ",".join("?" * len(keys))
        if undo:
            self.con.execute(
                f"delete from debt_skip where lemma in ({marks})", tuple(keys)
            )
        else:
            self.con.executemany(
                "insert or replace into debt_skip(lemma, added) values (?, ?)",
                [(k, int(time.time())) for k in keys],
            )
        self.con.commit()
        total = self.con.execute("select count(*) from debt_skip").fetchone()[0]
        return len(keys), total

    def debt(self, limit: int = 10, offset: int = 0) -> DebtPage:
        """Words met only inside other cards' sentences.

        The learner reads them on every review but they have no card — so
        neither forms nor translation were ever learned. It's a ready
        "what to add" list, drawn from the learner's own material rather
        than a general frequency dictionary.

        Ordered by number of encounters: the more a word catches the eye,
        the more annoying it is that it isn't learned.

        Selection goes from the top and stops at the needed page: parsing
        function words costs a morphology call, and the learner pages
        through two or three. Hence the "is there more" answer — we take
        one word extra.

        Bounds are clamped silently, and that's not the same as the palette
        limit. There a cut hid part of the vocabulary forever and had to be
        shouted about; here the next page is one call away, and the bottom
        line already says the list goes on. A silently empty answer on
        limit=0 would be much worse: it reads as "no debt".
        """
        limit = max(1, min(limit, DEBT_PAGE_MAX))
        offset = max(0, offset)
        picked: list[tuple[str, int]] = []
        need = offset + limit + 1
        for lemma, n_notes in self.con.execute(
            # A card outside scope is still a card: in the A1 branch laps,
            # koer and sõber have one, and without this condition the tool
            # claimed the learner had never studied them.
            "select lemma, n_notes from lemma where n_word_notes = 0"
            " and lemma not in (select lemma from outside)"
            " and lemma not in (select lemma from debt_skip)"
            " order by n_notes desc, lemma"
        ):
            if len(picked) >= need:
                break
            if len(lemma) < 2 or " " in lemma:
                continue
            if (
                self._is_function(lemma)
                or self.lem.is_name(lemma)
                or self._frozen_case(lemma)
            ):
                continue
            picked.append((lemma, n_notes))
        page = picked[offset : offset + limit]
        return DebtPage(
            [Debt(w, n, self._sample(w)) for w, n in page],
            offset,
            len(picked) > offset + limit,
            self.con.execute("select count(*) from debt_skip").fetchone()[0],
        )


SUBDECKS = ("01. sõnad", "02. sõnad typein", "03. lause typein")


_NUMBER_RE = re.compile(r"^\s*\d+[.)]?\s*")


def topic_decks(con: sqlite3.Connection, topic: str) -> list[str]:
    """A topic's decks — by the path level's name, not by substring.

    The substring 'Poes' also caught 14. Riidepoes and 16. Toidupoes. A
    numbered topic ('12. Poes') must match a path level exactly; an
    unnumbered one ('Poes') — a level with its number stripped.
    """
    want = topic.strip().lower()
    if not want:
        return []
    numbered = bool(_NUMBER_RE.match(want))

    def matches(part: str) -> bool:
        part = part.strip().lower()
        return part == want if numbered else _NUMBER_RE.sub("", part) == want

    return [
        deck for (deck,) in con.execute("select distinct deck from entry order by deck")
        if any(matches(part) for part in deck.split("::"))
    ]


def topic_of(deck: str) -> str:
    """Extract the topic from a deck path: the 01/02/03 subdeck by itself
    ('01. sõnad') says nothing; decisions are made per topic."""
    parts = deck.split("::")
    if len(parts) > 1 and parts[-1] in SUBDECKS:
        parts.pop()
    return parts[-1] if parts else deck


def topic_state(con: sqlite3.Connection, topic: str) -> str:
    """Replaces the list_decks + find_notes combo with a single call."""
    decks = topic_decks(con, topic)
    marks = ",".join("?" * len(decks))
    rows = con.execute(
        f"select deck, count(*) from entry where deck in ({marks}) group by 1 order by 1",
        decks,
    ).fetchall() if decks else []
    if not rows:
        return (
            f"topic '{topic}' is not in the index — either it does not exist "
            "yet, or its deck is out of scope"
        )
    root = rows[0][0].split("::")
    while root and root[-1] in SUBDECKS:
        root.pop()
    root_path = "::".join(root)

    lines = [root_path]
    present = {r[0].split("::")[-1]: r[1] for r in rows}
    for sub in SUBDECKS:
        n = present.get(sub)
        lines.append(f"  {sub:18} {n if n is not None else '—'}")
    # By lemma, not headword: in subdeck 02 each form is its own note, and a
    # headword list gave 'Alates' and 'alates', 'sall' and 'salli' as
    # different words, tripling the topic counter.
    heads = [
        h for (h,) in con.execute(
            "select distinct f.lemma from form f"
            " join entry e on e.note_id = f.note_id"
            f" where e.deck in ({marks}) and e.is_phrase = 0 and f.slot <> 'example'"
            " order by f.lemma",
            decks,
        )
    ]
    if heads:
        # The full list on a big topic is about a thousand tokens.
        # vocab_check exists for checking specific words.
        shown = heads[:60]
        tail = f" … +{len(heads) - len(shown)} more" if len(heads) > len(shown) else ""
        lines.append(f"words ({len(heads)}): " + ", ".join(shown) + tail)
    return "\n".join(lines)


# --- compact output: saving tokens is the whole point of the project ---


def format_check(hits: list[Hit]) -> str:
    width = max((len(h.query) for h in hits), default=1)
    lines = []
    for h in hits:
        if h.status == UNKNOWN:
            lines.append(f"{h.query:<{width}} ✗")
            continue
        if h.status == SEEN:
            # The tags here belong to someone else — the card whose example
            # the word appeared in — so show the hit count and the sentence instead.
            seen_in = f"  «{h.example}»" if h.example else ""
            known = "  · learner marked it known" if h.marked_known else ""
            lines.append(
                f"{h.query:<{width}} ~ seen {h.times}×  "
                f"{'/'.join(h.forms[:4])}{seen_in}{known}".rstrip()
            )
            continue
        tags = ",".join(h.topics[:3])
        meta = " ".join(x for x in (h.level, tags) if x)
        forms = "/".join(h.forms[:4])
        # The topic list is the basis of the duplicate report: it decides
        # whether it's a real duplicate or the word came from another topic.
        # Cut for tokens, but never silently — 23% of words live in more
        # than two topics.
        shown = "; ".join(h.decks[:2])
        extra = len(h.decks) - 2
        where = f"  [{shown}{f'; +{extra} more' if extra > 0 else ''}]" if h.decks else ""
        lines.append(
            f"{h.query:<{width}} {MARK[h.status]} {meta}  {forms}{where}".rstrip()
        )
    known = sum(1 for h in hits if h.status == KNOWN)
    seen = sum(1 for h in hits if h.status == SEEN)
    missing = [h.query for h in hits if h.status == UNKNOWN]
    lines.append(f"— word card {known}, sentences only {seen}, missing {len(missing)}")
    if seen:
        lines.append(
            "— '~' is not a duplicate: no card, only met inside other cards' "
            "examples. Don't card it yet — show it to the learner and let "
            "them decide"
        )
    if missing:
        lines.append("missing: " + ", ".join(missing))
    return "\n".join(lines)


def format_pool(p: Palette) -> str:
    topical, core, dropped = p.topical, p.core, p.dropped
    if not topical and not core:
        return "nothing matches these filters — drop level or pos"
    lines = []
    if topical:
        lines.append(f"topic ({len(topical)}): " + " · ".join(topical))
    if core:
        label = "rest of vocabulary" if topical else "vocabulary"
        lines.append(f"{label} ({len(core)}): " + " · ".join(core))
    if p.weak:
        lines.append(
            f"forgotten in reviews, hardest first ({len(p.weak)}): "
            + " · ".join(p.weak)
        )
        lines.append(
            "— these the learner keeps failing: a content word taken from "
            "here buys an extra encounter where memory is actually weak"
        )
    if p.stale:
        lines.append(
            f"not used lately ({len(p.stale)}): " + " · ".join(p.stale)
        )
        lines.append(
            "— a nudge for variety, not a limit: the full vocabulary is above"
        )
    if dropped:
        lines.append(
            f"⚠ truncated by limit: {dropped} words hidden. Drop limit or "
            "narrow with pos — otherwise these words never reach your examples."
        )
    return "\n".join(lines)


def sentence_ok(pct: int, new: list[str], min_coverage: int = 70) -> bool:
    """Soft rule: one new word is always allowed, beyond that coverage
    decides. A hard cap of "exactly one new" broke sentences — a natural
    phrase can't always be assembled from what's known.

    A separate function because two places compute the verdict: the report
    and the log line. A duplicated formula would diverge on the first edit,
    and the log would start lying about its own response.
    """
    return pct >= min_coverage or len(new) <= 1


def format_repeats(repeats: list[Repeat], with_topic: bool) -> list[str]:
    """Monotony lines. A separate function — two places print them:
    text_check on a draft and topic_write on write.

    Two lines, and not for decoration. The first is about this batch — it
    gets rewritten. The second is about the word's accumulated weight in
    the topic, and it fires on almost every call in a mature topic; mixing
    them would put "rewrite" next to "keep in mind", and the model would
    read such a list as noise and stop reading it at all.
    """
    lines = []
    crowded = [r for r in repeats if r.crowded][:REPEAT_SHOWN]
    heavy = [r for r in repeats if not r.crowded][:REPEAT_SHOWN]
    if crowded:
        parts = []
        for r in crowded:
            tail = (
                f" (+{r.in_topic} already in the topic)"
                if with_topic and r.in_topic else ""
            )
            parts.append(f"{r.lemma} ×{r.in_batch}{tail}")
        lines += [
            "leaning too hard on: " + " · ".join(parts),
            "— these sentences read as variations on one word: rewrite some "
            "of them around another. Structural verbs are already excluded, "
            "so everything listed here has an alternative.",
        ]
    if heavy:
        lines += [
            "already heavy in this topic: "
            + " · ".join(f"{r.lemma} ({r.in_topic})" for r in heavy),
            "— a preference, not a verdict: where a swap costs nothing, take "
            "the word from vocab_pool's 'forgotten in reviews' or 'not used "
            "lately' line instead.",
        ]
    return lines


def format_text_check(
    results: list[Check],
    min_coverage: int = 70,
    repeats: list[Repeat] | None = None,
    with_topic: bool = False,
) -> str:
    lines = []
    candidates: list[str] = []
    typos: list[str] = []
    low = 0
    for i, r in enumerate(results, 1):
        for w in r.new:
            if w not in candidates:
                candidates.append(w)
        typos += [w for w in r.typos if w not in typos]
        ok = sentence_ok(r.pct, r.new, min_coverage)
        low += not ok
        shown = ", ".join(r.new + [f"{w}?" for w in r.typos]) or "—"
        verdict = "typo?" if r.typos else ("ok" if ok else "low")
        lines.append(f"{i:<3}{r.pct:>4}%  {shown:<34} {verdict}")
    avg = round(sum(r.pct for r in results) / len(results)) if results else 100
    tail = f"average coverage {avg}%"
    if low:
        tail += f" · below {min_coverage}%: {low} of {len(results)}"
    if candidates:
        tail += "\nnew words (candidates for the next topic): " + ", ".join(candidates)
    if typos:
        tail += (
            "\nnot in the dictionary — likely typos, fix the spelling "
            "(a misspelt word teaches a wrong form): " + ", ".join(typos)
        )
    lines.append(tail)
    lines.extend(format_repeats(repeats or [], with_topic))
    return "\n".join(lines)


def format_debt(page: DebtPage) -> str:
    items, offset = page.items, page.offset
    if not items:
        return (
            "no words left in the debt list"
            + (f" past #{offset}" if offset else "")
        )
    lines = [
        "met only inside example sentences — no card of their own, "
        "so the forms and the meaning were never learned:"
    ]
    width = max(len(i.word) for i in items)
    for n, item in enumerate(items, start=offset + 1):
        # Tags aren't shown here: they're tags of another card whose example
        # the word appeared in in passing — they say nothing about the word
        # itself and only lengthen the line. The sentence gives the context.
        seen_in = f"  {item.example}" if item.example else ""
        lines.append(
            f"{n:>3}. {item.word:<{width}} {item.times:>4}×{seen_in}".rstrip()
        )
    lines.append(
        f"next page: vocab_debt(offset={offset + len(items)})"
        if page.more
        else "— end of the list"
    )
    # Explain the marking mechanism in the output itself: otherwise the same
    # dozen comes back in every new chat, and nobody will guess why.
    tail = "say vocab_debt_skip(words=[…]) for any the learner already knows"
    if page.skipped:
        tail += f" · {page.skipped} already marked known"
    lines.append("— " + tail)
    return "\n".join(lines)
