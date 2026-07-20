"""Ai-dos — киоск-страница «видео-аватар» (:8100), выбранный фронтенд пилота.

Пока тихо — крутится idle-ролик (аватар стоит); задали вопрос — играет WAV
живого ответа (STT→RAG→TTS с основного бэкенда) и параллельно немой ролик
«аватар говорит», зациклённый на длину звука. Липсинк не нужен (решение
руководителя). Поверх — таблицы и графики из базы, QR-коды порталов,
караоке-подсветка, увеличение по тапу (см. static/).

Почему отдельное приложение на своём порту (:8100):
  * бэкенд (:8000) остаётся чистым пайплайном без киоск-логики;
  * вопросы ПРОКСИРУЕМ на :8000 (см. /voice) — свой STT/RAG/TTS не поднимаем;
  * прокси (а не прямой запрос из браузера на :8000) нужен, чтобы страница и её
    фетчи были same-origin: иначе в бэкенд пришлось бы добавлять CORS.

Запуск: bash run_api.sh (весь стек) или bash video_ui/run.sh (только страница).
"""

import os

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_DIR, "static")
_VIDEO_DIR = os.path.join(_STATIC_DIR, "video")

# Рабочий бэкенд (:8000), куда проксируем вопросы.
BACKEND = os.environ.get("AIDOS_BACKEND", "http://localhost:8000").rstrip("/")
# /voice — самый долгий путь (STT + RAG + TTS). На GPU ~секунды, на CPU-TTS — минуты,
# поэтому таймаут щедрый: лучше подождать, чем оборвать ответ на демонстрации.
BACKEND_TIMEOUT = float(os.environ.get("AIDOS_BACKEND_TIMEOUT", "300"))

app = FastAPI(title="Ai-dos — киоск «видео-аватар»", docs_url=None, redoc_url=None)


@app.middleware("http")
async def _no_stale_assets(request, call_next):
    """Страница и её код (html/js/css) — всегда свежие, без ручной очистки кэша.

    StaticFiles не шлёт Cache-Control, поэтому браузер эвристически кэшировал
    старый answer_render.js и по F5 его НЕ перечитывал — правки (QR над аватаром)
    на киоске «не применялись». `no-cache` заставляет браузер каждый раз
    сверяться с сервером (ETag → 304, если не менялся: дёшево, файлы локальные) и
    подхватывать новую версию сразу. Ролики (.mp4) НЕ трогаем — они тяжёлые,
    пусть кэшируются, иначе перекачивались бы на каждой загрузке страницы."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


def _videos_present() -> dict:
    """Лежат ли ролики на месте — частая причина «чёрного экрана» на демо.

    Сверяем имена ТОЧНО (listdir), а не через os.path.exists: на macOS файловая
    система регистро-независимая, и exists("idle.mp4") вернёт True для лежащего
    рядом "Idle.mp4". На Linux регистр важен — браузер получит 404, и проверка,
    прошедшая на маке, промолчала бы ровно там, где ломается (сервер АФМ)."""
    try:
        real = set(os.listdir(_VIDEO_DIR))
    except OSError:
        real = set()
    return {name: name in real for name in ("idle.mp4", "talk.mp4")}


@app.get("/", include_in_schema=False)
async def root():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Иконка вкладки — чтобы браузер не сыпал 404 в лог. Берём ту же, что и
    основной вариант (:8000), иначе 204 «пусто, и не спрашивай больше»."""
    path = os.path.join(os.path.dirname(_DIR), "app", "static", "swagger-ui",
                        "favicon-32x32.png")
    if os.path.exists(path):
        return FileResponse(path, media_type="image/png")
    return Response(status_code=204)


@app.get("/health")
async def health():
    """Жив ли демо-сервер, виден ли ему рабочий бэкенд и на месте ли ролики."""
    backend_reachable = False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            backend_reachable = (await c.get(BACKEND + "/health")).status_code == 200
    except Exception:
        pass
    return {"status": "ok", "backend": BACKEND,
            "backend_reachable": backend_reachable, "videos": _videos_present()}


@app.post("/voice")
async def voice(data: UploadFile = File(...), language: str = Form("russian"),
                suggest: str = Form(default=None)):
    """Вопрос → рабочий бэкенд (:8000) → WAV ответа + текст в заголовках.

    Тело и заголовки прокидываем как есть: контракт /voice бэкенда —
    WAV + percent-encoded X-Question/X-Answer (X-Suggest —
    подсказка «возможно, вы хотели спросить…», страница вернёт её полем suggest
    со следующим вопросом)."""
    audio = await data.read()
    files = {"data": (data.filename or "q.wav", audio, data.content_type or "audio/wav")}
    form = {"language": language}
    if suggest:
        form["suggest"] = suggest
    try:
        async with httpx.AsyncClient(timeout=BACKEND_TIMEOUT) as c:
            r = await c.post(BACKEND + "/voice", files=files, data=form)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"Рабочий бэкенд {BACKEND} недоступен: {e!r}")
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text[:200])
        except Exception:
            detail = r.text[:200]
        raise HTTPException(status_code=r.status_code, detail=detail)
    headers = {}
    for h in ("x-question", "x-answer", "x-suggest", "x-print"):
        if r.headers.get(h):
            headers[h.title()] = r.headers[h]
    return Response(content=r.content,
                    media_type=r.headers.get("content-type", "audio/wav"),
                    headers=headers)
