---
name: anki-topic-cards
description: Add Estonian A2 vocabulary straight into Anki via the Anki MCP server, across all three subdecks of a topic at once (01. sõnad, 02. sõnad typein, 03. lause typein) — no CSV, and no need to browse or read existing cards first to relearn the format (it's fully specified in this skill). Example sentences are grounded in the learner's own vocabulary through the anki-vocab MCP server, so new cards reuse words they already know instead of introducing unfamiliar ones. Use whenever the user sends/pastes a list of Estonian words (with or without Russian translations, in dictionary form or as inflected forms met while reading) and asks to add them to an Anki topic deck, e.g. "добавь эти слова в анки через мцп", "закинь слова в тему ...", or names a deck like kursuse eesti keel::A2::09. Vaba aeg. Do NOT use this for CSV-file export requests (use anki-cards / anki-cards-simple / anki-verb-typein instead) or for audio-card requests (use anki-audio-cards instead).
---

# Anki Topic Cards (direct MCP, 3 subdecks)

Captures the exact workflow already validated on real topics (07. Aias, 08. Loomad, 09. Vaba aeg, …). Everything about the format below is already known — don't spend tool calls re-deriving it by calling `notes_info`/`find_notes` on old cards. To find out what a topic already contains, use `anki-vocab:topic_state`; to find out whether a word is already in the collection, use `anki-vocab:vocab_check`. Reading existing notes with `anki:notes_info` is still the right move when the user asks you to audit or continue a specific topic and needs the actual card contents — the rule above is only against re-deriving the format.

## Two MCP servers, two roles

| Server       | Role                             | Tools used here                                                        |
| ------------ | -------------------------------- | ---------------------------------------------------------------------- |
| `anki-vocab` | reading the learner's vocabulary | `vocab_check`, `vocab_pool`, `text_check`, `word_forms`, `topic_state` |
| `anki`       | writing cards (the Anki add-on)  | `add_notes`, `create_deck`, `change_deck`                              |

`anki-vocab` reads a local index built from a copy of the collection, so it works whether or not Anki is open and it never writes anything. All writing goes through `anki`, which needs Anki running.

## Deck layout

```
<course path>::<NN. Topic>
├── 01. sõnad         (model: Estonian 3 Forms)
├── 02. sõnad typein  (model: Estonian Word Forms with TypeIn)
└── 03. lause typein  (model: Estonian Word Forms with TypeIn)
```

Default course path (unless the user gives another): `kursuse eesti keel::A2`. If the user only names a topic ("Vaba aeg", "09"), assume this default root and these three subdeck names verbatim.

## Models & fields (fill every field, exact names)

**Estonian 3 Forms** (deck 01): `Russian, Form1, Form2, Form3, Ex1, Ex1ru, Ex2, Ex2ru, Ex3, Ex3ru, Add Reverse`

- `Add Reverse` is always `"1"`.
- `Form1` may be capitalized (dictionary style); `Form2`/`Form3` lowercase.
- `Ex1` must contain `Form1`, `Ex2` must contain `Form2`, `Ex3` must contain `Form3` — each example tests exactly the form it sits next to.

**Estonian Word Forms with TypeIn** (decks 02 & 03): `Russian, Form, Answer, Example_ET, Example_RU`

- **Deck 02** — one note per grammatical form (see table below, usually 3 per word). `Answer` = that form, always lowercase. `Example_ET`/`Example_RU` = the matching `Ex`/`Ex..ru` pair reused verbatim from deck 01 (same sentence, since it already targets that exact form). `Form` = the Russian label (e.g. `"1-я форма (номинатив)"`); empty for invariant words.
- **Deck 03** — one note per word. `Answer` = a brand-new full Estonian sentence (normal capitalization, not reused from 01/02), `Russian` = its translation. `Form`/`Example_ET`/`Example_RU` stay empty.

## Tags

Shared tags on every note of the topic: `["A2", <topic words>]`.
`<topic words>` = the subdeck's topic name with the leading number+dot stripped, split into words, lowercased, alphabetically sorted.
Example: topic `"09. Vaba aeg"` → strip `"09. "` → `"Vaba aeg"` → `["aeg", "vaba"]`.

Per-note extra tag:

- Decks 01 & 02: the part of speech, in Russian — `существительное` / `глагол` / `прилагательное` / `наречие` / `местоимение`.
- Deck 03: `lause` (instead of a POS tag), merged alphabetically into the shared list.

Full examples: `["A2","aeg","vaba","существительное"]` (deck 01/02); `["A2","aeg","lause","vaba"]` (deck 03).

These tags are what `vocab_pool` filters on later, so getting them right is what makes the next topic's palette useful.

Older topics in this collection carry the topic as a **single underscored tag** instead (`kodused_tööd`, `tule_mulle_külla`). `vocab_pool` matches both conventions — it compares tags word by word — so don't retag old notes to "fix" them, and keep writing the split-words form on new ones.

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
- the **давно не встречались** line, which is a fixed 40 words whatever
  the vocabulary size.

Then rely on `text_check` as the actual guarantee — it validates against
the entire vocabulary regardless of what the palette showed you. The
palette is a convenience; the check is the contract.

### Rotation: reach for the words you haven't used lately

`vocab_pool` ends with a line **«давно не встречались в новых карточках»** — words the learner knows well but which haven't appeared in a new card for two weeks. Use it:

- When you need a **content word** — a noun, an adjective, a specific verb — take it from that line before reaching elsewhere in the palette. `järv`, `külaline`, `koristama` beat inventing another sentence about `koer` and `maja`.
- **Do not apply this to structural words.** `olema`, `meeldima`, `tegema`, `minema`, `tahtma` are missing from that line precisely because every sentence needs them. Rotating them out produces contorted Estonian, which defeats the point.
- The line is a preference, not a filter: the full vocabulary sits above it, and nothing is hidden.

The loop closes on its own. Example sentences are indexed, so a word you use today stops being "stale" and drops off the hint for the next two weeks — the next topic gets a different suggestion list without anyone tracking anything.

**After drafting**, validate every sentence you wrote — Ex1/Ex2/Ex3 of deck 01 and the deck-03 sentence — with one batched call:

```
anki-vocab:text_check(sentences=[...], targets=[<the words being taught>])
```

Always pass `targets`: those words are new by definition and must not count against coverage. Rewrite whatever comes back `низко`, leaning on the palette, then check again. **Two rounds maximum** — then report what is left rather than grinding further.

### If `anki-vocab` is unavailable

The server reads a local index and needs nothing running, but it can still be missing (not registered, failed to start). If its tools are absent or a call errors out, **stop and ask before doing anything else**:

> «Сервер `anki-vocab` недоступен, поэтому я не смогу ни отсеять дубли, ни проверить покрытие. Делать карточки без этого или сначала разберёмся с сервером?»

Don't retry in a loop, and don't create any cards until the user answers. Cards built without the check are exactly the cards this skill exists to avoid, so that is the user's call, not a fallback you take on your own.

If the user says to go ahead: compose from your own knowledge of the topic, skip `vocab_check` and `text_check`, and repeat at the top of the report that coverage was not checked and duplicates were not filtered.

### Reading the marks

`vocab_check` and `text_check` use three marks:

- `✓` — the word is on a word card. Most of these are in the palette; a word whose only card is a `02. sõnad typein` form is not.
- `~` — the word has only ever appeared inside an example sentence. Counts as known, never offered in the palette.
- `✗` — not in the collection at all.

A `✓` line also shows, in square brackets, the topic(s) the word already lives in. The line is capped to keep it short — at most 3 tags, 4 forms and 2 decks; when a word lives in more topics the brackets end with `ещё N`, and the duplicate report should say so rather than pretend the list is complete:

```
korvpall ✓ a2 aeg,elu,liikumine  korvpall/korvpalli/korvpalliga  [09. Vaba aeg; 10. Liikumine on elu]
ujuma    ✓ a2 aeg,vaba  ujub/ujuda/ujuma/ujumas  [08. Loomad; 09. Vaba aeg; ещё 1]
```

That location matters for the duplicate report below: a word already sitting in the topic being filled is a true duplicate, while the same word coming from a different topic may still be worth adding here.

## Vocabulary target (soft)

Prefer words the learner already knows — that is what `vocab_pool` is for, not a hard quota.

- The card's target word never counts; pass it in `targets`.
- **One further new word per sentence is always fine**, whatever the coverage.
- Beyond that, keep coverage at or above 70%. `text_check` marks anything lower as `низко`.
- **Never contort a sentence to hit the number.** A natural A2 sentence with two new words beats an awkward one with none. The target is a preference, not a constraint.
- The new words are whatever the last `text_check` listed after «новые слова» — that line, and only that line, goes into the final report under «кандидаты в следующую тему». Never add words you _think_ are new: `text_check` checks the whole collection, you see one filtered slice of it, and a word you'd guess at may already have 75 cards.

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

3. **Check the topic.** One `anki-vocab:topic_state(topic="09. Vaba aeg")` call reports which of the three subdecks exist, how many notes each holds, and which words are already there. Don't call `list_decks` or `find_notes`. If the 3 subdecks don't exist yet: `anki:create_deck` only nests 2 levels per call, so for a 4-level path (`course::A2::NN. Topic::01. sõnad`), add the notes to the nearest existing parent deck first, then batch-move them with `card_management → change_deck` to the full path (this auto-creates the missing hierarchy).

4. **Load the palette.** `anki-vocab:vocab_pool(level="a2", topics=<topic words>)` — once per topic. **Don't pass `limit`.** The default returns every dictionary word of that level (~630 words, ~2200 tokens); any smaller number silently hides more than half of it, and always the same half.

   The palette is narrower than what the learner knows, by design: it holds only words that have a card in `01. sõnad`. `level="a2"` hides their A1 words, and words met only inside example sentences or grammar drills (`jooma`, `aitama`, `helistama`) have no dictionary card and never appear. All of those still count as known — coverage is measured against the whole collection. So **absence from the palette proves nothing**: it is a source of words to compose from, never evidence that a word is new. Only `vocab_check` and `text_check` answer that.

5. **Per word**: identify lemma + POS, derive the 3 forms from the table above, and write the example sentences/translations per the grammar and A2-simplicity rules, **composing from the palette**.

6. **Validate.** One batched `anki-vocab:text_check(sentences=[all sentences you wrote], targets=[the new words])`. Rewrite anything marked `низко` and check again. Two rounds maximum.

7. **Batch-create with `anki:add_notes`** (never `add_note` one at a time), one call per subdeck:
   - **01**: one note per word, model `Estonian 3 Forms`, `allow_duplicate: true`.
   - **02**: interleaved **word by word** (word1-form1, word1-form2, word1-form3, word2-form1, …) — never grouped by form across words. Invariant words = 1 note. `allow_duplicate: true`.
   - **03**: one brand-new sentence per word, `allow_duplicate: true`.
     Pass the shared `["A2", <topic words>]` tags via the batch call's `tags` parameter, and the per-note POS/`lause` tag via each note's own `tags` (merges with the shared ones).

   `allow_duplicate: true` stays because step 2 has already done the deduplication properly — the flag only stops Anki's own first-field heuristic from blocking legitimate notes (e.g. three forms of one word).

8. **Report back in Russian**, concisely:
   - how many notes went into each deck;
   - average coverage from `text_check`, and any sentence still marked `низко` with the reason;
   - **кандидаты в следующую тему** — copy the «новые слова» line from the last `text_check` verbatim. If it listed nothing, write «нет». Don't extend the list from your own sense of what's new, and don't drop anything from it;
   - **дубли — отдельным списком, готовым к действию.** For every word set aside in step 2, give the word, its Russian meaning, the topic(s) it already lives in, and its existing forms:

     ```
     ### Пропущено как дубли
     | слово | значение | уже есть в теме | формы |
     |---|---|---|---|
     | korvpall | баскетбол | 09. Vaba aeg; 10. Liikumine on elu | korvpall/korvpalli/korvpalliga |
     | ujuma | плавать | 08. Loomad; 09. Vaba aeg; ещё 1 | ujub/ujuda/ujuma/ujumas |
     ```

     Close the report by offering to add them anyway — one line, e.g. «Скажи, если всё равно нужно добавить — сделаю карточки и для них.» If the user asks for it, run the full workflow for exactly those words, skipping step 2's filter, and reuse the forms from `word_forms` so the new cards agree with the existing ones. Never add them on your own initiative: a word already in the target topic is usually a real duplicate, while the same word arriving from a different topic may be worth a second card — that is the user's call, not yours.

   - any translation you had to invent because the user didn't supply one;
   - any form you weren't fully sure of and double-checked online.

## Worked example (for calibration only — write fresh sentences, don't reuse these verbatim)

Word: **Jõgi** (noun, "река")

- Deck 01 fields: `Form1: Jõgi, Form2: jõe, Form3: jõge`; `Ex1: "Jõgi on suvel väga soe." / Ex1ru: "Река летом очень тёплая."`; `Ex2: "Jõe kaldal on ilus jalutada." / Ex2ru: "Приятно гулять по берегу реки."`; `Ex3: "Me ületasime paadiga jõge." / Ex3ru: "Мы переправлялись на лодке через реку."`; tags `["A2","aeg","vaba","существительное"]`.
- Deck 02: 3 notes — `Answer: jõgi` / `Form: 1-я форма (номинатив)` (+ same Ex1/Ex1ru), `Answer: jõe` / `2-я форма (генитив)` (+ Ex2/Ex2ru), `Answer: jõge` / `3-я форма (партитив)` (+ Ex3/Ex3ru).
- Deck 03: one new note, e.g. `Answer: "Meile meeldib suvel jõe ääres kalastada." / Russian: "Нам нравится летом рыбачить у реки."`, tags `["A2","aeg","lause","vaba"]`.

Word: **Kogu** (invariant pronoun/determiner, "весь, вся, всё")

- Deck 01: `Form1/2/3: Kogu/kogu/kogu`; `Ex1=Ex2=Ex3: "Kogu klass läks ekskursioonile." / "Весь класс поехал на экскурсию."`; tag `местоимение`.
- Deck 02: a single note, `Answer: kogu`, `Form: ""`, same example.
- Deck 03: one new sentence, e.g. `"Kogu perekond käis nädalavahetusel matkal." / "Вся семья ходила в поход на выходных."`.

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
покрытие в среднем 100%
```

The same word written without the palette scores 40% and drags in `kärestikuline`, `nautima`, `vaikus` — three unfamiliar words around the one being taught. That sentence is unlearnable, which is the whole reason this step exists.
