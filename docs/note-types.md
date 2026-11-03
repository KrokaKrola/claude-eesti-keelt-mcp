# Note types and deck layout

`topic_write` writes two note types. Their templates and CSS are in
[`anki/note-types/`](../anki/note-types), and `uv run python -m anki_vocab.setup`
creates them in Anki for you. The names and field order must match exactly, because
the server fills fields by name.

## Estonian 3 Forms

Dictionary card for a word: translation, its forms, and three examples.

| Field | Content | Example |
| --- | --- | --- |
| `Russian` | translation | река |
| `Form1` | nominative / ma-infinitive | jõgi |
| `Form2` | genitive / da-infinitive | jõe |
| `Form3` | partitive / mina-vorm | jõge |
| `Form4` | partitive plural (declinable words only; empty otherwise) | jõgesid |
| `Ex1`, `Ex1ru` | example showing Form1, and its translation | Jõgi on suur. / Река большая. |
| `Ex2`, `Ex2ru` | example showing Form2 | |
| `Ex3`, `Ex3ru` | example showing Form3 | |
| `Add Reverse` | non-empty → also make the ET→RU card | 1 |

Cards: **RU→ET** (translation on the front, forms and examples on the back) and
**ET→RU** (forms on the front; generated only when `Add Reverse` is filled).
`Form4` is hidden when empty.

## Estonian Word Forms with TypeIn

A type-in card: you see the Russian and a hint, and type the Estonian.

| Field | Content |
| --- | --- |
| `Russian` | translation of the word, or a whole Russian sentence |
| `Form` | which form to type, e.g. `2-я форма (генитив)`; empty for sentences |
| `Answer` | the Estonian form or sentence to type |
| `Example_ET`, `Example_RU` | example for that form (optional) |

## What one word turns into

Topic `09. Vaba aeg` in course `kursuse eesti keel::A2`, the word *jõgi*:

| Deck | Note type | Notes |
| --- | --- | --- |
| `…::09. Vaba aeg::01. sõnad` | Estonian 3 Forms | 1 note → 2 cards (RU→ET, ET→RU) |
| `…::09. Vaba aeg::02. sõnad typein` | Estonian Word Forms with TypeIn | 1 note per form: 4 for a declinable word, 3 for a verb, 1 for an invariant word |
| `…::09. Vaba aeg::03. lause typein` | Estonian Word Forms with TypeIn | 1 note: a Russian sentence to translate into Estonian |

Notes are tagged with the level (`A2`), the topic's words (`vaba`, `aeg`) and the part of
speech. Deck-03 notes get `lause` instead of the part of speech.

## Conventions the server relies on

- **Form labels** in deck 02 are written in Russian, e.g. `1-я форма (номинатив)`.
  The server generates them, so you never type them.
- **Part-of-speech tags** are Russian: `существительное`, `глагол`, `прилагательное`,
  `наречие`, `местоимение`, `числительное`. English tags (`noun`, `verb`, …) are
  understood when reading.
- **Translations** go in Russian. The parser tells the Estonian side from the
  translation by the Cyrillic script, so for another language you'd need to change
  `parse.py`.

## Existing cards in other formats

When reading, the index also understands `Basic`, `Basic (and reversed card)`,
`Estonian Word Forms` (the older three-form variant without `Form4`) and `Audio card`, so
vocabulary you already have counts as known. See [`parse.py`](../src/anki_vocab/parse.py)
for the exact field layouts.
