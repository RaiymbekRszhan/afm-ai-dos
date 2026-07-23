"""
Адаптеры к вашему серверу АФМ (OpenAI-совместимый vLLM).

LightRAG вызывает две функции:
  - llm_model_func(prompt, system_prompt, history_messages, ...)  -> str
  - embedding_func(texts: list[str])                              -> np.ndarray

Здесь мы оборачиваем готовые хелперы LightRAG, подставляя ваши base_url/model.
"""
from __future__ import annotations

import contextvars
import logging
import re

import numpy as np
from lightrag.utils import EmbeddingFunc
from lightrag.llm.openai import openai_complete_if_cache, openai_embed

from . import config

log = logging.getLogger("ragsvc")

# Язык ТЕКУЩЕГО вопроса (ru|kk|None). LightRAG зовёт rerank-функцию только с
# (query, documents) — язык туда не передать параметром, поэтому прокидываем
# через ContextVar: он task-local (безопасен при параллельных запросах asyncio),
# ставится в rag_engine перед aquery. Нужен локальному языковому дедупу ниже.
CURRENT_LANG: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "afm_current_lang", default=None
)


# ---------------------------------------------------------------------------
# LLM: генеративная модель на сервере АФМ
# ---------------------------------------------------------------------------
async def llm_model_func(
    prompt: str,
    system_prompt: str | None = None,
    history_messages: list | None = None,
    keyword_extraction: bool = False,
    **kwargs,
) -> str:
    return await openai_complete_if_cache(
        config.LLM_MODEL,
        prompt,
        system_prompt=system_prompt,
        history_messages=history_messages or [],
        base_url=config.LLM_BASE_URL,
        api_key=config.LLM_API_KEY,
        # детерминизм важнее «красоты»: меньше шанс выдумать
        temperature=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Embedding: qwen3-embedding-8b на сервере АФМ
# ---------------------------------------------------------------------------
async def _embed(texts: list[str]) -> np.ndarray:
    # ВАЖНО: вызываем .func (необёрнутую функцию), а НЕ сам openai_embed.
    # openai_embed задекорирован @wrap_embedding_func_with_attrs(embedding_dim=1536)
    # под OpenAI text-embedding-3-small. Прямой вызов проверял бы размерность
    # qwen3-embedding-8b (4096) против хардкода 1536 и падал бы
    # "Embedding dimension mismatch (4096 vs 1536)". Размерность валидирует наш
    # EmbeddingFunc ниже (config.EMBEDDING_DIM). .func сохраняет @retry.
    return await openai_embed.func(
        texts,
        model=config.EMBEDDING_MODEL,
        base_url=config.EMBEDDING_BASE_URL,
        api_key=config.EMBEDDING_API_KEY,
    )


embedding_func = EmbeddingFunc(
    embedding_dim=config.EMBEDDING_DIM,
    max_token_size=config.EMBEDDING_MAX_TOKENS,
    func=_embed,
)


# ---------------------------------------------------------------------------
# Реранк-хук LightRAG (process_chunks_unified → apply_rerank_if_enabled).
# Контракт: rerank_func(query, documents: list[str], top_n) -> список
# [{"index": i, "relevance_score": s}]; LightRAG по index берёт исходный чанк,
# фильтрует по min_rerank_score (дефолт 0.5) и усекает до chunk_top_k.
#
# Два независимых слоя:
#   1) внешний bge-reranker-v2-m3 (RERANK_ENABLED=1, нужен живой RERANK_BASE_URL);
#   2) локальный языковой дедуп (LANG_DEDUP=1, БЕЗ внешнего сервиса).
# Пока сервис реранка не развёрнут — работает только дедуп.
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"^\s*\[(RU|KK)\]")


def _doc_text(d) -> str:
    if isinstance(d, str):
        return d
    if isinstance(d, dict):
        return d.get("content") or d.get("text") or ""
    return str(d)


def _doc_lang(d) -> str | None:
    m = _TAG_RE.match(_doc_text(d))
    return m.group(1).lower() if m else None


async def rerank_func(query: str, documents: list, top_n: int | None = None, **kwargs):
    """Внешний reranker (Cohere-совместимый /rerank). Включается RERANK_ENABLED=1.
    Требует непустой RERANK_BASE_URL — иначе падаем ГРОМКО, а не шлём в пустоту."""
    import httpx

    if not config.RERANK_BASE_URL:
        raise RuntimeError(
            "RERANK_ENABLED=1, но RERANK_BASE_URL пуст. Укажи эндпоинт "
            "bge-reranker-v2-m3 в rag/.env или выключи RERANK_ENABLED."
        )
    payload = {
        "model": config.RERANK_MODEL,
        "query": query,
        "documents": [_doc_text(d) for d in documents],
    }
    # Ретрив расширен до TOP_K (chunk_top_k=TOP_K в rag_engine), поэтому САМИ
    # усекаем до CHUNK_TOP_K — иначе LightRAG усечёт по chunk_top_k=TOP_K и в
    # контекст уйдёт весь пул. top_n просим у сервиса тоже CHUNK_TOP_K.
    payload["top_n"] = config.CHUNK_TOP_K
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{config.RERANK_BASE_URL}/rerank",
            json=payload,
            headers={"Authorization": f"Bearer {config.RERANK_API_KEY}"},
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    # На всякий случай сортируем по убыванию релевантности и режем top-k
    # (не полагаемся на то, что сервис уже отсортировал/усёк).
    results = [x for x in results if isinstance(x, dict) and "index" in x]
    results.sort(key=lambda x: x.get("relevance_score", 0.0), reverse=True)
    return results[: config.CHUNK_TOP_K]


async def lang_dedup_rerank(query: str, documents: list, top_n: int | None = None, **kwargs):
    """Локальный «реранкер» без внешнего сервиса: чанки ЯЗЫКА ВОПРОСА ставит выше
    зеркальных иноязычных дублей и усекает до CHUNK_TOP_K. Так top-k заполняется
    РАЗНЫМИ нормами нужного языка, а не ru+kk-копиями одной статьи (иначе половина
    бюджета — дубли, и в казахский ответ протекают русские слова). При нехватке
    одноязычных чанков добираем иноязычными — recall не теряем. Возврат — в
    index-формате контракта LightRAG; скоры в [0.6, 1.0] держим выше дефолтного
    порога min_rerank_score (0.5), чтобы отбор делали ПОРЯДОК и усечение, а не
    случайная отсечка синтетического скора."""
    n = len(documents)
    if n == 0:
        return []
    lang = CURRENT_LANG.get()
    order = list(range(n))
    if lang in ("ru", "kk"):
        same = [i for i in order if _doc_lang(documents[i]) == lang]
        other = [i for i in order if _doc_lang(documents[i]) != lang]
        order = same + other  # стабильно: порядок ретрива внутри групп сохранён
    keep = config.CHUNK_TOP_K if config.CHUNK_TOP_K > 0 else n
    order = order[:keep]
    denom = max(len(order) - 1, 1)
    return [
        {"index": i, "relevance_score": round(1.0 - 0.4 * (rank / denom), 4)}
        for rank, i in enumerate(order)
    ]


def get_rerank():
    """Какую реранк-функцию отдать LightRAG. Внешний bge (если развёрнут и включён)
    строго сильнее — он и релевантность поднимает, и зеркальные дубли демотит; тогда
    локальный дедуп не нужен. Иначе — локальный языковой дедуп."""
    if config.RERANK_ENABLED:
        return rerank_func
    if config.LANG_DEDUP:
        return lang_dedup_rerank
    return None
