"""Дымовые тесты самого сервера.

Раньше ни один тест не импортировал server.py, поэтому опечатка
в отступе доехала до Claude Desktop и уронила его при старте.

Тесты работают на своём пустом индексе: лезть в рабочий нельзя —
его же в это время пересобирает сервер, запущенный в Desktop.
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
        assert callable(getattr(server, name)), f"инструмент {name} не определён"


@pytest.mark.parametrize(
    "name,kwargs",
    [
        ("vocab_check", {"words": ["jõgi", "xyzzy"]}),
        ("vocab_pool", {"level": "a2"}),
        ("vocab_pool", {"topics": ["несуществующая"]}),
        ("vocab_pool", {"limit": 5}),
        ("text_check", {"sentences": ["Ma lähen koju."], "targets": ["minema"]}),
        ("word_forms", {"word": "xyzzy"}),
        ("topic_state", {"topic": "нет такой темы"}),
    ],
)
def test_tool_runs_and_returns_text(name, kwargs):
    """Вызываем ровно так, как это делает SDK — только именованными."""
    out = getattr(server, name)(**kwargs)
    assert isinstance(out, str) and out


def test_index_is_not_opened_until_a_tool_is_called():
    assert server._con is None, "открытие индекса на импорте ломает изоляцию"
    server.vocab_check(words=["x"])
    assert server._con is not None


def test_phrasal_verb_form_is_found_in_its_example():
    """'saan teada' стоит в примере дословно, а проверка 01.10 уверяла,
    что формы там нет: фраза разбиралась как одно слово."""
    from anki_vocab.query import Vocab
    import sqlite3

    con = sqlite3.connect(":memory:")
    con.executescript(index.SCHEMA)
    check = server._form_in_example(Vocab(con))
    assert check("saan teada", "Ma saan teada homme.")
    assert check("teada saama", "Ta tuli hinda teada saama.")
    assert check("teada saada", "Ma tahan seda kiiresti teada.") is False
