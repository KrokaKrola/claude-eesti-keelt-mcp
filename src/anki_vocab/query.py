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
from dataclasses import dataclass, field

from .config import FUNCTION_WORDS, HALF_KNOWN_MAX
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

# Потолок страницы долга. Смысл проекта — экономия контекста, а инструмент
# отдаёт limit наружу: 400 слов — это 8k токенов одним ответом.
DEBT_PAGE_MAX = 50


@dataclass
class Palette:
    """Выдача vocab_pool. Полей уже пять, и позиционный кортеж на месте
    вызова читался как (a, b, _, _)."""

    topical: list[str]
    core: list[str]
    dropped: int = 0
    stale: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)


@dataclass
class Novel:
    """Новые слова одного предложения, парами (лемма, форма в тексте)."""

    unknown: list[tuple[str, str]] = field(default_factory=list)
    half: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Debt:
    """Слово, которое человек читает в своих же примерах, но карточки
    ему так и не завёл."""

    word: str
    times: int
    example: str | None = None


@dataclass
class DebtPage:
    """Страница долга вместе со смещением, с которого её печатать.

    Смещение отдаём наружу, а не берём у вызывающего: границы вправляются
    внутри, и с сырым offset=-5 нумерация уехала бы в «#-4».
    """

    items: list[Debt]
    offset: int = 0
    more: bool = False
    skipped: int = 0


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
    times: int = 0


class Vocab:
    def __init__(self, con: sqlite3.Connection):
        self.con = con
        self.lem = Lemmatizer(con)

    # --- поиск -----------------------------------------------------------
    def _row(self, lemma: str) -> Hit | None:
        row = self.con.execute(
            "select lemma, ru, level, pos, topics, forms, n_word_notes, n_notes"
            " from lemma where lemma = ?",
            (lemma,),
        ).fetchone()
        if not row:
            return None
        lem, ru, level, pos, topics, forms, n_word, n_notes = row
        decks = self._where(lem)
        return Hit(
            query=lemma,
            status=KNOWN if n_word else SEEN,
            lemma=lem, ru=ru, level=level, pos=pos,
            topics=tuple(json.loads(topics)), forms=tuple(json.loads(forms)),
            decks=tuple(dict.fromkeys(decks)), times=n_notes,
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
        weak_hint: int = 30,
        mature_ivl: int = 21,
    ) -> Palette:
        """Слова по теме, ядро, и две подсказки — забытые и залежавшиеся.

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

        Дальше идут две подсказки, и они отвечают на разные вопросы.
        «Давно не использовал» — ходовые слова, которые давно не попадали
        в новые карточки; это про однообразие примеров. «Трудные» —
        слова, которые человек на повторах забывал и до зрелого интервала
        так и не довёл; это про его память, и новое предложение с таким
        словом даёт лишнюю встречу ровно там, где она нужна.

        Не исключаем ничего — просто показываем, чем разбавить. Исключать
        «свежие» слова нельзя: в этой корзине сидят olema, meeldima,
        tegema, без которых фразу не построишь.
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
        blank = (0, 0.0, 0, None)
        meta = {
            lemma: (n_notes, strength, lapses, last_used)
            for lemma, n_notes, strength, lapses, last_used in self.con.execute(
                "select lemma, n_notes, strength, lapses, last_used from lemma"
            )
        }
        ranked = sorted(
            seen, key=lambda w: (-meta.get(w, blank)[0], -meta.get(w, blank)[1], w)
        )

        wanted = _tag_words(topics or [])
        topical, rest = [], []
        cutoff = (time.time() - stale_days * 86400) * 1000
        stale: list[tuple[int, str]] = []
        for word in ranked:
            (topical if wanted & seen[word] else rest).append(word)
            n_notes, _, _, last_used = meta.get(word, blank)
            if last_used and last_used < cutoff:
                stale.append((n_notes, word))
        stale.sort(key=lambda x: (-x[0], x[1]))
        hint = sorted(word for _, word in stale[:stale_hint])
        weak = self._weak(ranked, meta, weak_hint, mature_ivl)

        if limit is None:
            return Palette(sorted(topical), sorted(rest), 0, hint, weak)
        kept_topical = topical[:limit]
        kept_core = rest[: max(0, limit - len(kept_topical))]
        dropped = (len(topical) - len(kept_topical)) + (len(rest) - len(kept_core))
        return Palette(
            sorted(kept_topical), sorted(kept_core), dropped, hint, weak
        )

    def _weak(
        self, ranked: list[str], meta: dict, want: int, mature_ivl: int
    ) -> list[str]:
        """Слова, которые человек забывал и до зрелого интервала не довёл.

        Срыв без интервала бесполезен: слово можно было ронять полгода
        назад и с тех пор выучить намертво (ahi — 29 срывов и интервал
        103 дня). Порог зрелости берём у самого Anki — 21 день.

        Порядок — по тяжести, самое проблемное первым: подсказка нужна,
        чтобы с неё брали сверху. Алфавит здесь потерял бы весь сигнал.

        Служебные слова отсеиваем на выходе, а не по всему словарю:
        разбор стоит вызова морфологии, а нужно из него слов тридцать.
        """
        candidates = sorted(
            (w for w in ranked if meta.get(w, (0, 0.0, 0, None))[2] >= 1
             and meta[w][1] < mature_ivl),
            key=lambda w: (-meta[w][2], meta[w][1], w),
        )
        out: list[str] = []
        for word in candidates:
            if len(out) >= want:
                break
            if not self._is_function(word):
                out.append(word)
        return out

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

    def _scan(
        self, text: str, targets: set[str]
    ) -> list[tuple[str, str, str, int]]:
        """Знаменательные слова предложения: (форма, статус, лемма, попаданий).

        Форму держим рядом с леммой не для красоты: промахи морфологии
        видно только в паре. 'soojat' — партитив от soe, но Vabamorf
        отдаёт его самостоятельной леммой, и в отчёте это выглядит
        честным новым словом. С формой рядом сразу ясно, что случилось.

        targets — целевые слова карточек: они новые по определению
        и бюджет не тратят.
        """
        out: list[tuple[str, str, str, int]] = []
        for word in words(text):
            low = word.lower()
            if self._is_function(low):
                continue
            if targets and ({low} | self.lem.lemmas(low)) & targets:
                continue
            hit = self.lookup(low)
            out.append((low, hit.status, hit.lemma or self.lem.lemma(low), hit.times))
        return out

    def check_sentence(
        self, text: str, targets: set[str] | None = None
    ) -> tuple[int, list[str], list[str]]:
        """(процент покрытия, незнакомые леммы, знаменательные слова)."""
        scan = self._scan(text, targets or set())
        if not scan:
            return 100, [], []
        unknown = list(dict.fromkeys(
            lemma for _f, status, lemma, _n in scan if status == UNKNOWN
        ))
        covered = sum(status != UNKNOWN for _f, status, _l, _n in scan)
        return (
            round(100 * covered / len(scan)),
            unknown,
            [form for form, _s, _l, _n in scan],
        )

    def novel(self, text: str, taught: set[str] | None = None) -> Novel:
        """Новое в предложении — материал дозы колоды 03.

        Отличается от check_sentence тем, что редкие '~'-слова не
        проходят молча. Там слово, мелькнувшее в чужом примере, честно
        считается известным — иначе проверка ругалась бы на словах с
        его же карточек. Для дозы это другой вопрос: одна встреча в
        чужом примере запоминанию не равна, и такое слово тоже растит
        словарь, просто мягче полностью незнакомого.

        Порог HALF_KNOWN_MAX обязателен: ветка A1 в индекс не входит,
        поэтому без него в «полузнакомые» уезжали ema, koer и täna —
        самые знакомые слова, какие вообще есть.
        """
        unknown: dict[str, str] = {}
        half: dict[str, str] = {}
        for form, status, lemma, times in self._scan(text, taught or set()):
            if status == UNKNOWN:
                unknown.setdefault(lemma, form)
            elif status == SEEN and times <= HALF_KNOWN_MAX:
                half.setdefault(lemma, form)
        return Novel(
            unknown=[(lemma, form) for lemma, form in unknown.items()],
            half=[(lemma, form) for lemma, form in half.items()],
        )


    # --- долг: слова без своей карточки -----------------------------------
    def _sample(self, lemma: str, cap: int = 80) -> str | None:
        """Самое короткое предложение, где слово попадалось. Голое слово
        без контекста ничего не напоминает — а именно узнавание («а, вот
        оно откуда») и решает, брать его в тему или нет."""
        best: str | None = None
        for form, examples, et_raw, is_phrase in self.con.execute(
            "select f.form, e.examples, e.et_raw, e.is_phrase from form f"
            " join entry e on e.note_id = f.note_id where f.lemma = ?",
            (lemma,),
        ):
            texts = [et for et, _ru in json.loads(examples)]
            if is_phrase:
                texts.append(et_raw)
            for text in texts:
                if form.lower() in text.lower() and (
                    best is None or len(text) < len(best)
                ):
                    best = text
        if best and len(best) > cap:
            best = best[: cap - 1].rstrip() + "…"
        return best

    def debt_skip(self, words: list[str], undo: bool = False) -> tuple[int, int]:
        """«Это я знаю» — чтобы слово больше не предлагалось.

        Иначе в новом чате приезжает ровно тот же десяток: инструмент
        читает индекс, индекс собран из Anki, а ответа человека там нет
        и взяться ему неоткуда.

        Слово приводим к лемме: человек отвечает тем словом, которое
        увидел, но ответить он может и словоформой из примера.
        """
        keys = set()
        for word in words:
            low = word.strip().lower()
            if not low:
                continue
            known = self.con.execute(
                "select 1 from lemma where lemma = ?", (low,)
            ).fetchone()
            keys.add(low if known else self.lem.lemma(low))
        if not keys:
            return 0, self.con.execute(
                "select count(*) from debt_skip"
            ).fetchone()[0]
        marks = ",".join("?" * len(keys))
        if undo:
            self.con.execute(
                f"delete from debt_skip where lemma in ({marks})", tuple(keys)
            )
        else:
            self.con.executemany(
                "insert or replace into debt_skip(lemma, added) values (?, ?)",
                [(k, int(time.time())) for k in keys],
            )
        self.con.commit()
        total = self.con.execute("select count(*) from debt_skip").fetchone()[0]
        return len(keys), total

    def debt(self, limit: int = 10, offset: int = 0) -> DebtPage:
        """Слова, встреченные только внутри чужих предложений.

        Человек читает их на каждом повторе, но карточки у них нет —
        значит, ни форм, ни перевода он не учил. Это готовый список
        «чего добавить», причём из собственного материала, а не из
        частотного словаря вообще.

        Порядок — по числу встреч: чем чаще слово мозолит глаза, тем
        обиднее, что оно не выучено.

        Отбор идёт сверху и ровно до нужной страницы: разбор служебных
        слов стоит вызова морфологии, а страниц человек листает две-три.
        Отсюда же и ответ «есть ли ещё» — берём на одно слово больше.

        Границы вправляем молча, и это не то же самое, что предел
        палитры. Там срез прятал часть словаря навсегда, и о нём надо
        было кричать; здесь следующая страница в одном вызове, а нижняя
        строка и так говорит, что список не кончился. Молча пустой ответ
        на limit=0 был бы куда хуже: он читается как «долгов нет».
        """
        limit = max(1, min(limit, DEBT_PAGE_MAX))
        offset = max(0, offset)
        picked: list[tuple[str, int]] = []
        need = offset + limit + 1
        for lemma, n_notes in self.con.execute(
            # Карточка вне области видимости — всё равно карточка: в ветке
            # A1 у laps, koer и sõber она есть, и без этого условия
            # инструмент уверял, что человек их никогда не учил.
            "select lemma, n_notes from lemma where n_word_notes = 0"
            " and lemma not in (select lemma from outside)"
            " and lemma not in (select lemma from debt_skip)"
            " order by n_notes desc, lemma"
        ):
            if len(picked) >= need:
                break
            if len(lemma) < 2 or " " in lemma:
                continue
            if self._is_function(lemma) or self.lem.is_name(lemma):
                continue
            picked.append((lemma, n_notes))
        page = picked[offset : offset + limit]
        return DebtPage(
            [Debt(w, n, self._sample(w)) for w, n in page],
            offset,
            len(picked) > offset + limit,
            self.con.execute("select count(*) from debt_skip").fetchone()[0],
        )


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


def format_pool(p: Palette) -> str:
    topical, core, dropped = p.topical, p.core, p.dropped
    if not topical and not core:
        return "nothing matches these filters — drop level or pos"
    lines = []
    if topical:
        lines.append(f"topic ({len(topical)}): " + " · ".join(topical))
    if core:
        label = "rest of vocabulary" if topical else "vocabulary"
        lines.append(f"{label} ({len(core)}): " + " · ".join(core))
    if p.weak:
        lines.append(
            f"forgotten in reviews, hardest first ({len(p.weak)}): "
            + " · ".join(p.weak)
        )
        lines.append(
            "— these the learner keeps failing: a content word taken from "
            "here buys an extra encounter where memory is actually weak"
        )
    if p.stale:
        lines.append(
            f"not used lately ({len(p.stale)}): " + " · ".join(p.stale)
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


def sentence_ok(pct: int, new: list[str], min_coverage: int = 70) -> bool:
    """Мягкое правило: одно новое слово допустимо всегда, дальше смотрим
    на покрытие. Жёсткий потолок «ровно одно новое» ломал предложения —
    естественную фразу не всегда можно собрать из готового.

    Отдельной функцией, потому что вердикт считают двое: отчёт наружу и
    строка в логе. Продублированная формула разъехалась бы при первой
    же правке, и лог начал бы врать про собственный ответ.
    """
    return pct >= min_coverage or len(new) <= 1


def format_text_check(
    results: list[tuple[int, list[str], list[str]]], min_coverage: int = 70
) -> str:
    lines = []
    candidates: list[str] = []
    low = 0
    for i, (pct, new, _content) in enumerate(results, 1):
        for w in new:
            if w not in candidates:
                candidates.append(w)
        ok = sentence_ok(pct, new, min_coverage)
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


def format_debt(page: DebtPage) -> str:
    items, offset = page.items, page.offset
    if not items:
        return (
            "no words left in the debt list"
            + (f" past #{offset}" if offset else "")
        )
    lines = [
        "met only inside example sentences — no card of their own, "
        "so the forms and the meaning were never learned:"
    ]
    width = max(len(i.word) for i in items)
    for n, item in enumerate(items, start=offset + 1):
        # Теги тут не показываем: это теги чужой карточки, в примере
        # которой слово попалось мимоходом, — про само слово они не
        # говорят ничего, а строку удлиняют. Контекст даёт предложение.
        seen_in = f"  {item.example}" if item.example else ""
        lines.append(
            f"{n:>3}. {item.word:<{width}} {item.times:>4}×{seen_in}".rstrip()
        )
    lines.append(
        f"next page: vocab_debt(offset={offset + len(items)})"
        if page.more
        else "— end of the list"
    )
    # Про механизм отметок говорим в самой выдаче: без этого тот же
    # десяток приезжает в каждом новом чате, и никто не догадается почему.
    tail = "say vocab_debt_skip(words=[…]) for any the learner already knows"
    if page.skipped:
        tail += f" · {page.skipped} already marked known"
    lines.append("— " + tail)
    return "\n".join(lines)
