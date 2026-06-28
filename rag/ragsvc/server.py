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
from pydantic import BaseModel

from . import config
from .rag_engine import build_rag, answer, get_sources

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    _state["rag"] = await build_rag()  # один раз на старте
    yield
    _state.clear()


app = FastAPI(title="AFM Digital Officer RAG", lifespan=lifespan)


class Ask(BaseModel):
    question: str
    lang: str | None = None        # "ru" | "kk" | None (язык кнопки на экране)
    with_sources: bool = False


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "naive", "rerank": config.RERANK_ENABLED}


@app.post("/ask")
async def ask(req: Ask):
    rag = _state["rag"]
    text = await answer(rag, req.question, req.lang)
    out = {"answer": text}
    if req.with_sources:
        out["sources"] = await get_sources(rag, req.question, req.lang)
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("ragsvc.server:app", host=config.HOST, port=config.PORT)
