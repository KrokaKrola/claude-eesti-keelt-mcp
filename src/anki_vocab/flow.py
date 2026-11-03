"""What happened before a topic write — the skill's steps as the server sees them.

Reviewing the log for 18.09–01.10 came down to one question: was the
workflow followed? 17. Kohvikus went into the deck without vocab_pool,
topic_state and text_check, and the only way to tell was walking through
call numbers by hand. Here the server itself remembers recent calls and on
every write says which steps preceded it — as one log line and a journal
field.

Memory is per process: one conversation goes through one server process,
and Claude Desktop's second process keeps its own.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# A topic is filled in one sitting; calls older than this are another sitting.
WINDOW_SECONDS = 3 * 3600


@dataclass
class Event:
    at: float
    tool: str
    data: dict = field(default_factory=dict)


class Flow:
    def __init__(self) -> None:
        self.events: deque[Event] = deque(maxlen=1000)

    def note(self, tool: str, **data) -> None:
        self.events.append(Event(time.time(), tool, data))

    def _recent(self, tool: str) -> list[Event]:
        since = time.time() - WINDOW_SECONDS
        return [e for e in self.events if e.tool == tool and e.at >= since]

    def before_write(self, topic: str, words: list[str], sentences: list[str]) -> dict:
        """Summary of the steps before a topic write: what was called and what was missed."""
        now = time.time()
        words = [w.strip().lower() for w in words if w.strip()]
        texts = {s.strip() for s in sentences if s.strip()}
        topic_key = topic.strip().lower()

        def ago(events: list[Event]) -> int | None:
            return round((now - events[-1].at) / 60) if events else None

        checked: dict[str, str] = {}
        for e in self._recent("vocab_check"):
            checked.update(e.data.get("marks", {}))
        seen_by_paradigm = set()
        for e in self._recent("word_paradigm"):
            seen_by_paradigm |= set(e.data.get("words", []))
        drafted = set()
        topic_checks = []
        for e in self._recent("text_check"):
            drafted |= set(e.data.get("sentences", []))
            if (e.data.get("topic") or "").strip().lower() == topic_key:
                topic_checks.append(e)
        pools = self._recent("vocab_pool")
        dry = [e for e in self._recent("topic_write_dry")
               if e.data.get("topic", "").strip().lower() == topic_key]
        states = [e for e in self._recent("topic_state")
                  if e.data.get("topic", "").strip().lower() == topic_key]

        return {
            "vocab_check_min_ago": ago(self._recent("vocab_check")),
            "not_vocab_checked": [w for w in words if w not in checked],
            # A word with a card (✓) written again, or '~' without the
            # learner's decision: the skill says to set those aside.
            "written_duplicates": [w for w in words if checked.get(w) == "✓"],
            "written_sentence_only": [w for w in words if checked.get(w) == "~"],
            "not_in_word_paradigm": [w for w in words if w not in seen_by_paradigm],
            "vocab_pool_min_ago": ago(pools),
            "topic_state_min_ago": ago(states),
            "text_check_topic_min_ago": ago(topic_checks),
            "sentences": len(texts),
            "sentences_text_checked": len(texts & drafted),
            "dry_run_min_ago": ago(dry),
        }


def summary(report: dict) -> str:
    """One line for server.log: ✓ — the step happened, ✗ — skipped."""
    def step(name: str, minutes: int | None) -> str:
        return f"{name} ✓{minutes}m" if minutes is not None else f"{name} ✗"

    parts = [
        step("vocab_check", report["vocab_check_min_ago"]),
        step("topic_state", report["topic_state_min_ago"]),
        step("vocab_pool", report["vocab_pool_min_ago"]),
        step("text_check(topic)", report["text_check_topic_min_ago"]),
        step("dry_run", report["dry_run_min_ago"]),
        f"text_check covered {report['sentences_text_checked']}/{report['sentences']} sent.",
    ]
    for key, label in (
        ("not_vocab_checked", "no vocab_check"),
        ("not_in_word_paradigm", "forms not from word_paradigm"),
        ("written_duplicates", "⚠ duplicates ✓ written"),
        # Not an error by itself: the learner may have said "add it". But
        # without their answer in the chat it breaks step 2 — check the journal.
        ("written_sentence_only", "~ words written (only after the learner's answer)"),
    ):
        if report[key]:
            parts.append(f"{label}: {', '.join(report[key][:8])}"
                         + (f" +{len(report[key]) - 8}" if len(report[key]) > 8 else ""))
    return " · ".join(parts)
