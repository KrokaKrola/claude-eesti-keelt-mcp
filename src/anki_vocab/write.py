"""Writing cards to Anki via the Anki MCP Server add-on.

We never write to the live collection with our own SQLite — collection.py
explains why in detail. Anki writes; we only deliver ready notes to it
over HTTP on localhost.

The module has two halves:

  * ``build_topic`` expands a word into notes for the topic's three
    subdecks — pure functions with no network, which the tests cover;
  * ``call`` and ``write_topic`` talk to the add-on.

The word-to-notes layout used to live in the model's head: it read the
label table from the skill and copied it by hand into 45 notes. Here
it has no choice, and so no way to get it wrong.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import ADDON_META, DOSE_WINDOW_HOURS, NEW_EVERY, NEW_MAX, POS_TAGS
from .query import format_repeats

SUBDECKS = ("01. sõnad", "02. sõnad typein", "03. lause typein")
MODEL_WORD = "Estonian 3 Forms"
MODEL_TYPEIN = "Estonian Word Forms with TypeIn"

# The add-on caps a batch at 100 notes; a word takes up to four deck-02
# notes — one per form — so that deck hits the cap first.
MAX_NOTES_PER_BATCH = 100
NOTES_PER_WORD_02 = 4
MAX_WORDS = MAX_NOTES_PER_BATCH // NOTES_PER_WORD_02

NOUN_LABELS = (
    "1-я форма (номинатив)",
    "2-я форма (генитив)",
    "3-я форма (партитив)",
    "4-я форма (партитив мн. ч.)",
)
VERB_LABELS = (
    "1-я форма (ma-инфинитив)",
    "2-я форма (da-инфинитив)",
    "3-я форма (mina-vorm)",
)

# These decline like nouns — and only they can have a fourth form, the
# partitive plural. Everything else either conjugates (verb) and gets by
# with three, or doesn't change at all and gets no form labels.
# The fourth form of a declinable word is optional: for mass nouns
# (vesi, jahu, mesi, sool, või) the partitive plural isn't used in
# practice, and an invented form is worse than none — three forms are
# accepted as is, Form4 goes to Anki empty, and deck 02 gets three notes
# for the word instead of four.
NOUN_LIKE = {"существительное", "прилагательное", "местоимение", "числительное"}

FORMS_RULE = (
    "4 non-empty forms for a declinable word (nominative, genitive, partitive, "
    "partitive plural) — 3 if the partitive plural is not used in practice "
    "(mass noun: vesi, jahu, mesi), 3 for a verb, 1 for an invariant word"
)


@dataclasses.dataclass
class Dose:
    """How much new vocabulary actually went into deck-03 sentences.

    The only place the dose is visible at all. text_check can't compute
    it: it gets a flat list of sentences and doesn't know which belong to
    deck 03 — and the dose lives only there.
    """

    # Whether the dose was computed at all: without the closure from server.py
    # (tests, plain note building) an empty new-word list means "didn't look",
    # not "the model added nothing".
    counted: bool = False
    words: int = 0
    # Expectation for the whole topic fill, including earlier calls in the window.
    expected_total: int = 0
    # What went into the topic earlier within DOSE_WINDOW_HOURS.
    prior_words: int = 0
    prior_new: list[str] = dataclasses.field(default_factory=list)
    new: list[str] = dataclasses.field(default_factory=list)
    half: list[str] = dataclasses.field(default_factory=list)
    # Sentences with two new words at once: two unknowns in a phrase the
    # learner translates on their own — exactly the difficulty the dose is
    # meant to avoid.
    crowded: list[str] = dataclasses.field(default_factory=list)

    @property
    def expected(self) -> int:
        """How many more we expect from this call."""
        return max(0, self.expected_total - len(self.prior_new))

    @property
    def short(self) -> bool:
        return len(self.new) < self.expected

    @property
    def over(self) -> bool:
        return bool(self.new) and (
            len(self.prior_new) + len(self.new) > self.expected_total
        )


def expected_dose(n_words: int) -> int:
    """How many new words to expect from filling a topic of n_words words.

    No lower bound: the old max(1, …) demanded a new word from a one-word
    call — three times more often than intended, and on 01.10 it produced
    the chain jogurt → keefir → kohupiim → pannkook → uba, where each new
    word pulled in the next.
    """
    return min(n_words // NEW_EVERY, NEW_MAX)


class AnkiError(RuntimeError):
    """The add-on is unavailable or refused the request."""


class NoteDataError(ValueError):
    """Word data failed validation.

    Collect all complaints at once: one at a time, the model fixes them in
    circles, rewriting the whole list each time.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# --------------------------------------------------------------------------
# Expanding a word into notes
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Batch:
    """One subdeck: which model and which notes go into it."""

    deck: str
    model: str
    notes: list[dict]


def topic_words(topic: str) -> list[str]:
    """'09. Vaba aeg' → ['aeg', 'vaba'].

    Strip the number, lowercase the words and sort them: vocab_pool later
    searches by these tags, and the order must be the same across all
    topics.
    """
    bare = re.sub(r"^\s*\d+[.)]?\s*", "", topic.strip())
    return sorted({w.lower() for w in re.findall(r"[^\W\d_]+", bare, re.UNICODE)})


def level_tag(course: str) -> str | None:
    """'kursuse eesti keel::A2' → 'A2'. No match — no level tag."""
    tail = course.split("::")[-1].strip()
    return tail.upper() if re.fullmatch(r"[ABC][12]", tail, re.IGNORECASE) else None


def form_labels(pos: str, forms: list[str]) -> tuple[str, ...] | None:
    """Form labels — three, or four for a declinable word. None means the
    word is invariant.

    Invariance is judged by the forms themselves, not by POS: an adverb
    with three identical forms and a pronoun like 'kogu' behave the same,
    and asking the model about it is one more chance to get it wrong.
    """
    if len({f.strip().lower() for f in forms}) == 1:
        return None
    labels = VERB_LABELS if pos == "глагол" else NOUN_LABELS
    return labels[: len(forms)]


def _pair(value: Any) -> tuple[str, str] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        et, ru = (str(x).strip() for x in value)
        if et and ru:
            return et, ru
    return None


def _validate(word: dict, seen: set[str], where: str, problems: list[str]) -> dict | None:
    """Validate one word. Returns the normalized form or None."""
    before = len(problems)

    et = str(word.get("word", "")).strip()
    if not et:
        problems.append(f"{where}: empty 'word'")
        et = "?"

    key = et.lower()
    if key in seen:
        problems.append(f"{where}: '{et}' appears twice in the list")
    seen.add(key)

    pos = POS_TAGS.get(str(word.get("pos", "")).strip().lower())
    if pos is None:
        problems.append(
            f"{where} ({et}): pos='{word.get('pos')}' — expected one of: "
            + ", ".join(sorted(set(POS_TAGS.values())))
        )

    ru = str(word.get("ru", "")).strip()
    if not ru:
        problems.append(f"{where} ({et}): empty 'ru'")

    forms = word.get("forms")
    if isinstance(forms, str):
        forms = [forms]
    if not isinstance(forms, list) or not forms or not all(str(f).strip() for f in forms):
        problems.append(f"{where} ({et}): forms — {FORMS_RULE}")
        forms = []
    else:
        forms = [str(f).strip() for f in forms]
        # Invariance shows in the forms, not the POS: 'kogu' is tagged as a
        # pronoun but has no partitive plural.
        invariant = len({f.lower() for f in forms}) == 1
        declinable = pos in NOUN_LIKE and not invariant
        if len(forms) == 1:
            forms = forms * 3
        elif len(forms) == 4 and not declinable:
            problems.append(
                f"{where} ({et}): forms — got 4, but the 4th form (partitive "
                "plural) belongs to a declinable word only; a verb takes 3"
            )
            forms = []
        elif len(forms) not in (3, 4):
            problems.append(f"{where} ({et}): forms — got {len(forms)}, need {FORMS_RULE}")
            forms = []

    raw_ex = word.get("ex")
    if isinstance(raw_ex, list) and len(raw_ex) in (1, 3):
        pairs = [_pair(p) for p in raw_ex]
        if None in pairs:
            problems.append(f"{where} ({et}): ex — each example is a pair ['estonian', 'russian'], both non-empty")
            ex = []
        else:
            ex = pairs * 3 if len(pairs) == 1 else pairs
    else:
        problems.append(f"{where} ({et}): ex — 3 pairs ['estonian', 'russian'] (or 1 for an invariant word)")
        ex = []

    sentence = _pair(word.get("sentence"))
    if sentence is None:
        problems.append(f"{where} ({et}): sentence — a pair ['estonian', 'russian'] for deck 03")

    if len(problems) > before:
        return None
    return {"word": et, "pos": pos, "ru": ru, "forms": forms, "ex": ex, "sentence": sentence}


def build_topic(
    course: str,
    topic: str,
    words: list[dict],
    form_in_example: Callable[[str, str], bool] | None = None,
    novel_words: Callable[[str, set[str]], tuple[list[str], list[str]]] | None = None,
    dose_history: tuple[int, list[str]] = (0, []),
    form_check: Callable[[list[dict]], tuple[list[str], list[str]]] | None = None,
) -> tuple[list[Batch], list[str], Dose]:
    """Words → three note batches, warnings and a dose report.

    A warning is not a rejection: 4% of dictionary cards in the collection
    already break the "example contains the form" rule quite legitimately
    (the verb 'algama' has an example with 'algab'), so it can't block.
    """
    if not words:
        raise NoteDataError(["words list is empty"])
    if len(words) > MAX_WORDS:
        raise NoteDataError(
            [f"{len(words)} words at once — the add-on takes at most {MAX_WORDS} "
             f"(deck 02 makes up to {NOTES_PER_WORD_02} notes per word, batch cap "
             f"{MAX_NOTES_PER_BATCH}). "
             "Split into several calls."]
        )

    problems: list[str] = []
    seen: set[str] = set()
    clean = [
        ok
        for i, w in enumerate(words)
        if (ok := _validate(w if isinstance(w, dict) else {}, seen, f"word {i + 1}", problems))
    ]
    if problems:
        raise NoteDataError(problems)

    warnings: list[str] = []
    if form_check is not None:
        # Forms against the dictionary — before writing, with rejection: a
        # wrong form on a card gets memorized and then has to be fixed in memory.
        wrong, unsure = form_check(clean)
        if wrong:
            raise NoteDataError(wrong)
        warnings += unsure

    shared = [t for t in (level_tag(course),) if t] + topic_words(topic)
    root = f"{course}::{topic}"
    prior_words, prior_new = dose_history
    dose = Dose(
        counted=novel_words is not None,
        words=len(clean),
        expected_total=expected_dose(prior_words + len(clean)),
        prior_words=prior_words,
        prior_new=list(prior_new),
    )
    # The topic's own words don't count as new: they're what's being taught.
    # The caller lemmatizes them — this module knows nothing about morphology.
    taught = {w["word"].strip().lower() for w in clean}

    notes01: list[dict] = []
    notes02: list[dict] = []
    notes03: list[dict] = []

    for w in clean:
        forms, ex, pos = w["forms"], w["ex"], w["pos"]
        labels = form_labels(pos, forms)

        if form_in_example is not None:
            # zip stops at three example pairs: there's nothing to check the
            # fourth form against, it has no example.
            for i, (form, (et, _)) in enumerate(zip(forms, ex), start=1):
                if not form_in_example(form, et):
                    warnings.append(f"{w['word']}: form {i} ({form}) is absent from its example '{et}'")

        # Form4 is always included: verbs and invariant words don't have it,
        # and the field must reach Anki empty rather than vanish from the note —
        # the template shows the fourth form only when it's filled.
        fields01 = {"Russian": w["ru"], "Add Reverse": "1", "Form4": ""}
        for i, form in enumerate(forms, start=1):
            fields01[f"Form{i}"] = form
        for i, (et, ru) in enumerate(ex, start=1):
            fields01[f"Ex{i}"] = et
            fields01[f"Ex{i}ru"] = ru
        notes01.append({"fields": fields01, "tags": sorted({*shared, pos})})

        # One note per form, word by word: Anki shows new cards in the order
        # they were added, and the three forms should come up together, not
        # scattered over a week.
        # The fourth form has no example — it's asked as is.
        slots = (
            [
                (label, form, ex[i] if i < len(ex) else ("", ""))
                for i, (label, form) in enumerate(zip(labels, forms))
            ]
            if labels
            else [("", forms[0], ex[0])]
        )
        for label, form, (et, ru) in slots:
            notes02.append({
                "fields": {
                    "Russian": w["ru"],
                    "Form": label,
                    "Answer": form.lower(),
                    "Example_ET": et,
                    "Example_RU": ru,
                },
                "tags": sorted({*shared, pos}),
            })

        et, ru = w["sentence"]
        if novel_words is not None:
            new, half = novel_words(et, taught)
            dose.new.extend(x for x in new if x not in dose.new)
            dose.half.extend(x for x in half if x not in dose.half)
            if len(new) > 1:
                dose.crowded.append(et)
        notes03.append({
            "fields": {
                "Russian": ru,
                "Form": "",
                "Answer": et,
                "Example_ET": "",
                "Example_RU": "",
            },
            "tags": sorted({*shared, "lause"}),
        })

    batches = [
        Batch(f"{root}::{SUBDECKS[0]}", MODEL_WORD, notes01),
        Batch(f"{root}::{SUBDECKS[1]}", MODEL_TYPEIN, notes02),
        Batch(f"{root}::{SUBDECKS[2]}", MODEL_TYPEIN, notes03),
    ]
    return batches, warnings, dose


# --------------------------------------------------------------------------
# Talking to the add-on
# --------------------------------------------------------------------------


def endpoint() -> str:
    """The add-on's address comes from its own settings: the port can be
    changed in its config, and a hardcoded one would silently diverge."""
    if url := os.environ.get("ANKI_MCP_URL"):
        return url
    host, port, path = "127.0.0.1", 3141, ""
    try:
        cfg = json.loads(ADDON_META.read_text()).get("config", {})
        host = cfg.get("http_host") or host
        port = cfg.get("http_port") or port
        path = cfg.get("http_path") or ""
    except (OSError, ValueError):
        pass
    return f"http://{host}:{port}/{path.lstrip('/')}"


def _unwrap(body: str) -> dict:
    """The response comes as SSE: 'event: message' and a 'data: {...}' line."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AnkiError(f"add-on replied off-protocol: {body[:200]}")


def rpc(method: str, params: dict, timeout: float = 60.0) -> dict:
    """JSON-RPC request to the add-on. Errors become a readable string —
    the model sees it instead of a traceback and can tell the user what
    to do."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        endpoint(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AnkiError(
            f"cannot reach the add-on at {endpoint()}: {exc}. "
            "Is Anki running? Nothing was written."
        ) from exc

    message = _unwrap(body)
    if "error" in message:
        raise AnkiError(f"{method}: {message['error'].get('message', message['error'])}")
    return message.get("result", {})


def call(tool: str, args: dict, timeout: float = 60.0) -> dict:
    """Call an add-on tool."""
    result = rpc("tools/call", {"name": tool, "arguments": args}, timeout)
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []))
        raise AnkiError(f"{tool}: {text or 'add-on returned an error with no text'}")
    # structuredContent and content duplicate each other — take the already
    # parsed one and don't carry the extra copy further.
    if (structured := result.get("structuredContent")) is not None:
        return structured
    for chunk in result.get("content", []):
        if chunk.get("type") == "text":
            return json.loads(chunk["text"])
    raise AnkiError(f"{tool}: empty response")


def deck_names() -> set[str]:
    """All Anki decks. The response is large, but it stays inside the
    server and never reaches the model's context."""
    return {d["name"] for d in call("list_decks", {}).get("decks", [])}


def _nearest_existing(deck: str, existing: set[str]) -> str:
    """Nearest existing ancestor: the add-on's create_deck only handles two
    levels, so a deep branch has to be created by moving cards. Notes land
    here temporarily."""
    parts = deck.split("::")
    for i in range(len(parts) - 1, 0, -1):
        if (candidate := "::".join(parts[:i])) in existing:
            return candidate
    return "Default"


def _card_ids(note_ids: list[int]) -> list[int]:
    info = call("notes_info", {"notes": note_ids, "include_fields": [], "excerpt_chars": 1})
    return [cid for note in info.get("notes", []) for cid in note.get("cards", [])]


def _add(batch: Batch, existing: set[str]) -> tuple[int, list[str]]:
    """Put a batch into its subdeck, creating it if needed.

    Returns the number of notes created and the add-on's rejections.
    """
    if not batch.notes:
        return 0, []

    target_exists = batch.deck in existing
    dest = batch.deck if target_exists else _nearest_existing(batch.deck, existing)

    result = call("add_notes", {
        "deck_name": dest,
        "model_name": batch.model,
        "notes": batch.notes,
        # Duplicates were filtered earlier via vocab_check; here the flag only
        # gets in the way by making Anki reject three forms of one word.
        "allow_duplicate": True,
    })

    created = [r for r in result.get("results", []) if r.get("status") == "created"]
    rejected = [
        f"note {r['index'] + 1}: {r.get('error') or r.get('reason', 'rejected')}"
        for r in result.get("results", [])
        if r.get("status") != "created"
    ]

    if not target_exists and created:
        # card_change_deck creates a path of any depth — the only way to get
        # a four-level deck through the add-on.
        call("card_change_deck", {
            "card_ids": _card_ids([r["note_id"] for r in created]),
            "deck": batch.deck,
        })
        existing.add(batch.deck)

    return len(created), rejected


def write_topic(
    course: str,
    topic: str,
    words: list[dict],
    form_in_example: Callable[[str, str], bool] | None = None,
    novel_words: Callable[[str, set[str]], tuple[list[str], list[str]]] | None = None,
    repeat_check: Callable[[list[str], set[str]], list] | None = None,
    spell_check: Callable[[list[str], set[str]], list[tuple[str, str]]] | None = None,
    form_check: Callable[[list[dict]], tuple[list[str], list[str]]] | None = None,
    dose_history: tuple[int, list[str]] = (0, []),
    dry_run: bool = False,
) -> tuple[str, Dose]:
    """Full pass: validate, lay out, create decks, write.

    The dose report is returned as a second value, not just as a string:
    the caller logs it, otherwise telling after the fact whether the dose
    was respected would mean rereading the model's replies by eye.
    """
    batches, warnings, dose = build_topic(
        course, topic, words, form_in_example, novel_words, dose_history, form_check
    )
    # Counted here too, not only in text_check: that one checks a draft and
    # may not be called at all — while this receives exactly what goes into
    # the deck.
    repeats = repeat_check(*_sentences(batches)) if repeat_check else []
    typos = spell_check(*_sentences(batches)) if spell_check else []

    head = f"{course}::{topic}"
    lines: list[str] = []

    if dry_run:
        lines.append(f"{head} — dry run, nothing written")
        for batch in batches:
            lines.append(f"  {batch.deck.split('::')[-1]:<18} {len(batch.notes)}")
        example = batches[0].notes[0]
        lines.append("  first note of 01: " + json.dumps(example, ensure_ascii=False))
        return (
            "\n".join(
                lines + _typo_lines(typos) + _dose_lines(dose)
                + _repeat_lines(repeats) + _warning_lines(warnings)
            ),
            dose,
        )

    existing = deck_names()
    total = 0
    for batch in batches:
        name = batch.deck.split("::")[-1]
        try:
            created, rejected = _add(batch, existing)
        except AnkiError as exc:
            # Stop here: half a topic beats half with a hole in the middle,
            # and Ctrl+Z in Anki undoes what was written as a whole.
            lines.append(f"  {name:<18} ERROR: {exc}")
            lines.append("  remaining decks untouched; Ctrl+Z in Anki undoes what was written")
            return "\n".join([f"{head} — {total} notes written, then failed"] + lines), dose
        total += created
        mark = "✓" if not rejected else "⚠"
        lines.append(f"  {name:<18} {created} {mark}")
        lines.extend(f"      {r}" for r in rejected)

    return (
        "\n".join(
            [f"{head} — {total} notes"] + lines + _typo_lines(typos)
            + _dose_lines(dose) + _repeat_lines(repeats) + _warning_lines(warnings)
        ),
        dose,
    )


def _sentences(batches: list[Batch]) -> tuple[list[str], set[str]]:
    """All sentences of the batch and the words the topic teaches.

    Target words must be subtracted from the repeat count: an example for
    a word must contain it, and without this every topic word would look
    like a repeat exactly four times.
    """
    texts: list[str] = []
    for note in batches[0].notes:
        # An invariant word has one example copied into three slots —
        # otherwise each of its words would look like a triple repeat.
        texts += dict.fromkeys(
            note["fields"][f"Ex{i}"] for i in (1, 2, 3) if note["fields"].get(f"Ex{i}")
        )
    texts += [note["fields"]["Answer"] for note in batches[2].notes]
    taught = {
        note["fields"]["Form1"].strip().lower() for note in batches[0].notes
    }
    return texts, taught


def _typo_lines(typos: list[tuple[str, str]]) -> list[str]:
    if not typos:
        return []
    return ["", "⚠ not in the dictionary — likely typos, fix before writing:"] + [
        f"  {word} — '{text}'" for word, text in typos
    ]


def _repeat_lines(repeats: list) -> list[str]:
    if not repeats:
        return []
    return [""] + format_repeats(repeats, with_topic=True)


def _dose_lines(dose: Dose) -> list[str]:
    """The dose line isn't decoration — it's the only way to see that
    deck-03 sentences were again built entirely from known words."""
    if not dose.counted:
        return []
    got = ", ".join(dose.new) if dose.new else "none"
    mark = " ⚠ short" if dose.short else " ⚠ over" if dose.over else ""
    lines = [
        "",
        f"deck-03 new words: {len(dose.new)} of {dose.expected} expected"
        f"{mark} — {got}",
    ]
    if dose.prior_words:
        lines.append(
            f"  this topic in the last {DOSE_WINDOW_HOURS}h: {dose.prior_words} "
            f"words, new words already placed: "
            + (", ".join(dose.prior_new) or "none")
        )
    if dose.over:
        lines.append(
            f"  the dose is one new word per {NEW_EVERY} words of the topic, "
            f"{NEW_MAX} at most per filling — counted across calls, so "
            "take the extra ones out of the deck-03 sentences"
        )
    if dose.half:
        lines.append(
            "  half-known (met only inside examples, do not count): "
            + ", ".join(dose.half)
        )
    for et in dose.crowded:
        lines.append(f"  ⚠ two new words in one sentence: '{et}' — one at most")
    if dose.short:
        lines.append(
            "  a deck-03 sentence built only from known words teaches nothing "
            "beyond its target word; put one ordinary A2 word into every "
            f"{NEW_EVERY}rd of them, everything else around it known"
        )
    return lines


def _warning_lines(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    return ["", "warnings (did not block the write):"] + [f"  {w}" for w in warnings]
