"""Lazy exposure of the add-on's tools.

The add-on can do 41 things, and its schemas weigh 15,500 tokens. They sit
in the context of every conversation, even ones that have nothing to do
with Anki: the model only sees what arrived in the request, so the tool
list has to arrive up front.

Here it doesn't. Two small handles live in the context: one shows the
table of contents and fetches a tool's schema on demand, the other calls
it. Discovery costs ~1100 tokens and only happens in conversations that
actually get to Anki.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import DATA_DIR
from .write import AnkiError, call, rpc

CACHE = DATA_DIR / "anki_tools.json"

# The add-on's response on a large collection reaches 30 KB (list_decks on
# 115 decks). Truncate with an explicit marker: a silently cut list is worse
# than an honestly short one.
MAX_RESULT = 12000

# Add-on tools that only read. We list these rather than the writing ones:
# the add-on adds new tools, and an unknown one must count as a write —
# better the log says "bypassing topic_write" once too often than stay
# silent. A prefix list of writers did stay silent about 60 card moves
# via card_management.
READ_ONLY = {
    "cards_stats", "find_notes", "get_card_memory_state", "get_due_cards",
    "get_fsrs_params", "get_media_files_names", "list_decks",
    "model_field_names", "model_names", "model_styling", "model_templates",
    "notes_info", "present_card",
    "gui_browse", "gui_current_card", "gui_deck_browser", "gui_select_card",
    "gui_show_answer", "gui_show_question",
}

# Actions of multi-purpose tools (card_management, tag_management,
# filtered_deck, …) that change nothing.
READ_ONLY_ACTIONS = {"get_tags", "get_info"}


def mutating(tool: str, args: dict | None = None) -> bool:
    if tool in READ_ONLY:
        return False
    args = args or {}
    params = args.get("params")
    if isinstance(params, dict) and params.get("action") in READ_ONLY_ACTIONS:
        return False
    # The FSRS optimizer without apply_results is a dry run.
    if tool == "optimize_fsrs_params" and not args.get("apply_results"):
        return False
    return True


# Don't return more than a few schemas at once: a broad pattern like 'a'
# would otherwise dump all 41 and defeat the whole point.
MAX_SCHEMAS = 4


def _fetch() -> list[dict]:
    return rpc("tools/list", {}, timeout=20.0).get("tools", [])


def catalog(refresh: bool = False) -> tuple[list[dict], str]:
    """The add-on's table of contents and a note on where it came from.

    The live list is more accurate — the add-on updates and renames actions
    (forget → forget_cards has happened). But the table of contents is
    needed with Anki closed too, so the last good response is kept on disk.
    """
    if not refresh and CACHE.exists():
        try:
            saved = json.loads(CACHE.read_text())
        except ValueError:
            saved = None
        if saved:
            try:
                fresh = _fetch()
            except AnkiError:
                age = int((time.time() - CACHE.stat().st_mtime) / 86400)
                return saved["tools"], f"cached list, {age}d old (Anki not running)"
            _save(fresh)
            return fresh, "live"
    fresh = _fetch()
    _save(fresh)
    return fresh, "live"


def _save(tools: list[dict]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"tools": tools}, ensure_ascii=False))


def names(tools: list[dict]) -> str:
    return ", ".join(sorted(t["name"] for t in tools))


def match(tools: list[dict], pattern: str) -> list[dict]:
    """An exact name beats a substring: 'sync' must not also bring in
    sync_state when a tool with that exact name exists."""
    needle = pattern.strip().lower()
    exact = [t for t in tools if t["name"].lower() == needle]
    if exact:
        return exact
    return [
        t for t in tools
        if needle in t["name"].lower() or needle in t.get("description", "").lower()
    ]


def describe(tools: list[dict], pattern: str, source: str) -> str:
    hits = match(tools, pattern)
    if not hits:
        return (
            f"no add-on tool matches '{pattern}' [{source}]\n"
            f"available: {names(tools)}"
        )
    if len(hits) > MAX_SCHEMAS:
        return (
            f"{len(hits)} tools match '{pattern}' — narrow it down [{source}]\n"
            + ", ".join(sorted(t["name"] for t in hits))
        )
    out = [f"[{source}]"]
    for t in hits:
        out.append(json.dumps(t, ensure_ascii=False))
    return "\n".join(out)


def result_text(payload: dict) -> str:
    """The add-on's response comes indented and with a second copy in
    structuredContent. We already took the parsed copy; just don't
    pretty-print it."""
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) <= MAX_RESULT:
        return text
    return (
        text[:MAX_RESULT]
        + f"\n… truncated at {MAX_RESULT} chars of {len(text)}; "
        "narrow the request (filters, fewer ids) instead of reading it all"
    )


def invoke(tools: list[dict], tool: str, args: dict | None) -> str:
    known = {t["name"] for t in tools}
    if tool not in known:
        close = sorted(n for n in known if tool.lower() in n.lower() or n.lower() in tool.lower())
        hint = f" did you mean: {', '.join(close)}?" if close else f" available: {names(tools)}"
        return f"no such add-on tool: {tool}.{hint}"
    try:
        return result_text(call(tool, args or {}))
    except AnkiError as exc:
        return f"{exc}\ncheck the arguments with anki_tools('{tool}')"
