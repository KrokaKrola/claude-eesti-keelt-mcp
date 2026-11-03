"""Shipped note types and the setup check. No Anki needed: the add-on is faked."""
from __future__ import annotations

import re

import pytest

from anki_vocab import setup
from anki_vocab.write import MODEL_TYPEIN, MODEL_WORD

SPECS = [setup.load_note_type(d) for d in sorted(setup.NOTE_TYPES_DIR.iterdir())]
SPECIAL = {"FrontSide", "Tags", "Type", "Deck", "Subdeck", "Card"}


def test_ships_the_note_types_the_writer_uses():
    assert {s["model_name"] for s in SPECS} == {MODEL_WORD, MODEL_TYPEIN}


@pytest.mark.parametrize("spec", SPECS, ids=lambda s: s["model_name"])
def test_templates_reference_only_existing_fields(spec):
    """create_model rejects a template that names a missing field."""
    fields = set(spec["in_order_fields"])
    for t in spec["card_templates"]:
        for ref in re.findall(r"{{([^}]+)}}", t["Front"] + t["Back"]):
            name = ref.lstrip("#/^").split(":")[-1].strip()
            assert name in fields | SPECIAL, f"{t['Name']}: {{{{{ref}}}}}"


def test_writer_fields_exist_in_shipped_note_types():
    from anki_vocab.write import build_topic

    word = {
        "word": "jõgi", "pos": "существительное", "ru": "река",
        "forms": ["jõgi", "jõe", "jõge", "jõgesid"],
        "ex": [["Jõgi on suur.", "Река большая."], ["Jõe ääres.", "У реки."],
               ["Näen jõge.", "Вижу реку."]],
        "sentence": ["Jõgi on külm.", "Река холодная."],
    }
    batches, *_ = build_topic("kursuse eesti keel::A2", "09. Vaba aeg", [word])
    fields_of = {s["model_name"]: set(s["in_order_fields"]) for s in SPECS}
    for batch in batches:
        for note in batch.notes:
            assert set(note["fields"]) <= fields_of[batch.model], batch.deck


def test_missing_note_types_are_created_with_yes(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(setup, "ARGS", setup.argparse.Namespace(yes=True))
    monkeypatch.setattr(setup, "call", lambda tool, args: calls.append((tool, args)) or {})
    assert setup.check_note_types(set())
    assert [args["model_name"] for tool, args in calls if tool == "create_model"] == [
        s["model_name"] for s in SPECS
    ]


def test_mismatched_fields_are_reported_not_changed(monkeypatch, capsys):
    calls = []

    def fake(tool, args):
        calls.append(tool)
        return {"field_names": ["Front", "Back"]}

    monkeypatch.setattr(setup, "call", fake)
    assert not setup.check_note_types({s["model_name"] for s in SPECS})
    assert "create_model" not in calls
    assert "different fields" in capsys.readouterr().out
