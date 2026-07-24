"""Логирование Ai-dos: ops-логи в journald + аналитика по /voice в JSONL.

Два независимых потока (см. DEPLOY.md, раздел «Логи и аналитика»):

    ai_dos.*          -> stderr -> journald (systemd)   # ошибки, тайминги, request-id
    ai_dos.analytics  -> logs/interactions.jsonl        # по строке на /voice
                         TimedRotatingFileHandler(when=midnight,
                         backupCount=log_retention_days) -> старые суточные файлы
                         сами удаляются = ретеншен ПДн граждан.

Оркестратор — единственная точка, видящая весь путь STT->RAG->TTS одного
гражданина, поэтому вся interaction-аналитика собирается здесь.

Приватность (настройки log_questions/log_answers): текст вопроса можно писать
полностью, хешировать или не писать; текст ответа — по флагу. См. config.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from app.config import settings

# request-id текущего запроса — проставляется в /voice и попадает во ВСЕ строки
# лога этого запроса (в т.ч. warning'и из клиентов stt/rag/tts), чтобы стадии
# одного обращения можно было связать в journald.
_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_OPS_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s"
# Помечаем свои хендлеры, чтобы configure_logging была идемпотентной (тесты
# переконфигурируют на временный каталог, повторный вызов не должен плодить хендлеры).
_MARK = "_ai_dos_handler"

analytics = logging.getLogger("ai_dos.analytics")


class _RequestIdFilter(logging.Filter):
    """Проставляет record.request_id из ContextVar — иначе формат падает на KeyError."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = _request_id_var.get()
        return True


def _clear_our_handlers(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _MARK, False):
            logger.removeHandler(h)
            h.close()


def configure_logging() -> None:
    """Идемпотентно настраивает ops- и analytics-логгеры из settings.

    Вызывается на старте (lifespan). Без этого вызова `ai_dos.*` логгеры видны
    только через last-resort хендлер Python (WARNING+), а INFO/аналитика молчат.
    """
    root = logging.getLogger("ai_dos")
    _clear_our_handlers(root)
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    root.setLevel(level)
    # Свой хендлер + propagate=False: не дублируемся в uvicorn/last-resort.
    stream = logging.StreamHandler()  # stderr -> journald
    stream.setFormatter(logging.Formatter(_OPS_FORMAT))
    stream.addFilter(_RequestIdFilter())
    setattr(stream, _MARK, True)
    root.addHandler(stream)
    root.propagate = False

    # Аналитика: отдельный файл, свой хендлер, НЕ propagate (JSONL не должен
    # попасть в journald-поток, а ops-строки — в JSONL).
    _clear_our_handlers(analytics)
    analytics.setLevel(logging.INFO)
    analytics.propagate = False
    if settings.log_analytics:
        try:
            os.makedirs(settings.log_dir, exist_ok=True)
            path = os.path.join(settings.log_dir, "interactions.jsonl")
            fileh = TimedRotatingFileHandler(
                path, when="midnight", backupCount=max(1, settings.log_retention_days),
                encoding="utf-8", utc=True,
            )
            fileh.setFormatter(logging.Formatter("%(message)s"))  # message УЖЕ json
            setattr(fileh, _MARK, True)
            analytics.addHandler(fileh)
        except OSError as e:
            # Логирование не должно ронять сервис: без файла аналитика просто молчит.
            root.warning("не удалось открыть JSONL-аналитику в %r: %r", settings.log_dir, e)


def set_request_id(rid: str):
    """Ставит request-id в контекст; вернёт token для reset() в finally."""
    return _request_id_var.set(rid)


def reset_request_id(token) -> None:
    _request_id_var.reset(token)


def _redact_question(question: str | None) -> str | None:
    """Применяет политику приватности log_questions к тексту вопроса."""
    if question is None:
        return None
    mode = settings.log_questions
    if mode == "off":
        return None
    if mode == "hash":
        return "sha256:" + hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
    return question  # full


def record_interaction(
    *,
    request_id: str,
    lang: str,
    question: str | None = None,
    answer: str | None = None,
    corrected: bool = False,
    answer_found: bool | None = None,
    suggested: bool = False,
    print_ids: list[str] | None = None,
    provider: str | None = None,
    stt_ms: int | None = None,
    rag_ms: int | None = None,
    tts_ms: int | None = None,
    total_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Пишет одну строку аналитики (JSONL) + человекочитаемую ops-строку.

    Вызывается один раз на /voice (в т.ч. на пути ошибки). Приватность вопроса —
    через log_questions; ответ пишется только при log_answers.
    """
    ops = logging.getLogger("ai_dos.api")
    # Человекочитаемая строка в journald (быстро глазами по journalctl).
    ops.info(
        "interaction lang=%s stt=%sms rag=%sms tts=%sms total=%sms "
        "found=%s suggest=%s print=%s provider=%s error=%s",
        lang, stt_ms, rag_ms, tts_ms, total_ms,
        answer_found, int(suggested), ",".join(print_ids or []) or "-",
        provider or "-", error or "-",
    )
    if not settings.log_analytics:
        return
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "id": request_id,
        "lang": lang,
        "question": _redact_question(question),
        "corrected": corrected,
        "answer_found": answer_found,
        "suggested": suggested,
        "print_ids": print_ids or [],
        "provider": provider,
        "stt_ms": stt_ms,
        "rag_ms": rag_ms,
        "tts_ms": tts_ms,
        "total_ms": total_ms,
        "error": error,
    }
    if settings.log_answers:
        rec["answer"] = answer
    analytics.info(json.dumps(rec, ensure_ascii=False))
