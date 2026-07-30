"""Конфигурация: читаем всё из .env, ничего не хардкодим."""
import os
from dotenv import load_dotenv

load_dotenv()


def _b(key: str, default: str = "0") -> bool:
    return os.getenv(key, default).strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: str) -> int:
    """int из env с понятной ошибкой (какая переменная кривая), а не голым ValueError."""
    raw = os.getenv(key, default).strip()
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"Переменная окружения {key}={raw!r} должна быть целым числом.")


def _float(key: str, default: str) -> float:
    raw = os.getenv(key, default).strip()
    try:
        return float(raw)
    except ValueError:
        raise RuntimeError(f"Переменная окружения {key}={raw!r} должна быть числом.")


# Embedding
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "http://192.168.165.2:8806/v1")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding-8b")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "dummy")
EMBEDDING_DIM = _int("EMBEDDING_DIM", "4096")
EMBEDDING_MAX_TOKENS = _int("EMBEDDING_MAX_TOKENS", "8192")

# LLM
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "dummy")

# Reranker (внешний bge-reranker-v2-m3, Cohere-совместимый /rerank).
RERANK_ENABLED = _b("RERANK_ENABLED")
RERANK_BASE_URL = os.getenv("RERANK_BASE_URL", "")
RERANK_MODEL = os.getenv("RERANK_MODEL", "bge-reranker-v2-m3")
RERANK_API_KEY = os.getenv("RERANK_API_KEY", "dummy")

# Языковой дедуп выдачи (локальный, БЕЗ внешнего сервиса). Ставит чанки языка
# вопроса выше зеркальных иноязычных дублей и усекает до CHUNK_TOP_K — top-k
# заполняется РАЗНЫМИ нормами нужного языка, а не ru+kk-копиями одной статьи.
# Работает через тот же реранк-хук LightRAG (providers.lang_dedup_rerank); когда
# включён внешний RERANK_ENABLED, он строго сильнее и заменяет дедуп. Чтобы хук
# получил ШИРЕ пул кандидатов (иначе naive тянет лишь CHUNK_TOP_K и дедупить
# нечего), при включённом дедупе/реранке ретрив расширяется до TOP_K (см.
# rag_engine._query_param). Выключить: LANG_DEDUP=0.
LANG_DEDUP = _b("LANG_DEDUP", "1")

# Объём устного ответа. Пусто = берём prompts.RESPONSE_TYPE (2–4 предложения,
# до 400 символов). Строка отсюда его ЗАМЕЩАЕТ — нужна для двух вещей:
#   * замерить «до/после» на eval, не трогая код: RAG_RESPONSE_TYPE="Multiple
#     Paragraphs" возвращает прежнее поведение (дефолт LightRAG);
#   * откатить длину ответа на бою одной строкой в .env, если короткий ответ
#     где-то теряет важное условие, — без выкатки кода.
RESPONSE_TYPE_OVERRIDE = os.getenv("RAG_RESPONSE_TYPE", "").strip()

# RAG — только векторный поиск (naive). Граф знаний не используем.
WORKING_DIR = os.getenv("WORKING_DIR", "./rag_storage")
CHUNK_TOKEN_SIZE = _int("CHUNK_TOKEN_SIZE", "1200")
CHUNK_OVERLAP = _int("CHUNK_OVERLAP", "100")
# Сколько кандидатов тянуть из поиска и сколько чанков отдавать в LLM-контекст.
TOP_K = _int("TOP_K", "40")
CHUNK_TOP_K = _int("CHUNK_TOP_K", "12")
# Минимальное косинусное сходство, ниже которого чанк даже не попадёт в контекст
# LLM. Раньше не переопределялся — держался на дефолте самого LightRAG (0.2),
# неявно. Задан явно здесь (тот же дефолт 0.2 — поведение не меняем, но теперь
# это видимый, настраиваемый порог антигаллюцинации, а не спрятанный в библиотеке).
# Если вопрос гражданина ложится на лексику чужой статьи, но не по смыслу, порог
# 0.2 может протащить её в контекст — единственная защита тогда системный промпт.
# Поднимать осторожно и только после проверки на rag/eval (см. dataset.yaml).
COSINE_THRESHOLD = _float("RAG_COSINE_THRESHOLD", "0.2")
# Предел длины вопроса (символов): защита от DoS и от ухода мегабайтного текста
# в эмбеддинг-сервис. Обычный вопрос гражданина — десятки/сотни символов.
MAX_QUESTION_CHARS = _int("MAX_QUESTION_CHARS", "2000")
# Кэш ответов LLM. По умолчанию ВЫКЛ: ключ кэша LightRAG не привязан к версии базы,
# поэтому после переингеста (обновления закона) он продолжал бы отдавать старый
# ответ. Для «строго по актуальной базе» это критично. LLM_CACHE=1 — включить.
LLM_CACHE = _b("LLM_CACHE", "0")

# Параллелизм/таймаут. При загрузке зовётся только эмбеддинг (LLM не дёргается —
# граф отключён); LLM_MAX_ASYNC ограничивает одновременные LLM-вызовы при ЗАПРОСЕ.
LLM_MAX_ASYNC = _int("LLM_MAX_ASYNC", "4")
MAX_PARALLEL_INSERT = _int("MAX_PARALLEL_INSERT", "3")  # документов параллельно при загрузке
LLM_TIMEOUT = _int("LLM_TIMEOUT", "240")               # таймаут одного LLM-вызова (сек)

# Server. По умолчанию слушаем localhost: единственный клиент — оркестратор
# (обычно на той же машине). Для отдельной ноды поставь HOST=0.0.0.0.
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _int("PORT", "8077")


def assert_ready():
    missing = []
    if not LLM_MODEL:
        missing.append("LLM_MODEL")
    if not LLM_BASE_URL:
        missing.append("LLM_BASE_URL")
    if missing:
        raise RuntimeError(
            "Не заполнены обязательные параметры в .env: "
            + ", ".join(missing)
            + ". Узнай эндпоинт и имя генеративной модели у команды АФМ."
        )
