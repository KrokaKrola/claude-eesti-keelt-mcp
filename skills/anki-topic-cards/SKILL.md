---
name: anki-topic-cards
description: Add Estonian A2 vocabulary straight into Anki via the anki-vocab MCP server, across all three subdecks of a topic at once (01. sõnad, 02. sõnad typein, 03. lause typein) — no CSV, and no need to browse or read existing cards first to relearn the format (it's fully specified in this skill). Example sentences are grounded in the learner's own vocabulary through the anki-vocab MCP server, so new cards reuse words they already know instead of introducing unfamiliar ones. Use whenever the user sends/pastes a list of Estonian words (with or without Russian translations, in dictionary form or as inflected forms met while reading) and asks to add them to an Anki topic deck, e.g. "добавь эти слова в анки через мцп", "закинь слова в тему ...", or names a deck like kursuse eesti keel::A2::09. Vaba aeg. Do NOT use this for CSV-file export requests (use anki-cards / anki-cards-simple / anki-verb-typein instead) or for audio-card requests (use anki-audio-cards instead).
---

# Anki Topic Cards (direct MCP, 3 subdecks)

Captures the exact workflow already validated on real topics (07. Aias, 08. Loomad, 09. Vaba aeg, …). Everything about the format below is already known — don't spend tool calls re-deriving it from old cards. To find out what a topic already contains, use `anki-vocab:topic_state`; to find out whether a word is already in the collection, use `anki-vocab:vocab_check`; to find a deck whose exact name you don't remember, `anki-vocab:deck_find`; if the user has no word list of their own, `anki-vocab:vocab_debt`.

## One MCP server

Everything goes through `anki-vocab`: `vocab_check`, `vocab_pool`, `text_check`, `word_forms`, `topic_state`, `deck_find`, `vocab_debt` for reading, `topic_write` and `vocab_debt_skip` for writing.

Reading works off a local index built from a copy of the collection, so it needs nothing running. Writing is passed on to the Anki add-on behind the scenes, so **`topic_write` needs Anki open** — if it isn't, the call comes back saying so and nothing is written.

## Deck layout

```
<course path>::<NN. Topic>
├── 01. sõnad         (model: Estonian 3 Forms)
├── 02. sõnad typein  (model: Estonian Word Forms with TypeIn)
└── 03. lause typein  (model: Estonian Word Forms with TypeIn)
```

Default course path (unless the user gives another): `kursuse eesti keel::A2`. If the user only names a topic ("Vaba aeg", "09"), assume this default root and these three subdeck names verbatim.

## What you supply per word

`topic_write` takes one entry per word and builds all five notes from it. Note types, field names, form labels, note order, tags and deck creation are the server's job — you never write them out:

```
{"word": "jõgi", "pos": "существительное", "ru": "река",
 "forms": ["Jõgi", "jõe", "jõge"],
 "ex": [["Jõgi on suvel väga soe.", "Река летом очень тёплая."],
        ["Jõe kaldal on ilus jalutada.", "Приятно гулять по берегу реки."],
        ["Me ületasime paadiga jõge.", "Мы переправлялись на лодке через реку."]],
 "sentence": ["Meile meeldib jõe ääres kalastada.", "Нам нравится рыбачить у реки."]}
```

- `forms` — the three canonical forms by part of speech (table below). `Form1` may be capitalized (dictionary style), the other two lowercase. An invariant word takes a single form and a single `ex` pair.
- `ex` — example N shows form N: `ex[0]` uses form 1, `ex[1]` form 2, `ex[2]` form 3. These land on the deck-01 card and are reused on the matching deck-02 notes.
- `sentence` — a brand-new sentence for deck 03, different wording from `ex`, normal capitalization.

The server checks the forms actually appear in their examples and warns (by lemma, so `algama` in «Kool algab kell kaheksa» passes). A warning doesn't block the write; read it and decide.

**Tags are derived, not supplied:** level from the course path, topic words from the topic name (`"09. Vaba aeg"` → `aeg`, `vaba`), part of speech from `pos`, `lause` on deck 03. These are what `vocab_pool` filters on later.

Older topics in this collection carry the topic as a **single underscored tag** instead (`kodused_tööd`, `tule_mulle_külla`). `vocab_pool` matches both conventions — it compares tags word by word — so don't retag old notes to "fix" them.

## Part of speech → the 3 canonical forms

| POS                                                                                                                   | Form1 (label)                                                                                      | Form2 (label)                              | Form3 (label)                                           |
| --------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ------------------------------------------ | ------------------------------------------------------- |
| Noun                                                                                                                  | nominative — `1-я форма (номинатив)`                                                               | genitive — `2-я форма (генитив)`           | partitive — `3-я форма (партитив)`                      |
| Adjective                                                                                                             | same labels as noun                                                                                |                                            |                                                         |
| Declinable pronoun (`keegi→kellegi→kedagi`, `ükski→ühegi→ühtegi`, …)                                                  | same labels as noun                                                                                |                                            |                                                         |
| Verb                                                                                                                  | ma-infinitive — `1-я форма (ma-инфинитив)`                                                         | da-infinitive — `2-я форма (da-инфинитив)` | mina-vorm, 1st p. sg. present — `3-я форма (mina-vorm)` |
| Invariant word — most adverbs (edasi, otsas, ringi…) and a handful of indeclinable pronouns/determiners (kogu, sama…) | same word repeated for Form1/2/3 in deck 01; **deck 02 gets a single note** with `Form` left empty |                                            |                                                         |

If the word the user sends is an **inflected form** (e.g. they copied "Erinevaid", "Viskab", "Valas", "Suled", "Ühtegi" straight out of a text), first work out the dictionary lemma and its POS, then derive the 3 canonical forms from that lemma — don't just repeat the inflected form across all three slots. Translate the **lemma** to Russian (its base meaning), not necessarily the narrower sense of the one inflected form the user happened to encounter. If a word arrives with no translation at all, work it out yourself and flag it in the final report rather than silently guessing forever without mentioning it.

## Vocabulary grounding (mandatory)

The learner's problem is that invented example sentences pile up unfamiliar words, which makes the cards hard to study. Every example you write must lean on words they already have.

One exception, and it is deliberate: the deck-03 dose below. Grounding decides what the *other* words in a sentence are, not whether a sentence may carry a new one — read **Introducing new words on purpose** before you take this section as an absolute.

**Before composing anything**, load the palette with one call:

```
anki-vocab:vocab_pool(level="a2", topics=<topic words>)
```

It returns the first field of the learner's `01. sõnad` cards — dictionary forms: nominative for nouns, ma-infinitive for verbs, the word itself for invariant ones.

**Take the words, not the forms.** Inflect each one as the sentence requires — `sall` in the palette becomes `Tema sall on villane ja pikk`, `soe` becomes `sooja mütsi`. Coverage is measured by lemma, so every form counts. Copying palette entries unchanged into sentences produces stilted Estonian and is not what grounding means.

### When the vocabulary outgrows the palette

The palette exists because you cannot guess what a beginner knows. That
value falls as the learner's vocabulary grows, while its token cost rises
linearly (~240 new words a month here). Measured on this collection:
plain everyday A2 sentences written _without_ the palette already score
100% coverage — what fails the check is ornate vocabulary, not ordinary
sentences.

So when `vocab_pool` starts returning more than roughly 1500 words, stop
asking for the whole thing. Keep the two parts that don't grow:

- `vocab_pool(level=…, topics=…, pos="глагол")` — the topical group plus
  verbs (~180 words here). Note that `pos` also drops the ~55 word cards
  that carry no part-of-speech tag, so use it to narrow a huge palette,
  never to decide what the learner knows;
- the **not used lately** line, which is a fixed 40 words whatever
  the vocabulary size.

Then rely on `text_check` as the actual guarantee — it validates against
the entire vocabulary regardless of what the palette showed you. The
palette is a convenience; the check is the contract.

### Two hint lines: reach for weak words first, then for unused ones

`vocab_pool` ends with two extra lines, and they answer different questions.

**`forgotten in reviews`** — words the learner keeps failing in Anki and has
not carried to a mature interval (21 days). This is the memory signal, and it
outranks everything else here: a sentence built around such a word is an extra
encounter exactly where recall is breaking down, and it costs nothing, since
the word is known by definition. The line is ordered hardest first — take from
the top.

**`not used lately`** — words the learner knows well but which haven't appeared
in a new card for two weeks. This is a variety signal, not a memory one: it
keeps every topic from being another sentence about `koer` and `maja`.

So when you need a **content word** — a noun, an adjective, a specific verb —
try `forgotten in reviews` first, `not used lately` second, and the rest of the
palette after that.

- **Do not apply either line to structural words.** `olema`, `meeldima`,
  `tegema`, `minema`, `tahtma` are missing from them precisely because every
  sentence needs them. Rotating them out produces contorted Estonian, which
  defeats the point.
- Both lines are a preference, not a filter: the full vocabulary sits above
  them, and nothing is hidden.
- Don't force it. One weak word per sentence, where it fits naturally, is the
  whole idea; three of them crammed together gives an unreadable sentence,
  and an unreadable sentence teaches nothing.

The variety loop closes on its own. Example sentences are indexed, so a word
you use today stops being "stale" and drops off that hint for the next two
weeks. The weak line closes differently and more slowly: it clears when the
learner's own reviews start going well, which is the point of putting the word
back in front of them.

**After drafting**, validate every sentence you wrote — Ex1/Ex2/Ex3 of deck 01 and the deck-03 sentence — with one batched call:

```
anki-vocab:text_check(sentences=[...], targets=[<the words being taught>],
                      topic="12. Poes")
```

A deck-03 sentence carrying one deliberate new word comes back `ok` — one new
word is always within the allowance — and that word shows up in the `new words`
line. That line is how you verify the dose (see **Introducing new words on
purpose** below) was actually kept: count it before writing.

Always pass `targets`: those words are new by definition and must not count against coverage. Always pass `topic` too — without it the check sees only the sentences in front of it and cannot tell you that this is the eleventh time this topic reaches for the same verb. Rewrite whatever comes back `low`, leaning on the palette, then check again. **Two rounds maximum** — then report what is left rather than grinding further.

### The two monotony lines

Coverage cannot see monotony: ten sentences built on the same verb score a
perfect 100%, and that is exactly how `12. Poes` ended up with `panema` in ten
of its sentences and `uus` in seventy-six. So `text_check` also counts, by
lemma, how often your sentences reuse the same word — `panin`, `pani` and
`panime` are one word — and answers on two separate lines:

```
leaning too hard on: panema ×3 (+20 already in the topic)
already heavy in this topic: uus (76) · suur (35) · müüja (28)
```

**`leaning too hard on` is about the batch in front of you, and it is a
rewrite.** Treat it like `low`: those sentences read as variations on one word,
so recast some of them around another before writing. It is not cosmetic — a
topic where every third card is another `Ma panin X-i Y-sse` teaches one
construction eleven times instead of eleven things.

**`already heavy in this topic` is a preference.** These are the words the
topic has been leaning on all along, counted across everything written there.
It will show up on almost every call into a mature topic, and that is fine —
it is not asking you to contort a sentence, only to take the other word where
the swap is free. That is what the palette's `forgotten in reviews` (first) and
`not used lately` (second) lines are for.

Two things are deliberately absent from both lines, so anything listed really
does have an alternative:

- **structural verbs** (`olema`, `minema`, `tegema`, `saama`, `tahtma`,
  `meeldima`, `pidama`, `võima`, `käima`, `hakkama`) — every sentence needs
  them and rotating them produces contorted Estonian;
- **the topic's own words** — `pood` in `12. Poes` is supposed to be frequent.
  They are exempt from the topic count, not from the batch count: the topic
  teaches a word, it does not license building the whole set on it.

`topic_write` prints the same two lines, counted over everything the call is
about to write — deck-01 examples included. Neither blocks the write, and
`dry_run` shows both beforehand. If `leaning too hard on` appears there after
you thought the drafts were clean, the honest move is to fix and rewrite, not
to report and move on.

### If `anki-vocab` is unavailable

The server reads a local index and needs nothing running, but it can still be missing (not registered, failed to start). If its tools are absent or a call errors out, **stop and ask before doing anything else**:

> «Сервер `anki-vocab` недоступен, поэтому я не смогу ни отсеять дубли, ни проверить покрытие. Делать карточки без этого или сначала разберёмся с сервером?»

Don't retry in a loop, and don't create any cards until the user answers. Cards built without the check are exactly the cards this skill exists to avoid, so that is the user's call, not a fallback you take on your own.

If the user says to go ahead: compose from your own knowledge of the topic, skip `vocab_check` and `text_check`, and repeat at the top of the report that coverage was not checked and duplicates were not filtered. Note that without the server there is no way to write the cards either — say so instead of looking for another route.

**`topic_write` failing is a different thing.** Reading works with Anki closed, writing does not. If the call answers «Anki не запущен» or «не достучался до аддона», the drafted cards are intact and nothing was written: ask the user to open Anki, then call again with the same words.

### Reading the marks

`vocab_check` and `text_check` use three marks:

- `✓` — the word is on a word card. Most of these are in the palette; a word whose only card is a `02. sõnad typein` form is not.
- `~` — the word has only ever appeared inside an example sentence. Counts as known, never offered in the palette.
- `✗` — not in the collection at all.

A `✓` line also shows, in square brackets, the topic(s) the word already lives in and **what kind of record sits there**:

- `3F` — a full three-forms card (deck `01. sõnad`). This is a real duplicate.
- `ty` — only a single-form typein note (deck `02`) or an old-format drill. The word has no proper dictionary card yet.
- `ex` — the word merely turned up inside somebody else's example sentence. Not a card at all.

Each topic carries its strongest mark only, and topics with `3F` come first, so a truncated list never hides the duplicate signal. The line is capped to stay short — at most 3 tags, 4 forms and 2 topics; beyond that the brackets end with `+N more`, and the duplicate report should say so rather than pretend the list is complete:

```
korvpall ✓ a2 aeg,elu,liikumine  korvpall/korvpalli/korvpalliga  [09. Vaba aeg 3F; 10. Liikumine on elu 3F]
ujuma    ✓ a2 aeg,vaba  ujub/ujuda/ujuma/ujumas  [09. Vaba aeg 3F; 08. Loomad ex; +1 more]
pidama   ✓ lihtminevik  peab/peame/pean  [01. sõnad typein ty; 04. kodused tööd ex; +7 more]
```

`korvpall` and `ujuma` already have proper cards; `pidama` does not — it only ever got a typein note, so a three-forms card for it would be new material, not a duplicate.

That distinction drives the duplicate report below, together with location: a word with `3F` in the topic being filled is a true duplicate, the same word with `3F` in another topic may still be worth adding here, and `ty`/`ex` mean the word has no dictionary card at all.

## Vocabulary target (soft)

Prefer words the learner already knows — that is what `vocab_pool` is for, not a hard quota.

- The card's target word never counts; pass it in `targets`.
- **One further new word per sentence is always fine**, whatever the coverage.
- Beyond that, keep coverage at or above 70%. `text_check` marks anything lower as `low`.
- **Never contort a sentence to hit the number.** A natural A2 sentence with two new words beats an awkward one with none. The target is a preference, not a constraint.
- The new words you **report** are whatever the last `text_check` listed after `new words` — that line, and only that line, goes into the final report under «кандидаты в следующую тему». Never extend it from your own sense of what's new: `text_check` checks the whole collection, you see one filtered slice of it, and a word you'd guess at may already have 75 cards.

  This is a rule about the report, not about composing. Guessing a word might be new and then **verifying it with `vocab_check`** is exactly right — that is how the deck-03 dose below is assembled.

## Introducing new words on purpose (the deck-03 dose)

Grounding has a failure mode of its own: sentences built strictly from words
the learner already has teach nothing beyond the target word. Measured on this
collection, the topic `14. Riidepoes` introduced **2** new words across 65
notes, and `11. Lähme välja` introduced none at all. At that rate the learner's
vocabulary only ever grows by the words they explicitly ask for, and every
topic reads as a recombination of the previous ones.

So put new words into the **deck-03 sentence** — the one the learner translates
from Russian. Yes, that means the first review of that card may well be a
miss: the learner knows this, has chosen it deliberately, and the dose below is
what keeps it from becoming frustrating. Do not argue the point in the report.

### The dose

```
NEW_EVERY = 3     one new word in roughly every third deck-03 sentence
NEW_MAX   = 5     never more than this many in one topic write
```

These two numbers live in `src/anki_vocab/config.py` — that is the only copy.
`topic_write` counts against them and says so in its report:

```
deck-03 new words: 1 of 4 expected ⚠ short — ümbrik
  half-known (met only inside examples, do not count): sall
```

**That line is the check, and `text_check` cannot replace it.** `text_check`
receives a flat list of sentences and has no idea which of them are deck 03,
so it can never tell you the dose was missed. `topic_write` sees the deck-03
sentence as its own field, so it can. Run it with `dry_run=true` to read the
line before anything is written, and fix a `⚠ short` then.

- **One new word per sentence, never two.** Two unknowns in a sentence the
  learner has to produce is exactly the friction being avoided. `topic_write`
  flags any sentence that breaks this.
- Spread them across the topic rather than bunching them at the front.
- A small topic (3–4 words) gets one, not `NEW_MAX`.
- The rest of the deck-03 sentences stay fully grounded, as before.
- Words already being taught in this same call never count as new — the
  counter excludes them for you.
- **`NEW_MAX` is per call, and the server cannot see across calls.** A topic
  split into two `topic_write` calls would be allowed `NEW_MAX` twice — keep
  the total for the whole topic under `NEW_MAX` yourself.
- If the topic genuinely has no suitable new word left — everything ordinary
  that fits is already known — leave it short and say so in the report in one
  line. A forced ornate word is worse than a missed dose.

**Deck 01 and 02 are unchanged.** Their examples stay grounded under the usual
soft rule — an occasional new word is tolerated there, but do not place one on
purpose. The deliberate dose lives in deck 03 only.

### Where the new words come from

This is the step that gets skipped, and skipping it is why the dose quietly
came out at zero. `vocab_pool` cannot help here — by construction it returns
only what the learner **already knows**. Nothing hands you unknown words, so
you have to propose them and then verify:

1. Before composing deck 03, write down `NEW_EVERY`-worth of candidates from
   your own knowledge of the topic — ordinary A2 words that fit it.
2. Put them through `anki-vocab:vocab_check(words=[...])` in the same call you
   are already making, or a second one. Ask for a few more than you need.
3. The ones that come back `✗` are your dose. `~` also works and is a softer
   pick — the learner met the word once or twice inside somebody's example but
   never studied it; `topic_write` reports those separately and does not count
   them. It only lists the genuinely rare ones: the A1 branch is outside the
   index, so common words like `ema` or `täna` carry a `~` too and would
   otherwise drown the line.
4. `✓` means it is not new. Drop it and take the next candidate.

**Choosing among them.** The word has to earn its place: ordinary A2
vocabulary that fits the topic, in a sentence where *everything else is
known*, so the Russian side and the context carry the meaning. `sall` in a
sentence about winter clothes is a good pick; an ornate verb the learner will
not meet again for a year is not.

**Report every one of them** — the word, its translation, and the sentence it
went into, under «новые слова в предложениях». The learner decides from that
list whether a word deserves cards of its own in decks 01/02; never create
those cards on your own initiative.

The loop closes either way: a word used in a sentence is indexed from it, so it
stops counting as unknown, and later surfaces in `vocab_debt` as a candidate
for a proper card.

## Grammar rules to keep applying (condensed; full detail lives in project memory)

- Total vs. partial object: bounded/completed action → genitive singular (or nom. plural) object; ongoing/indefinite/negated action → partitive.
- `meeldima`: the liked thing stays nominative; the verb agrees with it, not with the person.
- Adessive `-l` for possession (`X-l on Y`) and for time-of-day / day-name expressions.
- Negation (`ei`) sits directly before the verb and forces partitive on the object; negative indefinites (`keegi, midagi, kunagi, ükski`) follow the verb.
- `pidama` takes the **ma**-infinitive (exception among modal-like verbs).
- Genitive + noun for color/material/type compounds (`erineva värvi sokid`, `puu laud`…).
- Postpositions (`jaoks, ääres, järel`…) govern genitive; `pärast`/`enne` used _before_ the noun (prepositionally) in a temporal sense take **partitive**, not genitive (`pärast tööd`, `enne lõunat`).
- If a form or usage feels genuinely uncertain (rare compound, unusual gradation, an idiom you're not sure of), check it against Sõnaveeb/EKI with `web_search` before committing to it — don't silently guess on something you're not confident about.

## A2 sentence-simplicity rule (mandatory — see memory item on this)

Keep every example a simple, everyday A2 sentence. Avoid:

- gerund/nominalization subjects (`"...tegemine võtab aega"`, `"...leidmine on..."`)
- passive/impersonal voice (`"...-akse/-takse"`)
- infinitive chains (`"osata ... pöörata"`, `"...miseks on vaja..."`)

What's genuinely fine (don't avoid this — it's not what makes a sentence complex): varied **case endings** for flavor and variety — comitative `-ga`, allative `-le`, essive `-na`, translative `-ks`, elative `-st`, and so on. A simple one-clause sentence built around `Ta läks sõbraga kinno` (comitative) or `Ta töötab õpetajana` (essive) is exactly the kind of A2-appropriate variety to aim for in deck 03 — the goal is one simple clause, not "no case endings." Only the three constructions listed above (nominalized subjects, passive/impersonal voice, infinitive chains) should actually be avoided.

Prefer simple SVO, `meeldima` + nominative, genitive+noun, adessive time/possession, and at most one da-/ma-infinitive per clause — but freely vary the case of the object/complement across sentences (partitive, allative, comitative, essive, translative, elative…) so deck 03 doesn't read as monotonous. No duplicate sentences within a deck, and vary the wording between the deck 01/02 examples and the deck 03 sentence for the same word (don't just copy one into the other).

Simplicity and grounding pull the same way: short everyday clauses built from known words are exactly what scores well on `text_check`.

## Workflow

1. **Gather inputs.** The word list (Estonian; may include inflected forms; translations optional) — or, if the user has none, `anki-vocab:vocab_debt()` and let them pick — and the target topic (a full deck path, or just a bare topic name/number against the default course root). If proceeding on an ambiguous topic name, state the assumed full path rather than asking, unless the ambiguity is severe.

2. **Dedupe.** One `anki-vocab:vocab_check(words=[...])` call on the whole incoming list. It matches any inflected form, so `kübarat` finds `kübar`.

   Words that come back `✓` or `~` are already in the collection. **Set them aside — do not create cards for them in this pass, and do not silently drop them either.** Keep the word, the topic(s) it already lives in, and its existing forms; they all go into the duplicate list in the final report (step 9).

   For words you do keep, `anki-vocab:word_forms(word=...)` returns the forms used on the existing card, so a re-add stays consistent instead of inventing a second variant.

3. **Check the topic.** One `anki-vocab:topic_state(topic="09. Vaba aeg")` call reports which of the three subdecks exist, how many notes each holds, and which words are already there. Missing subdecks are not your problem — `topic_write` creates whatever is absent. If you are unsure the course path itself is spelled right, `anki-vocab:deck_find(pattern="vaba")`.

4. **Load the palette.** `anki-vocab:vocab_pool(level="a2", topics=<topic words>)` — once per topic. **Don't pass `limit`.** The default returns every dictionary word of that level (~630 words, ~2200 tokens); any smaller number silently hides more than half of it, and always the same half.

   The palette is narrower than what the learner knows, by design: it holds only words that have a card in `01. sõnad`. `level="a2"` hides their A1 words, and words met only inside example sentences or grammar drills (`jooma`, `aitama`, `helistama`) have no dictionary card and never appear. All of those still count as known — coverage is measured against the whole collection. So **absence from the palette proves nothing**: it is a source of words to compose from, never evidence that a word is new. Only `vocab_check` and `text_check` answer that.

5. **Pick the dose.** Decide how many new words this topic gets — one per
   `NEW_EVERY` words, capped at `NEW_MAX` — then name that many candidates
   plus a couple of spares from your own knowledge of the topic, and check
   them: `anki-vocab:vocab_check(words=[<candidates>])`. The `✗` ones are the
   dose, `~` ones are a softer alternative, `✓` ones are dropped. **Do this
   before composing, not after**: retrofitting a new word into a finished
   sentence is what turns into "no new words this time".

6. **Per word**: identify lemma + POS, derive the 3 forms from the table above, and write the example sentences/translations per the grammar and A2-simplicity rules, **composing from the palette** — placing one dose word in every `NEW_EVERY`-th deck-03 sentence, and nothing new anywhere else on purpose.

7. **Validate.** One batched `anki-vocab:text_check(sentences=[all sentences you wrote], targets=[the words being taught], topic=<the topic>)`. Rewrite anything marked `low` and check again — but keep the deliberate dose words, they are the point. Two rounds maximum. Rewrite whatever shows up on `leaning too hard on` as well: that one is not about a single sentence but about the set reading as variations on one word. `already heavy in this topic` is a preference — follow it where the swap is free.

8. **Write the topic** with one `anki-vocab:topic_write` call:

   ```
   anki-vocab:topic_write(topic="09. Vaba aeg", words=[<one entry per word>])
   ```

   `course` defaults to `kursuse eesti keel::A2`; pass it only when the user names another root. Up to 33 words per call — split a longer list across calls.

   Validation runs before anything is written: if an entry is malformed the call creates **no notes at all** and returns every complaint at once. Fix them all in one pass and call again. Duplicate filtering is not its job — step 2 already did that properly.

   If the call reports an error mid-write, some subdecks may already hold notes. Say so plainly in the report: **Ctrl+Z in Anki undoes the batch**.

   **Read the `deck-03 new words:` line it returns.** If it says `⚠ short`,
   the dose did not happen — that is the one signal nothing else gives you.
   On a topic where you are unsure, call with `dry_run=true` first and read
   the line before writing for real.

9. **Report back in Russian**, concisely. The `anki-vocab` tools answer in English to save tokens — that is between you and them; what reaches the user is Russian, including the words you quote from `new words` and `low` lines.
   - how many notes went into each deck;
   - average coverage from `text_check`, and any sentence still marked `low` with the reason;
   - anything still on `leaning too hard on` after the rewrites, and why it stayed (`already heavy in this topic` is a preference and does not belong in the report);
   - **новые слова в предложениях** — the words you put into the deck-03 sentences on purpose: word, translation, and the sentence each went into. Close the list by offering to make cards for any of them in decks 01/02 — that is the learner's call, not yours;
   - **кандидаты в следующую тему** — the rest of the `new words` line from the last `text_check`, i.e. whatever turned up beyond the deliberate dose. If it listed nothing, write «нет». Don't extend the list from your own sense of what's new, and don't drop anything from it;
   - **дубли — отдельным списком, готовым к действию.** For every word set aside in step 2, give the word, its Russian meaning, the topic(s) it already lives in, and its existing forms:

     ```
     ### Пропущено как дубли
     | слово | значение | уже есть в теме | что там лежит | формы |
     |---|---|---|---|---|
     | korvpall | баскетбол | 09. Vaba aeg; 10. Liikumine on elu | карточка 3 форм | korvpall/korvpalli/korvpalliga |
     | ujuma | плавать | 09. Vaba aeg; 08. Loomad; +1 more | карточка 3 форм / только в примере | ujub/ujuda/ujuma/ujumas |
     ```

     Close the report by offering to add them anyway — one line, e.g. «Скажи, если всё равно нужно добавить — сделаю карточки и для них.» If the user asks for it, run the full workflow for exactly those words, skipping step 2's filter, and reuse the forms from `word_forms` so the new cards agree with the existing ones. Never add them on your own initiative: a word already in the target topic is usually a real duplicate, while the same word arriving from a different topic may be worth a second card — that is the user's call, not yours.

   - any translation you had to invent because the user didn't supply one;
   - any form you weren't fully sure of and double-checked online.

## Worked example (for calibration only — write fresh sentences, don't reuse these verbatim)

A regular noun and an invariant word, as `topic_write` wants them:

```
{"word": "jõgi", "pos": "существительное", "ru": "река",
 "forms": ["Jõgi", "jõe", "jõge"],
 "ex": [["Jõgi on suvel väga soe.", "Река летом очень тёплая."],
        ["Jõe kaldal on ilus jalutada.", "Приятно гулять по берегу реки."],
        ["Me ületasime paadiga jõge.", "Мы переправлялись на лодке через реку."]],
 "sentence": ["Meile meeldib suvel jõe ääres kalastada.",
              "Нам нравится летом рыбачить у реки."]}

{"word": "kogu", "pos": "местоимение", "ru": "весь, вся, всё",
 "forms": ["Kogu"],
 "ex": [["Kogu klass läks ekskursioonile.", "Весь класс поехал на экскурсию."]],
 "sentence": ["Kogu perekond käis nädalavahetusel matkal.",
              "Вся семья ходила в поход на выходных."]}
```

`kogu` doesn't decline, so one form and one example are enough: the server repeats them across deck 01 and gives deck 02 a single note with no form label.

## Worked grounding example

Word **Aerutama** ("грести"), topic `09. Vaba aeg`, palette loaded.

Drafted and checked:

```
anki-vocab:text_check(
  sentences=["Ma lähen jõele aerutama.",
             "Vabal ajal on tore aerutada.",
             "Ma aerutan igal hommikul jõel."],
  targets=["aerutama"],
  topic="09. Vaba aeg")

1   100%  —    ok
2   100%  —    ok
3   100%  —    ok
average coverage 100%
leaning too hard on: jõgi ×2 (+9 already in the topic)
```

Coverage is perfect and the batch still needs work: two of the three examples
put the same river under the boat. `Vabal ajal on tore aerutada` stays,
`Ma aerutan igal hommikul jõel` becomes `Ma aerutan igal hommikul järvel` —
and `järv` came off the palette's `not used lately` line, so the swap costs
nothing and buys the learner a word they had not seen in a fortnight.

The same word written without the palette scores 40% and drags in `kärestikuline`, `nautima`, `vaikus` — three unfamiliar words around the one being taught. That sentence is unlearnable, which is the whole reason this step exists.
