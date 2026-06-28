"""
Адаптеры к вашему серверу АФМ (OpenAI-совместимый vLLM).

LightRAG вызывает две функции:
  - llm_model_func(prompt, system_prompt, history_messages, ...)  -> str
  - embedding_func(texts: list[str])                              -> np.ndarray

Здесь мы оборачиваем готовые хелперы LightRAG, подставляя ваши base_url/model.
"""
from __future__ import annotations

import numpy as np
from lightrag.utils import EmbeddingFunc
from lightrag.llm.openai import openai_complete_if_cache, openai_embed

from . import config


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
# Reranker (опционально). bge-reranker-v2-m3 поднимает точность поиска
# по юридическим текстам заметно — рекомендуется, когда дойдут руки.
# ---------------------------------------------------------------------------
async def rerank_func(query: str, documents: list, top_n: int | None = None, **kwargs):
    """Заглушка под ваш будущий reranker-эндпоинт (Cohere-совместимый /rerank).
    Включается флагом RERANK_ENABLED=1 в .env."""
    import httpx

    payload = {
        "model": config.RERANK_MODEL,
        "query": query,
        "documents": [d.get("content", d) if isinstance(d, dict) else d for d in documents],
    }
    if top_n:
        payload["top_n"] = top_n
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{config.RERANK_BASE_URL}/rerank",
            json=payload,
            headers={"Authorization": f"Bearer {config.RERANK_API_KEY}"},
        )
        r.raise_for_status()
        return r.json().get("results", [])


def get_rerank():
    return rerank_func if config.RERANK_ENABLED else None
