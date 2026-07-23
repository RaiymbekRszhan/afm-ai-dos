"""
Стратегия нарезки. Для юридических текстов нельзя резать «по N символов» —
режем по статьям, чтобы поиск попадал точно в норму, а цитата была корректной.

Каждый файл в data/ начинается со служебных строк-метаданных, например:

    # LAW: Административный процедурно-процессуальный кодекс РК
    # SHORT: АПК РК
    # TYPE: code        (code | faq | reference)
    # LANG: ru          (ru | kk)

Возвращаем список Unit(text, label, uid):
  - text  — текст чанка (идёт в эмбеддинг + LLM)
  - label — человекочитаемая метка источника (станет ЦИТАТОЙ в ответе)
  - uid   — стабильный id документа (для дедупликации/обновления)
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class Unit:
    text: str
    label: str
    uid: str


def _uid(prefix: str, body: str) -> str:
    h = hashlib.md5(body.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}-{h}"


# --- Чистка редакционных примечаний ------------------------------------------
# В официальных текстах между нормами вставлены СТРОКИ-ПРИМЕЧАНИЯ о будущих/
# прошлых правках: «В ч.2 статьи 64 предусматривается изменение Законом РК от
# 11.06.2026 …», «N-бапқа өзгеріс енгізу көзделген – ҚР … Заңымен», «См. изменения
# в статью …». Это НЕ действующая норма — для устного ответа гражданину это шум
# (аватар мог бы проговорить «готовится изменение с такой-то даты»). Вырезаем их
# ДО нарезки. Якоря узкие и проверены (rag/eval + сухая калибровка): обычное
# нормативное «көзделген» («предусмотрено») и заключительные «вводится в действие»/
# «Исключен Законом РК …» НЕ трогаем — только форму editorial-примечания.
_EDITORIAL_NOTE_RE = re.compile(
    r"("
    r"(предусматрива[её]тся|предусмотрено)\s+(изменение|дополнение|исключение|.{0,40}в\s+редакции)"
    r"|См\.\s*изменени[яе]\s+в"
    r"|(өзгеріс енгізу|толықтыру|алып тастау|.{0,30}редакциясында)\s+көзделген\s*[–\-—]\s*ҚР"
    r")",
    re.IGNORECASE,
)


def _strip_editorial_notes(body: str) -> str:
    """Удаляет строки-примечания о правках (см. _EDITORIAL_NOTE_RE)."""
    return "\n".join(
        ln for ln in body.splitlines() if not _EDITORIAL_NOTE_RE.search(ln)
    )


# Строка «содержательна», если в ней есть слово из букв (а не только номер пункта
# «1.», подчёркивания бланка «____» и знаки). По ней выбираем метку-цитату у
# reference-чанков: у форм uchet_sfm первая строка — пустое поле «1. ______»,
# из-за чего цитата получалась мусорной («Порядок учёта СФМ: 1. ______»).
_WORDY = re.compile(r"[A-Za-zА-Яа-яӘҒҚҢӨҰҮҺІіәғқңөұүһЁё]{3,}")


def _first_meaningful_line(block: str, limit: int = 80) -> str:
    for ln in block.splitlines():
        s = ln.strip()
        if _WORDY.search(s):
            return s[:limit]
    return block.splitlines()[0].strip()[:limit] if block.splitlines() else ""


def parse_header(raw: str):
    """Снимаем метаданные из первых строк файла."""
    meta = {"LAW": "", "SHORT": "", "TYPE": "reference", "LANG": "ru"}
    body_lines = []
    for line in raw.splitlines():
        m = re.match(r"^#\s*(LAW|SHORT|TYPE|LANG)\s*:\s*(.+)$", line.strip())
        if m:
            meta[m.group(1)] = m.group(2).strip()
        else:
            body_lines.append(line)
    return meta, "\n".join(body_lines).strip()


# --- Кодексы: режем по «Статья N» (ru) или «N-бап» (kk) -------------------
_RU_ARTICLE = re.compile(r"(?=^\s*Статья\s+\d+)", re.MULTILINE)
_KK_ARTICLE = re.compile(r"(?=^\s*\d+(?:-\d+)?-бап)", re.MULTILINE)
_RU_NUM = re.compile(r"Статья\s+(\d+(?:-\d+)?)")
_KK_NUM = re.compile(r"(\d+(?:-\d+)?)-бап")


def chunk_code(body: str, short: str, lang: str) -> list[Unit]:
    splitter = _KK_ARTICLE if lang == "kk" else _RU_ARTICLE
    num_re = _KK_NUM if lang == "kk" else _RU_NUM
    art_word = "бап" if lang == "kk" else "статья"

    parts = [p.strip() for p in splitter.split(body) if p.strip()]
    units: list[Unit] = []
    for part in parts:
        m = num_re.search(part)
        num = m.group(1) if m else "?"
        label = f"{short}, {num}-{art_word}" if lang == "kk" else f"{short}, {art_word} {num}"
        units.append(Unit(text=part, label=label, uid=_uid(f"{short}-{num}", part)))
    return units


# --- FAQ: каждая пара «вопрос/ответ» отделена строкой «===» ----------------
def chunk_faq(body: str, short: str) -> list[Unit]:
    blocks = [b.strip() for b in re.split(r"^===+\s*$", body, flags=re.MULTILINE) if b.strip()]
    units: list[Unit] = []
    for b in blocks:
        first = b.splitlines()[0].strip().rstrip("?")[:80]
        label = f"{short}: {first}"
        units.append(Unit(text=b, label=label, uid=_uid(short, b)))
    return units


# --- Справочный текст: режем по пустым строкам в абзацы --------------------
def chunk_reference(body: str, short: str) -> list[Unit]:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    units: list[Unit] = []
    for b in blocks:
        first = _first_meaningful_line(b)
        label = f"{short}: {first}" if short else first
        units.append(Unit(text=b, label=label, uid=_uid(short or "ref", b)))
    return units


# LightRAG до-режет любую вставленную единицу длиннее CHUNK_TOKEN_SIZE (1200
# токенов, см. rag_engine.build_rag) на несколько чанков, а языковую метку
# [RU]/[KK] несёт только первый кусок — «хвосты» длинных статей теряли язык, и
# LLM тянула из безметочного русского хвоста слово в казахский ответ. Поэтому
# длинные единицы дробим САМИ на под-куски заведомо короче лимита и метим КАЖДЫЙ.
# 2400 символов ≈ 900-1100 токенов o200k на кириллице (под лимитом 1200, чтобы
# LightRAG НЕ дорезал и метка сохранилась). Держим кусок КРУПНЫМ, близким к
# естественной нарезке LightRAG: мелкий бюджет (было 1500) удвоил число чанков и
# измельчил retrieval — анти-выдумочный кейс eval начал вместо чистого отказа
# добавлять пояснение (2026-07-22). Слишком крупный (к 1200 токенам) рискует тем,
# что LightRAG снова дорежет и вернёт безметочный хвост — 2400 это компромисс.
_MAX_UNIT_CHARS = 2400


def _split_long_text(text: str, max_chars: int = _MAX_UNIT_CHARS) -> list[str]:
    """Режет длинный текст на части <= max_chars по «мягким» границам.

    Приоритет границ: конец абзаца/строки -> конец предложения -> жёсткий разрез
    (крайний случай). Порядок и полнота текста сохраняются — ничего не теряется и
    не задваивается.
    """
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    rest = text.strip()
    while len(rest) > max_chars:
        window = rest[:max_chars]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        if cut < max_chars // 2:
            # нет близкой границы абзаца/строки — ищем конец предложения
            last = None
            for mm in re.finditer(r"[.!?;]\s", window):
                last = mm
            cut = last.end() if last and last.end() >= max_chars // 2 else max_chars
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        parts.append(rest)
    return [p for p in parts if p]


def chunk_file(raw: str) -> list[Unit]:
    meta, body = parse_header(raw)
    body = _strip_editorial_notes(body)  # убираем строки-примечания о правках
    short = meta["SHORT"] or meta["LAW"] or "Документ"
    t = meta["TYPE"].lower()
    if t == "code":
        units = chunk_code(body, short, meta["LANG"].lower())
    elif t == "faq":
        units = chunk_faq(body, short)
    else:
        units = chunk_reference(body, short)
    # Языковая метка ИСТОЧНИКА в начале КАЖДОГО чанка (не путать с language
    # вопроса): ru/kk-версии одной статьи семантически почти идентичны, поэтому
    # векторный поиск нередко достаёт ОБЕ в один контекст — без метки LLM
    # подмешивала в казахский ответ русский термин прямо оттуда (правило 1 в
    # prompts.py учит модель не брать лексику из чужеязычного куска). Длинные
    # единицы дробим, чтобы метку получил и «хвост» (см. _split_long_text).
    tag = "[KK]" if meta["LANG"].strip().lower() == "kk" else "[RU]"
    out: list[Unit] = []
    for u in units:
        pieces = _split_long_text(u.text)
        for i, piece in enumerate(pieces):
            uid = u.uid if len(pieces) == 1 else f"{u.uid}-p{i}"
            out.append(Unit(text=f"{tag} {piece}", label=u.label, uid=uid))
    return out
