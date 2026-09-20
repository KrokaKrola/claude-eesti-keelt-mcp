"""Запись карточек в Anki через аддон Anki MCP Server.

Своей SQLite в живую коллекцию не пишем никогда — почему, подробно
написано в collection.py. Пишет Anki, мы только доносим до него готовые
заметки по HTTP на localhost.

Модуль делится надвое:

  * ``build_topic`` разворачивает слово в заметки трёх подколод темы —
    чистые функции без сети, их и проверяют тесты;
  * ``call`` и ``write_topic`` разговаривают с аддоном.

Раскладка слова по заметкам раньше жила в голове модели: она читала
таблицу подписей из скилла и переписывала её руками в 45 заметок. Здесь
у неё нет выбора, а значит нет и способа ошибиться.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from .config import ADDON_META, NEW_EVERY, NEW_MAX, POS_TAGS
from .query import format_repeats

SUBDECKS = ("01. sõnad", "02. sõnad typein", "03. lause typein")
MODEL_WORD = "Estonian 3 Forms"
MODEL_TYPEIN = "Estonian Word Forms with TypeIn"

# Аддон режет пачку на 100 заметок; на слово приходится до четырёх
# заметок колоды 02 — по одной на форму, — она и упирается в потолок
# первой.
MAX_NOTES_PER_BATCH = 100
NOTES_PER_WORD_02 = 4
MAX_WORDS = MAX_NOTES_PER_BATCH // NOTES_PER_WORD_02

NOUN_LABELS = (
    "1-я форма (номинатив)",
    "2-я форма (генитив)",
    "3-я форма (партитив)",
    "4-я форма (партитив мн. ч.)",
)
VERB_LABELS = (
    "1-я форма (ma-инфинитив)",
    "2-я форма (da-инфинитив)",
    "3-я форма (mina-vorm)",
)

# Склоняются как существительное — и только у них есть четвёртая форма,
# партитив множественного числа. Остальное либо спрягается (глагол) и
# обходится тремя, либо не меняется вовсе и подписи форм не получает.
NOUN_LIKE = {"существительное", "прилагательное", "местоимение", "числительное"}

FORMS_RULE = (
    "4 non-empty forms for a declinable word (номинатив, генитив, партитив, "
    "партитив мн. ч.), 3 for a verb, 1 for an invariant word"
)


@dataclasses.dataclass
class Dose:
    """Сколько нового реально уехало в предложения колоды 03.

    Единственное место, где дозу вообще видно. text_check её посчитать
    не может: он получает плоский список предложений и не знает, какие
    из них колоды 03, — а доза живёт только там.
    """

    # Считали ли дозу вообще: без закрытия из server.py (тесты, чистая
    # сборка заметок) пустой список новых слов означает «не смотрели», а
    # не «модель ничего не положила».
    counted: bool = False
    words: int = 0
    expected: int = 0
    new: list[str] = dataclasses.field(default_factory=list)
    half: list[str] = dataclasses.field(default_factory=list)
    # Предложения с двумя новыми словами разом: два неизвестных в фразе,
    # которую человек переводит сам, — ровно та трудность, которой доза
    # и старается избежать.
    crowded: list[str] = dataclasses.field(default_factory=list)

    @property
    def short(self) -> bool:
        return len(self.new) < self.expected


def expected_dose(n_words: int) -> int:
    """Сколько новых слов ждём от темы в n_words слов."""
    return min(max(1, n_words // NEW_EVERY), NEW_MAX) if n_words else 0


class AnkiError(RuntimeError):
    """Аддон недоступен или отказался выполнять запрос."""


class NoteDataError(ValueError):
    """Данные слов не прошли проверку.

    Собираем все претензии разом: по одной модель чинит их кругами,
    каждый раз заново сочиняя весь список.
    """

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# --------------------------------------------------------------------------
# Разворачивание слова в заметки
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Batch:
    """Одна подколода: что за модель и какие заметки в неё кладём."""

    deck: str
    model: str
    notes: list[dict]


def topic_words(topic: str) -> list[str]:
    """'09. Vaba aeg' → ['aeg', 'vaba'].

    Номер отрезаем, слова опускаем в нижний регистр и сортируем: именно
    по этим тегам потом ищет vocab_pool, и порядок должен быть один и
    тот же у всех тем.
    """
    bare = re.sub(r"^\s*\d+[.)]?\s*", "", topic.strip())
    return sorted({w.lower() for w in re.findall(r"[^\W\d_]+", bare, re.UNICODE)})


def level_tag(course: str) -> str | None:
    """'kursuse eesti keel::A2' → 'A2'. Не угадали — тега уровня не будет."""
    tail = course.split("::")[-1].strip()
    return tail.upper() if re.fullmatch(r"[ABC][12]", tail, re.IGNORECASE) else None


def form_labels(pos: str, forms: list[str]) -> tuple[str, ...] | None:
    """Подписи форм — трёх, а у склоняемого слова четырёх. None — слово
    неизменяемое.

    Неизменяемость определяем по самим формам, а не по части речи:
    наречие с тремя одинаковыми формами и местоимение вроде 'kogu'
    ведут себя одинаково, а спрашивать об этом модель — лишний повод
    ошибиться.
    """
    if len({f.strip().lower() for f in forms}) == 1:
        return None
    labels = VERB_LABELS if pos == "глагол" else NOUN_LABELS
    return labels[: len(forms)]


def _pair(value: Any) -> tuple[str, str] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        et, ru = (str(x).strip() for x in value)
        if et and ru:
            return et, ru
    return None


def _validate(word: dict, seen: set[str], where: str, problems: list[str]) -> dict | None:
    """Проверяет одно слово. Возвращает нормализованный вид или None."""
    before = len(problems)

    et = str(word.get("word", "")).strip()
    if not et:
        problems.append(f"{where}: empty 'word'")
        et = "?"

    key = et.lower()
    if key in seen:
        problems.append(f"{where}: '{et}' appears twice in the list")
    seen.add(key)

    pos = POS_TAGS.get(str(word.get("pos", "")).strip().lower())
    if pos is None:
        problems.append(
            f"{where} ({et}): pos='{word.get('pos')}' — expected one of: "
            + ", ".join(sorted(set(POS_TAGS.values())))
        )

    ru = str(word.get("ru", "")).strip()
    if not ru:
        problems.append(f"{where} ({et}): empty 'ru'")

    forms = word.get("forms")
    if isinstance(forms, str):
        forms = [forms]
    if not isinstance(forms, list) or not forms or not all(str(f).strip() for f in forms):
        problems.append(f"{where} ({et}): forms — {FORMS_RULE}")
        forms = []
    else:
        forms = [str(f).strip() for f in forms]
        # Неизменяемость видно по самим формам, а не по части речи:
        # 'kogu' помечен местоимением, но партитива мн. ч. у него нет.
        invariant = len({f.lower() for f in forms}) == 1
        declinable = pos in NOUN_LIKE and not invariant
        if len(forms) == 1:
            forms = forms * 3
        elif len(forms) == 3 and declinable:
            problems.append(
                f"{where} ({et}): forms — got 3, a declinable word needs 4: "
                "номинатив, генитив, партитив, партитив мн. ч."
            )
            forms = []
        elif len(forms) == 4 and not declinable:
            problems.append(
                f"{where} ({et}): forms — got 4, but the 4th form (партитив "
                "мн. ч.) belongs to a declinable word only; a verb takes 3"
            )
            forms = []
        elif len(forms) not in (3, 4):
            problems.append(f"{where} ({et}): forms — got {len(forms)}, need {FORMS_RULE}")
            forms = []

    raw_ex = word.get("ex")
    if isinstance(raw_ex, list) and len(raw_ex) in (1, 3):
        pairs = [_pair(p) for p in raw_ex]
        if None in pairs:
            problems.append(f"{where} ({et}): ex — each example is a pair ['estonian', 'russian'], both non-empty")
            ex = []
        else:
            ex = pairs * 3 if len(pairs) == 1 else pairs
    else:
        problems.append(f"{where} ({et}): ex — 3 pairs ['estonian', 'russian'] (or 1 for an invariant word)")
        ex = []

    sentence = _pair(word.get("sentence"))
    if sentence is None:
        problems.append(f"{where} ({et}): sentence — a pair ['estonian', 'russian'] for deck 03")

    if len(problems) > before:
        return None
    return {"word": et, "pos": pos, "ru": ru, "forms": forms, "ex": ex, "sentence": sentence}


def build_topic(
    course: str,
    topic: str,
    words: list[dict],
    form_in_example: Callable[[str, str], bool] | None = None,
    novel_words: Callable[[str, set[str]], tuple[list[str], list[str]]] | None = None,
) -> tuple[list[Batch], list[str], Dose]:
    """Слова → три пачки заметок, предупреждения и отчёт о дозе.

    Предупреждение — не отказ: 4% словарных карточек в коллекции уже
    нарушают правило «пример содержит форму» вполне законно (у глагола
    'algama' пример идёт с 'algab'), так что блокировать по нему нельзя.
    """
    if not words:
        raise NoteDataError(["words list is empty"])
    if len(words) > MAX_WORDS:
        raise NoteDataError(
            [f"{len(words)} words at once — the add-on takes at most {MAX_WORDS} "
             f"(deck 02 makes up to {NOTES_PER_WORD_02} notes per word, batch cap "
             f"{MAX_NOTES_PER_BATCH}). "
             "Split into several calls."]
        )

    problems: list[str] = []
    seen: set[str] = set()
    clean = [
        ok
        for i, w in enumerate(words)
        if (ok := _validate(w if isinstance(w, dict) else {}, seen, f"word {i + 1}", problems))
    ]
    if problems:
        raise NoteDataError(problems)

    shared = [t for t in (level_tag(course),) if t] + topic_words(topic)
    root = f"{course}::{topic}"
    warnings: list[str] = []
    dose = Dose(
        counted=novel_words is not None,
        words=len(clean),
        expected=expected_dose(len(clean)),
    )
    # Слова самой темы новыми не считаются: их сейчас и учат. Лемму из
    # них делает вызывающий — здесь про морфологию ничего не знают.
    taught = {w["word"].strip().lower() for w in clean}

    notes01: list[dict] = []
    notes02: list[dict] = []
    notes03: list[dict] = []

    for w in clean:
        forms, ex, pos = w["forms"], w["ex"], w["pos"]
        labels = form_labels(pos, forms)

        if form_in_example is not None:
            # zip обрывается на трёх парах примеров: четвёртую форму
            # проверять не на чем, примера у неё нет.
            for i, (form, (et, _)) in enumerate(zip(forms, ex), start=1):
                if not form_in_example(form, et):
                    warnings.append(f"{w['word']}: form {i} ({form}) is absent from its example '{et}'")

        # Form4 кладём всегда: у глагола и неизменяемого слова её нет, и
        # поле должно уехать в Anki пустым, а не пропасть из заметки —
        # шаблон показывает четвёртую форму только когда она заполнена.
        fields01 = {"Russian": w["ru"], "Add Reverse": "1", "Form4": ""}
        for i, form in enumerate(forms, start=1):
            fields01[f"Form{i}"] = form
        for i, (et, ru) in enumerate(ex, start=1):
            fields01[f"Ex{i}"] = et
            fields01[f"Ex{i}ru"] = ru
        notes01.append({"fields": fields01, "tags": sorted({*shared, pos})})

        # Одна заметка на форму, слово за словом: Anki показывает новые
        # карточки в порядке добавления, и три формы должны попасться
        # подряд, а не с разбросом в неделю.
        # Четвёртая форма идёт без примера — её спрашивают как есть.
        slots = (
            [
                (label, form, ex[i] if i < len(ex) else ("", ""))
                for i, (label, form) in enumerate(zip(labels, forms))
            ]
            if labels
            else [("", forms[0], ex[0])]
        )
        for label, form, (et, ru) in slots:
            notes02.append({
                "fields": {
                    "Russian": w["ru"],
                    "Form": label,
                    "Answer": form.lower(),
                    "Example_ET": et,
                    "Example_RU": ru,
                },
                "tags": sorted({*shared, pos}),
            })

        et, ru = w["sentence"]
        if novel_words is not None:
            new, half = novel_words(et, taught)
            dose.new.extend(x for x in new if x not in dose.new)
            dose.half.extend(x for x in half if x not in dose.half)
            if len(new) > 1:
                dose.crowded.append(et)
        notes03.append({
            "fields": {
                "Russian": ru,
                "Form": "",
                "Answer": et,
                "Example_ET": "",
                "Example_RU": "",
            },
            "tags": sorted({*shared, "lause"}),
        })

    batches = [
        Batch(f"{root}::{SUBDECKS[0]}", MODEL_WORD, notes01),
        Batch(f"{root}::{SUBDECKS[1]}", MODEL_TYPEIN, notes02),
        Batch(f"{root}::{SUBDECKS[2]}", MODEL_TYPEIN, notes03),
    ]
    return batches, warnings, dose


# --------------------------------------------------------------------------
# Разговор с аддоном
# --------------------------------------------------------------------------


def endpoint() -> str:
    """Адрес аддона берём из его же настроек: порт там меняется мышкой,
    и хардкод разошёлся бы с реальностью молча."""
    if url := os.environ.get("ANKI_MCP_URL"):
        return url
    host, port, path = "127.0.0.1", 3141, ""
    try:
        cfg = json.loads(ADDON_META.read_text()).get("config", {})
        host = cfg.get("http_host") or host
        port = cfg.get("http_port") or port
        path = cfg.get("http_path") or ""
    except (OSError, ValueError):
        pass
    return f"http://{host}:{port}/{path.lstrip('/')}"


def _unwrap(body: str) -> dict:
    """Ответ приходит как SSE: 'event: message' и строка 'data: {...}'."""
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise AnkiError(f"add-on replied off-protocol: {body[:200]}")


def rpc(method: str, params: dict, timeout: float = 60.0) -> dict:
    """Запрос к аддону по JSON-RPC. Ошибки переводим в понятную строку —
    модель увидит её вместо стектрейса и сможет сказать пользователю,
    что делать."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        endpoint(),
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AnkiError(
            f"cannot reach the add-on at {endpoint()}: {exc}. "
            "Is Anki running? Nothing was written."
        ) from exc

    message = _unwrap(body)
    if "error" in message:
        raise AnkiError(f"{method}: {message['error'].get('message', message['error'])}")
    return message.get("result", {})


def call(tool: str, args: dict, timeout: float = 60.0) -> dict:
    """Вызов инструмента аддона."""
    result = rpc("tools/call", {"name": tool, "arguments": args}, timeout)
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in result.get("content", []))
        raise AnkiError(f"{tool}: {text or 'add-on returned an error with no text'}")
    # structuredContent и content дублируют друг друга — берём то, что
    # уже разобрано, и лишнюю копию дальше не тащим.
    if (structured := result.get("structuredContent")) is not None:
        return structured
    for chunk in result.get("content", []):
        if chunk.get("type") == "text":
            return json.loads(chunk["text"])
    raise AnkiError(f"{tool}: empty response")


def deck_names() -> set[str]:
    """Все колоды Anki. Ответ большой, но он остаётся внутри сервера и
    в контекст модели не попадает."""
    return {d["name"] for d in call("list_decks", {}).get("decks", [])}


def _nearest_existing(deck: str, existing: set[str]) -> str:
    """Ближайший существующий предок: create_deck аддона умеет только
    два уровня, поэтому глубокую ветку приходится создавать переносом
    карточек. Заметки временно ложатся сюда."""
    parts = deck.split("::")
    for i in range(len(parts) - 1, 0, -1):
        if (candidate := "::".join(parts[:i])) in existing:
            return candidate
    return "Default"


def _card_ids(note_ids: list[int]) -> list[int]:
    info = call("notes_info", {"notes": note_ids, "include_fields": [], "excerpt_chars": 1})
    return [cid for note in info.get("notes", []) for cid in note.get("cards", [])]


def _add(batch: Batch, existing: set[str]) -> tuple[int, list[str]]:
    """Кладёт пачку в свою подколоду, создавая её при необходимости.

    Возвращает число созданных заметок и список отказов от аддона.
    """
    if not batch.notes:
        return 0, []

    target_exists = batch.deck in existing
    dest = batch.deck if target_exists else _nearest_existing(batch.deck, existing)

    result = call("add_notes", {
        "deck_name": dest,
        "model_name": batch.model,
        "notes": batch.notes,
        # Дубли отсеяны раньше через vocab_check; здесь флаг только
        # мешает Anki зарубить три формы одного слова как повтор.
        "allow_duplicate": True,
    })

    created = [r for r in result.get("results", []) if r.get("status") == "created"]
    rejected = [
        f"note {r['index'] + 1}: {r.get('error') or r.get('reason', 'rejected')}"
        for r in result.get("results", [])
        if r.get("status") != "created"
    ]

    if not target_exists and created:
        # change_deck создаёт путь любой глубины — это единственный
        # способ получить четырёхуровневую колоду через аддон.
        call("card_management", {
            "params": {
                "action": "change_deck",
                "card_ids": _card_ids([r["note_id"] for r in created]),
                "deck": batch.deck,
            }
        })
        existing.add(batch.deck)

    return len(created), rejected


def write_topic(
    course: str,
    topic: str,
    words: list[dict],
    form_in_example: Callable[[str, str], bool] | None = None,
    novel_words: Callable[[str, set[str]], tuple[list[str], list[str]]] | None = None,
    repeat_check: Callable[[list[str], set[str]], list] | None = None,
    dry_run: bool = False,
) -> tuple[str, Dose]:
    """Полный проход: проверить, разложить, создать колоды, записать.

    Отчёт о дозе отдаём вторым значением, а не только строкой: вызывающий
    пишет его в лог, иначе понять постфактум, соблюдалась доза или нет,
    можно было бы только перечитывая ответы модели глазами.
    """
    batches, warnings, dose = build_topic(
        course, topic, words, form_in_example, novel_words
    )
    # Считаем и здесь, а не только в text_check: тот проверяет черновик,
    # и его могут не вызвать вовсе, — а сюда приходит ровно то, что
    # ляжет в колоду.
    repeats = repeat_check(*_sentences(batches)) if repeat_check else []

    head = f"{course}::{topic}"
    lines: list[str] = []

    if dry_run:
        lines.append(f"{head} — dry run, nothing written")
        for batch in batches:
            lines.append(f"  {batch.deck.split('::')[-1]:<18} {len(batch.notes)}")
        example = batches[0].notes[0]
        lines.append("  first note of 01: " + json.dumps(example, ensure_ascii=False))
        return (
            "\n".join(
                lines + _dose_lines(dose) + _repeat_lines(repeats)
                + _warning_lines(warnings)
            ),
            dose,
        )

    existing = deck_names()
    total = 0
    for batch in batches:
        name = batch.deck.split("::")[-1]
        try:
            created, rejected = _add(batch, existing)
        except AnkiError as exc:
            # Дальше не идём: половина темы лучше, чем половина с дырой
            # посередине, а Ctrl+Z в Anki снимает записанное целиком.
            lines.append(f"  {name:<18} ERROR: {exc}")
            lines.append("  remaining decks untouched; Ctrl+Z in Anki undoes what was written")
            return "\n".join([f"{head} — {total} notes written, then failed"] + lines), dose
        total += created
        mark = "✓" if not rejected else "⚠"
        lines.append(f"  {name:<18} {created} {mark}")
        lines.extend(f"      {r}" for r in rejected)

    return (
        "\n".join(
            [f"{head} — {total} notes"] + lines
            + _dose_lines(dose) + _repeat_lines(repeats) + _warning_lines(warnings)
        ),
        dose,
    )


def _sentences(batches: list[Batch]) -> tuple[list[str], set[str]]:
    """Все предложения пачки и слова, которым тема учит.

    Целевые слова из счёта повторов надо вычесть: пример к слову обязан
    его содержать, и без этого каждое слово темы выглядело бы повтором
    ровно четыре раза.
    """
    texts: list[str] = []
    for note in batches[0].notes:
        # У несклоняемого слова один пример размножен на три слота —
        # иначе каждое его слово выглядело бы троекратным повтором.
        texts += dict.fromkeys(
            note["fields"][f"Ex{i}"] for i in (1, 2, 3) if note["fields"].get(f"Ex{i}")
        )
    texts += [note["fields"]["Answer"] for note in batches[2].notes]
    taught = {
        note["fields"]["Form1"].strip().lower() for note in batches[0].notes
    }
    return texts, taught


def _repeat_lines(repeats: list) -> list[str]:
    if not repeats:
        return []
    return [""] + format_repeats(repeats, with_topic=True)


def _dose_lines(dose: Dose) -> list[str]:
    """Строка дозы — не украшение отчёта, а единственный способ увидеть,
    что предложения колоды 03 опять собрали целиком из известного."""
    if not dose.counted:
        return []
    got = ", ".join(dose.new) if dose.new else "none"
    mark = " ⚠ short" if dose.short else ""
    lines = [
        "",
        f"deck-03 new words: {len(dose.new)} of {dose.expected} expected"
        f"{mark} — {got}",
    ]
    if dose.half:
        lines.append(
            "  half-known (met only inside examples, do not count): "
            + ", ".join(dose.half)
        )
    for et in dose.crowded:
        lines.append(f"  ⚠ two new words in one sentence: '{et}' — one at most")
    if dose.short:
        lines.append(
            "  a deck-03 sentence built only from known words teaches nothing "
            "beyond its target word; put one ordinary A2 word into every "
            f"{NEW_EVERY}rd of them, everything else around it known"
        )
    return lines


def _warning_lines(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    return ["", "warnings (did not block the write):"] + [f"  {w}" for w in warnings]
