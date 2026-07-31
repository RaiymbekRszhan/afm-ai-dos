import asyncio
import base64
import csv
import io
import json
import logging
import os
import re
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from time import perf_counter

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import analytics, kiosks, logging_setup, service
from app.clients import rag, stt, tts
from app.config import settings
from app.schemas import ChatRequest, ChatResponse, SpeakRequest, TranscribeResponse

log = logging.getLogger("ai_dos.api")


def _ms(t0: float) -> int:
    """Миллисекунды с момента t0 (perf_counter) — для таймингов стадий."""
    return round((perf_counter() - t0) * 1000)

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Общий ограничитель конкурентности TTS/GPU: защищает и /voice (весь дорогой
# пайплайн), и прямой /speak (см. _guarded_synthesize) — иначе пачка запросов
# кладёт единственную TTS/GPU-ноду.
_tts_sem = asyncio.Semaphore(max(1, settings.max_concurrent_voice))


async def _guarded_synthesize(text: str, language: str | None) -> bytes:
    """Синтез под общим семафором TTS/GPU (N12). /speak идёт через него, чтобы
    прямые вызовы не заняли единственный ресурс мимо лимита, как уже сделано для
    /voice. НЕ звать из-под уже удержанного _tts_sem (в /voice) — семафор не
    реентрантен, при лимите 1 это дедлок; /voice синтезирует, уже держа семафор."""
    async with _tts_sem:
        return await tts.synthesize(text, language)
# Ссылка на фоновый прогрев Whisper — иначе GC может убить task до завершения.
_warmup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _warmup_task
    # Логирование: ops -> journald, аналитика /voice -> logs/interactions.jsonl.
    logging_setup.configure_logging()
    # Источник ответа — внешний RAG-сервис (:8077). Локальный индекс больше не грузим.
    # Прогрев Whisper-kk в фоне, чтобы первый казахский запрос не тормозил.
    if settings.stt_kk_use_whisper:
        print("[startup] прогреваю Whisper-kk в фоне...")
        _warmup_task = asyncio.create_task(stt.warmup())
    yield


async def _read_upload(data: UploadFile) -> bytes:
    """Читает загруженное аудио с ограничением размера (защита от перегруза памяти).

    Сначала проверяем известный размер (data.size заполнен парсером ДО чтения в
    память) — чтобы не тянуть гигантский файл в RAM. Затем бэкстоп после чтения
    (если size неизвестен).
    """
    limit = settings.max_upload_mb * 1024 * 1024

    def _too_big(n: int) -> HTTPException:
        return HTTPException(
            status_code=413,
            detail=f"Файл больше {settings.max_upload_mb} МБ ({n // (1024 * 1024)} МБ)",
        )

    if data.size is not None and data.size > limit:
        raise _too_big(data.size)
    audio = await data.read()
    if len(audio) > limit:
        raise _too_big(len(audio))
    return audio


# docs_url/redoc_url/openapi_url=None — отключаем встроенные роуты Swagger (он тянет
# CSS/JS с CDN, а в сети АФМ нет интернета; схему в проде вообще не раскрываем).
# Ниже — свои /docs и /openapi.json, ОБА за флагом settings.enable_docs (N6).
app = FastAPI(title="АФМ — Цифровой офицер Ai-dos", lifespan=lifespan, docs_url=None,
              redoc_url=None, openapi_url=None)

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/ui", include_in_schema=False)
async def ui():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Иконка вкладки — чтобы браузер не сыпал 404 на /favicon.ico."""
    path = os.path.join(_STATIC_DIR, "swagger-ui", "favicon-32x32.png")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return Response(status_code=204)


@app.get("/openapi.json", include_in_schema=False)
async def openapi_json():
    """Схема API — только при enable_docs (в проде выключена, N6)."""
    if not settings.enable_docs:
        raise HTTPException(status_code=404)
    return app.openapi()


@app.get("/docs", include_in_schema=False)
async def custom_docs():
    """Swagger UI с локальными ассетами (без интернета). Только при enable_docs (N6)."""
    if not settings.enable_docs:
        raise HTTPException(status_code=404)
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=app.title,
        swagger_js_url="/static/swagger-ui/swagger-ui-bundle.js",
        swagger_css_url="/static/swagger-ui/swagger-ui.css",
        swagger_favicon_url="/static/swagger-ui/favicon-32x32.png",
    )


@app.get("/health")
async def health():
    """Статус оркестратора + доступность RAG-сервиса (источника ответа)."""
    return {
        "status": "ok",
        "rag": await rag.healthy(),
        "tts": {
            "enabled": settings.tts_enabled,
            "ru": settings.tts_provider,
            "kk": settings.tts_kk_provider,
            "servers": await tts.healthy(),
        },
        "stt_kk_whisper": settings.stt_kk_use_whisper,
    }


def _gate(request: Request, rec: dict, key: str | None) -> None:
    """Общая проходная для дорогих путей: пропуск точки + темп запросов.

    Порядок важен: сначала дешёвые проверки, потом STT/RAG/TTS. Оба отказа
    обходятся серверу почти бесплатно, а именно они и защищают от того, что
    один клиент выест облако и оба слота семафора.

    ⚠️ Поле `error` ставим ЗДЕСЬ, а не у вызывающего. Раньше /voice этого не
    делал, и отказ уходил в аналитику как УСПЕШНОЕ обращение — с пустым вопросом
    и нулевой задержкой, то есть завышал и объём, и качество (поймано тестом
    30.07 перед включением строгого режима пропуска). Если строку ставит
    вызывающий, достаточно забыть её в новом месте, чтобы это повторилось.
    """
    kiosk = rec["kiosk"]
    kiosks.note_key(kiosk, bool(key))
    if not kiosks.key_ok(kiosk, key):
        rec["error"] = "gate"
        log.warning("kiosk=%s: неверный пропуск, отказ", kiosk or "-")
        raise HTTPException(status_code=403, detail="Киоск не опознан.")
    # Не назвалась — считаем по адресу, иначе анонимный поток обходил бы лимит.
    who = kiosk or (request.client.host if request.client else "unknown")
    if not kiosks.rate_ok(who):
        rec["error"] = "gate"
        log.warning("%s: превышен темп запросов (%s/мин), отказ",
                    who, settings.kiosk_rate_per_min)
        raise HTTPException(status_code=429,
                            detail="Слишком много запросов. Подождите минуту.")


@app.post("/kiosk/ping")
async def kiosk_ping(kiosk: str = Form(default=None), key: str = Form(default=None)):
    """Точка отмечается живой и узнаёт, не отключили ли её.

    Два дела за один дешёвый запрос. Первое — heartbeat: без него погасшую точку
    видно только по ОТСУТСТВИЮ строк в отчёте, то есть постфактум и на глаз.
    Второе — киоск узнаёт об отключении ДО того, как гражданин заговорит: иначе
    он подойдёт, нажмёт, продиктует вопрос и только потом получит отказ.
    """
    kiosk_id = _clean_kiosk(kiosk)
    # Пинг дешёвый, но подделанный пинг рисовал бы погасшую точку живой —
    # значит пропуск сверяем и здесь. Темп пинга не ограничиваем: он и так редкий.
    kiosks.note_key(kiosk_id, bool(key))
    if not kiosks.key_ok(kiosk_id, key):
        raise HTTPException(status_code=403, detail="Киоск не опознан.")
    kiosks.touch_ping(kiosk_id)
    blocked = kiosks.disabled_message(kiosk_id)
    return {
        "enabled": blocked is None,
        "message": blocked or "",
        "ping_seconds": settings.kiosk_ping_seconds,
    }


def _fleet_payload(days: int) -> dict:
    """Флот + цифры из логов за период.

    Живость (`last_seen`) берём из памяти — она по природе моментальная. А вот
    «сколько вопросов» — ИЗ ЛОГОВ: счётчик в памяти обнуляется при рестарте api,
    и в отчёте это была бы ложь.
    """
    rows = analytics.filter_rows(analytics.load(settings.log_dir, days))
    stats = {k["kiosk"]: k for k in analytics.by_kiosk(rows)}
    fleet = kiosks.status_rows()

    # Точка, которая есть В ЛОГАХ, но не в списке флота и сейчас не пингует
    # (старый киоск, переименованный регион, записи пилота без имени), иначе
    # выпала бы из таблицы совсем — и её история стала бы невидимой, хотя
    # обращения были.
    known = {row["kiosk"] for row in fleet}
    for kiosk_id in stats:
        if kiosk_id in known:
            continue
        fleet.append({
            "kiosk": kiosk_id,
            "human": "(нет в списке флота)" if kiosk_id != "-" else "(без имени точки)",
            "enabled": kiosks.disabled_message(kiosk_id) is None,
            "disabled_here": False,
            "message": "",
            "online": False, "ping_ago_s": None, "ask_ago_s": None, "asks": 0,
            "in_fleet": False,
        })

    for row in fleet:
        s = stats.get(row["kiosk"])
        row["period_asks"] = s["total"] if s else 0
        row["period_fallback"] = s["fallback"] if s else 0
        row["period_fallback_pct"] = s["fallback_pct"] if s else "—"
        # Сбои отдельно от отказов рубильника: отключённый регион не сломан.
        row["period_failures"] = s["failures"] if s else 0
        row["period_refused"] = s["refused"] if s else 0
        row["period_errors"] = s["errors"] if s else 0
        row["period_p50"] = s["p50"] if s else None
    return {
        "kiosks": fleet,
        "maintenance": kiosks.maintenance_message(),
        "offline_after_s": settings.kiosk_offline_after_s,
        "days": days,
        "key_required": settings.kiosk_key_required,
        # Сколько точек обращалось БЕЗ пропуска: пока это не ноль, включать
        # строгий режим нельзя — они получат отказ.
        "without_key": sum(1 for r in fleet if r.get("has_key") is False),
    }


def _days(days: int | None) -> int:
    """Период запроса, зажатый в разумное: 0/None -> дефолт, максимум = ретеншен."""
    if not days or days < 1:
        return settings.admin_default_days
    return min(days, max(1, settings.log_retention_days))


@app.get("/admin/kiosks")
async def admin_kiosks(token: str = None, days: int = None):
    """Состояние флота для админки: кто жив, кто отключён, сколько спрашивали."""
    _require_admin(token)
    return _fleet_payload(_days(days))


@app.get("/admin/stats")
async def admin_stats(token: str = None, days: int = None, kiosk: str = None,
                      top: int = 20):
    """Сводка для раздела «Обзор»: та же арифметика, что у CLI-отчёта."""
    _require_admin(token)
    period = _days(days)
    rows = analytics.filter_rows(analytics.load(settings.log_dir, period),
                                 kiosk=_clean_kiosk(kiosk))
    top = max(1, min(top, 100))
    return {
        "days": period,
        "kiosk": _clean_kiosk(kiosk),
        "summary": analytics.summarize(rows, settings.admin_slow_ms),
        "by_kiosk": analytics.by_kiosk(rows),
        "by_lang": analytics.by_lang(rows, settings.admin_slow_ms),
        "by_day": analytics.by_day(rows),
        # Тексты — ПДн: при admin_logs=false отдаём пустые списки, а страница
        # честно скажет, что тексты выключены (а не «вопросов не было»).
        "top_questions": analytics.top_questions(rows, top) if settings.admin_logs else [],
        "top_unanswered": analytics.top_unanswered(rows, top) if settings.admin_logs else [],
        "texts_enabled": settings.admin_logs,
    }


# Поля журнала: тексты выделены отдельно, чтобы их можно было не отдавать.
_LOG_FIELDS = ("ts", "id", "kiosk", "lang", "answer_found", "suggested",
               "corrected", "print_ids", "provider", "error",
               "stt_ms", "rag_ms", "tts_ms", "tts_first_ms", "total_ms")


def _log_rows(days: int, kiosk: str | None, only: str | None,
              q: str | None) -> list[dict]:
    rows = analytics.filter_rows(analytics.load(settings.log_dir, days),
                                 kiosk=kiosk, only=only, search=q)
    return analytics.newest_first(rows)


def _trim(rec: dict) -> dict:
    out = {k: rec.get(k) for k in _LOG_FIELDS}
    if settings.admin_logs:
        out["question"] = rec.get("question")
        out["answer"] = rec.get("answer")
    return out


@app.get("/admin/interactions")
async def admin_interactions(token: str = None, days: int = None, kiosk: str = None,
                             only: str = None, q: str = None,
                             limit: int = None, offset: int = 0):
    """Журнал обращений: новые сверху, с пагинацией.

    `only` = errors | fallback, `q` — поиск по тексту вопроса/ответа.
    При `admin_logs=false` полей `question`/`answer` в ответе НЕТ вовсе (а не
    пустые строки): страница должна отличать «текстов нет» от «текст пустой».
    """
    _require_admin(token)
    period = _days(days)
    rows = _log_rows(period, _clean_kiosk(kiosk), only, q)
    size = max(1, min(limit or settings.admin_page_size, 500))
    offset = max(0, offset)
    return {
        "days": period,
        "total": len(rows),
        "offset": offset,
        "limit": size,
        "texts_enabled": settings.admin_logs,
        "rows": [_trim(r) for r in rows[offset:offset + size]],
    }


@app.get("/admin/export.csv")
async def admin_export_csv(token: str = None, days: int = None, kiosk: str = None,
                           only: str = None, q: str = None):
    """Тот же отфильтрованный журнал в CSV — для справки руководству.

    utf-8-sig обязателен: без BOM Excel на Windows показывает кириллицу
    кракозябрами (та же грабля, что решена в README архивов киосков).
    """
    _require_admin(token)
    period = _days(days)
    rows = _log_rows(period, _clean_kiosk(kiosk), only, q)
    fields = list(_LOG_FIELDS) + (["question", "answer"] if settings.admin_logs else [])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for rec in rows:
        row = _trim(rec)
        row["print_ids"] = ",".join(row.get("print_ids") or [])
        writer.writerow(row)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = f"ai-dos-{kiosk or 'all'}-{period}d-{stamp}.csv"
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/admin/kiosks")
async def admin_set_kiosk(
    token: str = Form(default=None),
    kiosk: str = Form(...),
    enabled: bool = Form(...),
    message: str = Form(default=""),
):
    """Включить/выключить точку из админки.

    Пишем в тот же `kiosks-disabled.txt`, что правят руками по ssh: два пути
    управления одним состоянием разошлись бы в первый же день.
    """
    _require_admin(token)
    kiosk_id = kiosk if kiosk == kiosks.ALL else _clean_kiosk(kiosk)
    if not kiosk_id:
        raise HTTPException(status_code=400, detail="Не указана точка.")
    try:
        kiosks.set_enabled(kiosk_id, enabled, message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        log.warning("не записал список отключённых киосков: %r", e)
        raise HTTPException(status_code=500, detail="Не удалось сохранить список.")
    log.info("admin: киоск %s -> %s", kiosk_id, "включён" if enabled else "отключён")
    return {"kiosks": kiosks.status_rows(), "maintenance": kiosks.maintenance_message()}


@app.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    data: UploadFile = File(...),
    language: str = Form(default=None),
    correct: bool = Form(default=None),
):
    """Шаг STT: аудио → текст (+ опциональная LLM-коррекция)."""
    audio = await _read_upload(data)
    lang = language or settings.stt_default_language
    token = logging_setup.set_request_id(uuid.uuid4().hex[:8])
    t = perf_counter()
    try:
        try:
            raw = await stt.transcribe(
                audio, data.filename or "audio.wav", lang,
                content_type=data.content_type or "audio/wav",
            )
        except Exception as e:
            # Полную ошибку — в лог; клиенту — обобщённо (в тексте httpx-ошибок ходят
            # внутренние адреса сервисов АФМ, это раскрытие топологии сети).
            log.warning("STT error [%s]: %r", lang, e)
            raise HTTPException(status_code=502, detail="Ошибка распознавания речи (STT).")

        do_correct = service.should_correct(lang) if correct is None else correct
        text = await service.correct_transcript(raw, lang) if do_correct else raw
        log.info("transcribe lang=%s stt=%dms", lang, _ms(t))
        return TranscribeResponse(text=text, language=lang, raw_text=raw)
    finally:
        logging_setup.reset_request_id(token)


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest):
    """Шаг RAG: текст вопроса → ответ строго по базе (внешний RAG-сервис :8077)."""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Пустой вопрос")
    token = logging_setup.set_request_id(uuid.uuid4().hex[:8])
    t = perf_counter()
    try:
        service.check_injection(req.question, req.language)  # только логируем, не блокируем
        try:
            result = await rag.ask(req.question, req.language, with_sources=True)
        except Exception as e:
            log.warning("RAG error [%s]: %r", req.language, e)
            raise HTTPException(status_code=502, detail="Ошибка поиска по базе (RAG).")
        log.info("chat lang=%s rag=%dms", req.language, _ms(t))
        return result
    finally:
        logging_setup.reset_request_id(token)


@app.post("/speak")
async def speak_endpoint(req: SpeakRequest):
    """Шаг TTS: текст → аудио на нужном языке."""
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")
    token = logging_setup.set_request_id(uuid.uuid4().hex[:8])
    t = perf_counter()
    try:
        try:
            audio = await _guarded_synthesize(req.text, req.language)
        except RuntimeError as e:
            # RuntimeError здесь — наши собственные сообщения (провайдер не настроен и
            # т.п.), их можно показать; сетевые ошибки идут ниже и обезличиваются.
            raise HTTPException(status_code=503, detail=str(e))
        except Exception as e:
            log.warning("TTS error [%s]: %r", req.language, e)
            raise HTTPException(status_code=502, detail="Ошибка синтеза речи (TTS).")
        log.info("speak lang=%s tts=%dms chars=%d", req.language, _ms(t), len(req.text))
        return Response(content=audio, media_type=f"audio/{settings.tts_format}")
    finally:
        logging_setup.reset_request_id(token)


_KIOSK_RE = re.compile(r"[^A-Za-z0-9._-]")


def _clean_kiosk(kiosk: str | None) -> str | None:
    """Номер точки из запроса -> безопасная метка для логов (или None).

    Значение приходит от киоска, то есть снаружи: в journald и в JSONL оно
    попадает как есть, поэтому перевод строки в нём подделал бы соседнюю запись
    (log injection), а длинная строка раздула бы каждую строку аналитики.
    Оставляем буквы/цифры/`._-` и режем до 32 символов — этого хватает на
    `astana-01` и на `%COMPUTERNAME%`."""
    if not kiosk:
        return None
    cleaned = _KIOSK_RE.sub("", kiosk)[:32]
    return cleaned or None


def _require_admin(token: str | None) -> None:
    """Админка живёт на киоск-странице, а её видят все 20 регионов на :80.

    Пустой ADMIN_TOKEN = админки нет вовсе (404, а не 401): не подсказываем, что
    тут вообще есть что открывать. Сравнение через compare_digest — токен
    короткий, и перебор по времени ответа тут не нужен никому.
    """
    if not settings.admin_token:
        raise HTTPException(status_code=404, detail="Not Found")
    if not token or not secrets.compare_digest(token, settings.admin_token):
        raise HTTPException(status_code=403, detail="Неверный токен администратора.")


def _new_record(rid: str, lang: str, kiosk: str | None = None) -> dict:
    """Заготовка строки аналитики: собирается по ходу, пишется один раз в finally
    (в т.ч. на пути ошибки — видно, какая стадия упала и сколько заняла)."""
    return {
        "request_id": rid, "lang": lang, "kiosk": _clean_kiosk(kiosk),
        "provider": tts._provider_for(lang),
        "corrected": False, "suggested": False, "print_ids": [], "answer_found": None,
        "question": None, "answer": None,
        "stt_ms": None, "rag_ms": None, "tts_ms": None, "tts_first_ms": None,
        "error": None,
    }


async def _answer_pipeline(audio: bytes, filename: str, content_type: str,
                           lang: str, suggest: str | None, rec: dict
                           ) -> tuple[str, str, str | None, list[str]]:
    """STT → RAG → уточнение/печать. Возвращает (вопрос, ответ, подсказка, бланки).

    Общая часть `/voice` и `/voice/stream`: различаются они только тем, КАК
    отдают озвучку (одним WAV или потоком кусков), а путь до текста — один.
    Звать ПОД семафором _tts_sem: следом идёт синтез.
    """
    t = perf_counter()  # стадия «речь → текст» = STT (+ опц. LLM-коррекция)
    try:
        question = await stt.transcribe(audio, filename, lang, content_type=content_type)
        if service.should_correct(lang):
            corrected = await service.correct_transcript(question, lang)
            rec["corrected"] = corrected != question
            question = corrected
    except Exception as e:
        rec["error"] = "stt"
        log.warning("STT error [%s]: %r", lang, e)
        raise HTTPException(status_code=502, detail="Ошибка распознавания речи (STT).")
    rec["stt_ms"] = _ms(t)

    if not question.strip():
        rec["error"] = "empty"
        raise HTTPException(status_code=400,
                            detail="Не удалось распознать речь. Повторите вопрос.")

    # Движок STT на шуме уходит в петлю («Елбасы, елбасы, елбасы…» ×50 в логах
    # киоска 29.07). Схлопываем повторы ДО всего остального: и поиск идёт по
    # осмысленному тексту, и на экране вопрос вместо простыни, и в аналитике
    # читаемая строка. Если после схлопывания видно, что это была именно петля,
    # в RAG не идём вовсе — просим повторить (аватар это проговорит).
    # ⚠️ Оцениваем ИСХОДНЫЙ текст: после схлопывания петля выглядит нормальным
    # коротким вопросом, и проверка бы не срабатывала никогда.
    degenerate = service.looks_degenerate(question) or service.looks_not_speech(question)
    question = service.collapse_repeats(question)
    if degenerate:
        rec["question"] = question
        # НЕ answer_found=False: базу мы не спрашивали вовсе. Иначе шум с киоска
        # («Продолжение следует.» на тишине) попадал в долю «нет в базе» и в
        # список «чем пополнять базу» — то есть портил ровно тот отчёт, ради
        # которого этот список и нужен (найдено на живой странице 30.07: 66,7%
        # «нет в базе», из них две записи — артефакт и петля).
        rec["error"] = "noise"
        answer = service.not_recognized_phrase(lang)
        rec["answer"] = answer
        log.info("STT-петля: вопрос не распознан, отвечаем просьбой повторить")
        # Гражданину вопрос НЕ показываем (страница при пустом q строку не
        # рисует): «Продолжение следует.» над просьбой повторить выглядит так,
        # будто киоск это услышал, и человек начинает думать, что сказал не то.
        # В аналитике текст остаётся (rec["question"] выше) — по нему и видно,
        # что движок выдумывает на тишине, и какие артефакты добавлять в фильтр.
        return "", answer, None, []

    # «Да» в ответ на наше «возможно, вы хотели спросить …?» — отвечаем на
    # исправленный вопрос. Любая другая реплика — обычный новый вопрос.
    suggest_used = False
    if suggest and service.is_affirmative(question):
        question = suggest
        suggest_used = True
    rec["question"] = question

    service.check_injection(question, lang)  # только логируем, не блокируем

    t = perf_counter()
    try:
        # sources не нужны в озвучке: with_sources=True заставил бы RAG делать
        # второй запрос к графу/LLM на каждый вопрос — лишняя задержка.
        result = await rag.ask(question, lang, with_sources=False)
    except Exception as e:
        rec["error"] = "rag"
        log.warning("RAG error [%s]: %r", lang, e)
        raise HTTPException(status_code=502, detail="Ошибка поиска по базе (RAG).")
    rec["rag_ms"] = _ms(t)

    answer = result["answer"]
    # Нашёл ли RAG ответ — фиксируем ДО подмены answer подсказкой ниже.
    rec["answer_found"] = not service.looks_not_found(answer)

    # RAG не нашёл ответа — вероятно, STT исказил вопрос. Предлагаем гражданину
    # исправленную формулировку (доп. LLM-вызов ТОЛЬКО на пути отказа). После
    # подтверждённой подсказки повторно не уточняем — иначе цикл уточнений.
    suggestion = None
    if not suggest_used and service.looks_not_found(answer):
        suggestion = await service.suggest_question(question, lang)
        if suggestion:
            answer = service.clarify_phrase(suggestion, lang)
    rec["suggested"] = bool(suggestion)

    # Печать образцов: ответ про подачу заявления/жалобы/приём → предлагаем
    # распечатать бланк. Приглашение ДОПИСЫВАЕМ в ответ (аватар проговорит +
    # уйдёт в поле answer на экран), id образцов — в поле print. НЕ предлагаем
    # на пути уточнения (suggestion — там ещё не ответ по существу) и на пути
    # ОТКАЗА (answer_found=False): фраза-отказ сама содержит «подать обращение
    # через e-Otinish», иначе бланк предлагался бы на любой неизвестный/off-topic
    # вопрос (напр. «кто субъекты Аргентины» → печать заявления — бессмыслица).
    print_ids: list[str] = []
    if not suggestion and rec["answer_found"]:
        print_ids = service.detect_print_templates(answer)
        if print_ids:
            answer = service.with_print_offer(answer, lang)
    rec["print_ids"] = print_ids
    rec["answer"] = answer
    return question, answer, suggestion, print_ids


@app.post("/voice")
async def voice_endpoint(
    request: Request,
    data: UploadFile = File(...),
    language: str = Form(default=None),
    suggest: str = Form(default=None),
    # Номер точки (20 киосков в пилоте) — только метка для логов, на ответ не влияет.
    kiosk: str = Form(default=None),
    # Пропуск точки: без него `?id=` — просто подпись, и отключённый регион
    # обходит рубильник, убрав параметр из ярлыка (см. kiosks.key_ok).
    key: str = Form(default=None),
):
    """Полный пайплайн: аудио → STT → RAG+LLM → (TTS). Ответ — ОДНИМ JSON.

    Тело: {question, answer, suggest, print, provider, format, audio_b64} —
    текст и аудио (base64) в теле, не в заголовках: длинный табличный ответ
    упирался в лимит заголовка, и экран тихо оставался без текста (N5).

    `suggest` — подсказка с ПРОШЛОГО ответа: если STT исказил вопрос и RAG не
    нашёл ответа, мы предлагаем исправленную формулировку; страница шлёт её со
    следующим вопросом, и реплика-согласие («да», «иә») означает «отвечай на
    исправленный вопрос».

    Озвучка отдаётся ЦЕЛИКОМ: гражданин ждёт полного синтеза. Потоковый вариант
    (первый звук через ~1.5 с вместо ~6.5) — `/voice/stream`.
    """
    # request-id связывает все строки лога одного обращения (STT/RAG/TTS/ошибки).
    rid = uuid.uuid4().hex[:8]
    audio = await _read_upload(data)  # может дать 413 ДО старта пайплайна — не логируем
    lang = language or settings.stt_default_language
    rec = _new_record(rid, lang, kiosk)
    token = logging_setup.set_request_id(rid)
    t_start = perf_counter()
    try:
        _gate(request, rec, key)
        # Вопрос = точка жива, даже если она старой версии и не умеет пинговать.
        kiosks.touch_ask(rec["kiosk"])
        # Точка отключена оператором — разворачиваем ДО STT/RAG/TTS: платить за
        # облако и держать слот семафора ради отказа незачем. Обращение при этом
        # логируется (error=disabled): полезно видеть, что у погашенной точки
        # всё-таки стоят люди.
        blocked = kiosks.disabled_message(rec["kiosk"])
        if blocked:
            rec["error"] = "disabled"
            raise HTTPException(status_code=503, detail=blocked)
        # /voice — самый дорогой путь; ограничиваем число одновременных, чтобы пачка
        # запросов не положила TTS/GPU-ноду. Лишние ждут очереди (не отвергаются).
        async with _tts_sem:
            question, answer, suggestion, print_ids = await _answer_pipeline(
                audio, data.filename or "audio.wav",
                data.content_type or "audio/wav", lang, suggest, rec)

            audio_b64 = None
            if settings.tts_enabled:
                t = perf_counter()
                try:
                    out_audio, used_provider = await tts.synthesize_with_provider(answer, lang)
                except Exception as e:
                    rec["error"] = "tts"
                    log.warning("TTS error [%s]: %r", lang, e)
                    raise HTTPException(status_code=502, detail="Ошибка синтеза речи (TTS).")
                rec["tts_ms"] = _ms(t)
                # Провайдер мог смениться фолбэком (облако недоступно -> Spark):
                # и в аналитику, и на страницу должен уйти ФАКТИЧЕСКИЙ — иначе
                # киоск включит темп чужого движка (медленный Spark на 1.0).
                rec["provider"] = used_provider
                audio_b64 = base64.b64encode(out_audio).decode("ascii")

            return {
                "question": question,
                "answer": answer,
                # null, если не на пути уточнения; страница вернёт это полем `suggest`.
                "suggest": suggestion,
                # [] или список образцов (fl/ul/priem) — страница строит меню печати.
                "print": print_ids,
                # ФАКТИЧЕСКИЙ TTS-провайдер этого ответа (после возможного
                # фолбэка): страница подбирает по нему темп проговаривания
                # (eleven vs spark), см. video_ui (N7).
                "provider": rec["provider"],
                "format": settings.tts_format,
                # base64-WAV; null, если TTS выключен (страница покажет только текст).
                "audio_b64": audio_b64,
            }
    finally:
        rec["total_ms"] = _ms(t_start)
        logging_setup.record_interaction(**rec)
        logging_setup.reset_request_id(token)


@app.post("/voice/stream")
async def voice_stream_endpoint(
    request: Request,
    data: UploadFile = File(...),
    language: str = Form(default=None),
    suggest: str = Form(default=None),
    kiosk: str = Form(default=None),
    # Пропуск точки: без него `?id=` — просто подпись, и отключённый регион
    # обходит рубильник, убрав параметр из ярлыка (см. kiosks.key_ok).
    key: str = Form(default=None),
):
    """То же, что `/voice`, но ответ идёт ПОТОКОМ — NDJSON, по строке на событие.

    Зачем: TTS доминирует в задержке (замер: ответ на 1000 символов — ~6.5 с
    синтеза на F5, ~20 с на Spark), а гражданин у киоска всё это время слушает
    тишину. Синтез идёт быстрее реального времени, поэтому достаточно дождаться
    ПЕРВОГО куска — остальные успевают досинтезироваться, пока звучит предыдущий.

        {"type":"meta","question":…,"answer":…,"suggest":…,"print":[…],
         "provider":…,"format":"wav","chunks":N}      ← сразу после RAG, текст на экран
        {"type":"audio","seq":0,"provider":…,"chars":…,"audio_b64":…}
        …
        {"type":"end"}                                 ← или {"type":"error","detail":…}

    Ошибки ДО первого байта (STT/RAG) — обычные HTTP-коды, как у `/voice`.
    После начала потока статус уже отправлен, поэтому сбой синтеза приходит
    строкой `error`: текст ответа на экране у гражданина уже есть.
    """
    rid = uuid.uuid4().hex[:8]
    audio = await _read_upload(data)
    lang = language or settings.stt_default_language
    rec = _new_record(rid, lang, kiosk)
    token = logging_setup.set_request_id(rid)
    t_start = perf_counter()

    def _finish() -> None:
        rec["total_ms"] = _ms(t_start)
        logging_setup.record_interaction(**rec)

    try:
        _gate(request, rec, key)
    except HTTPException:
        rec["error"] = "gate"
        _finish()
        logging_setup.reset_request_id(token)
        raise
    kiosks.touch_ask(rec["kiosk"])
    # Тот же рубильник, что и в /voice: до потока ошибка отдаётся обычным HTTP.
    blocked = kiosks.disabled_message(rec["kiosk"])
    if blocked:
        rec["error"] = "disabled"
        _finish()
        logging_setup.reset_request_id(token)
        raise HTTPException(status_code=503, detail=blocked)

    # Семафор держим на ВЕСЬ путь, включая поток синтеза, поэтому берём его
    # руками: освобождает генератор в finally (Starlette закрывает генератор и
    # при обрыве соединения — гражданин ушёл от киоска, ресурс не залипает).
    await _tts_sem.acquire()
    try:
        question, answer, suggestion, print_ids = await _answer_pipeline(
            audio, data.filename or "audio.wav",
            data.content_type or "audio/wav", lang, suggest, rec)
    except BaseException:
        _tts_sem.release()
        _finish()
        logging_setup.reset_request_id(token)
        raise
    logging_setup.reset_request_id(token)  # дальше работает генератор — со своим контекстом

    async def events():
        # Starlette крутит тело потока ОТДЕЛЬНОЙ задачей, то есть в другом
        # контексте: токен эндпоинта здесь сбросить нельзя (ValueError), поэтому
        # request-id ставим заново — иначе строки лога синтеза потеряют связь с
        # обращением. Сбрасывать не нужно: контекст задачи умрёт вместе с ней.
        logging_setup.set_request_id(rid)
        try:
            # Движок зависит от языка ОТВЕТА, а не от переключателя киоска
            # (гражданин мог спросить по-русски в казахском режиме). Киоск берёт
            # из `provider` темп проигрывания, поэтому предсказание в meta должно
            # совпадать с тем, чем реально озвучим.
            rec["provider"] = tts.provider_for_text(answer, lang)
            meta = {
                "type": "meta", "question": question, "answer": answer,
                "suggest": suggestion, "print": print_ids,
                "provider": rec["provider"], "format": settings.tts_format,
                # Символов в озвучиваемом тексте (после нормализации и вырезания
                # экранных таблиц). Караоке-подсветка в потоке не может опираться
                # на длительность — общая известна только в конце, — поэтому
                # считает прогресс по доле символов: тут знаменатель, в кусках
                # (`chars`) — слагаемые.
                "speech_chars": len(tts.prepare_for_tts(answer, lang)[0]),
            }
            yield json.dumps(meta, ensure_ascii=False) + "\n"
            if not settings.tts_enabled:
                yield json.dumps({"type": "end"}) + "\n"
                return
            t = perf_counter()
            try:
                async for seq, chunk, provider, chars in tts.synthesize_stream(answer, lang):
                    if seq == 0:
                        # Воспринимаемая задержка: сколько гражданин ждал ЗВУКА.
                        rec["tts_first_ms"] = _ms(t)
                    rec["provider"] = provider
                    yield json.dumps({
                        "type": "audio", "seq": seq, "provider": provider,
                        "chars": chars,
                        "audio_b64": base64.b64encode(chunk).decode("ascii"),
                    }) + "\n"
            except Exception as e:
                rec["error"] = "tts"
                log.warning("TTS stream error [%s]: %r", lang, e)
                yield json.dumps({"type": "error",
                                  "detail": "Ошибка синтеза речи (TTS)."}) + "\n"
                return
            rec["tts_ms"] = _ms(t)
            yield json.dumps({"type": "end"}) + "\n"
        finally:
            _tts_sem.release()
            _finish()

    # X-Accel-Buffering: nginx перед киоском иначе копит ответ целиком и съедает
    # весь смысл потока.
    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store",
                                      "X-Accel-Buffering": "no"})
