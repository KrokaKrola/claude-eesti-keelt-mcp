"""Что происходило перед записью темы — шаги скилла глазами сервера.

Разбор лога за 18.09–01.10 упирался в одно: соблюдался ли сценарий.
17. Kohvikus ушёл в колоду без vocab_pool, topic_state и text_check, и
понять это можно было только перебирая номера вызовов вручную. Здесь
сервер сам помнит недавние вызовы и при каждой записи говорит, какие
шаги перед ней были, — одной строкой в логе и полем в журнале.

Память на процесс: одна беседа идёт через один процесс сервера, а
второй процесс Claude Desktop ведёт свою.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

# Тема заполняется за один заход; вызовы старше этого — уже другой заход.
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
        """Сводка шагов перед записью темы: что вызывалось и что упущено."""
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
            # Слово с карточкой (✓) записано снова, или '~' без решения
            # ученика: скилл велит такие откладывать.
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
    """Одна строка для server.log: ✓ — шаг был, ✗ — пропущен."""
    def step(name: str, minutes: int | None) -> str:
        return f"{name} ✓{minutes}м" if minutes is not None else f"{name} ✗"

    parts = [
        step("vocab_check", report["vocab_check_min_ago"]),
        step("topic_state", report["topic_state_min_ago"]),
        step("vocab_pool", report["vocab_pool_min_ago"]),
        step("text_check(тема)", report["text_check_topic_min_ago"]),
        step("dry_run", report["dry_run_min_ago"]),
        f"text_check покрыл {report['sentences_text_checked']}/{report['sentences']} предл.",
    ]
    for key, label in (
        ("not_vocab_checked", "без vocab_check"),
        ("not_in_word_paradigm", "формы не из word_paradigm"),
        ("written_duplicates", "⚠ записаны дубли ✓"),
        # Не ошибка сама по себе: ученик мог сказать «добавь». Но без его
        # ответа в беседе это нарушение шага 2 — проверять по журналу.
        ("written_sentence_only", "записаны ~-слова (только после ответа ученика)"),
    ):
        if report[key]:
            parts.append(f"{label}: {', '.join(report[key][:8])}"
                         + (f" +{len(report[key]) - 8}" if len(report[key]) > 8 else ""))
    return " · ".join(parts)
