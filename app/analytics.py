"""Аналитика обращений: чтение JSONL и подсчёт сводок.

ОДНА логика подсчёта на два потребителя — веб-админку (`/admin/stats`) и
CLI-отчёт (`scripts.interactions_report`). Дублировать её нельзя: цифры в
браузере и в консоли разошлись бы, и доверять перестали бы обеим.

Источник — `logs/interactions.jsonl` (+ суточные архивы `.YYYY-MM-DD`), который
пишет оркестратор на каждый `/voice`; поля см. `app/logging_setup.record_interaction`.

Только stdlib и НИКАКОГО app.config: CLI-отчёт должен оставаться пригодным для
запуска на сервере АФМ отдельно от приложения.

⚠️ Порядок строк из `load()` — как в файлах (glob по именам), а НЕ по времени.
Так было в исходном отчёте, и от этого зависят ничьи в `Counter.most_common()`:
пересортировка молча поменяла бы вывод. Журналу нужны новые сверху — для этого
есть `newest_first()`.
"""
from __future__ import annotations

import glob
import json
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

# Кэш разобранных строк: отпечаток файлов -> строки. Страница админки
# обновляется каждые 30 с, перечитывать и разбирать 30 суток логов на каждый
# запрос незачем. Тот же приём, что в app/kiosks.py.
_cache: tuple[tuple, list[dict]] | None = None


def _fingerprint(paths: list[str]) -> tuple:
    out = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append((p, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _read_all(log_dir: str) -> list[dict]:
    """Читает interactions.jsonl + ротированные interactions.jsonl.YYYY-MM-DD."""
    global _cache
    files = sorted(glob.glob(os.path.join(log_dir, "interactions.jsonl*")))
    fp = _fingerprint(files)
    if _cache is not None and _cache[0] == fp:
        return _cache[1]
    rows: list[dict] = []
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # битую строку пропускаем, не роняем отчёт
        except OSError:
            continue
    _cache = (fp, rows)
    return rows


def _when(rec: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(rec.get("ts", "").replace("Z", "+00:00"))
    except ValueError:
        return None


def load(log_dir: str, days: int | None = None) -> list[dict]:
    """Строки аналитики, при days — только за последние N суток.

    Запись с непарсимым `ts` НЕ отбрасывается: лучше показать её в общей куче,
    чем потерять обращение из-за одного битого поля.
    """
    rows = _read_all(log_dir)
    if days is None:
        return list(rows)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    keep = []
    for rec in rows:
        when = _when(rec)
        if when is not None and when < cutoff:
            continue
        keep.append(rec)
    return keep


def reset_cache() -> None:
    """Забыть прочитанное (тесты и принудительное обновление)."""
    global _cache
    _cache = None


def newest_first(rows: list[dict]) -> list[dict]:
    """Для журнала: свежие сверху. Записи без времени — в конец."""
    return sorted(rows, key=lambda r: r.get("ts") or "", reverse=True)


# ---------- элементарные метрики ----------
def pct(part: int, whole: int) -> str:
    return f"{(100.0 * part / whole):.1f}%" if whole else "—"


def percentile(values: list, p: float) -> int | None:
    vals = [v for v in values if isinstance(v, int)]
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, round((p / 100.0) * (len(s) - 1))))
    return s[k]


def latency(rows: list[dict], field: str) -> dict:
    """p50/p95/max/n по одной стадии. Пустая выборка — все None."""
    vals = [r.get(field) for r in rows]
    vals = [v for v in vals if isinstance(v, int)]
    if not vals:
        return {"p50": None, "p95": None, "max": None, "n": 0}
    return {"p50": percentile(vals, 50), "p95": percentile(vals, 95),
            "max": max(vals), "n": len(vals)}


# ---------- фильтры ----------
# Нагрузочные прогоны (scripts.load_test) и приёмки бьют одним образцом голоса:
# в топе вопросов они забивают первые строки и портят долю «нет в базе».
DEFAULT_EXCLUDE = ("loadtest",)

# Поле `error` мешает в одну кучу три разных по смыслу исхода, и оператору важно
# их различать: бежать что-то починять надо только из-за первой группы.
#   сбой     — упал сервис (STT/RAG/TTS), это поломка;
#   не расслышал — на записи шум или тишина, гражданин просто повторит вопрос.
#                  Нормальный исход работы фильтров, а не отказ системы;
#   отказано — сработали НАШИ настройки: рубильник по точке или пропуск.
#              Считать это ошибкой значит раздувать долю сбоев на ровном месте
#              и прятать настоящие проблемы (найдено на живой странице 30.07).
FAILURE_STAGES = ("stt", "rag", "tts")
# empty — распознали пустоту; noise — распознали шум/петлю и в базу не ходили.
NOT_HEARD = ("empty", "noise")
REFUSALS = ("disabled", "gate")


def filter_rows(rows: list[dict], *, kiosk: str | None = None,
                exclude: tuple[str, ...] | set[str] = DEFAULT_EXCLUDE,
                only: str | None = None, search: str | None = None) -> list[dict]:
    """Отбор для журнала и выгруза.

    `kiosk` — только эта точка (тогда `exclude` не применяется: спросили именно
    про неё). `only` = errors | fallback. `search` — подстрока в вопросе или
    ответе, без учёта регистра.
    """
    out = rows
    if kiosk:
        out = [r for r in out if (r.get("kiosk") or "") == kiosk]
    elif exclude:
        drop = set(exclude)
        out = [r for r in out if (r.get("kiosk") or "") not in drop]
    if only == "errors":
        out = [r for r in out if r.get("error")]
    elif only == "fallback":
        out = [r for r in out if not r.get("error") and r.get("answer_found") is False]
    if search:
        needle = search.casefold()
        out = [r for r in out
               if needle in (r.get("question") or "").casefold()
               or needle in (r.get("answer") or "").casefold()]
    return out


# ---------- сводки ----------
def summarize(rows: list[dict]) -> dict:
    """Общая сводка. Списки пар — уже отсортированы по убыванию (most_common)."""
    total = len(rows)
    errors = [r for r in rows if r.get("error")]
    ok = [r for r in rows if not r.get("error")]
    failures = [r for r in errors if r["error"] in FAILURE_STAGES]
    not_heard = [r for r in errors if r["error"] in NOT_HEARD]
    refused = [r for r in errors if r["error"] in REFUSALS]
    fallback = [r for r in ok if r.get("answer_found") is False]
    suggested = [r for r in ok if r.get("suggested")]
    printed = [r for r in ok if r.get("print_ids")]
    return {
        "total": total,
        "ok": len(ok),
        # errors — ВСЁ, где поле error непусто (общий баланс: ok + errors = total).
        "errors": len(errors),
        "err_by_stage": Counter(r["error"] for r in errors).most_common(),
        # Три разных исхода по отдельности — см. комментарий у FAILURE_STAGES.
        "failures": len(failures),
        "failures_by_stage": Counter(r["error"] for r in failures).most_common(),
        "not_heard": len(not_heard),
        "refused": len(refused),
        "refused_by_kind": Counter(r["error"] for r in refused).most_common(),
        "langs": Counter(r.get("lang", "?") for r in rows).most_common(),
        "providers": Counter(r.get("provider", "?") for r in ok).most_common(),
        "fallback": len(fallback),
        "suggested": len(suggested),
        "printed": len(printed),
        "latency": {name: latency(ok, f"{name}_ms")
                    for name in ("total", "stt", "rag", "tts", "tts_first")},
    }


def by_kiosk(rows: list[dict]) -> list[dict]:
    """Строка на точку. При двух десятках киосков средние числа врут: одна
    мёртвая точка (0 обращений) или одна проблемная (все вопросы в fallback) в
    общей куче не видна."""
    counts = Counter(r.get("kiosk") or "-" for r in rows)
    out = []
    for kid, n in counts.most_common():
        k_rows = [r for r in rows if (r.get("kiosk") or "-") == kid]
        k_ok = [r for r in k_rows if not r.get("error")]
        k_fb = [r for r in k_ok if r.get("answer_found") is False]
        k_err = [r for r in k_rows if r.get("error")]
        out.append({
            "kiosk": kid,
            "total": n,
            "ok": len(k_ok),
            "fallback": len(k_fb),
            "fallback_pct": pct(len(k_fb), len(k_ok)),
            "errors": len(k_err),
            # Отдельно от `errors`: в колонке «сбоев» не должны сидеть отказы
            # рубильника — иначе отключённый регион выглядит сломанным.
            "failures": len([r for r in k_err if r["error"] in FAILURE_STAGES]),
            "refused": len([r for r in k_err if r["error"] in REFUSALS]),
            # is not None, а не «истинность»: total_ms=0 (быстрый мок/кэш) —
            # это значение, а не отсутствие замера.
            "p50": percentile([r["total_ms"] for r in k_ok
                               if r.get("total_ms") is not None], 50),
        })
    return out


def by_day(rows: list[dict]) -> list[dict]:
    """Обращения по суткам (UTC, как в ts) — для графика. Старые слева."""
    days: dict[str, dict] = {}
    for rec in rows:
        day = (rec.get("ts") or "")[:10]
        if len(day) != 10:
            continue
        d = days.setdefault(day, {"date": day, "total": 0, "errors": 0, "fallback": 0})
        d["total"] += 1
        if rec.get("error"):
            d["errors"] += 1
        elif rec.get("answer_found") is False:
            d["fallback"] += 1
    return [days[k] for k in sorted(days)]


def top_questions(rows: list[dict], n: int = 20) -> list[list]:
    """Частые вопросы. Пусто, если тексты не писались (log_questions=off/hash)."""
    questions = [r["question"] for r in rows if r.get("question")]
    return [list(pair) for pair in Counter(q.strip() for q in questions).most_common(n)]


def top_unanswered(rows: list[dict], n: int = 20) -> list[list]:
    """Вопросы, на которые база НЕ ответила — прямой список на её пополнение.

    Самая полезная для АФМ выборка: показывает не «сколько промахов», а какие
    именно темы гражданам нужны, а базе неизвестны.
    """
    gaps = [r for r in rows if not r.get("error") and r.get("answer_found") is False]
    return top_questions(gaps, n)
