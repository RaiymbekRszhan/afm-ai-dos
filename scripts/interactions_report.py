#!/usr/bin/env python3
"""Отчёт по логам взаимодействий Ai-dos (logs/interactions.jsonl + суточные архивы).

Печатает сводку для АФМ: сколько обращений, доля fallback (что база НЕ покрыла —
кандидаты на пополнение базы), доля ошибок, задержки p50/p95 (всего и по стадиям),
разбивка ru/kk, печать бланков, топ вопросов.

Считает НЕ САМ: вся арифметика в `app.analytics`, оттуда же её берёт веб-админка
(`/admin/stats`). Иначе цифры в браузере и в консоли разошлись бы, и доверять
перестали бы обеим. Модуль намеренно на голой stdlib, без app.config — этот
скрипт должен запускаться на сервере АФМ отдельно от приложения.

    python -m scripts.interactions_report                  # весь каталог logs/
    python -m scripts.interactions_report --dir /opt/ai-dos/logs
    python -m scripts.interactions_report --days 7         # только за последние 7 суток
    python -m scripts.interactions_report --top 30         # топ-30 вопросов
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics  # noqa: E402  (после правки sys.path)


def _lat_line(name: str, lat: dict, slow_ms: int) -> str:
    if not lat["n"]:
        return f"  {name:<8} нет данных"
    # max при малом числе замеров РАВЕН p95 (перцентиль попадает на последний
    # элемент), поэтому рядом печатаем счётчик неприемлемо долгих — он осмыслен
    # при любом объёме.
    slow = f"  дольше {slow_ms // 1000} с: {lat['slow']}" if lat["slow"] else ""
    return (f"  {name:<8} p50={lat['p50']} мс  p95={lat['p95']} мс  "
            f"max={lat['max']} мс  (n={lat['n']}){slow}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Отчёт по логам взаимодействий Ai-dos")
    ap.add_argument("--dir", default="logs", help="каталог с interactions.jsonl (по умолч. logs)")
    ap.add_argument("--days", type=int, default=None, help="только за последние N суток")
    ap.add_argument("--top", type=int, default=20, help="сколько частых вопросов показать")
    # Нагрузочные прогоны (scripts.load_test помечает их kiosk=loadtest) и
    # приёмки бьют одним и тем же образцом голоса — в топе вопросов они забивают
    # первые строки и портят долю «нет в базе». По умолчанию их не показываем.
    ap.add_argument("--exclude-kiosk", default=",".join(analytics.DEFAULT_EXCLUDE),
                    help="не учитывать эти точки (через запятую); пусто — учитывать все")
    ap.add_argument("--kiosk", default=None,
                    help="ТОЛЬКО эта точка (например astana-01)")
    args = ap.parse_args()

    rows = analytics.load(args.dir, args.days)
    exclude = tuple(k.strip() for k in args.exclude_kiosk.split(",") if k.strip())
    before = len(rows)
    rows = analytics.filter_rows(rows, kiosk=args.kiosk, exclude=exclude)
    skipped = 0 if args.kiosk else before - len(rows)
    total = len(rows)
    if not total:
        print(f"Нет записей в {args.dir!r}"
              + (f" за последние {args.days} сут." if args.days else "") + ".")
        return

    s = analytics.summarize(rows)
    period = f"за последние {args.days} сут." if args.days else "за всё время"

    print(f"\n=== Ai-dos: отчёт по обращениям ({period}) ===\n")
    if args.kiosk:
        print(f"Только точка:         {args.kiosk}")
    elif skipped:
        print(f"Отброшено служебных:  {skipped} (прогоны/приёмки: {args.exclude_kiosk};"
              f" показать всё — --exclude-kiosk '')")
    print(f"Всего обращений:      {total}")
    print(f"  успешных:           {s['ok']} ({analytics.pct(s['ok'], total)})")
    # Три исхода по отдельности: бежать что-то починять надо только из-за сбоев.
    # «Не расслышал» — шум у киоска, «отказано» — наши же настройки сработали.
    print(f"  сбоев:              {s['failures']} ({analytics.pct(s['failures'], total)})"
          + (f"  по стадиям: {dict(s['failures_by_stage'])}" if s["failures"] else ""))
    print(f"  не расслышал:       {s['not_heard']} ({analytics.pct(s['not_heard'], total)})")
    print(f"  отказано:           {s['refused']} ({analytics.pct(s['refused'], total)})"
          + (f"  {dict(s['refused_by_kind'])}" if s["refused"] else "")
          + "  (рубильник/пропуск)")
    print(f"  язык:               " + ", ".join(f"{k}={v}" for k, v in s["langs"]))
    print(f"  TTS-провайдер:      " + ", ".join(f"{k}={v}" for k, v in s["providers"]))
    print()
    print(f"НЕ найдено в базе (fallback):  {s['fallback']} "
          f"({analytics.pct(s['fallback'], s['ok'])}) — кандидаты на пополнение базы")
    print(f"Уточнение вопроса (suggest):   {s['suggested']} "
          f"({analytics.pct(s['suggested'], s['ok'])})")
    print(f"Предложена печать бланка:      {s['printed']} "
          f"({analytics.pct(s['printed'], s['ok'])})")
    print()
    print("Задержки:")
    for name in ("total", "stt", "rag", "tts"):
        print(_lat_line(name, s["latency"][name], s["slow_ms"]))
    print()

    # Разбивка по точкам: при двух десятках киосков средние числа врут — одна
    # мёртвая точка (0 обращений) или одна проблемная (все вопросы в fallback)
    # в общей куче не видна.
    kiosks = analytics.by_kiosk(rows)
    if len(kiosks) > 1 or (kiosks and kiosks[0]["kiosk"] != "-"):
        print("По киоскам:")
        # Ширина колонки — по самому длинному номеру (%COMPUTERNAME% бывает
        # длиннее «astana-01»), иначе таблица разъезжается ровно там, где её
        # читают глазами.
        w = max(10, min(32, max(len(k["kiosk"]) for k in kiosks)))
        print(f"  {'киоск':<{w}} {'обращений':>9} {'нет в базе':>12} {'сбоев':>7} {'total p50':>10}")
        for k in kiosks:
            p50 = f"{k['p50']} мс" if k["p50"] is not None else "—"
            print(f"  {k['kiosk']:<{w}} {k['total']:>9} {k['fallback']:>6}"
                  f" {k['fallback_pct']:>5} {k['failures']:>7} {p50:>10}")
        print()

    # Топ вопросов (только если тексты писались — log_questions=full).
    top = analytics.top_questions(rows, args.top)
    if top:
        print(f"Топ-{args.top} вопросов:")
        for q, n in top:
            q1 = q if len(q) <= 80 else q[:77] + "…"
            print(f"  {n:>4}×  {q1}")
    else:
        print("Тексты вопросов не логировались (log_questions=off/hash) — топ недоступен.")
    print()


if __name__ == "__main__":
    main()
