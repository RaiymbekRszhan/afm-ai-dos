"""
Оценка ответов RAG-сервиса по тест-набору (eval/dataset.yaml).

Меряет то, что важно для госюр-ассистента:
  - ссылается ли ответ на нужную норму (expect_source);
  - содержит ли ключевые факты (expect_contains);
  - НЕ выдумывает ли: на внебазовых вопросах должен сработать fallback (expect_fallback).

Запуск (RAG-сервис должен быть поднят на :8077):
    cd rag && source .venv/bin/activate
    python -m eval.run_eval                 # весь набор
    python -m eval.run_eval --url http://localhost:8077/ask
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import yaml

DATASET = Path(__file__).resolve().parent / "dataset.yaml"
# Маркеры фиксированной фразы «нет в базе» (ru + kk; см. ragsvc/prompts.py).
FALLBACK_MARKS = ("нет точной информации", "нақты ақпарат жоқ")


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
    args = ap.parse_args(argv)

    cases = yaml.safe_load(DATASET.read_text(encoding="utf-8"))["cases"]
    passed = 0
    async with httpx.AsyncClient(timeout=180) as client:
        for i, case in enumerate(cases, 1):
            need_src = bool(case.get("expect_source"))
            try:
                resp = await ask(client, args.url, case["question"], case.get("lang", "ru"), need_src)
            except Exception as e:
                print(f"[{i:>2}] ERR : {case['question']}\n        запрос не прошёл: {e}")
                continue
            fails = check(case, resp)
            if fails:
                print(f"[{i:>2}] FAIL: {case['question']}")
                for f in fails:
                    print(f"        - {f}")
                print(f"        ответ: {(resp.get('answer') or '')[:180]}")
            else:
                passed += 1
                print(f"[{i:>2}] OK  : {case['question']}")

    print(f"\nИтого: {passed}/{len(cases)} прошло")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
