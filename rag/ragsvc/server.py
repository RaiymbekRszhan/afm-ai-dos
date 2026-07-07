"""
HTTP-обёртка для аватара.

Поток: микрофон --STT--> текст --POST /ask--> текст --TTS--> динамик.

Запуск:
    uvicorn ragsvc.server:app --host 0.0.0.0 --port 8077
(или python -m ragsvc.server)
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel, Field

from . import config
from .rag_engine import build_rag, answer, get_sources, _not_found

_state: dict = {}


def _norm_lang(lang: str | None) -> str | None:
    """Приводит код языка к 'ru' | 'kk' | None. Неизвестный код -> None (LightRAG
    ответит на языке вопроса), а не молча ломает языковую логику."""
    if not lang:
        return None
    low = lang.strip().lower()
    if low in ("ru", "kk"):
        return low
    if low.startswith(("kk", "kz", "kaz", "қаз", "каз")):
        return "kk"
    if low.startswith(("ru", "рус")):
        return "ru"
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["rag"] = await build_rag()  # один раз на старте
    yield
    _state.clear()


app = FastAPI(title="AFM Digital Officer RAG", lifespan=lifespan)


class Ask(BaseModel):
    # max_length — защита от DoS/мегабайтного текста в эмбеддинг-сервис.
    question: str = Field(..., max_length=config.MAX_QUESTION_CHARS)
    lang: str | None = None        # "ru" | "kk" | None (язык кнопки на экране)
    with_sources: bool = False


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "naive", "rerank": config.RERANK_ENABLED}


@app.post("/ask")
async def ask(req: Ask):
    rag = _state["rag"]
    question = req.question.strip()
    lang = _norm_lang(req.lang)
    if not question:
        # Пустой вопрос (например, мусорный STT): сразу фраза-отказ, без ретрива/LLM.
        return {"answer": _not_found(lang), "sources": ""}
    text = await answer(rag, question, lang)
    out = {"answer": text}
    if req.with_sources:
        out["sources"] = await get_sources(rag, question, lang)
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ragsvc.server:app", host=config.HOST, port=config.PORT)
