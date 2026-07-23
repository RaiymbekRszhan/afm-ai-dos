"""
Сборка LightRAG под АФМ и две функции, которые дёргает бэкенд аватара:
  - answer(question, lang)   -> чистый текст для TTS
  - get_sources(question)    -> какие документы использованы (для экрана/логов)
"""
from __future__ import annotations

import logging
import re

from lightrag import LightRAG, QueryParam
from lightrag.kg.shared_storage import initialize_pipeline_status

from . import config
from .providers import llm_model_func, embedding_func, get_rerank, CURRENT_LANG
from .prompts import AFM_SYSTEM_PROMPT, NOT_FOUND_KK, NOT_FOUND_RU

log = logging.getLogger("ragsvc")


async def build_rag() -> LightRAG:
    config.assert_ready()
    rag = LightRAG(
        working_dir=config.WORKING_DIR,
        llm_model_func=llm_model_func,
        embedding_func=embedding_func,
        rerank_model_func=get_rerank(),
        chunk_token_size=config.CHUNK_TOKEN_SIZE,
        chunk_overlap_token_size=config.CHUNK_OVERLAP,
        # Явно (было неявным дефолтом библиотеки) — порог антигаллюцинации
        # ретривала, см. комментарий у config.COSINE_THRESHOLD.
        cosine_better_than_threshold=config.COSINE_THRESHOLD,
        # Кэш ответов LLM. Ключ кэша LightRAG НЕ учитывает версию базы, поэтому
        # после переингеста он отдавал бы устаревшие ответы. По умолчанию выкл
        # (LLM_CACHE в config) — «строго по актуальной базе» важнее скорости.
        enable_llm_cache=config.LLM_CACHE,
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
    if hasattr(rag, "_process_extract_entities"):
        rag._process_extract_entities = _skip_kg
    else:
        # API LightRAG изменился — патч не применился. Без него ingest снова начнёт
        # звать LLM на каждый чанк (таймауты). Громко предупреждаем, не молчим.
        log.warning("LightRAG: метод _process_extract_entities не найден — "
                    "пропуск извлечения сущностей НЕ применён (сверь версию lightrag).")
    return rag


def _query_param(lang: str | None) -> QueryParam:
    hint = "Вопрос задан на русском, отвечай на русском." if lang == "ru" else (
        "Сұрақ қазақ тілінде, қазақ тілінде жауап бер." if lang == "kk" else
        "Отвечай на том же языке, что и вопрос."
    )
    # Реранк-хук включаем, если жив внешний реранкер ЛИБО локальный языковой
    # дедуп. Тогда же расширяем ретрив: naive тянет ровно chunk_top_k кандидатов
    # (search_top_k = chunk_top_k or top_k в LightRAG), а дедупу/реранку нужен
    # ПУЛ (TOP_K), из которого отобрать РАЗНЫЕ нормы — сами усекут до CHUNK_TOP_K
    # (providers). Без обоих — прежнее поведение: тянем ровно CHUNK_TOP_K.
    hook_on = config.RERANK_ENABLED or config.LANG_DEDUP
    chunk_top_k = config.TOP_K if hook_on else config.CHUNK_TOP_K
    return QueryParam(
        mode="naive",                      # только векторный поиск
        enable_rerank=hook_on,
        include_references=False,          # ссылки не нужны в озвучке
        top_k=config.TOP_K,
        chunk_top_k=chunk_top_k,
        user_prompt=hint,
    )


_CLEANUP = re.compile(r"[#*`]+")
# Служебная языковая метка чанка (chunking.chunk_file) — LLM иногда цитирует её
# буквально вместе с фактом из чужеязычного куска; в устном ответе ей не место.
_LANG_TAG = re.compile(r"\[(?:RU|KK)\]\s*")

# LightRAG при НУЛЕВОЙ выдаче поиска (пустой/мусорный вопрос) короткозамыкает и
# возвращает свою английскую заглушку с маркером [no-context] — БЕЗ вызова LLM,
# минуя наш промпт. В озвучку её пускать нельзя (гражданин услышит английский).
_LC_FALLBACK_RE = re.compile(r"\[no-context\]|sorry,?\s+i'?m not able to provide",
                             re.IGNORECASE)


def _not_found(lang: str | None) -> str:
    return NOT_FOUND_KK if lang == "kk" else NOT_FOUND_RU


def _for_tts(text: str) -> str:
    """Подчищаем остатки markdown/служебных секций, чтобы TTS читал гладко."""
    text = re.split(r"\n#{1,6}\s*References", text)[0]
    text = re.split(r"\n###\s*Источники", text)[0]
    text = _CLEANUP.sub("", text)
    text = _LANG_TAG.sub("", text)
    return text.strip()


async def answer(rag: LightRAG, question: str, lang: str | None = None) -> str:
    # Язык вопроса — в task-local ContextVar: его читает языковой дедуп в
    # реранк-хуке (LightRAG туда язык не пробрасывает). См. providers.CURRENT_LANG.
    CURRENT_LANG.set(lang)
    resp = await rag.aquery(
        question,
        param=_query_param(lang),
        system_prompt=AFM_SYSTEM_PROMPT,
    )
    if resp is None:
        # aquery вернул None — запрос внутри LightRAG упал (см. логи сервиса).
        raise RuntimeError("LightRAG вернул пустой результат на запрос")
    if isinstance(resp, str):
        text = _for_tts(resp)
    else:
        # на случай stream=True — собираем
        chunks = [c async for c in resp]
        text = _for_tts("".join(chunks))
    if not text:
        # Пустой ответ (LLM вернул "" — напр. finish_reason не length, а просто
        # пустое содержимое) не матчит ни _LC_FALLBACK_RE, ни service.looks_not_found
        # на стороне оркестратора — раньше это доходило до tts.synthesize("") и
        # падало 502 "Ошибка синтеза речи", вместо вежливого отказа с номером 1458.
        return _not_found(lang)
    if _LC_FALLBACK_RE.search(text):
        # Заменяем англ. заглушку LightRAG фирменной фразой отказа на языке вопроса.
        return _not_found(lang)
    return text


_REF_HEADER = re.compile(r"Reference Document List[^\n]*:\s*", re.IGNORECASE)
_REF_LINE = re.compile(r"^\s*\[\d+\]\s*(.+?)\s*$", re.MULTILINE)


async def get_sources(rag: LightRAG, question: str, lang: str | None = None) -> str:
    """Какие нормы подтянулись — компактным списком меток для экрана/логов.

    Парсим секцию «Reference Document List» из контекста (там наши метки-цитаты
    из file_paths) вместо возврата всего дампа графа знаний.
    """
    CURRENT_LANG.set(lang)  # язык для дедупа в реранк-хуке (как в answer)
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
