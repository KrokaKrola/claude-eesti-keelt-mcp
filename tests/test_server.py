"""Smoke tests for the server itself.

Previously no test imported server.py, so an indentation typo made it
all the way to Claude Desktop and crashed it on startup.

Tests run on their own empty index: touching the real one is not allowed —
the server running in Desktop is rebuilding it at the same time.
"""
import pytest

from anki_vocab import config, index, server

TOOLS = ["vocab_check", "vocab_pool", "text_check", "word_forms", "topic_state"]


@pytest.fixture(autouse=True)
def isolated_index(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "INDEX", tmp_path / "vocab.sqlite")
    monkeypatch.setattr(server, "ensure_fresh", lambda con, **_: False)
    monkeypatch.setattr(server, "_con", None)
    monkeypatch.setattr(server, "_vocab", None)
    yield
    if server._con is not None:
        server._con.close()


def test_module_registers_every_tool():
    for name in TOOLS:
        assert callable(getattr(server, name)), f"tool {name} is not defined"


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("vocab_check", {"words": ["jõgi", "xyzzy"]}),
        ("vocab_pool", {"level": "a2"}),
        ("vocab_pool", {"topics": ["nonexistent"]}),
        ("vocab_pool", {"limit": 5}),
        ("text_check", {"sentences": ["Ma lähen koju."], "targets": ["minema"]}),
        ("word_forms", {"word": "xyzzy"}),
        ("topic_state", {"topic": "no such topic"}),
    ],
)
def test_tool_runs_and_returns_text(name, kwargs):
    """Call exactly the way the SDK does — keyword arguments only."""
    out = getattr(server, name)(**kwargs)
    assert isinstance(out, str) and out


def test_index_is_not_opened_until_a_tool_is_called():
    assert server._con is None, "opening the index on import breaks isolation"
    server.vocab_check(words=["x"])
    assert server._con is not None


def test_phrasal_verb_form_is_found_in_its_example():
    """'saan teada' stands verbatim in the example, yet the check on 01.10
    claimed the form wasn't there: the phrase was parsed as one word."""
    from anki_vocab.query import Vocab
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.executescript(index.SCHEMA)
    check = server._form_in_example(Vocab(con))
    assert check("saan teada", "Ma saan teada homme.")
    assert check("teada saama", "Ta tuli hinda teada saama.")
    assert check("teada saada", "Ma tahan seda kiiresti teada.") is False
