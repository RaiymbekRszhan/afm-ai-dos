#!/usr/bin/env python3
"""Бенч TTS Ai-dos: скорость синтеза и артефакты звука, замер «до/после».

Зачем: любую правку TTS (нарезка, паузы, паддинг, провайдер, референс голоса)
нужно принимать по числам, а не по ощущению. STT-сервер АФМ для этого НЕ годится
(он сам теряет хвосты длинного аудио и даёт ложные «съедания»), поэтому здесь
меряется то, что считается прямо по WAV: время синтеза, длительность, тишина по
краям, длина швов между кусками, «хвосты-сироты», клиппинг.

Гоняется на машине со стеком (нужен доступ к :8000 и к TTS-серверам АФМ):

    python -m scripts.tts_bench                      # вся батарея, каталог out/tts_bench/<дата>
    python -m scripts.tts_bench --lang ru            # только русский
    python -m scripts.tts_bench --only ru_long,ru_numbers
    python -m scripts.tts_bench --repeat 3           # медиана из 3 прогонов (F5 плавает)
    python -m scripts.tts_bench --baseline out/tts_bench/2026-07-28_1200   # сравнить с прошлым
    python -m scripts.tts_bench --preview            # БЕЗ сети: что уйдёт в TTS и как порежется

На выходе в каталоге прогона:
    report.json   — все метрики (машинно; его же скармливают как --baseline)
    report.md     — таблица для отчёта
    listen.html   — страница прослушки; с --baseline плееры «до/после» рядом
    NN_<id>.wav   — синтезированные ответы

Прослушка обязательна для просодии: ударения, темп и естественность швов числами
не меряются. Всё остальное здесь автоматом.

Только stdlib (+ опционально app.* для предпросмотра нарезки) — работает офлайн.
"""
from __future__ import annotations

import argparse
import array
import html
import io
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime

# --- Пороги анализа звука ---------------------------------------------------
# SPEECH_THRESH — тот же порог, что у подрезки в бою (_EDGE_THRESH в
# app/clients/tts.py): ниже него считаем тишиной. Громкость меряем окнами по
# WIN_MS, а не посэмплово: волна постоянно пересекает ноль, и поэлементный
# критерий дробил бы речь на тысячи микро-сегментов.
WIN_MS = 10
SPEECH_THRESH = 300
# Тишина длиннее — это шов между кусками или пауза между фразами (наши зазоры
# 280/140 мс + подрезанные края дают ~230–370 мс).
SEAM_MIN_MS = 150
# «Хвост-сирота» — тот же критерий, что у резака в бою (_BLOB_* в tts.py).
ORPHAN_MAX_MS = 500
ORPHAN_GAP_MS = 180
ORPHAN_MIN_SPEECH_MS = 1500

# --- Батарея --------------------------------------------------------------
# Основа — РЕАЛЬНЫЕ ответы стенда (logs/interactions.jsonl, 27.07), обрезанные до
# целого предложения: бенч должен мерить то, что слышит гражданин, а не
# синтетику. Три фразы с пометкой «проба» собраны нарочно — под нормализацию
# (даты, аббревиатуры, казахские числа), их правильное чтение проверяется ушами.
BATTERY: list[dict] = [
    {
        "id": "ru_short", "lang": "russian", "tag": "короткий ответ",
        "text": "К сожалению, по этому вопросу у меня нет точной информации в базе "
                "Агентства. Рекомендую обратиться в call-центр Агентства по номеру 1458. "
                "Звонок бесплатный. Также можно подать обращение через платформу e-Otinish.",
    },
    {
        "id": "ru_mid", "lang": "russian", "tag": "средний ответ, телефон, срок",
        "text": "Чтобы подать заявление за наличный прием, обратитесь в канцелярию "
                "Агентства Республики Казахстан по финансовому мониторингу с письменным "
                "обращением, в котором изложите суть вопроса, укажите свои контактные "
                "данные и приложите документы, если они есть. Заявление принимается в "
                "рабочие дни, и оно будет зарегистрировано в тот же день. Предварительную "
                "запись на приём можно оформить по телефону 32-13-04. Срок рассмотрения "
                "обращения — 15 рабочих дней.",
    },
    {
        "id": "ru_long", "lang": "russian", "tag": "длинный ответ, много швов",
        "text": "Как свидетель, вы имеете право отказаться от дачи показаний, если они "
                "могут повлечь преследование вас, вашего супруга или близких "
                "родственников; давать показания на родном языке или языке, которым "
                "владеете; пользоваться бесплатной помощью переводчика; собственноручно "
                "записывать свои показания в протоколе; заявлять отводы; приносить жалобы "
                "на действия дознавателя, следователя или прокурора; а также знакомиться с "
                "протоколами следственных действий, в которых вы участвовали, и подавать "
                "на них замечания. Права свидетеля разъясняются перед первым допросом. "
                "Допрос проводится в дневное время, не может продолжаться непрерывно более "
                "четырех часов, а в течение дня — более восьми часов.",
    },
    {
        "id": "ru_numbers", "lang": "russian", "tag": "числа, МРП, диапазоны, статьи",
        "text": "Штрафы за нарушения в сфере финансового мониторинга устанавливаются по "
                "статье 214 Кодекса об административных правонарушениях Республики "
                "Казахстан. Например, за несвоевременное предоставление информации об "
                "операциях — штраф от 30 до 280 МРП в зависимости от категории нарушителя; "
                "за предоставление недостоверной информации — от 45 до 450 МРП; за "
                "неприменение мер по проверке клиентов — от 30 до 280 МРП. За несоответствие "
                "правил внутреннего контроля законодательству — от 110 до 700 МРП.",
    },
    {
        "id": "ru_table", "lang": "russian", "tag": "экранная таблица + телефоны",
        "text": "Чтобы срочно заблокировать карту, позвоните на горячую линию своего банка "
                "— номера всех банков показаны в таблице на экране. Карту также можно "
                "заблокировать через мобильное приложение банка.\n\n"
                "[ТАБЛИЦА]\nБанк | Номер\nKaspi Bank | 9999\nForte Bank | 7575\n"
                "Алтын Банк | +7 727 356 57 77\nЕвразийский банк | +7 771 000 77 22\n"
                "[/ТАБЛИЦА]",
    },
    {
        "id": "ru_dates", "lang": "russian", "tag": "проба: даты и диапазоны",
        "text": "Заявление подано 1 января 2024 года, а изменения применяются с 15 марта "
                "2025 года. Срок рассмотрения — от 5 до 30 рабочих дней. Ответ направлен "
                "3 февраля 2026 года по статье 63 Административного "
                "процедурно-процессуального кодекса.",
    },
    {
        "id": "ru_abbr", "lang": "russian", "tag": "проба: аббревиатуры и латиница",
        "text": "Субъект финансового мониторинга обязан сообщить в АФМ по правилам ПОД/ФТ. "
                "Порог — 5 000 000 тенге, штраф до 700 МРП по статье 214 КоАП РК и статье "
                "218 УК РК. Подать обращение можно через e-Gov, e-Otinish или приложение "
                "Kaspi; звонок по номеру 8 800 080 18 90 бесплатный.",
    },
    {
        "id": "kk_short", "lang": "kazakh", "tag": "короткий ответ",
        "text": "К сожалению, по этому вопросу у меня нет точной информации в базе "
                "Агентства. Рекомендую обратиться в call-центр Агентства по номеру 1458. "
                "Звонок бесплатный. Қаласаңыз, өтініш үлгісін басып шығарып бере аламын — "
                "«Үлгіні басып шығару» түймесін басыңыз.",
    },
    {
        "id": "kk_mid", "lang": "kazakh", "tag": "средний ответ, телефон, срок",
        "text": "Жеке қабылдауға жазылу үшін өтініш жазу қажет емес — сіз e-Otinish "
                "платформасы арқылы немесе Агенттіктің кеңсесінде қағаз тасығышта жолданым "
                "қалдырып, жазылуыңыз мүмкін. Жолданым беру кезінде мәселенің мәнін "
                "баяндау, кері байланыс үшін байланыс деректерін көрсету қажет. Жолданымды "
                "қарау мерзімі — 15 жұмыс күні. Жазылуға 32-13-04 телефоны арқылы да "
                "байланыс болады.",
    },
    {
        "id": "kk_subjects", "lang": "kazakh", "tag": "длинный ответ, перечисление",
        "text": "Қаржы мониторингінің субъектілері «Қылмыстық жолмен алынған кірістерді "
                "заңдастыруға (жылыстатуға) және терроризмді қаржыландыруға қарсы іс-қимыл "
                "туралы» Заңның 3-бабында көрсетілген. Оларға банктер, биржалар, "
                "сақтандыру ұйымдары, зейнетақы қорлары, бағалы қағаздар нарығының кәсіби "
                "қатысушылары, нотариустар, адвокаттар, бухгалтерлік ұйымдар, ойын бизнесін "
                "ұйымдастырушылар, почта операторлары, микроқаржылық ұйымдар және төлем "
                "ұйымдары кіреді.",
    },
    {
        "id": "kk_table", "lang": "kazakh", "tag": "экранная таблица + латиница",
        "text": "Егер банктен хабарласып жатса, алдымен тексеріңіз: банк құпиясөз, PIN, CVV "
                "немесе SMS-кодты сұрамайды. Егер сұраса — қоңырауды үзіп, банкке ресми "
                "нөмір арқылы өзіңіз хабарласыңыз. Ресми банктер WhatsApp арқылы қоңырау "
                "шалмайды.\n\n[КЕСТЕ]\nБанк | Нөмір\nKaspi Bank | 9999\n"
                "Алтын Банк | +7 727 356 57 77\n[/КЕСТЕ]",
    },
    {
        "id": "kk_numbers", "lang": "kazakh", "tag": "проба: числа, годы, номер",
        "text": "Заңның 3-бабында көрсетілген талаптар 2024 жылдан бастап қолданылады. "
                "Айыппұл 30 айлық есептік көрсеткіштен 280 айлық есептік көрсеткішке дейін "
                "белгіленеді. Толық ақпаратты 1458 нөмірі арқылы алуға болады.",
    },
]


# --- Анализ WAV -------------------------------------------------------------
def analyze(blob: bytes) -> dict:
    """Метрики звука: длительность, края, швы, сироты, громкость, клиппинг."""
    try:
        with wave.open(io.BytesIO(blob), "rb") as r:
            nch, sw, fr = r.getnchannels(), r.getsampwidth(), r.getframerate()
            raw = r.readframes(r.getnframes())
    except (wave.Error, EOFError) as e:
        return {"error": f"не WAV: {e}"}
    if sw != 2:
        return {"error": f"не 16-битный WAV (sampwidth={sw})"}
    samples = array.array("h")
    samples.frombytes(raw)
    n_frames = len(samples) // nch if nch else 0
    if not n_frames:
        return {"error": "пустой WAV"}

    win = max(1, int(fr * WIN_MS / 1000))
    win_ms = win / fr * 1000
    peaks: list[int] = []
    total_sq = 0
    clipped = 0
    for start in range(0, n_frames, win):
        hi = 0
        for i in range(start * nch, min((start + win) * nch, len(samples))):
            v = abs(samples[i])
            if v > hi:
                hi = v
            total_sq += v * v
            if v >= 32700:
                clipped += 1
        peaks.append(hi)

    loud = [p >= SPEECH_THRESH for p in peaks]
    # Прогоны речи в окнах: [(индекс_начала, индекс_конца_включительно), ...]
    runs: list[tuple[int, int]] = []
    start_i = None
    for i, is_loud in enumerate(loud):
        if is_loud and start_i is None:
            start_i = i
        elif not is_loud and start_i is not None:
            runs.append((start_i, i - 1))
            start_i = None
    if start_i is not None:
        runs.append((start_i, len(loud) - 1))

    duration_ms = round(n_frames / fr * 1000)
    rms = (total_sq / (len(samples) or 1)) ** 0.5
    out = {
        "duration_ms": duration_ms,
        "framerate": fr,
        "channels": nch,
        "peak": max(peaks) if peaks else 0,
        "rms": round(rms, 1),
        "clipped_samples": clipped,
    }
    if not runs:
        out.update({"error_soft": "речь не обнаружена (тишина)", "speech_ms": 0,
                    "lead_ms": duration_ms, "tail_ms": duration_ms, "seams": [],
                    "orphan_tail": False})
        return out

    lead_ms = round(runs[0][0] * win_ms)
    tail_ms = round((len(loud) - 1 - runs[-1][1]) * win_ms)
    seams = [round((runs[i + 1][0] - runs[i][1] - 1) * win_ms) for i in range(len(runs) - 1)]
    seams = [s for s in seams if s >= SEAM_MIN_MS]
    speech_ms = round(sum(e - s + 1 for s, e in runs) * win_ms)

    # Сирота: короткий всплеск в самом конце через заметную тишину после речи.
    orphan = False
    if len(runs) > 1:
        blob_ms = (runs[-1][1] - runs[-1][0] + 1) * win_ms
        gap_ms = (runs[-1][0] - runs[-2][1] - 1) * win_ms
        before_ms = sum(e - s + 1 for s, e in runs[:-1]) * win_ms
        orphan = (blob_ms <= ORPHAN_MAX_MS and gap_ms >= ORPHAN_GAP_MS
                  and before_ms >= ORPHAN_MIN_SPEECH_MS)

    out.update({
        "speech_ms": speech_ms,
        "lead_ms": lead_ms,
        "tail_ms": tail_ms,
        "n_runs": len(runs),
        "seams": seams,
        "seam_min": min(seams) if seams else None,
        "seam_max": max(seams) if seams else None,
        "seam_spread": (max(seams) - min(seams)) if seams else None,
        "orphan_tail": orphan,
    })
    return out


# --- Синтез -----------------------------------------------------------------
def speak(api: str, text: str, lang: str, timeout: float) -> tuple[bytes, int]:
    """POST /speak -> (WAV-байты, время ответа в мс). Ошибку поднимаем наверх."""
    body = json.dumps({"text": text, "language": lang}).encode("utf-8")
    req = urllib.request.Request(
        api.rstrip("/") + "/speak", data=body,
        headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read()
    return blob, round((time.perf_counter() - t0) * 1000)


def _http_error_text(e: urllib.error.HTTPError) -> str:
    try:
        detail = json.loads(e.read().decode("utf-8", "ignore")).get("detail", "")
    except Exception:
        detail = ""
    return f"HTTP {e.code} {detail}".strip()


def load_planner():
    """tts.prepare_for_tts, если запущено в venv оркестратора (иначе None).

    Нужна для предпросмотра нарезки и для замера первого куска — того самого
    времени до первого звука, ради которого делается стриминг.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from app.clients.tts import prepare_for_tts
        return prepare_for_tts
    except Exception as e:  # запущено вне venv — не беда, метрики звука не пострадают
        print(f"[bench] предпросмотр нарезки недоступен ({e.__class__.__name__}: {e})")
        return None


# --- Отчёты -----------------------------------------------------------------
def _fmt(v, dash="—"):
    return dash if v is None else v


def write_markdown(rows: list[dict], path: str, baseline: dict | None) -> None:
    lines = [
        f"# Бенч TTS — {os.path.basename(os.path.dirname(path))}",
        "",
        "RTF = длительность звука / время синтеза (>1 — быстрее реального времени).",
        "«1-й кусок» — время синтеза первого куска: столько ждал бы гражданин при",
        "стриминге вместо полного времени.",
        "",
        "| id | язык | симв | кусков | время, мс | 1-й кусок, мс | звук, мс | RTF | мс/симв | края (нач/кон) | швы | сирота |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|",
    ]
    for r in rows:
        a = r.get("audio") or {}
        seams = a.get("seams") or []
        seam_txt = f"{len(seams)}× {a['seam_min']}–{a['seam_max']}" if seams else "—"
        delta = ""
        if baseline and r["id"] in baseline:
            b = baseline[r["id"]]
            if b.get("t_ms") and r.get("t_ms"):
                diff = r["t_ms"] - b["t_ms"]
                delta = f" ({diff:+d})"
        lines.append(
            f"| {r['id']} | {r['lang'][:2]} | {r['chars']} | {_fmt(r.get('n_chunks'))} | "
            f"{_fmt(r.get('t_ms'))}{delta} | {_fmt(r.get('t_first_ms'))} | "
            f"{_fmt(a.get('duration_ms'))} | {_fmt(r.get('rtf'))} | {_fmt(r.get('ms_per_char'))} | "
            f"{_fmt(a.get('lead_ms'))}/{_fmt(a.get('tail_ms'))} | {seam_txt} | "
            f"{'ДА' if a.get('orphan_tail') else '—'} |"
        )
        if r.get("error"):
            lines.append(f"| ↳ | | | | **ошибка:** {r['error']} | | | | | | | |")
    lines += ["", "## Итог по языкам", ""]
    for lang in ("russian", "kazakh"):
        got = [r for r in rows if r["lang"] == lang and r.get("t_ms")]
        if not got:
            continue
        med = statistics.median([r["ms_per_char"] for r in got if r.get("ms_per_char")])
        first = [r["t_first_ms"] for r in got if r.get("t_first_ms")]
        lines.append(
            f"- **{lang}**: фраз {len(got)}, медиана {med:.1f} мс/симв, "
            f"полное время {min(r['t_ms'] for r in got)}–{max(r['t_ms'] for r in got)} мс"
            + (f", первый кусок {min(first)}–{max(first)} мс" if first else "")
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


_HTML_HEAD = """<meta charset="utf-8">
<title>Бенч TTS — прослушка</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;margin:24px;max-width:1100px}
 h1{font-size:20px} .row{border:1px solid #ddd;border-radius:8px;padding:12px;margin:12px 0}
 .id{font-weight:600} .tag{color:#666;font-size:13px}
 .txt{white-space:pre-wrap;color:#333;background:#fafafa;padding:8px;border-radius:6px;
      margin:8px 0;font-size:13px;max-height:8em;overflow:auto}
 .m{font-size:13px;color:#444} .m b{color:#000}
 audio{width:100%;margin-top:6px} .ab{display:flex;gap:16px} .ab>div{flex:1}
 .warn{color:#b00}
</style>
"""


def write_html(rows: list[dict], path: str, baseline_dir: str | None) -> None:
    out_dir = os.path.dirname(path)
    parts = [_HTML_HEAD, "<h1>Бенч TTS — прослушка</h1>"]
    if baseline_dir:
        parts.append(f"<p>Сравнение с базой: <code>{html.escape(baseline_dir)}</code> "
                     f"(слева «до», справа «после»).</p>")
    parts.append("<p>Слушать на швы (наезд/затянутость), концы слов, ударения и темп. "
                 "Числа считает report.md — уши нужны для просодии.</p>")
    for r in rows:
        a = r.get("audio") or {}
        parts.append('<div class="row">')
        parts.append(f'<div class="id">{html.escape(r["id"])} '
                     f'<span class="tag">{html.escape(r["tag"])} · {r["lang"]}</span></div>')
        parts.append(f'<div class="txt">{html.escape(r["text"])}</div>')
        if r.get("error"):
            parts.append(f'<div class="warn">ошибка: {html.escape(r["error"])}</div></div>')
            continue
        seams = a.get("seams") or []
        parts.append(
            f'<div class="m">время <b>{r.get("t_ms")} мс</b>'
            + (f' · первый кусок <b>{r["t_first_ms"]} мс</b>' if r.get("t_first_ms") else "")
            + f' · звук {a.get("duration_ms")} мс · кусков {r.get("n_chunks")}'
            f' · края {a.get("lead_ms")}/{a.get("tail_ms")} мс'
            + (f' · швы {len(seams)}× {a.get("seam_min")}–{a.get("seam_max")} мс' if seams else "")
            + (' · <span class="warn">хвост-сирота</span>' if a.get("orphan_tail") else "")
            + "</div>")
        cur = f'<audio controls preload="none" src="{html.escape(r["wav"])}"></audio>'
        base_wav = None
        if baseline_dir:
            cand = os.path.join(baseline_dir, r["wav"])
            if os.path.isfile(cand):
                base_wav = os.path.relpath(cand, out_dir)
        if base_wav:
            parts.append(
                f'<div class="ab"><div>до<audio controls preload="none" '
                f'src="{html.escape(base_wav)}"></audio></div>'
                f'<div>после{cur}</div></div>')
        else:
            parts.append(cur)
        parts.append("</div>")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts) + "\n")


# --- Прогон -----------------------------------------------------------------
def preview(items: list[dict], planner) -> None:
    """Без сети: что уйдёт в TTS после нормализации и как порежется на куски."""
    if planner is None:
        print("Предпросмотр требует venv оркестратора (нужен app.config).")
        return
    for it in items:
        speech, chunks = planner(it["text"], it["lang"])
        print(f"\n=== {it['id']} [{it['lang']}] {it['tag']} — "
              f"{len(it['text'])} симв -> {len(speech)} после нормализации, "
              f"кусков {len(chunks)} ===")
        print(speech)
        for i, c in enumerate(chunks):
            print(f"  [{i}] {len(c):3d} симв: {c[:90]}{'…' if len(c) > 90 else ''}")


# Короткая фраза для прогрева: первый запрос к GPU-ноде АФМ обходится в разы
# дороже последующих (замер 27.07: 13.5 c против 1.3 c на сопоставимой фразе —
# RTF 0.95 против 8.5). Без прогрева этот штраф садится на первую фразу батареи
# и портит и её метрику, и сравнение прогонов между собой.
WARMUP = {"russian": "Проверка связи.", "kazakh": "Байланысты тексеру."}


def warmup(args, items: list[dict]) -> None:
    for lang in dict.fromkeys(it["lang"] for it in items):
        try:
            _, ms = speak(args.api, WARMUP[lang], lang, args.timeout)
            print(f"[bench] прогрев {lang}: {ms} мс (в отчёт не идёт)", flush=True)
        except Exception as e:
            print(f"[bench] прогрев {lang} не удался: {e!r}")


def run(args, items: list[dict], planner) -> list[dict]:
    rows: list[dict] = []
    for idx, it in enumerate(items, 1):
        chunks = None
        if planner is not None:
            try:
                _, chunks = planner(it["text"], it["lang"])
            except Exception as e:
                print(f"[bench] нарезка {it['id']} не посчиталась: {e!r}")
        row = {
            "id": it["id"], "lang": it["lang"], "tag": it["tag"], "text": it["text"],
            "chars": len(it["text"]), "n_chunks": len(chunks) if chunks else None,
            "wav": f"{idx:02d}_{it['id']}.wav",
        }
        print(f"[{idx}/{len(items)}] {it['id']} ({len(it['text'])} симв)...", flush=True)
        times: list[int] = []
        blob = b""
        try:
            for _ in range(args.repeat):
                blob, ms = speak(args.api, it["text"], it["lang"], args.timeout)
                times.append(ms)
                print(f"      прогон: {ms} мс", flush=True)
        except urllib.error.HTTPError as e:
            row["error"] = _http_error_text(e)
        except Exception as e:
            row["error"] = f"{e.__class__.__name__}: {e}"
        if times:
            row["t_ms"] = round(statistics.median(times))
            row["t_ms_all"] = times
            row["audio"] = analyze(blob)
            dur = row["audio"].get("duration_ms")
            if dur:
                row["rtf"] = round(dur / row["t_ms"], 2)
            row["ms_per_char"] = round(row["t_ms"] / max(1, row["chars"]), 1)
            with open(os.path.join(args.out, row["wav"]), "wb") as f:
                f.write(blob)
        # Время до первого звука при стриминге = синтез ОДНОГО первого куска.
        # Меряем уже сейчас, чтобы у Этапа 3 была честная цель, а не оценка.
        if chunks and len(chunks) > 1 and not args.no_first_chunk and not row.get("error"):
            try:
                _, ms = speak(args.api, chunks[0], it["lang"], args.timeout)
                row["t_first_ms"] = ms
                print(f"      первый кусок: {ms} мс", flush=True)
            except Exception as e:
                print(f"      первый кусок не замерен: {e!r}")
        if row.get("error"):
            print(f"      ОШИБКА: {row['error']}")
        rows.append(row)
    return rows


def main() -> int:
    p = argparse.ArgumentParser(description="Бенч TTS Ai-dos (скорость + артефакты)")
    p.add_argument("--api", default=os.environ.get("API", "http://localhost:8000"),
                   help="адрес оркестратора (по умолчанию http://localhost:8000)")
    p.add_argument("--lang", choices=["ru", "kk", "both"], default="both")
    p.add_argument("--only", default="", help="список id через запятую")
    p.add_argument("--repeat", type=int, default=1, help="прогонов на фразу (медиана)")
    p.add_argument("--timeout", type=float, default=300.0, help="таймаут запроса, с")
    p.add_argument("--out", default="", help="каталог прогона (по умолчанию out/tts_bench/<дата>)")
    p.add_argument("--baseline", default="", help="каталог прошлого прогона для сравнения")
    p.add_argument("--preview", action="store_true", help="без сети: нормализация и нарезка")
    p.add_argument("--no-first-chunk", action="store_true",
                   help="не мерить отдельно первый кусок (на один запрос меньше)")
    p.add_argument("--no-warmup", action="store_true",
                   help="не прогревать TTS перед батареей (тогда первая фраза "
                        "получит штраф холодного старта GPU-ноды)")
    args = p.parse_args()

    items = BATTERY
    if args.lang != "both":
        want = "russian" if args.lang == "ru" else "kazakh"
        items = [i for i in items if i["lang"] == want]
    if args.only:
        ids = {s.strip() for s in args.only.split(",") if s.strip()}
        unknown = ids - {i["id"] for i in BATTERY}
        if unknown:
            print(f"Неизвестные id: {', '.join(sorted(unknown))}")
            return 2
        items = [i for i in items if i["id"] in ids]
    if not items:
        print("Батарея пуста после фильтров.")
        return 2

    planner = load_planner()
    if args.preview:
        preview(items, planner)
        return 0

    args.out = args.out or os.path.join(
        "out", "tts_bench", datetime.now().strftime("%Y-%m-%d_%H%M"))
    os.makedirs(args.out, exist_ok=True)
    print(f"[bench] {args.api} -> {args.out} ({len(items)} фраз, повторов {args.repeat})")

    if not args.no_warmup:
        warmup(args, items)
    rows = run(args, items, planner)

    baseline = None
    if args.baseline:
        try:
            with open(os.path.join(args.baseline, "report.json"), encoding="utf-8") as f:
                baseline = {r["id"]: r for r in json.load(f)["rows"]}
        except Exception as e:
            print(f"[bench] база для сравнения не прочитана: {e!r}")

    meta = {"api": args.api, "ts": datetime.now().isoformat(timespec="seconds"),
            "repeat": args.repeat, "baseline": args.baseline or None,
            "warmup": not args.no_warmup}
    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "rows": rows}, f, ensure_ascii=False, indent=2)
    write_markdown(rows, os.path.join(args.out, "report.md"), baseline)
    write_html(rows, os.path.join(args.out, "listen.html"), args.baseline or None)

    failed = [r["id"] for r in rows if r.get("error")]
    print(f"\nГотово: {args.out}")
    print(f"  report.md    — таблица метрик")
    print(f"  listen.html  — прослушка (открыть в браузере)")
    if failed:
        print(f"  ОШИБКИ: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
