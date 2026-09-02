---
name: anki-topic-cards
description: Add Estonian A2 vocabulary straight into Anki via the anki-vocab MCP server, across all three subdecks of a topic at once (01. sõnad, 02. sõnad typein, 03. lause typein) — no CSV, and no need to browse or read existing cards first to relearn the format (it's fully specified in this skill). Example sentences are grounded in the learner's own vocabulary through the anki-vocab MCP server, so new cards reuse words they already know instead of introducing unfamiliar ones. Use whenever the user sends/pastes a list of Estonian words (with or without Russian translations, in dictionary form or as inflected forms met while reading) and asks to add them to an Anki topic deck, e.g. "добавь эти слова в анки через мцп", "закинь слова в тему ...", or names a deck like kursuse eesti keel::A2::09. Vaba aeg. Do NOT use this for CSV-file export requests (use anki-cards / anki-cards-simple / anki-verb-typein instead) or for audio-card requests (use anki-audio-cards instead).
---

# Anki Topic Cards (direct MCP, 3 subdecks)

Captures the exact workflow already validated on real topics (07. Aias, 08. Loomad, 09. Vaba aeg, …). Everything about the format below is already known — don't spend tool calls re-deriving it from old cards. To find out what a topic already contains, use `anki-vocab:topic_state`; to find out whether a word is already in the collection, use `anki-vocab:vocab_check`; to find a deck whose exact name you don't remember, `anki-vocab:deck_find`.

## One MCP server

Everything goes through `anki-vocab`: `vocab_check`, `vocab_pool`, `text_check`, `word_forms`, `topic_state`, `deck_find` for reading, `topic_write` for writing.

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

**Before composing anything**, load the palette with one call:

```
anki-vocab:vocab_pool(level="a2", topics=<topic words>)
```

It returns the first field of the learner's `01. sõnad` cards — dictionary forms: nominative for nouns, ma-infinitive for verbs, the word itself for invariant ones.

**Take the words, not the forms.** Inflect each one as the sentence requires — `sall` in the palette becomes `Ta pani kaela sinise salli`, `soe` becomes `sooja mütsi`. Coverage is measured by lemma, so every form counts. Copying palette entries unchanged into sentences produces stilted Estonian and is not what grounding means.

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

### Rotation: reach for the words you haven't used lately

`vocab_pool` ends with a **not used lately** line — words the learner knows well but which haven't appeared in a new card for two weeks. Use it:

- When you need a **content word** — a noun, an adjective, a specific verb — take it from that line before reaching elsewhere in the palette. `järv`, `külaline`, `koristama` beat inventing another sentence about `koer` and `maja`.
- **Do not apply this to structural words.** `olema`, `meeldima`, `tegema`, `minema`, `tahtma` are missing from that line precisely because every sentence needs them. Rotating them out produces contorted Estonian, which defeats the point.
- The line is a preference, not a filter: the full vocabulary sits above it, and nothing is hidden.

The loop closes on its own. Example sentences are indexed, so a word you use today stops being "stale" and drops off the hint for the next two weeks — the next topic gets a different suggestion list without anyone tracking anything.

**After drafting**, validate every sentence you wrote — Ex1/Ex2/Ex3 of deck 01 and the deck-03 sentence — with one batched call:

```
anki-vocab:text_check(sentences=[...], targets=[<the words being taught>])
```

Always pass `targets`: those words are new by definition and must not count against coverage. Rewrite whatever comes back `low`, leaning on the palette, then check again. **Two rounds maximum** — then report what is left rather than grinding further.

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
- The new words are whatever the last `text_check` listed after `new words` — that line, and only that line, goes into the final report under «кандидаты в следующую тему». Never add words you _think_ are new: `text_check` checks the whole collection, you see one filtered slice of it, and a word you'd guess at may already have 75 cards.

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

1. **Gather inputs.** The word list (Estonian; may include inflected forms; translations optional) and the target topic (a full deck path, or just a bare topic name/number against the default course root). If proceeding on an ambiguous topic name, state the assumed full path rather than asking, unless the ambiguity is severe.

2. **Dedupe.** One `anki-vocab:vocab_check(words=[...])` call on the whole incoming list. It matches any inflected form, so `kübarat` finds `kübar`.

   Words that come back `✓` or `~` are already in the collection. **Set them aside — do not create cards for them in this pass, and do not silently drop them either.** Keep the word, the topic(s) it already lives in, and its existing forms; they all go into the duplicate list in the final report (step 8).

   For words you do keep, `anki-vocab:word_forms(word=...)` returns the forms used on the existing card, so a re-add stays consistent instead of inventing a second variant.

3. **Check the topic.** One `anki-vocab:topic_state(topic="09. Vaba aeg")` call reports which of the three subdecks exist, how many notes each holds, and which words are already there. Missing subdecks are not your problem — `topic_write` creates whatever is absent. If you are unsure the course path itself is spelled right, `anki-vocab:deck_find(pattern="vaba")`.

4. **Load the palette.** `anki-vocab:vocab_pool(level="a2", topics=<topic words>)` — once per topic. **Don't pass `limit`.** The default returns every dictionary word of that level (~630 words, ~2200 tokens); any smaller number silently hides more than half of it, and always the same half.

   The palette is narrower than what the learner knows, by design: it holds only words that have a card in `01. sõnad`. `level="a2"` hides their A1 words, and words met only inside example sentences or grammar drills (`jooma`, `aitama`, `helistama`) have no dictionary card and never appear. All of those still count as known — coverage is measured against the whole collection. So **absence from the palette proves nothing**: it is a source of words to compose from, never evidence that a word is new. Only `vocab_check` and `text_check` answer that.

5. **Per word**: identify lemma + POS, derive the 3 forms from the table above, and write the example sentences/translations per the grammar and A2-simplicity rules, **composing from the palette**.

6. **Validate.** One batched `anki-vocab:text_check(sentences=[all sentences you wrote], targets=[the new words])`. Rewrite anything marked `low` and check again. Two rounds maximum.

7. **Write the topic** with one `anki-vocab:topic_write` call:

   ```
   anki-vocab:topic_write(topic="09. Vaba aeg", words=[<one entry per word>])
   ```

   `course` defaults to `kursuse eesti keel::A2`; pass it only when the user names another root. Up to 33 words per call — split a longer list across calls.

   Validation runs before anything is written: if an entry is malformed the call creates **no notes at all** and returns every complaint at once. Fix them all in one pass and call again. Duplicate filtering is not its job — step 2 already did that properly.

   If the call reports an error mid-write, some subdecks may already hold notes. Say so plainly in the report: **Ctrl+Z in Anki undoes the batch**.

8. **Report back in Russian**, concisely. The `anki-vocab` tools answer in English to save tokens — that is between you and them; what reaches the user is Russian, including the words you quote from `new words` and `low` lines.
   - how many notes went into each deck;
   - average coverage from `text_check`, and any sentence still marked `low` with the reason;
   - **кандидаты в следующую тему** — copy the `new words` line from the last `text_check` verbatim. If it listed nothing, write «нет». Don't extend the list from your own sense of what's new, and don't drop anything from it;
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
  targets=["aerutama"])

1   100%  —    ok
2   100%  —    ok
3   100%  —    ok
average coverage 100%
```

The same word written without the palette scores 40% and drags in `kärestikuline`, `nautima`, `vaikus` — three unfamiliar words around the one being taught. That sentence is unlearnable, which is the whole reason this step exists.
