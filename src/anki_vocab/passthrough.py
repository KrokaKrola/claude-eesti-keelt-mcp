"""Ленивая выдача инструментов аддона.

Аддон умеет 41 вещь, и его схемы весят 15 500 токенов. Они лежат в
контексте каждого разговора, даже когда речь не про Anki: модель видит
только то, что приехало в запросе, поэтому список инструментов обязан
приехать заранее.

Здесь он не приезжает. В контексте живут две маленькие ручки: одна
показывает оглавление и по требованию достаёт схему нужного
инструмента, вторая его вызывает. Разведка стоит ~1100 токенов и
случается только в тех разговорах, где до Anki реально дошло дело.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import DATA_DIR
from .write import AnkiError, call, rpc

CACHE = DATA_DIR / "anki_tools.json"

# Ответ аддона на большой коллекции доходит до 30 КБ (list_decks на 115
# колодах). Режем с явной пометкой: молча обрезанный список хуже, чем
# честно короткий.
MAX_RESULT = 12000

# Инструменты аддона, которые меняют коллекцию. Перечислять их поимённо
# нельзя: аддон добавляет новые, и список устаревал бы молча — а молчит
# он ровно там, где запись прошла мимо topic_write.
WRITE_PREFIXES = (
    "add_", "answer_", "bury", "change_", "clear_", "create_", "delete_",
    "forget", "gui_", "import_", "move_", "remove_", "reposition", "set_",
    "suspend", "sync", "unbury", "unsuspend", "update_",
)


def mutating(tool: str) -> bool:
    return tool.startswith(WRITE_PREFIXES)


# Схем больше нескольких сразу не отдаём: широкий шаблон вроде 'a' иначе
# вывалит все 41 и сведёт всю затею на нет.
MAX_SCHEMAS = 4


def _fetch() -> list[dict]:
    return rpc("tools/list", {}, timeout=20.0).get("tools", [])


def catalog(refresh: bool = False) -> tuple[list[dict], str]:
    """Оглавление аддона и пометка, откуда оно взялось.

    Живой список точнее — аддон обновляется и переименовывает действия
    (forget → forget_cards уже случалось). Но с закрытым Anki оглавление
    тоже нужно, поэтому последний удачный ответ лежит на диске.
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
    """Точное имя выигрывает у подстроки: 'sync' не должен приводить
    заодно и sync_state, если инструмент с таким именем есть."""
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
    """Ответ аддона едет с отступами и вторым экземпляром в
    structuredContent. Мы уже взяли разобранную копию, осталось не
    печатать её красиво."""
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
