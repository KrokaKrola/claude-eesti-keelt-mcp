"""Оглавление аддона и проброс вызовов. Anki для этих тестов не нужен:
фикстура — сохранённый ответ tools/list из backup/."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from anki_vocab import passthrough as pt

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "backup/2026-09-02-before-mcp-slim/tools-list-before.json"
)


@pytest.fixture
def tools():
    raw = FIXTURE.read_text()
    line = next(l for l in raw.splitlines() if l.startswith("data: "))
    return json.loads(line[6:])["result"]["tools"]


def test_fixture_is_the_whole_addon(tools):
    assert len(tools) == 41


def test_names_are_sorted_and_plain(tools):
    line = pt.names(tools)
    assert line.startswith("add_note, add_notes, card_management")
    assert "\n" not in line


def test_exact_name_wins_over_substring(tools):
    """'sync' обязан отдать sync, а не всё, где встречается это слово."""
    assert [t["name"] for t in pt.match(tools, "sync")] == ["sync"]


def test_keyword_search_looks_into_descriptions(tools):
    """У инструмента может не быть слова в имени: 'scheduler' находит
    get_due_cards, хотя в названии его нет."""
    hits = {t["name"] for t in pt.match(tools, "scheduler")}
    assert "get_due_cards" in hits


def test_describe_returns_a_usable_schema(tools):
    out = pt.describe(tools, "create_deck", "live")
    body = json.loads(out.splitlines()[1])
    assert body["name"] == "create_deck"
    assert "deck_name" in body["inputSchema"]["properties"]


def test_wide_pattern_lists_names_instead_of_dumping_schemas(tools):
    """Ради этого всё и затевалось: широкий шаблон не должен вываливать
    те самые 15 тысяч токенов обратно в контекст."""
    out = pt.describe(tools, "a", "live")
    assert "narrow it down" in out
    assert "inputSchema" not in out


def test_unknown_pattern_shows_what_is_available(tools):
    out = pt.describe(tools, "нетакого", "live")
    assert "no add-on tool matches" in out and "add_notes" in out


def test_unknown_tool_suggests_close_names(tools):
    out = pt.invoke(tools, "delete_note", None)
    assert "no such add-on tool" in out and "delete_notes" in out


def test_long_results_are_truncated_out_loud(tools, monkeypatch):
    monkeypatch.setattr(pt, "call", lambda *a, **k: {"decks": ["x" * pt.MAX_RESULT]})
    out = pt.invoke(tools, "list_decks", {})
    assert "truncated at" in out
    assert len(out) < pt.MAX_RESULT + 300


def test_result_is_compact_json(tools, monkeypatch):
    monkeypatch.setattr(pt, "call", lambda *a, **k: {"deckId": 1, "created": True})
    assert pt.invoke(tools, "create_deck", {"deck_name": "x"}) == \
        '{"deckId": 1, "created": true}'


def test_addon_error_points_at_the_schema(tools, monkeypatch):
    def boom(*a, **k):
        raise pt.AnkiError("create_deck: Deck name can have maximum 2 levels")
    monkeypatch.setattr(pt, "call", boom)
    out = pt.invoke(tools, "create_deck", {"deck_name": "a::b::c"})
    assert "maximum 2 levels" in out and "anki_tools('create_deck')" in out


def test_mutating_covers_the_write_tools(tools):
    """Проброс должен отличать запись от чтения: запись мимо topic_write
    — единственное, что потом не сходится в логе."""
    names = {t["name"] for t in tools}
    for tool in ("add_notes", "update_note_fields", "delete_notes", "create_deck"):
        assert tool in names, f"аддон больше не отдаёт {tool}"
        assert pt.mutating(tool)
    for tool in ("find_notes", "notes_info", "list_decks", "gui_browse"):
        assert tool in names
        assert not pt.mutating(tool)


def test_multi_action_tools_are_writes_unless_they_only_read():
    """card_management двигал и переставлял карточки, а лог молчал:
    по префиксу имени он на запись не походил."""
    assert pt.mutating("card_management", {"params": {"action": "change_deck"}})
    assert pt.mutating("tag_management", {"params": {"action": "replace_tags"}})
    assert not pt.mutating("tag_management", {"params": {"action": "get_tags"}})
    assert pt.mutating("some_future_tool"), "неизвестное считаем записью"
