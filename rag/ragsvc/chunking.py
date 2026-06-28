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
        first = b.splitlines()[0].strip()[:80]
        label = f"{short}: {first}" if short else first
        units.append(Unit(text=b, label=label, uid=_uid(short or "ref", b)))
    return units


def chunk_file(raw: str) -> list[Unit]:
    meta, body = parse_header(raw)
    short = meta["SHORT"] or meta["LAW"] or "Документ"
    t = meta["TYPE"].lower()
    if t == "code":
        return chunk_code(body, short, meta["LANG"].lower())
    if t == "faq":
        return chunk_faq(body, short)
    return chunk_reference(body, short)
