"""
Сборка LightRAG под АФМ и две функции, которые дёргает бэкенд аватара:
  - answer(question, lang)   -> чистый текст для TTS
  - get_sources(question)    -> какие документы использованы (для экрана/логов)
"""
from __future__ import annotations

import re

from lightrag import LightRAG, QueryParam
from lightrag.kg.shared_storage import initialize_pipeline_status

from . import config
from .providers import llm_model_func, embedding_func, get_rerank
from .prompts import AFM_SYSTEM_PROMPT


async def build_rag() -> LightRAG:
    config.assert_ready()
    rag = LightRAG(
        working_dir=config.WORKING_DIR,
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        rerank_model_func=get_rerank(),
        chunk_token_size=config.CHUNK_TOKEN_SIZE,
        chunk_overlap_token_size=config.CHUNK_OVERLAP,
        # параллелизм при загрузке/запросах
        llm_model_max_async=config.LLM_MAX_ASYNC,
        max_parallel_insert=config.MAX_PARALLEL_INSERT,
        default_llm_timeout=config.LLM_TIMEOUT,
    )
    await rag.initialize_storages()
    await initialize_pipeline_status()
    # Используем ТОЛЬКО векторный поиск (граф знаний не нужен). Отключаем извлечение
    # сущностей при загрузке: иначе LightRAG зовёт LLM на КАЖДЫЙ чанк (на медленном
    # LLM АФМ — таймауты и брошенные чанки). Чанки всё равно идут в векторное хранилище.
    async def _skip_kg(chunk, pipeline_status=None, pipeline_status_lock=None):
        return []
    rag._process_extract_entities = _skip_kg
    return rag


def _query_param(lang: str | None) -> QueryParam:
    hint = "Вопрос задан на русском, отвечай на русском." if lang == "ru" else (
        "Сұрақ қазақ тілінде, қазақ тілінде жауап бер." if lang == "kk" else
        "Отвечай на том же языке, что и вопрос."
    )
    return QueryParam(
        mode="naive",                      # только векторный поиск
        enable_rerank=config.RERANK_ENABLED,
        include_references=False,          # ссылки не нужны в озвучке
        top_k=config.TOP_K,
        chunk_top_k=config.CHUNK_TOP_K,
        user_prompt=hint,
    )


_CLEANUP = re.compile(r"[#*`]+")


def _for_tts(text: str) -> str:
    """Подчищаем остатки markdown/служебных секций, чтобы TTS читал гладко."""
    text = re.split(r"\n#{1,6}\s*References", text)[0]
    text = re.split(r"\n###\s*Источники", text)[0]
    text = _CLEANUP.sub("", text)
    return text.strip()


async def answer(rag: LightRAG, question: str, lang: str | None = None) -> str:
    resp = await rag.aquery(
        question,
        param=_query_param(lang),
        system_prompt=AFM_SYSTEM_PROMPT,
    )
    if resp is None:
        # aquery вернул None — запрос внутри LightRAG упал (см. логи сервиса).
        raise RuntimeError("LightRAG вернул пустой результат на запрос")
    if isinstance(resp, str):
        return _for_tts(resp)
    # на случай stream=True — собираем
    chunks = [c async for c in resp]
    return _for_tts("".join(chunks))


_REF_HEADER = re.compile(r"Reference Document List[^\n]*:\s*", re.IGNORECASE)
_REF_LINE = re.compile(r"^\s*\[\d+\]\s*(.+?)\s*$", re.MULTILINE)


async def get_sources(rag: LightRAG, question: str, lang: str | None = None) -> str:
    """Какие нормы подтянулись — компактным списком меток для экрана/логов.

    Парсим секцию «Reference Document List» из контекста (там наши метки-цитаты
    из file_paths) вместо возврата всего дампа графа знаний.
    """
    p = _query_param(lang)
    p.only_need_context = True
    ctx = await rag.aquery(question, param=p, system_prompt=AFM_SYSTEM_PROMPT)
    if not isinstance(ctx, str):
        return ""
    tail = _REF_HEADER.split(ctx, maxsplit=1)
    block = tail[1] if len(tail) > 1 else ctx
    labels: list[str] = []
    for m in _REF_LINE.finditer(block):
        label = m.group(1).strip().strip("`")
        if label and label not in labels:
            labels.append(label)
    return "; ".join(labels)
