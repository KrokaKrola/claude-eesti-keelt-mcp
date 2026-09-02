"""Логика инструментов: проверка слов, палитра, покрытие текста.

Два яруса словаря. Палитра — первые поля словарных карточек (колода
01. sõnad), из них Claude сочиняет. Второй ярус — всё остальное, что
человек видел: другие формы с карточек, слова из примеров и дриллов.
Сочинять из них не предлагаем, но известными считаем, иначе проверка
ругалась бы на слова из его же карточек.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from .config import FUNCTION_WORDS
from .lemma import Lemmatizer
from .parse import words

KNOWN, SEEN, UNKNOWN = "known", "seen", "unknown"

# Типы заметок словарной карточки: три формы слова в одном поле-заголовке.
WORD_CARD_NOTETYPES = ("Estonian 3 Forms", "Estonian Word Forms")


def _tag_words(tags: list[str] | set[str]) -> set[str]:
    """Тег темы и слова из него.

    Соглашение об именовании со временем поменялось: старые темы помечены
    одним тегом с подчёркиваниями ('tule_mulle_külla', 'kodused_tööd'),
    новые — словами ('elu', 'liikumine', 'on'). Скилл считает теги из
    имени колоды по второму правилу, поэтому у старых тем совпадений не
    было вовсе и группа «по теме» молча приезжала пустой. Сравниваем
    по словам, и обе схемы сходятся.
    """
    out = set()
    for tag in tags:
        low = tag.strip().lower()
        if low:
            out.add(low)
            out.update(part for part in low.split("_") if part)
    return out
MARK = {KNOWN: "✓", SEEN: "~", UNKNOWN: "✗"}

# Откуда в теме взялось слово. Слот заметки честнее колоды: старые темы
# лежат в других форматах, а Form1/2/3 — это карточка трёх форм всегда.
SLOT_MARK = {
    "Form1": "3F", "Form2": "3F", "Form3": "3F",  # карточка трёх форм
    "Answer": "ty",                               # typein, одна форма
    "example": "ex",                              # только внутри примера
}
MARK_ORDER = ["3F", "ty", "other", "ex"]


@dataclass
class Hit:
    query: str
    status: str
    lemma: str | None = None
    ru: str | None = None
    level: str | None = None
    pos: str | None = None
    topics: tuple[str, ...] = ()
    forms: tuple[str, ...] = ()
    decks: tuple[str, ...] = ()


class Vocab:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.lem = Lemmatizer(con)

    # --- поиск -----------------------------------------------------------
    def _row(self, lemma: str) -> Hit | None:
        row = self.con.execute(
            "select lemma, ru, level, pos, topics, forms, n_word_notes"
            " from lemma where lemma = ?",
            (lemma,),
        ).fetchone()
        if not row:
            return None
        lem, ru, level, pos, topics, forms, n_word = row
        decks = self._where(lem)
        return Hit(
            query=lemma,
            status=KNOWN if n_word else SEEN,
            lemma=lem, ru=ru, level=level, pos=pos,
            topics=tuple(json.loads(topics)), forms=tuple(json.loads(forms)),
            decks=tuple(dict.fromkeys(decks)),
        )

    def _where(self, lemma: str) -> list[str]:
        """Тема плюс пометки, какого рода запись в ней лежит.

        Одной темы мало: «слово уже в 09. Vaba aeg» может значить и
        полноценную карточку трёх форм, и случайное попадание в чужой
        пример. Решения это разные, а по имени темы их не различить.
        """
        # Держим сильнейшую пометку темы, а не все: у слова с карточкой
        # трёх форм почти всегда есть и typein, и попадания в примеры —
        # перечислять их значит платить токенами за то, что и так ясно.
        best: dict[str, int] = {}
        for deck, slot in self.con.execute(
            "select distinct e.deck, f.slot from form f"
            " join entry e on e.note_id = f.note_id"
            " where f.lemma = ? and e.is_phrase = 0",
            (lemma,),
        ):
            topic = topic_of(deck)
            rank = MARK_ORDER.index(SLOT_MARK.get(slot, "other"))
            best[topic] = min(rank, best.get(topic, len(MARK_ORDER)))
        # Тема с карточкой трёх форм — главный ответ на вопрос «дубль ли
        # это», и обрезка списка не должна её съесть.
        return [
            f"{topic} {MARK_ORDER[rank]}"
            for topic, rank in sorted(best.items(), key=lambda kv: kv[1])
        ]

    def lookup(self, word: str) -> Hit:
        """Точная форма → лемма формы → лемма по морфологии."""
        raw = word.strip().lower()
        if not raw:
            return Hit(query=word, status=UNKNOWN)

        if " " in raw:  # фразовый глагол — самостоятельная единица
            hit = self._row(raw)
            if hit:
                hit.query = word
                return hit
            return Hit(query=word, status=UNKNOWN)

        # Одна форма может висеть на разных леммах: внутри предложения
        # слово разбирается без тега части речи, и 'salli' уходит в
        # глагол sallima, хотя словарная карточка — существительное
        # «шарф». Впереди идёт лемма со словарными карточками.
        candidates = [
            lemma for (lemma,) in self.con.execute(
                "select f.lemma from form f"
                " left join lemma l on l.lemma = f.lemma"
                " where f.form = ?"
                " group by f.lemma"
                " order by coalesce(max(l.n_word_notes), 0) desc, f.lemma",
                (raw,),
            )
        ]
        candidates.append(raw)
        candidates.extend(self.lem.lemmas(raw))

        for candidate in dict.fromkeys(candidates):
            hit = self._row(candidate)
            if hit:
                hit.query = word
                return hit
        return Hit(query=word, status=UNKNOWN)

    # --- палитра ---------------------------------------------------------
    def pool(
        self,
        level: str | None = None,
        topics: list[str] | None = None,
        pos: str | None = None,
        limit: int | None = None,
        stale_days: int = 14,
        stale_hint: int = 40,
    ) -> tuple[list[str], list[str], int, list[str]]:
        """(слова по теме, ядро, отброшено пределом, подсказка).

        Палитра — первые поля словарных карточек, как их написал человек:
        номинатив у существительных, ma-инфинитив у глаголов. Через
        морфологию их не гоняем. Карточка и так содержит словарную форму,
        а анализатор без контекста умеет только испортить её: 'Lisaks'
        превращался в 'lisa', 'Kindad' — в 'kinnas', карточка 'Harjutama'
        заводила вторую лемму harjuma (привыкать — другой глагол), а
        фразовый глагол растраивался на 'kaasa elama', 'kaasa elada' и
        'elan kaasa'. Морфология остаётся там, где она незаменима, —
        в lookup и check_sentence, которые разбирают произвольный текст.

        Обратная сторона: слова, встреченные только в предложениях и
        грамматических дриллах (jooma, aitama, helistama), в палитру не
        попадают — у них нет словарной карточки. Знакомыми они при этом
        остаются: покрытие считается по всему индексу.

        Предела по умолчанию нет. Любое число здесь прячет часть словаря,
        причём всегда одну и ту же — а словарь растёт на ~240 слов в месяц,
        так что любая константа устаревает за недели. Если предел всё же
        передан и он сработал, отброшенное число возвращается наружу,
        чтобы усечение не было молчаливым.

        Фильтр по теме сам по себе не работает: медиана — 5 слов на тему,
        а у новой темы совпадений нет вовсе, и палитра выходила пустой.
        Поэтому к тематическим словам добавляем ядро — самые частотные
        слова этого уровня независимо от темы. Из ядра и строится
        обычная фраза, тема даёт существительные.

        Четвёртым идёт подсказка: ходовые слова, которые давно не попадали
        в новые карточки. Не исключаем ничего — просто показываем, чем
        разбавить. Исключать «свежие» слова нельзя: в этой корзине сидят
        olema, meeldima, tegema, без которых фразу не построишь.
        """
        marks = ",".join("?" * len(WORD_CARD_NOTETYPES))
        sql = [f"select headword, topics from entry where notetype in ({marks})"]
        args: list = list(WORD_CARD_NOTETYPES)
        if level:
            # Карточка без тега уровня — всё равно выученное слово, и
            # прятать её нельзя: в старой колоде «03. minu kodu» без тега
            # лежит вся бытовая лексика (laud, kapp, köök, lamp), а это
            # ровно те существительные, из которых строится фраза A2.
            sql.append("and (level = ? or level is null)")
            args.append(level.lower())
        if pos:
            sql.append("and pos = ?")
            args.append(pos)
        # lower() в SQLite знает только латиницу и оставил бы 'Ämber'
        # отдельным словом рядом с 'ämber' — приводим регистр в Python.
        seen: dict[str, set[str]] = {}
        for headword, blob in self.con.execute(" ".join(sql), args):
            word = headword.strip().lower()
            if word:
                seen.setdefault(word, set()).update(_tag_words(json.loads(blob)))

        # Ходовость и «когда попадалось в последний раз» берём из индекса
        # лемм по совпадению строки. Для 96% слов карточки лемма совпадает
        # с первым полем; остальным сигнал просто нулевой, и они уезжают
        # в конец ранжирования — на состав палитры это не влияет.
        #
        # Ранжируем по ходовости: n_notes — сколько раз слово встречается
        # во всей коллекции, включая примеры внутри предложений. Это и есть
        # «насколько слово рабочее». Интервал повторения разбивает ничьи.
        #
        # Два тупика, которые пришлось пройти. Число карточек не годится:
        # у 457 слов их ровно четыре, и сортировка сваливалась в алфавит —
        # слова на ä и õ не попадали в выдачу никогда. Чистый интервал тоже
        # не годится: наверх всплывают порядковые числительные вроде
        # «üheksasajas» — они лёгкие, поэтому интервал огромный, но фразу
        # из них не построишь.
        meta = {
            lemma: (n_notes, strength, last_used)
            for lemma, n_notes, strength, last_used in self.con.execute(
                "select lemma, n_notes, strength, last_used from lemma"
            )
        }
        ranked = sorted(
            seen, key=lambda w: (-meta.get(w, (0, 0.0, None))[0],
                                 -meta.get(w, (0, 0.0, None))[1], w)
        )

        wanted = _tag_words(topics or [])
        topical, rest = [], []
        cutoff = (time.time() - stale_days * 86400) * 1000
        stale: list[tuple[int, str]] = []
        for word in ranked:
            (topical if wanted & seen[word] else rest).append(word)
            n_notes, _, last_used = meta.get(word, (0, 0.0, None))
            if last_used and last_used < cutoff:
                stale.append((n_notes, word))
        stale.sort(key=lambda x: (-x[0], x[1]))
        hint = sorted(word for _, word in stale[:stale_hint])

        if limit is None:
            return sorted(topical), sorted(rest), 0, hint
        kept_topical = topical[:limit]
        kept_core = rest[: max(0, limit - len(kept_topical))]
        dropped = (len(topical) - len(kept_topical)) + (len(rest) - len(kept_core))
        return sorted(kept_topical), sorted(kept_core), dropped, hint

    # --- покрытие --------------------------------------------------------
    def _is_function(self, word: str) -> bool:
        """Служебное слово. Список закрывает частотные формы olema и ei,
        а коды Vabamorf — послелоги (K) и союзы (J), которых в словаре
        и не должно быть: это грамматика, а не лексика."""
        analyses = self.lem.analyses(word)
        if {lem for lem, _ in analyses} & FUNCTION_WORDS or word in FUNCTION_WORDS:
            return True
        codes = {code for _, code in analyses if code}
        return bool(codes) and codes <= {"K", "J"}

    def check_sentence(
        self, text: str, targets: set[str] | None = None
    ) -> tuple[int, list[str], list[str]]:
        """(процент покрытия, незнакомые леммы, знаменательные слова).

        targets — целевые слова карточек: они новые по определению
        и бюджет не тратят.
        """
        targets = targets or set()
        content: list[str] = []
        unknown: list[str] = []
        for word in words(text):
            low = word.lower()
            if self._is_function(low):
                continue
            if targets and ({low} | self.lem.lemmas(low)) & targets:
                continue
            content.append(low)
            if self.lookup(low).status == UNKNOWN:
                base = self.lem.lemma(low)
                if base not in unknown:
                    unknown.append(base)
        if not content:
            return 100, [], []
        covered = len(content) - len(
            [w for w in content if self.lookup(w).status == UNKNOWN]
        )
        return round(100 * covered / len(content)), unknown, content


SUBDECKS = ("01. sõnad", "02. sõnad typein", "03. lause typein")


def topic_of(deck: str) -> str:
    """Из пути колоды достаём тему: подколода 01/02/03 сама по себе
    ('01. sõnad') ничего не говорит, решение принимается по теме."""
    parts = deck.split("::")
    if len(parts) > 1 and parts[-1] in SUBDECKS:
        parts.pop()
    return parts[-1] if parts else deck


def topic_state(con: sqlite3.Connection, topic: str) -> str:
    """Заменяет связку list_decks + find_notes одним вызовом."""
    like = f"%{topic.strip()}%"
    rows = con.execute(
        "select deck, count(*) from entry where deck like ? group by 1 order by 1",
        (like,),
    ).fetchall()
    if not rows:
        return (
            f"topic '{topic}' is not in the index — either it does not exist "
            "yet, or its deck is out of scope"
        )
    root = rows[0][0].split("::")
    while root and root[-1] in SUBDECKS:
        root.pop()
    root_path = "::".join(root)

    lines = [root_path]
    present = {r[0].split("::")[-1]: r[1] for r in rows}
    for sub in SUBDECKS:
        n = present.get(sub)
        lines.append(f"  {sub:18} {n if n is not None else '—'}")
    # По леммам, а не по headword: в подколоде 02 каждая форма — своя
    # заметка, и список из заголовков выдавал 'Alates' и 'alates', 'sall'
    # и 'salli' как разные слова, втрое завышая счётчик темы.
    heads = [
        h for (h,) in con.execute(
            "select distinct f.lemma from form f"
            " join entry e on e.note_id = f.note_id"
            " where e.deck like ? and e.is_phrase = 0 and f.slot <> 'example'"
            " order by f.lemma",
            (like,),
        )
    ]
    if heads:
        # Полный список на большой теме — это под тысячу токенов.
        # Для проверки конкретных слов есть vocab_check.
        shown = heads[:60]
        tail = f" … +{len(heads) - len(shown)} more" if len(heads) > len(shown) else ""
        lines.append(f"words ({len(heads)}): " + ", ".join(shown) + tail)
    return "\n".join(lines)


# --- компактный вывод: экономия токенов — смысл всего проекта -----------


def format_check(hits: list[Hit]) -> str:
    width = max((len(h.query) for h in hits), default=1)
    lines = []
    for h in hits:
        if h.status == UNKNOWN:
            lines.append(f"{h.query:<{width}} ✗")
            continue
        tags = ",".join(h.topics[:3])
        meta = " ".join(x for x in (h.level, tags) if x)
        forms = "/".join(h.forms[:4])
        # Список тем — основа отчёта о дублях: по нему решают, настоящий
        # это дубль или слово пришло из другой темы. Обрезаем ради токенов,
        # но молча обрезать нельзя — 23% слов живут больше чем в двух темах.
        shown = "; ".join(h.decks[:2])
        extra = len(h.decks) - 2
        where = f"  [{shown}{f'; +{extra} more' if extra > 0 else ''}]" if h.decks else ""
        lines.append(
            f"{h.query:<{width}} {MARK[h.status]} {meta}  {forms}{where}".rstrip()
        )
    known = sum(1 for h in hits if h.status == KNOWN)
    seen = sum(1 for h in hits if h.status == SEEN)
    missing = [h.query for h in hits if h.status == UNKNOWN]
    lines.append(f"— word card {known}, sentences only {seen}, missing {len(missing)}")
    if missing:
        lines.append("missing: " + ", ".join(missing))
    return "\n".join(lines)


def format_pool(
    topical: list[str],
    core: list[str],
    dropped: int = 0,
    stale_hint: list[str] | None = None,
) -> str:
    if not topical and not core:
        return "nothing matches these filters — drop level or pos"
    lines = []
    if topical:
        lines.append(f"topic ({len(topical)}): " + " · ".join(topical))
    if core:
        label = "rest of vocabulary" if topical else "vocabulary"
        lines.append(f"{label} ({len(core)}): " + " · ".join(core))
    if stale_hint:
        lines.append(
            f"not used lately ({len(stale_hint)}): " + " · ".join(stale_hint)
        )
        lines.append(
            "— a nudge for variety, not a limit: the full vocabulary is above"
        )
    if dropped:
        lines.append(
            f"⚠ truncated by limit: {dropped} words hidden. Drop limit or "
            "narrow with pos — otherwise these words never reach your examples."
        )
    return "\n".join(lines)


def format_text_check(
    results: list[tuple[int, list[str], list[str]]], min_coverage: int = 70
) -> str:
    """Мягкое правило: одно новое слово допустимо всегда, дальше смотрим
    на покрытие. Жёсткий потолок «ровно одно новое» ломал предложения —
    естественную фразу не всегда можно собрать из готового."""
    lines = []
    candidates: list[str] = []
    low = 0
    for i, (pct, new, _content) in enumerate(results, 1):
        for w in new:
            if w not in candidates:
                candidates.append(w)
        ok = pct >= min_coverage or len(new) <= 1
        low += not ok
        shown = ", ".join(new) if new else "—"
        lines.append(f"{i:<3}{pct:>4}%  {shown:<34} {'ok' if ok else 'low'}")
    avg = round(sum(r[0] for r in results) / len(results)) if results else 100
    tail = f"average coverage {avg}%"
    if low:
        tail += f" · below {min_coverage}%: {low} of {len(results)}"
    if candidates:
        tail += "\nnew words (candidates for the next topic): " + ", ".join(candidates)
    lines.append(tail)
    return "\n".join(lines)
