"""Step summary before a write — what reviewing the 01.10 log meant
reconstructing by hand from call numbers."""
import json

from anki_vocab import journal
from anki_vocab.flow import Flow, summary


def test_write_with_no_steps_before_it_says_so():
    """17. Kohvikus went into the deck without a single check."""
    report = Flow().before_write("17. Kohvikus", ["baar"], ["Baar on suur."])
    line = summary(report)
    for step in ("vocab_check ✗", "vocab_pool ✗", "text_check(topic) ✗", "dry_run ✗"):
        assert step in line
    assert "text_check covered 0/1" in line


def test_full_scenario_is_recognised():
    flow = Flow()
    flow.note("vocab_check", marks={"baar": "✗", "pakkuma": "~", "kohvik": "✓"})
    flow.note("topic_state", topic="17. Kohvikus")
    flow.note("vocab_pool", topics=["kohvikus"])
    flow.note("word_paradigm", words=["baar"])
    flow.note("text_check", topic="17. Kohvikus", sentences=["Baar on suur."])
    flow.note("topic_write_dry", topic="17. kohvikus")
    report = flow.before_write(
        "17. Kohvikus", ["Baar", "pakkuma", "kohvik"], ["Baar on suur.", "Uus lause."]
    )
    assert report["sentences_text_checked"] == 1 and report["sentences"] == 2
    assert report["written_duplicates"] == ["kohvik"]
    assert report["written_sentence_only"] == ["pakkuma"]
    assert report["not_in_word_paradigm"] == ["pakkuma", "kohvik"]
    assert report["dry_run_min_ago"] == 0
    assert "⚠ duplicates ✓ written: kohvik" in summary(report)


def test_journal_keeps_full_arguments(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    import logging
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    monkeypatch.setattr(journal, "PATH", path)
    journal._log.addHandler(handler)
    try:
        long = "Ma " + "väga " * 100 + "pikk lause."
        journal.record("text_check", 3, {"sentences": [long]}, "ok", 12.3,
                       extra={"flow": {"x": 1}})
    finally:
        journal._log.removeHandler(handler)
        handler.close()
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["args"]["sentences"] == [long]
    assert entry["tool"] == "text_check" and entry["flow"] == {"x": 1}
