# anki-vocab

A local [MCP](https://modelcontextprotocol.io) server that gives Claude access to the
Estonian vocabulary in your Anki collection. With it, Claude writes example sentences
mostly from words you already know, flags words that are actually new, avoids
duplicate cards, takes word forms from the EKI dictionary instead of guessing them,
and writes finished topic cards straight into Anki.

It's built for Russian speakers learning Estonian at A1–A2 level: cards carry Russian
translations, and the note types and deck layout ship with the repo. See
[Assumptions](#assumptions).

## How it works

- **Reading never touches the live collection.** The server copies `collection.anki2`
  together with its `-wal`/`-shm` files and opens the copy. If you open the live
  database with SQLite while Anki is running, the two can checkpoint the WAL at the
  same time and corrupt the collection. The details are in
  [`collection.py`](src/anki_vocab/collection.py).
- **Index.** Notes are parsed and lemmatized with
  [Vabamorf](https://github.com/Filosoft/vabamorf) (via `estnltk`) into a SQLite index
  in `data/vocab.sqlite`. Every tool call checks that the index is fresh (a `stat()`
  on three files) and refreshes it incrementally when the collection has changed.
- **Writing goes through Anki.** Cards are created through the
  [Anki MCP Server](https://github.com/ankimcp/anki-mcp-server-addon) add-on over HTTP on
  localhost, so Anki has to be running when you write. Reading works with Anki closed.
  Only anki-vocab is connected to Claude; it talks to the add-on itself.
- **Forms come from the dictionary.** `word_paradigm` fetches canonical forms from the
  [EKI dictionary](https://ekilex.ee) (Ekilex API, the database behind Sõnaveeb), and
  `topic_write` refuses to write if a card's forms disagree with it. Responses are
  cached in the index. Without an API key, writes still go through, with a warning
  that the forms weren't checked.
- **Small context footprint.** The add-on's ~40 tool schemas (~15k tokens) are not
  loaded up front. `anki_tools` looks them up when they are needed and `anki_call`
  runs them.

## Tools

| Tool | What it does |
| --- | --- |
| `vocab_check` | Is each word known, seen only in examples, or new? Lists the topics where it already appears. |
| `vocab_pool` | The palette: words to compose from, filtered by level, topic and part of speech, plus "forgotten" and "stale" hints. |
| `text_check` | Coverage of draft sentences: unknown words, form → lemma pairs, and overused words. |
| `word_forms` | Forms and existing examples of a word from your cards. |
| `word_paradigm` | Canonical card forms from the EKI dictionary. |
| `topic_state` | What a topic deck already contains. |
| `deck_find` | Find a deck by part of its name. |
| `vocab_debt` / `vocab_debt_skip` | Words you keep seeing in examples but have no card for, and a way to mark them as known. |
| `topic_write` | Validate a topic's words, expand them into notes for three subdecks, and write them to Anki. |
| `anki_tools` / `anki_call` | Look up and run any tool of the Anki MCP Server add-on. |

Tool responses are in English to save tokens. The server tells the model to answer
the user in Russian. You can change that in the `instructions` in
[`server.py`](src/anki_vocab/server.py).

## What you need

- [Anki](https://apps.ankiweb.net) 25.02+ with the **Anki MCP Server** add-on, which
  is what actually writes to Anki: code `124672614`
  ([AnkiWeb](https://ankiweb.net/shared/info/124672614) ·
  [GitHub](https://github.com/ankimcp/anki-mcp-server-addon) · [ankimcp.ai](https://ankimcp.ai)).
  Reading works without it; writing needs Anki open with the add-on enabled.
- [uv](https://docs.astral.sh/uv/) (it brings Python 3.12)
- Claude Desktop or Claude Code
- Optional: a free [Ekilex](https://ekilex.ee) API key for dictionary form checks

Tested on macOS. Linux and Windows profile paths are supported but untested.

## Quick start

```sh
git clone https://github.com/KrokaKrola/claude-eesti-keelt-mcp.git
cd claude-eesti-keelt-mcp
uv sync
uv run python -m anki_vocab.setup   # with Anki open
```

The setup command finds your collection, builds the index, checks the add-on, offers
to create the [two note types](docs/note-types.md), and prints the config to paste into
Claude Desktop or Claude Code. Then install the
[skill](skills/anki-topic-cards/SKILL.md).

**Step by step: [docs/setup.md](docs/setup.md).**
What the cards look like: [docs/note-types.md](docs/note-types.md).

## Configuration

Personal settings go in `anki-vocab.toml` (not tracked by git). Copy
[`anki-vocab.example.toml`](anki-vocab.example.toml) to start:

| Key | Default | Purpose |
| --- | --- | --- |
| `collection` | auto-detected when there's one Anki profile | Collection to index |
| `course` | `kursuse eesti keel::A2` | Where `topic_write` puts topics |
| `include_decks` | top-level deck of `course` | Deck branches that count as your vocabulary |
| `exclude_decks` | none | Branches inside them to skip |

Environment variables, mostly for the `env` block of the Claude config:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANKI_COLLECTION` | from `anki-vocab.toml` | Overrides `collection` |
| `ANKI_VOCAB_CONFIG` | `anki-vocab.toml` in the repo | Settings file location |
| `EKILEX_API_KEY` | read from `data/ekilex_api_key` | Ekilex API key |
| `ANKI_MCP_URL` / `ANKI_MCP_API_KEY` | read from the add-on's settings | Add-on endpoint and key |
| `ANKI_VOCAB_LOG` | `INFO` | Log level (`DEBUG` adds full arguments and responses) |
| `ANKI_VOCAB_LOG_FILE` | `data/server.log` | Log file; an empty value disables it |
| `ANKI_VOCAB_JOURNAL` | `data/journal.jsonl` | JSON journal of every call; an empty value disables it |

Tag classification and tuning constants are in [`config.py`](src/anki_vocab/config.py).

## Index CLI

The server keeps the index fresh on its own; these are for manual use.

```sh
uv run python -m anki_vocab.index --rebuild      # full rebuild
uv run python -m anki_vocab.index --refresh      # incremental refresh
uv run python -m anki_vocab.index --stats        # summary
uv run python -m anki_vocab.index --prune-cache  # drop unused lemma cache entries
```

## Assumptions

- Cards are Estonian with **Russian** translations and Russian part-of-speech tags;
  the Estonian side is recognized by the absence of Cyrillic.
- Topics use the three-subdeck layout described in [docs/note-types.md](docs/note-types.md).
- Existing cards in `Basic`, `Basic (and reversed card)`, `Estonian Word Forms` and
  `Audio card` are read as known vocabulary too.

For another language pair, start with `parse.py` and the skill.

## Development

```sh
uv run pytest
```

The tests don't need Anki, a real collection or network access.

## License

[MIT](LICENSE)
