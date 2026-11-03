# Setup guide

This walks you from nothing to Claude writing cards into your Anki, in about 15 minutes.
At the end you'll have:

- **Anki** with the **Anki MCP Server** add-on, which lets programs talk to Anki
- the two **note types** this project writes
- the **anki-vocab** server connected to Claude Desktop or Claude Code
- the **anki-topic-cards** skill, the workflow Claude follows to make cards

There are two MCP servers involved, and it helps to keep them apart:

| | anki-vocab (this repo) | Anki MCP Server (add-on) |
| --- | --- | --- |
| Runs | as a separate process started by Claude | inside Anki |
| Reads | a copy of your collection file, so Anki may be closed | — |
| Writes | nothing directly; it asks the add-on | notes and decks |
| Claude talks to it | yes | no, only through anki-vocab |

You connect only anki-vocab to Claude. It reaches the add-on over `http://127.0.0.1:3141`,
and its `anki_tools` / `anki_call` tools expose the rest of the add-on when needed.

## 1. Install the tools

- [Anki](https://apps.ankiweb.net) 25.02 or newer.
- [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.12
  and the dependencies for you.
- [Claude Desktop](https://claude.ai/download) or [Claude Code](https://claude.com/claude-code).

Then get the code:

```sh
git clone https://github.com/KrokaKrola/claude-eesti-keelt-mcp.git
cd claude-eesti-keelt-mcp
uv sync
```

The first `uv sync` downloads `estnltk` (the Estonian morphology library), which is
fairly large.

## 2. Install the Anki MCP Server add-on

1. In Anki: **Tools → Add-ons → Get Add-ons…**, enter code **`124672614`**, click OK.
2. Restart Anki.

Links: [AnkiWeb page](https://ankiweb.net/shared/info/124672614) ·
[source on GitHub](https://github.com/ankimcp/anki-mcp-server-addon) ·
[ankimcp.ai](https://ankimcp.ai)

The default settings work. If you change the port or set an API key in the add-on's
config, anki-vocab picks both up from the add-on's settings file. Writing cards only
works while Anki is open.

## 3. Choose your collection and decks (optional)

Skip this step if you have one Anki profile and are happy with the default deck names.

```sh
cp anki-vocab.example.toml anki-vocab.toml
```

`anki-vocab.toml` stays out of git. In it you can set:

- `collection`: needed if you have more than one Anki profile.
- `course`: where new topics go. The default is `kursuse eesti keel::A2`, so a topic
  `09. Vaba aeg` ends up in `kursuse eesti keel::A2::09. Vaba aeg::01. sõnad`, `…::02. sõnad typein`
  and `…::03. lause typein`. The decks are created on the first write.
- `include_decks` / `exclude_decks`: which deck branches count as "your vocabulary".

See [note-types.md](note-types.md) for what goes into each deck.

## 4. Run the setup check

Open Anki first, then run:

```sh
uv run python -m anki_vocab.setup
```

It checks each piece in turn and tells you what to fix:

1. Finds your collection and reads a copy of it.
2. Builds the vocabulary index in `data/vocab.sqlite` (a few seconds).
3. Connects to the add-on.
4. Checks the two note types. If they're missing, it offers to create them from
   [`anki/note-types/`](../anki/note-types). That's the only change it makes in Anki,
   and only if you answer `y`. If a note type with the same name but different fields
   already exists, it reports the mismatch and changes nothing.
5. Checks the optional Ekilex key (step 5).
6. Prints the exact config for Claude Desktop and Claude Code, with the paths on your
   machine filled in.

Run it again whenever something looks off. It's safe to repeat.

## 5. Dictionary checks (optional, recommended)

With a free [Ekilex](https://ekilex.ee) API key, Claude takes word forms from the EKI
dictionary (the database behind Sõnaveeb) instead of guessing, and `topic_write`
refuses forms that disagree with it.

1. Sign up at ekilex.ee and copy the API key from your profile.
2. Save it to `data/ekilex_api_key`, or set `EKILEX_API_KEY` in the server's `env` in
   the Claude config.

Without the key everything still works. Writes then carry a warning that the forms
weren't checked.

## 6. Connect Claude

**Claude Desktop:** open `claude_desktop_config.json`
(Settings → Developer → Edit Config) and merge in the `anki-vocab` block printed by the
setup check. Then **quit Desktop completely** (Cmd+Q on macOS) and reopen it. Use the absolute
path to `uv` as printed, because Desktop doesn't see your shell's `PATH`.

**Claude Code:** run the `claude mcp add …` line printed by the setup check.

Do **not** connect the Anki MCP Server add-on to Claude directly as well. Its ~40 tool
schemas cost ~15k tokens in every conversation; anki-vocab already exposes them on demand.

## 7. Install the skill

The workflow lives in [`skills/anki-topic-cards/SKILL.md`](../skills/anki-topic-cards/SKILL.md).

- **Claude Desktop / claude.ai:** zip the `skills/anki-topic-cards` folder and upload it
  under Settings → Capabilities → Skills.
- **Claude Code:** copy or symlink the folder into `~/.claude/skills/`:

  ```sh
  ln -s "$PWD/skills/anki-topic-cards" ~/.claude/skills/anki-topic-cards
  ```

The skill writes its reports to you in Russian and expects Russian translations on the
cards. Edit `SKILL.md` if you want another language.

## 8. Try it

With Anki open, ask Claude something like:

> Add these words to topic 09. Vaba aeg: jõgi, ujuma, kalastama

Claude checks for duplicates, loads your known vocabulary, drafts examples from it,
checks them, and writes the cards. Before writing for real you can ask for a dry run
(`topic_write` with `dry_run`).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `collection: several Anki profiles` | Set `collection` in `anki-vocab.toml`. |
| `cannot reach the add-on … Is Anki running?` | Open Anki, and check that the add-on is enabled under Tools → Add-ons. |
| `note type '…' exists with different fields` | Rename your existing note type in Anki (Tools → Manage Note Types) and rerun setup, or adjust its fields to match [note-types.md](note-types.md). |
| Claude Desktop doesn't show the tools | Check the absolute `uv` path in the config, quit Desktop fully and reopen. The logs are in `~/Library/Logs/Claude/mcp-server-anki-vocab.log`. |
| Index looks empty | Your decks are outside `include_decks`. Run `uv run python -m anki_vocab.index --stats`. |

The server also writes `data/server.log` (readable log) and `data/journal.jsonl` (every
call with full arguments).
