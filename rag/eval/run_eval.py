"""
Оценка ответов RAG-сервиса по тест-набору (eval/dataset.yaml).

Меряет то, что важно для госюр-ассистента:
  - ссылается ли ответ на нужную норму (expect_source);
  - содержит ли ключевые факты (expect_contains);
  - НЕ выдумывает ли: на внебазовых вопросах должен сработать fallback (expect_fallback);
  - СКОЛЬКО ГОВОРИТ: длина устной части в символах (см. ниже).

Длина — не придирка к стилю, а главный рычаг ожидания у киоска: гражданин
СЛУШАЕТ ответ целиком, ~100 символов ≈ 7 секунд речи, и ответ на 700 символов
он стоит и слушает минуту. Поэтому считаем длину БЕЗ блока [ТАБЛИЦА] (его
голос не читает — вырезает app/clients/tts.py strip_display_blocks) и печатаем
сводку: медиана, максимум, сколько кейсов вылезло за бюджет. Точность при этом
остаётся единственным критерием провала — длина только показывается, иначе
любая правка промпта начнёт «чиниться» обрезкой фактов.

Запуск (RAG-сервис должен быть поднят на :8077):
    cd rag && source .venv/bin/activate
    python -m eval.run_eval                 # весь набор
    python -m eval.run_eval --url http://localhost:8077/ask
    python -m eval.run_eval --budget 400    # свой бюджет длины (симв.)
"""
from __future__ import annotations

import argparse
import asyncio
import re
import statistics
import sys
from pathlib import Path

import httpx
import yaml

DATASET = Path(__file__).resolve().parent / "dataset.yaml"
# Маркеры фиксированной фразы «нет в базе» (ru + kk; см. ragsvc/prompts.py).
FALLBACK_MARKS = ("нет точной информации", "нақты ақпарат жоқ")
# Бюджет устного ответа — тот же, что просит ragsvc/prompts.RESPONSE_TYPE.
# Дублируется числом, а не импортом: eval ходит по HTTP и должен работать против
# ЧУЖОГО сервиса (сервер АФМ), где нашего кода под рукой нет.
DEFAULT_BUDGET = 400
# Экранный блок таблицы: его показывает video_ui, голос его не произносит.
_TABLE_RE = re.compile(r"\[ТАБЛИЦА\].*?\[/ТАБЛИЦА\]", re.DOTALL)


def spoken_len(answer: str) -> int:
    """Длина того, что реально прозвучит вслух (без экранной таблицы)."""
    return len(_TABLE_RE.sub("", answer or "").strip())


def _has_fallback(answer: str) -> bool:
    return any(m in answer for m in FALLBACK_MARKS)


async def ask(client: httpx.AsyncClient, url: str, q: str, lang: str, with_sources: bool) -> dict:
    r = await client.post(url, json={"question": q, "lang": lang, "with_sources": with_sources})
    r.raise_for_status()
    return r.json()


def check(case: dict, resp: dict) -> list[str]:
    """Возвращает список провалов (пусто = кейс прошёл)."""
    answer = (resp.get("answer") or "").lower()
    sources = (resp.get("sources") or "").lower()
    fails: list[str] = []

    if case.get("expect_fallback"):
        if not _has_fallback(answer):
            fails.append("ожидался отказ «нет в базе», но модель что-то ответила (риск выдумки)")
        return fails

    if _has_fallback(answer):
        fails.append("неожиданный отказ «нет в базе» — ответ должен был найтись")

    for sub in case.get("expect_contains", []):
        if sub.lower() not in answer:
            fails.append(f"в ответе нет ключевого: «{sub}»")

    # expect_contains_any — достаточно ОДНОГО совпадения (для сумм: слова ИЛИ цифры)
    any_subs = case.get("expect_contains_any")
    if any_subs and not any(s.lower() in answer for s in any_subs):
        fails.append(f"в ответе нет ни одного из: {any_subs}")

    src = case.get("expect_source")
    if src and src.lower() not in f"{answer} {sources}":
        fails.append(f"нет ссылки на источник: «{src}»")
    return fails


async def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8077/ask")
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET,
                    help=f"бюджет устного ответа в символах (по умолчанию {DEFAULT_BUDGET})")
    args = ap.parse_args(argv)

    cases = yaml.safe_load(DATASET.read_text(encoding="utf-8"))["cases"]
    passed = 0
    # Длины считаем ТОЛЬКО по содержательным ответам: фраза-отказ фиксированная,
    # она бы занизила медиану и спрятала разговорчивость на реальных вопросах.
    lengths: list[int] = []
    async with httpx.AsyncClient(timeout=180) as client:
        for i, case in enumerate(cases, 1):
            need_src = bool(case.get("expect_source"))
            try:
                resp = await ask(client, args.url, case["question"], case.get("lang", "ru"), need_src)
            except Exception as e:
                print(f"[{i:>2}] ERR : {case['question']}\n        запрос не прошёл: {e}")
                continue
            n = spoken_len(resp.get("answer") or "")
            if not case.get("expect_fallback"):
                lengths.append(n)
            over = "  ← длинно" if n > args.budget else ""
            fails = check(case, resp)
            if fails:
                print(f"[{i:>2}] FAIL: {case['question']}  [{n} симв.{over}]")
                for f in fails:
                    print(f"        - {f}")
                print(f"        ответ: {(resp.get('answer') or '')[:180]}")
            else:
                passed += 1
                print(f"[{i:>2}] OK  : {case['question']}  [{n} симв.{over}]")

    print(f"\nИтого: {passed}/{len(cases)} прошло")
    if lengths:
        over = sum(1 for n in lengths if n > args.budget)
        # ~7 секунд речи на 100 символов (замер на F5/eleven, см. CLAUDE.md).
        print(f"Длина устного ответа: медиана {statistics.median(lengths):.0f}, "
              f"максимум {max(lengths)}, дольше бюджета ({args.budget}) — "
              f"{over} из {len(lengths)}. "
              f"Медиана ≈ {statistics.median(lengths) * 0.07:.0f} с речи у экрана.")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
