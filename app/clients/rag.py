"""Клиент RAG-сервиса (отдельный FastAPI на :8077, свой venv).

Контракт: POST /ask {question, lang, with_sources} -> {answer, sources}.
Ответ формируется СТРОГО по базе кодексов/законов РК — без выдумывания.
RAG-сервис озвучивается голосовым слоем, но живёт отдельно (lightrag/torch
конфликтуют с зависимостями голосового слоя), поэтому общаемся по HTTP.
"""
from urllib.parse import urlparse, urlunparse

import httpx

from app.config import settings


def _resolve_lang(language: str | None) -> str:
    """Нормализует язык ('russian'/'kazakh'/'kk'/...) в 'ru' | 'kk' для RAG."""
    lang = (language or settings.stt_default_language or "russian").lower()
    if lang.startswith(("kaz", "kk", "kz", "қаз", "каз")):
        return "kk"
    return "ru"


async def ask(question: str, language: str | None = None,
              with_sources: bool = True) -> dict:
    """Вопрос -> ответ по базе. Возвращает {'answer': str, 'sources': str}."""
    payload = {
        "question": question,
        "lang": _resolve_lang(language),
        "with_sources": with_sources,
    }
    async with httpx.AsyncClient(timeout=settings.rag_timeout) as client:
        resp = await client.post(settings.rag_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return {"answer": data.get("answer", ""), "sources": data.get("sources", "")}


def _health_url() -> str:
    """Адрес /health RAG-сервиса, выведенный из rag_url (.../ask -> .../health).

    Через urlparse (а не rsplit): устойчиво и к URL без пути (http://host:8077).
    """
    p = urlparse(settings.rag_url)
    return urlunparse(p._replace(path="/health", params="", query="", fragment=""))


async def healthy() -> dict:
    """Быстрый пинг RAG-сервиса для /health оркестратора."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(_health_url())
            resp.raise_for_status()
            return {"reachable": True, **resp.json()}
    except Exception as e:
        return {"reachable": False, "error": str(e)}
