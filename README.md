# anki-vocab

A local [MCP](https://modelcontextprotocol.io) server that gives Claude access to the
Estonian vocabulary in your Anki collection. With it, Claude writes example sentences
mostly from words you already know, flags words that are actually new, avoids
duplicate cards, takes word forms from the EKI dictionary instead of guessing them,
and writes finished topic cards straight into Anki.

It started as a personal tool for a Russian speaker learning Estonian at A1–A2 level,
so some conventions (note types, deck layout, Russian translations and tags) are
specific to that setup. See [Assumptions](#assumptions) before using it on your own
collection.

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
  [Anki MCP Server](https://ankiweb.net/shared/info/124672614) add-on over HTTP on
  localhost, so Anki has to be running when you write. Reading works with Anki closed.
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

## Requirements

- macOS (the default collection and add-on paths assume `~/Library/Application Support/Anki2`)
- Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/)
- Anki with the Anki MCP Server add-on (only needed for writing)
- A free [Ekilex](https://ekilex.ee) API key (optional, from your profile there) for dictionary form checks

## Setup

```sh
git clone https://github.com/KrokaKrola/claude-eesti-keelt-mcp.git
cd claude-eesti-keelt-mcp
uv sync
ANKI_COLLECTION="$HOME/Library/Application Support/Anki2/<profile>/collection.anki2" \
  uv run python -m anki_vocab.index --rebuild
```

### Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json`.
Use an absolute path to `uv`, because Desktop does not inherit your shell's `PATH`:

```json
{
  "mcpServers": {
    "anki-vocab": {
      "command": "/absolute/path/to/uv",
      "args": ["run", "--directory", "/absolute/path/to/claude-eesti-keelt-mcp",
               "python", "-m", "anki_vocab.server"],
      "env": {
        "ANKI_COLLECTION": "/Users/<you>/Library/Application Support/Anki2/<profile>/collection.anki2"
      }
    }
  }
}
```

Quit Desktop completely (Cmd+Q) and start it again so it picks up the change.

### Claude Code

```sh
claude mcp add anki-vocab -e ANKI_COLLECTION="..." -- \
  uv run --directory /absolute/path/to/claude-eesti-keelt-mcp python -m anki_vocab.server
```

### Skill

[`skills/anki-topic-cards/SKILL.md`](skills/anki-topic-cards/SKILL.md) is the workflow
Claude follows to turn a word list into topic cards with these tools. Install it as a
skill in Claude Desktop or Claude Code.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANKI_COLLECTION` | `~/Library/Application Support/Anki2/Kekw/collection.anki2` | Collection to index |
| `EKILEX_API_KEY` | read from `data/ekilex_api_key` | Ekilex API key |
| `ANKI_MCP_URL` | read from the add-on's `meta.json` | Add-on endpoint |
| `ANKI_VOCAB_LOG` | `INFO` | Log level (`DEBUG` adds full arguments and responses) |
| `ANKI_VOCAB_LOG_FILE` | `data/server.log` | Log file; an empty value disables it |
| `ANKI_VOCAB_JOURNAL` | `data/journal.jsonl` | Full JSON journal of every tool call; an empty value disables it |

Deck scope, tag classification and tuning constants are in
[`config.py`](src/anki_vocab/config.py).

## Index CLI

```sh
uv run python -m anki_vocab.index --rebuild      # full rebuild
uv run python -m anki_vocab.index --refresh      # incremental refresh
uv run python -m anki_vocab.index --stats        # summary
uv run python -m anki_vocab.index --prune-cache  # drop unused lemma cache entries
```

## Assumptions

The parser and writer expect this setup:

- Decks under `kursuse eesti keel::<level>::<NN. Topic>`, each with the subdecks
  `01. sõnad`, `02. sõnad typein` and `03. lause typein` (see `INCLUDED_DECK_PREFIXES`
  in `config.py`).
- Note types `Estonian 3 Forms`, `Estonian Word Forms`, `Estonian Word Forms with TypeIn`,
  `Basic`, `Basic (and reversed card)` and `Audio card`, laid out as described in
  [`parse.py`](src/anki_vocab/parse.py).
- Russian translations, and part-of-speech tags in Russian (`существительное`, `глагол`, …).

If your collection is organized differently, start with `config.py` and `parse.py`.

## Development

```sh
uv run pytest
```

The tests don't need Anki, a real collection or network access.

## License

[MIT](LICENSE)
