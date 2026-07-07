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

# Reranker
RERANK_ENABLED = _b("RERANK_ENABLED")
RERANK_BASE_URL = os.getenv("RERANK_BASE_URL", "")
RERANK_MODEL = os.getenv("RERANK_MODEL", "bge-reranker-v2-m3")
RERANK_API_KEY = os.getenv("RERANK_API_KEY", "dummy")

# RAG — только векторный поиск (naive). Граф знаний не используем.
WORKING_DIR = os.getenv("WORKING_DIR", "./rag_storage")
CHUNK_TOKEN_SIZE = _int("CHUNK_TOKEN_SIZE", "1200")
CHUNK_OVERLAP = _int("CHUNK_OVERLAP", "100")
# Сколько кандидатов тянуть из поиска и сколько чанков отдавать в LLM-контекст.
TOP_K = _int("TOP_K", "40")
CHUNK_TOP_K = _int("CHUNK_TOP_K", "12")
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
