"""
Загрузка базы знаний в LightRAG.

Запуск:
    python -m ragsvc.ingest                 # вся папка data/
    python -m ragsvc.ingest data/faq_afm.md # один файл

Что делает: читает .md/.txt из data/, нарезает по типу (код → по статьям,
faq → по парам, reference → по абзацам), и кладёт в LightRAG. Метка источника
(label) уезжает в file_paths и становится ЦИТАТОЙ в ответах.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .chunking import chunk_file
from .rag_engine import build_rag
from . import config

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def collect_files(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args]
    # README/служебные файлы (имя с README или с префиксом «_») — не база знаний.
    def _is_doc(p: Path) -> bool:
        if p.suffix not in (".md", ".txt"):
            return False
        return not (p.stem.lower().startswith("readme") or p.name.startswith("_"))
    return sorted([p for p in DATA_DIR.glob("**/*") if _is_doc(p)])


async def main(args: list[str]):
    files = collect_files(args)
    if not files:
        print("Нет файлов для загрузки. Положи .md/.txt в data/")
        return

    rag = await build_rag()

    texts, ids, labels = [], [], []
    for f in files:
        raw = f.read_text(encoding="utf-8")
        units = chunk_file(raw)
        for u in units:
            texts.append(u.text)
            ids.append(u.uid)
            labels.append(u.label)
        print(f"  {f.name}: {len(units)} единиц")

    print(f"\nВсего {len(texts)} единиц на загрузку.")

    # дедупликация по uid (повторная загрузка не плодит копии)
    seen, T, I, L = set(), [], [], []
    for t, i, l in zip(texts, ids, labels):
        if i in seen:
            continue
        seen.add(i)
        T.append(t); I.append(i); L.append(l)

    await rag.ainsert(T, ids=I, file_paths=L)
    print(f"Готово. Загружено {len(T)} уникальных единиц в {config.WORKING_DIR}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
