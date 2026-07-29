#!/usr/bin/env python3
"""Отчёт по логам взаимодействий Ai-dos (logs/interactions.jsonl + суточные архивы).

Читает JSONL, который пишет оркестратор на каждый /voice (см. app/logging_setup.py),
и печатает сводку для АФМ: сколько обращений, доля fallback (что база НЕ покрыла —
кандидаты на пополнение базы), доля ошибок, задержки p50/p95 (всего и по стадиям),
разбивка ru/kk, печать бланков, топ вопросов.

Только stdlib — работает офлайн на сервере АФМ.

    python -m scripts.interactions_report                  # весь каталог logs/
    python -m scripts.interactions_report --dir /opt/ai-dos/logs
    python -m scripts.interactions_report --days 7         # только за последние 7 суток
    python -m scripts.interactions_report --top 30         # топ-30 вопросов
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone


def _load(log_dir: str, days: int | None) -> list[dict]:
    """Читает interactions.jsonl + ротированные interactions.jsonl.YYYY-MM-DD."""
    files = sorted(glob.glob(os.path.join(log_dir, "interactions.jsonl*")))
    cutoff = None
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue  # битую строку пропускаем, не роняем отчёт
                if cutoff is not None:
                    ts = rec.get("ts", "")
                    try:
                        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except ValueError:
                        when = None
                    if when is not None and when < cutoff:
                        continue
                rows.append(rec)
    return rows


def _pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def _percentile(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
    return s[k]


def _lat_line(name: str, vals: list[int]) -> str:
    vals = [v for v in vals if isinstance(v, int)]
    if not vals:
        return f"  {name:<8} нет данных"
    return (f"  {name:<8} p50={_percentile(vals, 50)} мс  "
            f"p95={_percentile(vals, 95)} мс  max={max(vals)} мс  (n={len(vals)})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Отчёт по логам взаимодействий Ai-dos")
    ap.add_argument("--dir", default="logs", help="каталог с interactions.jsonl (по умолч. logs)")
    ap.add_argument("--days", type=int, default=None, help="только за последние N суток")
    ap.add_argument("--top", type=int, default=20, help="сколько частых вопросов показать")
    # Нагрузочные прогоны (scripts.load_test помечает их kiosk=loadtest) и
    # приёмки бьют одним и тем же образцом голоса — в топе вопросов они забивают
    # первые строки и портят долю «нет в базе». По умолчанию их не показываем.
    ap.add_argument("--exclude-kiosk", default="loadtest",
                    help="не учитывать эти точки (через запятую); пусто — учитывать все")
    ap.add_argument("--kiosk", default=None,
                    help="ТОЛЬКО эта точка (например astana-01)")
    args = ap.parse_args()

    rows = _load(args.dir, args.days)
    skipped = 0
    if args.kiosk:
        rows = [r for r in rows if (r.get("kiosk") or "") == args.kiosk]
    elif args.exclude_kiosk.strip():
        drop = {k.strip() for k in args.exclude_kiosk.split(",") if k.strip()}
        before = len(rows)
        rows = [r for r in rows if (r.get("kiosk") or "") not in drop]
        skipped = before - len(rows)
    total = len(rows)
    if not total:
        print(f"Нет записей в {args.dir!r}"
              + (f" за последние {args.days} сут." if args.days else "") + ".")
        return

    period = f"за последние {args.days} сут." if args.days else "за всё время"
    errors = [r for r in rows if r.get("error")]
    ok = [r for r in rows if not r.get("error")]
    fallback = [r for r in ok if r.get("answer_found") is False]
    suggested = [r for r in ok if r.get("suggested")]
    printed = [r for r in ok if r.get("print_ids")]
    langs = Counter(r.get("lang", "?") for r in rows)
    err_by_stage = Counter(r["error"] for r in errors)
    providers = Counter(r.get("provider", "?") for r in ok)

    print(f"\n=== Ai-dos: отчёт по обращениям ({period}) ===\n")
    if args.kiosk:
        print(f"Только точка:         {args.kiosk}")
    elif skipped:
        print(f"Отброшено служебных:  {skipped} (прогоны/приёмки: {args.exclude_kiosk};"
              f" показать всё — --exclude-kiosk '')")
    print(f"Всего обращений:      {total}")
    print(f"  успешных:           {len(ok)} ({_pct(len(ok), total)})")
    print(f"  с ошибкой:          {len(errors)} ({_pct(len(errors), total)})"
          + (f"  по стадиям: {dict(err_by_stage)}" if errors else ""))
    print(f"  язык:               " + ", ".join(f"{k}={v}" for k, v in langs.most_common()))
    print(f"  TTS-провайдер:      " + ", ".join(f"{k}={v}" for k, v in providers.most_common()))
    print()
    print(f"НЕ найдено в базе (fallback):  {len(fallback)} ({_pct(len(fallback), len(ok))}) "
          f"— кандидаты на пополнение базы")
    print(f"Уточнение вопроса (suggest):   {len(suggested)} ({_pct(len(suggested), len(ok))})")
    print(f"Предложена печать бланка:      {len(printed)} ({_pct(len(printed), len(ok))})")
    print()
    print("Задержки:")
    print(_lat_line("total", [r.get("total_ms") for r in ok]))
    print(_lat_line("stt", [r.get("stt_ms") for r in ok]))
    print(_lat_line("rag", [r.get("rag_ms") for r in ok]))
    print(_lat_line("tts", [r.get("tts_ms") for r in ok]))
    print()

    # Разбивка по точкам: при двух десятках киосков средние числа врут — одна
    # мёртвая точка (0 обращений) или одна проблемная (все вопросы в fallback)
    # в общей куче не видна. Строка на киоск: сколько спрашивали, сколько
    # промахов по базе, сколько ошибок, медиана полного цикла.
    by_kiosk = Counter(r.get("kiosk") or "-" for r in rows)
    if len(by_kiosk) > 1 or "-" not in by_kiosk:
        print("По киоскам:")
        # Ширина колонки — по самому длинному номеру (%COMPUTERNAME% бывает
        # длиннее «astana-01»), иначе таблица разъезжается ровно там, где её
        # читают глазами.
        w = max(10, min(32, max(len(k) for k in by_kiosk)))
        print(f"  {'киоск':<{w}} {'обращений':>9} {'нет в базе':>12} {'ошибок':>7} {'total p50':>10}")
        for kid, n in by_kiosk.most_common():
            k_rows = [r for r in rows if (r.get("kiosk") or "-") == kid]
            k_ok = [r for r in k_rows if not r.get("error")]
            k_fb = [r for r in k_ok if r.get("answer_found") is False]
            k_err = [r for r in k_rows if r.get("error")]
            # is not None, а не «истинность»: total_ms=0 (быстрый мок/кэш) — это
            # значение, а не отсутствие замера.
            p50 = _percentile([r["total_ms"] for r in k_ok if r.get("total_ms") is not None], 50)
            print(f"  {kid:<{w}} {n:>9} {len(k_fb):>6} {_pct(len(k_fb), len(k_ok)):>5}"
                  f" {len(k_err):>7} {(f'{p50} мс') if p50 is not None else '—':>10}")
        print()

    # Топ вопросов (только если тексты писались — log_questions=full).
    questions = [r["question"] for r in rows if r.get("question")]
    if questions:
        print(f"Топ-{args.top} вопросов:")
        for q, n in Counter(q.strip() for q in questions).most_common(args.top):
            q1 = q if len(q) <= 80 else q[:77] + "…"
            print(f"  {n:>4}×  {q1}")
    else:
        print("Тексты вопросов не логировались (log_questions=off/hash) — топ недоступен.")
    print()


if __name__ == "__main__":
    main()
