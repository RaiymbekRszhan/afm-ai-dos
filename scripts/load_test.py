"""Нагрузочный замер /voice: сколько держит один бэкенд на N киосков.

Зачем: ёмкость до сих пор оценивалась арифметикой по логам («2 слота × 4 с»),
а пилот расширяется на 20 точек. Скрипт бьёт РЕАЛЬНЫМИ голосовыми запросами
через ту же дверь, что и киоск (прокси video_ui -> оркестратор -> STT/RAG/TTS),
и показывает, где начинается очередь.

    python -m scripts.load_test --host 10.10.42.44                 # лестница 1,2,3,5
    python -m scripts.load_test --host 10.10.42.44 --levels 1,4,8  # свои уровни
    python -m scripts.load_test --host 10.10.42.44 --lang kazakh   # каз (ПЛАТНО на eleven)
    python -m scripts.load_test --host 10.10.42.44 --dry-run       # что будет сделано

Что меряет на каждом уровне конкурентности: p50/p95/max полного цикла, время до
ПЕРВОГО байта, долю ошибок и «налог очереди» — во сколько раз ответ медленнее,
чем при одиночном запросе. Именно этот налог, а не загрузка CPU, определяет,
сколько человек могут говорить одновременно, не замечая ожидания.

⚠️ Бьёт по ЖИВОМУ стеку: каждый запрос проходит STT + RAG + TTS на инфраструктуре
АФМ, а казахский на ElevenLabs ещё и тарифицируется. По умолчанию язык русский
(F5 на GPU АФМ — своё железо, бесплатно) и запросов немного.

Все обращения помечаются `kiosk=loadtest` (поле аналитики), поэтому в отчёте
`scripts.interactions_report` тестовая нагрузка отделима от живых граждан.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import httpx

# Метка точки для аналитики: тестовые обращения не должны портить статистику
# живых киосков — по этому значению они фильтруются в отчёте.
KIOSK_TAG = "loadtest"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * (p / 100)
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


async def _one(client: httpx.AsyncClient, url: str, audio: bytes, lang: str) -> dict:
    """Один голосовой запрос. Возвращает тайминги, не бросает — сбой это тоже данные."""
    t0 = time.perf_counter()
    try:
        r = await client.post(
            url,
            files={"data": ("q.wav", audio, "audio/wav")},
            data={"language": lang, "kiosk": KIOSK_TAG},
        )
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            return {"ok": False, "sec": dt, "err": f"HTTP {r.status_code}"}
        body = r.json()
        return {
            "ok": True,
            "sec": dt,
            "provider": body.get("provider"),
            "found": bool(body.get("answer")),
            # Размер ответа важен для филиалов: он уезжает по WAN целиком.
            "kb": len(r.content) / 1024,
            "chars": len(body.get("answer") or ""),
        }
    except Exception as e:
        return {"ok": False, "sec": time.perf_counter() - t0, "err": type(e).__name__}


async def _one_stream(client: httpx.AsyncClient, url: str, audio: bytes, lang: str) -> dict:
    """То же через /voice/stream: меряем время до ПЕРВОГО КУСКА звука.

    Это и есть задержка, которую чувствует гражданин: аватар заговорил — ждать
    больше не нужно, остальное досинтезируется, пока звучит начало. Полное время
    (`sec`) тут тоже считаем — по нему видно, сколько всего длился синтез.
    """
    t0 = time.perf_counter()
    first = None
    try:
        async with client.stream(
            "POST", url,
            files={"data": ("q.wav", audio, "audio/wav")},
            data={"language": lang, "kiosk": KIOSK_TAG},
        ) as r:
            if r.status_code != 200:
                return {"ok": False, "sec": time.perf_counter() - t0,
                        "err": f"HTTP {r.status_code}"}
            size = chunks = 0
            chars = 0
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                size += len(line)
                # Разбираем событие: тип нужен и для отсечки первого звука, и для
                # счётчика кусков — именно он объясняет, есть ли чему течь.
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                if ev.get("type") == "meta":
                    chars = len(ev.get("answer") or "")
                elif ev.get("type") == "audio":
                    chunks += 1
                    if first is None:
                        first = time.perf_counter() - t0
            return {"ok": True, "sec": time.perf_counter() - t0, "first": first,
                    "kb": size / 1024, "chunks": chunks, "chars": chars}
    except Exception as e:
        return {"ok": False, "sec": time.perf_counter() - t0, "err": type(e).__name__}


async def _level(url: str, audio: bytes, lang: str, n: int, timeout: float,
                 stream: bool = False) -> list[dict]:
    """N одновременных запросов — имитация N говорящих киосков в один момент."""
    one = _one_stream if stream else _one
    async with httpx.AsyncClient(timeout=timeout) as client:
        return list(await asyncio.gather(*[one(client, url, audio, lang) for _ in range(n)]))


def _report(level: int, res: list[dict], base: float | None) -> float | None:
    ok = [r for r in res if r["ok"]]
    bad = [r for r in res if not r["ok"]]
    if not ok:
        errs = ", ".join(sorted({r.get("err", "?") for r in bad}))
        print(f"  {level:>2} одновременно | ВСЕ ЗАПРОСЫ УПАЛИ: {errs}")
        return base
    secs = [r["sec"] for r in ok]
    p50, p95, mx = _percentile(secs, 50), _percentile(secs, 95), max(secs)
    tax = f"×{p50 / base:.1f}" if base else "—"
    kb = statistics.mean(r["kb"] for r in ok)
    # В потоковом режиме главное число — не полное время, а «когда заговорил».
    # Рядом печатаем число КУСКОВ: если кусок один, течь нечему и «первый звук»
    # неизбежно равен полному времени — виноват не стриминг, а размер куска
    # (ELEVENLABS_MAX_CHARS / TTS_MAX_CHARS) либо слишком короткий ответ.
    firsts = [r["first"] for r in ok if r.get("first") is not None]
    chunks = [r["chunks"] for r in ok if r.get("chunks") is not None]
    first_txt = (f" | ПЕРВЫЙ ЗВУК p50 {_percentile(firsts, 50):4.1f} c"
                 f" p95 {_percentile(firsts, 95):4.1f} c") if firsts else ""
    if chunks:
        first_txt += f" | кусков {min(chunks)}-{max(chunks)}"
    chars = [r["chars"] for r in ok if r.get("chars")]
    chars_txt = f" | ответ {int(statistics.mean(chars))} симв." if chars else ""
    print(f"  {level:>2} одновременно | p50 {p50:5.1f} c | p95 {p95:5.1f} c | max {mx:5.1f} c"
          f" | налог очереди {tax:>5} | {kb:.0f} КБ" + chars_txt + first_txt
          + (f" | ОШИБОК {len(bad)}/{len(res)}" if bad else ""))
    return base or p50


def main() -> None:
    ap = argparse.ArgumentParser(description="Нагрузочный замер /voice (живой стек)")
    ap.add_argument("--host", default="10.10.42.44", help="адрес киоск-фронтенда (video_ui)")
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--lang", default="russian", choices=["russian", "kazakh"],
                    help="kazakh идёт на ElevenLabs — ПЛАТНО (~$0.08 за ответ)")
    ap.add_argument("--levels", default="1,2,3,5",
                    help="уровни конкурентности через запятую")
    ap.add_argument("--audio", default=None, help="WAV с вопросом (по умолчанию refs/ref_<lang>.wav)")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--pause", type=float, default=5.0,
                    help="пауза между уровнями, с (дать стеку остыть)")
    ap.add_argument("--stream", action="store_true",
                    help="через /voice/stream: меряет время до ПЕРВОГО звука "
                         "(то, что реально ждёт гражданин)")
    ap.add_argument("--dry-run", action="store_true", help="показать план и выйти")
    args = ap.parse_args()

    ref = args.audio or ("refs/ref_kk.wav" if args.lang == "kazakh" else "refs/ref_ru.wav")
    if not os.path.exists(ref):
        sys.exit(f"нет файла с вопросом: {ref} (запускай из корня проекта)")
    audio = open(ref, "rb").read()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    url = f"http://{args.host}:{args.port}/voice" + ("/stream" if args.stream else "")
    total = sum(levels)

    print(f"\n=== Нагрузка на {url} ===")
    print(f"  вопрос:   {ref} ({len(audio) / 1024:.0f} КБ), язык {args.lang}")
    print(f"  уровни:   {levels}  (всего {total} запросов, метка kiosk={KIOSK_TAG})")
    if args.lang == "kazakh":
        print(f"  ⚠️  казахский идёт на ElevenLabs: ~${0.08 * total:.2f} за прогон")
    if args.dry_run:
        print("  (dry-run: ничего не отправлено)\n")
        return
    print()

    base: float | None = None
    for i, n in enumerate(levels):
        res = asyncio.run(_level(url, audio, args.lang, n, args.timeout, args.stream))
        base = _report(n, res, base)
        if i < len(levels) - 1:
            time.sleep(args.pause)

    print("\n  Как читать: «налог очереди» — во сколько раз медленнее одиночного")
    print("  запроса. ×1 — очереди нет; ×2 и выше — люди уже ждут друг друга,")
    print("  пора поднимать MAX_CONCURRENT_VOICE (если TTS-нода держит) или")
    print("  включать стриминг, чтобы ожидание не выглядело зависанием.")
    print(f"  Убрать тестовые строки из отчёта: они помечены kiosk={KIOSK_TAG}.\n")


if __name__ == "__main__":
    main()
