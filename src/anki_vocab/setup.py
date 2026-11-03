"""Setup check: walks through every piece the server needs and says what's missing.

    uv run python -m anki_vocab.setup          # check, offer to create note types
    uv run python -m anki_vocab.setup --yes    # create missing note types without asking

Reads the collection only through a copy. The one thing it can write to Anki
is the two note types, through the add-on, and only after confirmation.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import ekilex
from .collection import CollectionError, snapshot
from .config import (
    COLLECTION, COLLECTION_HINT, COURSE, PROJECT_ROOT, SETTINGS_FILE, profile_collections,
)
from .index import build, open_index
from .write import AnkiError, call, endpoint

NOTE_TYPES_DIR = PROJECT_ROOT / "anki/note-types"
ADDON_URL = "https://ankiweb.net/shared/info/124672614"

OK, WARN, FAIL = "✓", "!", "✗"
ARGS = argparse.Namespace(yes=False)


def say(mark: str, text: str, hint: str = "") -> None:
    print(f"{mark} {text}")
    if hint:
        print("    " + hint.replace("\n", "\n    "))


def load_note_type(folder: Path) -> dict:
    """note-type.json plus the HTML/CSS files it names, in create_model's shape."""
    spec = json.loads((folder / "note-type.json").read_text(encoding="utf-8"))
    read = lambda name: (folder / name).read_text(encoding="utf-8")
    return {
        "model_name": spec["name"],
        "in_order_fields": spec["fields"],
        "card_templates": [
            {"Name": t["name"], "Front": read(t["front"]), "Back": read(t["back"])}
            for t in spec["templates"]
        ],
        "css": read(spec["css"]),
    }


def check_collection() -> bool:
    if COLLECTION_HINT:
        found = profile_collections()
        hint = (
            "".join(f"found: {p}\n" for p in found)
            + f'set collection = "<path>" in {SETTINGS_FILE.name} (copy anki-vocab.example.toml)'
            if found else
            "install Anki and open it once, or set collection = \"<path>\" in "
            f"{SETTINGS_FILE.name} (copy anki-vocab.example.toml)"
        )
        say(FAIL, f"collection: {COLLECTION_HINT}", hint)
        return False
    try:
        with snapshot() as con:
            notes = con.execute("select count(*) from notes").fetchone()[0]
    except CollectionError as exc:
        say(FAIL, f"collection: {exc}")
        return False
    say(OK, f"collection: {COLLECTION} ({notes} notes)")
    return True


def check_index() -> None:
    con = open_index()
    try:
        stats = build(con, full=False, quiet=True)
        entries = con.execute("select count(*) from entry").fetchone()[0]
    finally:
        con.close()
    if entries:
        say(OK, f"index: {entries} notes in scope ({stats['seconds']} s)")
    else:
        say(WARN, "index: no notes in scope yet",
            f"that's expected for a new course; scope is the deck '{COURSE.split('::')[0]}'"
            f" (include_decks in {SETTINGS_FILE.name})")


def check_addon() -> bool:
    try:
        models = set(call("model_names", {}).get("modelNames", []))
    except AnkiError as exc:
        say(FAIL, f"Anki MCP Server add-on: {exc}",
            f"install it from {ADDON_URL} (code 124672614), restart Anki and keep it "
            "open while writing cards")
        return False
    say(OK, f"Anki MCP Server add-on at {endpoint()}")
    return check_note_types(models)


def check_note_types(models: set[str]) -> bool:
    missing, ready = [], True
    for folder in sorted(NOTE_TYPES_DIR.iterdir()):
        spec = load_note_type(folder)
        name = spec["model_name"]
        if name not in models:
            missing.append(spec)
            continue
        have = call("model_field_names", {"model_name": name}).get("field_names", [])
        if have == spec["in_order_fields"]:
            say(OK, f"note type '{name}'")
        else:
            ready = False
            say(FAIL, f"note type '{name}' exists with different fields",
                f"have: {have}\nneed: {spec['in_order_fields']}\n"
                "rename yours or fix the fields in Anki (Tools → Manage Note Types)")
    return (create_note_types(missing) if missing else True) and ready


def create_note_types(missing: list[dict]) -> bool:
    names = ", ".join(f"'{s['model_name']}'" for s in missing)
    if not ARGS.yes:
        if not sys.stdin.isatty():
            say(FAIL, f"missing note types: {names}", "rerun with --yes to create them")
            return False
        if input(f"  create note types {names} in Anki? [y/N] ").strip().lower() != "y":
            say(FAIL, f"missing note types: {names}")
            return False
    for spec in missing:
        try:
            call("create_model", spec)
        except AnkiError as exc:
            say(FAIL, f"could not create '{spec['model_name']}': {exc}")
            return False
        say(OK, f"note type '{spec['model_name']}' created")
    return True


def check_ekilex() -> None:
    if not ekilex.api_key():
        say(WARN, "Ekilex key: not set — card forms won't be checked against the dictionary",
            "get a free key in your profile at https://ekilex.ee, then set EKILEX_API_KEY\n"
            "or save it to data/ekilex_api_key")
        return
    con = open_index()
    try:
        ekilex.Dictionary(con).lookup_many(["koer"])
        say(OK, "Ekilex key works")
    except ekilex.EkilexError as exc:
        say(WARN, f"Ekilex: {exc}")
    finally:
        con.close()


def print_client_config() -> None:
    uv = shutil.which("uv") or "/absolute/path/to/uv"
    args = ["run", "--directory", str(PROJECT_ROOT), "python", "-m", "anki_vocab.server"]
    desktop = {"mcpServers": {"anki-vocab": {"command": uv, "args": args}}}
    print("\nClaude Desktop — merge into claude_desktop_config.json, then quit and reopen:")
    print(json.dumps(desktop, indent=2))
    print("\nClaude Code:")
    print(f'claude mcp add anki-vocab -- {uv} run --directory "{PROJECT_ROOT}" '
          "python -m anki_vocab.server")


def main() -> None:
    global ARGS
    ap = argparse.ArgumentParser(prog="anki_vocab.setup", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="create missing note types without asking")
    ARGS = ap.parse_args()

    ready = check_collection()
    if ready:
        check_index()
    ready &= check_addon()
    check_ekilex()
    print_client_config()
    print("\nall set" if ready else "\nfix the ✗ items above and run this again")
    sys.exit(0 if ready else 1)


if __name__ == "__main__":
    main()
